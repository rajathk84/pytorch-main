#include <ATen/Context.h>
#include <ATen/DeviceAccelerator.h>
namespace at {

std::optional<c10::DeviceType> getAccelerator(bool checked) {
#define DETECT_AND_ASSIGN_ACCELERATOR(device_name) \
  if (at::has##device_name()) {                    \
    device_type = k##device_name;                  \
    TORCH_CHECK(                                   \
        !is_accelerator_detected,                  \
        "Cannot have ",                            \
        device_type.value(),                       \
        " with other accelerators.");              \
    is_accelerator_detected = true;                \
  }

  if (is_privateuse1_backend_registered()) {
    // We explicitly allow PrivateUse1 and another device at the same time as we
    // use this for testing. Whenever a PrivateUse1 device is registered, use it
    // first.
    return kPrivateUse1;
  }
  std::optional<c10::DeviceType> device_type = std::nullopt;
  bool is_accelerator_detected = false;
  DETECT_AND_ASSIGN_ACCELERATOR(CUDA)
  DETECT_AND_ASSIGN_ACCELERATOR(MTIA)
  DETECT_AND_ASSIGN_ACCELERATOR(XPU)
  DETECT_AND_ASSIGN_ACCELERATOR(HIP)
  DETECT_AND_ASSIGN_ACCELERATOR(MPS)
  if (checked) {
    TORCH_CHECK(
        device_type, "Cannot access accelerator device when none is available.")
  }
  return device_type;

#undef DETECT_AND_ASSIGN_ACCELERATOR
}

bool isAccelerator(c10::DeviceType d) {
  switch (d) {
    case at::kCUDA:
    case at::kMTIA:
    case at::kXPU:
    case at::kHIP:
    case at::kMPS:
    case at::kPrivateUse1:
      return true;
    default:
      return false;
  }
}

} // namespace at
