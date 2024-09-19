#import <ATen/native/metal/MetalTensorImpl.h>
#import <ATen/native/metal/MetalTensorImplStorage.h>
#import <ATen/native/metal/MetalContext.h>
#import <ATen/native/metal/MetalTensorUtils.h>
#include <ATen/metal/Context.h>
#include <torch/script.h>

namespace at {
namespace native::metal {

static Tensor& copy_from_metal_(Tensor& dst, const Tensor& src) {
  TORCH_INTERNAL_ASSERT(
      src.device().type() == DeviceType::Metal,
      "copy_from_metal input tensor's device is not metal");
  TORCH_INTERNAL_ASSERT(
      dst.device().is_cpu(),
      "copy_from_metal is implemented only for CPU device output");
  TORCH_INTERNAL_ASSERT(
      dst.layout() == Layout::Strided,
      "copy_from_metal is implemented only for Strided layout output");
  TORCH_INTERNAL_ASSERT(
      dst.scalar_type() == ScalarType::Float,
      "copy_from_metal is implemented only for float dtype output, got:",
      dst.scalar_type());
  TORCH_INTERNAL_ASSERT(
      dst.is_contiguous(),
      "copy_from_metal is implemented only for contiguous output tensor");
  if(dst.numel() == 0){
    return dst;
  }
  MetalTensorImplStorage& tensorImplStorage = getTensorImplStorage(src);
  tensorImplStorage.copy_data_to_host(dst.data_ptr<float>());
  return dst;
}

static Tensor& copy_to_metal_(Tensor& dst, const Tensor& src) {
  TORCH_INTERNAL_ASSERT(
      dst.device().type() == DeviceType::Metal,
      "copy_to_metal_ output tensor's device is not metal");
  TORCH_INTERNAL_ASSERT(
      src.device().is_cpu(),
      "copy_to_metal_ is implemented only for CPU device input");
  TORCH_INTERNAL_ASSERT(
      src.layout() == Layout::Strided,
      "copy_to_metal_ is implemented only for Strided layout input");
  TORCH_INTERNAL_ASSERT(
      src.scalar_type() == ScalarType::Float,
      "copy_to_metal_ is implemented only for float dtype");

  auto cpu_tensor_contiguous = src.contiguous();
  MetalTensorImplStorage& tensorImplStorage = getTensorImplStorage(dst);
  tensorImplStorage.set_data_from_host(cpu_tensor_contiguous.data_ptr<float>());
  return dst;
}

static Tensor& metal_copy_impl_(Tensor& dst, const Tensor& src) {
  if (src.device().type() == at::kMetal && dst.device().type() == at::kCPU) {
    return copy_from_metal_(dst, src);
  }
  if (src.device().type() == at::kCPU && dst.device().type() == at::kMetal) {
    return copy_to_metal_(dst, src);
  }
  TORCH_INTERNAL_ASSERT(
      src.device().type() == DeviceType::Metal,
      "metal_copy_ is implemented only for CPU,Strided,float->Metal; Metal->CPU,Strided,float");
  return dst;
}

#pragma mark - ATen Ops

static Tensor empty(
    c10::SymIntArrayRef sym_size,
    std::optional<ScalarType> dtype,
    std::optional<Layout> layout,
    std::optional<Device> device,
    std::optional<bool> pin_memory,
    std::optional<MemoryFormat> memory_format) {
  auto size = C10_AS_INTARRAYREF_SLOW(sym_size);
  TORCH_CHECK(
      !pin_memory.has_value(),
      "'pin_memory' argument is incompatible with Metal tensor");
  TORCH_CHECK(
      !memory_format.has_value(),
      "'memory_format' argument is incompatible with Metal tensor");
  MetalTensorImplStorage mt{size.vec()};
  return makeTensor(
      std::move(mt), at::device(at::kMetal).dtype(dtype));
};

static Tensor empty_strided(
    IntArrayRef size,
    IntArrayRef stride,
    std::optional<ScalarType> dtype,
    std::optional<Layout> layout,
    std::optional<Device> device,
    std::optional<bool> pin_memory) {
  TORCH_CHECK(
      !pin_memory.has_value() || !pin_memory.value(),
      "'pin_memory' argument is incompatible with Metal tensor");
  MetalTensorImplStorage mt{size.vec(), stride.vec()};
  return makeTensor(
      std::move(mt), at::device(at::kMetal).dtype(dtype));
}


TORCH_LIBRARY_IMPL(aten, Metal, m) {
  m.impl(TORCH_SELECTIVE_NAME("aten::empty.memory_format"), empty);
  m.impl(TORCH_SELECTIVE_NAME("aten::empty_strided"), TORCH_FN(empty_strided));
}

} // namespace native::metal

struct MetalImpl : public at::metal::MetalInterface {
  bool is_metal_available() const override {
#if defined(USE_PYTORCH_METAL)
    return [[MetalContext sharedInstance] available];
#else
    return false;
#endif
  }
  at::Tensor& metal_copy_(at::Tensor& input, const at::Tensor& src)
      const override {
    TORCH_CHECK(
        is_metal_available(), "Metal is not available on the current device");
    return native::metal::metal_copy_impl_(input, src);
  }
};
#if defined(USE_PYTORCH_METAL)
static at::metal::MetalImplRegistrar g_metal_impl(new MetalImpl());
#endif

} // namespace at
