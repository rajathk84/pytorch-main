# mypy: allow-untyped-defs
import collections
import copy
import dataclasses
import inspect
import logging
import threading
from collections import defaultdict
from typing import Any, Dict, List, Optional, Union

import torch
import torch.fx as fx
import torch.utils._pytree as pytree
from torch import Tensor
from torch._C import DispatchKey
from torch._ops import HigherOrderOperator
from torch._prims_common import clone_preserve_strides
from torch._subclasses.fake_tensor import FakeTensorMode
from torch.fx.experimental.proxy_tensor import (
    disable_proxy_modes_tracing,
    ProxyTorchDispatchMode,
    track_tensor_tree,
)


log = logging.getLogger("torch._dynamo")


###############################################################################
# Kernel Side Table


# We cannot put Triton Kernels into the FX graph as the graph nodes
# do not support arbitrary functions.
# Use a side table.
# We use two dicts so that fetching both the kernel and id are O(1)
class KernelSideTable:
    id_to_kernel: Dict[int, Any] = {}
    kernel_to_id: Dict[Any, int] = {}
    constant_args: Dict[int, Any] = {}
    lock = threading.Lock()

    # Returns index on the table
    def add_kernel(self, kernel) -> int:
        with self.lock:
            if kernel in self.kernel_to_id:
                return self.kernel_to_id[kernel]

            idx = len(self.id_to_kernel)
            self.id_to_kernel[idx] = kernel
            self.kernel_to_id[kernel] = idx
            return idx

    # Returns the triton kernel at the given index
    def get_kernel(self, idx: int):
        # No need to lock here as fetching from dict is atomic
        assert idx in self.id_to_kernel
        return self.id_to_kernel[idx]

    # Not every constant arg can be added to the graph. Use this side table
    # for constant args.
    def add_constant_args(self, args) -> int:
        with self.lock:
            idx = len(self.constant_args)
            self.constant_args[idx] = args
            return idx

    # Returns the constant args
    def get_constant_args(self, idx: int):
        # No need to lock here as fetching from dict is atomic
        assert idx in self.constant_args
        return self.constant_args[idx]

    # Resets the table (only meant to be used in unit tests)
    # This is only safe assuming single threaded execution
    def reset_table(self) -> None:
        self.id_to_kernel = {}
        self.kernel_to_id = {}
        self.constant_args = {}


kernel_side_table = KernelSideTable()


###############################################################################
# Mutation Tracker


@dataclasses.dataclass(frozen=True)
class Param:
    idx: int


@dataclasses.dataclass(frozen=True)
class Intermediate:
    idx: int

    def fake(self):
        return self.idx < 0


@dataclasses.dataclass(frozen=True)
class Op:
    name: str
    fn_call_name: Optional[str]
    args: List[Union[Param, Intermediate]]
    ret: Intermediate = dataclasses.field(repr=False)

    def __post_init__(self):
        if self.name == "tt.call":
            assert self.fn_call_name is not None
        else:
            assert self.fn_call_name is None


def generate_ttir(kernel, kwargs):
    """
    Uses Triton's internal code generation to create TTIR
    """
    import sympy
    import triton
    from triton.compiler.compiler import ASTSource
    from triton.runtime.autotuner import Autotuner
    from triton.runtime.jit import JITFunction

    import torch
    import torch._inductor.ir
    from torch._subclasses.fake_tensor import FakeTensor

    if isinstance(kernel, Autotuner):
        if len(kernel.configs) > 0:
            # If we are autotuning, then it doesn't matter which version gets
            # picked for tracing purposes, so lets pick the first one
            kwargs = {**kwargs, **kernel.configs[0].kwargs}
        kernel = kernel.fn

    assert isinstance(kernel, JITFunction)

    if len(kwargs) != len(kernel.arg_names):
        raise ValueError("Incorrect number of arguments passed to kernel")

    # Replace all SymExprs with a regular value for TTIR generation
    # Replace all FakeTensor/TensorBox with real tensors
    # These replacements are needed for triton's type, key and config functions
    ordered_args: Dict[str, Any] = {}
    for name in kernel.arg_names:
        a = kwargs[name]
        if isinstance(a, (torch.SymInt, torch.SymFloat, torch.SymBool, sympy.Expr)):
            ordered_args[name] = 2
        elif isinstance(a, (FakeTensor, torch._inductor.ir.TensorBox)):
            with torch._C._DisableTorchDispatch():
                ordered_args[name] = torch.empty(2, dtype=a.dtype)
        else:
            ordered_args[name] = a

    ordered_tensor_names = [
        name for name, arg in ordered_args.items() if isinstance(arg, Tensor)
    ]
    specialization = kernel._get_config(*ordered_args.values())
    constants = {
        name: arg for name, arg in ordered_args.items() if not isinstance(arg, Tensor)
    }

    # Build kernel signature -- doesn't include constexpr arguments.
    signature = {
        name: kernel._type_of(kernel._key_of(arg))
        for i, (name, arg) in enumerate(ordered_args.items())
        if i not in kernel.constexprs
    }

    context = triton._C.libtriton.ir.context()
    target = triton.runtime.driver.active.get_current_target()
    backend = triton.compiler.compiler.make_backend(target)
    options = backend.parse_options({})
    triton._C.libtriton.ir.load_dialects(context)
    backend.load_dialects(context)

    src = ASTSource(kernel, signature, constants, specialization)

    # Triton changes ASTSource.make_ir to take 3/4 arguments. Handle
    # backward compatibility here.
    make_ir_sig_params = len(inspect.signature(src.make_ir).parameters)
    if make_ir_sig_params == 2:
        ttir_module = src.make_ir(options, context)
    elif make_ir_sig_params == 3:
        codegen_fns = backend.get_codegen_implementation()
        ttir_module = src.make_ir(options, codegen_fns, context)
    else:
        codegen_fns = backend.get_codegen_implementation()
        module_map = backend.get_module_map()
        ttir_module = src.make_ir(options, codegen_fns, module_map, context)
    if not ttir_module.verify():
        raise RuntimeError("Verification for TTIR module has failed")

    return ttir_module, ordered_tensor_names


def ttir_to_functions(ttir_module) -> Dict[str, Dict[Intermediate, List[Op]]]:
    """
    Walk the `ttir_module` bottom up to mine the `functions` from
    the structured MLIR entities representing the Triton kernel
    (mlir::Operation, mlir::Block, mlir::Region).
    """
    functions: Dict[str, Dict[Intermediate, List[Op]]] = {}

    # block id --> op result (Intermediate) --> one or more ops
    op_stack: Dict[int, Dict[Intermediate, List[Op]]] = defaultdict(
        lambda: defaultdict(list)
    )
    region_id_to_block_ids: Dict[int, List[int]] = defaultdict(list)
    block_id_to_block_arg_ids: Dict[int, List[int]] = {}
    replacements: Dict[int, Union[Intermediate, Param]] = {}
    reindex_map: Dict[int, int] = {}
    next_fake_intermediate = 0

    def reindex(idx):
        if idx not in reindex_map:
            reindex_map[idx] = len(reindex_map)
        return reindex_map[idx]

    def mlir_to_functions(op) -> None:
        name: str = op.get_name()
        if name == "builtin.module":
            # this wraps all tt.func ops
            return

        operand_ids: List[int] = [
            reindex(op.get_operand(i).id()) for i in range(op.get_num_operands())
        ]
        result_ids: List[int] = [
            reindex(op.get_result(i).id()) for i in range(op.get_num_results())
        ]

        child_block_ids: List[int] = []
        for i in [op.get_region(i).id() for i in range(op.get_num_regions())]:
            # as the walk is bottom-up, the region_id_to_block_ids[i]
            # must be populated by the time we process the enclosing op
            child_block_ids.extend(region_id_to_block_ids[i])

        parent_block_id = -1
        parent_block = op.get_block()
        if parent_block is not None:
            parent_block_id = parent_block.id()
            if parent_block_id not in block_id_to_block_arg_ids:
                block_id_to_block_arg_ids[parent_block_id] = []
                for i in range(parent_block.get_num_arguments()):
                    block_id_to_block_arg_ids[parent_block_id].append(
                        reindex(parent_block.get_argument(i).id()),
                    )
                # the region info is collected via ops' parent blocks to be
                # used later when the region's encloding op is traversed
                parent_region = parent_block.get_parent()
                if parent_region is not None:
                    region_id_to_block_ids[parent_region.id()].append(parent_block_id)

        nonlocal next_fake_intermediate

        if name == "tt.func":
            # for function ops: gather and inline
            # the ops from all child blocks
            fn_ops = defaultdict(list)
            for child_block_id in child_block_ids:
                for result, block_fn_ops in op_stack.pop(child_block_id).items():
                    for block_fn_op in block_fn_ops:
                        fn_ops[result].append(block_fn_op)

            # replace the corresponding Intermediates in the
            # child op args with the function args (Params)
            for i, idx in enumerate(block_id_to_block_arg_ids[child_block_ids[0]]):
                replacements[idx] = Param(i)

            for fn_op_list in fn_ops.values():
                for fn_op in fn_op_list:
                    for i in range(len(fn_op.args)):
                        arg = fn_op.args[i]
                        seen = set()  # to break cycles
                        # there can be transitive replacements, but likely
                        # no cycles (we keep the `seen` set just in case)
                        while (
                            isinstance(arg, Intermediate)
                            and arg.idx in replacements
                            and arg.idx not in seen
                        ):
                            seen.add(arg.idx)
                            arg = fn_op.args[i] = replacements[arg.idx]

            # next function capture starts
            # with empty replacements
            replacements.clear()

            fn_name = op.get_str_attr("sym_name")
            functions[fn_name] = fn_ops
        elif child_block_ids:
            if name in {"scf.if", "scf.for", "scf.while", "tt.reduce", "tt.scan"}:
                # for blocked ops: inline the enclosed ops into
                # the parent block + rewire the last op in each
                # child block to return the block result
                return_ops = []
                for block_id in child_block_ids:
                    if name == "scf.for":
                        # example:
                        # %result = scf.for %iv = %lb to %ub step %step iter_args(%arg = %init) -> (i32) ...
                        # block args: 2 (%iv, %arg)
                        # op operands: 4 (%lb, %ub, %step, %init)
                        # `%arg` is mapping to `%init`
                        for i, idx in enumerate(block_id_to_block_arg_ids[block_id]):
                            if i == 0:
                                next_fake_intermediate -= 1
                                replacements[idx] = Intermediate(next_fake_intermediate)
                            else:
                                replacements[idx] = Intermediate(operand_ids[i + 2])
                    elif name == "scf.while":
                        # example:
                        # %3:3 = scf.while (%arg2 = %1, %arg3 = %2, %arg4 = %c0_i32_8) ...
                        # block args: 3 (%arg2, %arg3, %arg4)
                        # op operands: 3 (%1, %2, %c0_i32_8)
                        # `%arg2` is mapping to `%1`, `%arg3` is mapping to `%2`, ...
                        for i, idx in enumerate(block_id_to_block_arg_ids[block_id]):
                            replacements[idx] = Intermediate(operand_ids[i])
                    elif name == "scf.if":
                        # the scf block args are ignored by the pass. but, as they
                        # may be used as operands of the ops inside the block
                        # (and nested blocks inlined in the current block by now),
                        # they are replaced by new fake Intermediates to avoid "this
                        # operand is not returned by any other op in the fn" error
                        # in the downstream analysis
                        for idx in block_id_to_block_arg_ids[block_id]:
                            next_fake_intermediate -= 1
                            replacements[idx] = Intermediate(next_fake_intermediate)
                    else:
                        assert name in ("tt.reduce", "tt.scan")
                        # wire the block arguments to the op arguments
                        num_operands = len(operand_ids)
                        block_arg_ids = block_id_to_block_arg_ids[block_id]
                        assert len(block_arg_ids) == 2 * num_operands, (
                            f"{name} is expected to have twice as "
                            "many block arguments as op arguments: "
                            f"{operand_ids=}, {block_arg_ids=}."
                        )
                        for i, idx in enumerate(block_arg_ids):
                            # for a tt.reduce/tt.scan op with N arguments, the block
                            # arguments comprise N reduced values followed by
                            # N current values corresponding to the N op args
                            replacements[idx] = Intermediate(
                                operand_ids[i % num_operands]
                            )

                    if block_id in op_stack:
                        block_ops = op_stack.pop(block_id)
                        if not block_ops:
                            continue
                        last_ret, last_ops = block_ops.popitem()
                        if all(
                            op.name
                            in ("scf.yield", "tt.reduce.return", "tt.scan.return")
                            for op in last_ops
                        ):
                            # if last_ops are all return ops, treat them separately
                            return_ops.extend(last_ops)
                        else:
                            # otherwise, return last_ops to the block
                            block_ops[last_ret] = last_ops
                        for op_result, child_ops in block_ops.items():
                            op_stack[parent_block_id][op_result].extend(child_ops)

                scf_results = [Intermediate(idx) for idx in result_ids]
                for scf_result in scf_results:
                    for return_op in return_ops:
                        op_stack[parent_block_id][scf_result].append(return_op)
            else:
                raise RuntimeError(
                    f"Unknown blocked function: {name}. Can't capture the TTIR."
                )
        else:
            callee = None
            if name == "tt.call":
                callee = op.get_flat_symbol_ref_attr("callee")
            args: List[Union[Param, Intermediate]] = [
                Intermediate(operand) for operand in operand_ids
            ]
            block_ops = op_stack[parent_block_id]
            if result_ids:
                for result_id in result_ids:
                    res = Intermediate(result_id)
                    block_ops[res].append(Op(name, callee, args, res))
            else:
                next_fake_intermediate -= 1
                fake_res = Intermediate(next_fake_intermediate)
                block_ops[fake_res].append(Op(name, callee, args, fake_res))

    ttir_module.walk(mlir_to_functions)

    return functions


class MemoizeWithCycleCheck:
    def __init__(self, fn):
        self.fn = fn
        self.reset()

    def __call__(self, functions, fn_name, num_args):
        key = (fn_name, num_args)
        if key not in self.cache:
            self.cache[key] = None
            self.cache[key] = self.fn(functions, fn_name, num_args)
        if self.cache[key] is None:
            raise RuntimeError("Recursion is not supported")
        return self.cache[key]

    def reset(self):
        self.cache = {}


@MemoizeWithCycleCheck
def analyze_kernel_mutations(functions, fn_name, num_args):
    """
    Analyzes the graph to detect all sinks from a predefined list of sinks
    by using triton's MemWrite trait list. NOTE: What if triton exposed this?
    From each sink, it traverses the CFG backwards to identify all the input
    pointers that are mutated.
    """
    # Name of mutation op to mutated parameter indices
    # List from Triton Github include/triton/Dialect/Triton/IR/TritonOps.td
    # All the OPs that have MemWrite trait.
    # What if Triton exposed this?
    MUTATION_OPS = {"tt.store": [0], "tt.atomic_cas": [0], "tt.atomic_rmw": [0]}
    # Ops that we want to bail out on
    UNKNOWN_OPS = {"tt.elementwise_inline_asm"}

    stack: List[Union[Param, Intermediate]] = []
    visited = set()
    ops = functions[fn_name]
    for op_list in ops.values():
        for op in op_list:
            if op.name in UNKNOWN_OPS:
                raise RuntimeError(
                    f"ttir analysis hit an op we do not know how to analyze: {op.name}"
                )

            if op.name == "tt.call":
                assert op.fn_call_name in functions
                mutations = analyze_kernel_mutations(
                    functions, op.fn_call_name, len(op.args)
                )
                stack.extend(arg for arg, mutated in zip(op.args, mutations) if mutated)
            else:
                for idx in MUTATION_OPS.get(op.name, []):
                    stack.append(op.args[idx])

    # The following is an iterative DFS algorithm
    mutated = [False] * num_args
    while stack:
        arg = stack.pop()
        if arg in visited:
            continue

        visited.add(arg)

        if isinstance(arg, Param):
            if arg.idx >= num_args:
                # This is an argument defined in the kernel, not passed in
                continue
            mutated[arg.idx] = True
        elif isinstance(arg, Intermediate) and not arg.fake():
            for op in ops[arg]:
                # Skip arguments to load
                if op.name != "tt.load":
                    stack.extend(op.args)
    return mutated


def identify_mutated_tensors(kernel, kwargs):
    """
    Given a triton kernel and the arguments for this kernel, this function
    1) Retrieves the TTIR converted version of the kernel from Triton's API.
    2) Parses the TTIR and creates a control flow graph
    3) Analyzes the graph to detect all input tensor mutations
    """

    ttir_module = None
    functions = None
    try:
        ttir_module, ordered_tensor_names = generate_ttir(kernel, kwargs)

        # extract functions from TTIR using MLIR bindings exposed by Triton code
        functions = ttir_to_functions(ttir_module)

        assert functions is not None
        kernel_name = next(iter(functions.keys()))
        # Triton codegen modifies the name
        assert kernel.fn.__name__ in kernel_name
        # Reset the cache between top level invocations
        # The cache for analyze kernel mutations is mainly used for cycle
        # detection, so each top level invocation needs a clean cache
        analyze_kernel_mutations.reset()
        mutations = analyze_kernel_mutations(
            functions, kernel_name, len(ordered_tensor_names)
        )

        return [
            ordered_tensor_names[i] for i, mutated in enumerate(mutations) if mutated
        ]
    except Exception as e:
        log.warning(
            "Encountered an exception in identify_mutated_tensors, assuming every input is mutated",
            exc_info=True,
        )
        if ttir_module is not None:
            log.debug("TTIR:\n%s", str(ttir_module))
        if functions is not None:
            log.debug("functions:")
            for name, fn in functions.items():
                log.debug("===\t%s\t===", name)
                for ret, ops in fn.items():
                    log.debug("%s\t=>\t%s", ret, ops)
        return [key for key, value in kwargs.items() if isinstance(value, Tensor)]


###############################################################################
# Triton Kernel Wrappers


# Used for wrapping a Triton Kernel
class TritonKernelWrapperMutation(HigherOrderOperator):
    def __init__(self) -> None:
        super().__init__("triton_kernel_wrapper_mutation")

    def __call__(self, kernel_idx, constant_args_idx, grid, kwargs):
        return super().__call__(
            kernel_idx=kernel_idx,
            constant_args_idx=constant_args_idx,
            grid=grid,
            kwargs=kwargs,
        )


triton_kernel_wrapper_mutation = TritonKernelWrapperMutation()


# Used for wrapping a Triton Kernel in a functional manner
class TritonKernelWrapperFunctional(HigherOrderOperator):
    def __init__(self) -> None:
        super().__init__("triton_kernel_wrapper_functional")

    def __call__(self, kernel_idx, constant_args_idx, grid, kwargs, tensors_to_clone):
        return super().__call__(
            kernel_idx=kernel_idx,
            constant_args_idx=constant_args_idx,
            grid=grid,
            kwargs=kwargs,
            tensors_to_clone=tensors_to_clone,
        )


triton_kernel_wrapper_functional = TritonKernelWrapperFunctional()


@triton_kernel_wrapper_mutation.py_impl(DispatchKey.CompositeExplicitAutograd)
def triton_kernel_wrapper_mutation_dense(
    *, kernel_idx, constant_args_idx, grid, kwargs
):
    from torch._inductor.codegen.wrapper import user_defined_kernel_grid_fn_code

    kernel = kernel_side_table.get_kernel(kernel_idx)
    constant_args = kernel_side_table.get_constant_args(constant_args_idx)

    if len(grid) == 1:
        grid_fn = grid[0]
    else:
        fn_name, code = user_defined_kernel_grid_fn_code(
            kernel.fn.__name__, kernel.configs, grid
        )
        namespace: Dict[str, Any] = {}
        exec(code, namespace)
        grid_fn = namespace[fn_name]

    kernel[grid_fn](**kwargs, **constant_args)


@triton_kernel_wrapper_mutation.py_impl(FakeTensorMode)
def triton_kernel_wrapper_mutation_fake_tensor_mode(
    mode, *, kernel_idx, constant_args_idx, grid, kwargs
):
    with mode:
        return None


@triton_kernel_wrapper_mutation.py_impl(DispatchKey.Meta)
def _(*, kernel_idx, constant_args_idx, grid, kwargs):
    return None


def trace_triton_kernel_wrapper(proxy_mode, func_overload, node_args):
    with disable_proxy_modes_tracing():
        out = func_overload(**node_args)

    proxy_args = pytree.tree_map(proxy_mode.tracer.unwrap_proxy, node_args)
    out_proxy = proxy_mode.tracer.create_proxy(
        "call_function",
        func_overload,
        (),
        proxy_args,
        name=func_overload.__name__ + "_proxy",
    )
    ret = track_tensor_tree(out, out_proxy, constant=None, tracer=proxy_mode.tracer)
    return ret


@triton_kernel_wrapper_mutation.py_impl(ProxyTorchDispatchMode)
def triton_kernel_wrapper_mutation_proxy_torch_dispatch_mode(
    mode, *, kernel_idx, constant_args_idx, grid, kwargs
):
    trace_triton_kernel_wrapper(
        mode,
        triton_kernel_wrapper_mutation,
        {
            "kernel_idx": kernel_idx,
            "constant_args_idx": constant_args_idx,
            "grid": grid,
            "kwargs": kwargs,
        },
    )

    return None


def get_mutated_tensors(kernel_idx, constant_args_idx, kwargs):
    kernel = kernel_side_table.get_kernel(kernel_idx)
    constant_args = kernel_side_table.get_constant_args(constant_args_idx)
    return identify_mutated_tensors(kernel, {**kwargs, **constant_args})


@triton_kernel_wrapper_mutation.py_functionalize_impl
def triton_kernel_wrapper_mutation_functionalize(
    ctx, kernel_idx, constant_args_idx, grid, kwargs
):
    unwrapped_kwargs = ctx.unwrap_tensors(kwargs)
    # TODO(oulgen): Preexisting bug, if two kernel inputs are views of each
    # other, and one gets mutated in kernel, and later another gets mutated,
    # they are no longer equal. Fix this by graph breaking on this condition
    # earlier in dynamo.
    tensors_to_clone = get_mutated_tensors(
        kernel_idx, constant_args_idx, unwrapped_kwargs
    )
    with ctx.redispatch_to_next():
        unwrapped_outputs = triton_kernel_wrapper_functional(
            kernel_idx=kernel_idx,
            constant_args_idx=constant_args_idx,
            grid=grid,
            kwargs=unwrapped_kwargs,
            tensors_to_clone=tensors_to_clone,
        )

    assert set(unwrapped_outputs.keys()).issubset(set(kwargs.keys()))
    for key, output_arg in unwrapped_outputs.items():
        if not isinstance(output_arg, Tensor):
            continue
        input_arg = kwargs[key]
        assert isinstance(input_arg, Tensor)

        ctx.replace(input_arg, output_arg)
        # indicate that above replace is hidden from autograd
        ctx.mark_mutation_hidden_from_autograd(input_arg)
        ctx.commit_update(input_arg)
        ctx.sync(input_arg)
    return None


@triton_kernel_wrapper_functional.py_impl(DispatchKey.CompositeExplicitAutograd)
def triton_kernel_wrapper_functional_dense(
    *, kernel_idx, constant_args_idx, grid, kwargs, tensors_to_clone
):
    # TODO(oulgen): For performance reasons, we want to ensure that these
    # `clone_preserve_strides` calls are never executed at runtime
    # (inductor should always optimize them away).
    # Requires https://github.com/pytorch/pytorch/issues/109240
    kwargs = {
        key: (clone_preserve_strides(val) if key in tensors_to_clone else val)
        for key, val in kwargs.items()
    }
    triton_kernel_wrapper_mutation(
        kernel_idx=kernel_idx,
        constant_args_idx=constant_args_idx,
        grid=grid,
        kwargs=kwargs,
    )
    return {key: val for key, val in kwargs.items() if key in tensors_to_clone}


@triton_kernel_wrapper_functional.py_impl(FakeTensorMode)
def triton_kernel_wrapper_functional_fake_tensor_mode(
    mode, *, kernel_idx, constant_args_idx, grid, kwargs, tensors_to_clone
):
    # TODO(oulgen): For performance reasons, we want to ensure that these
    # `clone_preserve_strides` calls are never executed at runtime
    # (inductor should always optimize them away).
    # Requires https://github.com/pytorch/pytorch/issues/109240
    with mode:
        return {
            key: clone_preserve_strides(val)
            for key, val in kwargs.items()
            if key in tensors_to_clone
        }


@triton_kernel_wrapper_functional.py_impl(ProxyTorchDispatchMode)
def triton_kernel_wrapper_functional_proxy_torch_dispatch_mode(
    mode, *, kernel_idx, constant_args_idx, grid, kwargs, tensors_to_clone
):
    return trace_triton_kernel_wrapper(
        mode,
        triton_kernel_wrapper_functional,
        {
            "kernel_idx": kernel_idx,
            "constant_args_idx": constant_args_idx,
            "grid": grid,
            "kwargs": kwargs,
            "tensors_to_clone": tensors_to_clone,
        },
    )


@triton_kernel_wrapper_functional.py_functionalize_impl
def triton_kernel_wrapper_functional_functionalize(
    ctx, kernel_idx, constant_args_idx, grid, kwargs, tensors_to_clone
):
    unwrapped_kwargs = ctx.unwrap_tensors(kwargs)
    with ctx.redispatch_to_next():
        outputs = triton_kernel_wrapper_functional(
            kernel_idx=kernel_idx,
            constant_args_idx=constant_args_idx,
            grid=grid,
            kwargs=unwrapped_kwargs,
            tensors_to_clone=tensors_to_clone,
        )
        return ctx.wrap_tensors(outputs)


triton_kernel_wrapper_mutation.fallthrough(DispatchKey.PythonDispatcher)  # type: ignore[attr-defined]
triton_kernel_wrapper_mutation.fallthrough(DispatchKey.PythonTLSSnapshot)  # type: ignore[attr-defined]
triton_kernel_wrapper_mutation.fallthrough(DispatchKey.ADInplaceOrView)
triton_kernel_wrapper_mutation.fallthrough(DispatchKey.BackendSelect)
triton_kernel_wrapper_mutation.fallthrough(DispatchKey.AutocastCPU)  # type: ignore[attr-defined]
triton_kernel_wrapper_mutation.fallthrough(DispatchKey.AutocastCUDA)  # type: ignore[attr-defined]
triton_kernel_wrapper_mutation.fallthrough(DispatchKey.AutogradCUDA)
triton_kernel_wrapper_mutation.fallthrough(DispatchKey.AutogradCPU)

triton_kernel_wrapper_functional.fallthrough(DispatchKey.PythonDispatcher)  # type: ignore[attr-defined]
triton_kernel_wrapper_functional.fallthrough(DispatchKey.PythonTLSSnapshot)  # type: ignore[attr-defined]
triton_kernel_wrapper_functional.fallthrough(DispatchKey.ADInplaceOrView)
triton_kernel_wrapper_functional.fallthrough(DispatchKey.BackendSelect)
triton_kernel_wrapper_functional.fallthrough(DispatchKey.AutocastCPU)  # type: ignore[attr-defined]
triton_kernel_wrapper_functional.fallthrough(DispatchKey.AutocastCUDA)  # type: ignore[attr-defined]
triton_kernel_wrapper_functional.fallthrough(DispatchKey.AutogradCUDA)
triton_kernel_wrapper_functional.fallthrough(DispatchKey.AutogradCUDA)
triton_kernel_wrapper_functional.fallthrough(DispatchKey.AutogradCPU)


###############################################################################
# The "TritonHOPifier": a class that transforms a call to a triton kernel into
# a call to the triton_kernel_wrapper_mutation HOP.


class TritonHOPifier:
    """Orchestrator for converting a user-defined triton kernel into a call
    to the triton_kernel_wrapper_mutation HOP.

    It has two main use cases.

    1. When Dynamo sees a triton kernel, it wraps it into a TritonKernelVariable
    and uses the TritonHOPifier to convert calls to the TritonKernelVariable
    into a call to the HOP.

    2. In order to capture a user-defined triton kernel while performing
    tracing (via make_fx or non-strict export), a user must annotate their
    triton kernel with the `capture_triton` decorator. The decorator uses
    TritonHOPifier to convert calls to the triton kernel into a call
    to the HOP (which can then be traced).

    Because Dynamo has its own calling conventions for e.g. invoking a user-defined function
    TritonHOPifier is an abstract class that can be overriden by its subclasses.
    """

    def raise_unsupported(self, msg):
        raise NotImplementedError("abstract method")

    def is_callable(self, maybe_callable):
        raise NotImplementedError("abstract method")

    def get_value(self, val):
        raise NotImplementedError("abstract method")

    def call_grid(self, grid, meta, tx):
        raise NotImplementedError("abstract method")

    def call_HOP(self, variable, grids, combined_args, tx):
        raise NotImplementedError("abstract method")

    def check_grid(self, grid):
        raise NotImplementedError("abstract method")

    def init_variable(self, variable, kernel, kernel_idx, grid):
        from triton.runtime.autotuner import Autotuner

        assert kernel is not None

        variable.kernel = kernel
        variable.kernel_idx = kernel_side_table.add_kernel(kernel)

        assert kernel_idx is None or variable.kernel_idx == kernel_idx

        variable.grid = grid

        if isinstance(kernel, Autotuner):
            import torch
            import torch._dynamo

            # We only support configs and keys arguments of triton.autotune
            # Make sure other arguments are defaulted
            defaults = inspect.signature(Autotuner.__init__).parameters

            # Newer version of triton change attribute name from warmup to num_warmup and rep to num_rep.
            # The call to get_first_attr is to maintain backward-compatibility.
            if (
                not torch._inductor.config.unsafe_ignore_unsupported_triton_autotune_args
                and (
                    (
                        "warmup" in defaults
                        and defaults["warmup"].default
                        != torch._dynamo.utils.get_first_attr(
                            kernel, "num_warmups", "warmup"
                        )
                    )
                    or (
                        "rep" in defaults
                        and defaults["rep"].default
                        != torch._dynamo.utils.get_first_attr(kernel, "num_reps", "rep")
                    )
                    or (
                        "prune_configs_by" in defaults
                        and defaults["prune_configs_by"].default
                        != kernel.early_config_prune
                    )
                    # Set via reset_to_zero argument
                    or len(kernel.reset_idx) != 0
                    or len(kernel.restore_idx) != 0
                    or (
                        "use_cuda_graph" in defaults
                        and defaults["use_cuda_graph"].default != kernel.use_cuda_graph
                    )
                )
            ):
                self.raise_unsupported(
                    "Only configs and keys are supported for triton.autotune"
                )

    def call_getitem(self, variable, args):
        # __getitem__ should only be called if we don't already have a grid
        # Only grid needs to be passed
        if variable.grid is not None or len(args) != 1:
            self.raise_unsupported(
                "Triton kernels should be called with only a single grid"
            )

        return type(variable)(
            kernel=variable.kernel,
            kernel_idx=variable.kernel_idx,
            grid=args[0],
        )

    def call_run(self, variable, args, kwargs, tx):
        if "grid" not in kwargs:
            self.raise_unsupported("Triton kernel requires to be called with a grid")
        grid = kwargs.pop("grid")
        kwargs.pop("warmup", None)
        # rewrite kernel.run(*args, grid=grid) to kernel[grid](*args)
        return self.call_triton_kernel(
            type(variable)(
                kernel=variable.kernel, kernel_idx=variable.kernel_idx, grid=grid
            ),
            args,
            kwargs,
            tx,
        )

    def call_triton_kernel(self, variable, args, kwargs, tx):
        from triton.runtime.autotuner import autotune, Autotuner, Config

        if "num_ctas" in kwargs:
            self.raise_unsupported(
                "Passing num_ctas directly to the Triton kernel is not supported. "
                "Please use a Config in @triton.autotune instead."
            )

        special_kwargs = {}
        for name in ("num_warps", "num_stages"):
            if name in kwargs:
                # remove special kwargs from `kwargs`
                val = kwargs.pop(name)
                special_kwargs[name] = self.get_value(val)

        if special_kwargs:
            if isinstance(variable.kernel, Autotuner):
                # if there is Autotuner already, set
                # special kwargs to each of its configs
                new_configs = copy.deepcopy(variable.kernel.configs)
                for config in new_configs:
                    config.__dict__.update(special_kwargs)
                new_kernel = autotune(configs=new_configs, key=[])(variable.kernel.fn)
            else:
                # if there is no Autotuner, wrap the kernel into a
                # new one with a single config with special kwargs
                new_config = Config(kwargs={}, **special_kwargs)
                new_kernel = autotune(configs=[new_config], key=[])(variable.kernel)

            # create a new variable to contain the new (wrapped) kernel;
            # skip kernel_idx to get a new record in the kernel side table
            new_var = type(variable)(new_kernel, None, variable.grid)
            return self.call_triton_kernel(new_var, args, kwargs, tx)

        if variable.grid is None:
            self.raise_unsupported("Triton kernels should always be called with a grid")

        # Both for grid's meta as well as for the kernel, we need combined
        # args and kwargs combined and normalized
        combined_args_raw = {**dict(zip(variable.kernel.arg_names, args)), **kwargs}

        configs = (
            [config.kwargs for config in variable.kernel.configs]
            if isinstance(variable.kernel, Autotuner)
            else [{}]
        )
        grids = []
        for config_args in configs:
            # If the grid is a function, then lets execute it and convert it to
            # a list
            grid = variable.grid
            if self.is_callable(grid):
                # Populate the special "meta" argument to call the grid function
                meta = {**combined_args_raw, **config_args}
                grid = self.call_grid(grid, meta, tx)
            grids.append(self.check_grid(grid))

        for i in range(len(grids)):
            if not isinstance(grids[i], tuple):
                self.raise_unsupported("Only tuple grids are supported")
            # inductor expects all grids to be 3-tuple so lets make it
            if len(grids[i]) == 1:
                grids[i] = (grids[i][0], 1, 1)
            elif len(grids[i]) == 2:
                grids[i] = (grids[i][0], grids[i][1], 1)
            elif len(grids[i]) > 3:
                self.raise_unsupported("Grid can have at most rank 3")

        assert len(grids) != 0

        def intify(x):
            if isinstance(x, torch.SymInt):
                return int(x)
            else:
                return x

        if len(set(pytree.tree_map(intify, grids))) == 1:
            # If there's only one unique grid, lets simplify
            grids = [grids[0]]

        return self.call_HOP(variable, grids, combined_args_raw, tx)


###############################################################################
# Helpers for capture_triton API that makes a user-defined triton kernel traceable into
# a graph via make_fx or non-strict export (coming soon)


class TracingTritonHOPifier(TritonHOPifier):
    def raise_unsupported(self, msg):
        raise RuntimeError(msg)

    def is_callable(self, maybe_callable):
        return callable(maybe_callable)

    def get_value(self, val):
        return val

    def call_grid(self, grid, meta, tx):
        assert tx is None
        return grid(meta)

    def check_grid(self, grid):
        if not isinstance(grid, collections.abc.Sequence):
            raise RuntimeError(
                "capture_triton can only handle grids that resolve to Sequence[int]."
            )
        # normalize to tuple
        return tuple(grid)

    def call_HOP(self, variable, grids, combined_args, tx):
        assert tx is None

        def is_graphable(val):
            return isinstance(val, fx.node.base_types)

        non_graphable_args = {
            k: v for k, v in combined_args.items() if not is_graphable(v)
        }
        graphable_args = {k: v for k, v in combined_args.items() if is_graphable(v)}

        constant_args_idx = kernel_side_table.add_constant_args(non_graphable_args)
        return triton_kernel_wrapper_mutation(
            kernel_idx=variable.kernel_idx,
            constant_args_idx=constant_args_idx,
            grid=grids,
            kwargs=graphable_args,
        )


tracing_triton_hopifier_singleton = TracingTritonHOPifier()


class TraceableTritonKernelWrapper:
    def __init__(self, kernel, kernel_idx, grid):
        self.kernel = None
        self.grid = None
        tracing_triton_hopifier_singleton.init_variable(self, kernel, kernel_idx, grid)
        assert self.kernel is not None

    def __getitem__(self, *args):
        return tracing_triton_hopifier_singleton.call_getitem(self, args)

    def run(self, *args, **kwargs):
        from torch._library.triton import is_capture_triton_enabled

        if is_capture_triton_enabled():
            return tracing_triton_hopifier_singleton.call_run(self, args, kwargs, None)
        else:
            assert self.kernel is not None
            return self.kernel.run(*args, **kwargs)

    def __call__(self, *args, **kwargs):
        from torch._library.triton import is_capture_triton_enabled

        if is_capture_triton_enabled():
            return tracing_triton_hopifier_singleton.call_triton_kernel(
                self, args, kwargs, None
            )
        else:
            assert self.kernel is not None
            return self.kernel[self.grid](*args, **kwargs)
