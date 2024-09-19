#define TORCH_ASSERT_ONLY_METHOD_OPERATORS
#include <ATen/Dispatch.h>
#include <ATen/mps/MPSProfiler.h>
#include <ATen/native/Histogram.h>
#include <ATen/native/mps/OperationUtils.h>

#ifndef AT_PER_OPERATOR_HEADERS
#include <ATen/Functions.h>
#include <ATen/NativeFunctions.h>
#else
#include <ATen/ops/aminmax.h>
#include <ATen/ops/sum.h>
#endif

namespace at::native {
namespace mps {

enum BIN_SELECTION_ALGORITHM {
  LINEAR_INTERPOLATION,
  LINEAR_INTERPOLATION_WITH_LOCAL_SEARCH,
  BINARY_SEARCH,
};

static MetalShaderLibrary lib(R"HISTOGRAM_METAL(

#include <metal_stdlib>
using namespace metal;

enum BIN_SELECTION_ALGORITHM {
  LINEAR_INTERPOLATION,
  LINEAR_INTERPOLATION_WITH_LOCAL_SEARCH,
  BINARY_SEARCH,
};

// Re-implementation of std::upper_bound with some modifications.
template<typename T, typename U>
U upper_bound(constant T * arr, U first, U len, T val) {
  while (len > 0) {
    U half_ = len >> 1;
    U middle = first + half_;

    if (val < arr[middle]) {
      len = half_;
    } else {
      first = middle + 1;
      len -= half_ + 1;
    }
  }
  return first;
}

// The implementation here is mostly taken from the CPU's implementation with some modifications.
// Please see `aten/src/ATen/native/cpu/HistogramKernel.cpp` for more details.
template<typename T>
kernel void histogramdd(constant T  * input_            [[buffer(0)]],
                  constant T        * weight            [[buffer(1)]],
                  device   T        * local_out         [[buffer(2)]],
                  constant uint     * offsets           [[buffer(3)]],
                  constant size_t   & num_dims          [[buffer(4)]],
                  constant T        * bin_seq           [[buffer(5)]],
                  constant int64_t  * num_bin_edges     [[buffer(6)]],
                  constant T        * leftmost_edge     [[buffer(7)]],
                  constant T        * rightmost_edge    [[buffer(8)]],
                  constant int64_t  * local_out_strides [[buffer(9)]],
                  constant uint8_t  & algorithm         [[buffer(10)]],
                  constant uint8_t  & has_weight        [[buffer(11)]],
                  uint tid [[thread_position_in_grid]]) {

  constexpr T eps = 4e-6;
  bool skip_element = false;
  int64_t hist_index = 0;
  int64_t bin_seq_offset = 0;

  for (size_t dim = 0; dim < num_dims; dim++) {
    T element = input_[offsets[tid * num_dims + dim]];

    // Skips elements which fall outside the specified bins and NaN elements
    // Adding an eps to the edges to eliminate precision issues that cause elements accidentally skipped,
    // this is likely due to the minuscule implementation differences between the CPU and MPS's linspace.
    if (!(element >= (leftmost_edge[dim] - eps) && element <= (rightmost_edge[dim] + eps))) {
        skip_element = true;
        break;
    }
    int64_t pos = -1;

    if (algorithm == BIN_SELECTION_ALGORITHM::BINARY_SEARCH) {
      pos = upper_bound(
        bin_seq,
        bin_seq_offset,
        num_bin_edges[dim],
        element
      ) - bin_seq_offset - 1;
    } else if (
      algorithm == BIN_SELECTION_ALGORITHM::LINEAR_INTERPOLATION ||
      algorithm == BIN_SELECTION_ALGORITHM::LINEAR_INTERPOLATION_WITH_LOCAL_SEARCH) {
      pos = static_cast<int64_t>((element - leftmost_edge[dim])
                            * (num_bin_edges[dim] - 1)
                            / (rightmost_edge[dim] - leftmost_edge[dim]));
      if (algorithm == LINEAR_INTERPOLATION_WITH_LOCAL_SEARCH) {
          int64_t pos_min = max(static_cast<int64_t>(0), pos - 1);
          int64_t pos_max = min(pos + 2, num_bin_edges[dim]);
          pos = upper_bound(
            bin_seq,
            bin_seq_offset + pos_min,
            pos_max - pos_min,
            element
          ) - bin_seq_offset - 1;
      }
    }

    if (pos == (num_bin_edges[dim] - 1)) {
      pos -= 1;
    }
    hist_index += local_out_strides[dim + 1] * pos;
    bin_seq_offset += num_bin_edges[dim];
  }
  if (!skip_element) {
    // In the unweighted case, the default weight is 1
    local_out[local_out_strides[0] * tid + hist_index] += has_weight ? weight[tid] : 1;
  }
}


#define REGISTER_HISTOGRAMDD_OP(DTYPE)                        \
template                                                      \
[[host_name("histogramdd_" #DTYPE)]]                          \
kernel void histogramdd<DTYPE>(                               \
  constant DTYPE    * input_                  [[buffer(0)]],  \
  constant DTYPE    * weight                  [[buffer(1)]],  \
  device   DTYPE    * local_out               [[buffer(2)]],  \
  constant uint     * offsets                 [[buffer(3)]],  \
  constant size_t   & num_dims                [[buffer(4)]],  \
  constant DTYPE    * bin_seq                 [[buffer(5)]],  \
  constant int64_t  * num_bin_edges           [[buffer(6)]],  \
  constant DTYPE    * leftmost_edge           [[buffer(7)]],  \
  constant DTYPE    * rightmost_edge          [[buffer(8)]],  \
  constant int64_t  * local_out_strides       [[buffer(9)]],  \
  constant uint8_t  & bin_selection_algorithm [[buffer(10)]], \
  constant uint8_t  & has_weight              [[buffer(11)]], \
  uint tid [[thread_position_in_grid]]);

REGISTER_HISTOGRAMDD_OP(float);
REGISTER_HISTOGRAMDD_OP(half);

kernel void kernel_index_offset(constant uint         * strides         [[buffer(0)]],
                                device uint           * data_offsets    [[buffer(1)]],
                                constant uint         * iter_shape      [[buffer(2)]],
                                constant uint         & num_dimensions  [[buffer(3)]],
                                uint thread_index [[thread_position_in_grid]]) {
    data_offsets[thread_index] = 0;
    uint32_t idx = thread_index;
    for (uint32_t dim = 0; dim < num_dimensions; dim++) {
        uint32_t reversed_dim = num_dimensions - dim -1;
        uint32_t remainder = idx % iter_shape[reversed_dim];
        idx /= iter_shape[reversed_dim];

        data_offsets[thread_index] += remainder * strides[reversed_dim];
    }
}
)HISTOGRAM_METAL");

template <typename input_t, BIN_SELECTION_ALGORITHM algorithm>
void histogramdd_kernel_impl(Tensor& hist_output,
                             const TensorList& bin_edges,
                             const Tensor& input,
                             const std::optional<Tensor>& weight) {
  TORCH_CHECK(input.dtype() != at::kDouble, "float64 is not supported on MPS");
  TORCH_INTERNAL_ASSERT(input.dim() == 2);

  constexpr uint8_t bin_selection_algorithm = algorithm;
  const int64_t N = input.size(0);
  const bool has_weight = weight.has_value();

  if (has_weight) {
    TORCH_CHECK(weight.value().is_contiguous(), "histogramdd(): weight should be contiguous on MPS");
    TORCH_INTERNAL_ASSERT(weight.value().dim() == 1 && weight.value().numel() == N);
    TORCH_INTERNAL_ASSERT(weight.value().scalar_type() == input.scalar_type());
  }

  const int64_t D = input.size(1);
  size_t bin_edges_numel = 0;
  TORCH_INTERNAL_ASSERT(int64_t(bin_edges.size()) == D);
  for (const auto dim : c10::irange(D)) {
    bin_edges_numel += bin_edges[dim].numel();
    TORCH_INTERNAL_ASSERT(bin_edges[dim].is_contiguous());
    TORCH_INTERNAL_ASSERT(hist_output.size(dim) + 1 == bin_edges[dim].numel());
  }

  if (D == 0) {
    // hist is an empty tensor in this case; nothing to do here
    return;
  }

  std::vector<input_t> bin_seq(bin_edges_numel);
  std::vector<int64_t> num_bin_edges(D);
  std::vector<input_t> leftmost_edge(D);
  std::vector<input_t> rightmost_edge(D);
  size_t bin_seq_offset = 0;

  for (const auto dim : c10::irange(D)) {
    for (const auto elem_idx : c10::irange(bin_edges[dim].numel())) {
      bin_seq[bin_seq_offset + elem_idx] = (bin_edges[dim][elem_idx].item().to<input_t>());
    }
    num_bin_edges[dim] = bin_edges[dim].numel();
    leftmost_edge[dim] = bin_seq[bin_seq_offset];
    rightmost_edge[dim] = bin_seq[bin_seq_offset + num_bin_edges[dim] - 1];
    bin_seq_offset += num_bin_edges[dim];
  }

  // for MPSProfiler
  auto allTensorsList = bin_edges.vec();
  allTensorsList.push_back(input);
  if (has_weight) {
    allTensorsList.push_back(weight.value());
  }

  const uint32_t stridedIndicesNumThreads = input.numel();
  const uint32_t numThreads = N;
  const auto hist_sizes = hist_output.sizes();

  DimVector thread_hist_sizes(hist_sizes.size() + 1); // [n_threads, output_sizes...]
  thread_hist_sizes[0] = numThreads;
  std::copy(hist_sizes.begin(), hist_sizes.end(), thread_hist_sizes.begin() + 1);
  Tensor thread_histograms = at::zeros(
      thread_hist_sizes, hist_output.scalar_type(), std::nullopt /* layout */, kMPS, std::nullopt /* pin_memory */
  );
  TORCH_INTERNAL_ASSERT(thread_histograms.is_contiguous());

  id<MTLDevice> device = MPSDevice::getInstance()->device();
  MPSStream* mpsStream = getCurrentMPSStream();
  const uint32_t nDim = input.sizes().size();
  TORCH_CHECK(input.numel() * input.element_size() <= UINT32_MAX, "histogramdd(): Tensor is larger than 4Gb");

  dispatch_sync_with_rethrow(mpsStream->queue(), ^() {
    @autoreleasepool {
      id<MTLComputeCommandEncoder> computeEncoder = mpsStream->commandEncoder();
      const IntArrayRef& inputShape = input.sizes();
      std::vector<uint32_t> inputShapeData(inputShape.size());
      std::vector<uint32_t> strides(input.strides().begin(), input.strides().end());

      for (const auto i : c10::irange(inputShape.size())) {
        inputShapeData[i] = static_cast<uint32_t>(inputShape[i]);
      }

      id<MTLBuffer> stridedIndicesBuffer = [[device newBufferWithLength:stridedIndicesNumThreads * sizeof(uint)
                                                                options:0] autorelease];
      id<MTLComputePipelineState> stridedIndicesPSO = lib.getPipelineStateForFunc("kernel_index_offset");

      [computeEncoder setComputePipelineState:stridedIndicesPSO];
      mtl_setBytes(computeEncoder, strides, 0);
      [computeEncoder setBuffer:stridedIndicesBuffer offset:0 atIndex:1];
      mtl_setBytes(computeEncoder, inputShapeData, 2);
      mtl_setBytes(computeEncoder, nDim, 3);

      mtl_dispatch1DJob(computeEncoder, stridedIndicesPSO, stridedIndicesNumThreads);

      const std::string kernel = "histogramdd_" + scalarToMetalTypeString(input);
      id<MTLComputePipelineState> histogramPSO = lib.getPipelineStateForFunc(kernel);

      // this function call is a no-op if MPS Profiler is not enabled
      getMPSProfiler().beginProfileKernel(histogramPSO, "histogram", allTensorsList);

      [computeEncoder setComputePipelineState:histogramPSO];
      mtl_setBuffer(computeEncoder, input, 0);
      if (has_weight) {
        mtl_setBuffer(computeEncoder, weight.value(), 1);
      }
      mtl_setBuffer(computeEncoder, thread_histograms, 2);
      [computeEncoder setBuffer:stridedIndicesBuffer offset:0 atIndex:3];
      mtl_setBytes(computeEncoder, D, 4);
      [computeEncoder setBytes:bin_seq.data() length:sizeof(input_t) * bin_seq_offset atIndex:5];
      mtl_setBytes(computeEncoder, num_bin_edges, 6);
      mtl_setBytes(computeEncoder, leftmost_edge, 7);
      mtl_setBytes(computeEncoder, rightmost_edge, 8);
      mtl_setBytes(computeEncoder, thread_histograms.strides(), 9);
      mtl_setBytes(computeEncoder, bin_selection_algorithm, 10);
      mtl_setBytes(computeEncoder, has_weight, 11);

      mtl_dispatch1DJob(computeEncoder, histogramPSO, numThreads);

      getMPSProfiler().endProfileKernel(histogramPSO);
    }
  });
  at::sum_out(hist_output, thread_histograms, /*dim=*/{0});
}

template <BIN_SELECTION_ALGORITHM bin_algorithm>
static void histogramdd_out_mps_template(const Tensor& self,
                                         const std::optional<Tensor>& weight,
                                         bool density,
                                         Tensor& hist,
                                         const TensorList& bin_edges) {
  hist.fill_(0);

  const int64_t N = self.size(-1);
  const int64_t M =
      std::accumulate(self.sizes().begin(), self.sizes().end() - 1, (int64_t)1, std::multiplies<int64_t>());

  const Tensor reshaped_input = self.reshape({M, N});

  const auto reshaped_weight =
      weight.has_value() ? std::optional<Tensor>(weight.value().reshape({M})) : std::optional<Tensor>();

  std::vector<Tensor> bin_edges_contig(bin_edges.size());
  for (const auto dim : c10::irange(bin_edges_contig.size())) {
    bin_edges_contig[dim] = bin_edges[dim].contiguous();
  }

  AT_DISPATCH_FLOATING_TYPES(self.scalar_type(), "histogram_mps", [&]() {
    mps::histogramdd_kernel_impl<scalar_t, bin_algorithm>(hist, bin_edges_contig, reshaped_input, reshaped_weight);
  });

  /* Divides each bin's value by the total count/weight in all bins,
   * and by the bin's volume.
   */
  if (density) {
    const auto hist_sum = hist.sum().item();
    hist.div_(hist_sum);

    /* For each dimension, divides each bin's value
     * by the bin's length in that dimension.
     */
    for (const auto dim : c10::irange(N)) {
      const auto bin_lengths = bin_edges[dim].diff();

      // Used to reshape bin_lengths to align with the corresponding dimension of hist.
      std::vector<int64_t> shape(N, 1);
      shape[dim] = bin_lengths.numel();

      hist.div_(bin_lengths.reshape(shape));
    }
  }
}
} // namespace mps

static void histogramdd_kernel(const Tensor& self,
                               const std::optional<Tensor>& weight,
                               bool density,
                               Tensor& hist,
                               const TensorList& bin_edges) {
  mps::histogramdd_out_mps_template<mps::BINARY_SEARCH>(self, weight, density, hist, bin_edges);
}

static void histogramdd_linear_kernel(const Tensor& self,
                                      const std::optional<Tensor>& weight,
                                      bool density,
                                      Tensor& hist,
                                      const TensorList& bin_edges,
                                      bool local_search) {
  if (local_search) {
    // histogramdd codepath: both hist and bin_edges are eventually returned as output,
    // so we'll keep them consistent
    mps::histogramdd_out_mps_template<mps::LINEAR_INTERPOLATION_WITH_LOCAL_SEARCH>(
        self, weight, density, hist, bin_edges);
  } else {
    // histc codepath: bin_edges are not returned to the caller
    mps::histogramdd_out_mps_template<mps::LINEAR_INTERPOLATION>(self, weight, density, hist, bin_edges);
  }
}

static void histogram_select_outer_bin_edges_kernel(const Tensor& input,
                                                    const int64_t N,
                                                    std::vector<double>& leftmost_edges,
                                                    std::vector<double>& rightmost_edges) {
  auto [min, max] = at::aminmax(input, 0);

  for (const auto i : c10::irange(N)) {
    leftmost_edges[i] = min[i].item().to<double>();
    rightmost_edges[i] = max[i].item().to<double>();
  }
}

REGISTER_DISPATCH(histogramdd_stub, &histogramdd_kernel);
REGISTER_DISPATCH(histogramdd_linear_stub, &histogramdd_linear_kernel);
REGISTER_DISPATCH(histogram_select_outer_bin_edges_stub, &histogram_select_outer_bin_edges_kernel);
} // namespace at::native
