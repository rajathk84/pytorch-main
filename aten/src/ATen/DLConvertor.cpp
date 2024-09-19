#include <ATen/DLConvertor.h>
#include <ATen/Functions.h>

using namespace std;
namespace at {

DLDataType getDLDataType(const Tensor& t) {
  DLDataType dtype;
  dtype.lanes = 1;
  dtype.bits = t.element_size() * 8;
  switch (t.scalar_type()) {
    case ScalarType::UInt1:
    case ScalarType::UInt2:
    case ScalarType::UInt3:
    case ScalarType::UInt4:
    case ScalarType::UInt5:
    case ScalarType::UInt6:
    case ScalarType::UInt7:
    case ScalarType::Byte:
    case ScalarType::UInt16:
    case ScalarType::UInt32:
    case ScalarType::UInt64:
      dtype.code = DLDataTypeCode::kDLUInt;
      break;
    case ScalarType::Char:
      dtype.code = DLDataTypeCode::kDLInt;
      break;
    // NOLINTNEXTLINE(bugprone-branch-clone)
    case ScalarType::Double:
      dtype.code = DLDataTypeCode::kDLFloat;
      break;
    case ScalarType::Float:
      dtype.code = DLDataTypeCode::kDLFloat;
      break;
    // NOLINTNEXTLINE(bugprone-branch-clone)
    case ScalarType::Int:
      dtype.code = DLDataTypeCode::kDLInt;
      break;
    case ScalarType::Long:
      dtype.code = DLDataTypeCode::kDLInt;
      break;
    case ScalarType::Short:
      dtype.code = DLDataTypeCode::kDLInt;
      break;
    case ScalarType::Half:
      dtype.code = DLDataTypeCode::kDLFloat;
      break;
    case ScalarType::Bool:
      dtype.code = DLDataTypeCode::kDLBool;
      break;
    case ScalarType::ComplexHalf:
      dtype.code = DLDataTypeCode::kDLComplex;
      break;
    case ScalarType::ComplexFloat:
      dtype.code = DLDataTypeCode::kDLComplex;
      break;
    case ScalarType::ComplexDouble:
      dtype.code = DLDataTypeCode::kDLComplex;
      break;
    case ScalarType::BFloat16:
      dtype.code = DLDataTypeCode::kDLBfloat;
      break;
    case ScalarType::Float8_e5m2:
    case ScalarType::Float8_e5m2fnuz:
    case ScalarType::Float8_e4m3fn:
    case ScalarType::Float8_e4m3fnuz:
      TORCH_CHECK(false, "float8 types are not supported by dlpack");
      break;
    case ScalarType::QInt8:
    case ScalarType::QUInt8:
    case ScalarType::QInt32:
    case ScalarType::QUInt4x2:
    case ScalarType::QUInt2x4:
      TORCH_CHECK(false, "QUInt/QInt types are not supported by dlpack");
      break;
    case ScalarType::Bits1x8:
    case ScalarType::Bits2x4:
    case ScalarType::Bits4x2:
    case ScalarType::Bits8:
    case ScalarType::Bits16:
      TORCH_CHECK(false, "Bit types are not supported by dlpack");
      break;
    case ScalarType::Undefined:
      TORCH_CHECK(false, "Undefined is not a valid ScalarType");
    case ScalarType::NumOptions:
      TORCH_CHECK(false, "NumOptions is not a valid ScalarType");
  }
  return dtype;
}

static DLDevice getDLDevice(const Tensor& tensor, c10::DeviceIndex device_id) {
  DLDevice ctx;
  ctx.device_id = static_cast<int32_t>(device_id);
  switch (tensor.device().type()) {
    case DeviceType::CPU:
      ctx.device_type = DLDeviceType::kDLCPU;
      break;
    case DeviceType::CUDA:
#ifdef USE_ROCM
      // ROCM, if enabled will look like cuda to PyTorch
      // while everyone else should see HIP
      ctx.device_type = DLDeviceType::kDLROCM;
#else
      ctx.device_type = DLDeviceType::kDLCUDA;
#endif
      break;
    case DeviceType::OPENCL:
      ctx.device_type = DLDeviceType::kDLOpenCL;
      break;
    case DeviceType::HIP:
      ctx.device_type = DLDeviceType::kDLROCM;
      break;
    case DeviceType::XPU:
      ctx.device_type = DLDeviceType::kDLOneAPI;
      ctx.device_id =
          at::detail::getXPUHooks().getGlobalIdxFromDevice(tensor.device());
      break;
    case DeviceType::MAIA:
      ctx.device_type = DLDeviceType::kDLMAIA;
      break;
    default:
      TORCH_CHECK(false, "Cannot pack tensors on " + tensor.device().str());
  }
  return ctx;
}

static Device getATenDevice(const DLDevice& ctx, void* data) {
  switch (ctx.device_type) {
    case DLDeviceType::kDLCPU:
      return at::Device(DeviceType::CPU);
#ifndef USE_ROCM
    // if we are compiled under HIP, we cannot do cuda
    case DLDeviceType::kDLCUDA:
      return at::Device(DeviceType::CUDA, static_cast<c10::DeviceIndex>(ctx.device_id));
#endif
    case DLDeviceType::kDLOpenCL:
      return at::Device(DeviceType::OPENCL, static_cast<c10::DeviceIndex>(ctx.device_id));
    case DLDeviceType::kDLROCM:
#ifdef USE_ROCM
      // this looks funny, we need to return CUDA here to masquerade
      return at::Device(DeviceType::CUDA, static_cast<c10::DeviceIndex>(ctx.device_id));
#else
      return at::Device(DeviceType::HIP, static_cast<c10::DeviceIndex>(ctx.device_id));
#endif
    case DLDeviceType::kDLOneAPI:
      return at::detail::getXPUHooks().getDeviceFromPtr(data);
    case DLDeviceType::kDLMAIA:
      return at::Device(DeviceType::MAIA, static_cast<c10::DeviceIndex>(ctx.device_id));
    default:
      TORCH_CHECK(
          false, "Unsupported device_type: ", std::to_string(ctx.device_type));
  }
}

ScalarType toScalarType(const DLDataType& dtype) {
  ScalarType stype = ScalarType::Undefined;
  TORCH_CHECK(dtype.lanes == 1, "ATen does not support lanes != 1");
  switch (dtype.code) {
    case DLDataTypeCode::kDLUInt:
      switch (dtype.bits) {
        case 8:
          stype = ScalarType::Byte;
          break;
        case 16:
          stype = ScalarType::UInt16;
          break;
        case 32:
          stype = ScalarType::UInt32;
          break;
        case 64:
          stype = ScalarType::UInt64;
          break;
        default:
          TORCH_CHECK(
              false, "Unsupported kUInt bits ", std::to_string(dtype.bits));
      }
      break;
    case DLDataTypeCode::kDLInt:
      switch (dtype.bits) {
        case 8:
          stype = ScalarType::Char;
          break;
        case 16:
          stype = ScalarType::Short;
          break;
        case 32:
          stype = ScalarType::Int;
          break;
        case 64:
          stype = ScalarType::Long;
          break;
        default:
          TORCH_CHECK(
              false, "Unsupported kInt bits ", std::to_string(dtype.bits));
      }
      break;
    case DLDataTypeCode::kDLFloat:
      switch (dtype.bits) {
        case 16:
          stype = ScalarType::Half;
          break;
        case 32:
          stype = ScalarType::Float;
          break;
        case 64:
          stype = ScalarType::Double;
          break;
        default:
          TORCH_CHECK(
              false, "Unsupported kFloat bits ", std::to_string(dtype.bits));
      }
      break;
    case DLDataTypeCode::kDLBfloat:
      switch (dtype.bits) {
        case 16:
          stype = ScalarType::BFloat16;
          break;
        default:
          TORCH_CHECK(
              false, "Unsupported kFloat bits ", std::to_string(dtype.bits));
      }
      break;
    case DLDataTypeCode::kDLComplex:
      switch (dtype.bits) {
        case 32:
          stype = ScalarType::ComplexHalf;
          break;
        case 64:
          stype = ScalarType::ComplexFloat;
          break;
        case 128:
          stype = ScalarType::ComplexDouble;
          break;
        default:
          TORCH_CHECK(
              false, "Unsupported kFloat bits ", std::to_string(dtype.bits));
      }
      break;
    case DLDataTypeCode::kDLBool:
      switch (dtype.bits) {
        case 8:
          stype = ScalarType::Bool;
          break;
        default:
          TORCH_CHECK(
              false, "Unsupported kDLBool bits ", std::to_string(dtype.bits));
      }
      break;
    default:
      TORCH_CHECK(false, "Unsupported code ", std::to_string(dtype.code));
  }
  return stype;
}

// NOLINTNEXTLINE(cppcoreguidelines-pro-type-member-init)
struct ATenDLMTensor {
  Tensor handle;
  DLManagedTensor tensor;
};

static void deleter(DLManagedTensor* arg) {
  delete static_cast<ATenDLMTensor*>(arg->manager_ctx);
}

// This function returns a shared_ptr to memory managed DLpack tensor
// constructed out of ATen tensor
DLManagedTensor* toDLPack(const Tensor& src) {
  // create a new tensor with possibly normalized strides
  // gh-83069
  auto shape = src.sizes();
  auto strides = src.strides().vec();
  for (int i = 0; i < src.dim(); i++) {
    if (shape[i] < 2) {
      strides[i] = 1;
    }
  }

  auto view = src.as_strided(shape, strides, src.storage_offset());
  ATenDLMTensor* atDLMTensor(new ATenDLMTensor);
  atDLMTensor->handle = view;
  atDLMTensor->tensor.manager_ctx = atDLMTensor;
  atDLMTensor->tensor.deleter = &deleter;
  atDLMTensor->tensor.dl_tensor.data = view.data_ptr();
  c10::DeviceIndex device_id = 0;
  if (src.is_cuda()) {
    device_id = src.get_device();
  }
  atDLMTensor->tensor.dl_tensor.device = getDLDevice(src, device_id);
  atDLMTensor->tensor.dl_tensor.ndim = static_cast<int32_t>(src.dim());
  atDLMTensor->tensor.dl_tensor.dtype = getDLDataType(src);
  atDLMTensor->tensor.dl_tensor.shape = view.sizes().data();
  atDLMTensor->tensor.dl_tensor.strides = view.strides().data();
  atDLMTensor->tensor.dl_tensor.byte_offset = 0;
  return &(atDLMTensor->tensor);
}

Tensor fromDLPack(DLManagedTensor* src) {
  auto deleter = [src](void* self [[maybe_unused]]) {
    if (src->deleter) {
      src->deleter(src);
    }
  };
  return fromDLPack(src, std::move(deleter));
}

Tensor fromDLPack(DLManagedTensor* src, std::function<void(void*)> deleter) {
  Device device = getATenDevice(src->dl_tensor.device, src->dl_tensor.data);
  ScalarType stype = toScalarType(src->dl_tensor.dtype);
  if (!src->dl_tensor.strides) {
    return at::from_blob(
        src->dl_tensor.data,
        IntArrayRef(src->dl_tensor.shape, src->dl_tensor.ndim),
        std::move(deleter),
        at::device(device).dtype(stype),
        {device});
  }
  return at::from_blob(
      src->dl_tensor.data,
      IntArrayRef(src->dl_tensor.shape, src->dl_tensor.ndim),
      IntArrayRef(src->dl_tensor.strides, src->dl_tensor.ndim),
      deleter,
      at::device(device).dtype(stype),
      {device});
}
} // namespace at
