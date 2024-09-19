#include <c10/macros/Macros.h>
#include <torch/csrc/jit/backends/backend_debug_info.h>

namespace torch {
namespace jit {
namespace backend {
namespace {
#ifdef BUILD_LITE_INTERPRETER
static auto cls = torch::class_<PyTorchBackendDebugInfoDummy>(
                      kBackendUtilsNamespace,
                      kBackendDebugInfoClass)
                      .def(torch::init<>());
#else
static auto cls = torch::class_<PyTorchBackendDebugInfo>(
                      kBackendUtilsNamespace,
                      kBackendDebugInfoClass)
                      .def(torch::init<>());
#endif

} // namespace
} // namespace backend
} // namespace jit
} // namespace torch
