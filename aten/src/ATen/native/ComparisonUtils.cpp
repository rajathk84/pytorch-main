#include <ATen/core/TensorBase.h>
#include <ATen/core/TensorBody.h>
#include <c10/util/OptionalArrayRef.h>

#ifdef AT_PER_OPERATOR_HEADERS
#include <ATen/ops/_assert_tensor_metadata_native.h>
#endif

namespace at {

class Tensor;

namespace native {

template<typename O, typename C>
void _assert_match(const O& original, const C& compared, const std::string& name) {
  if (compared) {
    bool equal = (original == compared.value());
    if (!equal) {
      std::stringstream msg;
      msg << "Tensor " << name << " mismatch!";
      AT_ASSERT(equal, msg.str());
    }
  }
}

void _assert_tensor_metadata(at::Tensor const& tensor, at::OptionalIntArrayRef sizes, at::OptionalIntArrayRef strides, std::optional<c10::ScalarType> dtype) {
  _assert_match(tensor.sizes(), sizes, "sizes");
  _assert_match(tensor.strides(), strides, "strides");
  _assert_match(tensor.dtype(), dtype, "dtype");
}

}
}  // namespace at::native
