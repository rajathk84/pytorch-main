# mypy: allow-untyped-defs
from __future__ import annotations

import collections
import contextlib
import dataclasses
import dis
import functools
import inspect
import logging
import operator
import re
import tempfile
from itertools import count
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Set,
    Tuple,
    TYPE_CHECKING,
    Union,
)

import sympy
from sympy import Expr

import torch
import torch._ops
from torch import dtype as torch_dtype
from torch._dynamo.utils import counters, dynamo_timed
from torch._inductor.codegen.debug_utils import DebugPrinterManager
from torch._inductor.codegen.multi_kernel import MultiKernelState
from torch._inductor.runtime.runtime_utils import cache_dir
from torch.fx.experimental.symbolic_shapes import ConvertIntKey, DivideByKey, SymTypes
from torch.fx.node import _get_qualified_name
from torch.utils._sympy.singleton_int import SingletonInt
from torch.utils._sympy.symbol import symbol_is_type, SymT

from .. import async_compile, config, ir
from ..codecache import output_code_log
from ..ir import ReinterpretView
from ..runtime import triton_heuristics
from ..runtime.hints import DeviceProperties
from ..utils import (
    cache_on_self,
    get_benchmark_name,
    LineContext,
    sympy_product,
    sympy_str,
)
from ..virtualized import V
from .aoti_hipify_utils import maybe_hipify_code_wrapper
from .common import CodeGen, DeferredLine, IndentedBuffer, PythonPrinter
from .triton_utils import config_of, should_unwrap_unspec_arg, signature_to_meta


if TYPE_CHECKING:
    import triton

    from ..graph import GraphLowering


pexpr = PythonPrinter().doprint


ReuseKey = Tuple[torch.device, torch.dtype, str]


def buffer_reuse_key(node: ir.Buffer) -> ReuseKey:
    return (
        node.get_device(),
        node.get_dtype(),
        # NB: this is symbolic so that we don't try to reuse a buffer
        # for s0 for s1, just because they happen to share the same
        # size hint
        sympy_str(V.graph.sizevars.simplify(node.layout.storage_size())),
    )


def convert_arg_type(arg: torch.Argument) -> str:
    from .cpp import CONTAINER_PYTHON_TO_CPP, PYTHON_TO_CPP

    # use x.real_type instead of x.type so that we get ScalarType instead of int
    python_type = repr(arg.real_type)  # type: ignore[attr-defined]

    if python_type == "Tensor":
        # Conversions rules follow https://github.com/pytorch/pytorch/tree/main/aten/src/ATen/native#func
        if arg.alias_info is not None and arg.alias_info.is_write:
            return f"at::{python_type}&"
        else:
            return f"at::{python_type} const&"

    if python_type in PYTHON_TO_CPP:
        cpp_type = PYTHON_TO_CPP[python_type]
        return cpp_type

    # Convert args of container types e.g. Optional[*]
    for py_container, cpp_container in CONTAINER_PYTHON_TO_CPP.items():
        container_match = re.findall(py_container + r"\[([a-zA-Z_]+)]", python_type)
        if len(container_match) == 1:
            contained_type = container_match[0]
            assert (
                contained_type in PYTHON_TO_CPP
            ), f"unsupported {py_container} type in convert_arg_type: {contained_type}"
            cpp_contained_type = PYTHON_TO_CPP[contained_type]
            return f"{cpp_container}<{cpp_contained_type}>"

    raise AssertionError(f"unsupport python_type: {python_type}")


def convert_return_type(ret: torch.Argument) -> str:
    # use x.real_type instead of x.type so that we get ScalarType instead of int
    python_type = repr(ret.real_type)  # type: ignore[attr-defined]
    python_to_cpp = {
        "Tensor": "at::Tensor",
        "List[Tensor]": "std::vector<at::Tensor>",
    }

    cpp_type = python_to_cpp.get(python_type, None)
    assert cpp_type is not None, f"NYI return type: {python_type}"
    # An output aliasing an input is returned by reference only when it's a
    # Tensor, not when it's a Tensor[]. For example, aten.split.Tensor's output
    # aliases the input tensor, but the op returns a vector by value.
    if python_type == "Tensor" and ret.alias_info is not None:
        cpp_type += "&"
    return cpp_type


def get_cpp_op_schema(kernel: torch._ops.OpOverload) -> str:
    args = kernel._schema.arguments
    returns = kernel._schema.returns

    num_returns = len(returns)
    assert num_returns > 0, "must have at least one return value"

    if num_returns == 1:
        cpp_return_value = convert_return_type(returns[0])
    elif num_returns > 1:
        tuple_returns = ", ".join([convert_return_type(r) for r in returns])
        cpp_return_value = f"std::tuple<{tuple_returns}>"

    cpp_arg_type = [f"{convert_arg_type(arg)} {arg.name}" for arg in args]
    return f"{cpp_return_value}({', '.join(cpp_arg_type)})"  # type: ignore[possibly-undefined]


# TODO: Move to a well known place
TritonMetaParams = Dict[str, int]
TritonGrid = Union[
    Tuple[Union[int, sympy.Expr], ...], Callable[[TritonMetaParams], Tuple[int, ...]]
]


def user_defined_kernel_grid_fn_code(
    name: str,
    configs: List[triton.Config],  # type: ignore[name-defined]
    grids: List[TritonGrid],
    wrapper: Optional[WrapperCodeGen] = None,
) -> Tuple[str, str]:
    output = IndentedBuffer()

    def _convert_to_sympy_expr(item: Union[int, sympy.Expr]) -> sympy.Expr:
        return item if isinstance(item, sympy.Expr) else sympy.Integer(item)

    def determine_grid(
        grid: TritonGrid,
    ):
        """
        This function return a tuple of two values: the first one is for the real grid
        which is used in the generated code; the second one is an example grid with
        concreate values which is used in the autotune block to run the generated
        kernels at compile time.
        """
        if wrapper is None or callable(grid):
            # return as-is when used in eager mode or when grid is callable
            return grid, grid
        # Grid contains ints/Expr, so utilize wrapper's expr printer for codegen
        sympy_grid = tuple(_convert_to_sympy_expr(g) for g in grid)
        return (
            wrapper.codegen_shape_tuple(sympy_grid),
            wrapper.codegen_shape_tuple(
                tuple(
                    wrapper.generate_example_arg_value(g, type(g)) for g in sympy_grid
                )
            )
            if config.triton.autotune_at_compile_time
            else None,
        )

    def writeline(line: str, example_grid: Optional[str] = None):
        output.writeline(line)
        if (
            wrapper
            and config.triton.autotune_at_compile_time
            and name not in wrapper.kernel_autotune_names
        ):
            wrapper.kernel_autotune_calls.writeline(example_grid or line)

    fn_name = f"grid_wrapper_for_{name}"
    writeline(f"def {fn_name}(meta):")
    kernel_autotune_calls_indent = (
        wrapper.kernel_autotune_calls.indent()
        if wrapper and config.triton.autotune_at_compile_time
        else contextlib.nullcontext()
    )
    with output.indent(), kernel_autotune_calls_indent:
        if len(grids) == 1:
            grid, example_grid = determine_grid(grids[0])
            writeline(f"return {grid}", f"return {example_grid}")
        else:
            assert len(grids) > 1
            assert len(grids) == len(configs)
            seen = set()
            for grid, c in zip(grids, configs):
                guards = [f"meta['{name}'] == {val}" for name, val in c.kwargs.items()]
                guards = " and ".join(guards)
                grid, example_grid = determine_grid(grid)
                statement = f"if {guards}: return {grid}"
                if statement in seen:
                    continue
                seen.add(statement)
                writeline(statement, f"if {guards}: return {example_grid}")

    return fn_name, output.getvalue()


@dataclasses.dataclass
class SymbolicCallArg:
    inner: str
    # the original symbolic expression represented by inner
    inner_expr: sympy.Expr

    def __str__(self):
        return str(self.inner)


# Default thread stack sizes vary by platform:
# - Linux: 8 MB
# - macOS: 512 KB
# - Windows: 1 MB
# Just pick something comfortably smaller than the smallest for now.
MAX_STACK_ALLOCATION_SIZE = 1024 * 100


class MemoryPlanningState:
    def __init__(self):
        super().__init__()
        self.reuse_pool: Dict[
            ReuseKey, List[FreeIfNotReusedLine]
        ] = collections.defaultdict(list)
        self.total_allocated_buffer_size: int = 0

    def __contains__(self, key: ReuseKey) -> bool:
        return bool(self.reuse_pool.get(key, None))

    def pop(self, key: ReuseKey) -> FreeIfNotReusedLine:
        item = self.reuse_pool[key].pop()
        assert not item.is_reused
        return item

    def push(self, key: ReuseKey, item: FreeIfNotReusedLine) -> None:
        assert not item.is_reused
        self.reuse_pool[key].append(item)


class WrapperLine:
    pass


@dataclasses.dataclass
class EnterSubgraphLine(WrapperLine):
    wrapper: WrapperCodeGen
    graph: GraphLowering

    def __post_init__(self) -> None:
        self.wrapper.push_computed_sizes(self.wrapper.computed_sizes)

    def codegen(self, code: IndentedBuffer) -> None:
        self.wrapper.push_codegened_graph(self.graph)
        code.do_indent()


@dataclasses.dataclass
class ExitSubgraphLine(WrapperLine):
    wrapper: WrapperCodeGen

    def __post_init__(self) -> None:
        self.wrapper.computed_sizes = self.wrapper.pop_computed_sizes()

    def codegen(self, code: IndentedBuffer) -> None:
        self.wrapper.pop_codegened_graph()
        code.do_unindent()


@dataclasses.dataclass
class EnterDeviceContextManagerLine(WrapperLine):
    device_idx: int
    last_seen_device_guard_index: Optional[int]

    def codegen(self, code: IndentedBuffer) -> None:
        if V.graph.cpp_wrapper:
            code.writeline("\n")
            if V.graph.aot_mode:
                # In AOT mode, we have a stream provided as a param. A stream is
                # associated with a device, so we never expect the device to change.
                # CUDAStreamGuard sets the stream and the device.
                if self.last_seen_device_guard_index is None:
                    if config.abi_compatible:
                        code.writeline(
                            f"{V.graph.device_ops.cpp_aoti_stream_guard()} stream_guard(stream, this->device_idx_);"
                        )
                    else:
                        code.writeline(
                            maybe_hipify_code_wrapper(
                                f"{V.graph.device_ops.cpp_stream_guard()} stream_guard("
                                + f"{V.graph.device_ops.cpp_getStreamFromExternal()}(stream, this->device_idx_));"
                            )
                        )
                else:
                    assert (
                        self.last_seen_device_guard_index == self.device_idx
                    ), "AOTInductor only supports running on one CUDA device"
            else:
                if self.last_seen_device_guard_index is None:
                    code.writeline(
                        f"{V.graph.device_ops.cpp_aoti_device_guard()} device_guard({self.device_idx});"
                        if config.abi_compatible
                        else maybe_hipify_code_wrapper(
                            f"{V.graph.device_ops.cpp_device_guard()} device_guard({self.device_idx});"
                        )
                    )
                else:
                    code.writeline(f"device_guard.set_index({self.device_idx});")
        else:
            # Note _DeviceGuard has less overhead than device, but only accepts
            # integers
            code.writeline(f"with {V.graph.device_ops.device_guard(self.device_idx)}:")
            code.do_indent()
            code.writeline(V.graph.device_ops.set_device(self.device_idx))


class ExitDeviceContextManagerLine(WrapperLine):
    def codegen(self, code: IndentedBuffer) -> None:
        if not V.graph.cpp_wrapper:
            code.do_unindent()


@dataclasses.dataclass
class MemoryPlanningLine(WrapperLine):
    wrapper: WrapperCodeGen

    def plan(self, state: MemoryPlanningState) -> MemoryPlanningLine:
        """First pass to find reuse"""
        return self

    def codegen(self, code: IndentedBuffer) -> None:
        """Second pass to output code"""

    def __str__(self) -> str:
        """
        Emits a string representation that fits on one line.
        """
        args: List[str] = []
        for field in dataclasses.fields(self):
            if field.name == "wrapper":
                continue
            val = getattr(self, field.name)
            args.append(
                f"{field.name}={val.get_name() if field.type is ir.Buffer else val}"
            )
        return f"{type(self).__name__}({', '.join(args)})"


@dataclasses.dataclass
class AllocateLine(MemoryPlanningLine):
    node: ir.Buffer

    def plan(self, state: MemoryPlanningState) -> MemoryPlanningLine:
        if self.node.get_name() in V.graph.removed_buffers:
            return NullLine(self.wrapper)

        # try to reuse a recently freed buffer
        key = buffer_reuse_key(self.node)
        if config.allow_buffer_reuse and key in state:
            free_line = state.pop(key)
            free_line.is_reused = True
            return ReuseLine(self.wrapper, free_line.node, self.node)

        if self.node.get_device().type == "cpu":
            static_shape = self.wrapper.static_shape_for_buffer_or_none(self.node)
            if static_shape is not None:
                state.total_allocated_buffer_size += int(
                    functools.reduce(operator.mul, static_shape, 1)
                )

        return self

    def codegen(self, code: IndentedBuffer) -> None:
        assert self.node.get_name() not in V.graph.removed_buffers
        line = self.wrapper.make_buffer_allocation(self.node)
        code.writeline(line)


@dataclasses.dataclass
class FreeIfNotReusedLine(MemoryPlanningLine):
    node: ir.Buffer
    is_reused: bool = False

    def plan(self, state: MemoryPlanningState) -> MemoryPlanningLine:
        if len(self.node.get_inputs_that_alias_output()) > 0:
            return self
        if isinstance(self.node.layout, ir.MultiOutputLayout):
            return self
        assert not self.is_reused
        if self.node.get_name() in V.graph.removed_buffers:
            return NullLine(self.wrapper)
        if config.allow_buffer_reuse:
            state.push(buffer_reuse_key(self.node), self)
        return self

    def codegen(self, code: IndentedBuffer) -> None:
        assert self.node.get_name() not in V.graph.removed_buffers
        if not self.is_reused:
            code.writeline(self.wrapper.make_buffer_free(self.node))


@dataclasses.dataclass
class ReuseLine(MemoryPlanningLine):
    node: ir.Buffer
    reused_as: ir.Buffer
    delete_old: bool = True

    def plan(self, state: MemoryPlanningState) -> MemoryPlanningLine:
        if self.node.get_name() in V.graph.removed_buffers:
            assert self.reused_as.get_name() in V.graph.removed_buffers
            return NullLine(self.wrapper)
        assert self.reused_as.get_name() not in V.graph.removed_buffers
        return self

    def codegen(self, code: IndentedBuffer) -> None:
        assert self.node.get_name() not in V.graph.removed_buffers
        assert self.reused_as.get_name() not in V.graph.removed_buffers
        code.writeline(
            self.wrapper.make_buffer_reuse(self.node, self.reused_as, self.delete_old)
        )


class NullLine(MemoryPlanningLine):
    pass


BufferName = str


class WrapperCodeGen(CodeGen):
    """
    Generate outer wrapper in Python that calls the kernels.
    """

    def __init__(self):
        super().__init__()
        self._names_iter: Iterator[int] = count()
        self.imports = IndentedBuffer()
        self.header = IndentedBuffer()
        self.prefix = IndentedBuffer()
        self.suffix = IndentedBuffer()
        self.wrapper_call = IndentedBuffer()
        self.kernel_autotune_defs = IndentedBuffer()
        self.kernel_autotune_calls = IndentedBuffer()
        self.kernel_autotune_names: Set[str] = set()
        # If the generated source code is exactly the same, reuse the
        # pre-existing kernel for it
        self.src_to_kernel: Dict[str, str] = {}
        self.kernel_numel_expr: Set[Tuple[str, GraphLowering]] = set()
        self.lines: List[Union[MemoryPlanningLine, LineContext]] = []
        self.declare = ""
        self.declare_maybe_reference = ""
        self.ending = ""
        self.open_bracket = "["
        self.closed_bracket = "]"
        self.comment = "#"
        self.namespace = ""
        self.none_str = "None"
        self.size = "size()"
        self.stride = "stride()"
        self.last_seen_device_guard_index: Optional[int] = None
        self.supports_intermediate_hooks = True
        self.expr_printer: Callable[[Any], str] = pexpr
        self.user_defined_kernel_cache: Dict[Tuple[Any, ...], Tuple[str, Any]] = {}
        self.unbacked_symbol_decls: Set[str] = set()  # str of sympy.Symbol
        self.allow_stack_allocation: Optional[bool] = None
        self.stack_allocated_buffers: Dict[BufferName, ir.Buffer] = {}
        self.computed_sizes: Set[sympy.Symbol] = set()

        # this is used for tracking which GraphLowering instance---parent graph
        # or (nested) subgraph---is currently codegened; the primary use case is
        # including the graph instance into a cache key to avoid cross-graph
        # caching during lowering of nested subgraphs
        self.codegened_graph_stack = []
        self.computed_sizes_stack = []

        self.write_header()
        self.write_prefix()
        self.write_kernel_autotune_defs_header()

        if not V.graph.aot_mode:
            for name, hashed in V.graph.constant_reprs.items():
                # include a hash so our code cache puts different constants into different files
                self.write_constant(name, hashed)

        self.allocated: Set[BufferName] = set()
        self.freed: Set[BufferName] = set()

        # maps from reusing buffer to reused buffer
        self.reuses: Dict[BufferName, BufferName] = {}

        self.write_get_raw_stream = functools.lru_cache(None)(  # type: ignore[assignment]
            self.write_get_raw_stream
        )

        @functools.lru_cache(None)
        def add_import_once(line: str) -> None:
            self.imports.writeline(line)
            if config.triton.autotune_at_compile_time:
                self.kernel_autotune_calls.writeline(line)

        self.add_import_once = add_import_once
        self._metas: Dict[str, str] = {}
        self._meta_vars: Set[str] = set()
        self.multi_kernel_state = MultiKernelState()

        # intermediate tensor value printing utility
        self.debug_printer = DebugPrinterManager(
            debug_printer_level=config.aot_inductor.debug_intermediate_value_printer
        )

    def write_constant(self, name: str, hashed: str) -> None:
        self.header.writeline(f"{name} = None  # {hashed}")

    def write_header(self) -> None:
        context = torch._guards.TracingContext.try_get()
        aot_config_comment = ""
        if context is not None and context.aot_graph_name is not None:
            aot_config_comment = f"# AOT ID: {context.aot_graph_name}"
        aot_inductor_debug_utils = ""
        if int(config.aot_inductor.debug_intermediate_value_printer) > 0:
            aot_inductor_debug_utils = "from torch._inductor.codegen.debug_utils import _print_debugging_tensor_value_info"
        self.imports.splice(
            f"""
                {aot_config_comment}
                from ctypes import c_void_p, c_long, c_int
                import torch
                import math
                import random
                import os
                import tempfile
                from math import inf, nan
                from torch._inductor.hooks import run_intermediate_hooks
                from torch._inductor.utils import maybe_profile
                from torch._inductor.codegen.memory_planning import _align as align
                from torch import device, empty_strided
                from {async_compile.__name__} import AsyncCompile
                from torch._inductor.select_algorithm import extern_kernels
                from torch._inductor.codegen.multi_kernel import MultiKernelCall
                {aot_inductor_debug_utils}
            """,
            strip=True,
        )
        self.header.splice(
            """
                aten = torch.ops.aten
                inductor_ops = torch.ops.inductor
                _quantized = torch.ops._quantized
                assert_size_stride = torch._C._dynamo.guards.assert_size_stride
                empty_strided_cpu = torch._C._dynamo.guards._empty_strided_cpu
                empty_strided_cuda = torch._C._dynamo.guards._empty_strided_cuda
                empty_strided_xpu = torch._C._dynamo.guards._empty_strided_xpu
                reinterpret_tensor = torch._C._dynamo.guards._reinterpret_tensor
                alloc_from_pool = torch.ops.inductor._alloc_from_pool
                async_compile = AsyncCompile()
            """,
            strip=True,
        )

    def include_extra_header(self, header: str):
        pass

    def write_kernel_autotune_defs_header(self) -> None:
        self.kernel_autotune_defs.splice(
            f"""
                import torch
                from torch._dynamo.testing import rand_strided
                from torch._dynamo.utils import preserve_rng_state
                from torch._inductor.select_algorithm import AlgorithmSelectorCache
                from {async_compile.__name__} import AsyncCompile

                async_compile = AsyncCompile()
                generate_example_value = AlgorithmSelectorCache.generate_example_value
            """
        )

    @cache_on_self
    def write_triton_header_once(self) -> None:
        import_str = f"""
            import triton
            import triton.language as tl
            from {triton_heuristics.__name__} import grid, split_scan_grid, grid_combo_kernels, start_graph, end_graph
            """
        self.imports.splice(import_str, strip=True)
        if config.triton.autotune_at_compile_time:
            self.kernel_autotune_calls.splice(import_str)
        self.write_get_raw_stream_header_once()

    @cache_on_self
    def write_get_raw_stream_header_once(self) -> None:
        self.imports.writeline(
            V.graph.device_ops.import_get_raw_stream_as("get_raw_stream")
        )
        if config.triton.autotune_at_compile_time:
            self.kernel_autotune_calls.writeline(
                V.graph.device_ops.import_get_raw_stream_as("get_raw_stream")
            )

    def add_meta_once(self, meta: TritonMetaParams) -> str:
        meta = repr(meta)
        if meta not in self._metas:
            var = f"meta{len(self._metas)}"
            self._metas[meta] = var
            self.header.writeline(f"{var} = {meta}")
            if config.triton.autotune_at_compile_time:
                self.kernel_autotune_calls.writeline(f"{var} = {meta}")
                self._meta_vars.add(var)
        return self._metas[meta]

    @cache_on_self
    def get_output_refs(self) -> List[str]:
        return [x.codegen_reference(self.wrapper_call) for x in V.graph.graph_outputs]

    def mark_output_type(self) -> None:
        return

    def codegen_input_size_asserts(self) -> None:
        for name, buf in V.graph.graph_inputs.items():
            if isinstance(buf, sympy.Expr):
                continue

            # comparing strides for 0 size tensor is tricky. Ignore them for now.
            if sympy_product(buf.get_size()) == 0:
                continue
            size = self.codegen_shape_tuple(buf.get_size())
            stride = self.codegen_shape_tuple(buf.get_stride())
            self.prefix.writeline(f"assert_size_stride({name}, {size}, {stride})")

    def codegen_input_nan_asserts(self) -> None:
        self.prefix.writeline("# make sure graph inputs are not nan/inf")
        for name, buf in V.graph.graph_inputs.items():
            if isinstance(buf, sympy.Expr):
                continue

            line = f"assert not {name}.isnan().any().item()"
            self.prefix.writeline(line)
            line = f"assert not {name}.isinf().any().item()"
            self.prefix.writeline(line)

    def write_prefix(self) -> None:
        self.prefix.splice(
            """

            async_compile.wait(globals())
            del async_compile

            def call(args):
            """
        )
        with self.prefix.indent():
            if config.triton.debug_sync_graph:
                self.prefix.writeline(V.graph.device_ops.synchronize())
            if V.graph.graph_inputs:
                lhs = ", ".join(V.graph.graph_input_names)
                if len(V.graph.graph_input_names) == 1:
                    lhs += ","
                self.prefix.writeline(f"{lhs} = args")
                self.prefix.writeline("args.clear()")

            self.codegen_inputs(self.prefix, V.graph.graph_inputs)
            if config.size_asserts:
                self.codegen_input_size_asserts()
            if config.nan_asserts:
                self.codegen_input_nan_asserts()

    # this function (and below) takes a graph as input so
    # that stream caching happens per graph instance. this
    # is important for nested subgraph codegening.
    def write_get_raw_stream(self, device_idx: int, graph=None) -> str:
        self.write_get_raw_stream_header_once()
        name = f"stream{device_idx}"
        self.writeline(f"{name} = get_raw_stream({device_idx})")
        return name

    def get_codegened_graph(self):
        return self.codegened_graph_stack[-1]

    def push_codegened_graph(self, graph):
        self.codegened_graph_stack.append(graph)

    def pop_codegened_graph(self):
        return self.codegened_graph_stack.pop()

    def push_computed_sizes(self, computed_sizes):
        from copy import deepcopy

        return self.computed_sizes_stack.append(deepcopy(computed_sizes))

    def pop_computed_sizes(self):
        return self.computed_sizes_stack.pop()

    def next_kernel_suffix(self) -> str:
        return f"{next(self._names_iter)}"

    def codegen_device_guard_enter(self, device_idx: int) -> None:
        self.writeline(
            EnterDeviceContextManagerLine(device_idx, self.last_seen_device_guard_index)
        )
        if config.triton.autotune_at_compile_time:
            # mimic logic of EnterDeviceContextManagerLine.codegen for the autotune code block
            self.write_triton_header_once()
            self.kernel_autotune_calls.writeline(
                f"with {V.graph.device_ops.device_guard(device_idx)}:"
            )
            self.kernel_autotune_calls.do_indent()
            self.kernel_autotune_calls.writeline(
                V.graph.device_ops.set_device(device_idx)
            )
            self.kernel_autotune_calls.writeline(
                f"stream{device_idx} = get_raw_stream({device_idx})"
            )
        self.last_seen_device_guard_index = device_idx

    def codegen_device_guard_exit(self) -> None:
        self.writeline(ExitDeviceContextManagerLine())
        if config.triton.autotune_at_compile_time:
            self.kernel_autotune_calls.do_unindent()

    def generate_return(self, output_refs: List[str]) -> None:
        if output_refs:
            self.wrapper_call.writeline("return (" + ", ".join(output_refs) + ", )")
        else:
            self.wrapper_call.writeline("return ()")

    def generate_before_suffix(self, result: IndentedBuffer) -> None:
        return

    def generate_end(self, result: IndentedBuffer) -> None:
        return

    def generate_fallback_kernel(self, fallback_kernel, args):
        self.generate_extern_kernel_alloc(fallback_kernel, args)

    def generate_extern_kernel_alloc(self, extern_kernel, args):
        # If it's a NoneLayout then the extern_kernel should essentially be
        # treated as if it doesn't return anything
        no_return = isinstance(extern_kernel.layout, ir.NoneLayout)
        output_name = extern_kernel.get_name()
        origin_node = extern_kernel.get_origin_node()
        kernel_name = extern_kernel.get_kernel_name()
        ending = self.ending
        if config.memory_planning and "view_as_complex" in kernel_name:
            # view operation fallbacks cause issues since inductor
            # doesn't know the memory is still needed and might reuse it.
            ending = f".clone(){ending}"

        if no_return:
            self.writeline(f"{self.declare}{kernel_name}({', '.join(args)}){ending}")
        else:
            self.writeline(
                f"{self.declare}{output_name} = {kernel_name}({', '.join(args)}){ending}"
            )
            if (
                self.supports_intermediate_hooks
                and config.generate_intermediate_hooks
                and origin_node is not None
            ):
                counters["inductor"]["intermediate_hooks"] += 1
                self.writeline(
                    f"run_intermediate_hooks({origin_node.name!r}, {output_name})"
                )

    def generate_extern_kernel_out(
        self, kernel: str, out: str, out_view: Optional[str], args: List[str]
    ):
        # add debug printer code for triton kernel calls at (jit) inductor level
        debug_printer_manager = V.graph.wrapper_code.debug_printer
        debug_printer_manager.set_printer_args(args, kernel, None, None, "extern")
        args.append(f"out={out_view if out_view else out}")
        with debug_printer_manager:
            self.writeline(f"{kernel}({', '.join(args)})")

    def generate_user_defined_triton_kernel(
        self,
        kernel_name: str,
        raw_args: List[Any],
        grid: List[Any],
        configs,
        triton_meta,
        constexprs,
    ):
        grid_fn, code = user_defined_kernel_grid_fn_code(
            kernel_name, configs, grid, wrapper=self
        )
        # Must happen after free symbols are already codegened
        # Emit the grid wrapper function right before the call
        for line in code.split("\n"):
            self.writeline(line)

        args = [self.val_to_arg_str(v) for v in raw_args]
        arg_types = [
            arg.get_dtype() if hasattr(arg, "get_dtype") else type(arg)
            for arg in raw_args
        ]
        self.generate_kernel_call(
            kernel_name, args, grid_fn=grid_fn, arg_types=arg_types, raw_args=raw_args
        )

    def generate_scatter_fallback(
        self,
        output,
        inputs,
        cpp_kernel_name,
        python_kernel_name,
        src_is_tensor,
        reduce,
        kwargs,
    ):
        line = f"{python_kernel_name}({','.join(map(str, inputs))}"
        if python_kernel_name.startswith("aten.scatter_reduce"):
            line += ", ".join([""] + kwargs)
        else:
            if reduce:
                line += f", reduce={repr(reduce)}"
        line += ")"
        self.writeline(line)

    def generate_index_put_fallback(self, kernel, x, indices, values, accumulate):
        indices_str = f"{self.open_bracket}{', '.join(indices)}{self.closed_bracket}"
        args = [x, indices_str, values, accumulate]
        self.writeline(self.wrap_kernel_call(kernel, args))

    def generate_extern_kernel_alloc_and_find_schema_if_needed(
        self,
        buf_name: str,
        python_kernel_name: str,
        cpp_kernel_name: str,
        codegen_args: List[str],
        cpp_op_schema: str,
        cpp_kernel_key: str,
        cpp_kernel_overload_name: str = "",
        op_overload: Optional[torch._ops.OpOverload] = None,
        raw_args=None,
        outputs=None,
    ):
        self.writeline(f"{buf_name} = {python_kernel_name}({', '.join(codegen_args)})")

    def generate(self, is_inference):
        with dynamo_timed("WrapperCodeGen.generate"):
            return self._generate(is_inference)

    def _generate(self, is_inference):
        if config.profile_bandwidth:
            self.write_triton_header_once()
        result = IndentedBuffer()
        result.splice(self.imports)
        result.writeline("")
        result.splice(self.header)
        # We do not want the cpp header for intermediate const graph. Headers would be
        # rendered by the main module instead.
        if V.graph.aot_mode and V.graph.cpp_wrapper and V.graph.is_const_graph:
            result = IndentedBuffer()

        with contextlib.ExitStack() as stack:
            stack.enter_context(self.wrapper_call.indent())
            if config.profiler_mark_wrapper_call:
                self.generate_profiler_mark_wrapper_call(stack)
            if config.profile_bandwidth:
                self.generate_start_graph()

            # We disable planning during training because it presently increases peak memory consumption.
            if is_inference and config.memory_planning:
                self.memory_plan()
                # TODO: integrate memory planning & stack allocation?
                self.allow_stack_allocation = False
            else:
                self.memory_plan_reuse()

            if config.triton.store_cubin:
                self.generate_reset_kernel_saved_flags()

            for line in self.lines:
                if isinstance(line, WrapperLine):
                    line.codegen(self.wrapper_call)
                else:
                    self.wrapper_call.writeline(line)

            output_refs = self.get_output_refs()
            self.mark_output_type()
            if config.triton.debug_sync_graph:
                self.wrapper_call.writeline(V.graph.device_ops.synchronize())

            if config.profile_bandwidth:
                self.generate_end_graph()

            if config.triton.store_cubin:
                self.generate_save_uncompiled_kernels()

            if config.triton.autotune_at_compile_time:
                self.generate_and_run_autotune_block()

            self.generate_return(output_refs)

        self.finalize_prefix()
        result.splice(self.prefix)

        with result.indent():
            result.splice(self.wrapper_call)

        self.generate_before_suffix(result)
        result.splice(self.suffix)

        self.generate_end(result)

        self.add_benchmark_harness(result)

        return result.getvaluewithlinemap()

    def generate_and_run_autotune_block(self):
        """
        Compose self.kernel_autotune_defs and self.kernel_autotune_calls into a single block of
        code and execute it to trigger Triton kernel compilation and auto-tuning
        """
        self.kernel_autotune_defs.splice(
            """
            async_compile.wait(globals())
            del async_compile
        """
        )
        scope = {}  # type: ignore[var-annotated]
        tuning_code = (
            self.kernel_autotune_defs.getvalue() + self.kernel_autotune_calls.getvalue()
        )
        if output_code_log.level == logging.DEBUG:
            # Save the autotuning code block into a file
            # Create a temporary file
            with tempfile.NamedTemporaryFile(
                dir=cache_dir(), suffix=".py", delete=False
            ) as f:
                f.write(tuning_code.encode("utf-8"))
                file_path = f.name
            output_code_log.debug(
                "\nCompile-time auto-tuning code: \n%s\nAuto-tuning code written to %s",
                tuning_code,
                file_path,
            )
        # Execute the code to autotune kernels
        exec(tuning_code, scope)

    def memory_plan(self):
        from .memory_planning import MemoryPlanner

        self.lines = MemoryPlanner(self).plan(self.lines)

    def memory_plan_reuse(self):
        out_names = V.graph.get_output_names()

        while (
            self.lines
            and isinstance(self.lines[-1], MemoryPlanningLine)
            # TODO: this seems legit, NullLine has no node
            and self.lines[-1].node.name not in out_names  # type: ignore[attr-defined]
        ):
            # these lines will be pointless
            self.lines.pop()

        # codegen allocations in two passes
        planning_states = [MemoryPlanningState()]
        past_planning_states = []
        for i in range(len(self.lines)):
            line = self.lines[i]
            if isinstance(line, MemoryPlanningLine):
                self.lines[i] = line.plan(planning_states[-1])
            elif isinstance(line, EnterSubgraphLine):
                planning_states.append(MemoryPlanningState())
            elif isinstance(line, ExitSubgraphLine):
                past_planning_states.append(planning_states.pop())
        past_planning_states.append(planning_states.pop())
        assert len(planning_states) == 0

        # conservatively use the sum of all allocated buffer sizes
        # in potentially nested scopes as the total allocated size
        total_allocated_buffer_size = sum(
            s.total_allocated_buffer_size for s in past_planning_states
        )

        self.allow_stack_allocation = (
            self.allow_stack_allocation is not False
            and config.allow_stack_allocation
            and total_allocated_buffer_size <= MAX_STACK_ALLOCATION_SIZE
        )

    def codegen_input_size_var_decl(self, code: IndentedBuffer, name):
        code.writeline(f"{self.declare}{name}_size = {name}.{self.size}{self.ending}")

    def codegen_input_stride_var_decl(self, code: IndentedBuffer, name):
        code.writeline(
            f"{self.declare}{name}_stride = {name}.{self.stride}{self.ending}"
        )

    def codegen_inputs(
        self, code: IndentedBuffer, graph_inputs: Dict[str, ir.TensorBox]
    ):
        """Assign all symbolic shapes to locals"""

        @functools.lru_cache(None)
        def sizeof(name):
            self.codegen_input_size_var_decl(code, name)
            return f"{name}_size"

        @functools.lru_cache(None)
        def strideof(name):
            self.codegen_input_stride_var_decl(code, name)
            return f"{name}_stride"

        # Assign all symbolic shapes needed to local variables
        bound_vars: Set[sympy.Symbol] = set()

        def is_expr(x):
            return isinstance(x[1], sympy.Expr)

        graph_inputs_expr = list(filter(is_expr, graph_inputs.items()))
        graph_inputs_tensors = list(
            filter(lambda x: not is_expr(x), graph_inputs.items())
        )

        for name, shape in graph_inputs_expr:
            if isinstance(shape, sympy.Symbol) and shape not in bound_vars:
                code.writeline(f"{self.declare}{shape} = {name}{self.ending}")
                bound_vars.add(shape)

        for name, value in graph_inputs_tensors:
            shapes = value.get_size()
            for dim, shape in enumerate(shapes):
                if isinstance(shape, sympy.Symbol) and shape not in bound_vars:
                    code.writeline(
                        f"{self.declare}{shape} = {sizeof(name)}[{dim}]{self.ending}"
                    )
                    bound_vars.add(shape)

        for name, value in graph_inputs_tensors:
            shapes = value.get_stride()
            for dim, shape in enumerate(shapes):
                if isinstance(shape, sympy.Symbol) and shape not in bound_vars:
                    code.writeline(
                        f"{self.declare}{shape} = {strideof(name)}[{dim}]{self.ending}"
                    )
                    bound_vars.add(shape)

    def ensure_size_computed(self, sym: sympy.Symbol):
        if isinstance(sym, sympy.Symbol) and symbol_is_type(sym, SymT.PRECOMPUTED_SIZE):
            if sym in self.computed_sizes:
                return
            self.computed_sizes.add(sym)
            expr = V.graph.sizevars.inv_precomputed_replacements[sym]
            self.writeline(
                f"{self.declare}{sym} = {self.expr_printer(expr)}{self.ending}"
            )

    def finalize_prefix(self):
        pass

    def codegen_python_sizevar(self, x: Expr, *, simplify: bool = True) -> str:
        return pexpr(x, simplify=simplify)

    def codegen_sizevar(self, x: Expr) -> str:
        return self.codegen_python_sizevar(x)

    def codegen_tuple_access(self, basename: str, name: str, index: str) -> str:
        return f"{basename}[{index}]"

    def codegen_python_shape_tuple(self, shape: Tuple[Expr, ...]) -> str:
        parts = list(map(self.codegen_python_sizevar, shape))
        if len(parts) == 0:
            return "()"
        if len(parts) == 1:
            return f"({parts[0]}, )"
        return f"({', '.join(parts)})"

    def codegen_shape_tuple(self, shape: Tuple[Expr, ...]) -> str:
        return self.codegen_python_shape_tuple(shape)

    def codegen_alloc_from_pool(self, name, offset, dtype, shape, stride) -> str:
        return "alloc_from_pool({})".format(
            ", ".join(
                [
                    name,
                    pexpr(offset),  # bytes not numel
                    str(dtype),
                    self.codegen_shape_tuple(shape),
                    self.codegen_shape_tuple(stride),
                ]
            )
        )

    def codegen_reinterpret_view(
        self, data, size, stride, offset, writer, dtype=None
    ) -> str:
        if (
            size == data.layout.size
            and stride == data.layout.stride
            and offset == data.layout.offset
        ):
            if dtype is not None and dtype != data.dtype:
                return f"aten.view.dtype({data.get_name()}, {dtype})"
            else:
                return f"{data.get_name()}"
        else:
            size = self.codegen_shape_tuple(size)
            stride = self.codegen_shape_tuple(stride)
            offset = self.codegen_sizevar(offset)
            if dtype is not None and dtype != data.dtype:
                return f"aten.view.dtype(reinterpret_tensor({data.get_name()}, {size}, {stride}, {offset}), {dtype})"
            else:
                return (
                    f"reinterpret_tensor({data.get_name()}, {size}, {stride}, {offset})"
                )

    def codegen_device_copy(self, src, dst):
        self.writeline(f"{dst}.copy_({src})")

    def codegen_multi_output(self, name, value):
        self.writeline(f"{self.declare}{name} = {value}{self.ending}")

    def codegen_dynamic_scalar(self, node):
        (data,) = (t.codegen_reference() for t in node.inputs)
        if len(node.keypath) == 0:
            self.writeline(f"{node.sym} = {data}.item()")
        elif len(node.keypath) == 1 and isinstance(node.keypath[0], ConvertIntKey):
            self.writeline(f"{node.sym} = 1 if {data}.item() else 0")
        elif len(node.keypath) == 1 and isinstance(node.keypath[0], DivideByKey):
            self.writeline(f"{node.sym}_undivided = {data}.item()")
            self.writeline(
                f"assert {node.sym}_undivided % {node.keypath[0].divisor} == 0, "
                f"f'{{{node.sym}_undivided}} not divisible by {node.keypath[0].divisor}'"
            )
            self.writeline(
                f"{node.sym} = {node.sym}_undivided // {node.keypath[0].divisor}"
            )
        else:
            raise AssertionError(f"unrecognized keypath {node.keypath}")
        # No one should ever use this buffer, but for uniformity
        # define the variable and assign it None
        self.writeline(f"{node.get_name()} = None")

    def benchmark_compiled_module(self, output):
        def add_fake_input(name, shape, stride, device, dtype):
            output.writeline(
                f"{name} = rand_strided("
                f"{self.codegen_python_shape_tuple(shape)}, "
                f"{self.codegen_python_shape_tuple(stride)}, "
                f"device='{device}', dtype={dtype})"
            )

        def add_expr_input(name, val):
            output.writeline(f"{name} = {val}")

        def add_torchbind_input(name, value):
            import pickle

            output.writeline(f"{name} = pickle.loads({pickle.dumps(value)!r})")

        output.writelines(
            ["", "", "def benchmark_compiled_module(times=10, repeat=10):"]
        )
        with output.indent():
            output.splice(
                """
                from torch._dynamo.testing import rand_strided
                from torch._inductor.utils import print_performance
                """,
                strip=True,
            )

            for name, value in V.graph.constants.items():
                # all the constants are global variables, that's why we need
                # these 'global var_name' lines
                output.writeline(f"global {name}")
                add_fake_input(
                    name, value.size(), value.stride(), value.device, value.dtype
                )

            if len(V.graph.torchbind_constants) > 0:
                output.writeline("import pickle")
                for name, torchbind_obj in V.graph.torchbind_constants.items():
                    # all the constants are global variables, that's why we need
                    # these 'global var_name' lines
                    output.writeline(f"global {name}")
                    add_torchbind_input(name, torchbind_obj)

            for name, value in V.graph.graph_inputs.items():
                if isinstance(value, sympy.Symbol) and isinstance(
                    V.graph.sizevars.var_to_val.get(value, None), SingletonInt
                ):
                    # Inductor should only work with dense -> dense graph, and
                    # SingletonInts belong to metadata that should only live on
                    # the subclass.
                    continue
                if isinstance(value, sympy.Expr):  # Don't need to add symbolic
                    # TODO: this fallback and those below actually will generate possibly
                    # invalid benchmark code, because it's not guaranteed 42
                    # is actually a valid value for the kernel in question.
                    # See https://github.com/pytorch/pytorch/issues/124686
                    add_expr_input(name, V.graph.sizevars.size_hint(value, fallback=42))
                else:
                    shape = [
                        V.graph.sizevars.size_hint(x, fallback=42)
                        for x in value.get_size()
                    ]
                    stride = [
                        V.graph.sizevars.size_hint(x, fallback=42)
                        for x in value.get_stride()
                    ]
                    add_fake_input(
                        name,
                        shape,
                        stride,
                        value.get_device(),
                        value.get_dtype(),
                    )

            call_str = f"call([{', '.join(V.graph.graph_inputs.keys())}])"
            output.writeline(f"fn = lambda: {call_str}")
            output.writeline("return print_performance(fn, times=times, repeat=repeat)")

    def add_benchmark_harness(self, output):
        """
        Append a benchmark harness to generated code for debugging
        """
        if not config.benchmark_harness:
            return

        self.benchmark_compiled_module(output)

        output.writelines(["", "", 'if __name__ == "__main__":'])
        with output.indent():
            output.writelines(
                [
                    "from torch._inductor.wrapper_benchmark import compiled_module_main",
                    f"compiled_module_main('{get_benchmark_name()}', benchmark_compiled_module)",
                ]
            )

    def define_kernel(
        self, name: str, kernel: str, metadata: Optional[str] = None, gpu=True
    ):
        metadata_comment = f"{metadata}\n" if metadata else ""
        body = f"\n\n{metadata_comment}{name} = {kernel}"
        self.header.splice(body)
        if config.triton.autotune_at_compile_time:
            self.kernel_autotune_defs.splice(body)

    def define_user_defined_triton_kernel(self, kernel, configs, kwargs):
        from torch.utils._triton import patch_triton_dtype_repr

        patch_triton_dtype_repr()

        original_name = kernel.__name__

        from .common import KernelArgType, SizeArg, TensorArg

        signature: List[KernelArgType] = []
        constants: Dict[int, Any] = {}
        non_constant_indices = []
        equal_to_1_arg_idx: List[int] = []
        for idx, key in enumerate(kernel.arg_names):
            if key not in kwargs:
                continue
            arg = kwargs[key]
            if idx in kernel.constexprs:
                constants[idx] = arg
            else:
                non_constant_indices.append(idx)
                if isinstance(arg, ir.Buffer):
                    signature.append(
                        TensorArg(
                            name=key,
                            buffer=arg.get_name(),
                            dtype=arg.get_dtype(),
                        )
                    )
                elif isinstance(arg, ir.ReinterpretView):
                    # for ReinterpretView we use the underlying
                    # buffer name and note the (possibly non-zero)
                    # offset relative to the underlying buffer
                    signature.append(
                        TensorArg(
                            name=key,
                            buffer=arg.data.get_name(),
                            dtype=arg.get_dtype(),
                            offset=arg.layout.offset,
                        )
                    )
                else:
                    signature.append(SizeArg(key, arg))
                    if isinstance(
                        arg, (int, sympy.Integer)
                    ) and V.graph.sizevars.statically_known_equals(
                        arg, 1  # type: ignore[arg-type]
                    ):
                        equal_to_1_arg_idx.append(idx)
        index_dtype = "tl.int32"
        triton_meta = {
            "signature": signature_to_meta(
                signature,
                size_dtype=index_dtype,
                indices=non_constant_indices,
            ),
            "device": DeviceProperties.create(
                V.graph.scheduler.get_current_device_or_throw()
            ),
            # Triton compiler includes equal_to_1 args into constants even
            # when they are not constexpr. otherwise there may be a segfault
            # during launching the Inductor-compiled Triton kernel.
            # TODO(aakhundov): add None args to constants, too. currently, this
            # causes CUDA errors in test_aot_inductor.test_triton_kernel_with_none_input.
            # https://github.com/pytorch/pytorch/issues/120478#issuecomment-1962822307
            # https://github.com/openai/triton/blob/231efe9ed2d200be0f69a07c298e4342b08efe3d/python/triton/runtime/jit.py#L384
            "constants": {
                **constants,
                **dict.fromkeys(equal_to_1_arg_idx, 1),
            },
            "configs": [
                config_of(
                    signature,
                    indices=non_constant_indices,
                )
            ],
        }

        # Distinguish between different functions using function id
        cache_key: List[Any] = [id(kernel.fn)]
        if len(configs) > 0:
            for arg in kwargs.values():
                # We need to key on non tensor arg only in autotune mode
                if not isinstance(arg, (ir.Buffer, ir.ReinterpretView)):
                    cache_key.append(arg)
        cache_key.append(str(triton_meta))
        cache_key = tuple(cache_key)

        if cache_key in self.user_defined_kernel_cache:
            return self.user_defined_kernel_cache[cache_key]

        name = f"{original_name}_{len(self.user_defined_kernel_cache)}"
        # Add to the cache for the next use
        self.user_defined_kernel_cache[cache_key] = (name, triton_meta)

        compile_wrapper = IndentedBuffer()
        compile_wrapper.writeline(f"async_compile.triton({original_name!r}, '''")

        from .triton import gen_common_triton_imports, TritonKernel

        compile_wrapper.splice(gen_common_triton_imports())

        inductor_meta = {
            "kernel_name": name,
            **TritonKernel.inductor_meta_common(),
        }

        configs = [
            {
                "kwargs": config.kwargs,
                "num_warps": config.num_warps,
                "num_stages": config.num_stages,
            }
            for config in configs
        ]

        compile_wrapper.splice(
            f"""
            @triton_heuristics.user_autotune(
                configs={configs!r},
                inductor_meta={inductor_meta!r},
                triton_meta={triton_meta!r},
                filename=__file__,
                custom_kernel=True,
            )
            @triton.jit
            """
        )
        compile_wrapper.splice(kernel.src, strip=True)

        # Also include any possible kernel being called indirectly
        from triton import JITFunction  # type: ignore[name-defined, attr-defined]
        from triton.language import constexpr  # type: ignore[name-defined]

        # global constexpr vars handled above
        symbols_included = {original_name}

        def traverse(cur_kernel):
            # here we extract the unqualified names (i.e., not attributes and
            # without prepended module name) loaded in the kernel code, which
            # are matched with the co_names and __globals__ below to codegen
            # the respective imports necessary for the kernel compilation
            unqualified_loads = {
                inst.argval
                for inst in dis.Bytecode(cur_kernel.fn)
                if inst.opname == "LOAD_GLOBAL"
            }
            global_annotations = cur_kernel.fn.__globals__.get("__annotations__", {})
            for symbol_name in cur_kernel.fn.__code__.co_names:
                if symbol_name in symbols_included:
                    continue
                if symbol_name in cur_kernel.fn.__globals__:
                    symbol = cur_kernel.fn.__globals__[symbol_name]
                    if isinstance(symbol, JITFunction):
                        compile_wrapper.newline()
                        compile_wrapper.writeline("@triton.jit")
                        compile_wrapper.splice(symbol.src, strip=True)
                        symbols_included.add(symbol_name)
                        traverse(symbol)
                    elif isinstance(symbol, (int, str, bool, constexpr)):
                        compile_wrapper.newline()
                        if isinstance(symbol, constexpr):
                            symbol_str = f"tl.constexpr({symbol.value!r})"
                        else:
                            symbol_str = f"{symbol!r}"
                        if annotation := global_annotations.get(symbol_name):
                            annotion_code = ""
                            if isinstance(annotation, type):
                                annotation_code = (
                                    f": {annotation.__module__}.{annotation.__name__}"
                                )
                            else:
                                annotation_code = f": {annotation!r}"
                            compile_wrapper.writeline(
                                f"{symbol_name}{annotation_code} = {symbol_str}"
                            )
                        else:
                            compile_wrapper.writeline(f"{symbol_name} = {symbol!r}")
                        symbols_included.add(symbol_name)
                    elif (
                        symbol_name in unqualified_loads
                        and symbol_name != "tl"  # already imported
                        and hasattr(symbol, "__module__")
                        # only codegen imports from triton; JITFunctions
                        # imported from other modules will be codegened
                        # in the separate branch above
                        and symbol.__module__.startswith("triton")
                    ):
                        # a global symbol imported from triton is referenced
                        # without module qualification (i.e., `store` instead
                        # of `tl.store`): need to codegen an import
                        compile_wrapper.writeline(
                            f"from {symbol.__module__} import {symbol.__name__} as {symbol_name}"
                        )
                        symbols_included.add(symbol_name)

        traverse(kernel)

        current_device = V.graph.scheduler.get_current_device_or_throw()
        compile_wrapper.writeline(f"''', device_str='{current_device.type}')")
        _, lineno = inspect.getsourcelines(kernel.fn)
        srcfile = inspect.getsourcefile(kernel.fn)
        metadata = f"# Original path: {srcfile}:{lineno}"
        self.define_kernel(
            name,
            compile_wrapper.getvalue(),
            metadata,
        )
        return name, triton_meta

    def generate_numel_expr(self, kernel_name: str, tree, suffix: Optional[str] = None):
        expr = f"{kernel_name}_{tree.prefix}numel"
        if suffix is not None:
            expr += f"_{suffix}"
        if (expr, V.graph) not in self.kernel_numel_expr:
            # declare expr once in each graph (scope)
            self.kernel_numel_expr.add((expr, V.graph))
            self.writeline(
                f"{self.declare}{expr} = {self.expr_printer(tree.numel)}{self.ending}"
            )
        else:
            self.writeline(f"{expr} = {self.expr_printer(tree.numel)}{self.ending}")
        # We can get symbolic expressions here, like s0*64
        # It is fine to have them here, but we need to handle them correctly as their own type
        # This is tricky to do, so we wrap in a custom type, distinct from scalars, but also from sympy*
        # scalars as well.
        # This is handled in `generate_args_decl` which has a correct comment of: TODO: only works for
        # constant now, need type info. I agree, this needs type info, and while this is not true type info
        # it suffices as a type hint for the purposes of producing the correct code for this type.
        return SymbolicCallArg(expr, tree.numel)

    def generate_workspace_allocation(self, nbytes, device, zero_fill):
        line = self.make_allocation(
            "workspace", device, torch.uint8, shape=(nbytes,), stride=(1,)
        )
        self.writeline(line)
        if zero_fill:
            self.writeline(f"workspace.zero_(){self.ending}")

    def wrap_kernel_call(self, name, call_args):
        return f"{name}({', '.join(call_args)}){self.ending}"

    def generate_profiler_mark_wrapper_call(self, stack):
        self.wrapper_call.writeline("from torch.profiler import record_function")
        self.wrapper_call.writeline(
            f"with record_function('graph_{V.graph.graph_id}_inductor_wrapper_call'):"
        )
        stack.enter_context(self.wrapper_call.indent())

    def generate_start_graph(self):
        self.wrapper_call.writeline("start_graph()")

    def generate_end_graph(self):
        self.wrapper_call.writeline(f"end_graph({config.profile_bandwidth_output!r})")

    def generate_reset_kernel_saved_flags(self):
        self.wrapper_call.splice(
            f"""
            for kernel in globals().values():
                if isinstance(kernel, {triton_heuristics.__name__}.CachingAutotuner):
                    kernel.cuda_kernel_saved = False
            """
        )

    def generate_save_uncompiled_kernels(self):
        """
        Precompile and save the CUBINs of the Triton kernels that haven't
        been precompiled and saved as a side effect of running the generated
        JIT model (Python wrapper). This can happen when the model contains
        control flow: only one pass through the control flow operators covers
        the kernels that are saved, the remaining kernels are not launched,
        hence not saved. The main purpose of this codegen is to compile and
        save the Triton kernels outside the active control flow path for
        subsequent AOTInductor code generation and compilation.
        """
        self.wrapper_call.splice(
            f"""
            for kernel in globals().values():
                if isinstance(kernel, {triton_heuristics.__name__}.CachingAutotuner):
                    if not kernel.cuda_kernel_saved:
                        if len(kernel.launchers) == 0:
                            kernel.precompile()
                        kernel.save_gpu_kernel(
                            grid=(0, 0, 0),   # use dummy grid
                            stream="stream",  # use dummy stream
                            launcher=kernel.launchers[0],
                        )
            """
        )

    def generate_default_grid(
        self,
        kernel_name: str,
        grid: List[Any],
        gpu: bool = True,
        grid_callable: Optional[Callable[..., Any]] = None,
        **grid_extra_kwags,
    ):
        return grid

    def prepare_triton_kernel_call(self, device_index, call_args):
        def wrap_arg(arg):
            if isinstance(arg, str):
                # dynamo wraps unspec variable as 0d CPU tensor, need convert to scalar
                return arg + ".item()" if should_unwrap_unspec_arg(arg) else arg
            elif isinstance(arg, (int, float, bool, SymbolicCallArg)):
                return str(arg)
            else:
                return self.expr_printer(V.graph.sizevars.simplify(arg))

        call_args = [wrap_arg(arg) for arg in call_args]

        if device_index is None:
            current_device = V.graph.scheduler.get_current_device_or_throw()
            device_index = current_device.index

        return device_index, call_args

    def generate_example_arg_value(self, arg, arg_type, raw_arg=None, index=None):
        if isinstance(arg_type, torch_dtype):
            if V.graph.try_get_buffer(arg) is not None:
                buf_name = arg
                buf = V.graph.get_buffer(arg)
            else:
                assert (
                    raw_arg is not None
                ), "V.graph.get_buffer(arg) and raw_arg can't be None at the same time"
                buf_name = f"tmp_arg_{index}"
                buf = raw_arg

            size = V.graph.sizevars.size_hints(
                buf.get_size(),
                fallback=config.unbacked_symint_fallback,
            )
            stride = V.graph.sizevars.size_hints(
                buf.get_stride(),
                fallback=config.unbacked_symint_fallback,
            )
            device = buf.get_device()
            dtype = buf.get_dtype()
            offset = V.graph.sizevars.size_hint(
                buf.layout.offset,
                fallback=config.unbacked_symint_fallback,
            )
            value = f"generate_example_value({size}, {stride}, '{device}', {dtype}, {offset})"
            self.kernel_autotune_calls.writeline(f"{buf_name} = {value}")
            return buf_name
        elif issubclass(arg_type, sympy.Basic) or isinstance(arg, SymbolicCallArg):
            # arg is a symbol or symbolic expression
            if isinstance(arg, str):
                if arg in self._meta_vars:
                    return arg
                if raw_arg is None:
                    return "None"
                arg = raw_arg
            if isinstance(arg, SymbolicCallArg):
                arg = arg.inner_expr
            if arg in V.graph.sizevars.inv_precomputed_replacements:
                arg = V.graph.sizevars.inv_precomputed_replacements[arg]
            return str(
                V.graph.sizevars.size_hint(
                    arg,
                    fallback=config.unbacked_symint_fallback,
                )
            )
        elif isinstance(arg, (str, int, float, bool)):
            return str(arg)
        elif isinstance(arg, list):
            return f"[{', '.join(self.generate_example_arg_value(a, type(a)) for a in arg)}]"
        else:
            raise NotImplementedError(f"Unsupported type {type(arg)}")

    def _grid_dim_str(self, grid_per_dim):
        if isinstance(grid_per_dim, list):
            return (
                "[" + ", ".join(self._grid_dim_str(item) for item in grid_per_dim) + "]"
            )
        else:
            return pexpr(grid_per_dim)

    def generate_kernel_call(
        self,
        kernel_name,
        call_args,
        grid=None,
        device_index=None,
        gpu=True,
        triton=True,
        arg_types=None,
        raw_args=None,
        grid_fn: str = "grid",
        triton_meta=None,
        autotune_configs=None,
        grid_extra_kwargs="",
    ):
        """
        Generates kernel call code.

        gpu: Defines whether the backend is GPU. Otherwise the backend is CPU.

        triton: Defines whether the GPU backend uses Triton for codegen.
                Otherwise it uses the CUDA language for codegen.
                Only valid when gpu == True.
        """
        if gpu:
            device_index, call_args_str = self.prepare_triton_kernel_call(
                device_index, call_args
            )
            call_args_str = ", ".join(call_args_str)
            stream_name = self.write_get_raw_stream(device_index, V.graph)
            if triton:
                self.write_triton_header_once()
                if grid is None:
                    grid_str = grid_fn
                else:
                    grid_str = ", ".join(self._grid_dim_str(item) for item in grid)
                    if grid_extra_kwargs:
                        grid_str = f"{grid_str}, {grid_extra_kwargs}"
                    grid_str = f"{grid_fn}({grid_str})"
                # add debug printer code for triton kernel calls at (jit) inductor level
                debug_printer_manager = V.graph.wrapper_code.debug_printer
                debug_printer_manager.set_printer_args(
                    call_args, kernel_name, arg_types, None
                )
                with debug_printer_manager:
                    self.writeline(
                        f"{kernel_name}.run({call_args_str}, grid={grid_str}, stream={stream_name})"
                    )
                if (
                    config.triton.autotune_at_compile_time
                    and kernel_name not in self.kernel_autotune_names
                ):
                    # Create example args for autotune in a separate epilogue
                    assert arg_types is not None and len(call_args) == len(
                        arg_types
                    ), "call_args and arg_types do not match"

                    tensor_args = {}
                    all_args = []
                    if raw_args is None:
                        # create a dummy raw_args for uniform behavior in the following loop
                        raw_args = [None] * len(call_args)
                    else:
                        assert len(raw_args) == len(
                            call_args
                        ), "call_args and raw_args do not match"

                    for i, (arg, arg_type, raw_arg) in enumerate(
                        zip(call_args, arg_types, raw_args)
                    ):
                        key = None
                        if isinstance(arg, str) and "=" in str(arg):
                            # arg may be passed in a kwarg style, and then we need to extract its value
                            key, arg = arg.split("=")

                        if isinstance(arg_type, torch_dtype):
                            if arg not in tensor_args:
                                arg_str = self.generate_example_arg_value(
                                    arg, arg_type, raw_arg, i
                                )
                                tensor_args[arg] = arg_str
                            else:
                                arg_str = tensor_args[arg]
                        else:
                            arg_str = self.generate_example_arg_value(
                                arg, arg_type, raw_arg, i
                            )
                        all_args.append(arg_str if key is None else f"{key}={arg_str}")

                    if grid is None:
                        grid_str = grid_fn
                    else:
                        grid_str = ", ".join(
                            self.generate_example_arg_value(g, type(g)) for g in grid
                        )
                        if grid_extra_kwargs:
                            grid_str = f"{grid_str}, {grid_extra_kwargs}"
                        grid_str = f"{grid_fn}({grid_str})"

                    self.kernel_autotune_calls.writeline(
                        f"{kernel_name}.run({', '.join(all_args)}, grid={grid_str}, stream={stream_name})"
                    )
                    self.kernel_autotune_calls.writeline(
                        f"del {', '.join(arg for arg in tensor_args.values())}\n",
                    )
                    self.kernel_autotune_names.add(kernel_name)
            else:
                stream_ptr = f"c_void_p({stream_name})"
                self.writeline(
                    f"{kernel_name}.{kernel_name}({call_args_str}, {stream_ptr})"
                )
        else:
            self.writeline(self.wrap_kernel_call(kernel_name, call_args))

    def writeline(self, line):
        self.lines.append(line)

    def writelines(self, lines):
        for line in lines:
            self.writeline(line)

    def enter_context(self, ctx):
        self.lines.append(LineContext(ctx))

    def val_to_arg_str(self, s, type_=None):
        from torch.utils._triton import dtype_to_string, has_triton_package

        if has_triton_package():
            import triton

        if isinstance(s, SymTypes):
            return pexpr(s.node.expr)
        elif isinstance(s, sympy.Expr):
            return pexpr(s)
        elif isinstance(s, (tuple, list)):

            @dataclasses.dataclass
            class Shim:
                ref: Any

                def __repr__(self):
                    return self.ref

            return repr(type(s)(Shim(self.val_to_arg_str(a)) for a in s))
        elif isinstance(s, torch._ops.OpOverload):
            return _get_qualified_name(s)
        elif isinstance(s, (ir.Buffer, ReinterpretView)):
            return s.codegen_reference()
        elif has_triton_package() and isinstance(s, triton.language.dtype):  # type: ignore[possibly-undefined]
            return dtype_to_string(s)
        else:
            return repr(s)

    # The following methods are for memory management
    def make_buffer_allocation(self, buffer):
        device = buffer.get_device()
        dtype = buffer.get_dtype()
        shape = tuple(buffer.get_size())
        stride = tuple(buffer.get_stride())
        return self.make_allocation(buffer.get_name(), device, dtype, shape, stride)

    def make_allocation(self, name, device, dtype, shape, stride):
        if device.type in ("cpu", "cuda", "xpu"):
            # optimized path for faster allocations, saving ~2us versus the stuff below
            return (
                f"{name} = empty_strided_{device.type}("
                f"{self.codegen_shape_tuple(shape)}, "
                f"{self.codegen_shape_tuple(stride)}, "
                f"{dtype})"
            )
        # all other devices:
        return (
            f"{name} = empty_strided("
            f"{self.codegen_shape_tuple(shape)}, "
            f"{self.codegen_shape_tuple(stride)}, "
            f"device='{device.type}', dtype={dtype})"
        )

    def make_tensor_alias(self, new_name, old_name, comment=""):
        return f"{self.declare}{new_name} = {old_name}{self.ending}  {self.comment} {comment}"

    def make_buffer_free(self, buffer):
        return f"del {buffer.get_name()}"

    def make_free_by_names(self, names_to_del: List[str]):
        return f"del {', '.join(name for name in names_to_del)}"

    def codegen_exact_buffer_reuse(self, old_name: str, new_name: str, del_line: str):
        return f"{self.declare_maybe_reference}{new_name} = {old_name}{del_line}{self.ending}  {self.comment} reuse"

    def make_buffer_reuse(self, old: ir.Buffer, new: ir.Buffer, delete_old: bool):
        assert old.get_dtype() == new.get_dtype()
        old_name = old.get_name()
        new_name = new.get_name()
        del_line = ";"
        if old_name not in V.graph.get_output_names() and delete_old:
            del_line = f"; {self.make_buffer_free(old)}"

        if old.get_size() == new.get_size() and old.get_stride() == new.get_stride():
            if old_name in self.stack_allocated_buffers:
                self.stack_allocated_buffers[new_name] = new
            return self.codegen_exact_buffer_reuse(old_name, new_name, del_line)

        reinterpret_view = self.codegen_reinterpret_view(
            old, new.get_size(), new.get_stride(), 0, self.wrapper_call
        )
        if reinterpret_view in self.stack_allocated_buffers:
            self.stack_allocated_buffers[new_name] = new
        return f"{self.declare_maybe_reference}{new_name} = {reinterpret_view}{del_line}  {self.comment} reuse"

    def codegen_deferred_allocation(self, name, layout):
        self.writeline(
            DeferredLine(
                name,
                f"{self.declare_maybe_reference}{name} = {layout.view.codegen_reference()}{self.ending}  "
                f"{self.comment} alias",
            )
        )

    def codegen_allocation(self, buffer: ir.Buffer):
        name = buffer.get_name()

        if name in V.graph.removed_buffers or name in self.allocated:
            return
        self.allocated.add(name)
        if isinstance(
            buffer.get_defining_op(),
            (ir.ExternKernelAlloc, ir.MultiOutput),
        ):
            return

        layout = buffer.get_layout()
        if isinstance(layout, ir.MutationLayoutSHOULDREMOVE):
            return
        if isinstance(layout, ir.NoneLayout):
            return
        if isinstance(layout, ir.NonOwningLayout):
            assert isinstance(
                layout.view, ir.ReinterpretView
            ), f"unexpected {type(layout.view)}: {layout.view}"
            assert isinstance(layout.view.data, ir.StorageBox), type(layout.view.data)
            assert isinstance(layout.view.data.data, ir.Buffer), type(layout.view.data)
            self.codegen_allocation(layout.view.data.data)
            self.codegen_deferred_allocation(name, layout)
            return

        self.writeline(AllocateLine(self, buffer))

    def codegen_free(self, buffer):
        name = buffer.get_name()

        # can be freed but not reused
        if isinstance(buffer, ir.InputBuffer):
            self.writeline(self.make_buffer_free(buffer))
            return

        if not self.can_reuse(buffer):
            return
        self.freed.add(name)

        self.writeline(FreeIfNotReusedLine(self, buffer))

    def can_reuse(self, input_buffer, output_buffer=None):
        name = input_buffer.get_name()
        return not (
            name in V.graph.removed_buffers
            or name in V.graph.graph_inputs
            or name in V.graph.constants
            or name in V.graph.torchbind_constants
            or name in V.graph.never_reuse_buffers
            or name in self.freed
        )

    def did_reuse(self, buffer, reused_buffer):
        # Check whether a given buffer was reused by a possible reuser in the wrapper codegen
        # Can be consulted from inside ir codegen, e.g. to determine whether a copy is needed
        return (
            buffer.get_name() in self.reuses
            and self.reuses[buffer.get_name()] == reused_buffer.get_name()
        )

    def codegen_inplace_reuse(self, input_buffer: ir.Buffer, output_buffer: ir.Buffer):
        assert buffer_reuse_key(input_buffer) == buffer_reuse_key(output_buffer)
        self.codegen_allocation(input_buffer)
        self.freed.add(input_buffer.get_name())
        self.allocated.add(output_buffer.get_name())
        self.reuses[output_buffer.get_name()] = input_buffer.get_name()
        self.writeline(ReuseLine(self, input_buffer, output_buffer))

    def codegen_unbacked_symbol_decl(self, symbol):
        name = str(symbol)
        if name in self.unbacked_symbol_decls:
            return name
        else:
            # When in CppWrapperCpu, we should only generate the declaration once
            self.unbacked_symbol_decls.add(name)
            return self.declare + name

    def codegen_subgraph_prefix(self, subgraph, outer_inputs, outer_outputs):
        for inner_input, outer_input in zip(subgraph.graph.graph_inputs, outer_inputs):
            self.writeline(f"{self.declare}{inner_input} = {outer_input}{self.ending}")

    def codegen_subgraph_suffix(self, subgraph, outer_inputs, outer_outputs):
        for inner_output, outer_output in zip(
            subgraph.graph.graph_outputs, outer_outputs
        ):
            self.writeline(
                f"{outer_output} = {inner_output.codegen_reference()}{self.ending}"
            )

    def codegen_subgraph(self, subgraph, outer_inputs, outer_outputs):
        try:
            self.push_codegened_graph(subgraph.graph)
            self.writeline(f"{self.comment} subgraph: {subgraph.name}")
            self.codegen_subgraph_prefix(subgraph, outer_inputs, outer_outputs)
            parent_graph = V.graph
            with V.set_graph_handler(subgraph.graph):
                subgraph.graph.codegen_subgraph(
                    parent_graph=parent_graph,
                )
            self.codegen_subgraph_suffix(subgraph, outer_inputs, outer_outputs)
        finally:
            self.pop_codegened_graph()

    def codegen_conditional(self, conditional):
        name = conditional.get_name()

        self.writeline(f"{name} = [None] * {len(conditional.outputs)}")

        outer_inputs = [buf.codegen_reference() for buf in conditional.operands]
        outer_outputs = [f"{name}[{i}]" for i in range(len(conditional.outputs))]

        predicate = conditional.predicate.codegen_reference()
        if not isinstance(conditional.predicate, ir.ShapeAsConstantBuffer):
            # move the Tensor predicate to host
            predicate = f"{predicate}.item()"

        self.writeline(f"{name} = [None] * {len(conditional.outputs)}")
        self.writeline(f"if {predicate}:")
        self.writeline(EnterSubgraphLine(self, conditional.true_subgraph.graph))
        self.codegen_subgraph(conditional.true_subgraph, outer_inputs, outer_outputs)
        self.writeline(ExitSubgraphLine(self))
        self.writeline("else:")
        self.writeline(EnterSubgraphLine(self, conditional.false_subgraph.graph))
        self.codegen_subgraph(conditional.false_subgraph, outer_inputs, outer_outputs)
        self.writeline(ExitSubgraphLine(self))

    def codegen_while_loop(self, while_loop):
        name = while_loop.get_name()
        outer_carried_inputs = [
            buf.codegen_reference() for buf in while_loop.carried_inputs
        ]
        outer_additional_inputs = [
            buf.codegen_reference() for buf in while_loop.additional_inputs
        ]

        self.writeline(f"{name} = [None] * {len(outer_carried_inputs)}")
        for i, inp in enumerate(outer_carried_inputs):
            # set the initial state before the loop
            self.writeline(f"{name}[{i}] = {inp}")

        cond_outer_inputs = [
            *[f"{name}[{i}]" for i in range(len(outer_carried_inputs))],
            *outer_additional_inputs,
        ]
        cond_outer_outputs = [f"{name}_cond_result"]
        body_outer_inputs = list(
            cond_outer_inputs
        )  # same inputs for cond_fn and body_fn
        # Carry over the state from body_fn. Note: We only carry over
        # the carried_inputs part of the inputs, the additional ones
        # are passed in as they're before.
        body_outer_outputs = body_outer_inputs[: len(outer_carried_inputs)]

        self.writeline("while True:")
        self.writeline(EnterSubgraphLine(self, while_loop.cond_subgraph.graph))
        self.codegen_subgraph(
            while_loop.cond_subgraph, cond_outer_inputs, cond_outer_outputs
        )
        self.writeline(
            f"if not {cond_outer_outputs[0]}.item(): break"
        )  # condition doesn't hold
        self.writeline(ExitSubgraphLine(self))
        self.writeline(EnterSubgraphLine(self, while_loop.body_subgraph.graph))
        self.codegen_subgraph(
            while_loop.body_subgraph, body_outer_inputs, body_outer_outputs
        )
        self.writeline(ExitSubgraphLine(self))

    @staticmethod
    def statically_known_int_or_none(x):
        try:
            if getattr(x, "free_symbols", None):
                # _maybe_evaluate_static will return (s0 // (2 // s0)) as 2, but
                # the actual codegen will still generate the full expression here.
                return None
            if isinstance(x, int):
                return x
            val = V.graph._shape_env._maybe_evaluate_static(x)
            return int(val)
        except Exception:
            return None

    @staticmethod
    def statically_known_list_of_ints_or_none(lst):
        result = []
        for x in lst:
            num = WrapperCodeGen.statically_known_int_or_none(x)
            if num is None:
                return None
            result.append(num)
        return result

    @staticmethod
    def is_statically_known_list_of_ints(lst):
        return WrapperCodeGen.statically_known_list_of_ints_or_none(lst) is not None

    @staticmethod
    def static_shape_for_buffer_or_none(buffer):
        return WrapperCodeGen.statically_known_list_of_ints_or_none(buffer.get_size())

    @staticmethod
    def can_prove_buffer_has_static_shape(buffer):
        return WrapperCodeGen.static_shape_for_buffer_or_none(buffer) is not None
