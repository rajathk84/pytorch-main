# Owner(s): ["module: inductor"]
import os
import shutil
import sys
import unittest

import torch
import torch._dynamo
import torch.utils.cpp_extension
from torch._C import FileCheck


try:
    from extension_backends.cpp.extension_codegen_backend import (  # @manual=fbcode//caffe2/test/inductor/extension_backends:extension_codegen_backend  # noqa: B950
        ExtensionCppWrapperCodegen,
        ExtensionScheduling,
        ExtensionWrapperCodegen,
    )
except ImportError:
    from .extension_backends.cpp.extension_codegen_backend import (
        ExtensionCppWrapperCodegen,
        ExtensionScheduling,
        ExtensionWrapperCodegen,
    )

import torch._inductor.config as config
from torch._inductor import cpu_vec_isa, metrics
from torch._inductor.codegen import cpp_utils
from torch._inductor.codegen.common import (
    get_scheduling_for_device,
    get_wrapper_codegen_for_device,
    register_backend_for_device,
)
from torch.testing._internal.common_utils import IS_FBCODE, IS_MACOS


try:
    try:
        from . import test_torchinductor
    except ImportError:
        import test_torchinductor  # @manual=fbcode//caffe2/test/inductor:test_inductor-library
except unittest.SkipTest:
    if __name__ == "__main__":
        sys.exit(0)
    raise


run_and_get_cpp_code = test_torchinductor.run_and_get_cpp_code
TestCase = test_torchinductor.TestCase


def remove_build_path():
    if sys.platform == "win32":
        # Not wiping extensions build folder because Windows
        return
    default_build_root = torch.utils.cpp_extension.get_default_build_root()
    if os.path.exists(default_build_root):
        shutil.rmtree(default_build_root, ignore_errors=True)


@unittest.skipIf(IS_FBCODE, "cpp_extension doesn't work in fbcode right now")
class ExtensionBackendTests(TestCase):
    module = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Build Extension
        remove_build_path()
        source_file_path = os.path.dirname(os.path.abspath(__file__))
        source_file = os.path.join(
            source_file_path, "extension_backends/cpp/extension_device.cpp"
        )
        cls.module = torch.utils.cpp_extension.load(
            name="extension_device",
            sources=[
                str(source_file),
            ],
            extra_cflags=["-g"],
            verbose=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls._stack.close()
        super().tearDownClass()

        remove_build_path()

    def setUp(self):
        torch._dynamo.reset()
        super().setUp()

        # cpp extensions use relative paths. Those paths are relative to
        # this file, so we'll change the working directory temporarily
        self.old_working_dir = os.getcwd()
        os.chdir(os.path.dirname(os.path.abspath(__file__)))
        assert self.module is not None

    def tearDown(self):
        super().tearDown()
        torch._dynamo.reset()

        # return the working directory (see setUp)
        os.chdir(self.old_working_dir)

    def test_open_device_registration(self):
        torch.utils.rename_privateuse1_backend("extension_device")
        torch._register_device_module("extension_device", self.module)

        register_backend_for_device(
            "extension_device",
            ExtensionScheduling,
            ExtensionWrapperCodegen,
            ExtensionCppWrapperCodegen,
        )
        self.assertTrue(
            get_scheduling_for_device("extension_device") == ExtensionScheduling
        )
        self.assertTrue(
            get_wrapper_codegen_for_device("extension_device")
            == ExtensionWrapperCodegen
        )
        self.assertTrue(
            get_wrapper_codegen_for_device("extension_device", True)
            == ExtensionCppWrapperCodegen
        )

        self.assertFalse(self.module.custom_op_called())
        device = self.module.custom_device()
        x = torch.empty(2, 16).to(device=device).fill_(1)
        self.assertTrue(self.module.custom_op_called())
        y = torch.empty(2, 16).to(device=device).fill_(2)
        z = torch.empty(2, 16).to(device=device).fill_(3)
        ref = torch.empty(2, 16).fill_(5)

        self.assertTrue(x.device == device)
        self.assertTrue(y.device == device)
        self.assertTrue(z.device == device)

        def fn(a, b, c):
            return a * b + c

        cpp_utils.DEVICE_TO_ATEN["extension_device"] = "at::kPrivateUse1"
        for cpp_wrapper_flag in [True, False]:
            with config.patch({"cpp_wrapper": cpp_wrapper_flag}):
                metrics.reset()
                opt_fn = torch.compile()(fn)
                _, code = run_and_get_cpp_code(opt_fn, x, y, z)
                if cpu_vec_isa.valid_vec_isa_list():
                    load_expr = "loadu"
                else:
                    load_expr = " = in_ptr0[static_cast<long>(i0)];"
                FileCheck().check("void").check(load_expr).check(
                    "extension_device"
                ).run(code)
                opt_fn(x, y, z)
                res = opt_fn(x, y, z)
                self.assertEqual(ref, res.to(device="cpu"))


if __name__ == "__main__":
    from torch._inductor.test_case import run_tests
    from torch.testing._internal.inductor_utils import HAS_CPU

    # cpp_extension doesn't work in fbcode right now
    if HAS_CPU and not IS_MACOS and not IS_FBCODE:
        run_tests(needs="filelock")
