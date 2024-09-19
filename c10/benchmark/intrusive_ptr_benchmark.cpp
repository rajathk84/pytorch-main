#include <c10/util/intrusive_ptr.h>
#include <c10/util/irange.h>

#include <benchmark/benchmark.h>
#include <memory>

using c10::intrusive_ptr;
using c10::intrusive_ptr_target;
using c10::make_intrusive;

namespace {

// Foo uses intrusive ptr
class Foo : public intrusive_ptr_target {
 public:
  Foo(int param_) : param(param_) {}
  int param;
};

class Bar : public std::enable_shared_from_this<Bar> {
 public:
  Bar(int param_) : param(param_) {}
  int param;
};

static void BM_IntrusivePtrCtorDtor(benchmark::State& state) {
  intrusive_ptr<Foo> var = make_intrusive<Foo>(0);
  while (state.KeepRunning()) {
    // NOLINTNEXTLINE(performance-unnecessary-copy-initialization)
    volatile intrusive_ptr<Foo> var2 = var;
  }
}
BENCHMARK(BM_IntrusivePtrCtorDtor);

static void BM_SharedPtrCtorDtor(benchmark::State& state) {
  std::shared_ptr<Bar> var = std::make_shared<Bar>(0);
  while (state.KeepRunning()) {
    // NOLINTNEXTLINE(performance-unnecessary-copy-initialization)
    volatile std::shared_ptr<Bar> var2 = var;
  }
}
BENCHMARK(BM_SharedPtrCtorDtor);

static void BM_IntrusivePtrArray(benchmark::State& state) {
  intrusive_ptr<Foo> var = make_intrusive<Foo>(0);
  const size_t kLength = state.range(0);
  std::vector<intrusive_ptr<Foo>> vararray(kLength);
  while (state.KeepRunning()) {
    for (const auto i : c10::irange(kLength)) {
      vararray[i] = var;
    }
    for (const auto i : c10::irange(kLength)) {
      vararray[i].reset();
    }
  }
}
// NOLINTNEXTLINE(cppcoreguidelines-avoid-non-const-global-variables,cppcoreguidelines-avoid-magic-numbers)
BENCHMARK(BM_IntrusivePtrArray)->RangeMultiplier(2)->Range(16, 4096);

static void BM_SharedPtrArray(benchmark::State& state) {
  std::shared_ptr<Bar> var = std::make_shared<Bar>(0);
  const size_t kLength = state.range(0);
  std::vector<std::shared_ptr<Bar>> vararray(kLength);
  while (state.KeepRunning()) {
    for (const auto i : c10::irange(kLength)) {
      vararray[i] = var;
    }
    for (const auto i : c10::irange(kLength)) {
      vararray[i].reset();
    }
  }
}
// NOLINTNEXTLINE(cppcoreguidelines-avoid-non-const-global-variables,cppcoreguidelines-avoid-magic-numbers)
BENCHMARK(BM_SharedPtrArray)->RangeMultiplier(2)->Range(16, 4096);

static void BM_IntrusivePtrExclusiveOwnership(benchmark::State& state) {
  while (state.KeepRunning()) {
    volatile auto var = make_intrusive<Foo>(0);
  }
}
BENCHMARK(BM_IntrusivePtrExclusiveOwnership);

static void BM_SharedPtrExclusiveOwnership(benchmark::State& state) {
  while (state.KeepRunning()) {
    volatile auto var = std::make_shared<Foo>(0);
  }
}
BENCHMARK(BM_SharedPtrExclusiveOwnership);

} // namespace

BENCHMARK_MAIN();
