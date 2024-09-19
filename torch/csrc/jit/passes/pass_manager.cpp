#include <torch/csrc/jit/passes/pass_manager.h>

namespace torch::jit {

// Start UUID at 1
static GraphPassNameType graphPassID = 1;

std::vector<GraphPassEntry>& getCustomPostPasses() {
  static std::vector<GraphPassEntry> passes;
  return passes;
}

std::vector<GraphPassEntry>& getCustomPrePasses() {
  static std::vector<GraphPassEntry> passes;
  return passes;
}

GraphPassNameType registerPostPass(GraphPass p) {
  getCustomPostPasses().emplace_back(std::move(p), graphPassID);
  return graphPassID++;
}

static GraphPassNameType registerPass(GraphPass p) {
  return registerPostPass(std::move(p));
}

GraphPassNameType registerPrePass(GraphPass p) {
  getCustomPrePasses().emplace_back(std::move(p), graphPassID);
  return graphPassID++;
}

void clearPostPass(GraphPassNameType pid) {
  auto& passes = getCustomPostPasses();
  auto it = passes.begin();
  for (; it != passes.end(); it++) {
    if (pid == (*it).second)
      break;
  }
  if (it != passes.end())
    passes.erase(it);
}

void clearPrePass(GraphPassNameType pid) {
  auto& passes = getCustomPrePasses();
  auto it = passes.begin();
  for (; it != passes.end(); it++) {
    if (pid == (*it).second)
      break;
  }
  if (it != passes.end())
    passes.erase(it);
}

void clearAllPostPasses() {
  auto& passes = getCustomPostPasses();
  passes.erase(passes.begin(), passes.end());
}

void clearAllPrePasses() {
  auto& passes = getCustomPrePasses();
  passes.erase(passes.begin(), passes.end());
}

// LEGACY CALL
RegisterPostPass::RegisterPostPass(GraphPass p) {
  registerPass(std::move(p));
}

} // namespace torch::jit
