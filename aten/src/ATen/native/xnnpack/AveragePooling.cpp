#ifdef USE_XNNPACK

#include <ATen/native/utils/Factory.h>
#include <ATen/native/xnnpack/Common.h>
#include <ATen/native/xnnpack/Engine.h>
#include <ATen/native/xnnpack/Pooling.h>

namespace at::native::xnnpack {

bool use_global_average_pool(const Tensor& input) {
  return xnnpack::available() && (1 <= input.ndimension()) &&
      (input.device().is_cpu()) && (kFloat == input.scalar_type()) &&
      !input.requires_grad() && true;
}

Tensor global_average_pool(const Tensor& input) {
  using namespace internal;

  const Tensor input_padded_contig_nhwc =
      mobile::allocate_padded_contiguous_if_needed(
          input, MemoryFormat::ChannelsLast);

  Tensor output = mobile::empty_with_tail_padding(
      {
          input_padded_contig_nhwc.size(Layout::Activation4D::batch),
          input_padded_contig_nhwc.size(Layout::Activation4D::channels),
          1,
          1,
      },
      input_padded_contig_nhwc.options().dtype(),
      MemoryFormat::ChannelsLast,
      input_padded_contig_nhwc.opt_names());

  xnn_operator_t global_average_pooling_op{};
  const xnn_status create_status = xnn_create_global_average_pooling_nwc_f32(
      -std::numeric_limits<float>::infinity(),
      std::numeric_limits<float>::infinity(),
      0 /* flags */,
      &global_average_pooling_op);

  TORCH_CHECK(
      xnn_status_success == create_status,
      "xnn_create_global_average_pooling_nwc_f32 failed!");

  Operator global_avg_pool_scoped_op(global_average_pooling_op);

  size_t workspace_size = 0;
  size_t workspace_alignment = 0;

  const xnn_status reshape_status = xnn_reshape_global_average_pooling_nwc_f32(
      global_average_pooling_op,
      input_padded_contig_nhwc.size(Layout::Activation4D::batch), // batch_size
      input_padded_contig_nhwc.size(Layout::Activation4D::width) *
          input_padded_contig_nhwc.size(Layout::Activation4D::height), // width
      input_padded_contig_nhwc.size(Layout::Activation4D::channels), // channels
      input_padded_contig_nhwc.size(
          Layout::Activation4D::channels), // input stride
      input_padded_contig_nhwc.size(
          Layout::Activation4D::channels), // output stride
      &workspace_size, // workspace_size
      &workspace_alignment, // workspace_alignment
      caffe2::pthreadpool_());

  TORCH_CHECK(
      xnn_status_success == reshape_status,
      "xnn_reshape_global_average_pooling_nwc_f32 failed!");

  // Create Workspace pointer, which we will align and pad with 16 bytes
  size_t xnnpack_buffer_padding = 16;
  std::vector<char> workspace_vector(workspace_size + workspace_alignment + xnnpack_buffer_padding);
  void* maybe_aligned_workspace = workspace_vector.data();
  void* aligned_workspace =
      (void*)((intptr_t)maybe_aligned_workspace + workspace_alignment - (intptr_t)maybe_aligned_workspace % workspace_alignment);

  const xnn_status setup_status = xnn_setup_global_average_pooling_nwc_f32(
      global_average_pooling_op,
      aligned_workspace,
      input_padded_contig_nhwc.data_ptr<float>(),
      output.data_ptr<float>());

  TORCH_CHECK(
      xnn_status_success == setup_status,
      "xnn_setup_global_average_pooling_nwc_f32 failed!");

  const xnn_status run_status =
      xnn_run_operator(global_average_pooling_op, caffe2::pthreadpool_());

  TORCH_CHECK(
      xnn_status_success == run_status,
      "xnn_setup_global_average_pooling_nwc_f32 failed!");

  return output.to(input.suggest_memory_format());
}

} // namespace at::native::xnnpack

#endif /* USE_XNNPACK */
