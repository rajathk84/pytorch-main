#include <ATen/native/vulkan/ops/Common.h>
#include <ATen/native/vulkan/ops/Utils.h>
#include <torch/library.h>

namespace at {
namespace native {
namespace vulkan {
namespace ops {
namespace {

using namespace api::utils;

void set_cumsum_kernel_params(
    const long long num_dims,
    const long long dim,
    const IntArrayRef v_input_sizes,
    api::ShaderInfo& shader_descriptor,
    api::utils::ivec4& input_shader_extents,
    api::utils::ivec4& early_exit,
    api::utils::ivec4& input_dim_stride,
    api::utils::ivec4& input_tensor_dims) {
  if (num_dims == 1) {
    early_exit.data[0u] = 1;
    input_dim_stride.data[0u] = 1;
    shader_descriptor = VK_KERNEL(cumsum_batch_height_width);
  } else if (num_dims == 2) {
    // for height, width dim case, we can reuse a single shader
    // with vectorized parameters
    shader_descriptor = VK_KERNEL(cumsum_batch_height_width);
    if (dim == 0) {
      early_exit.data[1u] = 1;
      input_dim_stride.data[1u] = 1;
    } else { // dim == 1
      early_exit.data[0u] = 1;
      input_dim_stride.data[0u] = 1;
    }
  } else if (num_dims == 3) {
    for (uint32_t i = 0; i < num_dims; i++) {
      input_tensor_dims.data[i + 1] = safe_downcast<int32_t>(v_input_sizes[i]);
    }
    if (dim == 0) {
      early_exit.data[2u] = 1;
      input_dim_stride.data[2u] = 1;
      shader_descriptor = VK_KERNEL(cumsum_channel);
    } else if (dim == 1) {
      // for height, width dim case, we can reuse a single shader
      // with vectorized parameters
      early_exit.data[1u] = 1;
      input_dim_stride.data[1u] = 1;
      shader_descriptor = VK_KERNEL(cumsum_batch_height_width);
    } else { // dim == 2
      early_exit.data[0u] = 1;
      input_dim_stride.data[0u] = 1;
      shader_descriptor = VK_KERNEL(cumsum_batch_height_width);
    }
  } else {
    // assume num_dims is 4
    for (uint32_t i = 0; i < num_dims; i++) {
      input_tensor_dims.data[i] = safe_downcast<int32_t>(v_input_sizes[i]);
    }
    if (dim == 1) {
      // for 4-rank Tensor, scan along channel dim case, the memory layout
      // forces a different shader algorithm than other dims
      input_shader_extents.data[2u] =
          v_input_sizes[Layout::Activation4D::batch];
      shader_descriptor = VK_KERNEL(cumsum_channel);
    } else {
      // for batch, height, width dim case, we can reuse a single shader
      // with vectorized parameters
      if (dim == 0) {
        early_exit.data[2u] = safe_downcast<int32_t>(
            std::ceil(v_input_sizes[Layout::Activation4D::channels] / 4.0));
        input_dim_stride.data[2u] = safe_downcast<int32_t>(
            std::ceil(v_input_sizes[Layout::Activation4D::channels] / 4.0));
      } else if (dim == 2) {
        early_exit.data[1u] = 1;
        input_dim_stride.data[1u] = 1;
      } else { // dim == 3
        early_exit.data[0u] = 1;
        input_dim_stride.data[0u] = 1;
      }
      shader_descriptor = VK_KERNEL(cumsum_batch_height_width);
    }
  }
}

Tensor cumsum(
    const at::Tensor& input_arg,
    const int64_t dim_arg,
    const std::optional<ScalarType> dtype) {
  TORCH_CHECK(
      input_arg.dim() >= 1 && input_arg.dim() <= 4,
      "Vulkan cumsum expects 1 <= input dimension <= 4, Tensor input dimensions ",
      input_arg.dim());

  TORCH_CHECK(
      dim_arg < input_arg.dim(),
      "cumsum dim input was ",
      dim_arg,
      " out of range for Tensor input with dimensions ",
      input_arg.dim());

  int64_t dim = utils::normalize(dim_arg, input_arg.dim());

  api::Context* const context = api::context();

  const Tensor input = input_arg.is_vulkan() ? input_arg : input_arg.vulkan();
  const vTensor& v_input = convert(input);
  const IntArrayRef v_input_sizes = v_input.sizes();

  vTensor v_output{
      context,
      v_input.sizes(),
      v_input.dtype(),
  };

  const api::utils::uvec3 global_workgroup_extents = v_output.extents();
  api::utils::ivec4 input_shader_extents = {
      safe_downcast<int32_t>(v_input.extents().data[0u]),
      safe_downcast<int32_t>(v_input.extents().data[1u]),
      safe_downcast<int32_t>(v_input.extents().data[2u]),
      0 // zero pad
  };
  // early_exit is the global workgroup position-based condition for
  // unnecessary invocations to exit.
  api::utils::ivec4 early_exit = {
      safe_downcast<int32_t>(v_input.extents().data[0u]),
      safe_downcast<int32_t>(v_input.extents().data[1u]),
      safe_downcast<int32_t>(v_input.extents().data[2u]),
      0 // zero pad
  };
  // for batch/height/width, they share the same shader
  // vectorized by input_dim_stride for each dimension case
  api::utils::ivec4 input_dim_stride = {
      0,
      0,
      0,
      0, // zero pad
  };
  api::utils::ivec4 input_tensor_dims = {
      0,
      0,
      0,
      0,
  };
  api::ShaderInfo shader_descriptor;
  set_cumsum_kernel_params(
      input_arg.dim(),
      dim,
      v_input_sizes,
      shader_descriptor,
      input_shader_extents,
      early_exit,
      input_dim_stride,
      input_tensor_dims);

  const struct Block final {
    ivec4 input_shader_extents;
    ivec4 input_tensor_dims;
    ivec4 input_dim_stride;
    ivec4 early_exit;
  } block{
      input_shader_extents, input_tensor_dims, input_dim_stride, early_exit};

  api::UniformParamsBuffer params(context, block);
  api::PipelineBarrier pipeline_barrier{};

  context->submit_compute_job(
      // shader descriptor
      shader_descriptor,
      // pipeline barrier
      pipeline_barrier,
      // global work group size
      global_workgroup_extents,
      // local work group size
      adaptive_work_group_size(global_workgroup_extents),
      // fence handle
      VK_NULL_HANDLE,
      // shader arguments
      v_output.image(
          pipeline_barrier,
          api::PipelineStage::COMPUTE,
          api::MemoryAccessType::WRITE),
      v_input.image(pipeline_barrier, api::PipelineStage::COMPUTE),
      // params buffer
      params.buffer());

  return convert(v_output);
}

#ifdef USE_VULKAN_API

TORCH_LIBRARY_IMPL(aten, Vulkan, m) {
  m.impl(TORCH_SELECTIVE_NAME("aten::cumsum"), TORCH_FN(cumsum));
}

#endif /* USE_VULKAN_API */

} // namespace
} // namespace ops
} // namespace vulkan
} // namespace native
} // namespace at
