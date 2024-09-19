#define TORCH_ASSERT_ONLY_METHOD_OPERATORS
#include <ATen/core/Tensor.h>
#include <ATen/Context.h>
#include <ATen/native/quantized/cpu/QuantizedOps.h>
#include <ATen/native/quantized/cpu/init_qnnpack.h>
#include <ATen/native/quantized/cpu/QnnpackUtils.h>
#include <caffe2/utils/threadpool/pthreadpool-cpp.h>

#ifndef AT_PER_OPERATOR_HEADERS
#include <ATen/Functions.h>
#include <ATen/NativeFunctions.h>
#else
#include <ATen/ops/_empty_affine_quantized.h>
#include <ATen/ops/hardsigmoid_native.h>
#endif

#include <algorithm>

namespace at {
namespace native {

DEFINE_DISPATCH(qhardsigmoid_stub);

namespace {

#ifdef USE_PYTORCH_QNNPACK
Tensor qnnpack_hardsigmoid(Tensor input) {
  TORCH_CHECK(input.ndimension() > 0, "qnnpack_hardsigmoid(): Got empty input tensor");
  TORCH_CHECK(input.scalar_type() == c10::kQUInt8,
                "qnnpack_hardsigmoid(): Expected input data type ",
                toString(c10::kQUInt8),
                " but got ",
                toString(input.scalar_type()));
  initQNNPACK();

  Tensor input_contig = input.contiguous(input.suggest_memory_format());
  size_t num_elems = input_contig.numel() / input_contig.size(0);
  const auto i_zero_point = input_contig.q_zero_point();
  const auto i_scale = input_contig.q_scale();
  constexpr float o_scale = 1.0f / 256.0f;
  constexpr int32_t o_zero_point = 0;

  pytorch_qnnp_operator_t hardsigmoid_op{nullptr};
  const pytorch_qnnp_status createStatus = pytorch_qnnp_create_hardsigmoid_nc_q8(
    num_elems, // channels
    i_zero_point,
    i_scale,
    o_zero_point,
    o_scale,
    std::numeric_limits<uint8_t>::min(), // output min
    std::numeric_limits<uint8_t>::max(), // output max
    0, // flags
    &hardsigmoid_op);

  std::unique_ptr<pytorch_qnnp_operator, QnnpackOperatorDeleter>
      qnnpack_uniq_ptr(hardsigmoid_op);

  TORCH_INTERNAL_ASSERT(createStatus == pytorch_qnnp_status_success,
                        "failed to create QNNPACK Hardsigmoid operator");
  Tensor qy = at::_empty_affine_quantized(
    input_contig.sizes(),
    at::device(kCPU).dtype(input_contig.dtype()),
    o_scale,
    o_zero_point,
    input_contig.suggest_memory_format());

  const pytorch_qnnp_status setupStatus = pytorch_qnnp_setup_hardsigmoid_nc_q8(
    hardsigmoid_op,
    input_contig.size(0), // batch size
    (uint8_t*)input_contig.data_ptr<c10::quint8>(), // input data
    num_elems, // input stride
    (uint8_t*)qy.data_ptr<c10::quint8>(), // output data
    num_elems); // output stride
  TORCH_INTERNAL_ASSERT(setupStatus == pytorch_qnnp_status_success,
                        "failed to setup QNNPACK Hardsigmoid operator");

  pthreadpool_t threadpool = caffe2::pthreadpool_();

  const pytorch_qnnp_status runStatus =
    pytorch_qnnp_run_operator(hardsigmoid_op, threadpool);

  TORCH_INTERNAL_ASSERT(
    runStatus == pytorch_qnnp_status_success,
    "failed to run QNNPACK Hardsigmoid operator");
  return qy;
}
#endif // USE_PYTORCH_QNNPACK

} // namespace
Tensor hardsigmoid_quantized_cpu(const Tensor& qx) {
#ifdef USE_PYTORCH_QNNPACK
  if (at::globalContext().qEngine() == at::QEngine::QNNPACK &&
      qx.scalar_type() == kQUInt8) {
    return qnnpack_hardsigmoid(qx);
  }
#endif  // USE_PYTORCH_QNNPACK
  Tensor qy;
  qhardsigmoid_stub(qx.device().type(), qx, qy);
  return qy;
}

Tensor& hardsigmoid_out_quantized_cpu(const Tensor& qx, Tensor& result) {
  // Note: we create a new temporary tensor because the output of hardsigmoid
  // usually has different quantization parameters from the input, and
  // quantization are currently only supported per entire tensor or per entire
  // channel of a tensor.
  Tensor qy = hardsigmoid_quantized_cpu(qx);
  result.copy_(qy);
  return result;
}

}}  // namespace at::native
