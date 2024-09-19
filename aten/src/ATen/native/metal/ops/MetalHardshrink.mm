#include <ATen/Tensor.h>
#import <ATen/native/metal/MetalCommandBuffer.h>
#import <ATen/native/metal/MetalContext.h>
#import <ATen/native/metal/MetalTensorImpl.h>
#import <ATen/native/metal/MetalTensorImplStorage.h>
#import <ATen/native/metal/MetalTensorUtils.h>
#import <ATen/native/metal/mpscnn/MPSCNNUtils.h>
#import <ATen/native/metal/mpscnn/MPSImage+Tensor.h>
#import <ATen/native/metal/mpscnn/MPSImageUtils.h>
#include <torch/library.h>

namespace at::native::metal {

using MetalTensorImpl = at::MetalTensorImpl<MetalTensorImplStorage>;

// NB: this is currently unused, but I've left it because in principle
// it's useful
static Tensor& hardshrink_(Tensor& input, const at::Scalar& lambda=0.5) {
  float l = lambda.toFloat();
  MPSImage* X = imageFromTensor(input);
  MetalCommandBuffer* commandBuffer = getCommandBuffer(input);
  IntArrayRef outputSize = input.sizes();
  std::vector<int64_t> imageSize = computeImageSize(outputSize);
  MPSImage* Y = createTemporaryImage(commandBuffer, imageSize);
  id<MTLComputeCommandEncoder> encoder =
      [commandBuffer.buffer computeCommandEncoder];
  id<MTLComputePipelineState> state =
      [[MetalContext sharedInstance] specializedPipelineState:"hardshrink"
                                                    Constants:@[
                                                      @(X.numberOfImages),
                                                      @(X.featureChannels),
                                                      @(X.height),
                                                      @(X.width),
                                                      @(l)
                                                    ]];

  [encoder setComputePipelineState:state];
  [encoder setTexture:[X texture] atIndex:0];
  [encoder setTexture:[Y texture] atIndex:1];

  const auto& launchParams =
      metal::mpscnn::spatialPointwiseKernelLaunchParams(state, X);
  [encoder dispatchThreadgroups:launchParams.threadgroupsPerGrid
          threadsPerThreadgroup:launchParams.threadsPerThreadgroup];
  [encoder endEncoding];
  MetalTensorImpl* impl = (MetalTensorImpl*)input.unsafeGetTensorImpl();
  MetalTensorImplStorage& implStorage = impl->unsafe_opaque_handle();
  implStorage.texture()->setImage(Y);
  return input;
}

static Tensor hardshrink(const at::Tensor& input, const at::Scalar& lambda=0.5) {
  float l = lambda.toFloat();
  MPSImage* X = imageFromTensor(input);
  IntArrayRef outputSize = input.sizes();
  MetalTensorImplStorage mt{outputSize.vec()};
  MetalCommandBuffer* commandBuffer = getCommandBuffer(input);
  mt.texture()->allocateTemporaryStorage(outputSize, commandBuffer);
  MPSImage* Y = mt.texture()->image();
  id<MTLComputeCommandEncoder> encoder =
      [commandBuffer.buffer computeCommandEncoder];
  id<MTLComputePipelineState> state =
      [[MetalContext sharedInstance] specializedPipelineState:"hardshrink"
                                                    Constants:@[
                                                      @(X.numberOfImages),
                                                      @(X.featureChannels),
                                                      @(X.height),
                                                      @(X.width),
                                                      @(l)
                                                    ]];

  [encoder setComputePipelineState:state];
  [encoder setTexture:[X texture] atIndex:0];
  [encoder setTexture:[Y texture] atIndex:1];

  const auto& launchParams =
      metal::mpscnn::spatialPointwiseKernelLaunchParams(state, X);
  [encoder dispatchThreadgroups:launchParams.threadgroupsPerGrid
          threadsPerThreadgroup:launchParams.threadsPerThreadgroup];
  [encoder endEncoding];

  auto output = makeTensor(std::move(mt), input.options());
  return output;
}

TORCH_LIBRARY_IMPL(aten, Metal, m) {
  m.impl(TORCH_SELECTIVE_NAME("aten::hardshrink"), TORCH_FN(hardshrink));
}

} // namespace at::native::metal
