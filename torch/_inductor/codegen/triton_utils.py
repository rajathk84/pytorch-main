# mypy: allow-untyped-defs
from typing import Any, Dict, List, Optional

import sympy

import torch

from .. import config
from ..runtime.hints import instance_descriptor
from ..utils import _type_of
from ..virtualized import V
from .common import KernelArgType, SizeArg, TensorArg, WorkspaceArg


def should_unwrap_unspec_arg(name: str):
    if V.graph.is_unspec_arg(name):
        # Unwrap on all devices except CPU
        if V.graph.scheduler.get_current_device_or_throw().type != "cpu":
            return True
        # Only unwrap on CPU if the input is not used as an output
        if name not in V.graph.mutated_buffers:
            return True
    return False


def signature_of(arg: KernelArgType, *, size_dtype: str) -> str:
    if isinstance(arg, TensorArg):
        # TODO: Remove fp8 special handling when Triton supports PyTorch fp8 dtypes.
        # Related PR: https://github.com/openai/triton/pull/2279/
        if arg.dtype == torch.float8_e4m3fn:
            tye = "*fp8e4nv"
        elif arg.dtype == torch.float8_e5m2:
            tye = "*fp8e5"
        elif arg.dtype == torch.float8_e4m3fnuz:
            tye = "*fp8e4b8"
        elif arg.dtype == torch.float8_e5m2fnuz:
            tye = "*fp8e5b16"
        else:
            tye = _type_of(arg.dtype)
        if should_unwrap_unspec_arg(arg.buffer):
            # had unwrapped 0d tensor as scalar
            new_tye = tye.lstrip("*")
            if new_tye in ["fp16", "bf16"]:
                return "fp32"
            else:
                return new_tye
        else:
            return tye
    if isinstance(arg, SizeArg):
        if arg.expr is None:
            # From triton/runtime/jit.py
            # `None` is nullptr.  Implicitly convert to *i8.
            return "*i8"
        elif isinstance(arg.expr, (float, sympy.Float)):
            return "fp32"
        if size_dtype == "tl.int32":
            return "i32"
        elif size_dtype == "tl.int64":
            return "i64"
        else:
            raise NotImplementedError(f"unhandled size_dtype {size_dtype}")
    if isinstance(arg, WorkspaceArg):
        return "*i8"
    raise NotImplementedError(f"unhandled {type(arg)}: {arg}")


def signature_to_meta(
    signature: List[KernelArgType],
    *,
    size_dtype: str,
    indices: Optional[List[int]] = None,
) -> Dict[int, str]:
    if indices is None:
        indices = list(range(len(signature)))
    return {
        i: signature_of(arg, size_dtype=size_dtype)
        for i, arg in zip(indices, signature)
    }


def is_unaligned_buffer(arg: TensorArg):
    buf_name = arg.buffer
    if buf_name in V.graph.graph_inputs:
        # See Note: [Input Alignment handling in Inductor]
        return buf_name not in V.graph.aligned_inputs

    if buf_name in V.graph.constants:
        # all constants are assumed to be aligned
        return False

    if V.graph.scheduler:
        layout = V.graph.scheduler.get_buffer_layout(buf_name)
    else:
        buffer = V.graph.try_get_buffer(buf_name)
        # output arg
        if not buffer:
            assert buf_name == V.kernel.output_node.name
            layout = V.kernel.output_node.layout
        else:
            layout = buffer.get_layout()

    if isinstance(layout, torch._inductor.ir.NonOwningLayout):
        return not layout.maybe_guard_aligned()
    else:
        return False


def config_of(
    args: List[KernelArgType],
    *,
    indices: Optional[List[int]] = None,
) -> Any:
    if indices is None:
        indices = list(range(len(args)))

    def is_aligned(x: KernelArgType, alignment: int, include_tensor: bool) -> bool:
        """
        Roughly follow triton code here:
        https://github.com/openai/triton/blob/5282ed890d453e10b9ee30076ef89115dd197761/python/triton/runtime/jit.py#L208-L222
        """
        if isinstance(x, TensorArg):
            if include_tensor:
                offset_aligned = V.graph.sizevars.statically_known_multiple_of(
                    x.offset * x.dtype.itemsize, alignment  # type: ignore[arg-type]
                )
                return offset_aligned and not is_unaligned_buffer(x)
            else:
                return False
        if isinstance(x, SizeArg):
            # TODO(voz): These are kinda redundant, if we can solve out statically_known_multiple_of with
            # _maybe_evaluate_static...
            if x.name.startswith("load_seed_offset"):
                return False
            if x.expr is None:
                return False
            if isinstance(x.expr, float):
                return False
            return V.graph.sizevars.statically_known_multiple_of(x.expr, alignment)  # type: ignore[arg-type]
        if isinstance(x, WorkspaceArg):
            return V.graph.sizevars.statically_known_multiple_of(x.nbytes, alignment)  # type: ignore[arg-type]
        raise NotImplementedError(f"unhandled {type(x)}: {x}")

    if config.triton.divisible_by_16:
        divisible_by_16 = tuple(
            i
            for i, arg in zip(indices, args)
            if is_aligned(arg, alignment=16, include_tensor=True)
        )
    else:
        divisible_by_16 = ()
    divisible_by_8 = tuple(
        i
        for i, arg in zip(indices, args)
        if is_aligned(arg, alignment=8, include_tensor=False)
    )

    equal_to_1 = tuple(
        i
        for i, arg in zip(indices, args)
        if isinstance(arg, SizeArg)
        and isinstance(arg.expr, (int, sympy.Integer))
        and V.graph.sizevars.statically_known_equals(arg.expr, 1)  # type: ignore[arg-type]
    )
    # ids_of_folded_args is set from equal_to_1
    # and None args by the Triton compiler
    ids_of_folded_args = tuple(equal_to_1)

    return instance_descriptor(
        divisible_by_16, equal_to_1, ids_of_folded_args, divisible_by_8
    )
