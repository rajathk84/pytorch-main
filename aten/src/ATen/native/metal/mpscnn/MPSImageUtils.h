#import <ATen/Tensor.h>
#import <ATen/native/metal/MetalCommandBuffer.h>
#import <ATen/native/metal/MetalTensorImpl.h>
#import <ATen/native/metal/MetalTensorUtils.h>

#import <MetalPerformanceShaders/MetalPerformanceShaders.h>

namespace at {
namespace native {
namespace metal {

MPSImage* createStaticImage(IntArrayRef sizes);
MPSImage* createStaticImage(const float* src, const IntArrayRef sizes);
MPSImage* createStaticImage(
    MPSTemporaryImage* image,
    MetalCommandBuffer* buffer,
    bool waitUntilCompleted);

MPSTemporaryImage* createTemporaryImage(
    MetalCommandBuffer* buffer,
    const IntArrayRef sizes);
MPSTemporaryImage* createTemporaryImage(
    MetalCommandBuffer* buffer,
    const IntArrayRef sizes,
    const float* src);
MPSTemporaryImage* createTemporaryImage(
    MetalCommandBuffer* buffer,
    MPSImage* image);

void copyImageToFloatBuffer(float* dst, MPSImage* image);

void copyImageToMetalBuffer(
    MetalCommandBuffer* buffer,
    id<MTLBuffer> dst,
    MPSImage* image);

static inline MPSImage* imageFromTensor(const Tensor& tensor) {
  TORCH_CHECK(tensor.is_metal());
  using MetalTensorImplStorage = at::native::metal::MetalTensorImplStorage;
  using MetalTensorImpl = at::MetalTensorImpl<MetalTensorImplStorage>;
  MetalTensorImpl* impl = (MetalTensorImpl*)tensor.unsafeGetTensorImpl();
  MetalTensorImplStorage& implStorage = impl->unsafe_opaque_handle();
  return implStorage.texture()->image();
}

/*
MPSImage carries a IntList shape which is identical to the shape of the CPU
tensor it’s converted from.
1) 1D tensors (W,) are always stored as MPSImage(N=1, C=1, H=1, W=W).
2) 2D tensors (H, W) are always stored as MPSImage(N=1, C=1, H=H, W=W).
3) 3D tensors (C, H, W) are always stored as MPSImage(N=1, C=C, H=H, W=W).
4) 4D tensors (N, C, H, W) are always stored as MPSImage(N=N, C=C, H=H, W=W).
5) 5D tensors (T, N, C, H, W) are always stored as MPSImage(N=T*N, C=C, H=H,
W=W). 6) ...
 */
static inline std::vector<int64_t> computeImageSize(IntArrayRef sizes) {
  std::vector<int64_t> imageSize(4, 1);
  int64_t index = 3;
  int64_t batch = 1;
  for (int64_t i = sizes.size() - 1; i >= 0; i--) {
    if (index != 0) {
      imageSize[index] = sizes[i];
      index--;
      continue;
    }
    // For higher dimensional tensors,
    // multiply rest of dims into imageSize[0]
    batch *= sizes[i];
  }
  imageSize[0] = batch;
  return imageSize;
}

} // namespace metal
} // namespace native
} // namespace at
