# Owner(s): ["module: inductor"]
import logging
import os
import unittest

import torch
from torch._inductor import config
from torch._inductor.test_case import run_tests, TestCase
from torch.testing._internal.common_utils import (
    instantiate_parametrized_tests,
    parametrize,
)
from torch.testing._internal.inductor_utils import HAS_CPU, HAS_CUDA


torch.set_float32_matmul_precision("high")
if HAS_CUDA:
    torch.cuda.memory._set_allocator_settings("expandable_segments:False")

log = logging.getLogger(__name__)


def _get_path_without_sccache() -> str:
    """
    Get the PATH environment variable without sccache.
    """
    path_envs = os.environ.get("PATH", "").split(":")
    path_envs = [env for env in path_envs if "/opt/cache/bin" not in env]
    return ":".join(path_envs)


@instantiate_parametrized_tests
class TestCKBackend(TestCase):
    def setUp(self):
        # The new inductor cache refresh mechanism
        # introduced with https://github.com/pytorch/pytorch/pull/122661
        # interacts badly with persistent subprocesses during
        # autotuning. So we need to disable automatic cache refresh
        # before calling setUp() on the parent class.
        old_disable_fresh_cache_envvar = os.environ.get(
            "INDUCTOR_TEST_DISABLE_FRESH_CACHE", ""
        )

        torch.random.manual_seed(1234)
        try:
            import ck4inductor  # @manual

            self.ck_dir = os.path.dirname(ck4inductor.__file__)
            os.environ["TORCHINDUCTOR_CK_DIR"] = self.ck_dir
        except ImportError as e:
            raise unittest.SkipTest("Composable Kernel library not installed") from e

        try:
            os.environ["INDUCTOR_TEST_DISABLE_FRESH_CACHE"] = "1"
            super().setUp()
        finally:
            os.environ[
                "INDUCTOR_TEST_DISABLE_FRESH_CACHE"
            ] = old_disable_fresh_cache_envvar

    @unittest.skipIf(not torch.version.hip, "ROCM only")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CK path setup")
    @unittest.mock.patch.dict(os.environ, {"PATH": _get_path_without_sccache()})
    @parametrize("max_autotune_gemm_backends", ("CK", "ATen,Triton,CK"))
    @parametrize("autotune_in_subproc", (True, False))
    def test_max_autotune_precompile_matmul(
        self, max_autotune_gemm_backends, autotune_in_subproc
    ):
        """
        Make sure autotuning mm doesn't crash.
        """

        torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False

        def mm(a, b):
            return a @ b

        tensor_options = {"device": "cuda", "dtype": torch.bfloat16}

        a = torch.randn(2240, 256, **tensor_options)
        b = torch.randn(256, 2048, **tensor_options)

        assert "rocm" in dir(config)

        with config.patch(
            {
                "max_autotune": True,
                "autotune_in_subproc": autotune_in_subproc,
                "max_autotune_gemm_backends": max_autotune_gemm_backends,
                "compile_threads": 2,
                "rocm.n_max_profiling_configs": 2,
                "rocm.ck_dir": self.ck_dir,
            }
        ):
            Y_compiled = torch.compile(mm, dynamic=False)(a, b)
            Y = mm(a, b)
            torch.testing.assert_close(Y_compiled, Y)

    @unittest.skipIf(not torch.version.hip, "ROCM only")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CK path setup")
    @unittest.mock.patch.dict(os.environ, {"PATH": _get_path_without_sccache()})
    @parametrize("max_autotune_gemm_backends", ("CK",))
    @parametrize("autotune_in_subproc", (True,))
    def test_max_autotune_precompile_matmul_dynamic(
        self, max_autotune_gemm_backends, autotune_in_subproc
    ):
        """
        Test matmul with dynamic shapes
        """

        torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False

        tensor_options = {"device": "cuda", "dtype": torch.bfloat16}

        a = torch.randn(2240, 256, **tensor_options)
        b = torch.randn(256, 2048, **tensor_options)

        torch._dynamo.mark_dynamic(a, 0)

        assert "rocm" in dir(config)

        with config.patch(
            {
                "max_autotune": True,
                "autotune_in_subproc": autotune_in_subproc,
                "max_autotune_gemm_backends": max_autotune_gemm_backends,
                "compile_threads": 2,
                "rocm.n_max_profiling_configs": 2,
                "rocm.ck_dir": self.ck_dir,
            }
        ):

            @torch.compile(dynamic=True)
            def compiled_mm(a, b):
                return a @ b

            Y_compiled = compiled_mm(a, b)
            Y = a @ b
            torch.testing.assert_close(Y_compiled, Y)

            a1 = torch.randn(1024, 256, **tensor_options)
            Y1_compiled = compiled_mm(a1, b)
            Y1 = a1 @ b
            torch.testing.assert_close(Y1_compiled, Y1)

    @unittest.skipIf(not torch.version.hip, "ROCM only")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CK path setup")
    @unittest.mock.patch.dict(os.environ, {"PATH": _get_path_without_sccache()})
    @parametrize("max_autotune_gemm_backends", ("CK", "ATen,Triton,CK"))
    def test_max_autotune_precompile_preselected(self, max_autotune_gemm_backends):
        """
        End to end test for picking preselected ck instances
        """

        torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False

        def mm(a, b):
            return a @ b

        tensor_options = {"device": "cuda", "dtype": torch.float16}

        a = torch.randn(2240, 256, **tensor_options)
        b = torch.randn(2048, 256, **tensor_options).transpose(0, 1)

        assert "rocm" in dir(config)

        with config.patch(
            {
                "max_autotune": True,
                "autotune_in_subproc": True,
                "max_autotune_gemm_backends": max_autotune_gemm_backends,
                "compile_threads": 12,
                "rocm.ck_dir": self.ck_dir,
                "rocm.use_preselected_instances": True,
            }
        ):
            Y_compiled = torch.compile(mm, dynamic=False)(a, b)
            Y = mm(a, b)
            torch.testing.assert_close(Y_compiled, Y)

    @unittest.skipIf(not torch.version.hip, "ROCM only")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CK path setup")
    @unittest.mock.patch.dict(os.environ, {"PATH": _get_path_without_sccache()})
    @parametrize("max_autotune_gemm_backends", ("CK", "ATen,Triton,CK"))
    def test_max_autotune_precompile_non_contiguous(self, max_autotune_gemm_backends):
        """
        Make sure the ck template can work with non-contiguous inputs
        """

        torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False

        tensor_options = {"device": "cuda", "dtype": torch.float16}

        a = torch.empty_strided((50257, 32768), (1, 50304), **tensor_options)
        b = torch.empty_strided((32768, 768), (768, 1), **tensor_options)

        assert "rocm" in dir(config)

        with config.patch(
            {
                "max_autotune": True,
                "autotune_in_subproc": True,
                "max_autotune_gemm_backends": max_autotune_gemm_backends,
                "compile_threads": 2,
                "rocm.ck_dir": self.ck_dir,
                "rocm.n_max_profiling_configs": 2,
            }
        ):

            @torch.compile(dynamic=False)
            def mm(a, b):
                return a @ b

            Y_compiled = mm(a, b)
            Y_eager = a @ b
            torch.testing.assert_close(Y_compiled, Y_eager)

    @unittest.skipIf(not torch.version.hip, "ROCM only")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CK path setup")
    @unittest.mock.patch.dict(os.environ, {"PATH": _get_path_without_sccache()})
    @parametrize("max_autotune_gemm_backends", ("CK", "ATen,Triton,CK"))
    @parametrize("x_shape", ([4096, 2048], [2048], [4096, 1]))
    def test_max_autotune_addmm(self, max_autotune_gemm_backends, x_shape):
        torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False

        m, k, n = 4096, 224, 2048
        alpha, beta = 1.0, 1.0

        tensor_options = {"device": "cuda", "dtype": torch.float16}
        x = torch.ones(x_shape, **tensor_options)
        a = torch.randn(m, k, **tensor_options)
        b = torch.randn(k, n, **tensor_options)

        assert "rocm" in dir(config)

        with config.patch(
            {
                "max_autotune": True,
                "autotune_in_subproc": True,
                "max_autotune_gemm_backends": max_autotune_gemm_backends,
                "compile_threads": 2,
                "rocm.ck_dir": self.ck_dir,
                "rocm.n_max_profiling_configs": 2,
            }
        ):

            @torch.compile(dynamic=False)
            def addmm(x, a, b, alpha, beta):
                return torch.addmm(x, a, b, alpha=alpha, beta=beta)

            Y_compiled = addmm(x, a, b, alpha, beta)
            Y_eager = torch.addmm(x, a, b, alpha=alpha, beta=beta)

            torch.testing.assert_close(Y_compiled, Y_eager)


if __name__ == "__main__":
    from torch._inductor.utils import is_big_gpu

    # Set env to make it work in CI.
    if HAS_CUDA and HAS_CPU and is_big_gpu(0):
        run_tests()
