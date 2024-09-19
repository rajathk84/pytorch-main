#define TORCH_ASSERT_NO_OPERATORS
#include <ATen/native/cuda/Distributions.h>
#include <ATen/Dispatch.h>
#include <ATen/cuda/CUDAApplyUtils.cuh>
#include <ATen/AccumulateType.h>
#include <ATen/cuda/CUDAGeneratorImpl.h>
#include <ATen/native/UnaryOps.h>
#include <ATen/native/cuda/DistributionTemplates.h>

#include <curand.h>
#include <curand_kernel.h>
#include <curand_philox4x32_x.h>
#include <utility>
#include <functional>

#include <ATen/native/Distributions.h>
#include <ATen/native/cuda/Loops.cuh>
#include <ATen/native/TensorIterator.h>

#include <cstdint>
#include <limits>
#include <utility>
#include <type_traits>

/**
 * Note [Register spilling in curand call for CUDA < 10]
 * ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
 * For CUDA < 10, curandStatePhilox4_32_10_t engine achieves poor performance (60% SOL bandwidth)
 * when called to generate one random number at a time. This is because the line
 *            unsigned ret = (&state->output.x)[state->STATE++];
 * in
 *            QUALIFIERS unsigned int curand(curandStatePhilox4_32_10_t *state)
 * in curand_kernel.h dynamically indexes into state.output, preventing the compiler from ever
 * storing state.output in registers.
 *
 * CUDA 10 fixed this problem. However, for backwards compatibility, in the following kernels
 * we are using curand distributions that utilize curand4 call. curand4 call doesn't have the
 * register spilling problem.
 */

namespace {

template <typename scalar_t>
void poisson_cuda_kernel(
    const at::TensorBase &ret,
    const at::TensorBase &lambda,
    at::PhiloxCudaState philox_args) {
  auto functor = [philox_args] __device__(
          scalar_t & ret_val, const scalar_t& lambda) {
        CUDA_KERNEL_ASSERT(lambda >= 0 && "invalid Poisson rate, expected rate to be non-negative");
        auto seeds = at::cuda::philox::unpack(philox_args);
        curandStatePhilox4_32_10_t state;
        curand_init(std::get<0>(seeds),
                    blockIdx.x * blockDim.x + threadIdx.x,
                    std::get<1>(seeds),
                    &state);
        ret_val = static_cast<scalar_t>(curand_poisson(&state, lambda));
      };
  at::cuda::CUDA_tensor_apply2<scalar_t, scalar_t, decltype(functor),
                               /*max_threads_per_block=*/512,
                               /*min_blocks_per_sm==*/2>(ret, lambda, functor);
}

struct curand_uniform_wrapper {
  curandStatePhilox4_32_10_t &state;
  __device__ curand_uniform_wrapper(curandStatePhilox4_32_10_t &state): state(state) {}
  __device__ float operator()() {

  uint32_t val = curand(&state); //need just bits
  constexpr auto MASK = static_cast<uint32_t>((static_cast<uint64_t>(1) << std::numeric_limits<float>::digits) - 1);
  constexpr auto DIVISOR = static_cast<float>(1) / (static_cast<uint32_t>(1) << std::numeric_limits<float>::digits);
    return (val & MASK) * DIVISOR;
  }
};

template <typename scalar_t>
void binomial_cuda_kernel(
    at::TensorIteratorBase &iter,
    at::PhiloxCudaState philox_args) {
  using accscalar_t = at::acc_type<scalar_t, true>;

  at::native::distribution_binary_kernel(iter, philox_args,
      [] GPU_LAMBDA (curandStatePhilox4_32_10_t& state, scalar_t count, scalar_t prob) {
        #if defined(__CUDA_ARCH__) || defined(USE_ROCM)
        auto uniform_lambda = curand_uniform_wrapper(state);
        BaseSampler<accscalar_t, decltype(uniform_lambda)> standard_uniform(uniform_lambda);
        auto sample = sample_binomial<scalar_t, accscalar_t, decltype(uniform_lambda)>(count, prob, standard_uniform);
        return static_cast<scalar_t>(sample);
        #else
        return count; // useless.
        #endif
      }
  );
}

template <typename scalar_t>
void gamma_cuda_kernel(
    const at::TensorBase &ret,
    const at::TensorBase &alpha,
    at::PhiloxCudaState philox_args) {
  using accscalar_t = at::acc_type<scalar_t, true>;
  auto functor = [philox_args] __device__(
          scalar_t & ret_val, const scalar_t& alpha) {
        auto seeds = at::cuda::philox::unpack(philox_args);
        curandStatePhilox4_32_10_t state;
        curand_init(std::get<0>(seeds),
                    blockIdx.x * blockDim.x + threadIdx.x,
                    std::get<1>(seeds),
                    &state);

        auto uniform_lambda = [&state] __device__ () {
          return curand_uniform(&state);
        };
        BaseSampler<accscalar_t, decltype(uniform_lambda)> standard_uniform(uniform_lambda);

        auto normal_lambda = [&state] __device__ () {
          return curand_normal(&state);
        };
        BaseSampler<accscalar_t, decltype(normal_lambda)> standard_normal(normal_lambda);
        auto sample = sample_gamma<scalar_t, accscalar_t, decltype(uniform_lambda), decltype(normal_lambda)>(alpha, standard_uniform, standard_normal);
        auto min_value = std::numeric_limits<scalar_t>::min();
        ret_val = (min_value > sample) ? min_value : sample;
      };
  at::cuda::CUDA_tensor_apply2<scalar_t, scalar_t, decltype(functor),
                               /*max_threads_per_block=*/256,
                               /*min_blocks_per_sm==*/2>(ret, alpha, functor);
}

} // namespace

namespace at::native {

void launch_dirichlet_kernel(at::TensorIteratorBase &iter) {
  AT_DISPATCH_FLOATING_TYPES_AND2(at::ScalarType::Half, at::ScalarType::BFloat16,
                                  iter.input_dtype(), "dirichlet_cuda", [&] {
    at::native::gpu_kernel(
        iter,
        [] GPU_LAMBDA (scalar_t gamma, scalar_t gamma_sum) {
      auto ret_val = gamma / gamma_sum;
      auto min_value = std::numeric_limits<scalar_t>::min();
      auto max_value = 1 - std::numeric_limits<scalar_t>::epsilon();
      ret_val = (min_value > ret_val) ? min_value : ret_val;
      ret_val = (max_value < ret_val) ? max_value : ret_val;
      return ret_val;
    });
  });
}

void launch_poisson_cuda_kernel(
    const TensorBase &ret, const TensorBase &lambda, CUDAGeneratorImpl *gen) {
  PhiloxCudaState rng_engine_inputs;
  {
    // See Note [Acquire lock when using random generators]
    std::lock_guard<std::mutex> lock(gen->mutex_);
    rng_engine_inputs = gen->philox_cuda_state(20);
  }
  AT_DISPATCH_FLOATING_TYPES_AND2(at::ScalarType::Half, at::ScalarType::BFloat16, ret.scalar_type(), "poisson_cuda", [&] {
    poisson_cuda_kernel<scalar_t>(ret, lambda, rng_engine_inputs);
  });
}

void launch_binomial_cuda_kernel(
    TensorIteratorBase &iter, CUDAGeneratorImpl *gen) {
  PhiloxCudaState rng_engine_inputs;
  {
    // See Note [Acquire lock when using random generators]
    std::lock_guard<std::mutex> lock(gen->mutex_);
    rng_engine_inputs = gen->philox_cuda_state(42);
  }
  AT_DISPATCH_FLOATING_TYPES_AND2(at::ScalarType::Half, at::ScalarType::BFloat16, iter.input_dtype(), "binomial_cuda", [&] {
    binomial_cuda_kernel<scalar_t>(iter, rng_engine_inputs);
  });
}

void launch_gamma_kernel(
    const TensorBase &ret, const TensorBase &alpha, CUDAGeneratorImpl *gen) {
  PhiloxCudaState rng_engine_inputs;
  {
    // See Note [Acquire lock when using random generators]
    std::lock_guard<std::mutex> lock(gen->mutex_);
    rng_engine_inputs = gen->philox_cuda_state(10);
  }
  AT_DISPATCH_FLOATING_TYPES_AND2(at::ScalarType::Half, at::ScalarType::BFloat16, ret.scalar_type(), "gamma_cuda", [&] {
     gamma_cuda_kernel<scalar_t>(ret, alpha, rng_engine_inputs);
   });
}

void launch_standard_gamma_grad_kernel(TensorIteratorBase &iter) {
  AT_DISPATCH_FLOATING_TYPES_AND2(at::ScalarType::Half, at::ScalarType::BFloat16, iter.input_dtype(), "_standard_gamma_grad_cuda", [&] {
    using accscalar_t = at::acc_type<scalar_t, true>;
    gpu_kernel(iter,
      [] GPU_LAMBDA (scalar_t self_val, scalar_t output_val) {
        return standard_gamma_grad_one<scalar_t, accscalar_t>(self_val, output_val);
      });
  });
}

void launch_dirichlet_grad_kernel(TensorIteratorBase &iter) {
  AT_DISPATCH_FLOATING_TYPES(iter.input_dtype(), "_dirichlet_grad_cuda", [&] {
    using accscalar_t = at::acc_type<scalar_t, true>;
    at::native::gpu_kernel(iter,
      [] GPU_LAMBDA (scalar_t x_val, scalar_t alpha_val, scalar_t total_val) -> scalar_t {
        return dirichlet_grad_one<scalar_t, accscalar_t>(x_val, alpha_val, total_val);
      });
  });
}

} // namespace at::native
