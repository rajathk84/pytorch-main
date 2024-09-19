// This is a simple predictor binary that loads a TorchScript CV model and runs
// a forward pass with fixed input `torch::ones({1, 3, 224, 224})`.
// It's used for end-to-end integration test for custom mobile build.

#include <iostream>
#include <string>
#include <c10/util/irange.h>
#include <torch/script.h>

using namespace std;

namespace {

struct MobileCallGuard {
  // Set InferenceMode for inference only use case.
  c10::InferenceMode guard;
  // Disable graph optimizer to ensure list of unused ops are not changed for
  // custom mobile build.
  torch::jit::GraphOptimizerEnabledGuard no_optimizer_guard{false};
};

torch::jit::Module loadModel(const std::string& path) {
  MobileCallGuard guard;
  auto module = torch::jit::load(path);
  module.eval();
  return module;
}

} // namespace

int main(int argc, const char* argv[]) {
  if (argc < 2) {
    std::cerr << "Usage: " << argv[0] << " <model_path>\n";
    return 1;
  }
  auto module = loadModel(argv[1]);
  auto input = torch::ones({1, 3, 224, 224});
  auto output = [&]() {
    MobileCallGuard guard;
    return module.forward({input}).toTensor();
  }();

  std::cout << std::setprecision(3) << std::fixed;
  for (const auto i : c10::irange(5)) {
    std::cout << output.data_ptr<float>()[i] << std::endl;
  }
  return 0;
}
