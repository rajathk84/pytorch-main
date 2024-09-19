#pragma once
#include <ATen/cuda/CUDAConfig.h>

#if AT_MAGMA_ENABLED()
#include <magma_types.h>
#include <magma_v2.h>
#endif

namespace at {
namespace native {

#if AT_MAGMA_ENABLED()

// RAII for a MAGMA Queue
struct MAGMAQueue {

  // Default constructor without a device will cause
  // destroying a queue which has not been initialized.
  MAGMAQueue() = delete;

  // Constructor
  explicit MAGMAQueue(int64_t device_id) {
    cublasHandle_t handle = at::cuda::getCurrentCUDABlasHandle();
#if !defined(USE_ROCM)
    // Magma operations is numerically sensitive, so TF32 should be off
    // regardless of the global flag.
    TORCH_CUDABLAS_CHECK(cublasGetMathMode(handle, &original_math_mode));
    TORCH_CUDABLAS_CHECK(cublasSetMathMode(handle, CUBLAS_DEFAULT_MATH));
#endif
    magma_queue_create_from_cuda(
      device_id,
      at::cuda::getCurrentCUDAStream(),
      handle,
      at::cuda::getCurrentCUDASparseHandle(),
      &magma_queue_);
  }

  // Getter
  magma_queue_t get_queue() const { return magma_queue_; }

  // Destructor
  ~MAGMAQueue() {
#if !defined(USE_ROCM)
    // We've manually set the math mode to CUBLAS_DEFAULT_MATH, now we
    // should restore the original math mode back
    cublasHandle_t handle = magma_queue_get_cublas_handle(magma_queue_);
    cublasSetMathMode(handle, original_math_mode);
#endif
    magma_queue_destroy(magma_queue_);
  }

 private:
  magma_queue_t magma_queue_;
#if !defined(USE_ROCM)
  cublasMath_t original_math_mode;
#endif
};

static inline magma_int_t magma_int_cast(int64_t value, const char* varname) {
  auto result = static_cast<magma_int_t>(value);
  if (static_cast<int64_t>(result) != value) {
    AT_ERROR("magma: The value of ", varname, "(", (long long)value,
             ") is too large to fit into a magma_int_t (", sizeof(magma_int_t), " bytes)");
  }
  return result;
}

// MAGMA functions that don't take a magma_queue_t aren't stream safe
// Work around this by synchronizing with the default stream
struct MagmaStreamSyncGuard {
  MagmaStreamSyncGuard() {
    auto stream = at::cuda::getCurrentCUDAStream();
    if (stream != at::cuda::getDefaultCUDAStream()) {
      at::cuda::stream_synchronize(stream);
    }
  }

  ~MagmaStreamSyncGuard() noexcept(false) {
    auto default_stream = at::cuda::getDefaultCUDAStream();
    if (at::cuda::getCurrentCUDAStream() != default_stream) {
      at::cuda::stream_synchronize(default_stream);
    }
  }
};
#endif

} // namespace native
} // namespace at
