# mypy: allow-untyped-defs
import functools
import itertools
from typing import Callable, List

import torch
import torch._prims_common as utils
import torch._subclasses.functional_tensor
import torch.utils._pytree as pytree
from torch._C import DispatchKey
from torch._higher_order_ops.utils import (
    _maybe_run_with_interpreter,
    _set_compilation_env,
    autograd_not_implemented,
    reenter_make_fx,
    unique_graph_id,
)
from torch._inductor.utils import is_pointwise_use
from torch._ops import HigherOrderOperator
from torch._subclasses.fake_tensor import FakeTensorMode
from torch.fx.experimental.proxy_tensor import (
    disable_proxy_modes_tracing,
    ProxyTorchDispatchMode,
    track_tensor_tree,
)


aten = torch._ops.ops.aten


def wrap_combine_fn_flat(*args, combine_fn, spec, num_leaves):
    assert len(args) == 2 * num_leaves
    lhs = pytree.tree_unflatten(args[:num_leaves], spec)
    rhs = pytree.tree_unflatten(args[num_leaves:], spec)
    combined = combine_fn(lhs, rhs)
    combined_leaves = pytree.tree_leaves(combined)
    assert num_leaves == len(combined_leaves)
    return combined_leaves


def _interleave(a, b, dim):
    # https://stackoverflow.com/questions/60869537/how-can-i-interleave-5-pytorch-tensors
    if b_trunc := (a.shape[dim] == b.shape[dim] + 1):
        pad = (
            [0] * ((b.ndim - dim - 1) * 2 + 1)
            + [1]
            + [0] * (b.ndim * 2 - ((b.ndim - dim - 1) * 2 + 2))
        )
        b = torch.nn.functional.pad(b, pad)

    stacked = torch.stack([a, b], dim=dim + 1)
    interleaved = torch.flatten(stacked, start_dim=dim, end_dim=dim + 1)
    if b_trunc:
        # TODO: find torch alternative for slice_along dim for torch.jit.script to work
        interleaved = aten.slice(interleaved, dim, 0, b.shape[dim] + a.shape[dim] - 1)
    return interleaved


def safe_map(f, *args):
    args = list(map(list, args))
    n = len(args[0])
    for arg in args[1:]:
        if len(arg) != n:
            raise ValueError("length mismatch: {list(map(len, args))}")

    def nf(a):
        return f(*a)

    return list(map(nf, zip(*args)))


class AssociativeScanOp(HigherOrderOperator):
    def __init__(self):
        super().__init__("associative_scan")

    def __call__(self, combine_fn, xs, dim):
        return super().__call__(combine_fn, xs, dim)


associative_scan_op = AssociativeScanOp()


def associative_scan(
    combine_fn: Callable[[pytree.PyTree, pytree.PyTree], pytree.PyTree],
    xs: pytree.PyTree,
    dim: int,
    reverse: bool = False,
    combine_mode: str = "pointwise",
) -> torch.Tensor:
    r"""
    Performs an inclusive scan with an associative combine function.

    .. warning::
        `torch.associative_scan` is a prototype feature in PyTorch. It currently
        does not support autograd and you may run into miscompiles.
        Read more about feature classification at:
        https://pytorch.org/blog/pytorch-feature-classification-changes/#prototype

    This operator requires runtime code generation and so requires support for
    ``torch.compile``. Further, only CUDA device codegen is supported at the moment.

    Args:
        combine_fn (Callable): A binary callable with type ``(Tensor, Tensor) -> Tensor``,
            or if input is a pytree ``(pytree, pytree) -> pytree``.
            This function must be pure, i.e., no lifted arguments are supported at the moment,
            satisfy the associative property and have no side-effects.
        xs (torch.Tensor): The input tensor, or nested pytree of tensors.
            All inputs are expected to have the same shape.
        dim (int): the dimension to scan over
        reverse (bool): A boolean stating if the scan should be reversed with respect to ``dim``, default ``False``.
        combine_mode (str): A string indicating whether the ``combine_fn`` is ``pointwise`` or ``generic``, default ``pointwise``.
            If ``combine_mode=pointwise``, ``combine_fn`` must be pure, may only contain pointwise operations
            and ``xs`` must be CUDA tensors.
            In all other cases ``combine_mode=generic`` should be used.
            Note: ``combine_mode=pointwise`` is more efficient than ``combine_mode=generic``.


    Example::

        def add(x: torch.Tensor, y: torch.Tensor):
            return x + y

        cumsum = associative_scan(add, x, dim)

    """
    if not callable(combine_fn):
        raise RuntimeError("Combine_fn must be a callable, but got {combine_fn}")
    if not isinstance(dim, int):
        raise RuntimeError("Dim must be an int, but got " + str(type(dim)))
    if combine_mode not in ["pointwise", "generic"]:
        raise RuntimeError(
            "Combine_mode must either 'pointwise' or 'generic', but got {combine_mode}"
        )

    if not torch._dynamo.is_compiling():
        with _set_compilation_env(), torch._dynamo.utils.disable_cache_limit():
            return torch.compile(associative_scan, fullgraph=True)(
                combine_fn, xs, dim, reverse=reverse, combine_mode=combine_mode
            )

    leaves, spec = pytree.tree_flatten(xs)

    if combine_mode == "pointwise" and not all(l.device.type == "cuda" for l in leaves):
        raise ValueError(
            "For combine_mode='pointwise', all input tensors need to be on CUDA"
        )

    if len(leaves) == 0:
        raise RuntimeError("Expected at least 1 xs leaf")
    if any(not isinstance(x, torch.Tensor) for x in leaves):
        raise RuntimeError("xs leaves must be a Tensor")

    if reverse:
        leaves = [torch.flip(elem, [dim]) for elem in leaves]

    shape = leaves[0].shape
    ndim = len(shape)
    dim = utils.canonicalize_dim(ndim, dim)

    for x in leaves[1:]:
        assert x.shape == shape, "All xs tensors must have the same shape"

    out = combine_fn(
        pytree.tree_unflatten(leaves, spec),
        pytree.tree_unflatten(leaves, spec),
    )
    out_leaves, tree_out = pytree.tree_flatten(out)
    if len(leaves) != len(out_leaves):
        raise RuntimeError(
            "The number of leaves of the pytree of the output of the operator needs to match the length of the pytree of the input"
        )
    if any(x.shape != shape for x in out_leaves):
        raise RuntimeError(
            "The pytree of the output of the operator needs to match the xs pytree"
        )

    combine_fn = functools.partial(
        wrap_combine_fn_flat, combine_fn=combine_fn, spec=spec, num_leaves=len(leaves)
    )

    if combine_mode == "generic":
        result_flat = generic_associative_scan(combine_fn, leaves, dim)
    else:
        result_flat = associative_scan_op(combine_fn, leaves, dim)

    if reverse:
        result_flat = [torch.flip(elem, [dim]) for elem in result_flat]

    return pytree.tree_unflatten(result_flat, spec)


def generic_associative_scan(operator, elems_flat, dim=0):
    r"""
    This function performs the associative_scan operation.
    The algorithm works by recursively collecting neighbours of ``elems_flat`` and subsequently
    applying the ``operator`` on all pairs in parallel along ``dim``.
    The results of the recursive calls are later combined.

    Args:
        operator (Callable): A binary callable with type ``(Tensor, Tensor) -> Tensor``,
            or if input is a pytree ``(pytree, pytree) -> pytree``.
            This function must be pure, pointwise, and satisfy the associative property.
        elems_flat (torch.Tensor): A list of torch.Tensors converted from the pytree of
            ``xs`` provided to ``associative_scan``.
            All inputs are expected to have the same shape.
        dim (int): the dimension to scan over


    Example::

        def add(x: torch.Tensor, y: torch.Tensor):
            return x + y

        elems_flat = torch.tensor([0.0, 1.0, 2.0, 3.0])

        First iteration of _scan ->
            # odd_elems -> apply operator on all neighbours
            # odd_elems = operator([torch.tensor([0.0, 2.0])],
            #                      [torch.tensor([1.0, 3.0])])
            odd_elems = torch.tensor([1.0, 5.0])
            Second iteration of _scan ->
                # odd_elems = operator([torch.tensor([1.0])],
                #                      [torch.tensor([5.0])])
                odd_elems = torch.tensor([6.0])
                # even_elems -> apply operator on all odd_elems and
                # every second element of ``elems``, starting from the second element.
                # even_elems is expanded with the first element of ``elems``
                even_elems = [1.0]
                # Merges odd_elems and even_elems
                res = torch.tensor([1.0, 6.0])
            # even_elems -> apply operator on all odd_elems and
            # every second element of ``elems``, starting from the second element.
            # even_elems is expanded with the first element of ``elems``
            even_elems = [0.0, 3.0]
            # Merges odd_elems and even_elems
            res = torch.tensor([0.0, 1.0, 3.0, 6.0])

    """

    def _scan(elems):
        """Perform the actual recursive scan on ``elems``."""
        num_elems = elems[0].shape[dim]

        if num_elems < 2:
            return elems

        reduced_elems = operator(
            *[aten.slice(elem, dim, 0, -1, 2) for elem in elems],
            *[aten.slice(elem, dim, 1, None, 2) for elem in elems],
        )

        # Recursively compute scan for partially reduced tensors.
        odd_elems = _scan(reduced_elems)

        if num_elems % 2 == 0:
            even_elems = operator(
                *[aten.slice(e, dim, 0, -1) for e in odd_elems],
                *[aten.slice(e, dim, 2, None, 2) for e in elems],
            )
        else:
            even_elems = operator(
                *odd_elems,
                *[aten.slice(e, dim, 2, None, 2) for e in elems],
            )

        # The first element of a scan is the same as the first element
        # of the original `elems`.
        even_elems = [
            torch.cat([aten.slice(elem, dim, 0, 1), result], dim=dim)
            if result.shape.numel() > 0 and elem.shape[dim] > 0
            else result
            if result.shape.numel() > 0
            else aten.slice(
                elem, dim, 0, 1
            )  # Jax allows/ignores concat with 0-dim, Pytorch does not
            for (elem, result) in zip(elems, even_elems)
        ]

        return list(
            safe_map(functools.partial(_interleave, dim=dim), even_elems, odd_elems)
        )

    scans = _scan(elems_flat)

    return scans


def trace_associative_scan(
    proxy_mode, func_overload, combine_fn: Callable, xs: List[torch.Tensor], dim: int
):
    with disable_proxy_modes_tracing():
        sample_xs = [
            torch.empty_like(
                x,
                dtype=x.dtype,
                device=x.device,
                requires_grad=x.requires_grad,
            )
            for x in itertools.chain(xs, xs)
        ]
        combine_graph = reenter_make_fx(combine_fn)(*sample_xs)

    outputs = None
    for node in combine_graph.graph.nodes:
        if node.op == "output":
            assert outputs is None
            assert len(node.args) == 1
            outputs = node.args[0]

        if not all(is_pointwise_use(use) or use.op == "output" for use in node.users):
            raise ValueError(
                "For combine_mode='pointwise', the combine_fn needs to be pointwise"
            )

    assert outputs is not None
    assert len(outputs) == len(
        xs
    ), f"expected combine_fn to return {len(xs)} results but got {len(outputs)}"

    for i, o in zip(xs, outputs):
        o_meta = o.meta["tensor_meta"]
        assert o_meta.dtype == i.dtype, (
            f"combine_fn output type mismatch, expected {i.dtype} "
            + f"but got {o_meta.dtype}"
        )

    _, combine_graph_name = unique_graph_id(proxy_mode, prefix="scan_combine_graph")

    proxy_mode.tracer.root.register_module(combine_graph_name, combine_graph)

    args = (combine_graph, xs, dim)
    proxy_args = pytree.tree_map(proxy_mode.tracer.unwrap_proxy, args)
    out_proxy = proxy_mode.tracer.create_proxy(
        "call_function", func_overload, proxy_args, {}, name="associative_scan"
    )

    with disable_proxy_modes_tracing():
        out = [aten.clone(x) for x in xs]

    return track_tensor_tree(out, out_proxy, constant=None, tracer=proxy_mode.tracer)


@associative_scan_op.py_impl(DispatchKey.CompositeExplicitAutograd)
def associative_scan_op_dense(combine_fn, xs, dim):
    raise NotImplementedError("associative_scan is not implemented for eager")


associative_scan_op.py_impl(DispatchKey.Autograd)(
    autograd_not_implemented(associative_scan_op, deferred_error=True)
)


@associative_scan_op.py_impl(ProxyTorchDispatchMode)
def associative_scan_proxy_mode(mode, combine_fn, xs, dim):
    return trace_associative_scan(mode, associative_scan_op, combine_fn, xs, dim)


@associative_scan_op.py_impl(FakeTensorMode)
def assoiciative_scan_fake_tensor_mode(mode, combine_fn, xs, dim):
    with mode:
        return [x.clone() for x in xs]


@associative_scan_op.py_functionalize_impl
def associative_scan_functionalize(ctx, combine_fn, xs, dim):
    unwrapped_xs = ctx.unwrap_tensors(xs)
    with ctx.redispatch_to_next() as m:
        functional_combine_fn = ctx.functionalize(
            _maybe_run_with_interpreter(combine_fn)
        )
        ret = associative_scan_op(functional_combine_fn, unwrapped_xs, dim)
    return ctx.wrap_tensors(ret)
