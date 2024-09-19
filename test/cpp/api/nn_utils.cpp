#include <gtest/gtest.h>

#include <c10/util/irange.h>
#include <torch/torch.h>

#include <test/cpp/api/support.h>

#include <algorithm>
#include <iostream>
#include <random>
#include <sstream>
#include <string>

using namespace torch::nn;

namespace rnn_utils = torch::nn::utils::rnn;

struct NNUtilsTest : torch::test::SeedingFixture {};
struct PackedSequenceTest : torch::test::SeedingFixture {};

TEST_F(NNUtilsTest, ClipGradNorm) {
  auto l = Linear(10, 10);
  float max_norm = 2;
  auto compute_norm = [&](float norm_type) -> float {
    float total_norm = 0.0;
    if (norm_type != std::numeric_limits<float>::infinity()) {
      for (const auto& p : l->parameters()) {
        total_norm +=
            p.grad().data().abs().pow(norm_type).sum().item().toFloat();
      }
      return std::pow(total_norm, 1.0 / norm_type);
    } else {
      for (const auto& p : l->parameters()) {
        auto param_max = p.grad().data().abs().max().item().toFloat();
        if (param_max > total_norm) {
          total_norm = param_max;
        }
      }
      return total_norm;
    }
  };
  auto compare_scaling =
      [&](const std::vector<torch::Tensor>& grads) -> torch::Tensor {
    std::vector<torch::Tensor> p_scale;
    for (const auto i : c10::irange(grads.size())) {
      auto param = l->parameters()[i];
      auto grad = grads[i];
      p_scale.push_back(param.grad().data().div(grad).view(-1));
    }
    auto scale = torch::cat(p_scale);
    return scale; // need to assert std is 0.
  };

  std::vector<torch::Tensor> grads = {
      torch::arange(1.0, 101).view({10, 10}),
      torch::ones({10}).div(1000),
  };
  std::vector<float> norm_types = {
      0.5,
      1.5,
      2.0,
      4.0,
      std::numeric_limits<float>::infinity(),
  };
  for (auto norm_type : norm_types) {
    for (const auto i : c10::irange(grads.size())) {
      l->parameters()[i].mutable_grad() =
          grads[i].clone().view_as(l->parameters()[i].data());
    }
    auto norm_before = compute_norm(norm_type);
    auto norm = utils::clip_grad_norm_(l->parameters(), max_norm, norm_type);
    auto norm_after = compute_norm(norm_type);
    ASSERT_FLOAT_EQ(norm, norm_before);
    ASSERT_NEAR(norm_after, max_norm, 1e-6);
    ASSERT_LE(norm_after, max_norm);
    auto scaled = compare_scaling(grads);
    ASSERT_NEAR(0, scaled.std().item().toFloat(), 1e-7);
  }
  // Small gradients should be left unchanged
  grads = {
      torch::rand({10, 10}).div(10000),
      torch::ones(10).div(500),
  };
  for (auto norm_type : norm_types) {
    for (const auto i : c10::irange(grads.size())) {
      l->parameters()[i].grad().data().copy_(grads[i]);
    }
    auto norm_before = compute_norm(norm_type);
    auto norm = utils::clip_grad_norm_(l->parameters(), max_norm, norm_type);
    auto norm_after = compute_norm(norm_type);
    ASSERT_FLOAT_EQ(norm, norm_before);
    ASSERT_FLOAT_EQ(norm_before, norm_after);
    ASSERT_LE(norm_after, max_norm);
    auto scaled = compare_scaling(grads);
    ASSERT_NEAR(0, scaled.std().item().toFloat(), 1e-7);
    ASSERT_FLOAT_EQ(scaled[0].item().toFloat(), 1);
  }
  // should accept a single tensor as input
  auto p1 = torch::randn({10, 10});
  auto p2 = torch::randn({10, 10});
  auto g = torch::arange(1., 101).view({10, 10});
  p1.mutable_grad() = g.clone();
  p2.mutable_grad() = g.clone();
  for (const auto norm_type : norm_types) {
    utils::clip_grad_norm_(p1, max_norm, norm_type);
    utils::clip_grad_norm_({p2}, max_norm, norm_type);
    ASSERT_TRUE(torch::allclose(p1.grad(), p2.grad()));
  }
}

// Check that clip_grad_norm_ raises an error if the norm of a gradient
// is non-finite
TEST_F(NNUtilsTest, ClipGradNormErrorIfNonfinite) {
  double inf = std::numeric_limits<double>::infinity();
  double nan = std::numeric_limits<double>::quiet_NaN();

  using Vector = std::vector<double>;

  Vector norms_pos = {0.1, 1, 2, 3.5, inf};
  Vector norms_neg = {-0.1, -1, -2, -3.5};
  Vector norms_neg_plus_0 = {0, -0.1, -1, -2, -3.5};
  Vector norms_except_0 = {0.1, 1, 2, 3.5, inf, -0.1, -1, -2, -3.5};
  Vector norms_all = {0, 0.1, 1, 2, 3.5, inf, -0.1, -1, -2, -3.5};

  // Each entry in test_cases has the following values, in this order:
  //
  // grad_only_one_elem    If True, only one element of the parameter's
  //                       gradient is set to the scalar grad, and the
  //                       rest of the elements are 0. If False, all grad
  //                       elements are equal to the scalar.
  //
  // prefix_finite_grad_param  If True, prefix a parameter that has a grad
  //                           of 1.
  //
  // scalars           Scalars to use as the parameter's grad, through
  //                   multiplication
  //
  // norms_nonfinite   Norm types that should produce nonfinite total norm
  //
  // norms_finite      Norm types that should produce finite total norm
  std::vector<std::tuple<bool, bool, Vector, Vector, Vector>> test_cases({
      // Test errors from an infinite grad
      std::make_tuple(
          false, false, Vector({inf, -inf}), norms_except_0, Vector({0})),
      std::make_tuple(
          false, true, Vector({inf, -inf}), norms_pos, norms_neg_plus_0),
      std::make_tuple(
          true, false, Vector({inf, -inf}), norms_pos, norms_neg_plus_0),
      std::make_tuple(
          false, true, Vector({inf, -inf}), norms_pos, norms_neg_plus_0),

      // Test errors from a NaN grad
      std::make_tuple(false, false, Vector({nan}), norms_except_0, Vector({0})),
      std::make_tuple(false, true, Vector({nan}), norms_except_0, Vector({0})),
      std::make_tuple(true, false, Vector({nan}), norms_except_0, Vector({0})),
      std::make_tuple(true, true, Vector({nan}), norms_except_0, Vector({0})),

      // Test a grad that should never error
      std::make_tuple(false, false, Vector({2e22, -2e22}), Vector(), norms_all),
      std::make_tuple(false, true, Vector({2e22, -2e22}), Vector(), norms_all),
      std::make_tuple(true, false, Vector({2e22, -2e22}), Vector(), norms_all),
      std::make_tuple(true, true, Vector({2e22, -2e22}), Vector(), norms_all),

      // Test a grad that will overflow to inf for only some norm orders
      std::make_tuple(
          false,
          false,
          Vector({2e200, -2e200}),
          Vector({3.5, 2, -2, -3.5}),
          Vector({inf, 1, 0.1, 0, -1, -0.1})),
      std::make_tuple(
          false,
          true,
          Vector({2e200, -2e200}),
          Vector({3.5, 2}),
          Vector({inf, 1, 0.1, 0, -1, -0.1, -2, -3.5})),
      std::make_tuple(
          true,
          false,
          Vector({2e200, -2e200}),
          Vector({3.5, 2}),
          Vector({inf, 1, 0.1, 0, -1, -0.1, -2, -3.5})),
      std::make_tuple(
          false,
          true,
          Vector({2e200, -2e200}),
          Vector({3.5, 2}),
          Vector({inf, 1, 0.1, 0, -1, -0.1, -2, -3.5})),
  });

  auto gen_parameters = [](double scalar,
                           bool grad_only_one_elem,
                           bool prefix_finite_grad_param,
                           torch::DeviceType device_type) {
    auto param = torch::ones(
        10,
        torch::TensorOptions()
            .dtype(torch::kDouble)
            .device(device_type)
            .requires_grad(true));
    if (grad_only_one_elem) {
      param[1].mul(scalar).sum().backward();
    } else {
      param.mul(scalar).sum().backward();
    }

    std::vector<torch::Tensor> parameters;
    if (prefix_finite_grad_param) {
      auto prefix_param = torch::ones(
          1,
          torch::TensorOptions()
              .dtype(torch::kDouble)
              .device(device_type)
              .requires_grad(true));
      prefix_param.mul(1).sum().backward();
      parameters.push_back(prefix_param);
    }
    parameters.push_back(param);

    return parameters;
  };

  auto run_test_case = [&gen_parameters](
                           double norm_type,
                           bool error_if_nonfinite,
                           double scalar,
                           bool grad_only_one_elem,
                           bool prefix_finite_grad_param,
                           bool is_norm_nonfinite,
                           torch::DeviceType device_type) {
    std::stringstream ss;
    ss << "device: " << device_type << ", norm_type: " << norm_type
       << ", error_if_nonfinite: " << error_if_nonfinite
       << ", scalar: " << scalar
       << ", grad_only_one_elem: " << grad_only_one_elem
       << ", prefix_finite_grad_param: " << prefix_finite_grad_param
       << ", is_norm_nonfinite: " << is_norm_nonfinite;
    std::string msg = ss.str();

    auto parameters = gen_parameters(
        scalar, grad_only_one_elem, prefix_finite_grad_param, device_type);

    if (is_norm_nonfinite && error_if_nonfinite) {
      std::vector<torch::Tensor> grads_before;
      // NOLINTNEXTLINE(performance-for-range-copy)
      for (auto p : parameters) {
        // NOLINTNEXTLINE(performance-inefficient-vector-operation)
        grads_before.push_back(p.grad().clone());
      }
      // NOLINTNEXTLINE(cppcoreguidelines-avoid-goto,hicpp-avoid-goto)
      EXPECT_THROW(
          utils::clip_grad_norm_(parameters, 1., norm_type, true),
          std::exception)
          << msg;
      // Grads should not change if error is thrown
      for (const auto p_idx : c10::irange(parameters.size())) {
        ASSERT_TRUE(torch::allclose(
            parameters[p_idx].grad(),
            grads_before[p_idx],
            1.0,
            0.0,
            /*equal_nan*/ true))
            << msg;
      }
    } else {
      // NOLINTNEXTLINE(cppcoreguidelines-avoid-goto,hicpp-avoid-goto)
      EXPECT_NO_THROW(
          utils::clip_grad_norm_(parameters, 1., norm_type, error_if_nonfinite))
          << msg;
    }
  };

  for (auto device_type : {torch::kCPU, torch::kCUDA}) {
    if (device_type == torch::kCUDA && !torch::cuda::is_available()) {
      continue;
    }
    for (auto test_case : test_cases) {
      auto grad_only_one_elem = std::get<0>(test_case);
      auto prefix_finite_grad_param = std::get<1>(test_case);
      auto scalars = std::get<2>(test_case);
      auto norms_nonfinite = std::get<3>(test_case);
      auto norms_finite = std::get<4>(test_case);

      for (auto error_if_nonfinite : {false, true}) {
        for (auto scalar : scalars) {
          for (auto norm_type : norms_nonfinite) {
            run_test_case(
                norm_type,
                error_if_nonfinite,
                scalar,
                grad_only_one_elem,
                prefix_finite_grad_param,
                true,
                device_type);
          }

          for (auto norm_type : norms_finite) {
            run_test_case(
                norm_type,
                error_if_nonfinite,
                scalar,
                grad_only_one_elem,
                prefix_finite_grad_param,
                false,
                device_type);
          }
        }
      }
    }
  }
}

TEST_F(NNUtilsTest, ClipGradValue) {
  auto l = Linear(10, 10);
  float clip_value = 2.5;

  torch::Tensor grad_w = torch::arange(-50., 50).view({10, 10}).div_(5);
  torch::Tensor grad_b = torch::ones({10}).mul_(2);
  std::vector<std::vector<torch::Tensor>> grad_lists = {
      {grad_w, grad_b}, {grad_w, torch::Tensor()}};
  for (auto grad_list : grad_lists) {
    for (const auto i : c10::irange(grad_list.size())) {
      auto p = l->parameters()[i];
      auto g = grad_list[i];
      p.mutable_grad() = g.defined() ? g.clone().view_as(p.data()) : g;
    }

    utils::clip_grad_value_(l->parameters(), clip_value);
    for (const auto& p : l->parameters()) {
      if (p.grad().defined()) {
        ASSERT_LE(p.grad().data().max().item().toFloat(), clip_value);
        ASSERT_GE(p.grad().data().min().item().toFloat(), -clip_value);
      }
    }
  }

  // Should accept a single Tensor as input
  auto p1 = torch::randn({10, 10});
  auto p2 = torch::randn({10, 10});
  auto g = torch::arange(-50., 50).view({10, 10}).div_(5);
  p1.mutable_grad() = g.clone();
  p2.mutable_grad() = g.clone();
  utils::clip_grad_value_(p1, clip_value);
  utils::clip_grad_value_({p2}, clip_value);
  ASSERT_TRUE(torch::allclose(p1.grad(), p2.grad()));
}

TEST_F(NNUtilsTest, ConvertParameters) {
  std::vector<torch::Tensor> parameters{
      torch::arange(9, torch::kFloat32),
      torch::arange(9, torch::kFloat32).view({3, 3}),
      torch::arange(8, torch::kFloat32).view({2, 2, 2})};

  auto expected = torch::cat(
      {torch::arange(9, torch::kFloat32),
       torch::arange(9, torch::kFloat32).view(-1),
       torch::arange(8, torch::kFloat32).view(-1)});
  auto vector = utils::parameters_to_vector(parameters);
  ASSERT_TRUE(vector.allclose(expected));

  std::vector<torch::Tensor> zero_parameters{
      torch::zeros({9}, torch::kFloat32),
      torch::zeros({9}, torch::kFloat32).view({3, 3}),
      torch::zeros({8}, torch::kFloat32).view({2, 2, 2})};

  utils::vector_to_parameters(vector, zero_parameters);
  for (const auto i : c10::irange(zero_parameters.size())) {
    ASSERT_TRUE(zero_parameters[i].allclose(parameters[i]));
  }

  {
    auto conv1 = Conv2d(3, 10, 5);
    auto fc1 = Linear(10, 20);
    auto model = Sequential(conv1, fc1);

    auto vec = utils::parameters_to_vector(model->parameters());
    ASSERT_EQ(vec.size(0), 980);
  }
  {
    auto conv1 = Conv2d(3, 10, 5);
    auto fc1 = Linear(10, 20);
    auto model = Sequential(conv1, fc1);

    auto vec = torch::arange(0., 980);
    utils::vector_to_parameters(vec, model->parameters());

    auto sample = model->parameters()[0][0][0][0];
    ASSERT_TRUE(torch::equal(sample.data(), vec.data().slice(0, 0, 5)));
  }
}

// NOLINTNEXTLINE(cppcoreguidelines-avoid-magic-numbers,cppcoreguidelines-avoid-non-const-global-variables)
int64_t PackedSequenceTest_batch_size = 5;
// NOLINTNEXTLINE(cppcoreguidelines-avoid-magic-numbers,cppcoreguidelines-avoid-non-const-global-variables)
int64_t PackedSequenceTest_max_length = 6;

std::vector<torch::Tensor> PackedSequenceTest_ordered_sequence(
    torch::ScalarType tensor_type) {
  std::vector<torch::Tensor> seqs;
  seqs.reserve(PackedSequenceTest_batch_size);
  for (const auto i : c10::irange(PackedSequenceTest_batch_size)) {
    (void)i; // Suppress unused variable warning
    seqs.emplace_back(torch::empty(
        {torch::randint(1, PackedSequenceTest_max_length, {1}).item<int64_t>()},
        tensor_type));
  }
  for (auto& s : seqs) {
    s.random_(-128, 128);
  }
  sort(
      seqs.begin(),
      seqs.end(),
      [&](const torch::Tensor& t1, const torch::Tensor& t2) {
        return t1.size(0) > t2.size(0);
      });
  return seqs;
}

std::tuple<torch::Tensor, torch::Tensor> PackedSequenceTest_padded_sequence(
    torch::ScalarType tensor_type) {
  // Create Tensor of random padded sequences
  auto ordered = PackedSequenceTest_ordered_sequence(tensor_type);
  auto lengths = torch::empty({(int64_t)ordered.size()}, torch::kInt64);
  for (const auto i : c10::irange(ordered.size())) {
    lengths[i] = ordered[i].size(0);
  }
  auto padded_tensor = rnn_utils::pad_sequence(ordered);
  return std::make_tuple(padded_tensor, lengths);
}

void assert_is_equal_packed_sequence(
    const rnn_utils::PackedSequence& a,
    const rnn_utils::PackedSequence& b) {
  ASSERT_TRUE(torch::allclose(a.data(), b.data()));
  ASSERT_TRUE(torch::allclose(a.batch_sizes(), b.batch_sizes()));
  ASSERT_TRUE(
      (!a.sorted_indices().defined() && !b.sorted_indices().defined()) ||
      torch::allclose(a.sorted_indices(), b.sorted_indices()));
  ASSERT_TRUE(
      (!a.unsorted_indices().defined() && !b.unsorted_indices().defined()) ||
      torch::allclose(a.unsorted_indices(), b.unsorted_indices()));
}

void assert_is_same_packed_sequence(
    const rnn_utils::PackedSequence& a,
    const rnn_utils::PackedSequence& b) {
  ASSERT_TRUE(a.data().is_same(b.data()));
  ASSERT_TRUE(a.batch_sizes().is_same(b.batch_sizes()));
  ASSERT_TRUE(a.sorted_indices().is_same(b.sorted_indices()));
  ASSERT_TRUE(a.unsorted_indices().is_same(b.unsorted_indices()));
}

TEST_F(PackedSequenceTest, WrongOrder) {
  auto a = torch::ones({25, 300});
  auto b = torch::ones({22, 300});
  auto b_a = rnn_utils::pad_sequence({b, a});
  // NOLINTNEXTLINE(cppcoreguidelines-avoid-goto,hicpp-avoid-goto)
  ASSERT_THROW(
      rnn_utils::pack_padded_sequence(
          b_a,
          torch::tensor({22, 25}),
          /*batch_first=*/false,
          /*enforce_sorted=*/true),
      c10::Error);
}

TEST_F(PackedSequenceTest, TotalLength) {
  auto [padded, lengths] = PackedSequenceTest_padded_sequence(torch::kFloat);
  int64_t max_length = torch::max(lengths).item<int64_t>();
  rnn_utils::PackedSequence packed =
      rnn_utils::pack_padded_sequence(padded, lengths);

  // test ValueError if total_length < max_length
  for (int64_t total_length : std::vector<int64_t>{-1, 0, max_length - 1}) {
    for (bool batch_first : std::vector<bool>{true, false}) {
      auto err_fn = [&]() {
        rnn_utils::pad_packed_sequence(
            packed,
            /*batch_first=*/batch_first,
            /*padding_value=*/0.0,
            /*total_length=*/total_length);
      };
      ASSERT_THROWS_WITH(
          err_fn(),
          "Expected total_length to be at least the length of the longest sequence in input");
    }
  }

  // test that pad_packed_sequence returns results of correct length
  for (bool batch_first : std::vector<bool>{true, false}) {
    auto no_extra_pad = std::get<0>(
        rnn_utils::pad_packed_sequence(packed, /*batch_first=*/batch_first));
    for (int64_t total_length_delta : std::vector<int64_t>{0, 1, 8}) {
      int64_t total_length = max_length + total_length_delta;
      auto [unpacked, lengths_out] = rnn_utils::pad_packed_sequence(
          packed,
          /*batch_first=*/batch_first,
          /*padding_value=*/0.0,
          /*total_length=*/total_length);
      ASSERT_TRUE(torch::allclose(lengths, lengths_out));
      ASSERT_EQ(unpacked.size(batch_first ? 1 : 0), total_length);
      torch::Tensor ref_output, extra_pad;
      if (total_length_delta == 0) {
        ref_output = no_extra_pad;
      } else if (batch_first) {
        extra_pad = torch::zeros(
            {PackedSequenceTest_batch_size, total_length_delta},
            no_extra_pad.options());
        ref_output = torch::cat({no_extra_pad, extra_pad}, 1);
      } else {
        extra_pad = torch::zeros(
            {total_length_delta, PackedSequenceTest_batch_size},
            no_extra_pad.options());
        ref_output = torch::cat({no_extra_pad, extra_pad}, 0);
      }
      ASSERT_TRUE(torch::allclose(unpacked, ref_output));
    }
  }
}

TEST_F(PackedSequenceTest, To) {
  for (bool enforce_sorted : std::vector<bool>{true, false}) {
    auto [padded, lengths] = PackedSequenceTest_padded_sequence(torch::kInt);
    rnn_utils::PackedSequence a = rnn_utils::pack_padded_sequence(
                                      padded,
                                      lengths,
                                      /*batch_first=*/false,
                                      /*enforce_sorted=*/enforce_sorted)
                                      .cpu();

    assert_is_same_packed_sequence(a, a.to(torch::kCPU));
    assert_is_same_packed_sequence(a, a.cpu());
    assert_is_same_packed_sequence(
        a, a.to(torch::device(torch::kCPU).dtype(torch::kInt32)));

    if (torch::cuda::is_available()) {
      auto b = a.cuda();
      assert_is_same_packed_sequence(b, b.to(torch::kCUDA));
      assert_is_same_packed_sequence(b, b.cuda());
      assert_is_equal_packed_sequence(a, b.to(torch::kCPU));
      assert_is_equal_packed_sequence(b, a.to(torch::kCUDA));
      assert_is_equal_packed_sequence(
          a, b.to(torch::device(torch::kCPU).dtype(torch::kInt32)));
      assert_is_same_packed_sequence(b, b.to(torch::kInt32));
    }
  }
}

TEST_F(NNUtilsTest, PackSequence) {
  auto _compatibility_test = [&](torch::ArrayRef<torch::Tensor> sequences,
                                 torch::Tensor lengths,
                                 bool batch_first,
                                 bool enforce_sorted = false) {
    torch::Tensor padded = rnn_utils::pad_sequence(sequences, batch_first);
    rnn_utils::PackedSequence packed =
        rnn_utils::pack_sequence(sequences, enforce_sorted);
    std::tuple<torch::Tensor, torch::Tensor> unpacked =
        rnn_utils::pad_packed_sequence(packed, batch_first);
    ASSERT_TRUE(torch::allclose(padded, std::get<0>(unpacked)));
    rnn_utils::PackedSequence pack_padded = rnn_utils::pack_padded_sequence(
        padded, lengths, batch_first, enforce_sorted);
    assert_is_equal_packed_sequence(packed, pack_padded);
  };

  // single dimensional
  auto a = torch::tensor({1, 2, 3});
  auto b = torch::tensor({4, 5});
  auto c = torch::tensor({6});
  rnn_utils::PackedSequence packed =
      rnn_utils::pack_sequence({a, b, c}, /*enforce_sorted=*/false);
  auto expected = torch::tensor({1, 4, 6, 2, 5, 3});
  ASSERT_TRUE(torch::allclose(packed.batch_sizes(), torch::tensor({3, 2, 1})));
  ASSERT_TRUE(torch::allclose(packed.data(), expected));
  ASSERT_TRUE(
      torch::allclose(packed.sorted_indices(), torch::tensor({0, 1, 2})));
  ASSERT_TRUE(
      torch::allclose(packed.unsorted_indices(), torch::tensor({0, 1, 2})));

  rnn_utils::PackedSequence packed_unsorted =
      rnn_utils::pack_sequence({b, c, a}, /*enforce_sorted=*/false);
  ASSERT_TRUE(
      torch::allclose(packed_unsorted.batch_sizes(), torch::tensor({3, 2, 1})));
  ASSERT_TRUE(torch::allclose(packed_unsorted.data(), expected));
  ASSERT_TRUE(torch::allclose(
      packed_unsorted.sorted_indices(), torch::tensor({2, 0, 1})));
  ASSERT_TRUE(torch::allclose(
      packed_unsorted.unsorted_indices(), torch::tensor({1, 2, 0})));

  // single dimensional, enforce_sorted = True
  rnn_utils::PackedSequence packed_enforce_sorted =
      rnn_utils::pack_sequence({a, b, c}, /*enforce_sorted=*/true);
  ASSERT_TRUE(torch::allclose(
      packed_enforce_sorted.batch_sizes(), torch::tensor({3, 2, 1})));
  ASSERT_TRUE(torch::allclose(packed_enforce_sorted.data(), expected));
  ASSERT_FALSE(packed_enforce_sorted.sorted_indices().defined());
  ASSERT_FALSE(packed_enforce_sorted.unsorted_indices().defined());

  ASSERT_THROWS_WITH(
      rnn_utils::pack_sequence({b, c, a}, /*enforce_sorted=*/true),
      "must be sorted in decreasing order");

  ASSERT_THROWS_WITH(
      rnn_utils::pack_sequence({b, c, a}, /*enforce_sorted=*/true),
      "You can pass `enforce_sorted=False`");

  // more dimensions
  int64_t maxlen = 9;
  for (int64_t num_dim : std::vector<int64_t>{0, 1, 2, 3}) {
    std::vector<torch::Tensor> sequences;
    std::vector<int64_t> lengths_vec;
    std::vector<int64_t> trailing_dims(num_dim, 4);
    for (int64_t i = maxlen; i > 0; i--) {
      int64_t seq_len = i * i;
      lengths_vec.emplace_back(seq_len);
      std::vector<int64_t> tensor_sizes{seq_len, 5};
      tensor_sizes.insert(
          tensor_sizes.end(), trailing_dims.begin(), trailing_dims.end());
      sequences.emplace_back(torch::rand(tensor_sizes));
    }
    std::vector<torch::Tensor> unsorted_sequences;
    for (const auto& s : sequences) {
      // NOLINTNEXTLINE(performance-inefficient-vector-operation)
      unsorted_sequences.emplace_back(s.clone());
    }
    std::shuffle(
        std::begin(unsorted_sequences),
        std::end(unsorted_sequences),
        std::default_random_engine{});

    std::vector<int64_t> unsorted_sequences_lengths_vec;
    for (const auto& t : unsorted_sequences) {
      // NOLINTNEXTLINE(performance-inefficient-vector-operation)
      unsorted_sequences_lengths_vec.emplace_back(t.size(0));
    }

    // compatibility with other utilities
    for (bool batch_first : std::vector<bool>{true, false}) {
      for (bool enforce_sorted : std::vector<bool>{true, false}) {
        _compatibility_test(
            sequences, torch::tensor(lengths_vec), batch_first, enforce_sorted);
      }
      _compatibility_test(
          unsorted_sequences,
          torch::tensor(unsorted_sequences_lengths_vec),
          batch_first);
    }
  }
}

TEST_F(NNUtilsTest, PackPaddedSequence) {
  auto generate_test_case = [&](torch::ArrayRef<int64_t> sorted_lengths,
                                bool should_shuffle) {
    auto pad = [&](torch::Tensor tensor, int64_t length) {
      std::vector<int64_t> tensor_sizes{length - tensor.size(0)};
      tensor_sizes.insert(
          tensor_sizes.end(),
          tensor.sizes().slice(1).begin(),
          tensor.sizes().slice(1).end());
      return torch::cat({tensor, torch::zeros(tensor_sizes, tensor.options())});
    };
    int64_t max_length = sorted_lengths[0];
    torch::Tensor batch_sizes = torch::empty({max_length}, torch::kInt64);
    for (int64_t i = 1; i < max_length + 1; i++) {
      int64_t total = 0;
      for (const auto& x : sorted_lengths) {
        if (x >= i) {
          total++;
        }
      }
      batch_sizes[i - 1] = total;
    }
    std::vector<torch::Tensor> tensors_to_be_cat;
    for (int64_t i = 1; i < static_cast<int64_t>(sorted_lengths.size() + 1);
         i++) {
      int64_t l = sorted_lengths.at(i - 1);
      tensors_to_be_cat.emplace_back(pad(
          i * 100 + torch::arange(1., 5 * l + 1).view({l, 1, 5}), max_length));
    }
    auto padded = torch::cat(tensors_to_be_cat, 1);
    std::vector<torch::Tensor> expected_data_vec;
    for (const auto n : c10::irange(batch_sizes.size(0))) {
      int64_t batch_size = batch_sizes[n].item<int64_t>();
      for (const auto i : c10::irange(batch_size)) {
        expected_data_vec.emplace_back(
            torch::arange(1., 6) + (i + 1) * 100 + 5 * n);
      }
    }
    auto expected_data = torch::stack(expected_data_vec, /*dim=*/0);

    torch::Tensor unsorted_indices, lengths;
    if (should_shuffle) {
      // Shuffle the padded sequence to create an unsorted sequence
      std::vector<int64_t> permutation;
      for (const auto i : c10::irange(sorted_lengths.size())) {
        permutation.emplace_back(i);
      }
      std::shuffle(
          std::begin(permutation),
          std::end(permutation),
          std::default_random_engine{});

      unsorted_indices = torch::tensor(permutation);
      padded = padded.index_select(1, unsorted_indices);
      lengths = torch::tensor(sorted_lengths).index_select(0, unsorted_indices);
    } else {
      unsorted_indices = torch::Tensor();
      lengths = torch::tensor(sorted_lengths);
    }

    return std::make_tuple(
        padded.requires_grad_(),
        lengths,
        expected_data,
        batch_sizes,
        unsorted_indices);
  };

  std::vector<std::pair<std::vector<int64_t>, bool>> test_cases = {
      // sorted_lengths, should_shuffle
      {{10, 8, 4, 2, 2, 2, 1}, false},
      {{11, 10, 8, 6, 4, 3, 1}, false},
      {{11, 10, 8, 6, 4, 3, 1}, true}};

  for (const auto& test_case : test_cases) {
    for (bool batch_first : std::vector<bool>{true, false}) {
      // NOLINTNEXTLINE(performance-unnecessary-copy-initialization)
      std::vector<int64_t> sorted_lengths = std::get<0>(test_case);
      bool should_shuffle = std::get<1>(test_case);

      auto [padded, lengths, expected_data, batch_sizes, unsorted_indices] =
          generate_test_case(sorted_lengths, should_shuffle);

      auto src = padded;
      if (batch_first) {
        src = src.transpose(0, 1);
      }

      // check output
      rnn_utils::PackedSequence packed = rnn_utils::pack_padded_sequence(
          src,
          lengths,
          /*batch_first=*/batch_first,
          /*enforce_sorted=*/!should_shuffle);
      ASSERT_TRUE(torch::allclose(packed.data(), expected_data));
      ASSERT_TRUE(torch::allclose(packed.batch_sizes(), batch_sizes));
      ASSERT_TRUE(
          (!packed.unsorted_indices().defined() &&
           !unsorted_indices.defined()) ||
          torch::allclose(packed.unsorted_indices(), unsorted_indices));

      // test inverse
      auto [unpacked, unpacked_len] =
          rnn_utils::pad_packed_sequence(packed, /*batch_first=*/batch_first);
      ASSERT_TRUE(torch::allclose(unpacked, src));
      ASSERT_TRUE(torch::allclose(unpacked_len, lengths));

      // check grad
      if (padded.grad().defined()) {
        torch::NoGradGuard no_grad;
        padded.grad().zero_();
      }
      torch::Tensor grad_output;
      {
        torch::NoGradGuard no_grad;
        grad_output = unpacked.clone().normal_();
      }
      unpacked.backward(grad_output);
      if (batch_first) {
        grad_output.transpose_(0, 1);
      }
      for (const auto i : c10::irange(lengths.size(0))) {
        int64_t l = lengths[i].item<int64_t>();
        ASSERT_TRUE(torch::allclose(
            padded.grad().narrow(0, 0, l).select(1, i),
            grad_output.narrow(0, 0, l).select(1, i)));
        if (l < 10) {
          ASSERT_EQ(
              padded.grad()
                  .narrow(0, l, padded.grad().size(0) - l)
                  .select(1, i)
                  .abs()
                  .sum()
                  .item<double>(),
              0);
        }
      }
    }
  }

  // test error messages
  ASSERT_THROWS_WITH(
      rnn_utils::pack_padded_sequence(
          torch::randn({3, 3}), torch::tensor({1, 3, 2})),
      "You can pass `enforce_sorted=False`");
  ASSERT_THROWS_WITH(
      rnn_utils::pack_padded_sequence(torch::randn({0, 0}), torch::tensor({})),
      "empty tensor");
}

TEST_F(NNUtilsTest, PadSequence) {
  auto pad = [&](const torch::Tensor& tensor, int64_t length) {
    torch::NoGradGuard no_grad;
    std::vector<int64_t> tensor_sizes{length - tensor.size(0)};
    tensor_sizes.insert(
        tensor_sizes.end(),
        tensor.sizes().slice(1).begin(),
        tensor.sizes().slice(1).end());
    return torch::cat({tensor, torch::zeros(tensor_sizes, tensor.options())});
  };

  // single dimensional
  auto a = torch::tensor({1, 2, 3});
  auto b = torch::tensor({4, 5});
  auto c = torch::tensor({6});

  torch::Tensor expected, padded;

  // batch_first = true
  expected = torch::tensor({{4, 5, 0}, {1, 2, 3}, {6, 0, 0}});
  padded = rnn_utils::pad_sequence({b, a, c}, true);
  ASSERT_TRUE(padded.allclose(expected));

  // batch_first = false
  padded = rnn_utils::pad_sequence({b, a, c});
  ASSERT_TRUE(padded.allclose(expected.transpose(0, 1)));

  // padding_side = "left", batch_first = true
  expected = torch::tensor({{0, 4, 5}, {1, 2, 3}, {0, 0, 6}});
  padded = rnn_utils::pad_sequence({b, a, c}, true, 0, "left");
  ASSERT_TRUE(padded.allclose(expected));

  // padding_side = "left", batch_first = false
  padded = rnn_utils::pad_sequence({b, a, c}, false, 0, "left");
  ASSERT_TRUE(padded.allclose(expected.transpose(0, 1)));

  // pad with non-zero value
  expected = torch::tensor({{4, 5, 1}, {1, 2, 3}, {6, 1, 1}});
  padded = rnn_utils::pad_sequence({b, a, c}, true, 1);
  ASSERT_TRUE(padded.allclose(expected));

  // Test pad sorted sequence
  expected = torch::tensor({{1, 2, 3}, {4, 5, 0}, {6, 0, 0}});
  padded = rnn_utils::pad_sequence({a, b, c}, true);
  ASSERT_TRUE(padded.allclose(expected));

  // more dimensions
  int64_t maxlen = 9;
  for (int64_t num_dim : std::vector<int64_t>{0, 1, 2, 3}) {
    std::vector<torch::Tensor> sequences;
    std::vector<int64_t> trailing_dims(num_dim, 4);
    for (int64_t i = 1; i < maxlen + 1; i++) {
      int64_t seq_len = i * i;
      std::vector<int64_t> tensor_sizes{seq_len, 5};
      tensor_sizes.insert(
          tensor_sizes.end(), trailing_dims.begin(), trailing_dims.end());
      sequences.emplace_back(torch::rand(tensor_sizes));
    }
    std::shuffle(
        std::begin(sequences),
        std::end(sequences),
        std::default_random_engine{});
    std::vector<torch::Tensor> expected_tensors;
    for (const torch::Tensor& seq : sequences) {
      // NOLINTNEXTLINE(performance-inefficient-vector-operation)
      expected_tensors.emplace_back(pad(seq, maxlen * maxlen));
    }

    // batch first = true
    auto expected = torch::stack(expected_tensors);
    auto padded = rnn_utils::pad_sequence(sequences, true);
    ASSERT_TRUE(padded.allclose(expected));

    // batch first = false
    padded = rnn_utils::pad_sequence(sequences);
    ASSERT_TRUE(padded.allclose(expected.transpose(0, 1)));

    // reset expected_tensors for padding_side
    expected_tensors.clear();
    for (const torch::Tensor& seq : sequences) {
      // NOLINTNEXTLINE(performance-inefficient-vector-operation)
      expected_tensors.emplace_back(
          torch::flip(pad(torch::flip(seq, {0}), maxlen * maxlen), {0}));
    }
    expected = torch::stack(expected_tensors);
    // padding_side = "left", batch_first = true
    padded = rnn_utils::pad_sequence(sequences, true, 0, "left");
    ASSERT_TRUE(padded.allclose(expected));

    // padding_side = "left", batch_first = false
    padded = rnn_utils::pad_sequence(sequences, false, 0, "left");
    ASSERT_TRUE(padded.allclose(expected.transpose(0, 1)));
  }
}
