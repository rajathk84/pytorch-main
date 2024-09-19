#ifdef USE_VULKAN_API
#include <ATen/ArrayRef.h>
#include <ATen/native/vulkan/ops/Common.h>
#include <ATen/native/vulkan/ops/QuantizedFunctions.h>
#include <ATen/native/vulkan/ops/Utils.h>
#include <torch/library.h>

namespace at {
namespace native {
namespace vulkan {
namespace ops {

using namespace api::utils;

static Tensor binary_op_scalar(
    const Tensor& self_arg,
    const Scalar& other,
    const std::optional<Scalar>& alpha_arg,
    const api::ShaderInfo& shader_descriptor) {
  api::Context* const context = api::context();

  const Tensor self = self_arg.is_vulkan() ? self_arg : self_arg.vulkan();
  const vTensor& v_self = convert(self);

  vTensor v_output{
      context,
      v_self.sizes(),
      v_self.dtype(),
  };

  const float other_val = alpha_arg ? other.to<float>() * alpha_arg->to<float>()
                                    : other.to<float>();
  const struct Block final {
    uvec3 extents;
    int fill0;
    float other;
  } block{
      v_self.extents(),
      0,
      other_val,
  };

  api::UniformParamsBuffer params(context, block);
  api::PipelineBarrier pipeline_barrier{};

  context->submit_compute_job(
      // shader descriptor
      shader_descriptor,
      // pipeline barrier
      pipeline_barrier,
      // global work group size
      v_output.extents(),
      // local work group size
      adaptive_work_group_size(v_output.extents()),
      // fence handle
      VK_NULL_HANDLE,
      // shader arguments
      v_output.image(
          pipeline_barrier,
          api::PipelineStage::COMPUTE,
          api::MemoryAccessType::WRITE),
      v_self.image(pipeline_barrier, api::PipelineStage::COMPUTE),
      // params buffer
      params.buffer());

  return convert(v_output);
}

static Tensor binary_op_preprocess_other_arg(const Tensor& other_arg) {
  // Similar to binary_op_scalar where tensors is mapped to float, we
  // also map known integer types (but not quant types) tensor to float.

  // Such conversion can only to be done before moving to vulkan, since vulkan
  // doesn't yet support integer types.
  Tensor other = other_arg;
  if (!other.is_vulkan()) {
    switch (other.scalar_type()) {
      case at::kByte:
      case at::kChar:
      case at::kShort:
      case at::kInt:
      case at::kLong:
      case at::kDouble:
        other = other.to(kFloat);
        break;
      case at::kFloat:
        // No op for expected type.
        break;
      default:
        TORCH_CHECK(
            false,
            "binary_op_tensor, doesn't support type %s",
            other.scalar_type());
        break;
    }
    other = other.vulkan();
  }

  return other;
}

static Tensor& binary_op_scalar_(
    Tensor& self_arg,
    const Scalar& other,
    const std::optional<Scalar>& alpha_arg,
    const api::ShaderInfo& shader_descriptor) {
  TORCH_CHECK(
      self_arg.is_vulkan(),
      "Vulkan: In-place operator is only supported on Vulkan tensors.");

  api::Context* const context = api::context();

  vTensor& v_self = convert(self_arg);

  const float other_val = alpha_arg ? other.to<float>() * alpha_arg->to<float>()
                                    : other.to<float>();
  const struct Block final {
    uvec3 extents;
    int fill0;
    float other;
  } block{
      v_self.extents(),
      0,
      other_val,
  };

  api::UniformParamsBuffer params(context, block);
  api::PipelineBarrier pipeline_barrier{};

  context->submit_compute_job(
      // shader descriptor
      shader_descriptor,
      // pipeline barrier
      pipeline_barrier,
      // global work group size
      v_self.extents(),
      // local work group size
      adaptive_work_group_size(v_self.extents()),
      // fence handle
      VK_NULL_HANDLE,
      // shader arguments
      v_self.image(
          pipeline_barrier,
          api::PipelineStage::COMPUTE,
          api::MemoryAccessType::READ | api::MemoryAccessType::WRITE),
      // params buffer
      params.buffer());

  return self_arg;
}

static Tensor binary_op_tensor(
    const Tensor& self_arg,
    const Tensor& other_arg,
    const std::optional<Scalar>& alpha_arg,
    const api::ShaderInfo& shader_descriptor) {
  utils::is_broadcastable(self_arg, other_arg);
  api::Context* const context = api::context();

  const Tensor self = self_arg.is_vulkan() ? self_arg : self_arg.vulkan();
  const vTensor& v_self = convert(self);

  Tensor other = binary_op_preprocess_other_arg(other_arg);

  const vTensor& v_other = convert(other);

  vTensor v_output{
      context,
      utils::broadcast_size(self_arg, other_arg),
      v_self.dtype(),
  };

  const double alpha = alpha_arg ? alpha_arg->to<double>() : 1.0;
  const struct Block final {
    uvec4 output_tensor_size;
    uvec4 input_tensor_size;
    uvec4 other_tensor_size;
    float alpha;
  } block{
      {get_dim<Dim4D::Width>(v_output),
       get_dim<Dim4D::Height>(v_output),
       get_dim<Dim4D::Channel>(v_output),
       get_dim<Dim4D::Batch>(v_output)},

      {get_dim<Dim4D::Width>(v_self),
       get_dim<Dim4D::Height>(v_self),
       get_dim<Dim4D::Channel>(v_self),
       get_dim<Dim4D::Batch>(v_self)},

      {get_dim<Dim4D::Width>(v_other),
       get_dim<Dim4D::Height>(v_other),
       get_dim<Dim4D::Channel>(v_other),
       get_dim<Dim4D::Batch>(v_other)},
      // alpha
      safe_downcast<float>(alpha),
  };

  api::UniformParamsBuffer params(context, block);
  api::PipelineBarrier pipeline_barrier{};

  context->submit_compute_job(
      // shader descriptor
      shader_descriptor,
      // pipeline barrier
      pipeline_barrier,
      // global work group size
      v_output.extents(),
      // local work group size
      adaptive_work_group_size(v_output.extents()),
      // fence handle
      VK_NULL_HANDLE,
      // shader arguments
      v_output.image(
          pipeline_barrier,
          api::PipelineStage::COMPUTE,
          api::MemoryAccessType::WRITE),
      v_self.image(pipeline_barrier, api::PipelineStage::COMPUTE),
      v_other.image(pipeline_barrier, api::PipelineStage::COMPUTE),
      // params buffer
      params.buffer());

  return convert(v_output);
}

static Tensor quantized_binary_op_tensor(
    const Tensor& self_arg,
    const Tensor& other_arg,
    const double scale,
    const int64_t zero_point,
    const api::ShaderInfo& shader_descriptor) {
  utils::is_broadcastable(self_arg, other_arg);
  api::Context* const context = api::context();

  const Tensor self = self_arg.is_vulkan() ? self_arg : self_arg.vulkan();
  const vTensor& v_self = convert(self);
  const Tensor other = other_arg.is_vulkan() ? other_arg : other_arg.vulkan();
  const vTensor& v_other = convert(other);

  TORCH_CHECK(v_self.is_quantized(), "Input tensor is not quantized");
  TORCH_CHECK(v_other.is_quantized(), "Input tensor is not quantized");

  vTensor v_output{
      context,
      utils::broadcast_size(self_arg, other_arg),
      scale,
      zero_point,
      api::kQUInt8,
  };

  const double scale1 = v_self.get_scale();
  const double scale2 = v_other.get_scale();
  const int64_t zero_point1 = v_self.get_zero_point();
  const int64_t zero_point2 = v_other.get_zero_point();
  const struct Block final {
    uvec3 extents;
    uint32_t channelSize;
    uvec3 input1Extents;
    uint32_t channelBatchSize1;
    uvec3 input2Extents;
    uint32_t channelBatchSize2;
    float scale1;
    float scale2;
    int32_t zeroPoint1;
    int32_t zeroPoint2;
    float scale;
    float fill1;
    int32_t zeroPoint;
    int32_t fill2;
  } block{
      v_output.extents(),
      get_dim<Dim4D::Channel>(v_output),
      v_self.extents(),
      get_dim<Dim4D::Channel>(self) * get_dim<Dim4D::Batch>(self),
      v_other.extents(),
      get_dim<Dim4D::Channel>(other) * get_dim<Dim4D::Batch>(other),
      safe_downcast<float>(scale1),
      safe_downcast<float>(scale2),
      safe_downcast<int32_t>(zero_point1),
      safe_downcast<int32_t>(zero_point2),
      safe_downcast<float>(scale),
      0.0f,
      safe_downcast<int32_t>(zero_point),
      0u,
  };

  api::UniformParamsBuffer params(context, block);
  api::PipelineBarrier pipeline_barrier{};

  context->submit_compute_job(
      // shader descriptor
      shader_descriptor,
      // pipeline barrier
      pipeline_barrier,
      // global work group size
      v_output.extents(),
      // local work group size
      adaptive_work_group_size(v_output.extents()),
      // fence handle
      VK_NULL_HANDLE,
      // shader arguments
      v_output.image(
          pipeline_barrier,
          api::PipelineStage::COMPUTE,
          api::MemoryAccessType::WRITE),
      v_self.image(pipeline_barrier, api::PipelineStage::COMPUTE),
      v_other.image(pipeline_barrier, api::PipelineStage::COMPUTE),
      // params buffer
      params.buffer());

  return convert_quantized(v_output);
}

static Tensor& binary_op_tensor_(
    Tensor& self_arg,
    const Tensor& other_arg,
    const std::optional<Scalar>& alpha_arg,
    const api::ShaderInfo& shader_descriptor) {
  TORCH_CHECK(
      get_dim<Dim4D::Batch>(self_arg) >= get_dim<Dim4D::Batch>(other_arg) &&
          get_dim<Dim4D::Channel>(self_arg) >=
              get_dim<Dim4D::Channel>(other_arg) &&
          get_dim<Dim4D::Height>(self_arg) >=
              get_dim<Dim4D::Height>(other_arg) &&
          get_dim<Dim4D::Width>(self_arg) >= get_dim<Dim4D::Width>(other_arg),
      "Dimensions of input tensor to Vulkan in-place binary elementwise op "
      "must be less than or equal the dimensions of the underlying tensor.");

  utils::is_broadcastable(self_arg, other_arg);

  TORCH_CHECK(
      self_arg.is_vulkan(),
      "Vulkan: In-place operator is only supported on Vulkan tensors.");

  api::Context* const context = api::context();

  vTensor& v_self = convert(self_arg);

  Tensor other = binary_op_preprocess_other_arg(other_arg);

  const vTensor& v_other = convert(other);

  const double alpha = alpha_arg ? alpha_arg->to<double>() : 1.0;
  const struct Block final {
    uvec4 input_tensor_size;
    uvec4 other_tensor_size;
    float alpha;
  } block{
      {get_dim<Dim4D::Width>(v_self),
       get_dim<Dim4D::Height>(v_self),
       get_dim<Dim4D::Channel>(v_self),
       get_dim<Dim4D::Batch>(v_self)},

      {get_dim<Dim4D::Width>(v_other),
       get_dim<Dim4D::Height>(v_other),
       get_dim<Dim4D::Channel>(v_other),
       get_dim<Dim4D::Batch>(v_other)},
      // alpha
      safe_downcast<float>(alpha),
  };

  api::UniformParamsBuffer params(context, block);
  api::PipelineBarrier pipeline_barrier{};

  context->submit_compute_job(
      // shader descriptor
      shader_descriptor,
      // pipeline barrier
      pipeline_barrier,
      // global work group size
      v_self.extents(),
      // local work group size
      adaptive_work_group_size(v_self.extents()),
      // fence handle
      VK_NULL_HANDLE,
      // shader arguments
      v_self.image(
          pipeline_barrier,
          api::PipelineStage::COMPUTE,
          api::MemoryAccessType::READ | api::MemoryAccessType::WRITE),
      v_other.image(pipeline_barrier, api::PipelineStage::COMPUTE),
      // params buffer
      params.buffer());

  return self_arg;
}

static Tensor add_scalar(
    const Tensor& self_arg,
    const Scalar& other,
    const Scalar& alpha) {
  return binary_op_scalar(
      self_arg, other, std::optional<Scalar>(alpha), VK_KERNEL(add_scalar));
}

static Tensor& add_scalar_(
    Tensor& self,
    const Scalar& other,
    const Scalar& alpha) {
  return binary_op_scalar_(
      self, other, std::optional<Scalar>(alpha), VK_KERNEL(add_scalar_inplace));
}

Tensor quantized_add(
    const Tensor& self_arg,
    const Tensor& other_arg,
    const double scale,
    const int64_t zero_point) {
  return quantized_binary_op_tensor(
      self_arg, other_arg, scale, zero_point, VK_KERNEL(quantized_add));
}

Tensor quantized_sub(
    const Tensor& self_arg,
    const Tensor& other_arg,
    const double scale,
    const int64_t zero_point) {
  return quantized_binary_op_tensor(
      self_arg, other_arg, scale, zero_point, VK_KERNEL(quantized_sub));
}

Tensor quantized_mul(
    const Tensor& self_arg,
    const Tensor& other_arg,
    const double scale,
    const int64_t zero_point) {
  return quantized_binary_op_tensor(
      self_arg, other_arg, scale, zero_point, VK_KERNEL(quantized_mul));
}

Tensor quantized_div(
    const Tensor& self_arg,
    const Tensor& other_arg,
    const double scale,
    const int64_t zero_point) {
  return quantized_binary_op_tensor(
      self_arg, other_arg, scale, zero_point, VK_KERNEL(quantized_div));
}

static Tensor add_tensor(
    const Tensor& self_arg,
    const Tensor& other_arg,
    const Scalar& alpha) {
  return binary_op_tensor(
      self_arg, other_arg, std::optional<Scalar>(alpha), VK_KERNEL(add));
}

static Tensor& add_tensor_(
    Tensor& self,
    const Tensor& other_arg,
    const Scalar& alpha) {
  return binary_op_tensor_(
      self, other_arg, std::optional<Scalar>(alpha), VK_KERNEL(add_inplace));
}

static Tensor sub_scalar(
    const Tensor& self_arg,
    const Scalar& other,
    const Scalar& alpha) {
  return binary_op_scalar(
      self_arg,
      other,
      std::optional<Scalar>(-1 * alpha.to<float>()),
      VK_KERNEL(add_scalar));
}

static Tensor& sub_scalar_(
    Tensor& self,
    const Scalar& other,
    const Scalar& alpha) {
  return binary_op_scalar_(
      self,
      other,
      std::optional<Scalar>(-1 * alpha.to<float>()),
      VK_KERNEL(add_scalar_inplace));
}

static Tensor sub_tensor(
    const Tensor& self_arg,
    const Tensor& other_arg,
    const Scalar& alpha) {
  return binary_op_tensor(
      self_arg, other_arg, std::optional<Scalar>(alpha), VK_KERNEL(sub));
}

static Tensor& sub_tensor_(
    Tensor& self,
    const Tensor& other_arg,
    const Scalar& alpha) {
  return binary_op_tensor_(
      self, other_arg, std::optional<Scalar>(alpha), VK_KERNEL(sub_inplace));
}

static Tensor mul_scalar(const Tensor& self_arg, const Scalar& other) {
  return binary_op_scalar(
      self_arg, other, std::optional<Scalar>(), VK_KERNEL(mul_scalar));
}

static Tensor& mul_scalar_(Tensor& self, const Scalar& other) {
  return binary_op_scalar_(
      self, other, std::optional<Scalar>(), VK_KERNEL(mul_scalar_inplace));
}

static Tensor mul_tensor(const Tensor& self_arg, const Tensor& other_arg) {
  return binary_op_tensor(
      self_arg, other_arg, std::optional<Scalar>(), VK_KERNEL(mul));
}

static Tensor& mul_tensor_(Tensor& self, const Tensor& other_arg) {
  return binary_op_tensor_(
      self, other_arg, std::optional<Scalar>(), VK_KERNEL(mul_inplace));
}

static Tensor div_scalar(const Tensor& self_arg, const Scalar& other) {
  return binary_op_scalar(
      self_arg,
      1.0 / other.to<float>(),
      std::optional<Scalar>(),
      VK_KERNEL(mul_scalar));
}

static Tensor& div_scalar_(Tensor& self, const Scalar& other) {
  return binary_op_scalar_(
      self,
      1.0 / other.to<float>(),
      std::optional<Scalar>(),
      VK_KERNEL(mul_scalar_inplace));
}

static Tensor div_tensor(const Tensor& self_arg, const Tensor& other_arg) {
  return binary_op_tensor(
      self_arg, other_arg, std::optional<Scalar>(), VK_KERNEL(div));
}

static Tensor& div_tensor_(Tensor& self, const Tensor& other_arg) {
  return binary_op_tensor_(
      self, other_arg, std::optional<Scalar>(), VK_KERNEL(div_inplace));
}

static Tensor pow(const Tensor& self, const Tensor& other) {
  return binary_op_tensor(self, other, std::optional<Scalar>(), VK_KERNEL(pow));
}

static Tensor& pow_(Tensor& self, const Tensor& other) {
  return binary_op_tensor_(
      self, other, std::optional<Scalar>(), VK_KERNEL(pow_inplace));
}

static Tensor pow_tensor_scalar(const Tensor& self, const Scalar& other) {
  return binary_op_scalar(
      self, other, std::optional<Scalar>(), VK_KERNEL(pow_tensor_scalar));
}

static Tensor& pow_tensor_scalar_(Tensor& self, const Scalar& other) {
  return binary_op_scalar_(
      self,
      other,
      std::optional<Scalar>(),
      VK_KERNEL(pow_tensor_scalar_inplace));
}

static Tensor pow_scalar_tensor(const Scalar& self, const Tensor& other) {
  return binary_op_scalar(
      other, self, std::optional<Scalar>(), VK_KERNEL(pow_scalar_tensor));
}

static Tensor floor_divide_scalar(const Tensor& self, const Scalar& other) {
  TORCH_CHECK(
      other.to<float>() != 0.0f, "floor_divide_scalar: can't divide by zero");
  return binary_op_scalar(
      self,
      1.0 / other.to<float>(),
      std::optional<Scalar>(),
      VK_KERNEL(floor_mul_scalar));
}

static Tensor& floor_divide_scalar_(Tensor& self, const Scalar& other) {
  TORCH_CHECK(
      other.to<float>() != 0.0f, "floor_divide_scalar_: can't divide by zero");
  return binary_op_scalar_(
      self,
      1.0 / other.to<float>(),
      std::optional<Scalar>(),
      VK_KERNEL(floor_mul_scalar_inplace));
}

static Tensor floor_divide_tensor(const Tensor& self, const Tensor& other) {
  return binary_op_tensor(
      self, other, std::optional<Scalar>(), VK_KERNEL(floor_divide));
}

static Tensor& floor_divide_tensor_(Tensor& self, const Tensor& other_arg) {
  return binary_op_tensor_(
      self,
      other_arg,
      std::optional<Scalar>(),
      VK_KERNEL(floor_divide_inplace));
}

TORCH_LIBRARY_IMPL(aten, Vulkan, m) {
  m.impl(TORCH_SELECTIVE_NAME("aten::add.Scalar"), TORCH_FN(add_scalar));
  m.impl(TORCH_SELECTIVE_NAME("aten::add_.Scalar"), TORCH_FN(add_scalar_));
  m.impl(TORCH_SELECTIVE_NAME("aten::add.Tensor"), TORCH_FN(add_tensor));
  m.impl(TORCH_SELECTIVE_NAME("aten::add_.Tensor"), TORCH_FN(add_tensor_));
  m.impl(TORCH_SELECTIVE_NAME("aten::sub.Scalar"), TORCH_FN(sub_scalar));
  m.impl(TORCH_SELECTIVE_NAME("aten::sub_.Scalar"), TORCH_FN(sub_scalar_));
  m.impl(TORCH_SELECTIVE_NAME("aten::sub.Tensor"), TORCH_FN(sub_tensor));
  m.impl(TORCH_SELECTIVE_NAME("aten::sub_.Tensor"), TORCH_FN(sub_tensor_));
  m.impl(TORCH_SELECTIVE_NAME("aten::mul.Scalar"), TORCH_FN(mul_scalar));
  m.impl(TORCH_SELECTIVE_NAME("aten::mul_.Scalar"), TORCH_FN(mul_scalar_));
  m.impl(TORCH_SELECTIVE_NAME("aten::mul.Tensor"), TORCH_FN(mul_tensor));
  m.impl(TORCH_SELECTIVE_NAME("aten::mul_.Tensor"), TORCH_FN(mul_tensor_));
  m.impl(TORCH_SELECTIVE_NAME("aten::div.Scalar"), TORCH_FN(div_scalar));
  m.impl(TORCH_SELECTIVE_NAME("aten::div_.Scalar"), TORCH_FN(div_scalar_));
  m.impl(TORCH_SELECTIVE_NAME("aten::div.Tensor"), TORCH_FN(div_tensor));
  m.impl(TORCH_SELECTIVE_NAME("aten::div_.Tensor"), TORCH_FN(div_tensor_));
  m.impl(TORCH_SELECTIVE_NAME("aten::pow.Tensor_Tensor"), TORCH_FN(pow));
  m.impl(TORCH_SELECTIVE_NAME("aten::pow_.Tensor"), TORCH_FN(pow_));
  m.impl(
      TORCH_SELECTIVE_NAME("aten::pow.Tensor_Scalar"),
      TORCH_FN(pow_tensor_scalar));
  m.impl(
      TORCH_SELECTIVE_NAME("aten::pow_.Scalar"), TORCH_FN(pow_tensor_scalar_));
  m.impl(TORCH_SELECTIVE_NAME("aten::pow.Scalar"), TORCH_FN(pow_scalar_tensor));
  m.impl(
      TORCH_SELECTIVE_NAME("aten::floor_divide.Scalar"),
      TORCH_FN(floor_divide_scalar));
  m.impl(
      TORCH_SELECTIVE_NAME("aten::floor_divide_.Scalar"),
      TORCH_FN(floor_divide_scalar_));
  m.impl(
      TORCH_SELECTIVE_NAME("aten::floor_divide"),
      TORCH_FN(floor_divide_tensor));
  m.impl(
      TORCH_SELECTIVE_NAME("aten::floor_divide_.Tensor"),
      TORCH_FN(floor_divide_tensor_));
}

} // namespace ops
} // namespace vulkan
} // namespace native
} // namespace at
#endif /* USE_VULKAN_API */
