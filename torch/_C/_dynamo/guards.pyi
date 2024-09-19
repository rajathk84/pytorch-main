# mypy: allow-untyped-defs
from typing import Any

import torch

class GlobalStateGuard:
    def check(self) -> bool: ...
    def reason(self) -> str: ...

class LeafGuard: ...
class GuardDebugInfo: ...

class GuardManager:
    def check(self, value) -> bool: ...
    def check_verbose(self, value) -> GuardDebugInfo: ...

    # Accessors
    def globals_dict_manager(
        self,
        f_globals: dict[str, Any],
        source,
        example_value,
        guard_manager_enum,
    ) -> GuardManager: ...
    def dict_getitem_manager(
        self,
        key,
        source,
        example_value,
        guard_manager_enum,
    ) -> GuardManager: ...
    def global_weakref_manager(
        self,
        global_name: str,
        source,
        example_value,
        guard_manager_enum,
    ) -> GuardManager: ...
    def type_manager(
        self,
        source,
        example_value,
        guard_manager_enum,
    ) -> GuardManager: ...
    def getattr_manager(
        self,
        attr: str,
        source,
        example_value,
        guard_manager_enum,
    ) -> GuardManager: ...
    def lambda_manager(
        self,
        python_lambda,
        source,
        example_value,
        guard_manager_enum,
    ) -> GuardManager: ...

    # Leaf guards
    def add_lambda_guard(self, user_lambda, verbose_code_parts: list[str]) -> None: ...
    def add_id_match_guard(self, id_val, verbose_code_parts: list[str]) -> None: ...
    def add_equals_match_guard(
        self,
        equals_val,
        verbose_code_parts: list[str],
    ) -> None: ...
    def add_global_state_guard(self, verbose_code_parts: list[str]) -> None: ...
    def add_torch_function_mode_stack_guard(
        self, initial_stack, verbose_code_parts: list[str]
    ) -> None: ...

class RootGuardManager(GuardManager):
    def get_epilogue_lambda_guards(self) -> list[LeafGuard]: ...
    def add_epilogue_lambda_guard(
        self,
        guard: LeafGuard,
        verbose_code_parts: list[str],
    ) -> None: ...

class DictGuardManager(GuardManager):
    def get_key_manager(
        self,
        index,
        source,
        example_value,
        guard_manager_enum,
    ) -> GuardManager: ...
    def get_value_manager(
        self,
        index,
        source,
        example_value,
        guard_manager_enum,
    ) -> GuardManager: ...

def install_object_aliasing_guard(
    guard_managers: list[GuardManager],
    tensor_names: list[str],
    verbose_code_parts: list[str],
): ...
def install_no_tensor_aliasing_guard(
    guard_managers: list[GuardManager],
    tensor_names: list[str],
    verbose_code_parts: list[str],
): ...

class TensorGuards:
    def __init__(
        self,
        *,
        dynamic_dims_sizes: list[torch.SymInt | None] | None = None,
        dynamic_dims_strides: list[torch.SymInt | None] | None = None,
    ) -> None: ...
    def check(self, *args) -> bool: ...
    def check_verbose(self, *args, tensor_check_names=None) -> bool | str: ...

def assert_size_stride(
    item: torch.Tensor,
    size: torch.types._size,
    stride: torch.types._size,
): ...
def check_obj_id(obj: object, expected: int) -> bool: ...
def check_type_id(obj: object, expected: int) -> bool: ...
def dict_version(d: dict[Any, Any]) -> int: ...
