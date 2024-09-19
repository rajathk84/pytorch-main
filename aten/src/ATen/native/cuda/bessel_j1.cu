#define TORCH_ASSERT_NO_OPERATORS

#include <ATen/native/UnaryOps.h>

#include <limits>

#include <ATen/AccumulateType.h>
#include <ATen/Dispatch.h>
#include <ATen/native/DispatchStub.h>
#include <ATen/native/Math.h>
#include <ATen/native/TensorIterator.h>
#include <ATen/native/cuda/JitLoops.cuh>
#include <ATen/native/cuda/Loops.cuh>
#include <ATen/native/cuda/Math.cuh>
#include <ATen/native/cuda/jit_utils.h>
#include <ATen/NumericUtils.h>
#include <c10/core/Scalar.h>
#include <c10/cuda/CUDAMathCompat.h>
#include <c10/util/complex.h>

namespace at::native {
namespace {
CONSTEXPR_EXCEPT_WIN_CUDA char bessel_j1_name[] = "bessel_j1_forward";

void bessel_j1_kernel_cuda(TensorIteratorBase& iterator) {
#if AT_USE_JITERATOR()
    AT_DISPATCH_FLOATING_TYPES(iterator.common_dtype(), "bessel_j1_cuda", [&]() {
        jitted_gpu_kernel<bessel_j1_name, scalar_t, scalar_t, 1>(iterator, bessel_j1_string);
    });
#else
    AT_DISPATCH_FLOATING_TYPES(iterator.common_dtype(), "bessel_j1_cuda", [&]() {
        gpu_kernel(iterator, []GPU_LAMBDA(scalar_t a) -> scalar_t {
            return bessel_j1_forward(a);
        });
    });
#endif // AT_USE_JITERATOR()
}

} // anonymous namespace

REGISTER_DISPATCH(special_bessel_j1_stub, &bessel_j1_kernel_cuda);
} // namespace at::native
