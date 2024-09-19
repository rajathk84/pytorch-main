# mypy: allow-untyped-decorators
# mypy: allow-untyped-defs
# flake8: noqa C101
"""This module implements the user facing API for flex_attention in PyTorch."""
import functools
import inspect
import itertools
import math
import operator
from contextlib import nullcontext
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import torch
from torch import Tensor
from torch._higher_order_ops.flex_attention import (
    flex_attention as flex_attention_hop,
    TransformGetItemToIndex,
)
from torch._higher_order_ops.utils import _set_compilation_env
from torch.fx.experimental.proxy_tensor import (
    _temp_remove_metadata_torch_function_mode,
    _temp_remove_pre_dispatch_torch_function_mode,
)
from torch.nn.attention._utils import _supported_head_dim, _validate_sdpa_input
from torch.utils._pytree import tree_map_only


__all__ = [
    "BlockMask",
    "flex_attention",
    "create_block_mask",
    "create_mask",
    "or_masks",
    "and_masks",
    "noop_mask",
]

_score_mod_signature = Callable[[Tensor, Tensor, Tensor, Tensor, Tensor], Tensor]
_mask_mod_signature = Callable[[Tensor, Tensor, Tensor, Tensor], Tensor]


class _ModificationType(Enum):
    """Enum for the type of modification function.
    - SCORE_MOD: score_mod function which accepts a score as the first argument
    - mask_mod: mask function which does not accept a score and is only used for generating
    block mask
    """

    SCORE_MOD = 1
    MASK_MOD = 2
    UNKNOWN = 3


def _get_mod_type(fn: Callable) -> _ModificationType:
    """Get the type of modification function.
    This function inspects the number of positional arguments of the function to determine
    the type of modification function. If the function has 5 positional arguments, it is
    considered as a score_mod function. If the function has 4 positional arguments, it is
    considered as a mask function.
    """
    num_positional_args = sum(
        1
        for param in inspect.signature(fn).parameters.values()
        if param.default == inspect.Parameter.empty
    )
    assert num_positional_args == 5 or num_positional_args == 4
    if num_positional_args == 5:
        return _ModificationType.SCORE_MOD
    elif num_positional_args == 4:
        return _ModificationType.MASK_MOD
    else:
        return _ModificationType.UNKNOWN


# Need to define it here so that Dynamo doesn't skip it
def _vmap_for_bhqkv(
    fn: Callable,
    prefix: Tuple[Optional[int], ...],
    suffix: Tuple[Optional[int], ...] = (),
    out_dims: Union[int, List[Optional[int]]] = 0,
    group_dim: bool = False,
):
    """Used to vmap both score_mods and mask_mods over 4-dimensional/5-dimension inputs.
    Mapping over the [b, hq, q_idx, kv_idx] or [b, hkv, g, q_idx, kv_idx] dimensions.

    Args:
        fn (callable): The function to vmap.
        prefix (tuple): The prefix of the vmap. For score mod functions,
                        this should be set to (0,). For mask_mods = ()
        suffix (tuple): We need to add (0,) if gradOut is being mapped over,
                        and (None,) * len(other_buffers).
        out_dims (tuple): For forward cases, keep this as the default 0 since
                          we are only returning 1 output. For backwards, the joint
                          graph returns grads for B, H, Q_idx, KV_idx and other_buffers,
                          so we set this to (0, None, None, None, None) + (None,) * len(other_buffers).

    Returns:
        callable: The vmapped function.
    """
    # We vamp a function 4 times, broadcasting the [b, h, q_idx, kv_idx] dimensions
    dimensions: List[Tuple[None | int, None | int, None | int, None | int]] = []
    dimensions = [
        (None, None, None, 0),
        (None, None, 0, None),
        (None, 0, None, None),
    ]

    if group_dim:
        dimensions += [
            (None, 0, None, None),
        ]

    dimensions += [
        (0, None, None, None),
    ]

    for dims in dimensions:
        fn = torch.vmap(fn, in_dims=prefix + dims + suffix, out_dims=out_dims)
    return fn


def _identity(
    score: Tensor,
    batch: Tensor,
    head: Tensor,
    token_q: Tensor,
    token_kv: Tensor,
) -> Tensor:
    return score


def noop_mask(
    batch: Tensor,
    head: Tensor,
    token_q: Tensor,
    token_kv: Tensor,
) -> Tensor:
    """Returns a noop mask_mod"""
    return batch.new_ones(size=(), dtype=torch.bool, device=batch.device)


_DEFAULT_SPARSE_BLOCK_SIZE = 128
_LARGE_SPARSE_BLOCK_SIZE = 1 << 30


def _ordered_to_dense(num_blocks_in_row: Tensor, col_indices: Tensor):
    num_rows = col_indices.shape[-2]
    num_cols = col_indices.shape[-1]
    batch_dims = num_blocks_in_row.shape[:-1]
    device = num_blocks_in_row.device

    def create_dense_one(kv_num_blocks, kv_indices):
        dense_mask = kv_indices.new_zeros(num_rows, num_cols + 1, dtype=torch.int32)

        row_indices = torch.arange(num_rows, dtype=torch.int, device=device).unsqueeze(
            -1
        )
        col_range = torch.arange(num_cols, dtype=torch.int, device=device)
        index_mask = col_range < kv_num_blocks.unsqueeze(-1)

        # We write to one spot "out of bounds"
        valid_indices = torch.where(index_mask, kv_indices, num_cols)

        # set the values in 'a' to 1 where the indices are valid
        dense_mask[row_indices, valid_indices] = 1
        return dense_mask[:, :num_cols].contiguous()

    create_dense_batched = create_dense_one
    for _ in range(len(batch_dims)):
        create_dense_batched = torch.vmap(create_dense_batched, in_dims=(0, 0))

    out = create_dense_batched(num_blocks_in_row, col_indices)
    return out


def _dense_to_ordered(dense_mask) -> Tuple:
    dense_mask = dense_mask.to(dtype=torch.int32)
    num_blocks_in_row = dense_mask.sum(dim=-1)
    col_indices = torch.argsort(dense_mask, dim=-1, descending=True, stable=True)
    return (
        num_blocks_in_row.to(torch.int32).contiguous(),
        col_indices.to(torch.int32).contiguous(),
    )


def _transpose_ordered(num_blocks_in_row: Tensor, col_indices: Tensor):
    dense = _ordered_to_dense(num_blocks_in_row, col_indices)
    return _dense_to_ordered(dense.transpose(-2, -1))


class BlockMask:
    r"""
    BlockMask is our format for representing a block-sparse attention mask.
    It is somewhat of a cross in-between BCSR and a non-sparse format.

    Basics
    ------
    A block-sparse mask means that instead of representing the sparsity of
    individual elements in the mask, a KV_BLOCK_SIZE x Q_BLOCK_SIZE block is
    considered sparse only if every element within that block is sparse.
    This aligns well with hardware, which generally expects to perform
    contiguous loads and computation.

    This format is primarily optimized for 1. simplicity, and 2. kernel
    efficiency. Notably, it is *not* optimized for size, as this mask is always
    reduced by a factor of KV_BLOCK_SIZE * Q_BLOCK_SIZE. If the size is a
    concern, the tensors can be reduced in size by increasing the block size.

    The essentials of our format are:

    num_blocks_in_row: Tensor[ROWS]:
    Describes the number of blocks present in each row.

    col_indices: Tensor[ROWS, MAX_BLOCKS_IN_COL]:
    `col_indices[i]` is the sequence of block positions for row i. The values of
    this row after `col_indices[i][num_blocks_in_row[i]]` are undefined.

    For example, to reconstruct the original tensor from this format:

    .. code-block:: python

        dense_mask = torch.zeros(ROWS, COLS)
        for row in range(ROWS):
            for block_idx in range(num_blocks_in_row[row]):
                dense_mask[row, col_indices[row, block_idx]] = 1

    Notably, this format makes it easier to implement a reduction along the
    *rows* of the mask.

    Details
    -------
    The basics of our format require only kv_num_blocks and kv_indices. But, we
    have up to 8 tensors on this object. This represents 4 pairs:

    1. (kv_num_blocks, kv_indices): Used for the forwards pass of attention, as
    we reduce along the KV dimension.

    2. [OPTIONAL] (full_kv_num_blocks, full_kv_indices): This is optional and
    purely an optimization. As it turns out, applying masking to every block
    is quite expensive! If we specifically know which blocks are "full" and
    don't require masking at all, then we can skip applying mask_mod to these
    blocks. This requires the user to split out a separate mask_mod from the
    score_mod. For causal masks, this is about a 15% speedup.

    3. [GENERATED] (q_num_blocks, q_indices): Required for the backwards pass,
    as computing dKV requires iterating along the mask along the Q dimension. These are autogenerated from 1.

    4. [GENERATED] (full_q_num_blocks, full_q_indices): Same as above, but for
    the backwards pass. These are autogenerated from 2.
    """
    kv_num_blocks: Tensor
    kv_indices: Tensor
    full_kv_num_blocks: Optional[Tensor]
    full_kv_indices: Optional[Tensor]
    q_num_blocks: Optional[Tensor]
    q_indices: Optional[Tensor]
    full_q_num_blocks: Optional[Tensor]
    full_q_indices: Optional[Tensor]
    BLOCK_SIZE: Tuple[int, int]
    mask_mod: _mask_mod_signature

    def __init__(
        self,
        kv_num_blocks: Tensor,
        kv_indices: Tensor,
        full_kv_num_blocks: Optional[Tensor],
        full_kv_indices: Optional[Tensor],
        q_num_blocks: Optional[Tensor],
        q_indices: Optional[Tensor],
        full_q_num_blocks: Optional[Tensor],
        full_q_indices: Optional[Tensor],
        BLOCK_SIZE: Tuple[int, int],
        mask_mod: _mask_mod_signature,
    ):
        if kv_indices.dim() < 2:
            raise RuntimeError("BlockMask must have at least 2 dimensions")
        assert kv_num_blocks is not None, "kv_num_blocks must be provided"
        assert kv_indices is not None, "kv_indices must be provided"
        assert q_num_blocks is not None, "q_num_blocks must be provided"
        assert q_indices is not None, "q_indices must be provided"
        assert (full_kv_num_blocks is None) == (
            full_kv_indices is None
        ), "full_kv_num_blocks and full_kv_indices must be both provided or omitted"
        assert (full_q_num_blocks is None) == (
            full_q_indices is None
        ), "full_q_num_blocks and full_q_indices must be both provided or omitted"

        self.kv_num_blocks = kv_num_blocks
        self.kv_indices = kv_indices
        self.full_kv_num_blocks = full_kv_num_blocks
        self.full_kv_indices = full_kv_indices
        self.q_num_blocks = q_num_blocks
        self.q_indices = q_indices
        self.full_q_num_blocks = full_q_num_blocks
        self.full_q_indices = full_q_indices
        self.BLOCK_SIZE = BLOCK_SIZE
        self.mask_mod = mask_mod

    @classmethod
    def from_kv_blocks(
        cls,
        kv_num_blocks: Tensor,
        kv_indices: Tensor,
        full_kv_num_blocks: Optional[Tensor] = None,
        full_kv_indices: Optional[Tensor] = None,
        BLOCK_SIZE: Union[int, Tuple[int, int]] = _DEFAULT_SPARSE_BLOCK_SIZE,
        mask_mod: Optional[_mask_mod_signature] = None,
    ):
        """
        Creates a BlockMask instance from key-value block information.

        Args:
            kv_num_blocks (Tensor): Number of kv_blocks in each Q_BLOCK_SIZE row tile.
            kv_indices (Tensor): Indices of key-value blocks in each Q_BLOCK_SIZE row tile.
            full_kv_num_blocks (Optional[Tensor]): Number of full kv_blocks in each Q_BLOCK_SIZE row tile.
            full_kv_indices (Optional[Tensor]): Indices of full key-value blocks in each Q_BLOCK_SIZE row tile.
            BLOCK_SIZE (Union[int, Tuple[int, int]]): Size of KV_BLOCK_SIZE x Q_BLOCK_SIZE tiles.
            mask_mod (Optional[Callable]): Function to modify the mask.

        Returns:
            BlockMask: Instance with full Q information generated via _transposed_ordered

        Raises:
            RuntimeError: If kv_indices has < 2 dimensions.
            AssertionError: If only one of full_kv_* args is provided.
        """
        if kv_indices.dim() < 2:
            raise RuntimeError("BlockMask must have at least 2 dimensions")

        assert (full_kv_num_blocks is None) == (
            full_kv_indices is None
        ), "full_kv_num_blocks and full_kv_indices must be both provided or omitted"

        # Generate q_num_blocks and q_indices
        q_num_blocks, q_indices = _transpose_ordered(kv_num_blocks, kv_indices)
        if full_kv_num_blocks is not None:
            assert full_kv_indices is not None
            full_q_num_blocks, full_q_indices = _transpose_ordered(
                full_kv_num_blocks, full_kv_indices
            )
        else:
            full_q_num_blocks, full_q_indices = None, None

        if isinstance(BLOCK_SIZE, int):
            BLOCK_SIZE = (BLOCK_SIZE, BLOCK_SIZE)

        mask_mod = mask_mod if mask_mod is not None else noop_mask

        return cls(
            kv_num_blocks=kv_num_blocks,
            kv_indices=kv_indices,
            full_kv_num_blocks=full_kv_num_blocks,
            full_kv_indices=full_kv_indices,
            q_num_blocks=q_num_blocks,
            q_indices=q_indices,
            full_q_num_blocks=full_q_num_blocks,
            full_q_indices=full_q_indices,
            BLOCK_SIZE=BLOCK_SIZE,
            mask_mod=mask_mod,
        )

    def as_tuple(self, flatten: bool = True):
        """
        Returns a tuple of the attributes of the BlockMask.

        Args:
            flatten (bool): If True, it will flatten the tuple of (KV_BLOCK_SIZE, Q_BLOCK_SIZE)
        """
        block_size = (
            (self.BLOCK_SIZE[0], self.BLOCK_SIZE[1]) if flatten else (self.BLOCK_SIZE,)
        )

        return (
            self.kv_num_blocks,
            self.kv_indices,
            self.full_kv_num_blocks,
            self.full_kv_indices,
            self.q_num_blocks,
            self.q_indices,
            self.full_q_num_blocks,
            self.full_q_indices,
            *block_size,
            self.mask_mod,
        )

    def __str__(self):
        s = f"BlockMask(shape={self.shape}, sparsity={self.sparsity():.2f}%, \n"
        mask_str = self.to_string().strip()
        s += mask_str
        s += "\n)"
        return s

    def __getitem__(self, index) -> "BlockMask":
        """
        Returns a new BlockMask instance by getting the mask for the given index position.

        Args:
            index: Index to apply to all attributes.

        Example Usage:
            .. code-block:: python

                def causal_mask(b, h, q_idx, kv_idx):
                    return q_idx >= kv_idx

                block_mask = create_block_mask(causal_mask, 4, 2, 512, 512, device="cuda")
                assert block_mask.kv_num_blocks.shape == (4,2,4)
                assert block_mask.kv_indices.shape == (4,2,4,4)

                # Index on batch dimension
                new_block_mask = block_mask[0]
                assert new_block_mask.kv_num_blocks.shape == (2,4)
                assert new_block_mask.kv_indices.shape == (2,4,4)

                # Index on batch and head dimension
                new_block_mask = block_mask[0, 1]
                assert new_block_mask.kv_num_blocks.shape == (4,)
                assert new_block_mask.kv_indices.shape == (4,4)

                # slicing on batch and head dimension
                new_block_mask = block_mask[0:2, 1:2]
                assert new_block_mask.kv_num_blocks.shape == (2,1,4)
                assert new_block_mask.kv_indices.shape == (2,1,4,4)

                # slicing on batch, head, and query dimension
                new_block_mask = block_mask[0:2, 1:2, torch.tensor([1], dtype=torch.int32)]
                assert new_block_mask.kv_num_blocks.shape == (2,1,1)
                assert new_block_mask.kv_indices.shape == (2,1,1,4)
        """
        new_kv_num_blocks = self.kv_num_blocks[index]
        new_kv_indices = self.kv_indices[index]
        if self.full_kv_num_blocks is not None:
            assert self.full_kv_indices is not None
            new_full_kv_num_blocks = self.full_kv_num_blocks[index]
            new_full_kv_indices = self.full_kv_indices[index]
        else:
            new_full_kv_num_blocks = None
            new_full_kv_indices = None
        return BlockMask.from_kv_blocks(
            new_kv_num_blocks,
            new_kv_indices,
            new_full_kv_num_blocks,
            new_full_kv_indices,
            BLOCK_SIZE=self.BLOCK_SIZE,
            mask_mod=None,
        )

    def __repr__(self):
        def shape_or_none(x: Optional[torch.Tensor]):
            return x.shape if x is not None else None

        return (
            f"BlockMask(\n"
            f"    kv_num_blocks={self.kv_num_blocks.shape},\n"
            f"    kv_indices={self.kv_indices.shape},\n"
            f"    full_kv_num_blocks={shape_or_none(self.full_kv_num_blocks )},\n"
            f"    full_kv_indices={shape_or_none(self.full_kv_indices)},\n"
            f"    q_num_blocks={shape_or_none(self.q_num_blocks)},\n"
            f"    q_indices={shape_or_none(self.q_indices)},\n"
            f"    full_q_num_blocks={shape_or_none(self.full_q_num_blocks)},\n"
            f"    full_q_indices={shape_or_none(self.full_q_indices)},\n"
            f"    BLOCK_SIZE={self.BLOCK_SIZE},\n"
            f"    shape={self.shape},\n"
            f"    sparsity={self.sparsity():.2f}%,\n"
            f"    mask_mod={self.mask_mod.__name__ if hasattr(self.mask_mod, '__name__') else self.mask_mod}\n"
            f")"
        )

    @property
    def shape(self):
        """Returns the shape of the mask."""
        *batch_dims, q_length, _ = self.kv_indices.shape
        q_length = self.kv_indices.shape[-2] * self.BLOCK_SIZE[0]
        kv_length = self.kv_indices.shape[-1] * self.BLOCK_SIZE[1]
        return tuple(batch_dims + [q_length, kv_length])

    def numel(self):
        """Returns the number of elements (not accounting for sparsity) in the mask."""
        shape = self.shape

        def _prod(xs):
            return functools.reduce(operator.mul, xs, 1)

        return _prod(shape)

    def sparsity(self) -> float:
        """Computes the percentage of blocks that are sparse (i.e. not computed)"""
        total_size = self.numel()
        computed_blocks = self.kv_num_blocks.sum()
        if self.full_kv_num_blocks is not None:
            computed_blocks += self.full_kv_num_blocks.sum()

        computed_size = computed_blocks.item() * self.BLOCK_SIZE[0] * self.BLOCK_SIZE[1]
        dense_ratio = computed_size / total_size
        return 100 * (1 - dense_ratio)

    def to_dense(self) -> Tensor:
        """Returns a dense block that is equivalent to the block mask."""
        partial_dense = _ordered_to_dense(self.kv_num_blocks, self.kv_indices)
        if self.full_kv_num_blocks is not None:
            assert self.full_kv_indices is not None
            return partial_dense | _ordered_to_dense(
                self.full_kv_num_blocks, self.full_kv_indices
            )
        return partial_dense

    def to_string(self, grid_size=(20, 20), limit=4):
        """Returns a string representation of the block mask. Quite nifty.

        If grid_size is None, prints out an uncompressed version. Warning, it can be quite big!
        """
        dense_mask = self.to_dense()
        *batch_dims, num_rows, num_cols = dense_mask.shape
        if isinstance(grid_size, int):
            max_rows = grid_size
            max_cols = grid_size
        elif grid_size == -1:
            max_rows = num_rows
            max_cols = num_cols
        else:
            max_rows, max_cols = grid_size

        def create_block_vis(*batch_idx):
            descriptors = []

            descriptors.append(f"{batch_idx}")

            vis = ", ".join(reversed(descriptors)) + "\n"

            def summarize_section(section):
                percentage = section.float().mean().item()
                if percentage == 1:
                    return "█"
                elif percentage == 0:
                    return " "
                else:
                    return "░"

            def cdiv(a, b):
                return (a + (b - 1)) // b

            row_step = max(1, cdiv(num_rows, max_rows))
            col_step = max(1, cdiv(num_cols, max_cols))

            for r in range(0, num_rows, row_step):
                for c in range(0, num_cols, col_step):
                    cur_mask = dense_mask
                    for idx in batch_idx:
                        cur_mask = cur_mask[idx]
                    char = summarize_section(
                        cur_mask[r : r + row_step, c : c + col_step]
                    )
                    vis += char * 2
                vis += "\n"
            return vis

        total_vis = []
        for idx, batch_idx in enumerate(
            itertools.product(*[range(i) for i in batch_dims])
        ):
            if idx == limit:
                total_vis.append("...")
                total_vis.append("To print out more, set BlockMask.to_string(limit=N)")
                total_vis.append(
                    "You can also index (BlockMask[batch, head]) to choose a specific batch or head"
                )
                break
            block_vis = create_block_vis(*batch_idx)
            total_vis.append(block_vis)

        return "\n".join(total_vis)

    def to(self, device: Union[torch.device, str]) -> "BlockMask":
        """Moves the BlockMask to the specified device.

        Args:
            device (torch.device or str): The target device to move the BlockMask to.
                Can be a torch.device object or a string (e.g., 'cpu', 'cuda:0').

        Returns:
            BlockMask: A new BlockMask instance with all tensor components moved
            to the specified device.

        Note:
            This method does not modify the original BlockMask in-place.
            Instead, it returns a new BlockMask instance where invidual tensor attributes
            may or may not be moved to the specified device, depending on their
            current device placement.
        """
        mapped_attributes = tree_map_only(
            torch.Tensor,
            lambda x: x.to(device),
            self.as_tuple(flatten=False),
        )
        return BlockMask(*mapped_attributes)


def _broadcast_to_dim(x, dim):
    while x.dim() < dim:
        x = x.unsqueeze(0)
    return x


def _round_up_to_multiple(x, multiple):
    return (x + multiple - 1) // multiple * multiple


def _convert_mask_to_block_mask(
    mask: Tensor,
    KV_BLOCK_SIZE=_DEFAULT_SPARSE_BLOCK_SIZE,
    Q_BLOCK_SIZE=_DEFAULT_SPARSE_BLOCK_SIZE,
    separate_full_blocks: bool = False,
) -> Tuple[Tensor, Optional[Tensor]]:
    assert mask.dtype == torch.bool
    mask = _broadcast_to_dim(mask, 4)
    B, H, Q, KV = mask.shape
    assert Q % Q_BLOCK_SIZE == 0
    assert KV % KV_BLOCK_SIZE == 0
    mask = mask.view(
        B, H, Q // Q_BLOCK_SIZE, Q_BLOCK_SIZE, KV // KV_BLOCK_SIZE, KV_BLOCK_SIZE
    )  # [B, H, Q//Q_BLOCK_SIZE, Q_BLOCK_SIZE, KV//KV_BLOCK_SIZE, KV_BLOCK_SIZE]
    mask = mask.permute(
        0, 1, 2, 4, 3, 5
    )  # [B, H, Q//Q_BLOCK_SIZE, KV//KV_BLOCK_SIZE, Q_BLOCK_SIZE, KV_BLOCK_SIZE]
    mask_block_sum = mask.sum(
        dim=[-2, -1]
    )  # [B, H, Q//Q_BLOCK_SIZE, KV//KV_BLOCK_SIZE]
    if separate_full_blocks:
        full_block_sum = Q_BLOCK_SIZE * KV_BLOCK_SIZE
        full_blocks = mask_block_sum == full_block_sum
        partial_blocks = (mask_block_sum > 0) & (mask_block_sum < full_block_sum)
        partial_blocks = partial_blocks.to(dtype=torch.int8)
        full_blocks = full_blocks.to(dtype=torch.int8)
        return partial_blocks, full_blocks
    else:
        partial_blocks = mask_block_sum > 0
        partial_blocks = partial_blocks.to(dtype=torch.int8)
        return partial_blocks, None


def or_masks(*mask_mods: _mask_mod_signature) -> _mask_mod_signature:
    """Returns a mask_mod that's the union of provided mask_mods"""
    if not all(callable(arg) for arg in mask_mods):
        raise RuntimeError(f"All inputs should be callable mask_mods: {mask_mods}")

    def or_mask(b, h, q_idx, kv_idx):
        result = b.new_zeros((), dtype=torch.bool)
        for mask in mask_mods:
            result = result | mask(b, h, q_idx, kv_idx)
        return result

    return or_mask


def and_masks(*mask_mods: _mask_mod_signature) -> _mask_mod_signature:
    """Returns a mask_mod that's the intersection of provided mask_mods"""
    if not all(callable(arg) for arg in mask_mods):
        raise RuntimeError(f"All inputs should be callable mask_mods: {mask_mods}")

    def and_mask(b, h, q_idx, kv_idx):
        result = b.new_ones((), dtype=torch.bool)
        for mask in mask_mods:
            result = result & mask(b, h, q_idx, kv_idx)
        return result

    return and_mask


def _convert_block_mask_to_mask(
    block_mask,
    KV_BLOCK_SIZE=_DEFAULT_SPARSE_BLOCK_SIZE,
    Q_BLOCK_SIZE=_DEFAULT_SPARSE_BLOCK_SIZE,
) -> Tensor:
    assert block_mask.dim() == 4
    B, H, Q, KV = block_mask.shape
    block_mask = block_mask.expand(Q_BLOCK_SIZE, KV_BLOCK_SIZE, *block_mask.shape)
    block_mask = block_mask.permute(2, 3, 4, 0, 5, 1).reshape(
        B, H, Q * Q_BLOCK_SIZE, KV * KV_BLOCK_SIZE
    )
    return block_mask


def _create_sparse_block_from_block_mask(
    block_mask: Tuple[Tensor, Optional[Tensor]],
    mask_mod: Optional[Callable],
    KV_BLOCK_SIZE: int = _DEFAULT_SPARSE_BLOCK_SIZE,
    Q_BLOCK_SIZE: int = _DEFAULT_SPARSE_BLOCK_SIZE,
) -> BlockMask:
    partial_blocks, full_blocks = block_mask

    partial_bm = _dense_to_ordered(partial_blocks)
    if full_blocks is not None:
        full_bm = _dense_to_ordered(full_blocks)
    else:
        full_bm = (None, None)

    return BlockMask.from_kv_blocks(
        partial_bm[0],
        partial_bm[1],
        full_bm[0],
        full_bm[1],
        BLOCK_SIZE=(KV_BLOCK_SIZE, Q_BLOCK_SIZE),
        mask_mod=mask_mod,
    )


def create_mask(
    mod_fn: Union[_score_mod_signature, _mask_mod_signature],
    B: Optional[int],
    H: Optional[int],
    Q_LEN: int,
    KV_LEN: int,
    device: str = "cuda",
    _compile: bool = False,
) -> Tensor:
    r"""This function creates a mask tensor from a mod_fn function.

    Args:
        mod_fn (Union[_score_mod_signature, _mask_mod_signature]): Function to modify attention scores.
        B (int): Batch size.
        H (int): Number of query heads.
        Q_LEN (int): Sequence length of query.
        KV_LEN (int): Sequence length of key/value.
        device (str): Device to run the mask creation on.

    Returns:
        mask (Tensor): A mask tensor with shape (B, H, M, N).
    """
    if B is None:
        B = 1
    if H is None:
        H = 1
    b = torch.arange(0, B, device=device)
    h = torch.arange(0, H, device=device)
    m = torch.arange(0, Q_LEN, device=device)
    n = torch.arange(0, KV_LEN, device=device)
    # TODO: fix this
    # Lack instantiation support for __torch_function__ mode support under compile
    if _compile:
        ctx = nullcontext()
    else:
        ctx = TransformGetItemToIndex()  # type: ignore[assignment]
    mod_type = _get_mod_type(mod_fn)

    with ctx:
        if mod_type == _ModificationType.SCORE_MOD:
            score_mod = mod_fn
            score_mod = _vmap_for_bhqkv(score_mod, prefix=(0,))  # first input is score
            out = score_mod(torch.zeros(B, H, Q_LEN, KV_LEN, device=device), b, h, m, n)
            mask = torch.where(torch.isneginf(out), False, True)
            return mask
        elif mod_type == _ModificationType.MASK_MOD:
            mask_mod = mod_fn
            mask_mod = _vmap_for_bhqkv(mask_mod, prefix=())
            mask = mask_mod(b, h, m, n)
            return mask
        else:
            raise AssertionError


def _create_block_mask_inner(
    mask_mod: Callable,
    B: int,
    H: int,
    Q_LEN: int,
    KV_LEN: int,
    device: str,
    KV_BLOCK_SIZE: int,
    Q_BLOCK_SIZE: int,
):
    r"""Work around for being unable to instantiate __torch_function__ mode under compile.
    `create_block_mask` will compile this inner function and wrap the call to this
    with the __torch_function__ mode.
    """
    mask_tensor = create_mask(mask_mod, B, H, Q_LEN, KV_LEN, device, _compile=True)
    partial_block_mask, full_block_mask = _convert_mask_to_block_mask(
        mask_tensor,
        KV_BLOCK_SIZE=KV_BLOCK_SIZE,
        Q_BLOCK_SIZE=Q_BLOCK_SIZE,
        separate_full_blocks=True,
    )
    return partial_block_mask, full_block_mask


def create_block_mask(
    mask_mod: _mask_mod_signature,
    B: Optional[int],
    H: Optional[int],
    Q_LEN: int,
    KV_LEN: int,
    device: str = "cuda",
    BLOCK_SIZE: Union[int, Tuple[int, int]] = _DEFAULT_SPARSE_BLOCK_SIZE,
    _compile=False,
) -> BlockMask:
    r"""This function creates a block mask tuple from a mask_mod function.

    Args:
        mask_mod (Callable): mask_mod function. This is a callable that defines the
            masking pattern for the attention mechanism. It takes four arguments:
            b (batch size), h (number of heads), q_idx (query index), and kv_idx (key/value index).
            It should return a boolean tensor indicating which attention connections are allowed (True)
            or masked out (False).
        B (int): Batch size.
        H (int): Number of query heads.
        Q_LEN (int): Sequence length of query.
        KV_LEN (int): Sequence length of key/value.
        device (str): Device to run the mask creation on.
        KV_BLOCK_SIZE (int): Block size of block mask for each query.
        Q_BLOCK_SIZE (int): Block size of block mask for each key/value.
        _compile (bool): Whether to compile the mask creation.

    Returns:
        BlockMask:  A BlockMask object that contains the block mask information.

    Example Usage:
        .. code-block:: python

            def causal_mask(b, h, q_idx, kv_idx):
                return q_idx >= kv_idx

            block_mask = create_block_mask(causal_mask, 1, 1, 8192, 8192, device="cuda")
            query = torch.randn(1, 1, 8192, 64, device="cuda", dtype=torch.float16)
            key = torch.randn(1, 1, 8192, 64, device="cuda", dtype=torch.float16)
            value = torch.randn(1, 1, 8192, 64, device="cuda", dtype=torch.float16)
            output = flex_attention(query, key, value, block_mask=block_mask)
    """
    mod_type = _get_mod_type(mask_mod)
    assert (
        mod_type == _ModificationType.MASK_MOD
    ), f"create-block_mask requires a mask_mod function! Got {mask_mod}"
    inner_func = _create_block_mask_inner
    if B is None:
        B = 1
    if H is None:
        H = 1
    if isinstance(BLOCK_SIZE, int):
        Q_BLOCK_SIZE = BLOCK_SIZE
        KV_BLOCK_SIZE = BLOCK_SIZE
    else:
        Q_BLOCK_SIZE, KV_BLOCK_SIZE = BLOCK_SIZE

    if Q_LEN < 128:
        Q_BLOCK_SIZE = Q_LEN
    else:
        Q_LEN = _round_up_to_multiple(Q_LEN, Q_BLOCK_SIZE)
    KV_LEN = _round_up_to_multiple(KV_LEN, KV_BLOCK_SIZE)
    if _compile:
        inner_func = torch.compile(inner_func, fullgraph=True, dynamic=False)
    with TransformGetItemToIndex():
        partial_block_mask, full_block_mask = inner_func(
            mask_mod, B, H, Q_LEN, KV_LEN, device, KV_BLOCK_SIZE, Q_BLOCK_SIZE
        )
        block_mask = _create_sparse_block_from_block_mask(
            (partial_block_mask, full_block_mask), mask_mod
        )
    return block_mask


def _create_empty_block_mask(query: Tensor, key: Tensor) -> BlockMask:
    r"""Default block mask for flex attention.
    If users don't specify any block sparse mask info, we create this
    empty block sparse mask. Which creates a BlockMask with 1 block that is the full length
    of the query and key tensors.
    """
    device = query.device
    return BlockMask.from_kv_blocks(
        kv_num_blocks=torch.ones([1, 1, 1], dtype=torch.int32, device=device),
        kv_indices=torch.zeros([1, 1, 1, 1], dtype=torch.int32, device=device),
        BLOCK_SIZE=_LARGE_SPARSE_BLOCK_SIZE,
    )


def _apply_kernel_options(
    query: Tensor, key: Tensor, value: Tensor, return_lse: bool, kernel_options
):
    kernel_options = {} if kernel_options is None else dict(kernel_options)

    kernel_options.setdefault("ROWS_GUARANTEED_SAFE", False)
    kernel_options.setdefault("PRESCALE_QK", False)

    # If foward kernel needs to return logsumexp is decided by this rule internally.
    assert "OUTPUT_LOGSUMEXP" not in kernel_options
    kernel_options["OUTPUT_LOGSUMEXP"] = True
    if not return_lse:
        any_inputs_require_grad = (
            query.requires_grad or key.requires_grad or value.requires_grad
        )
        output_logsumexp = any_inputs_require_grad and torch.is_grad_enabled()
        kernel_options["OUTPUT_LOGSUMEXP"] = output_logsumexp

    return kernel_options


def _validate_embed_dim(query: Tensor, key: Tensor, value: Tensor):
    if query.size(-1) != key.size(-1):
        raise ValueError(
            f"Expect query and key/value to have the same embedding dimension "
            f"but got E={query.size(-1)} and E={key.size(-1)}."
        )
    # TODO this config segfaults with Triton without:
    # https://github.com/triton-lang/triton/pull/4540
    if not (
        _supported_head_dim(query.size(-1)) and _supported_head_dim(value.size(-1))
    ):
        raise ValueError(
            f"NYI: Currently non power of 2 embedding dimension are not supported. "
            f"Got E={query.size(-1)} and Ev={value.size(-1)}."
        )
    if value.size(-1) > query.size(-1):
        raise ValueError(
            f"NYI: Currently value embedding dimension must be less than or equal to query embedding dimension. "
            f"Got Ev={value.size(-1)} and E={query.size(-1)}."
        )


def flex_attention(
    query: Tensor,
    key: Tensor,
    value: Tensor,
    score_mod: Optional[_score_mod_signature] = None,
    block_mask: Optional[BlockMask] = None,
    scale: Optional[float] = None,
    enable_gqa: bool = False,
    return_lse: bool = False,
    kernel_options: Optional[Dict[str, Any]] = None,
) -> Union[Tensor, Tuple[Tensor, Tensor]]:
    r"""This function implements scaled dot product attention with an arbitrary attention score modification function.

    This function computes the scaled dot product attention between query, key, and value tensors with a user-defined
    attention score modification function. The attention score modification function will be applied after the attention
    scores have been calculated between the query and key tensors. The attention scores are calculated as follows:

    The ``score_mod`` function should have the following signature:

    .. code-block:: python

        def score_mod(
            score: Tensor,
            batch: Tensor,
            head: Tensor,
            q_idx: Tensor,
            k_idx: Tensor
        ) -> Tensor:

    Where:
        - ``score``: A scalar tensor representing the attention score,
          with the same data type and device as the query, key, and value tensors.
        - ``batch``, ``head``, ``q_idx``, ``k_idx``: Scalar tensors indicating
          the batch index, query head index, query index, and key/value index, respectively.
          These should have the ``torch.int`` data type and be located on the same device as the score tensor.

    Args:
        query (Tensor): Query tensor; shape :math:`(B, Hq, L, E)`.
        key (Tensor): Key tensor; shape :math:`(B, Hkv, S, E)`.
        value (Tensor): Value tensor; shape :math:`(B, Hkv, S, Ev)`.
        score_mod (Optional[Callable]): Function to modify attention scores. By default no score_mod is applied.
        block_mask (Optional[BlockMask]): BlockMask object that controls the blocksparsity pattern of the attention.
        scale (Optional[float]): Scaling factor applied prior to softmax. If none, the default value is set to :math:`\frac{1}{\sqrt{E}}`.
        enable_gqa (bool): If set to True, enables Grouped Query Attention (GQA) and broadcasts key/value heads to query heads.
        return_lse (bool): Whether to return the logsumexp of the attention scores. Default is False.
        kernel_options (Optional[Dict[str, Any]]): Options to pass into the Triton kernels.

    Returns:
        output (Tensor): Attention output; shape :math:`(B, Hq, L, Ev)`.

    Shape legend:
        - :math:`N: \text{Batch size} ... : \text{Any number of other batch dimensions (optional)}`
        - :math:`S: \text{Source sequence length}`
        - :math:`L: \text{Target sequence length}`
        - :math:`E: \text{Embedding dimension of the query and key}`
        - :math:`Ev: \text{Embedding dimension of the value}`

    .. warning::
        `torch.nn.attention.flex_attention` is a prototype feature in PyTorch.
        Please look forward to a more stable implementation in a future version of PyTorch.
        Read more about feature classification at: https://pytorch.org/blog/pytorch-feature-classification-changes/#prototype

    """
    # Some basic input validation
    _validate_sdpa_input(query, key, value)
    _validate_embed_dim(query, key, value)
    if query.dim() != 4 or key.dim() != 4 or value.dim() != 4:
        raise NotImplementedError("NYI: query, key, and value must be 4D tensors")
    if (not enable_gqa) and query.size(-3) != key.size(-3):
        raise ValueError(
            f"Expect query and key/value to have the same number of heads "
            f"but got Hq={query.size(-3)} and Hkv={key.size(-3)}. "
            f"Try setting enable_gqa=True for GQA."
        )
    if enable_gqa:
        Hq = query.size(1)
        Hkv = key.size(1)
        if Hq % Hkv != 0:
            raise ValueError(
                f"Expect number of query heads to be a multiple of kv heads for GQA "
                f"but got Hq={Hq} and Hkv={Hkv}."
            )

    if score_mod is None:
        score_mod = _identity
    if block_mask is None:
        block_mask = _create_empty_block_mask(query, key)
    if scale is None:
        scale = 1.0 / math.sqrt(query.size(-1))

    if query.device != block_mask.kv_num_blocks.device:
        raise RuntimeError(
            f"Expect q/k/v and block_mask to be on the same device "
            f"but got {query.device} and {block_mask.kv_num_blocks.device}."
        )

    kernel_options = _apply_kernel_options(
        query,
        key,
        value,
        return_lse,
        kernel_options,
    )

    if torch.compiler.is_dynamo_compiling():
        # mark head_dim and number of heads to be static
        for x in [query, key, value]:
            torch._dynamo.mark_static(x, -3)
            torch._dynamo.mark_static(x, -1)
        out, lse = flex_attention_hop(
            query, key, value, score_mod, block_mask.as_tuple(), scale, kernel_options
        )
        if return_lse:
            return out, lse * math.log(2)
        else:
            return out

    if not torch._dynamo.is_dynamo_supported():
        raise RuntimeError("flex_attention requires dynamo support")

    from torch._dynamo.backends.debugging import (
        make_eager_backend_with_torch_function_mode,
    )

    # Dynamo is expecting a callable with "__code__" attribute.
    # We cannot directly pass hop to it. So we wrap it in a dummy function.
    def _flex_attention_hop_wrapper(*args, **kwargs):
        return flex_attention_hop(*args, **kwargs)

    with _set_compilation_env():
        with torch._dynamo.utils.disable_cache_limit():
            with _temp_remove_pre_dispatch_torch_function_mode():
                with _temp_remove_metadata_torch_function_mode() as metadata_mode:
                    if metadata_mode:
                        backend = make_eager_backend_with_torch_function_mode(
                            metadata_mode
                        )
                    else:
                        backend = "eager"
                    out, lse = torch.compile(
                        _flex_attention_hop_wrapper, backend=backend, fullgraph=True
                    )(
                        query,
                        key,
                        value,
                        score_mod,
                        block_mask.as_tuple(),
                        scale,
                        kernel_options,
                    )
                    if return_lse:
                        return out, lse * math.log(2)
                    else:
                        return out
