#define TORCH_ASSERT_ONLY_METHOD_OPERATORS
#include <ATen/ExpandUtils.h>
#include <ATen/TensorIndexing.h>
#include <ATen/mps/MPSProfiler.h>
#include <ATen/native/BinaryOps.h>
#include <ATen/native/TensorIterator.h>
#include <ATen/native/mps/OperationUtils.h>
#include <ATen/native/mps/operations/BinaryKernel.h>
// For MTLLanguageVersion_3_1
#include <ATen/native/mps/MPSGraphSonomaOps.h>

#ifndef AT_PER_OPERATOR_HEADERS
#include <ATen/Functions.h>
#include <ATen/NativeFunctions.h>
#else
#include <ATen/ops/complex_native.h>
#include <ATen/ops/maximum.h>
#include <ATen/ops/minimum.h>
#include <ATen/ops/nextafter_native.h>
#include <ATen/ops/polar_native.h>
#include <ATen/ops/view_as_real.h>
#endif

namespace at::native {
namespace mps {

static MetalShaderLibrary lib(R"BINARY_METAL(

#include <metal_stdlib>
using namespace metal;

template<typename T>
kernel void fmax(constant void     * input_        [[buffer(0)]],
                  constant void     * other_        [[buffer(1)]],
                  device   void     * out_          [[buffer(2)]],
                  constant uint3    * offsets       [[buffer(3)]],
                  uint tid [[thread_position_in_grid]]) {
  device   T* out   = (device   T*)((device uint8_t*)out_ + offsets[tid].x);
  constant T* input = (constant T*)((constant uint8_t*)input_ + offsets[tid].y);
  constant T* other = (constant T*)((constant uint8_t*)other_ + offsets[tid].z);

  *out = fmax(*input, *other);
}

template<typename T>
kernel void fmin(constant void     * input_        [[buffer(0)]],
                  constant void     * other_        [[buffer(1)]],
                  device   void     * out_          [[buffer(2)]],
                  constant uint3    * offsets       [[buffer(3)]],
                  uint tid [[thread_position_in_grid]]) {
  device   T* out   = (device   T*)((device uint8_t*)out_ + offsets[tid].x);
  constant T* input = (constant T*)((constant uint8_t*)input_ + offsets[tid].y);
  constant T* other = (constant T*)((constant uint8_t*)other_ + offsets[tid].z);

  *out = fmin(*input, *other);
}

template<typename T>
kernel void copysign(constant void     * input_        [[buffer(0)]],
                     constant void     * other_        [[buffer(1)]],
                     device   void     * out_          [[buffer(2)]],
                     constant uint3    * offsets       [[buffer(3)]],
                     uint tid [[thread_position_in_grid]]) {
  device   T* out   = (device   T*)((device uint8_t*)out_ + offsets[tid].x);
  constant T* input = (constant T*)((constant uint8_t*)input_ + offsets[tid].y);
  constant T* other = (constant T*)((constant uint8_t*)other_ + offsets[tid].z);

  *out = copysign(*input, *other);
}

template<typename T>
kernel void copysign_integral(constant void     * input_        [[buffer(0)]],
                     constant void     * other_        [[buffer(1)]],
                     device   void     * out_          [[buffer(2)]],
                     constant uint3    * offsets       [[buffer(3)]],
                     uint tid [[thread_position_in_grid]]) {
  device   float* out = (device float*)((device uint8_t*)out_ + offsets[tid].x);
  constant T* input = (constant T*)((constant uint8_t*)input_ + offsets[tid].y);
  constant T* other = (constant T*)((constant uint8_t*)other_ + offsets[tid].z);

  *out = copysign(static_cast<float>(*input), static_cast<float>(*other));
}

#define REGISTER_FMAX_OP(DTYPE)                        \
template                                               \
[[host_name("fmax_" #DTYPE)]]                          \
kernel void fmax<DTYPE>(                               \
  constant void     * input_        [[buffer(0)]],     \
  constant void     * other_        [[buffer(1)]],     \
  device   void     * out_          [[buffer(2)]],     \
  constant uint3    * offsets       [[buffer(3)]],     \
  uint tid [[thread_position_in_grid]]);

#define REGISTER_FMIN_OP(DTYPE)                        \
template                                               \
[[host_name("fmin_" #DTYPE)]]                          \
kernel void fmin<DTYPE>(                               \
  constant void     * input_        [[buffer(0)]],     \
  constant void     * other_        [[buffer(1)]],     \
  device   void     * out_          [[buffer(2)]],     \
  constant uint3    * offsets       [[buffer(3)]],     \
  uint tid [[thread_position_in_grid]]);

#define REGISTER_COPYSIGN_OP(DTYPE)                    \
template                                               \
[[host_name("copysign_" #DTYPE)]]                      \
kernel void copysign<DTYPE>(                           \
  constant void     * input_        [[buffer(0)]],     \
  constant void     * other_        [[buffer(1)]],     \
  device   void     * out_          [[buffer(2)]],     \
  constant uint3    * offsets       [[buffer(3)]],     \
  uint tid [[thread_position_in_grid]]);

#define REGISTER_COPYSIGN_INTEGRAL_OP(DTYPE)           \
template                                               \
[[host_name("copysign_" #DTYPE)]]                      \
kernel void copysign_integral<DTYPE>(                  \
  constant void     * input_        [[buffer(0)]],     \
  constant void     * other_        [[buffer(1)]],     \
  device   void     * out_          [[buffer(2)]],     \
  constant uint3    * offsets       [[buffer(3)]],     \
  uint tid [[thread_position_in_grid]]);

REGISTER_FMAX_OP(float);
REGISTER_FMAX_OP(half);
REGISTER_FMIN_OP(float);
REGISTER_FMIN_OP(half);
REGISTER_COPYSIGN_OP(float);
REGISTER_COPYSIGN_OP(half);
REGISTER_COPYSIGN_INTEGRAL_OP(int);
REGISTER_COPYSIGN_INTEGRAL_OP(long);
REGISTER_COPYSIGN_INTEGRAL_OP(short);
REGISTER_COPYSIGN_INTEGRAL_OP(char);
REGISTER_COPYSIGN_INTEGRAL_OP(uchar);
REGISTER_COPYSIGN_INTEGRAL_OP(bool);

template<typename T>
kernel void polar(constant void  * abs_         [[buffer(0)]],
                  constant void  * angle_       [[buffer(1)]],
                  device   void  * out_         [[buffer(2)]],
                  constant uint3 * offsets      [[buffer(3)]],
                  uint tid [[thread_position_in_grid]]) {
  device   T* out = (device T*)((device uint8_t*)out_ + offsets[tid].x);
  constant T* angle = (constant T*)((constant uint8_t*)angle_ + offsets[tid].z);
  constant T* abs = (constant T*)((constant uint8_t*)abs_ + offsets[tid].y);
  out[0] = abs[0] * cos(angle[0]);
  out[1] = abs[0] * sin(angle[0]);
}

#define REGISTER_POLAR_OP(DTYPE)       \
template                               \
[[host_name("polar_" #DTYPE)]]         \
kernel void polar<DTYPE>(              \
  constant void    * abs,              \
  constant void    * angle,            \
  device   void    * out,              \
  constant uint3   * offsets,          \
  uint tid)

REGISTER_POLAR_OP(float);
REGISTER_POLAR_OP(half);

template<typename T>
kernel void complex_mul(constant void  * input_       [[buffer(0)]],
                        constant void  * other_       [[buffer(1)]],
                        device   void  * out_         [[buffer(2)]],
                        constant uint3 * offsets      [[buffer(3)]],
                        uint tid [[thread_position_in_grid]]) {
  device   T* out   = (device   T*)((device uint8_t*)out_ + offsets[tid].x);
  constant T* input = (constant T*)((constant uint8_t*)input_ + offsets[tid].y);
  constant T* other = (constant T*)((constant uint8_t*)other_ + offsets[tid].z);
  out[0] = input[0]*other[0] - input[1]*other[1];
  out[1] = input[0]*other[1] + input[1]*other[0];
}

#define REGISTER_COMPLEX_MUL_OP(DTYPE)       \
template                                     \
[[host_name("complex_mul_" #DTYPE)]]         \
kernel void complex_mul<DTYPE>(              \
  constant void    * input,                  \
  constant void    * other,                  \
  device   void    * out,                    \
  constant uint3   * offsets,                \
  uint tid)

REGISTER_COMPLEX_MUL_OP(float);
REGISTER_COMPLEX_MUL_OP(half);

template<typename T, typename U>
kernel void nextafter_kernel(constant void  * input_       [[buffer(0)]],
                             constant void  * other_       [[buffer(1)]],
                             device   void  * out_         [[buffer(2)]],
                             constant uint3 * offsets      [[buffer(3)]],
                             uint tid [[thread_position_in_grid]]) {
  auto out   = (device   T*)((device uint8_t*)out_ + offsets[tid].x);
  auto input = *(constant T*)((constant uint8_t*)input_ + offsets[tid].y);
  auto other = *(constant T*)((constant uint8_t*)other_ + offsets[tid].z);
#if __METAL_VERSION__ >= 310
  *out = nextafter(input, other);
#else
  if (input == other) {
    *out = input;
  } else if (isnan(input) || isnan(other)) {
    *out = NAN;
  } else if (input == 0) {
    constexpr auto one = as_type<T>(static_cast<U>(1));
    *out = other > 0 ? one : -one;
  } else {
    U bits = as_type<U>(input);
    (input > 0) ^ (input > other) ? bits++ : bits--;
    *out = as_type<T>(bits);
  }
#endif
}

#define REGISTER_NEXTAFTER_OP(DTYPE, UTYPE)  \
template                                     \
[[host_name("nextafter_kernel_" #DTYPE)]]    \
kernel void nextafter_kernel<DTYPE, UTYPE>(  \
  constant void    * input,                  \
  constant void    * other,                  \
  device   void    * out,                    \
  constant uint3   * offsets,                \
  uint tid)

REGISTER_NEXTAFTER_OP(float, uint);
REGISTER_NEXTAFTER_OP(half, ushort);

template<typename T>
kernel void complex_kernel(constant void  * real_       [[buffer(0)]],
                           constant void  * imag_       [[buffer(1)]],
                           device   void  * out_        [[buffer(2)]],
                           constant uint3 * offsets     [[buffer(3)]],
                           uint tid [[thread_position_in_grid]]) {
  device   T* out  = (device   T*)((device uint8_t*)out_ + offsets[tid].x);
  constant T* real = (constant T*)((constant uint8_t*)real_ + offsets[tid].y);
  constant T* imag = (constant T*)((constant uint8_t*)imag_ + offsets[tid].z);
  out[0] = real[0];
  out[1] = imag[0];
}

#define REGISTER_COMPLEX_OUT_OP(DTYPE)   \
template                                 \
[[host_name("complex_kernel_" #DTYPE)]]  \
kernel void complex_kernel<DTYPE>(       \
  constant void    * real,               \
  constant void    * imag,               \
  device   void    * out,                \
  constant uint3   * offsets,            \
  uint tid)

REGISTER_COMPLEX_OUT_OP(float);
REGISTER_COMPLEX_OUT_OP(half);

)BINARY_METAL");

static void binary_mps_impl(TensorIteratorBase& iter, const std::string func_name) {
  TORCH_CHECK(iter.common_dtype() != at::kDouble, "float64 is not supported on MPS");

  Tensor input = iter.input(0);
  Tensor other = iter.input(1);
  Tensor out = iter.output();

  id<MTLDevice> device = MPSDevice::getInstance()->device();
  MPSStream* mpsStream = getCurrentMPSStream();
  const uint32_t nDim = iter.ndim();
  constexpr uint32_t nOffsets = 3;
  const uint32_t numThreads = iter.numel();
  dispatch_sync_with_rethrow(mpsStream->queue(), ^() {
    @autoreleasepool {
      id<MTLComputeCommandEncoder> computeEncoder = mpsStream->commandEncoder();
      const std::string kernel = func_name + "_" + scalarToMetalTypeString(input);
      auto kernelDataOffsets = generateKernelDataOffsets(computeEncoder, iter);

      id<MTLComputePipelineState> binaryPSO = lib.getPipelineStateForFunc(kernel);

      // this function call is a no-op if MPS Profiler is not enabled
      getMPSProfiler().beginProfileKernel(binaryPSO, kernel, {input, other});

      [computeEncoder setComputePipelineState:binaryPSO];
      mtl_setBuffer(computeEncoder, input, 0);
      mtl_setBuffer(computeEncoder, other, 1);
      mtl_setBuffer(computeEncoder, out, 2);
      [computeEncoder setBuffer:kernelDataOffsets offset:0 atIndex:3];
      mtl_dispatch1DJob(computeEncoder, binaryPSO, numThreads);

      getMPSProfiler().endProfileKernel(binaryPSO);
    }
  });
}

void complex_mul_out(const Tensor& input, const Tensor& other, const Tensor& output) {
  TORCH_INTERNAL_ASSERT(c10::isComplexType(input.scalar_type()) || c10::isComplexType(other.scalar_type()));
  auto new_size = at::infer_size(input.sizes(), other.sizes());
  if (!output.sizes().equals(new_size)) {
    output.resize_(new_size);
  }
  uint32_t length = output.numel();
  if (length == 0) {
    return;
  }
  auto common_dtype = output.scalar_type();
  auto output_as_real = at::view_as_real(output).select(output.dim(), 0);
  auto input_as_real = at::view_as_real(input.to(kMPS, common_dtype)).select(input.dim(), 0);
  auto other_as_real = at::view_as_real(other.to(kMPS, common_dtype)).select(other.dim(), 0);
  auto iter =
      TensorIteratorConfig().add_output(output_as_real).add_input(input_as_real).add_input(other_as_real).build();

  mps::binary_mps_impl(iter, "complex_mul");
}

} // namespace mps

static void fmax_mps_kernel(TensorIteratorBase& iter) {
  if (isFloatingType(iter.common_dtype())) {
    mps::binary_mps_impl(iter, "fmax");
  } else {
    at::maximum_out(const_cast<Tensor&>(iter.output()), iter.input(0), iter.input(1));
  }
}
static void fmin_mps_kernel(TensorIteratorBase& iter) {
  if (isFloatingType(iter.common_dtype())) {
    mps::binary_mps_impl(iter, "fmin");
  } else {
    at::minimum_out(const_cast<Tensor&>(iter.output()), iter.input(0), iter.input(1));
  }
}

static void copysign_mps_kernel(TensorIteratorBase& iter) {
  mps::binary_mps_impl(iter, "copysign");
}

static void nextafter_mps_kernel(TensorIteratorBase& iter) {
  TORCH_CHECK_TYPE(isFloatingType(iter.common_dtype()), "nextafter_mps not implemented for non-floating types");
  mps::binary_mps_impl(iter, "nextafter_kernel");
}

REGISTER_DISPATCH(fmax_stub, &fmax_mps_kernel);
REGISTER_DISPATCH(fmin_stub, &fmin_mps_kernel);
REGISTER_DISPATCH(copysign_stub, &copysign_mps_kernel);
REGISTER_DISPATCH(nextafter_stub, &nextafter_mps_kernel);

Tensor& polar_out_mps(const Tensor& abs, const Tensor& angle, Tensor& output) {
  auto new_size = at::infer_size(abs.sizes(), angle.sizes());
  if (!output.sizes().equals(new_size)) {
    output.resize_(new_size);
  }
  uint32_t length = output.numel();
  if (length == 0) {
    return output;
  }
  auto output_as_real = at::view_as_real(output).select(output.dim(), 0);
  auto iter = TensorIteratorConfig().add_output(output_as_real).add_input(abs).add_input(angle).build();

  mps::binary_mps_impl(iter, "polar");
  return output;
}

Tensor& complex_out_mps(const Tensor& real, const Tensor& imag, Tensor& output) {
  auto new_size = at::infer_size(real.sizes(), imag.sizes());
  if (!output.sizes().equals(new_size)) {
    output.resize_(new_size);
  }
  uint32_t length = output.numel();
  if (length == 0) {
    return output;
  }
  auto output_as_real = at::view_as_real(output).select(output.dim(), 0);
  auto iter = TensorIteratorConfig().add_output(output_as_real).add_input(real).add_input(imag).build();

  mps::binary_mps_impl(iter, "complex_kernel");
  return output;
}
} // namespace at::native
