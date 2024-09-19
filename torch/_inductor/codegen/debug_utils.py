# mypy: allow-untyped-defs
from __future__ import annotations

import functools
import logging
import os
from enum import Enum
from typing import List, Optional

from torch import dtype as torch_dtype

from .. import config
from ..virtualized import V
from .multi_kernel import MultiKernel


log = logging.getLogger(__name__)


def _print_debugging_tensor_value_info(msg, arg):
    # helper for printing debugging stats for intermediate tensor values
    # at jit inductor level codegen
    max_numel_to_print = 64
    print(msg)
    numel = arg.float().numel()
    # print the debug printing stats
    if numel <= max_numel_to_print:
        print(arg)
    print("Number of elements: ", numel)
    print("Size: ", arg.float().size())
    print("Dtype: ", arg.float().mean().item())
    print("Mean: ", arg.float().mean().item())
    print("Min: ", arg.float().min().item())
    print("Max: ", arg.float().max().item())
    print("Std: ", arg.float().std().item())


# AOTI debug printing related configs
class IntermediateValueDebuggingLevel(Enum):
    # OFF: No intermediate tensor value debug info will be printed or saved.
    OFF = "0"
    # LEVEL 1: Save all intermediate tensor values to individual `.pt` files. No debug printing will be displayed.
    SAVE_ONLY = "1"
    # LEVEL 2: Print all intermediate tensor values by default to the console. No debug saving will be performed.
    PRINT_ONLY = "2"


class DebugPrinterManager:
    def __init__(
        self,
        debug_printer_level,
        args_to_print_or_save: Optional[List[str]] = None,
        kernel_name: str = "",
        kernel=None,
        arg_signatures: Optional[List[type]] = None,
    ):
        self.debug_printer_level = IntermediateValueDebuggingLevel(debug_printer_level)
        if args_to_print_or_save is None:
            args_to_print_or_save = []
        self.args_to_print_or_save = args_to_print_or_save
        self.kernel_name = kernel_name
        self.arg_signatures: Optional[List[type]] = None
        self.kernel = kernel
        self.filtered_kernel_names_to_print = self._get_debug_filtered_kernel_names()

    def __enter__(self):
        self._perform_debug_print_or_save_helper(
            self.args_to_print_or_save,
            self.kernel_name,
            before_launch=True,
            arg_signatures=self.arg_signatures,
        )

    def __exit__(self, args_to_print_or_save, kernel_name, arg_signatures):
        self._perform_debug_print_or_save_helper(
            args_to_print_or_save,
            kernel_name,
            before_launch=False,
            arg_signatures=arg_signatures,
        )

    def _perform_debug_print_or_save_helper(
        self,
        args_to_print_or_save,
        kernel_name,
        before_launch,
        arg_signatures: Optional[List[type]] = None,
    ):
        if self.debug_printer_level == IntermediateValueDebuggingLevel.OFF:
            return
        if self.debug_printer_level == IntermediateValueDebuggingLevel.SAVE_ONLY:
            # by default save all the tensor values before launch
            self.codegen_intermediate_tensor_value_save(
                self.args_to_print_or_save,
                self.kernel_name,
                before_launch,
                arg_signatures=self.arg_signatures,
            )
        if self.debug_printer_level == IntermediateValueDebuggingLevel.PRINT_ONLY:
            # by default print all the tensor values before launch
            self.codegen_intermediate_tensor_value_print(
                self.args_to_print_or_save,
                self.kernel_name,
                before_launch,
                arg_signatures=self.arg_signatures,
            )

    @functools.lru_cache  # noqa: B019
    def _get_debug_filtered_kernel_names(self) -> List[str]:
        if config.aot_inductor.filtered_kernel_names is None:
            return []
        return [
            x.strip()
            for x in config.aot_inductor.filtered_kernel_names.lower().split(",")
        ]

    def set_printer_args(
        self,
        args_to_print_or_save: List[str],
        kernel_name: str,
        arg_signatures: Optional[List[type]],
        kernel,
        kernel_type=None,
    ):
        # Note: MultiKernel debug printing is not supported for now
        if isinstance(kernel, MultiKernel):
            log.info(
                "MultiKernel type is not supported in AOTI debug printer tool yet."
            )
            self.debug_printer_level = IntermediateValueDebuggingLevel.OFF

        # Note: if the kernel type is an extern kernel, we do a special handling to get the list of args_to_print_or_save
        # TODO: Find a more reliable way to detect kernel args types to print for extern kernel calls
        if kernel_type == "extern":
            args_to_print_or_save_extern = []
            for arg in args_to_print_or_save:
                if arg.startswith(("buf", "arg")):
                    args_to_print_or_save_extern.append(arg)
            self.args_to_print_or_save = args_to_print_or_save_extern
        else:
            self.args_to_print_or_save = args_to_print_or_save
        self.kernel_name = kernel_name
        self.arg_signatures = arg_signatures
        self.kernel = kernel

    def codegen_intermediate_tensor_value_save(
        self,
        args_to_save,
        kernel_name,
        before_launch=True,
        arg_signatures: Optional[List[type]] = None,
    ) -> None:
        for i, arg in enumerate(args_to_save):
            if arg_signatures is not None and not isinstance(
                arg_signatures[i], torch_dtype
            ):
                # infer from the arg data type (has torch.dtype) to see if it is a tensor type
                continue
            launch_prefix = "before_launch" if before_launch else "after_launch"
            if V.graph.cpp_wrapper:
                if config.abi_compatible:
                    V.graph.wrapper_code.writeline(
                        f'aoti_torch_save_tensor_handle({arg}, "{arg}", "{launch_prefix}", "{kernel_name}");'
                    )
                else:
                    # TODO: add non-abi compatible mode debug printing info
                    pass
            else:
                cwd = os.getcwd()
                saved_dir = cwd + "/tmp/jit_inductor/"
                if not os.path.exists(saved_dir):
                    log.info(
                        "Creating directory to save inductor intermediate tensor values."
                    )
                    os.makedirs(saved_dir)
                # Save the model to the directory
                saved_path = saved_dir + f"{launch_prefix}_{kernel_name}_{arg}.pt"
                log.info(
                    "Saved intermediate tensor %s for %s to %s",
                    arg,
                    kernel_name,
                    saved_path,
                )
                line = f"torch.save({arg}, '{saved_path}')"
                V.graph.wrapper_code.writeline(line)

    def codegen_intermediate_tensor_value_print(
        self,
        args_to_print,
        kernel_name,
        before_launch=True,
        arg_signatures: Optional[List[type]] = None,
    ) -> None:
        for i, arg in enumerate(args_to_print):
            if arg_signatures is not None and not isinstance(
                arg_signatures[i], torch_dtype
            ):
                # infer from the arg data type (has torch.dtype) to see if it is a tensor type
                continue
            if self.debug_printer_level == IntermediateValueDebuggingLevel.PRINT_ONLY:
                # when debug printing is enabled i.e. IntermediateValueDebuggingLevel.PRINT_ONLY,
                # check if filtered kernel name list is provided
                if (
                    len(self.filtered_kernel_names_to_print) > 0
                    and kernel_name not in self.filtered_kernel_names_to_print
                ):
                    continue

            launch_prefix = "before_launch" if before_launch else "after_launch"
            if V.graph.cpp_wrapper:
                if config.abi_compatible:
                    V.graph.wrapper_code.writeline(
                        f'aoti_torch_print_tensor_handle({arg}, "{launch_prefix} - {kernel_name} - {arg}");'
                    )
                else:
                    # TODO: add non-abi compatible mode debug printing info
                    pass
            else:
                V.graph.wrapper_code.writeline(
                    f'_print_debugging_tensor_value_info("inductor: {launch_prefix} - {kernel_name} - {arg}", {arg})'
                )
