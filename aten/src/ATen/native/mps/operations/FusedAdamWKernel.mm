#define TORCH_ASSERT_ONLY_METHOD_OPERATORS
#include <ATen/native/ForeachUtils.h>
#include <ATen/native/mps/operations/FusedAdamWAmsgradKernelImpl.h>
#include <ATen/native/mps/operations/FusedAdamWKernelImpl.h>

#ifndef AT_PER_OPERATOR_HEADERS
#include <ATen/Functions.h>
#include <ATen/NativeFunctions.h>
#else
#include <ATen/ops/_fused_adamw_native.h>
#endif

namespace at::native {

void _fused_adamw_kernel_mps_(at::TensorList params,
                              at::TensorList grads,
                              at::TensorList exp_avgs,
                              at::TensorList exp_avg_sqs,
                              at::TensorList max_exp_avg_sqs,
                              at::TensorList state_steps,
                              const double lr,
                              const double beta1,
                              const double beta2,
                              const double weight_decay,
                              const double eps,
                              const bool amsgrad,
                              const bool maximize,
                              const std::optional<at::Tensor>& grad_scale,
                              const std::optional<at::Tensor>& found_inf) {
  if (amsgrad) {
    TORCH_CHECK(at::native::check_fast_path_restrictions({params, grads, exp_avgs, exp_avg_sqs, max_exp_avg_sqs}),
                "params, grads, exp_avgs, exp_avg_sqs, and max_exp_avg_sqs must have same dtype, device, and layout");
    mps::_fused_adamw_amsgrad_mps_impl_(params,
                                        grads,
                                        exp_avgs,
                                        exp_avg_sqs,
                                        max_exp_avg_sqs,
                                        state_steps,
                                        lr,
                                        beta1,
                                        beta2,
                                        weight_decay,
                                        eps,
                                        maximize,
                                        grad_scale,
                                        found_inf);
  } else {
    TORCH_CHECK(at::native::check_fast_path_restrictions({params, grads, exp_avgs, exp_avg_sqs}),
                "params, grads, exp_avgs, and exp_avg_sqs must have same dtype, device, and layout");
    mps::_fused_adamw_mps_impl_(params,
                                grads,
                                exp_avgs,
                                exp_avg_sqs,
                                state_steps,
                                lr,
                                beta1,
                                beta2,
                                weight_decay,
                                eps,
                                maximize,
                                grad_scale,
                                found_inf);
  }
}

void _fused_adamw_kernel_mps_(at::TensorList params,
                              at::TensorList grads,
                              at::TensorList exp_avgs,
                              at::TensorList exp_avg_sqs,
                              at::TensorList max_exp_avg_sqs,
                              at::TensorList state_steps,
                              const at::Tensor& lr,
                              const double beta1,
                              const double beta2,
                              const double weight_decay,
                              const double eps,
                              const bool amsgrad,
                              const bool maximize,
                              const std::optional<at::Tensor>& grad_scale,
                              const std::optional<at::Tensor>& found_inf) {
  if (lr.is_cpu()) {
    return _fused_adamw_kernel_mps_(params,
                                    grads,
                                    exp_avgs,
                                    exp_avg_sqs,
                                    max_exp_avg_sqs,
                                    state_steps,
                                    lr.item<double>(),
                                    beta1,
                                    beta2,
                                    weight_decay,
                                    eps,
                                    amsgrad,
                                    maximize,
                                    grad_scale,
                                    found_inf);
  }

  // Manually check devices since we specify no device check in
  // native_functions.yaml
  Device param_device = params[0].device();

  TORCH_CHECK(lr.device() == param_device, "lr must be on the same GPU device as the params");

  if (amsgrad) {
    TORCH_CHECK(at::native::check_fast_path_restrictions({params, grads, exp_avgs, exp_avg_sqs, max_exp_avg_sqs}),
                "params, grads, exp_avgs, exp_avg_sqs, and max_exp_avg_sqs must have same dtype, device, and layout");
    mps::_fused_adamw_amsgrad_mps_impl_(params,
                                        grads,
                                        exp_avgs,
                                        exp_avg_sqs,
                                        max_exp_avg_sqs,
                                        state_steps,
                                        lr,
                                        beta1,
                                        beta2,
                                        weight_decay,
                                        eps,
                                        maximize,
                                        grad_scale,
                                        found_inf);
  } else {
    TORCH_CHECK(at::native::check_fast_path_restrictions({params, grads, exp_avgs, exp_avg_sqs}),
                "params, grads, exp_avgs, and exp_avg_sqs must have same dtype, device, and layout");
    mps::_fused_adamw_mps_impl_(params,
                                grads,
                                exp_avgs,
                                exp_avg_sqs,
                                state_steps,
                                lr,
                                beta1,
                                beta2,
                                weight_decay,
                                eps,
                                maximize,
                                grad_scale,
                                found_inf);
  }
}

} // namespace at::native
