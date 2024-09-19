#include <optional>

#include <gmock/gmock.h>
#include <gtest/gtest.h>

#include <array>
#include <cstdint>
#include <string>

#include <c10/util/ArrayRef.h>

namespace {

using testing::Eq;
using testing::Ge;
using testing::Gt;
using testing::Le;
using testing::Lt;
using testing::Ne;
using testing::Not;

template <typename T>
class OptionalTest : public ::testing::Test {
 public:
  using optional = std::optional<T>;
};

template <typename T>
T getSampleValue();

template <>
bool getSampleValue() {
  return true;
}

template <>
uint64_t getSampleValue() {
  return 42;
}

template <>
c10::IntArrayRef getSampleValue() {
  return {};
}

template <>
std::string getSampleValue() {
  return "hello";
}

using OptionalTypes = ::testing::Types<
    // 32-bit scalar optimization.
    bool,
    // Trivially destructible but not 32-bit scalar.
    uint64_t,
    // ArrayRef optimization.
    c10::IntArrayRef,
    // Non-trivial destructor.
    std::string>;

TYPED_TEST_SUITE(OptionalTest, OptionalTypes);

TYPED_TEST(OptionalTest, Empty) {
  typename TestFixture::optional empty;

  EXPECT_FALSE((bool)empty);
  EXPECT_FALSE(empty.has_value());

  // NOLINTNEXTLINE(bugprone-unchecked-optional-access,hicpp-avoid-goto,cppcoreguidelines-avoid-goto)
  EXPECT_THROW(empty.value(), std::bad_optional_access);
}

TYPED_TEST(OptionalTest, Initialized) {
  using optional = typename TestFixture::optional;

  const auto val = getSampleValue<TypeParam>();
  optional opt((val));
  auto copy(opt), moveFrom1(opt), moveFrom2(opt);
  optional move(std::move(moveFrom1));
  optional copyAssign;
  copyAssign = opt;
  optional moveAssign;
  moveAssign = std::move(moveFrom2);

  std::array<typename TestFixture::optional*, 5> opts = {
      &opt, &copy, &copyAssign, &move, &moveAssign};
  for (auto* popt : opts) {
    auto& opt = *popt;
    EXPECT_TRUE((bool)opt);
    EXPECT_TRUE(opt.has_value());

    // NOLINTNEXTLINE(bugprone-unchecked-optional-access)
    EXPECT_EQ(opt.value(), val);
    // NOLINTNEXTLINE(bugprone-unchecked-optional-access)
    EXPECT_EQ(*opt, val);
  }
}

class SelfCompareTest : public testing::TestWithParam<std::optional<int>> {};

TEST_P(SelfCompareTest, SelfCompare) {
  std::optional<int> x = GetParam();
  EXPECT_THAT(x, Eq(x));
  EXPECT_THAT(x, Le(x));
  EXPECT_THAT(x, Ge(x));
  EXPECT_THAT(x, Not(Ne(x)));
  EXPECT_THAT(x, Not(Lt(x)));
  EXPECT_THAT(x, Not(Gt(x)));
}

INSTANTIATE_TEST_SUITE_P(
    nullopt,
    SelfCompareTest,
    testing::Values(std::nullopt));
INSTANTIATE_TEST_SUITE_P(
    int,
    SelfCompareTest,
    testing::Values(std::make_optional(2)));

TEST(OptionalTest, Nullopt) {
  std::optional<int> x = 2;

  EXPECT_THAT(std::nullopt, Not(Eq(x)));
  EXPECT_THAT(x, Not(Eq(std::nullopt)));

  EXPECT_THAT(x, Ne(std::nullopt));
  EXPECT_THAT(std::nullopt, Ne(x));

  EXPECT_THAT(x, Not(Lt(std::nullopt)));
  EXPECT_THAT(std::nullopt, Lt(x));

  EXPECT_THAT(x, Not(Le(std::nullopt)));
  EXPECT_THAT(std::nullopt, Le(x));

  EXPECT_THAT(x, Gt(std::nullopt));
  EXPECT_THAT(std::nullopt, Not(Gt(x)));

  EXPECT_THAT(x, Ge(std::nullopt));
  EXPECT_THAT(std::nullopt, Not(Ge(x)));
}

// Ensure comparisons work...
using CmpTestTypes = testing::Types<
    // between two optionals
    std::pair<std::optional<int>, std::optional<int>>,

    // between an optional and a value
    std::pair<std::optional<int>, int>,
    // between a value and an optional
    std::pair<int, std::optional<int>>,

    // between an optional and a differently typed value
    std::pair<std::optional<int>, long>,
    // between a differently typed value and an optional
    std::pair<long, std::optional<int>>>;
template <typename T>
class CmpTest : public testing::Test {};
TYPED_TEST_SUITE(CmpTest, CmpTestTypes);

TYPED_TEST(CmpTest, Cmp) {
  TypeParam pair = {2, 3};
  auto x = pair.first;
  auto y = pair.second;

  EXPECT_THAT(x, Not(Eq(y)));

  EXPECT_THAT(x, Ne(y));

  EXPECT_THAT(x, Lt(y));
  EXPECT_THAT(y, Not(Lt(x)));

  EXPECT_THAT(x, Le(y));
  EXPECT_THAT(y, Not(Le(x)));

  EXPECT_THAT(x, Not(Gt(y)));
  EXPECT_THAT(y, Gt(x));

  EXPECT_THAT(x, Not(Ge(y)));
  EXPECT_THAT(y, Ge(x));
}

} // namespace
