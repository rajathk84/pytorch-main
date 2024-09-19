#include <gtest/gtest.h>

#include <torch/special.h>
#include <torch/torch.h>

#include <test/cpp/api/support.h>

// Simple test that verifies the special namespace is registered properly
//   properly in C++
TEST(SpecialTest, special) {
  auto t = torch::randn(128, torch::kDouble);
  torch::special::gammaln(t);
}
