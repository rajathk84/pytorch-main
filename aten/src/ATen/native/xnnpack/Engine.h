#pragma once

#include <ATen/core/Tensor.h>
#include <limits>

namespace at::native::xnnpack {

//
// Convolution
//

bool use_convolution2d(
    const Tensor& input,
    const Tensor& weight,
    const at::OptionalIntArrayRef bias_sizes_opt,
    const IntArrayRef padding,
    const IntArrayRef stride,
    const IntArrayRef dilation,
    const int64_t groups,
    const bool transposed);

Tensor convolution2d(
    const Tensor& input,
    const Tensor& weight,
    const Tensor& bias,
    const IntArrayRef padding,
    const IntArrayRef stride,
    const IntArrayRef dilation,
    const int64_t groups);

//
// Linear
//

bool use_linear(
  const Tensor& input,
  const Tensor& weight,
  const Tensor& bias);

Tensor linear(
  const Tensor& input,
  const Tensor& weight,
  const Tensor& bias);

//
// Max Pooling
//

bool use_max_pool2d(
    const Tensor& input,
    const IntArrayRef kernel,
    const IntArrayRef padding,
    IntArrayRef stride,
    const IntArrayRef dilation,
    const bool ceil_mode,
    const float output_min = -std::numeric_limits<float>::infinity(),
    const float output_max = +std::numeric_limits<float>::infinity());

Tensor max_pool2d(
    const Tensor& input,
    const IntArrayRef kernel,
    const IntArrayRef padding,
    IntArrayRef stride,
    const IntArrayRef dilation,
    const bool ceil_mode,
    const float output_min = -std::numeric_limits<float>::infinity(),
    const float output_max = +std::numeric_limits<float>::infinity());

//
// Global Average Pooling
//

bool use_global_average_pool(const Tensor& input);
Tensor global_average_pool(const Tensor& input);

//
// Channel Shuffle
//

bool use_channel_shuffle(
    const Tensor& input,
    const int64_t groups);

Tensor channel_shuffle(
    const Tensor& input,
    const int64_t groups);

//
// Activations
//
bool use_hardswish(const Tensor& input);
Tensor hardswish(const Tensor& input);
Tensor& hardswish_(Tensor& input);

} // namespace at::native::xnnpack
