# Owner(s): ["oncall: cpu inductor"]
import contextlib
import copy
import itertools
import unittest

import torch
import torch.ao.quantization.quantizer.x86_inductor_quantizer as xiq
from torch._dynamo import config as dynamo_config
from torch._dynamo.utils import counters
from torch._inductor import config, metrics
from torch._inductor.test_case import run_tests, TestCase
from torch._inductor.utils import run_and_get_code
from torch.ao.quantization.quantizer.x86_inductor_quantizer import X86InductorQuantizer
from torch.nn import functional as F
from torch.testing._internal.common_quantization import (
    _generate_qdq_quantized_model,
    skipIfNoDynamoSupport,
    skipIfNoONEDNN,
    skipIfNoONEDNNBF16,
)
from torch.testing._internal.common_utils import IS_LINUX, skipIfRocm, TEST_MKL
from torch.testing._internal.inductor_utils import _check_has_dynamic_shape, HAS_CPU


# The dict value is match_nodes(computation_op+unary_op)

unary_list = {
    torch.nn.ReLU(): 2,
    torch.nn.Sigmoid(): 2,
    torch.nn.Tanh(): 2,
    torch.nn.Hardswish(): 6,
    torch.nn.LeakyReLU(0.1, inplace=False): 4,
    # Use floats for min/max, otherwise they can get converted to symints
    torch.nn.Hardtanh(min_val=-0.5, max_val=4.0, inplace=False): 3,
    torch.nn.Hardtanh(min_val=-0.5, max_val=float("inf"), inplace=False): 3,
    torch.nn.GELU(approximate="none"): 6,
    torch.nn.GELU(approximate="tanh"): 10,
    torch.nn.ReLU6(): 3,
    torch.nn.SiLU(): 3,
    torch.nn.Hardsigmoid(): 5,
}

non_decomposed_unary_list = [
    torch.nn.ReLU,
    torch.nn.Sigmoid,
    torch.nn.Tanh,
]

# The dict value is (match_count, match_nodes, inplace)
binary_list = {
    lambda x, y: torch.add(x, y): (1, 2, False),  # call_function
    lambda x, y: torch.add(y, x): (1, 2, False),  # call_function
    lambda x, y: x.add(y): (1, 2, False),  # call_method
    lambda x, y: x.add_(y): (1, 2, True),  # call_method
    lambda x, y: torch.sub(x, y): (1, 2, False),  # call_function
    lambda x, y: x.sub(y): (1, 2, False),  # call_method
    lambda x, y: x.sub_(y): (1, 2, True),  # call_method
}

quantization_add_fn_list = [
    lambda x, y: torch.add(x, y),
    lambda x, y: x.add(y),
]

quantization_inplace_add_fn_list = [
    lambda x, y: x.add_(y),
]


def get_default_quantizer(is_qat, is_dynamic):
    quantizer = X86InductorQuantizer()
    quantizer.set_global(
        xiq.get_default_x86_inductor_quantization_config(
            is_qat=is_qat, is_dynamic=is_dynamic
        )
    )
    return quantizer


def cal_conv_generated_kernel_number(mod, input, dtype):
    # this function is to decide how many kernels are generated
    # while testing conv2d/3d/deconv2d
    # the assumption is:
    #   (1) There will be a to_dtype kernel for input for lp
    #   (2) inductor always use channe_last format, there will
    #       be a to_channel_last format for input
    #   (3) to_dtype and to_channel_last for input can be fused
    #   (4) inductor always get channel last format from mkldnn_conv_pointwise(binary),
    #       and force the output to have same stride with eager.
    #       So there will be a to_contiguous for output if eager output is contiguouse
    mod = copy.deepcopy(mod)
    input = input.clone()
    if dtype == torch.float32:
        maybe_autocast = contextlib.nullcontext()
    else:
        maybe_autocast = torch.cpu.amp.autocast(dtype=dtype)
    with torch.no_grad(), maybe_autocast:
        output = mod(input)
    input_kernel, output_kernel = 0, 0
    if (
        input.is_contiguous(memory_format=torch.contiguous_format)
        or dtype != torch.float32
    ):
        input_kernel = 1
    if output.is_contiguous(memory_format=torch.contiguous_format):
        output_kernel = 1
    return input_kernel + output_kernel


@config.patch({"freezing": True})
class TestPatternMatcherBase(TestCase):
    def _check_unary_is_decomposed(self, unary_fn):
        return not any(
            isinstance(unary_fn, fn)
            for fn in [torch.nn.ReLU, torch.nn.Sigmoid, torch.nn.Tanh]
        )

    def _clone_inputs(self, inputs):
        def clone(x):
            if not isinstance(x, torch.Tensor):
                return x
            return x.clone()

        return tuple(clone(x) for x in inputs)

    def _test_common(
        self,
        mod,
        inputs,
        matcher_count=None,
        matcher_nodes=None,
        atol=1e-5,
        rtol=1.3e-6,
        check_autocast=torch.float32,
        check_quantization=False,
        is_qat=False,
        matcher_check_fn=None,
        dtype=None,
        is_dynamic=False,
        quantizer=None,
    ):
        counters.clear()
        torch._dynamo.reset()
        assert matcher_check_fn is not None or (
            matcher_count is not None and matcher_nodes is not None
        )
        if (
            check_autocast == torch.bfloat16
            and torch.ops.mkldnn._is_mkldnn_bf16_supported()
        ):
            maybe_autocast = torch.cpu.amp.autocast(dtype=torch.bfloat16)
            atol, rtol = 1e-2, 1e-2
        elif (
            check_autocast == torch.float16
            and torch.ops.mkldnn._is_mkldnn_fp16_supported()
        ):
            maybe_autocast = torch.cpu.amp.autocast(dtype=torch.float16)
            atol, rtol = 1e-2, 1e-2
        else:
            assert check_autocast == torch.float32
            maybe_autocast = contextlib.nullcontext()

        if check_quantization:
            convert_model = _generate_qdq_quantized_model(
                mod, inputs, is_qat, is_dynamic, quantizer
            )
            with torch.no_grad(), maybe_autocast:
                _ = torch.compile(convert_model)(*inputs)
                if matcher_count is not None:
                    self.assertEqual(
                        counters["inductor"]["pattern_matcher_count"], matcher_count
                    )
                if matcher_nodes is not None:
                    self.assertEqual(
                        counters["inductor"]["pattern_matcher_nodes"],
                        matcher_nodes,
                    )
                if matcher_check_fn is not None:
                    matcher_check_fn()
        else:
            with torch.no_grad(), maybe_autocast:
                clone_inputs = self._clone_inputs(inputs)
                expected = mod(*inputs)
                actual = torch.compile(mod)(*clone_inputs)
                torch.testing.assert_close(actual, expected, atol=atol, rtol=rtol)
                if matcher_count is not None:
                    self.assertEqual(
                        counters["inductor"]["pattern_matcher_count"], matcher_count
                    )
                if matcher_nodes is not None:
                    self.assertEqual(
                        counters["inductor"]["pattern_matcher_nodes"],
                        matcher_nodes,
                    )
                if matcher_check_fn is not None:
                    matcher_check_fn()

    def _test_code_common(
        self,
        mod,
        inputs,
        include_ops,
        exclude_ops,
        atol=1e-5,
        rtol=1.3e-6,
        check_quantization=False,
        check_dynamic=None,
        num_include_ops=None,
    ):
        with torch.no_grad():
            clone_inputs = self._clone_inputs(inputs)
            if check_quantization:
                mod = _generate_qdq_quantized_model(mod, inputs)
            expected = mod(*inputs)
            actual, (source_code,) = run_and_get_code(
                torch.compile(mod, fullgraph=True, dynamic=check_dynamic),
                *clone_inputs,
            )
            for op in include_ops:
                self.assertIn(op, source_code)
            if num_include_ops is not None:
                assert len(include_ops) == len(num_include_ops)
                for i in range(len(include_ops)):
                    self.assertEqual(
                        source_code.count(include_ops[i]), num_include_ops[i]
                    )
            for op in exclude_ops:
                self.assertNotIn(op, source_code)
            if check_dynamic is not None:
                _check_has_dynamic_shape(self, source_code)
            if not check_quantization:
                # Skip due to reduce range setting for Quantization on preCI system.
                torch.testing.assert_close(actual, expected, atol=atol, rtol=rtol)


class TestPatternMatcher(TestPatternMatcherBase):
    def _test_conv_unary_cpu_base(self, dim=4):
        assert dim == 4 or dim == 5

        class M(torch.nn.Module):
            def __init__(
                self,
                unary_fn,
                **kwargs,
            ):
                super().__init__()
                if dim == 4:
                    self.conv = torch.nn.Conv2d(3, 16, kernel_size=3, stride=1)
                else:
                    self.conv = torch.nn.Conv3d(3, 16, kernel_size=3, stride=1)
                self.unary_fn = unary_fn

            def forward(self, x):
                x = self.conv(x)
                return self.unary_fn(x)

        dtypes = [
            torch.float,
        ]
        if torch.ops.mkldnn._is_mkldnn_bf16_supported():
            dtypes.append(torch.bfloat16)
        if torch.ops.mkldnn._is_mkldnn_fp16_supported():
            dtypes.append(torch.float16)
        cl_format = torch.channels_last if dim == 4 else torch.channels_last_3d
        options = itertools.product(
            unary_list.keys(),
            [torch.contiguous_format, cl_format],
            dtypes,
        )

        for (
            unary_fn,
            memory_format,
            dtype,
        ) in options:
            metrics.reset()
            if dim == 4:
                x_shape = (1, 3, 56, 56)
            else:
                x_shape = (1, 3, 20, 56, 56)
            mod = M(unary_fn).to(memory_format=memory_format).eval()

            v = (
                torch.randn(x_shape, dtype=torch.float32)
                .add(1)
                .to(memory_format=memory_format)
            )
            # Add 1 for weight packing pass.
            match_nodes = unary_list[unary_fn] + 1
            if dtype in (
                torch.float16,
                torch.bfloat16,
            ) and self._check_unary_is_decomposed(unary_fn):
                # Has extra dtype conversion nodes for autocast.
                match_nodes += 2
            self._test_common(mod, (v,), 2, match_nodes, check_autocast=dtype)
            generated_kernel_count = cal_conv_generated_kernel_number(mod, v, dtype)
            self.assertEqual(metrics.generated_kernel_count, generated_kernel_count)

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    @skipIfRocm
    def test_conv2d_unary_cpu(self):
        self._test_conv_unary_cpu_base(dim=4)

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    @skipIfRocm
    def test_conv3d_unary_cpu(self):
        self._test_conv_unary_cpu_base(dim=5)

    def test_linear_unary(self):
        class M(torch.nn.Module):
            def __init__(
                self,
                unary_fn,
                in_features,
                out_features,
                bias,
                **kwargs,
            ):
                super().__init__()
                self.linear = torch.nn.Linear(
                    in_features,
                    out_features,
                    bias,
                    **kwargs,
                )
                self.unary_fn = unary_fn

            def forward(self, x):
                x = self.linear(x)
                return self.unary_fn(x)

        dtypes = []
        if torch.ops.mkldnn._is_mkldnn_bf16_supported():
            dtypes.append(torch.bfloat16)
        if torch.ops.mkldnn._is_mkldnn_fp16_supported():
            dtypes.append(torch.float16)
        options = itertools.product(unary_list, [True, False], dtypes)
        for unary_fn, bias, dtype in options:
            metrics.reset()
            mod = M(unary_fn, 10, 30, bias=bias).eval()
            # only fuse for linear when the dtype is bf16
            mod = mod
            v = torch.randn(2, 10)
            # packing pass + unary fusion.
            matcher_count = 2
            # Add 1 for weight packing pass.
            matcher_nodes = unary_list[unary_fn] + 1
            if self._check_unary_is_decomposed(unary_fn):
                # Has extra dtype conversion nodes for autocast.
                matcher_nodes += 2
            self._test_common(
                mod, (v,), matcher_count, matcher_nodes, check_autocast=dtype
            )
            # only generated 1 kernel for "to"
            self.assertEqual(metrics.generated_kernel_count, 1)

    @unittest.skipIf(not TEST_MKL, "Test requires MKL")
    def test_linear_fp32(self):
        class M(torch.nn.Module):
            def __init__(self, bias):
                super().__init__()
                self.linear = torch.nn.Linear(10, 30, bias)

            def forward(self, x):
                return self.linear(x)

        for bias in [True, False]:
            mod = M(bias=bias).eval()
            v = torch.randn(2, 10)
            # packing pass.
            matcher_count = 1
            matcher_nodes = 1
            self._test_common(mod, (v,), matcher_count, matcher_nodes)

    def test_linear_add_bias(self):
        class M(torch.nn.Module):
            def __init__(self, dtype, unary_fn, cast_bias):
                super().__init__()
                self.linear1 = torch.nn.Linear(10, 64, bias=False)
                self.bias1 = torch.randn(64)
                self.linear2 = torch.nn.Linear(10, 64, bias=False)
                self.bias2 = torch.randn(64)
                if cast_bias:
                    self.bias1 = self.bias1.to(dtype=dtype)
                    self.bias2 = self.bias2.to(dtype=dtype)
                self.unary_fn = unary_fn

            def forward(self, x):
                a = self.linear1(x) + self.bias1
                b = self.linear2(x) + self.bias2
                return self.unary_fn(a), self.unary_fn(b)

        dtypes = []
        if torch.ops.mkldnn._is_mkldnn_bf16_supported():
            dtypes.append(torch.bfloat16)
        if torch.ops.mkldnn._is_mkldnn_fp16_supported():
            dtypes.append(torch.float16)
        options = itertools.product(unary_list, dtypes)
        for unary_fn, dtype in options:
            metrics.reset()
            fold_mod = M(dtype, unary_fn, cast_bias=True).eval()
            v = torch.randn(2, 10)
            matcher_count = 3
            # Add 1 for weight packing pass, add 2 for bias folding pass per linear.
            matcher_nodes = unary_list[unary_fn] + 3
            if self._check_unary_is_decomposed(unary_fn):
                # Has extra dtype conversion nodes for autocast.
                matcher_nodes += 2
            # we have 2 linears, so we double the matcher_count/nodes
            self._test_common(
                fold_mod,
                (v,),
                matcher_count * 2,
                matcher_nodes * 2,
                check_autocast=dtype,
            )
            self.assertEqual(metrics.generated_kernel_count, 1)
            # we won't fold the bias if bias is not same dtype with weight
            # https://github.com/pytorch/pytorch/pull/129138
            metrics.reset()
            mod = M(dtype, unary_fn, cast_bias=False).eval()
            self._test_common(mod, (v,), 2, 2, check_autocast=dtype)
            # 1 kernel for "to_lowp", 2 kernels for unary ops
            self.assertEqual(metrics.generated_kernel_count, 3)

    def _test_conv_transpose_unary_base(self, dim=4):
        assert dim == 4 or dim == 5

        class M(torch.nn.Module):
            def __init__(
                self,
                unary_fn,
                **kwargs,
            ):
                super().__init__()
                if dim == 4:
                    self.conv_transpose = torch.nn.ConvTranspose2d(
                        3, 16, 3, stride=2, padding=1
                    )
                else:
                    self.conv_transpose = torch.nn.ConvTranspose3d(
                        3, 16, 3, stride=2, padding=1
                    )
                self.unary_fn = unary_fn

            def forward(self, x):
                x = self.conv_transpose(x)
                return self.unary_fn(x)

        dtypes = [
            torch.float,
        ]
        if torch.ops.mkldnn._is_mkldnn_bf16_supported():
            dtypes.append(torch.bfloat16)
        if torch.ops.mkldnn._is_mkldnn_fp16_supported():
            dtypes.append(torch.float16)

        cl_format = torch.channels_last if dim == 4 else torch.channels_last_3d
        options = itertools.product(
            unary_list,
            [torch.contiguous_format, cl_format],
            dtypes,
        )

        for unary_fn, memory_format, dtype in options:
            metrics.reset()
            if dim == 4:
                x_shape = (1, 3, 28, 28)
            else:
                x_shape = (1, 3, 17, 28, 28)
            mod = M(unary_fn).eval()

            v = torch.randn(x_shape, dtype=torch.float32).to(
                memory_format=memory_format
            )
            # Add 1 for weight packing pass.
            match_nodes = unary_list[unary_fn] + 1
            if dtype in (
                torch.float16,
                torch.bfloat16,
            ) and self._check_unary_is_decomposed(unary_fn):
                # Has extra dtype conversion nodes for autocast.
                match_nodes += 2
            self._test_common(mod, (v,), 2, match_nodes, check_autocast=dtype)
            generated_kernel_count = cal_conv_generated_kernel_number(mod, v, dtype)
            self.assertEqual(metrics.generated_kernel_count, generated_kernel_count)

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    @skipIfRocm
    def test_conv_transpose2d_unary_cpu(self):
        self._test_conv_transpose_unary_base(dim=4)

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    @skipIfRocm
    def test_conv_transpose3d_unary_cpu(self):
        self._test_conv_transpose_unary_base(dim=5)

    def _test_conv_binary_base(self, dim=4):
        assert dim == 4 or dim == 5

        class M(torch.nn.Module):
            def __init__(
                self,
                binary_fn,
                has_relu,
                **kwargs,
            ):
                super().__init__()
                if dim == 4:
                    self.conv1 = torch.nn.Conv2d(3, 16, kernel_size=3, stride=1)
                    self.conv2 = torch.nn.Conv2d(3, 16, kernel_size=3, stride=1)
                else:
                    self.conv1 = torch.nn.Conv3d(3, 16, kernel_size=3, stride=1)
                    self.conv2 = torch.nn.Conv3d(3, 16, kernel_size=3, stride=1)
                self.binary_fn = binary_fn
                self.has_relu = has_relu

            def forward(self, x):
                x1 = self.conv1(x)
                x2 = self.conv2(x)
                if has_relu:
                    return self.binary_fn(x1, x2).relu()
                else:
                    return self.binary_fn(x1, x2)

        dtypes = [
            torch.float,
        ]
        if torch.ops.mkldnn._is_mkldnn_bf16_supported():
            dtypes.append(torch.bfloat16)
        if torch.ops.mkldnn._is_mkldnn_fp16_supported():
            dtypes.append(torch.float16)
        cl_format = torch.channels_last if dim == 4 else torch.channels_last_3d
        test_memory_format = [torch.contiguous_format, cl_format]
        options = itertools.product(
            binary_list,
            [True, False],
            test_memory_format,
            dtypes,
        )

        for (
            binary_fn,
            has_relu,
            memory_format,
            dtype,
        ) in options:
            metrics.reset()
            if dim == 4:
                x_shape = (1, 3, 56, 56)
            else:
                x_shape = (1, 3, 20, 56, 56)
            mod = M(binary_fn, has_relu).eval()
            v = (
                torch.randn(x_shape, dtype=torch.float32, requires_grad=True)
                .add(1)
                .to(memory_format=memory_format)
            )
            match_count = binary_list[binary_fn][0] + 2
            match_nodes = binary_list[binary_fn][1]
            if has_relu:
                match_nodes += 1
            self._test_common(
                mod, (v,), match_count, match_nodes + 2, check_autocast=dtype
            )
            generated_kernel_count = cal_conv_generated_kernel_number(mod, v, dtype)
            self.assertEqual(metrics.generated_kernel_count, generated_kernel_count)

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    @skipIfRocm
    def test_conv2d_binary(self):
        self._test_conv_binary_base(dim=4)

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    @skipIfRocm
    def test_conv3d_binary(self):
        self._test_conv_binary_base(dim=5)

    def test_linear_binary(self):
        class M(torch.nn.Module):
            def __init__(self, binary_fn, in_channels, out_channels, bias, **kwargs):
                super().__init__()
                self.linear = torch.nn.Linear(
                    in_channels, out_channels, bias=bias, **kwargs
                )
                self.binary_fn = binary_fn

            def forward(self, x, y):
                x = self.linear(x)
                x = self.binary_fn(x, y.clone())
                return x

        dtypes = []
        if torch.ops.mkldnn._is_mkldnn_bf16_supported():
            dtypes.append(torch.bfloat16)
        if torch.ops.mkldnn._is_mkldnn_fp16_supported():
            dtypes.append(torch.float16)
        options = itertools.product(
            binary_list, [[2, 3, 10], [2, 10]], [True, False], dtypes
        )
        out_feature = 30

        for binary_fn, input_shape, bias, dtype in options:
            metrics.reset()
            # addmm(mm) + (linear+add)
            match_count = 2
            match_nodes = 3
            if len(input_shape) == 3:
                is_inplace = binary_list[binary_fn][2]
                # view + linear + view(joint_graph+freeze pass)
                match_count = match_count + 5 if is_inplace else match_count + 3
                match_nodes = match_nodes + 7 if is_inplace else match_nodes + 5
            mod = M(binary_fn, input_shape[-1], out_feature, bias).eval()
            v = torch.randn(input_shape)
            other = torch.randn(input_shape[:-1] + [out_feature]).to(dtype)
            self._test_common(
                mod,
                (
                    v,
                    other,
                ),
                match_count,
                match_nodes,
                check_autocast=dtype,
            )
            self.assertEqual(metrics.generated_kernel_count, 1)

    def test_multi_linear_share_same_input(self):
        # llama pattern.
        class M(torch.nn.Module):
            def __init__(
                self,
            ):
                super().__init__()
                self.w1 = torch.nn.Linear(16, 16, bias=False)
                self.w2 = torch.nn.Linear(16, 16, bias=False)

            def forward(self, x):
                return F.silu(self.w1(x)) * F.relu(self.w2(x))

        dtypes = []
        if torch.ops.mkldnn._is_mkldnn_bf16_supported():
            dtypes.append(torch.bfloat16)
        if torch.ops.mkldnn._is_mkldnn_fp16_supported():
            dtypes.append(torch.float16)
        for dtype in dtypes:
            mod = M().to(dtype).eval()
            v = torch.randn(2, 4, 16).to(dtype)
            # 1. view(match_count=4, match_nodes=4).
            # 2. mm to packed linear(match_count=2, match_nodes=2).
            # 3. view+linear+view to linear(match_count=2, match_nodes=6).
            # 4. linear+silu fusion(match_count=1, match_nodes=5)
            # 5. linear+relu fusion(match_count=1, match_nodes=2)

            match_count = 10
            match_nodes = 19
            self._test_common(mod, (v,), match_count, match_nodes, rtol=1e-2, atol=1e-2)

    def _qconv2d_cpu_test_helper(self, int8_mixed_bf16=False):
        class M(torch.nn.Module):
            def __init__(
                self,
                **kwargs,
            ):
                super().__init__()
                self.conv = torch.nn.Conv2d(3, 128, kernel_size=3, stride=1)
                self.conv2 = torch.nn.Conv2d(128, 128, kernel_size=3, stride=1)

            def forward(self, x):
                return self.conv2(self.conv(x))

        mod = M().eval()
        v = torch.randn((1, 3, 8, 8), dtype=torch.float32, requires_grad=False).add(1)

        def matcher_check_fn():
            # 1. Dequant-Conv2D pattern matched in QConv2D weight prepack * 1
            #    int8_mixed_fp32: [dequant_node, dequantize_per_channel, clone, convolution]
            #    int8_mixed_bf16: [dequant_node, optional(convert_element_type_4),
            #     dequantize_per_channel, optional(convert_element_type_3), clone, convolution]
            self.assertEqual(
                counters["inductor"]["qconv2d_weight_prepack_matcher_count"], 2
            )
            self.assertEqual(
                counters["inductor"]["qconv2d_weight_prepack_matcher_nodes"],
                12 if int8_mixed_bf16 else 8,
            )

        self._test_common(
            mod,
            (v,),
            check_quantization=True,
            check_autocast=torch.bfloat16 if int8_mixed_bf16 else torch.float,
            matcher_check_fn=matcher_check_fn,
        )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    @skipIfRocm
    def test_qconv2d_cpu(self):
        r"""
        This testcase will quantize a single Conv2d module.
        """
        self._qconv2d_cpu_test_helper()

    @skipIfNoDynamoSupport
    @skipIfNoONEDNNBF16
    @skipIfNoONEDNN
    def test_qconv2d_int8_mixed_bf16(self):
        r"""
        This testcase will quantize a single Conv2d module with int8_mixed_bf16 quantization.
        """
        self._qconv2d_cpu_test_helper(int8_mixed_bf16=True)

    def _qconv2d_unary_cpu_test_helper(
        self,
        int8_mixed_bf16=False,
        unary_op=torch.nn.ReLU(),
        qconv2d_unary_matcher_nodes=None,
    ):
        class M(torch.nn.Module):
            def __init__(
                self,
                **kwargs,
            ):
                super().__init__()
                self.conv = torch.nn.Conv2d(3, 128, kernel_size=3, stride=1)
                self.unary_fn = copy.deepcopy(unary_op)
                self.conv2 = torch.nn.Conv2d(
                    128, 128, kernel_size=3, stride=1, bias=False
                )
                self.unary_fn2 = copy.deepcopy(unary_op)

            def forward(self, x):
                tmp = self.unary_fn(self.conv(x))
                return self.unary_fn2(self.conv2(tmp))

        mod = M().eval()
        v = torch.randn((1, 3, 8, 8), dtype=torch.float32, requires_grad=False).add(1)

        def matcher_check_fn():
            # 1. Dequant-Conv2D pattern matched in quantization weight prepack * 2
            self.assertEqual(
                counters["inductor"]["qconv2d_weight_prepack_matcher_count"], 2
            )
            # 2. QConv2D Unary fusion in post-grad fusion pass * 2
            self.assertEqual(counters["inductor"]["qconv2d_unary_matcher_count"], 2)
            if qconv2d_unary_matcher_nodes:
                self.assertEqual(
                    counters["inductor"]["qconv2d_unary_matcher_nodes"],
                    qconv2d_unary_matcher_nodes,
                )

        self._test_common(
            mod,
            (v,),
            check_quantization=True,
            check_autocast=torch.bfloat16 if int8_mixed_bf16 else torch.float,
            matcher_check_fn=matcher_check_fn,
        )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qconv2d_relu_cpu(self):
        r"""
        This testcase will quantize Conv2d->ReLU pattern.
        """
        self._qconv2d_unary_cpu_test_helper()

    @skipIfNoDynamoSupport
    @skipIfNoONEDNNBF16
    @skipIfNoONEDNN
    def test_qconv2d_relu_int8_mixed_bf16(self):
        r"""
        This testcase will quantize Conv2d->ReLU pattern with int8_mixed_bf16 quantization.
        """
        self._qconv2d_unary_cpu_test_helper(int8_mixed_bf16=True)

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qconv2d_relu6_cpu(self):
        r"""
        This testcase will quantize Conv2d->ReLU6 pattern.
        """
        self._qconv2d_unary_cpu_test_helper(unary_op=torch.nn.ReLU6())

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qconv2d_hardtanh_cpu(self):
        r"""
        This testcase will quantize Conv2d->Hardtanh pattern.
        """
        self._qconv2d_unary_cpu_test_helper(unary_op=torch.nn.Hardtanh())

    @skipIfNoDynamoSupport
    @skipIfNoONEDNNBF16
    @skipIfNoONEDNN
    def test_qconv2d_hardtanh_int8_mixed_bf16_cpu(self):
        r"""
        This testcase will quantize Conv2d->Hardtanh pattern.
        Match.nodes:
            [qconv2d_pointwise_default, convert_element_type, clamp_min, clamp_max, convert_element_type, quantize_per_tensor]
            [qconv2d_pointwise_default, convert_element_type, clamp_min, clamp_max, convert_element_type]
        """
        self._qconv2d_unary_cpu_test_helper(
            unary_op=torch.nn.Hardtanh(),
            int8_mixed_bf16=True,
            qconv2d_unary_matcher_nodes=11,
        )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qconv2d_hardswish_cpu(self):
        r"""
        This testcase will quantize Conv2d->Hardswish pattern.
        """
        self._qconv2d_unary_cpu_test_helper(unary_op=torch.nn.Hardswish())

    @skipIfNoDynamoSupport
    @skipIfNoONEDNNBF16
    @skipIfNoONEDNN
    def test_qconv2d_hardswish_int8_mixed_bf16_cpu(self):
        r"""
        This testcase will quantize Conv2d->Hardswish pattern.
        Match.nodes:
            [qconv2d_pointwise_default, convert_element_type, add, clamp_min,
             clamp_max, mul, div, convert_element_type, quantize_per_tensor]
            [qconv2d_pointwise_default, convert_element_type, add, clamp_min, clamp_max, mul, div, convert_element_type]
        """
        self._qconv2d_unary_cpu_test_helper(
            unary_op=torch.nn.Hardswish(),
            int8_mixed_bf16=True,
            qconv2d_unary_matcher_nodes=17,
        )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qconv2d_silu_cpu(self):
        r"""
        This testcase will quantize Conv2d->SiLU pattern.
        """
        self._qconv2d_unary_cpu_test_helper(unary_op=torch.nn.SiLU())

    @skipIfNoDynamoSupport
    @skipIfNoONEDNNBF16
    @skipIfNoONEDNN
    def test_qconv2d_silu_int8_mixed_bf16_cpu(self):
        r"""
        This testcase will quantize Conv2d->SiLU pattern.
        Match.nodes:
            [qconv2d_pointwise_default, convert_element_type, sigmoid, mul,
             convert_element_type, quantize_per_tensor]
            [qconv2d_pointwise_default, convert_element_type, sigmoid, mul, convert_element_type]
        """
        self._qconv2d_unary_cpu_test_helper(
            unary_op=torch.nn.SiLU(),
            int8_mixed_bf16=True,
            qconv2d_unary_matcher_nodes=11,
        )

    def _qconv2d_add_cpu_test_helper(self, use_relu=False, int8_mixed_bf16=False):
        r"""
        This testcase will quantize a Conv2d->Add pattern as:
                 X
               /   \
        Conv1(X)   Conv2(X)
               \   /
                Add
                 |
           Optional(relu)
                 |
                 Y
        """

        class M(torch.nn.Module):
            def __init__(
                self,
                add_fn,
                use_relu,
                **kwargs,
            ):
                super().__init__()
                self.conv1 = torch.nn.Conv2d(3, 6, kernel_size=3, stride=1)
                self.conv2 = torch.nn.Conv2d(3, 6, kernel_size=3, stride=1)
                self.add_fn = add_fn
                self.relu = torch.nn.ReLU()
                self.conv3 = torch.nn.Conv2d(6, 6, kernel_size=3, stride=1, bias=False)
                self.conv4 = torch.nn.Conv2d(6, 6, kernel_size=3, stride=1, bias=False)
                self.add_fn2 = add_fn
                self.relu2 = torch.nn.ReLU()
                self.use_relu = use_relu

            def forward(self, x):
                x1 = self.conv1(x)
                x2 = self.conv2(x)
                tmp = self.add_fn(x1, x2)
                if self.use_relu:
                    tmp = self.relu(tmp)
                tmp1 = self.conv3(tmp)
                tmp2 = self.conv4(tmp)
                res = self.add_fn2(tmp1, tmp2)
                if self.use_relu:
                    res = self.relu2(res)
                return res

        for add_fn in quantization_add_fn_list + quantization_inplace_add_fn_list:
            mod = M(add_fn, use_relu).eval()
            v = torch.randn((1, 3, 8, 8), dtype=torch.float32, requires_grad=False).add(
                1
            )

            def matcher_check_fn():
                # 1. Dequant-Conv2D pattern matched in quantization weight prepack * 4
                self.assertEqual(
                    counters["inductor"]["qconv2d_weight_prepack_matcher_count"], 4
                )
                # 2. Qconv2d Binary Unary fusion in post-grad fusion pass * 2
                self.assertEqual(
                    counters["inductor"]["qconv2d_binary_matcher_count"], 2
                )

            self._test_common(
                mod,
                (v,),
                check_quantization=True,
                check_autocast=torch.bfloat16 if int8_mixed_bf16 else torch.float,
                matcher_check_fn=matcher_check_fn,
            )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qconv2d_add_cpu(self):
        self._qconv2d_add_cpu_test_helper()

    @skipIfNoDynamoSupport
    @skipIfNoONEDNNBF16
    @skipIfNoONEDNN
    def test_qconv2d_add_int8_mixed_bf16(self):
        self._qconv2d_add_cpu_test_helper(int8_mixed_bf16=True)

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qconv2d_add_relu_cpu(self):
        self._qconv2d_add_cpu_test_helper(use_relu=True)

    @skipIfNoDynamoSupport
    @skipIfNoONEDNNBF16
    @skipIfNoONEDNN
    def test_qconv2d_add_relu_int8_mixed_bf16(self):
        self._qconv2d_add_cpu_test_helper(use_relu=True, int8_mixed_bf16=True)

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qconv2d_add_broadcast_shapes_cpu(self):
        r"""
        This testcase will quantize Conv2d->add pattern using broadcast shape inputs.
        Conv2d->Add fusion will fail for the broadcast shape inputs case.
        """

        class M(torch.nn.Module):
            def __init__(self, use_bias):
                super().__init__()
                self.conv = torch.nn.Conv2d(32, 32, kernel_size=3, stride=1)

            def forward(self, x1, x2):
                return torch.add(self.conv(x1), x2)

        bias_list = [True, False]
        for bias in bias_list:
            mod = M(bias).eval()
            x1 = torch.randn((2, 32, 9, 9))
            x2 = torch.randn((2, 32, 1, 1))

            def matcher_check_fn():
                # 1. Dequant-Conv2D pattern matched in quantization weight prepack * 1
                self.assertEqual(
                    counters["inductor"]["qconv2d_weight_prepack_matcher_count"], 1
                )
                # 2. Qconv2d Binary Unary fusion in post-grad fusion pass * 0
                self.assertEqual(
                    counters["inductor"]["qconv2d_binary_matcher_count"], 0
                )

            self._test_common(
                mod,
                (x1, x2),
                check_quantization=True,
                matcher_check_fn=matcher_check_fn,
            )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qconv2d_add_2(self):
        r"""
        This testcase prevents this pattern be matched as a conv_binary fusion by mistake.
                Conv(X)  3
                    \   /
                     Add
        We see this pattern in Mobilenet v3 large which add is decomposed from torch.nn.Hardswish or torch.nn.Hardsigmoid.
        """

        class M(torch.nn.Module):
            def __init__(
                self,
                post_op,
            ):
                super().__init__()
                self.conv = torch.nn.Conv2d(3, 6, kernel_size=3, stride=1)
                self.post_op = post_op

            def forward(self, x):
                return self.post_op(self.conv(x))

        for post_op in [
            torch.nn.Hardswish(inplace=True),
            torch.nn.Hardsigmoid(inplace=True),
        ]:
            mod = M(post_op).eval()
            v = torch.randn((1, 3, 8, 8), dtype=torch.float32, requires_grad=False).add(
                1
            )

            def matcher_check_fn():
                # Shouldn't hit conv binary fusion
                self.assertEqual(
                    counters["inductor"]["qconv2d_binary_matcher_count"], 0
                )

            self._test_common(
                mod,
                (v,),
                check_quantization=True,
                matcher_check_fn=matcher_check_fn,
            )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qconv2d_add_3(self):
        r"""
        This testcase will test below model:
             x
           /   \
        conv1  maxpool
          \    /   \
           add    conv2
            \     /
              cat
        Based on default recipe of x86InductorQuantizer, we will see this pattern after convert:
        qconv1    maxpool
         \           |
          \         q1
           \       /   \
            \     dq1  qconv2
             \   /
              add
               |
               q2
        Since q1 has 2 users and qconv2 is not ancestor node of qconv1, we shouldn't fuse:
                int8
                 /
        qconv1 dq1
           \   /
            add
             |
             q2
             |
            int8
        Instead we can match and fuse this pattern into qconv_binary:
        qconv1  fp32
            \   /
             add
              |
             fp32
        """

        class M(torch.nn.Module):
            def __init__(
                self,
            ):
                super().__init__()
                self.conv1 = torch.nn.Conv2d(3, 3, kernel_size=3, stride=1)
                self.conv2 = torch.nn.Conv2d(3, 3, kernel_size=1, stride=1)
                self.maxpool = torch.nn.MaxPool2d(
                    kernel_size=3, stride=1, padding=0, dilation=1
                )

            def forward(self, x):
                tmp1 = self.conv1(x)
                tmp2 = self.maxpool(x)
                add = torch.add(tmp1, tmp2)
                tmp3 = self.conv2(tmp2)
                return torch.cat((add, tmp3), dim=1)

        mod = M().eval()
        v = torch.randn((1, 3, 8, 8), dtype=torch.float32, requires_grad=False).add(1)

        def matcher_check_fn():
            self.assertEqual(counters["inductor"]["qconv2d_binary_matcher_count"], 1)
            # The matched qconv binary pattern should have 2 nodes [qconv, add]
            # instead of 11 which has dequant in binary input and output quant
            self.assertEqual(counters["inductor"]["qconv2d_binary_matcher_nodes"], 2)

        self._test_common(
            mod,
            (v,),
            check_quantization=True,
            matcher_check_fn=matcher_check_fn,
        )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    @skipIfRocm
    def test_qat_qconv2d(self):
        r"""
        This testcase will quantize a single Conv2d module with qat flow.
        """

        class M(torch.nn.Module):
            def __init__(
                self,
                **kwargs,
            ):
                super().__init__()
                self.conv = torch.nn.Conv2d(3, 128, kernel_size=3, stride=1)
                self.bn = torch.nn.BatchNorm2d(128)

            def forward(self, x):
                return self.bn(self.conv(x))

        mod = M().train()
        v = torch.randn((1, 3, 8, 8), dtype=torch.float32, requires_grad=True).add(1)

        def matcher_check_fn():
            # 1. Dequant-conv pattern matched in quantization weight prepack * 1
            #    [dequantize_per_tensor, dequantize_per_channel, clone, convolution]
            self.assertEqual(
                counters["inductor"]["qconv2d_weight_prepack_matcher_count"], 1
            )
            self.assertEqual(
                counters["inductor"]["qconv2d_weight_prepack_matcher_nodes"], 4
            )
            # 2. QConv2D Unary fusion in post-grad fusion pass * 1
            #    [qconv2d_pointwise_default, quantize_per_tensor]
            self.assertEqual(counters["inductor"]["qconv2d_unary_matcher_count"], 1)
            self.assertEqual(counters["inductor"]["qconv2d_unary_matcher_nodes"], 2)

        self._test_common(
            mod,
            (v,),
            check_quantization=True,
            is_qat=True,
            matcher_check_fn=matcher_check_fn,
        )

    def _qat_qconv2d_unary_cpu_test_helper(
        self,
        unary_op=torch.nn.ReLU(),
    ):
        class M(torch.nn.Module):
            def __init__(
                self,
                **kwargs,
            ):
                super().__init__()
                self.conv = torch.nn.Conv2d(3, 3, kernel_size=3, stride=1)
                self.unary_fn = copy.deepcopy(unary_op)
                self.bn = torch.nn.BatchNorm2d(3)
                self.conv2 = torch.nn.Conv2d(3, 3, kernel_size=3, stride=1)
                self.unary_fn2 = copy.deepcopy(unary_op)
                self.bn2 = torch.nn.BatchNorm2d(3)

            def forward(self, x):
                tmp = self.unary_fn(self.bn(self.conv(x)))
                return self.unary_fn2(self.bn2(self.conv2(tmp)))

        mod = M()
        v = torch.randn((1, 3, 8, 8), dtype=torch.float32, requires_grad=True).add(1)

        def matcher_check_fn():
            # 1. Dequant-conv pattern matched in quantization weight prepack * 1
            #    [convert_element_type_1, sub, mul_1, dequantize_per_channel, clone, convolution]
            self.assertEqual(
                counters["inductor"]["qconv2d_weight_prepack_matcher_count"], 2
            )
            # 2. QConv2D Unary fusion in post-grad fusion pass * 1
            #    [qconv2d_pointwise_default, relu, div_1, round_2, add_1, clamp_min_1, clamp_max_1, convert_element_type_2]
            self.assertEqual(counters["inductor"]["qconv2d_unary_matcher_count"], 2)

        self._test_common(
            mod,
            (v,),
            check_quantization=True,
            is_qat=True,
            matcher_check_fn=matcher_check_fn,
        )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qat_qconv2d_relu(self):
        r"""
        This testcase will quantize Conv2d->ReLU pattern with qat flow.
        """

        self._qat_qconv2d_unary_cpu_test_helper()

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qat_qconv2d_relu6(self):
        r"""
        This testcase will quantize Conv2d->ReLU6 pattern with qat flow.
        """
        self._qat_qconv2d_unary_cpu_test_helper(unary_op=torch.nn.ReLU6())

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qat_qconv2d_hardtanh(self):
        r"""
        This testcase will quantize Conv2d->Hardtanh pattern with qat flow.
        """
        self._qat_qconv2d_unary_cpu_test_helper(unary_op=torch.nn.Hardtanh())

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qat_qconv2d_silu(self):
        r"""
        This testcase will quantize Conv2d->SiLU pattern with qat flow.
        """
        self._qat_qconv2d_unary_cpu_test_helper(unary_op=torch.nn.SiLU())

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qat_qconv2d_hardswish(self):
        r"""
        This testcase will quantize Conv2d->Hardswish pattern with qat flow.
        """
        self._qat_qconv2d_unary_cpu_test_helper(unary_op=torch.nn.Hardswish())

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    @skipIfRocm
    def test_qat_qconv2d_add(self):
        r"""
        This testcase will quantize a Conv2d->Add pattern as:
                 X
               /   \
        Conv1(X)   Conv2(X)
               \   /
                Add
                 |
                 Y
        """

        class M(torch.nn.Module):
            def __init__(
                self,
                **kwargs,
            ):
                super().__init__()
                self.conv1 = torch.nn.Conv2d(3, 6, kernel_size=3, stride=1)
                self.bn1 = torch.nn.BatchNorm2d(6)
                self.conv2 = torch.nn.Conv2d(3, 6, kernel_size=3, stride=1)
                self.bn2 = torch.nn.BatchNorm2d(6)

            def forward(self, x):
                x1 = self.bn1(self.conv1(x))
                x2 = self.bn2(self.conv2(x))
                return x1 + x2

        mod = M().train()
        v = torch.randn((1, 3, 8, 8), dtype=torch.float32, requires_grad=True).add(1)

        def matcher_check_fn():
            # 1. Dequant-conv pattern matched in quantization weight prepack * 2
            #    [dequantize_per_tensor, dequantize_per_channel, clone, convolution]
            self.assertEqual(
                counters["inductor"]["qconv2d_weight_prepack_matcher_count"], 2
            )
            self.assertEqual(
                counters["inductor"]["qconv2d_weight_prepack_matcher_nodes"], 8
            )
            # 2. Qconv2d Binary fusion in post-grad fusion pass * 1
            #    [qconv2d_pointwise_default_1, dequantize_per_tensor, add_3, quantize_per_tensor]
            self.assertEqual(counters["inductor"]["qconv2d_binary_matcher_count"], 1)
            self.assertEqual(counters["inductor"]["qconv2d_binary_matcher_nodes"], 4)

        self._test_common(
            mod,
            (v,),
            check_quantization=True,
            is_qat=True,
            matcher_check_fn=matcher_check_fn,
        )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    @skipIfRocm
    def test_qat_qconv2d_add_relu(self):
        r"""
        This testcase will quantize a Conv2d->Add->ReLU pattern as:
                 X
               /   \
        Conv1(X)   Conv2(X)
               \   /
                Add
                 |
                ReLU
                 |
                 Y
        """

        class M(torch.nn.Module):
            def __init__(
                self,
                **kwargs,
            ):
                super().__init__()
                self.conv1 = torch.nn.Conv2d(3, 6, kernel_size=3, stride=1)
                self.bn1 = torch.nn.BatchNorm2d(6)
                self.conv2 = torch.nn.Conv2d(3, 6, kernel_size=3, stride=1)
                self.bn2 = torch.nn.BatchNorm2d(6)
                self.relu = torch.nn.ReLU()

            def forward(self, x):
                x1 = self.bn1(self.conv1(x))
                x2 = self.bn2(self.conv2(x))
                return self.relu(x1 + x2)

        mod = M().train()
        v = torch.randn((1, 3, 8, 8), dtype=torch.float32, requires_grad=True).add(1)

        def matcher_check_fn():
            # 1. Dequant-conv pattern matched in quantization weight prepack * 2
            #    [dequantize_per_tensor, dequantize_per_channel, clone, convolution]
            self.assertEqual(
                counters["inductor"]["qconv2d_weight_prepack_matcher_count"], 2
            )
            self.assertEqual(
                counters["inductor"]["qconv2d_weight_prepack_matcher_nodes"], 8
            )
            # 2. Qconv2d Binary fusion in post-grad fusion pass * 1
            #    [qconv2d_pointwise_default_1, dequantize_per_tensor, add_3, relu, quantize_per_tensor]
            self.assertEqual(counters["inductor"]["qconv2d_binary_matcher_count"], 1)
            self.assertEqual(counters["inductor"]["qconv2d_binary_matcher_nodes"], 5)

        self._test_common(
            mod,
            (v,),
            check_quantization=True,
            is_qat=True,
            matcher_check_fn=matcher_check_fn,
        )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    @skipIfRocm
    def test_qconv2d_dequant_promotion_cpu(self):
        r"""
        This testcase tests if dequant node before conv2d is promoted correctly:
                 X
                 |
              Conv1(X)
               /   \
        Conv2(X)   Conv3(X)
               \   /
                Add
                 |
                 Y
        """

        class M(torch.nn.Module):
            def __init__(
                self,
                **kwargs,
            ):
                super().__init__()
                self.conv1 = torch.nn.Conv2d(3, 6, kernel_size=3, stride=1)
                self.conv2 = torch.nn.Conv2d(6, 6, kernel_size=3, stride=1)
                self.conv3 = torch.nn.Conv2d(6, 6, kernel_size=3, stride=1)

            def forward(self, x):
                temp = self.conv1(x)
                temp = self.conv2(temp) + self.conv3(temp)
                return temp

        mod = M().eval()
        v = torch.randn((1, 3, 8, 8), dtype=torch.float32, requires_grad=False).add(1)

        def matcher_check_fn():
            # 1. Dequant pattern matcher for dequant promotion * 1
            #    [dequantize_per_tensor]
            self.assertEqual(counters["inductor"]["dequant_promotion_matcher_count"], 1)
            self.assertEqual(counters["inductor"]["dequant_promotion_matcher_nodes"], 1)
            # 2. Dequant-conv pattern matched in quantization weight prepack * 3
            #    [dequantize_per_tensor, dequantize_per_channel, clone, convolution]
            self.assertEqual(
                counters["inductor"]["qconv2d_weight_prepack_matcher_count"], 3
            )
            self.assertEqual(
                counters["inductor"]["qconv2d_weight_prepack_matcher_nodes"], 12
            )
            # 3. Qconv2d Binary fusion in post-grad fusion pass * 1
            #    [qconv2d_pointwise_default_1, add_3]
            self.assertEqual(counters["inductor"]["qconv2d_binary_matcher_count"], 1)
            self.assertEqual(counters["inductor"]["qconv2d_binary_matcher_nodes"], 2)

        self._test_common(
            mod,
            (v,),
            check_quantization=True,
            matcher_check_fn=matcher_check_fn,
        )

    def _qlinear_cpu_test_helper(
        self,
        inputs,
        int8_mixed_bf16=False,
        do_permute=False,
        matcher_check_fn=None,
        bias=True,
        is_dynamic=False,
        is_qat=False,
    ):
        class M(torch.nn.Module):
            def __init__(self, use_bias, do_permute=False):
                super().__init__()
                self.linear = torch.nn.Linear(4, 3, use_bias)
                self.linear2 = torch.nn.Linear(3, 4, use_bias)
                self.do_permute = do_permute

            def forward(self, x):
                if self.do_permute:
                    x = torch.reshape(torch.permute(x, (0, 2, 3, 1)), (2, 12, 4))
                return self.linear2(self.linear(x))

        mod = M(bias, do_permute=do_permute).eval()

        def _default_matcher_check_fn():
            self.assertEqual(
                counters["inductor"]["qlinear_weight_prepack_matcher_count"], 2
            )

        self._test_common(
            mod,
            inputs,
            check_autocast=torch.bfloat16 if int8_mixed_bf16 else torch.float,
            check_quantization=True,
            matcher_check_fn=matcher_check_fn
            if matcher_check_fn is not None
            else _default_matcher_check_fn,
            is_qat=is_qat,
            is_dynamic=is_dynamic,
        )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qlinear_cpu(self):
        r"""
        This testcase will quantize a single Linear Moduel.
        """
        for bias in [True, False]:
            self._qlinear_cpu_test_helper((torch.randn((2, 4)),), bias=bias)

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_dynamic_qlinear_cpu(self):
        r"""
        This testcase will quantize a single Linear Moduel.
        """
        for bias in [True, False]:
            self._qlinear_cpu_test_helper(
                (torch.randn((2, 4)),), bias=bias, is_dynamic=True
            )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_dynamic_qlinear_qat_cpu(self):
        r"""
        This testcase will quantize a single Linear Moduel.
        """
        for bias in [True, False]:
            self._qlinear_cpu_test_helper(
                (torch.randn((2, 4)),), bias=bias, is_dynamic=True, is_qat=True
            )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_dynamic_qlinear_input_dim_exceeds_2(self):
        r"""
        This testcase will quantize a single Linear Moduel.
        """
        for bias in [True, False]:
            self._qlinear_cpu_test_helper(
                (torch.randn((2, 3, 4)),), bias=bias, is_dynamic=True
            )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNNBF16
    @skipIfNoONEDNN
    def test_qlinear_int8_mixed_bf16(self):
        r"""
        This testcase will quantize a single Linear Moduel with int8_mixed_bf16 quantization.
        """
        for bias in [True, False]:
            self._qlinear_cpu_test_helper(
                (torch.randn((2, 4)),), int8_mixed_bf16=True, bias=bias
            )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qlinear_input_dim_exceeds_2(self):
        r"""
        This testcase will quantize a single Linear Moduel.
        """
        for bias in [True, False]:
            self._qlinear_cpu_test_helper((torch.randn((2, 3, 4)),), bias=bias)

    @skipIfNoDynamoSupport
    @skipIfNoONEDNNBF16
    @skipIfNoONEDNN
    def test_qlinear_int8_mixed_bf16_input_dim_exceeds_2(self):
        r"""
        This testcase will quantize a single Linear Moduel with int8_mixed_bf16 quantization.
        """
        for bias in [True, False]:
            self._qlinear_cpu_test_helper(
                (torch.randn((2, 3, 4)),), int8_mixed_bf16=True, bias=bias
            )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qlinear_input_dim_exceeds_2_and_not_contiguous(self):
        r"""
        This testcase will quantize a single Linear Module.
        * Input dim exceeds 2
        * Input not contiguous
        """
        for bias in [True, False]:

            def matcher_check_fn():
                self.assertEqual(
                    counters["inductor"]["qlinear_weight_prepack_matcher_count"], 2
                )
                self.assertEqual(
                    counters["inductor"]["qlinear_weight_prepack_matcher_nodes"],
                    13 if bias else 12,
                )

            self._qlinear_cpu_test_helper(
                (torch.randn((2, 4, 3, 4)),),
                do_permute=True,
                matcher_check_fn=matcher_check_fn,
                bias=bias,
            )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNNBF16
    @skipIfNoONEDNN
    def test_qlinear_int8_mixed_bf16_input_dim_exceeds_2_and_not_contiguous(self):
        r"""
        This testcase will quantize a single Linear Module for int8_bf16.
        * Input dim exceeds 2
        * Input not contiguous
        """
        for bias in [True, False]:

            def matcher_check_fn():
                self.assertEqual(
                    counters["inductor"]["qlinear_weight_prepack_matcher_count"], 2
                )
                self.assertEqual(
                    counters["inductor"]["qlinear_weight_prepack_matcher_nodes"],
                    17 if bias else 16,
                )

            self._qlinear_cpu_test_helper(
                (torch.randn((2, 4, 3, 4)),),
                int8_mixed_bf16=True,
                do_permute=True,
                matcher_check_fn=matcher_check_fn,
                bias=bias,
            )

    def _qlinear_unary_cpu_test_helper(
        self, inputs, unary_op=torch.nn.ReLU(), int8_mixed_bf16=False
    ):
        class M(torch.nn.Module):
            def __init__(self, use_bias):
                super().__init__()
                self.linear = torch.nn.Linear(4, 4, use_bias)
                self.unary_fn = copy.deepcopy(unary_op)
                self.linear2 = torch.nn.Linear(4, 4, use_bias)
                self.unary_fn2 = copy.deepcopy(unary_op)

            def forward(self, x):
                tmp = self.unary_fn(self.linear(x))
                return self.unary_fn2(self.linear2(tmp))

        bias_list = [True, False]
        for bias in bias_list:
            mod = M(bias).eval()

            def matcher_check_fn():
                # 1. dequant-linear pattern matched in quantization weight prepack
                self.assertEqual(
                    counters["inductor"]["qlinear_weight_prepack_matcher_count"], 2
                )
                # 2. QLinear Unary fusion in post-grad fusion pass
                self.assertEqual(counters["inductor"]["qlinear_unary_matcher_count"], 2)

            self._test_common(
                mod,
                inputs,
                check_autocast=torch.bfloat16 if int8_mixed_bf16 else torch.float,
                check_quantization=True,
                matcher_check_fn=matcher_check_fn,
            )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qlinear_relu_cpu(self):
        r"""
        This testcase will quantize a Linear->ReLU pattern.
        """
        self._qlinear_unary_cpu_test_helper((torch.randn((2, 4)),))

    @skipIfNoDynamoSupport
    @skipIfNoONEDNNBF16
    @skipIfNoONEDNN
    def test_qlinear_relu_int8_mixed_bf16(self):
        r"""
        This testcase will quantize a Linear->ReLU pattern with int8_mixed_bf16 quantization.
        """
        self._qlinear_unary_cpu_test_helper(
            (torch.randn((2, 4)),), int8_mixed_bf16=True
        )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qlinear_relu_input_dim_exceeds_2(self):
        r"""
        This testcase will quantize a Linear->ReLU pattern.
        """
        self._qlinear_unary_cpu_test_helper((torch.randn((2, 3, 4)),))

    @skipIfNoDynamoSupport
    @skipIfNoONEDNNBF16
    @skipIfNoONEDNN
    def test_qlinear_relu_int8_mixed_bf16_input_dim_exceeds_2(self):
        r"""
        This testcase will quantize a Linear->ReLU pattern with int8_mixed_bf16 quantization.
        """
        self._qlinear_unary_cpu_test_helper(
            (torch.randn((2, 3, 4)),), int8_mixed_bf16=True
        )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qlinear_gelu_cpu(self):
        r"""
        This testcase will quantize a Linear->GELU pattern.
        """
        for gelu in [torch.nn.GELU("none"), torch.nn.GELU("tanh")]:
            self._qlinear_unary_cpu_test_helper((torch.randn((2, 4)),), gelu)

    @skipIfNoDynamoSupport
    @skipIfNoONEDNNBF16
    @skipIfNoONEDNN
    def test_qlinear_gelu_int8_mixed_bf16(self):
        r"""
        This testcase will quantize a Linear->GELU pattern with int8_mixed_bf16 quantization.
        """
        for gelu in [torch.nn.GELU("none"), torch.nn.GELU("tanh")]:
            self._qlinear_unary_cpu_test_helper(
                (torch.randn((2, 4)),), gelu, int8_mixed_bf16=True
            )

    def _qlinear_add_cpu_test_helper(self, use_relu=False, int8_mixed_bf16=False):
        r"""
        This testcase will quantize two consecutive Linear->Add(->relu) patterns as:
                 X
               /   \
        linear(X)   linear(X)
               \   /
                Add
                 |
           Optional(relu)
               /   \
        linear(X)   linear(X)
               \   /
                Add
                 |
           Optional(relu)
                 |
                 Y
        """

        def fake_quant(x):
            # to produce a float32 result as extra input
            qlib = torch.ops.quantized_decomposed
            x = qlib.quantize_per_tensor.default(x, 0.0166785, 42, 0, 255, torch.uint8)
            x = qlib.dequantize_per_tensor.default(
                x, 0.0166785, 42, 0, 255, torch.uint8
            )
            return x

        class M(torch.nn.Module):
            def __init__(
                self,
                add_fn,
                use_relu,
                fake_quant_before_extra_input,
            ):
                super().__init__()
                self.linear1 = torch.nn.Linear(4, 4)
                self.linear2 = torch.nn.Linear(4, 4)
                self.add_fn = add_fn
                self.relu = torch.nn.ReLU()
                self.linear3 = torch.nn.Linear(4, 4)
                self.linear4 = torch.nn.Linear(4, 4)
                self.add_fn2 = add_fn
                self.relu2 = torch.nn.ReLU()
                self.use_relu = use_relu
                self.fake_quant_before_extra_input = fake_quant_before_extra_input

            def forward(self, x):
                x1 = self.linear1(x)
                x2 = self.linear2(x)
                if self.fake_quant_before_extra_input:
                    x2 = fake_quant(x2)
                tmp = self.add_fn(x1, x2)
                if self.use_relu:
                    tmp = self.relu(tmp)
                tmp1 = self.linear3(tmp)
                tmp2 = self.linear4(tmp)
                if self.fake_quant_before_extra_input:
                    tmp2 = fake_quant(tmp2)
                res = self.add_fn2(tmp1, tmp2)
                if self.use_relu:
                    res = self.relu2(res)
                return res

        add_fn_list = [
            lambda x, y: x + y,
            lambda x, y: y + x,
            lambda x, y: x.add_(y),
            lambda x, y: y.add_(x),
        ]
        fake_quant_x2_list = [False, True] if int8_mixed_bf16 else [False]
        cases = itertools.product(add_fn_list, fake_quant_x2_list)
        for add_fn, fq_x2 in cases:
            mod = M(add_fn, use_relu, fq_x2).eval()
            v = torch.randn((4, 4), dtype=torch.float32, requires_grad=False).add(1)

            def matcher_check_fn():
                # 1. Dequant-linear pattern matched in quantization weight prepack * 4
                self.assertEqual(
                    counters["inductor"]["qlinear_weight_prepack_matcher_count"], 4
                )
                # pattern = [dequant_per_tensor, (convert_dtype), dequant_per_channel, (convert_dtype), permute, addmm]
                nodes_per_match = 6 if int8_mixed_bf16 else 4
                self.assertEqual(
                    counters["inductor"]["qlinear_weight_prepack_matcher_nodes"],
                    4 * nodes_per_match,
                )
                # 2. Qlinear Binary Unary fusion in post-grad fusion pass * 2
                self.assertEqual(
                    counters["inductor"]["qlinear_binary_matcher_count"], 2
                )
                # Two linear-binary patterns are matched
                # matched patter1 = [qlinear, add, (convert dtype), (relu), quantize_per_tensor]
                # matched patter2 = [qlinear, add, (convert dtype), (relu)]
                # If add_fn is x.add_(y), x is bf16 and y is fp32, there is a to_bf16 node after binary
                to_bf16_after_binary = 2 * (add_fn == add_fn_list[2] and fq_x2)
                self.assertEqual(
                    counters["inductor"]["qlinear_binary_matcher_nodes"],
                    (4 if is_dynamic else 5) + 2 * use_relu + to_bf16_after_binary,
                )

            is_qat_list = [False, True]
            is_dynamic_list = [False, True]
            cases = itertools.product(is_qat_list, is_dynamic_list)
            for is_qat, is_dynamic in cases:
                self._test_common(
                    mod,
                    (v,),
                    check_quantization=True,
                    check_autocast=torch.bfloat16 if int8_mixed_bf16 else torch.float,
                    matcher_check_fn=matcher_check_fn,
                    is_qat=is_qat,
                    is_dynamic=is_dynamic,
                )
                if torch._inductor.config.cpp_wrapper:
                    # For CPP wrapper
                    self._test_code_common(
                        mod,
                        (v,),
                        [
                            "torch.ops.onednn.qlinear_pointwise.tensor",
                            "torch.ops.onednn.qlinear_pointwise.binary",
                        ]
                        if config.abi_compatible
                        else [
                            "op_onednn_qlinear_pointwise_tensor.call",
                            "op_onednn_qlinear_pointwise_binary_tensor.call",
                        ],
                        [],
                        check_quantization=True,
                        num_include_ops=[4, 4] if config.abi_compatible else [2, 2],
                    )
                else:
                    # For python wrapper
                    self._test_code_common(
                        mod,
                        (v,),
                        [
                            "torch.ops.onednn.qlinear_pointwise.tensor",
                            "torch.ops.onednn.qlinear_pointwise.binary",
                        ],
                        [],
                        check_quantization=True,
                        num_include_ops=[2, 2],
                    )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qlinear_add_cpu(self):
        self._qlinear_add_cpu_test_helper()

    @skipIfNoDynamoSupport
    @skipIfNoONEDNNBF16
    @skipIfNoONEDNN
    def test_qlinear_add_int8_mixed_bf16(self):
        self._qlinear_add_cpu_test_helper(int8_mixed_bf16=True)

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qlinear_add_relu_cpu(self):
        self._qlinear_add_cpu_test_helper(use_relu=True)

    @skipIfNoDynamoSupport
    @skipIfNoONEDNNBF16
    @skipIfNoONEDNN
    def test_qlinear_add_relu_int8_mixed_bf16(self):
        self._qlinear_add_cpu_test_helper(use_relu=True, int8_mixed_bf16=True)

    def _qlinear_dequant_promotion_cpu_test_helper(
        self,
        inputs,
        int8_mixed_bf16=False,
        is_dynamic=False,
        matcher_check_fn=None,
    ):
        class M(torch.nn.Module):
            def __init__(
                self,
                **kwargs,
            ):
                super().__init__()
                self.linear1 = torch.nn.Linear(4, 4)
                self.linear2 = torch.nn.Linear(4, 4)
                self.linear3 = torch.nn.Linear(4, 4)

            def forward(self, x):
                temp = self.linear1(x)
                temp = self.linear2(temp) + self.linear3(temp)
                return temp

        mod = M().eval()

        def default_matcher_check_fn():
            # 1. Dequant pattern matcher for dequant promotion * 1
            self.assertEqual(counters["inductor"]["dequant_promotion_matcher_count"], 1)
            # 2. dequant-linear pattern matched in quantization weight prepack * 3
            self.assertEqual(
                counters["inductor"]["qlinear_weight_prepack_matcher_count"], 3
            )
            # 3. QLinear Unary fusion in post-grad fusion pass * 1
            self.assertEqual(counters["inductor"]["qlinear_unary_matcher_count"], 1)

        self._test_common(
            mod,
            inputs,
            check_autocast=torch.bfloat16 if int8_mixed_bf16 else torch.float,
            check_quantization=True,
            matcher_check_fn=matcher_check_fn
            if matcher_check_fn is not None
            else default_matcher_check_fn,
            is_dynamic=is_dynamic,
        )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qlinear_dequant_promotion_cpu(self):
        r"""
        This testcase test if dequant node before linear is promoted correctly:
                  X
                  |
               Linear1(X)
                /   \
        Linear2(X)   Linear3(X)
                \   /
                 Add
                  |
                  Y
        """
        self._qlinear_dequant_promotion_cpu_test_helper((torch.randn((2, 4)),))

    @skipIfNoDynamoSupport
    @skipIfNoONEDNNBF16
    @skipIfNoONEDNN
    def test_qlinear_dequant_promotion_int8_mixed_bf16(self):
        r"""
        Test with int8_mixed_bf16 quantization.
        This testcase test if dequant node before linear is promoted correctly:
                  X
                  |
               Linear1(X)
                /   \
        Linear2(X)   Linear3(X)
                \   /
                 Add
                  |
                  Y
        """
        self._qlinear_dequant_promotion_cpu_test_helper(
            (torch.randn((2, 4)),), int8_mixed_bf16=True
        )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qlinear_dequant_promotion_cpu_input_dim_exceeds_2(self):
        r"""
        This testcase test if dequant node before linear is promoted correctly:
                  X
                  |
               Linear1(X)
                /   \
        Linear2(X)   Linear3(X)
                \   /
                 Add
                  |
                  Y
        """
        self._qlinear_dequant_promotion_cpu_test_helper((torch.randn((2, 3, 4)),))

    @skipIfNoDynamoSupport
    @skipIfNoONEDNNBF16
    @skipIfNoONEDNN
    def test_qlinear_dequant_promotion_int8_mixed_bf16_input_dim_exceeds_2(self):
        r"""
        Test with int8_mixed_bf16 quantization.
        This testcase test if dequant node before linear is promoted correctly:
                  X
                  |
               Linear1(X)
                /   \
        Linear2(X)   Linear3(X)
                \   /
                 Add
                  |
                  Y
        """
        self._qlinear_dequant_promotion_cpu_test_helper(
            (torch.randn((2, 3, 4)),), int8_mixed_bf16=True
        )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qlinear_dequant_promotion_dynamic_cpu(self):
        r"""
        This testcase test if dequant node before linear is promoted correctly:
                  X
                  |
               Linear1(X)
                /   \
        Linear2(X)   Linear3(X)
                \   /
                 Add
                  |
                  Y
        """

        def matcher_check_fn():
            # 1. Dequant pattern matcher for dequant promotion * 1
            self.assertEqual(counters["inductor"]["dequant_promotion_matcher_count"], 1)
            # 2. dequant-linear pattern matched in quantization weight prepack * 3
            self.assertEqual(
                counters["inductor"]["qlinear_weight_prepack_matcher_count"], 3
            )

        self._qlinear_dequant_promotion_cpu_test_helper(
            (torch.randn((2, 4)),),
            matcher_check_fn=matcher_check_fn,
            is_dynamic=True,
        )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qlinear_mul_cpu(self):
        r"""
        This testcase will quantize a Linear->Mul pattern.
        """

        class M(torch.nn.Module):
            def __init__(self, use_bias):
                super().__init__()
                self.linear = torch.nn.Linear(4, 5, use_bias)

            def forward(self, x1, x2):
                return torch.mul(self.linear(x1), x2)

        bias_list = [True, False]
        for bias in bias_list:
            mod = M(bias).eval()
            x1 = torch.randn((2, 4))
            x2 = torch.randn((2, 5))

            def matcher_check_fn():
                self.assertEqual(
                    counters["inductor"]["qlinear_weight_prepack_matcher_count"], 1
                )

            self._test_common(
                mod,
                (x1, x2),
                check_quantization=True,
                matcher_check_fn=matcher_check_fn,
            )

    @skipIfNoDynamoSupport
    def test_qmaxpool2d(self):
        r"""
        This testcase will quantize Conv2d->ReLU->MaxPool2d pattern.
        """

        class M(torch.nn.Module):
            def __init__(
                self,
                kwargs,
            ):
                super().__init__()
                self.conv = torch.nn.Conv2d(
                    3, 64, 7, bias=True, stride=2, padding=3, dilation=1
                )
                self.relu = torch.nn.ReLU()
                self.maxpool = torch.nn.MaxPool2d(3, **kwargs)

            def forward(self, x):
                return self.maxpool(self.relu(self.conv(x)))

        kwargs_list = [
            {"stride": 2},
            {"stride": 2, "padding": 1},
            {"stride": 2, "padding": 1, "dilation": 1},
            {"stride": 2, "padding": 1, "dilation": 1, "ceil_mode": False},
        ]
        for kwargs in kwargs_list:
            mod = M(kwargs).eval()
            v = torch.randn((1, 3, 8, 8), dtype=torch.float32, requires_grad=False).add(
                1
            )

            def matcher_check_fn():
                self.assertEqual(counters["inductor"]["qmaxpool2d_matcher_count"], 1)
                self.assertEqual(
                    counters["inductor"]["qconv2d_weight_prepack_matcher_count"], 1
                )
                self.assertEqual(counters["inductor"]["qconv2d_unary_matcher_count"], 1)

            self._test_common(
                mod,
                (v,),
                check_quantization=True,
                matcher_check_fn=matcher_check_fn,
            )

    @skipIfNoDynamoSupport
    def test_qflatten(self):
        r"""
        This testcase will quantize Conv2d->AdaptiveAvgPool2d->flatten pattern.
        """

        class M(torch.nn.Module):
            def __init__(
                self,
            ):
                super().__init__()
                self.conv = torch.nn.Conv2d(
                    3, 64, 7, bias=True, stride=2, padding=3, dilation=1
                )
                self.relu = torch.nn.ReLU()
                self.adaptive_avg_pool2d = torch.nn.AdaptiveAvgPool2d((1, 1))

            def forward(self, x):
                return torch.flatten(
                    self.adaptive_avg_pool2d(self.relu(self.conv(x))), 1
                )

        mod = M().eval()
        v = torch.randn((1, 3, 8, 8), dtype=torch.float32, requires_grad=False).add(1)

        def matcher_check_fn():
            self.assertEqual(counters["inductor"]["qreshape_matcher_count"], 1)

        self._test_common(
            mod,
            (v,),
            check_quantization=True,
            matcher_check_fn=matcher_check_fn,
        )

    @skipIfNoDynamoSupport
    def test_qcat(self):
        r"""
        This testcase will quantize cat based pattern:
                X
             /     \
        Conv1(X)  Pow(x)
            \        \
             \     Conv2(X)
              \    /
               Cat
                |
                Y
        """

        class M(torch.nn.Module):
            def __init__(
                self,
            ):
                super().__init__()
                self.conv = torch.nn.Conv2d(
                    3, 64, 7, bias=True, stride=2, padding=3, dilation=1
                )
                self.conv2 = torch.nn.Conv2d(
                    3, 64, 7, bias=True, stride=2, padding=3, dilation=1
                )

            def forward(self, x):
                temp1 = self.conv(x)
                temp2 = self.conv2(torch.pow(x, 2))
                return torch.cat((temp1, temp2), 1)

        mod = M().eval()
        v = torch.randn((1, 3, 8, 8), dtype=torch.float32, requires_grad=False).add(1)

        def matcher_check_fn():
            self.assertEqual(counters["inductor"]["qcat_matcher_count"], 1)
            self.assertEqual(
                counters["inductor"]["qconv2d_weight_prepack_matcher_count"], 2
            )
            self.assertEqual(counters["inductor"]["qconv2d_unary_matcher_count"], 2)

        self._test_common(
            mod,
            (v,),
            check_quantization=True,
            matcher_check_fn=matcher_check_fn,
        )

    # https://github.com/pytorch/pytorch/issues/99841.
    def test_hardtanh_pattern_fallback(self):
        class Model(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv_transpose = torch.nn.ConvTranspose2d(
                    in_channels=3, out_channels=32, kernel_size=3, stride=1, padding=1
                )

            def forward(self, x, min_value, max_value):
                conv_transpose_output = self.conv_transpose(x)
                clamp_min_output = torch.clamp_min(conv_transpose_output, min_value)
                clamp_max_output = torch.clamp_max(clamp_min_output, max_value)
                return clamp_max_output

        # check works for min_value > max_value.
        min_values = [3, torch.randn(1, 32, 28, 28)]
        max_values = [0, torch.randn(1, 32, 28, 28)]
        v = torch.randn(1, 3, 28, 28)
        for min_value, max_value in zip(min_values, max_values):
            mod = Model().eval()
            self._test_common(mod, (v, min_value, max_value), 2, 4)

    def test_leaky_relu_pattern_fallback(self):
        class Model(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels=3, out_channels=32, kernel_size=3, stride=1, padding=1
                )

            def forward(self, x, negative_slope):
                conv_out = self.conv(x)
                return torch.where(conv_out > 0, conv_out, conv_out * negative_slope)

        negative_slopes = [0.1, torch.randn(1, 32, 28, 28)]
        with torch.no_grad():
            v = torch.randn(1, 3, 28, 28)
            for negative_slope in negative_slopes:
                mod = Model().eval()
                self._test_common(mod, (v, negative_slope), 2, 5)

    # https://github.com/pytorch/pytorch/issues/99838.
    def test_conv2d_add_scalar(self):
        class Model(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels=3, out_channels=32, kernel_size=3, stride=1, padding=1
                )

            def forward(self, x):
                out_conv = self.conv(x)
                out = torch.add(out_conv, 1.0)
                return out

        with torch.no_grad():
            mod = Model().eval()
            v = torch.randn(1, 3, 28, 28)
            self._test_common(mod, (v,), 1, 1)

    def test_conv2d_binary_inplace_fusion_pass_cpu(
        self, include_ops=None, exclude_ops=None
    ):
        class Model_v1(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels=3, out_channels=32, kernel_size=3, stride=1, padding=1
                )

            def forward(self, x, other):
                conv_out = self.conv(x)
                return torch.add(conv_out, other.relu())

        class Model_v2(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels=3, out_channels=32, kernel_size=3, stride=1, padding=1
                )
                self.conv2 = torch.nn.Conv2d(
                    in_channels=32, out_channels=32, kernel_size=3, stride=1, padding=1
                )
                self.conv3 = torch.nn.Conv2d(
                    in_channels=32, out_channels=32, kernel_size=3, stride=1, padding=1
                )

            def forward(self, x, _):
                conv_out1 = self.conv(x)
                pow_out = torch.pow(conv_out1, 2)
                conv_out2 = self.conv2(pow_out)
                conv_out3 = self.conv3(conv_out2)
                res = torch.add(conv_out3, pow_out)
                return res

        input = torch.randn(1, 3, 28, 28).to(memory_format=torch.channels_last)
        others = [
            torch.randn(1, 32, 28, 28).to(memory_format=torch.channels_last),
            torch.randn(1, 32, 28, 28).to(memory_format=torch.channels_last),
        ]
        mod_v1 = Model_v1().to(memory_format=torch.channels_last).eval()
        mod_v2 = Model_v2().to(memory_format=torch.channels_last).eval()

        if include_ops is None:
            include_ops = ["mkldnn._convolution_pointwise_.binary"]
        if exclude_ops is None:
            exclude_ops = ["mkldnn._convolution_pointwise.binary"]

        for other, mod in zip(others, [mod_v1, mod_v2]):
            self._test_code_common(mod, (input, other), include_ops, exclude_ops)

    def test_conv2d_binary_inplace_fusion_failed_cpu(
        self, include_ops=None, exclude_ops=None
    ):
        # Written buffer is graph input, we can't fuse inplace.
        class Model_v1(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels=3, out_channels=32, kernel_size=3, stride=1, padding=1
                )

            def forward(self, x, other):
                conv_out = self.conv(x)
                return torch.add(conv_out, other)

        # Written buffer is an alias tensor, we can't fuse inplace.
        class Model_v2(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels=3, out_channels=32, kernel_size=3, stride=1, padding=1
                )

            def forward(self, x, other):
                conv_out = self.conv(x)
                return torch.add(conv_out, other[1:2, :, :, :]), other

        class Model_v3(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels=3, out_channels=32, kernel_size=3, stride=1, padding=1
                )
                self.conv2 = torch.nn.Conv2d(
                    in_channels=32, out_channels=32, kernel_size=3, stride=1, padding=1
                )

            def forward(self, x, _):
                pow_out = torch.pow(self.conv(x), 2)
                other2 = F.relu(pow_out)
                conv_out2 = self.conv2(pow_out)
                res = torch.add(conv_out2, pow_out)
                res = res + other2
                return res

        # Written buffer is an ReinterpretView, we can't fuse inplace.
        class Model_v4(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv = torch.nn.Conv2d(3, 32, 3, padding=1, bias=True)
                self.linear = torch.nn.Linear(32 * 28, 32 * 28)
                self.relu = torch.nn.ReLU()

            def forward(self, x, y):
                x = self.conv(self.relu(x))
                y = self.linear(y)
                y = torch.cat((y, y + 1), 1)
                y = torch.ops.aten.permute.default(y, [0, 2, 1]).reshape(1, 32, 28, 28)
                return x + y

        class Model_v5(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv = torch.nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.relu = torch.nn.ReLU()

            def forward(self, _, x):
                x1 = self.relu(x)
                return self.conv(x1) + x1

        input = torch.randn(1, 3, 28, 28).to(memory_format=torch.channels_last)
        others = [
            torch.randn(1, 32, 28, 28).to(memory_format=torch.channels_last),
            torch.randn(2, 32, 28, 28).to(memory_format=torch.channels_last),
            torch.randn(1, 32, 28, 28).to(memory_format=torch.channels_last),
            torch.randn(1, 14, 32 * 28),
            torch.randn(1, 32, 28, 28).to(memory_format=torch.channels_last),
        ]
        mod_v1 = Model_v1().to(memory_format=torch.channels_last).eval()
        mod_v2 = Model_v2().to(memory_format=torch.channels_last).eval()
        mod_v3 = Model_v3().to(memory_format=torch.channels_last).eval()
        mod_v4 = Model_v4().to(memory_format=torch.channels_last).eval()
        mod_v5 = Model_v5().to(memory_format=torch.channels_last).eval()

        if include_ops is None:
            include_ops = ["mkldnn._convolution_pointwise.binary"]
        if exclude_ops is None:
            exclude_ops = ["mkldnn._convolution_pointwise_.binary"]

        for other, mod in zip(others, [mod_v1, mod_v2, mod_v3, mod_v4, mod_v5]):
            self._test_code_common(mod, (input, other), include_ops, exclude_ops)

    def test_conv2d_binary_fusion_failed(self):
        # we don't support alpha !=1 case or other has different size with conv's output.
        class Model(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels=3, out_channels=32, kernel_size=3, stride=1, padding=1
                )

            def forward(self, x, other, alpha):
                conv_out = self.conv(x)
                return torch.add(conv_out, other, alpha=alpha)

        # https://github.com/pytorch/pytorch/issues/100802.
        # we can't do the fusion when add's inputs are same tensor.
        class Model2(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels=3, out_channels=16, kernel_size=3, stride=1, padding=1
                )

            def forward(self, x):
                out = self.conv(x)
                out = torch.add(out, out)
                return out

        # https://github.com/pytorch/pytorch/issues/101374.
        # we can't do the fusion when add's inputs are mixed dtype.
        class Model3(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels=3, out_channels=16, kernel_size=3, stride=1, padding=1
                )

            def forward(self, x):
                temp = self.conv(x)
                other = torch.ones(temp.shape, dtype=torch.double)
                out = torch.add(temp, other)
                return out

        input = torch.randn(1, 3, 28, 28).to(memory_format=torch.channels_last)
        others = [
            torch.randn(1, 32, 28, 28).to(memory_format=torch.channels_last),
            torch.randn(32, 28, 28),
        ]
        include_ops = ["mkldnn._convolution_pointwise"]
        exclude_ops = [
            "mkldnn._convolution_pointwise.binary",
            "mkldnn._convolution_pointwise_.binary",
        ]

        # case1
        for other, alpha in zip(others, [0.1, 1.0]):
            mod = Model().to(memory_format=torch.channels_last).eval()
            self._test_code_common(mod, (input, other, alpha), include_ops, exclude_ops)
        # case2:
        mod = Model2().to(memory_format=torch.channels_last).eval()
        self._test_code_common(mod, (input,), include_ops, exclude_ops)
        # case3:
        mod = Model3().to(memory_format=torch.channels_last).eval()
        self._test_code_common(mod, (input,), include_ops, exclude_ops)

    def test_reproduce_99842_issue(self):
        class Model(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv = torch.nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=1)

            def forward(self, input_tensor):
                x = self.conv(input_tensor)
                x = F.relu(x + torch.ones(x.size()))
                return x

        input = torch.randn(1, 3, 14, 14)
        mod = Model().eval()
        include_ops = ["mkldnn._convolution_pointwise_.binary"]
        self._test_code_common(mod, (input,), include_ops, [])

    def test_reproduce_113440_issue_1(self):
        class Mod(torch.nn.Module):
            def __init__(
                self,
                add_fn,
                **kwargs,
            ):
                super().__init__()
                self.conv1 = torch.nn.Conv2d(3, 6, kernel_size=3, stride=1)
                self.conv2 = torch.nn.Conv2d(3, 6, kernel_size=3, stride=1)
                self.add_fn = add_fn
                self.relu = torch.nn.ReLU(inplace=True)
                self.conv3 = torch.nn.Conv2d(6, 6, kernel_size=3, stride=1)
                self.conv4 = torch.nn.Conv2d(6, 6, kernel_size=3, stride=1)
                self.add_fn2 = add_fn
                self.relu2 = torch.nn.ReLU(inplace=True)
                self.use_relu = True

            def forward(self, x):
                x1 = self.conv1(x)
                x2 = self.conv2(x)
                tmp = self.add_fn(x1, x2)
                if self.use_relu:
                    tmp = self.relu(tmp)
                tmp1 = self.conv3(tmp)
                tmp2 = self.conv4(tmp)
                res = self.add_fn2(tmp1, tmp2)
                if self.use_relu:
                    res = self.relu2(res)
                return res

        with torch.no_grad():
            example_inputs = (
                torch.randn((1, 3, 8, 8), dtype=torch.float32, requires_grad=False).add(
                    1
                ),
            )
            example_inputs[0].get_device()
            m = Mod(
                lambda x, y: x.add_(y),
            ).eval()
            om = torch.compile(m)
            om(*example_inputs)
            om(*example_inputs)

    def test_reproduce_113440_issue_2(self):
        class Mod(torch.nn.Module):
            def __init__(
                self,
                add_fn,
                **kwargs,
            ):
                super().__init__()
                self.conv1 = torch.nn.Conv2d(3, 6, kernel_size=3, stride=1)
                self.conv2 = torch.nn.Conv2d(3, 6, kernel_size=3, stride=1)
                self.add_fn = add_fn
                self.relu = torch.nn.ReLU(inplace=True)
                self.conv3 = torch.nn.Conv2d(6, 6, kernel_size=3, stride=1)
                self.conv4 = torch.nn.Conv2d(6, 6, kernel_size=3, stride=1)
                self.add_fn2 = add_fn
                self.relu2 = torch.nn.ReLU(inplace=True)

                self.conv5 = torch.nn.Conv2d(6, 6, kernel_size=3, stride=1)
                self.conv6 = torch.nn.Conv2d(6, 6, kernel_size=3, stride=1)
                self.conv7 = torch.nn.Conv2d(6, 6, kernel_size=1, stride=1)
                self.add_fn3 = add_fn
                self.relu3 = torch.nn.ReLU(inplace=True)

                self.use_relu = True

            def forward(self, x):
                x1 = self.conv1(x)
                x2 = self.conv2(x)
                tmp = self.add_fn(x1, x2)
                if self.use_relu:
                    tmp = self.relu(tmp)

                tmp1 = self.conv3(tmp)
                res = self.relu2(tmp1)

                return res

        with torch.no_grad():
            example_inputs = (
                torch.randn((1, 3, 8, 8), dtype=torch.float32, requires_grad=False).add(
                    1
                ),
            )
            m = Mod(
                lambda x, y: x.add_(y),
            ).eval()
            om = torch.compile(m)
            om(*example_inputs)
            om(*example_inputs)

    @torch._dynamo.config.patch("inline_inbuilt_nn_modules", True)
    def test_reproduce_121253_issue(self):
        class Mod(torch.nn.Module):
            def __init__(self, weight, bias, beta, alpha):
                super().__init__()
                self.weight = weight
                self.bias = bias
                self.beta = beta
                self.alpha = alpha

            def forward(self, x):
                return torch.addmm(
                    self.bias, x, self.weight, beta=self.beta, alpha=self.alpha
                )

        dtypes = [torch.float32]
        if torch.ops.mkldnn._is_mkldnn_bf16_supported():
            dtypes.append(torch.bfloat16)
        for dtype in dtypes:
            linear_op = (
                "mkl._mkl_linear"
                if dtype == torch.float32
                else "mkldnn._linear_pointwise"
            )
            for beta, alpha in zip([1.0, 0.1, 0.0], [1.0, 0.1, 1.0]):
                weight = torch.nn.Parameter(torch.randn(64, 64, dtype=dtype))
                bias = torch.nn.Parameter(torch.randn(64, dtype=dtype))
                mod = Mod(weight, bias, beta, alpha).to(dtype).eval()
                with torch.no_grad():
                    x = torch.randn(1, 64, dtype=dtype)
                    include_ops = []
                    exclude_ops = []
                    if (beta != 1.0 and beta != 0.0) or alpha != 1.0:
                        exclude_ops = [linear_op]
                    else:
                        include_ops = [linear_op]
                    self._test_code_common(mod, (x,), include_ops, exclude_ops)

    @skipIfNoDynamoSupport
    def test_woq_int8(self):
        class M(torch.nn.Module):
            def __init__(self, is_permute):
                super().__init__()
                self.is_permute = is_permute

            def forward(self, x, weight, scales):
                if self.is_permute:
                    weight = weight.t()
                    m = torch.mm(
                        x.reshape(-1, x.shape[-1]),
                        weight.to(x.dtype),
                    )
                    y = m * scales.to(m.dtype)
                    y = y.reshape(*x.shape[:-1], y.shape[-1])
                    return y
                else:
                    return (
                        torch.nn.functional.linear(x, weight.to(dtype=x.dtype)) * scales
                    )

        x_shape = (1, 1, 256)
        s_shape = 12
        x_strides = [
            (256, 256, 1),  # linear dispatching to mm
            (256, 32, 1),  # linear dispatching to bmm
        ]
        is_permutes = [False, True]
        for x_stride, is_permute in itertools.product(x_strides, is_permutes):
            mod = M(is_permute=is_permute).eval()
            x = torch.randn(x_shape, dtype=torch.bfloat16).as_strided(x_shape, x_stride)
            w_shape = (12, 256)
            w = torch.randint(-128, 127, w_shape, dtype=torch.int8)
            s = torch.randn(s_shape, dtype=torch.bfloat16)

            def matcher_check_fn():
                self.assertEqual(counters["inductor"]["woq_matcher_count"], 1)

            self._test_common(
                mod,
                (x, w, s),
                matcher_check_fn=matcher_check_fn,
                check_quantization=False,
                atol=0.001,
                rtol=0.07,
            )


@dynamo_config.patch({"dynamic_shapes": True, "assume_static_by_default": False})
class TestDynamicPatternMatcher(TestPatternMatcherBase):
    _test_conv_unary_cpu_base = TestPatternMatcher._test_conv_unary_cpu_base
    test_conv2d_unary_dynamic_shapes = TestPatternMatcher.test_conv2d_unary_cpu
    test_conv3d_unary_dynamic_shapes = TestPatternMatcher.test_conv3d_unary_cpu
    _test_conv_binary_base = TestPatternMatcher._test_conv_binary_base
    test_conv2d_binary_dynamic_shapes = TestPatternMatcher.test_conv2d_binary
    test_conv3d_binary_dynamic_shapes = TestPatternMatcher.test_conv3d_binary
    test_linear_unary_dynamic_shapes = TestPatternMatcher.test_linear_unary

    def test_conv_transpose2d_dynamic_shapes(self):
        # We don't support conv_transpose2d for now.
        class M(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv_transpose2d = torch.nn.ConvTranspose2d(
                    3, 16, 3, stride=2, padding=1
                )

            def forward(self, x):
                return self.conv_transpose2d(x)

        x_shape = (1, 3, 28, 28)
        mod = M().eval()
        v = torch.randn(x_shape, dtype=torch.float32)
        self._test_common(mod, (v,), 0, 0)

    def test_multi_linear_share_same_input_dynamic(self):
        # llama pattern.
        class M(torch.nn.Module):
            def __init__(
                self,
            ):
                super().__init__()
                self.w1 = torch.nn.Linear(16, 16, bias=False)
                self.w2 = torch.nn.Linear(16, 16, bias=False)

            def forward(self, x):
                return F.silu(self.w1(x)) * F.relu(self.w2(x))

        dtypes = []
        if torch.ops.mkldnn._is_mkldnn_bf16_supported():
            dtypes.append(torch.bfloat16)
        if torch.ops.mkldnn._is_mkldnn_fp16_supported():
            dtypes.append(torch.float16)
        for dtype in dtypes:
            mod = M().to(dtype).eval()
            v = torch.randn(2, 4, 16).to(dtype)
            # 1. view(match_count=4, match_nodes=4).
            # 2. mm to packed linear(match_count=2, match_nodes=2).
            # 3. view+linear+view to linear(match_count=2, match_nodes=6).
            # 4. linear to linear+swish(match_count=1, match_nodes=2).
            # 5. linear to linear+relu(match_count=1, match_nodes=5).

            match_count = 10
            match_nodes = 19
            self._test_common(mod, (v,), match_count, match_nodes, rtol=1e-2, atol=1e-2)

    def test_qconv2d_maxpool2d_linear_dynamic_cpu(self, include_ops=None):
        r"""
        This testcase will quantize a single Conv2d->Maxpool2d->Linear module
        with dynamic batch size input.
        """

        class M(torch.nn.Module):
            def __init__(
                self,
                **kwargs,
            ):
                super().__init__()
                self.conv = torch.nn.Conv2d(
                    3, 16, (2, 2), stride=(1, 1), padding=(1, 1)
                )
                self.relu = torch.nn.ReLU()
                self.maxpool2d = torch.nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
                self.avgpool = torch.nn.AdaptiveAvgPool2d((1, 1))
                self.linear = torch.nn.Linear(16, 16)

            def forward(self, x):
                temp = self.relu(self.conv(x))
                temp = self.maxpool2d(temp)
                temp = self.avgpool(temp)
                temp = torch.flatten(temp, 1)
                return self.linear(temp)

        mod = M().eval()
        v = torch.randn((2, 3, 8, 8), dtype=torch.float32, requires_grad=False).add(1)
        if include_ops is None:
            include_ops = [
                "torch.ops.onednn.qconv2d_pointwise",
                "torch.ops.quantized.max_pool2d",
                "torch.ops.onednn.qlinear_pointwise",
            ]
        exclude_ops = []
        self._test_code_common(
            mod,
            (v,),
            include_ops,
            exclude_ops,
            check_quantization=True,
            check_dynamic=True,
        )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_qat_bn_conv2d(self):
        r"""
        This testcase will quantize a single BN Conv2d module with qat flow.
        """

        class M(torch.nn.Module):
            def __init__(
                self,
            ):
                super().__init__()
                self.conv = torch.nn.Conv2d(3, 3, 3)
                self.bn1 = torch.nn.BatchNorm2d(3)
                self.bn2 = torch.nn.BatchNorm2d(3)

            def forward(self, x):
                x = self.conv(self.bn1(x))
                return self.bn2(x)

        mod = M().train()
        v = torch.randn((1, 3, 8, 8), dtype=torch.float32, requires_grad=True).add(1)

        def matcher_check_fn():
            self.assertEqual(
                counters["inductor"]["qconv2d_weight_prepack_matcher_count"], 1
            )

        self._test_common(
            mod,
            (v,),
            check_quantization=True,
            is_qat=True,
            matcher_check_fn=matcher_check_fn,
        )

    @skipIfNoDynamoSupport
    @skipIfNoONEDNN
    def test_q_attention_block(self):
        class SelfAttnLikeModule(torch.nn.Module):
            def __init__(
                self,
                input_dim,
                transpose_for_score=False,
                num_attention_heads=None,
                attention_head_size=None,
            ) -> None:
                super().__init__()
                self.input_dim = input_dim
                self.q_proj = torch.nn.Linear(input_dim, input_dim, bias=False)
                self.k_proj = torch.nn.Linear(input_dim, input_dim, bias=False)
                self.v_proj = torch.nn.Linear(input_dim, input_dim, bias=False)
                self.softmax = torch.nn.Softmax(dim=-1)
                self.transpose_for_score = transpose_for_score
                if self.transpose_for_score:
                    assert num_attention_heads is not None
                    assert attention_head_size is not None
                    self.num_attention_heads = num_attention_heads
                    self.attention_head_size = attention_head_size

            def transpose_for_scores(self, x: torch.Tensor) -> torch.Tensor:
                new_x_shape = x.size()[:-1] + (
                    self.num_attention_heads,
                    self.attention_head_size,
                )
                x = x.view(new_x_shape)
                return x.permute(0, 2, 1, 3)

            def forward(self, x):
                q = self.q_proj(x)
                k = self.k_proj(x)
                v = self.v_proj(x)
                if self.transpose_for_score:
                    q = self.transpose_for_scores(q)
                    k = self.transpose_for_scores(k)
                    v = self.transpose_for_scores(v)
                scores = torch.matmul(q, k.transpose(-1, -2)) / (self.input_dim**0.5)
                attention = self.softmax(scores)
                weighted = torch.matmul(attention, v)
                return weighted

        for annotate_matmul in [False, True]:
            mod = SelfAttnLikeModule(
                input_dim=64 * 16,
                transpose_for_score=True,
                num_attention_heads=16,
                attention_head_size=64,
            ).eval()
            v = torch.randn(2, 384, 1024)

            def matcher_check_fn():
                self.assertEqual(
                    counters["inductor"]["qlinear_weight_prepack_matcher_count"], 3
                )
                self.assertEqual(
                    counters["inductor"]["qlinear_unary_matcher_count"],
                    3 if annotate_matmul else 0,
                )

            quantizer = X86InductorQuantizer()
            quantizer.set_global(xiq.get_default_x86_inductor_quantization_config())
            if annotate_matmul:
                quantizer.set_function_type_qconfig(
                    torch.matmul, quantizer.get_global_quantization_config()
                )

            self._test_common(
                mod,
                (v,),
                check_quantization=True,
                matcher_check_fn=matcher_check_fn,
                quantizer=quantizer,
            )


if __name__ == "__main__":
    if IS_LINUX and HAS_CPU and torch.backends.mkldnn.is_available():
        run_tests()
