#include <gtest/gtest.h>

#include <ATen/ATen.h>
#include <ATen/cuda/CUDAContext.h>
#include <optional>

#include <assert.h>

using namespace at;

// optional in cuda files
TEST(OptionalTest, OptionalTestCUDA) {
  if (!at::cuda::is_available()) return;
  std::optional<int64_t> trivially_destructible;
  std::optional<std::vector<int64_t>> non_trivially_destructible;
  ASSERT_FALSE(trivially_destructible.has_value());
  ASSERT_FALSE(non_trivially_destructible.has_value());

  trivially_destructible = {5};
  non_trivially_destructible = std::vector<int64_t>{5, 10};
  ASSERT_TRUE(trivially_destructible.has_value());
  ASSERT_TRUE(non_trivially_destructible.has_value());
}
