#include <ATen/ATen.h>
#include <ATen/Config.h>
#include <ATen/cuda/CUDAConfig.h>

#if defined(USE_ROCM) || !AT_CUDNN_ENABLED() || \
    (defined(CUDNN_VERSION) && CUDNN_VERSION < 8900)

namespace at {
namespace native {

void run_cudnn_SDP_fprop(
    int64_t b,
    int64_t h,
    int64_t s_q,
    int64_t s_kv,
    int64_t d_qk,
    int64_t d_v,
    float scaling_factor,
    bool isTraining,
    bool is_causal,
    double dropout_probability,
    const Tensor& q,
    const Tensor& k,
    const Tensor& v,
    const std::optional<Tensor>& attn_bias,
    Tensor& softmaxstats,
    Tensor& o,
    Tensor& dropoutseed,
    Tensor& dropoutoffset) {
  TORCH_CHECK(
      false, "PyTorch was not compiled with cuDNN Flash Attention enabled!");
}

void run_cudnn_SDP_bprop(
    int64_t b,
    int64_t h,
    int64_t s_q,
    int64_t s_kv,
    int64_t d_qk,
    int64_t d_v,
    float scaling_factor,
    bool is_causal,
    float dropout_probability,
    const Tensor& q,
    const Tensor& k,
    const Tensor& v,
    const std::optional<Tensor>& attn_bias,
    const Tensor& o,
    const Tensor& dO,
    const Tensor& softmaxstats,
    Tensor& dQ,
    Tensor& dK,
    Tensor& dV,
    const Tensor& dropoutseed,
    const Tensor& dropoutoffset) {
  TORCH_CHECK(
      false, "PyTorch was not compiled with cuDNN Flash Attention enabled!");
}

} // namespace native
} // namespace at

#else // AT_CUDNN_ENABLED && defined(CUDNN_VERSION) && CUDNN_VERSION >= 8900
#include <ATen/cudnn/Descriptors.h>
#include <ATen/cudnn/Types.h>
#include <ATen/cudnn/Utils.h>
#include <ATen/native/cudnn/MHA.h>

#include <ATen/cuda/Exceptions.h>
#include <cudnn_frontend.h>

#include <ATen/TensorUtils.h>
#include <ATen/native/utils/ParamsHash.h>

#include <c10/cuda/CUDACachingAllocator.h>
#include <cudnn.h>

#include <iostream>

namespace at {
namespace native {

#include <cudnn_frontend.h>

namespace fe = cudnn_frontend;
using graph_and_tensors = std::tuple<
    std::shared_ptr<fe::graph::Graph>,
    std::shared_ptr<fe::graph::Tensor_attributes>, // Q,
    std::shared_ptr<fe::graph::Tensor_attributes>, // K,
    std::shared_ptr<fe::graph::Tensor_attributes>, // V,
    std::optional<std::shared_ptr<fe::graph::Tensor_attributes>>, // Bias
    std::shared_ptr<fe::graph::Tensor_attributes>, // Attn_scale,
    // TODO(eqy): additional options
    // std::shared_ptr<fe::graph::Tensor_attributes>, // SEQ_LEN_Q,
    // std::shared_ptr<fe::graph::Tensor_attributes>, // SEQ_LEN_KV,
    std::shared_ptr<fe::graph::Tensor_attributes>, // Seed,
    std::shared_ptr<fe::graph::Tensor_attributes>, // Offset,
    // std::shared_ptr<fe::graph::Tensor_attributes>, // Dropout_mask,
    // std::shared_ptr<fe::graph::Tensor_attributes>, // Dropout_scale
    std::shared_ptr<fe::graph::Tensor_attributes>, // O
    std::shared_ptr<fe::graph::Tensor_attributes> // Stats
    >;

using graph_and_tensors_backward = std::tuple<
    std::shared_ptr<fe::graph::Graph>,
    std::shared_ptr<fe::graph::Tensor_attributes>, // Q,
    std::shared_ptr<fe::graph::Tensor_attributes>, // K,
    std::shared_ptr<fe::graph::Tensor_attributes>, // V,
    std::optional<std::shared_ptr<fe::graph::Tensor_attributes>>, // Bias,
    std::shared_ptr<fe::graph::Tensor_attributes>, // Attn_scale,
    std::shared_ptr<fe::graph::Tensor_attributes>, // Seed,
    std::shared_ptr<fe::graph::Tensor_attributes>, // Offset,
    std::shared_ptr<fe::graph::Tensor_attributes>, // O,
    std::shared_ptr<fe::graph::Tensor_attributes>, // dO,
    std::shared_ptr<fe::graph::Tensor_attributes>, // stats,
    std::shared_ptr<fe::graph::Tensor_attributes>, // dQ,
    std::shared_ptr<fe::graph::Tensor_attributes>, // dK,,
    std::shared_ptr<fe::graph::Tensor_attributes> // dV,
    >;

#define MAX_MHA_DIM 4

struct MHAParams {
  c10::DeviceIndex device_id;
  fe::DataType_t dataType;
  std::array<int, MAX_MHA_DIM> q_dim;
  std::array<int, MAX_MHA_DIM> k_dim;
  std::array<int, MAX_MHA_DIM> v_dim;
  std::array<int, MAX_MHA_DIM> q_stride;
  std::array<int, MAX_MHA_DIM> k_stride;
  std::array<int, MAX_MHA_DIM> v_stride;
  std::array<int, MAX_MHA_DIM> bias_dim;
  std::array<int, MAX_MHA_DIM> bias_stride;
  int64_t b;
  int64_t h;
  int64_t s_q;
  int64_t s_kv;
  int64_t d_qk;
  int64_t d_v;
  double dropout_probability;
  bool is_causal;
  bool return_softmaxstats;
  // might be redundant if we take 0 dim/stride
  // as signaling no-bias
  bool has_attn_bias;
};

void setMHAParams(
    MHAParams& params,
    int64_t b,
    int64_t h,
    int64_t s_q,
    int64_t s_kv,
    int64_t d_qk,
    int64_t d_v,
    const Tensor& q,
    const Tensor& k,
    const Tensor& v,
    const std::optional<Tensor>& attn_bias,
    double dropout_probability,
    bool is_causal,
    bool return_softmaxstats) {
  memset(&params, 0, sizeof(MHAParams));
  params.device_id = at::cuda::current_device();
  params.dataType = fe::DataType_t::HALF;
  if (q.scalar_type() == kBFloat16) {
    params.dataType = fe::DataType_t::BFLOAT16;
  }
  params.b = b;
  params.h = h;
  params.d_qk = d_qk;
  params.d_v = d_v;
  params.s_q = s_q;
  params.s_kv = s_kv;
  params.dropout_probability = dropout_probability;
  params.is_causal = is_causal;
  params.return_softmaxstats = return_softmaxstats;
  params.has_attn_bias = attn_bias.has_value();
  TORCH_INTERNAL_ASSERT(
      q.sizes().size() == MAX_MHA_DIM,
      "Q tensor has unexpected number of dims, please report a bug to PyTorch.");
  TORCH_INTERNAL_ASSERT(
      q.strides().size() == MAX_MHA_DIM,
      "Q tensor has unexpected number of dims, please report a bug to PyTorch.");
  TORCH_INTERNAL_ASSERT(
      k.sizes().size() == MAX_MHA_DIM,
      "K tensor has unexpected number of dims, please report a bug to PyTorch.");
  TORCH_INTERNAL_ASSERT(
      k.strides().size() == MAX_MHA_DIM,
      "K tensor has unexpected number of dims, please report a bug to PyTorch.");
  TORCH_INTERNAL_ASSERT(
      v.sizes().size() == MAX_MHA_DIM,
      "V tensor has unexpected number of dims, please report a bug to PyTorch.");
  TORCH_INTERNAL_ASSERT(
      v.strides().size() == MAX_MHA_DIM,
      "V tensor has unexpected number of dims, please report a bug to PyTorch.");
  std::copy(q.sizes().begin(), q.sizes().end(), params.q_dim.begin());
  std::copy(q.strides().begin(), q.strides().end(), params.q_stride.begin());
  std::copy(k.sizes().begin(), k.sizes().end(), params.k_dim.begin());
  std::copy(k.strides().begin(), k.strides().end(), params.k_stride.begin());
  std::copy(v.sizes().begin(), v.sizes().end(), params.v_dim.begin());
  std::copy(v.strides().begin(), v.strides().end(), params.v_stride.begin());
  // uninit is OK as the struct is memset 0'd
  if (params.has_attn_bias) {
    std::copy(
        attn_bias.value().sizes().begin(),
        attn_bias.value().sizes().end(),
        params.bias_dim.begin());
    std::copy(
        attn_bias.value().strides().begin(),
        attn_bias.value().strides().end(),
        params.bias_stride.begin());
  }
}

struct MHACacheKeyWrapper : ParamsWrapper<MHAParams> {
  MHACacheKeyWrapper(
      int64_t b,
      int64_t h,
      int64_t s_q,
      int64_t s_kv,
      int64_t d_qk,
      int64_t d_v,
      const Tensor& q,
      const Tensor& k,
      const Tensor& v,
      const std::optional<Tensor>& attn_bias,
      double dropout_probability,
      bool is_causal,
      bool return_softmaxstats) {
    setMHAParams(
        this->pod,
        b,
        h,
        s_q,
        s_kv,
        d_qk,
        d_v,
        q,
        k,
        v,
        attn_bias,
        dropout_probability,
        is_causal,
        return_softmaxstats);
  }
};

template <typename T, typename KeyType>
struct MHAGraphCache {
  std::unordered_map<KeyType, T, ParamsWrapperHash<KeyType>> engine_cache;

  // no mutexes here as caches are now thread local for v8, can also return a
  // pointer to the Execution Plan if we know it will not be invalidated by
  // another thread
  T* find(const KeyType& key) {
    auto it = engine_cache.find(key);
    if (it == engine_cache.end()) {
      return nullptr;
    }
    return &(it->second);
  }

  void update(const KeyType& key, T& results) {
    engine_cache.erase(key);
    engine_cache.emplace(key, std::move(results));
  }
};

// @eqy: use thread local caches as cuDNN Execution Plans are not guaranteed to
// be thread safe across all engines see Limitations in
// https://docs.nvidia.com/deeplearning/cudnn/release-notes/index.html
thread_local MHAGraphCache<graph_and_tensors, MHACacheKeyWrapper> mhagraphcache;
thread_local MHAGraphCache<graph_and_tensors_backward, MHACacheKeyWrapper>
    mhagraphbackwardcache;

namespace {
// analogous to the same function in Descriptors.h for cuDNN Convolutions...
auto fixSizeOneDimStrideSDPA(
    const IntArrayRef sizes,
    std::vector<int64_t> strides) {
  int dims = sizes.size();
  for (int d = 0; d < dims; d++) {
    int64_t curr_stride = strides[d];
    if (sizes[d] == 1 && !curr_stride) {
      curr_stride = 1;
      for (int d2 = d + 1; d2 < dims; d2++) {
        curr_stride *= strides[d2];
      }
      strides[d] = curr_stride;
    }
  }
  return strides;
}
} // namespace

auto build_graph_and_tensors(
    int64_t b,
    int64_t h,
    int64_t s_q,
    int64_t s_kv,
    int64_t d_qk,
    int64_t d_v,
    float scaling_factor,
    bool return_softmaxstats,
    bool is_causal,
    double dropout_probability,
    const Tensor& q,
    const Tensor& k,
    const Tensor& v,
    const std::optional<Tensor>& attn_bias,
    Tensor& softmaxstats,
    Tensor& o,
    Tensor& dropoutseed,
    Tensor& dropoutoffset,
    cudnnHandle_t& handle) {
  auto dtype = fe::DataType_t::HALF;
  if (q.scalar_type() == kBFloat16) {
    dtype = fe::DataType_t::BFLOAT16;
  }
  auto mha_graph = std::make_shared<fe::graph::Graph>();
  // We're baking in float accumulation and scale types
  // in theory the graph may support other types, but they
  // have not been tested
  mha_graph->set_io_data_type(dtype)
      .set_intermediate_data_type(fe::DataType_t::FLOAT)
      .set_compute_data_type(fe::DataType_t::FLOAT);
  auto attn_scale =
      mha_graph->tensor(fe::graph::Tensor_attributes()
                            .set_name("Attn_scale")
                            .set_dim({1, 1, 1, 1})
                            .set_stride({1, 1, 1, 1})
                            .set_is_pass_by_value(true)
                            .set_data_type(fe::DataType_t::FLOAT));
  auto seed = mha_graph->tensor(fe::graph::Tensor_attributes()
                                    .set_name("Seed")
                                    .set_dim({1, 1, 1, 1})
                                    .set_stride({1, 1, 1, 1})
                                    .set_data_type(fe::DataType_t::INT32));
  auto offset = mha_graph->tensor(fe::graph::Tensor_attributes()
                                      .set_name("Offset")
                                      .set_dim({1, 1, 1, 1})
                                      .set_stride({1, 1, 1, 1})
                                      .set_data_type(fe::DataType_t::INT32));
  auto scaled_dot_product_flash_attention_options =
      fe::graph::SDPA_attributes()
          .set_name("CUDNN_SDPA")
          .set_is_inference(return_softmaxstats == false)
          .set_causal_mask(is_causal)
          .set_attn_scale(attn_scale)
          .set_dropout(dropout_probability, seed, offset);
  auto Q = mha_graph->tensor(
      fe::graph::Tensor_attributes()
          .set_name("Q")
          .set_dim(q.sizes().vec())
          .set_stride(fixSizeOneDimStrideSDPA(q.sizes(), q.strides().vec())));
  auto K = mha_graph->tensor(
      fe::graph::Tensor_attributes()
          .set_name("K")
          .set_dim(k.sizes().vec())
          .set_stride(fixSizeOneDimStrideSDPA(k.sizes(), k.strides().vec())));
  auto V = mha_graph->tensor(
      fe::graph::Tensor_attributes()
          .set_name("V")
          .set_dim(v.sizes().vec())
          .set_stride(fixSizeOneDimStrideSDPA(v.sizes(), v.strides().vec())));
  std::optional<std::shared_ptr<fe::graph::Tensor_attributes>> bias;
  if (attn_bias.has_value()) {
    bias =
        mha_graph->tensor(fe::graph::Tensor_attributes()
                              .set_name("bias")
                              .set_dim(attn_bias.value().sizes().vec())
                              .set_stride(attn_bias.value().strides().vec()));
    scaled_dot_product_flash_attention_options.set_bias(bias.value());
  }
  auto seq_q = mha_graph->tensor(fe::graph::Tensor_attributes()
                                     .set_name("Seq_q")
                                     .set_dim({b, 1, 1, 1})
                                     .set_stride({1, 1, 1, 1})
                                     .set_data_type(fe::DataType_t::INT32));
  auto seq_kv = mha_graph->tensor(fe::graph::Tensor_attributes()
                                      .set_name("Seq_kv")
                                      .set_dim({b, 1, 1, 1})
                                      .set_stride({1, 1, 1, 1})
                                      .set_data_type(fe::DataType_t::INT32));

  auto [O, Stats] =
      mha_graph->sdpa(Q, K, V, scaled_dot_product_flash_attention_options);
  O->set_output(true).set_dim(o.sizes().vec()).set_stride(o.strides().vec());

  if (Stats) {
    Stats->set_output(true).set_data_type(fe::DataType_t::FLOAT);
  }

  AT_CUDNN_FRONTEND_CHECK(mha_graph->validate());
  AT_CUDNN_FRONTEND_CHECK(mha_graph->build_operation_graph(handle));
  AT_CUDNN_FRONTEND_CHECK(
      mha_graph->create_execution_plans({fe::HeurMode_t::A}));
  AT_CUDNN_FRONTEND_CHECK(mha_graph->check_support(handle));
  AT_CUDNN_FRONTEND_CHECK(mha_graph->build_plans(handle));

  return std::make_tuple(
      std::move(mha_graph),
      std::move(Q),
      std::move(K),
      std::move(V),
      std::move(bias),
      std::move(attn_scale),
      std::move(seed),
      std::move(offset),
      std::move(O),
      std::move(Stats));
}

auto build_graph_and_tensors_backward(
    int64_t b,
    int64_t h,
    int64_t s_q,
    int64_t s_kv,
    int64_t d_qk,
    int64_t d_v,
    float scaling_factor,
    bool is_causal,
    float dropout_probability,
    const Tensor& q,
    const Tensor& k,
    const Tensor& v,
    const std::optional<Tensor>& attn_bias,
    const Tensor& o,
    const Tensor& dO,
    const Tensor& softmaxstats,
    Tensor& dQ,
    Tensor& dK,
    Tensor& dV,
    const Tensor& dropoutseed,
    const Tensor& dropoutoffset,
    cudnnHandle_t& handle) {
  auto dtype = fe::DataType_t::HALF;
  if (q.scalar_type() == kBFloat16) {
    dtype = fe::DataType_t::BFLOAT16;
  }
  auto mha_graph = std::make_shared<fe::graph::Graph>();
  // We're baking in float accumulation and scale types
  // in theory the graph may support other types, but they
  // have not been tested
  mha_graph->set_io_data_type(dtype)
      .set_intermediate_data_type(fe::DataType_t::FLOAT)
      .set_compute_data_type(fe::DataType_t::FLOAT);
  auto attn_scale =
      mha_graph->tensor(fe::graph::Tensor_attributes()
                            .set_name("Attn_scale")
                            .set_dim({1, 1, 1, 1})
                            .set_stride({1, 1, 1, 1})
                            .set_is_pass_by_value(true)
                            .set_data_type(fe::DataType_t::FLOAT));
  auto sdpa_backward_options = fe::graph::SDPA_backward_attributes()
                                   .set_name("CUDNN_SDPA_BACKWARD")
                                   .set_causal_mask(is_causal)
                                   .set_attn_scale(attn_scale);
  auto Q = mha_graph->tensor(fe::graph::Tensor_attributes()
                                 .set_name("Q")
                                 .set_dim(q.sizes().vec())
                                 .set_stride(q.strides().vec()));
  auto K = mha_graph->tensor(fe::graph::Tensor_attributes()
                                 .set_name("K")
                                 .set_dim(k.sizes().vec())
                                 .set_stride(k.strides().vec()));
  auto V = mha_graph->tensor(fe::graph::Tensor_attributes()
                                 .set_name("V")
                                 .set_dim(v.sizes().vec())
                                 .set_stride(v.strides().vec()));
  std::optional<std::shared_ptr<fe::graph::Tensor_attributes>> bias;
  if (attn_bias.has_value()) {
    bias =
        mha_graph->tensor(fe::graph::Tensor_attributes()
                              .set_name("bias")
                              .set_dim(attn_bias.value().sizes().vec())
                              .set_stride(attn_bias.value().strides().vec()));
    sdpa_backward_options.set_bias(bias.value());
  }
  auto Seed = mha_graph->tensor(fe::graph::Tensor_attributes()
                                    .set_name("Seed")
                                    .set_dim({1, 1, 1, 1})
                                    .set_stride({1, 1, 1, 1})
                                    .set_data_type(fe::DataType_t::INT32));
  auto Offset = mha_graph->tensor(fe::graph::Tensor_attributes()
                                      .set_name("Offset")
                                      .set_dim({1, 1, 1, 1})
                                      .set_stride({1, 1, 1, 1})
                                      .set_data_type(fe::DataType_t::INT32));
  auto O = mha_graph->tensor(fe::graph::Tensor_attributes()
                                 .set_name("O")
                                 .set_dim(o.sizes().vec())
                                 .set_stride(o.strides().vec()));
  auto STATS = mha_graph->tensor(fe::graph::Tensor_attributes()
                                     .set_name("Stats")
                                     .set_dim(softmaxstats.sizes().vec())
                                     .set_stride(softmaxstats.strides().vec())
                                     .set_data_type(fe::DataType_t::FLOAT));
  auto DO = mha_graph->tensor(fe::graph::Tensor_attributes()
                                  .set_name("DO")
                                  .set_dim(dO.sizes().vec())
                                  .set_stride(dO.strides().vec()));
  if (dropout_probability != 0.0f) {
    sdpa_backward_options.set_dropout(dropout_probability, Seed, Offset);
  }
  auto [DQ, DK, DV] =
      mha_graph->sdpa_backward(Q, K, V, O, DO, STATS, sdpa_backward_options);
  DQ->set_output(true).set_dim(dQ.sizes().vec()).set_stride(dQ.strides().vec());
  DK->set_output(true).set_dim(dK.sizes().vec()).set_stride(dK.strides().vec());
  DV->set_output(true).set_dim(dV.sizes().vec()).set_stride(dV.strides().vec());
  AT_CUDNN_FRONTEND_CHECK(mha_graph->validate());
  AT_CUDNN_FRONTEND_CHECK(mha_graph->build_operation_graph(handle));
  AT_CUDNN_FRONTEND_CHECK(
      mha_graph->create_execution_plans({fe::HeurMode_t::A}));
  AT_CUDNN_FRONTEND_CHECK(mha_graph->check_support(handle));
  AT_CUDNN_FRONTEND_CHECK(mha_graph->build_plans(handle));
  return std::make_tuple(
      std::move(mha_graph),
      std::move(Q),
      std::move(K),
      std::move(V),
      std::move(bias),
      std::move(attn_scale),
      std::move(Seed),
      std::move(Offset),
      std::move(O),
      std::move(DO),
      std::move(STATS),
      std::move(DQ),
      std::move(DK),
      std::move(DV));
}

void run_cudnn_SDP_fprop(
    int64_t b,
    int64_t h,
    int64_t s_q,
    int64_t s_kv,
    int64_t d_qk,
    int64_t d_v,
    float scaling_factor,
    bool return_softmaxstats,
    bool is_causal,
    double dropout_probability,
    const Tensor& q,
    const Tensor& k,
    const Tensor& v,
    const std::optional<Tensor>& attn_bias,
    Tensor& softmaxstats,
    Tensor& o,
    Tensor& dropoutseed,
    Tensor& dropoutoffset) {
  cudnnHandle_t handle = getCudnnHandle();
  if (!o.defined()) {
    o = at::empty({b, h, s_q, d_v}, q.options());
  }

  if (return_softmaxstats && !softmaxstats.defined()) {
    // TODO(eqy): verify that this is correct
    softmaxstats = at::empty({b, h, s_q}, q.options().dtype(kFloat));
  }

  // do nothing if we got 0-element tensors
  if (!q.numel() || !k.numel() || !v.numel()) {
    return;
  }

  auto key = MHACacheKeyWrapper(
      b,
      h,
      s_q,
      s_kv,
      d_qk,
      d_v,
      q,
      k,
      v,
      attn_bias,
      dropout_probability,
      is_causal,
      return_softmaxstats);
  auto graph_and_tensors_ptr = mhagraphcache.find(key);
  graph_and_tensors graph_and_tensors_values;
  if (graph_and_tensors_ptr) {
    graph_and_tensors_values = *graph_and_tensors_ptr;
  } else {
    graph_and_tensors_values = build_graph_and_tensors(
        b,
        h,
        s_q,
        s_kv,
        d_qk,
        d_v,
        scaling_factor,
        return_softmaxstats,
        is_causal,
        dropout_probability,
        q,
        k,
        v,
        attn_bias,
        softmaxstats,
        o,
        dropoutseed,
        dropoutoffset,
        handle);
  }
  auto [mha_graph, Q, K, V, bias, attn_scale, seed, offset, O, Stats] =
      graph_and_tensors_values;
  std::unordered_map<std::shared_ptr<fe::graph::Tensor_attributes>, void*>
      variant_pack = {
          {Q, q.data_ptr()},
          {K, k.data_ptr()},
          {V, v.data_ptr()},
          {attn_scale, &scaling_factor},
          {seed, dropoutseed.data_ptr()},
          {offset, dropoutoffset.data_ptr()},
          {O, o.data_ptr()}};
  if (return_softmaxstats) {
    variant_pack[Stats] = softmaxstats.data_ptr();
  }
  if (attn_bias.has_value()) {
    variant_pack[bias.value()] = attn_bias.value().data_ptr();
  }
  auto workspace_size = mha_graph->get_workspace_size();
  auto workspace_ptr =
      c10::cuda::CUDACachingAllocator::get()->allocate(workspace_size);
  TORCH_CHECK(
      mha_graph->execute(handle, variant_pack, workspace_ptr.get()).is_good());
  mhagraphcache.update(key, graph_and_tensors_values);
}

void run_cudnn_SDP_bprop(
    int64_t b,
    int64_t h,
    int64_t s_q,
    int64_t s_kv,
    int64_t d_qk,
    int64_t d_v,
    float scaling_factor,
    bool is_causal,
    float dropout_probability,
    const Tensor& q,
    const Tensor& k,
    const Tensor& v,
    const std::optional<Tensor>& attn_bias,
    const Tensor& o,
    const Tensor& dO,
    const Tensor& softmaxstats,
    Tensor& dQ,
    Tensor& dK,
    Tensor& dV,
    const Tensor& dropoutseed,
    const Tensor& dropoutoffset) {
  // do nothing if we got 0-element tensors
  if (!q.numel() || !k.numel() || !v.numel() || !o.numel() || !dO.numel() ||
      !softmaxstats.numel()) {
    return;
  }

  Tensor dO_ = dO;
  if (!dO.strides()[dO.strides().size() - 1]) {
    TORCH_WARN(
        "cuDNN SDPA backward got an innermost stride of 0 in grad_out, which is unsupported."
        " Materializing a contiguous tensor which will increase memory usage...");
    dO_ = dO.contiguous();
  }
  if ( // handle trivial transposed case with a transposed dim of size 1
       // see also:  https://github.com/pytorch/pytorch/issues/134001
      !(dO_.is_contiguous() && o.is_contiguous()) &&
      !std::equal(
          o.strides().begin(), o.strides().end(), dO.strides().begin())) {
    TORCH_WARN(
        "cuDNN SDPA backward got grad_output.strides() != output.strides(), "
        "attempting to materialize a grad_output with matching strides...");
    if (o.is_contiguous()) {
      dO_ = dO.contiguous();
    } else {
      dO_ = dO.transpose(1, 2).contiguous().transpose(1, 2);
    }
  }
  TORCH_INTERNAL_ASSERT(
      (dO_.is_contiguous() && o.is_contiguous()) ||
          std::equal(
              dO_.strides().begin(), dO_.strides().end(), o.strides().begin()),
      "cuDNN SDPA expected grad_output.strides() == output.strides(), "
      "the previous step probably failed to materialize a grad_output "
      "with matching strides...");
  cudnnHandle_t handle = getCudnnHandle();
  auto key = MHACacheKeyWrapper(
      b,
      h,
      s_q,
      s_kv,
      d_qk,
      d_v,
      q,
      k,
      v,
      attn_bias,
      dropout_probability,
      is_causal,
      true);
  auto graph_and_tensors_backward_ptr = mhagraphbackwardcache.find(key);
  graph_and_tensors_backward graph_and_tensors_backward_values;
  if (graph_and_tensors_backward_ptr) {
    graph_and_tensors_backward_values = *graph_and_tensors_backward_ptr;
  } else {
    graph_and_tensors_backward_values = build_graph_and_tensors_backward(
        b,
        h,
        s_q,
        s_kv,
        d_qk,
        d_v,
        scaling_factor,
        is_causal,
        dropout_probability,
        q,
        k,
        v,
        attn_bias,
        o,
        dO_,
        softmaxstats,
        dQ,
        dK,
        dV,
        dropoutseed,
        dropoutoffset,
        handle);
  }
  auto
      [mha_graph,
       Q,
       K,
       V,
       bias,
       attn_scale,
       Seed,
       Offset,
       O,
       Do,
       Stats,
       Dq,
       Dk,
       Dv] = graph_and_tensors_backward_values;
  std::unordered_map<std::shared_ptr<fe::graph::Tensor_attributes>, void*>
      variant_pack = {// inputs
                      {Q, q.data_ptr()},
                      {K, k.data_ptr()},
                      {V, v.data_ptr()},
                      {O, o.data_ptr()},
                      {Do, dO_.data_ptr()},
                      {Stats, softmaxstats.data_ptr()},
                      // outputs
                      {Dq, dQ.data_ptr()},
                      {Dk, dK.data_ptr()},
                      {Dv, dV.data_ptr()},
                      // pass by value
                      {attn_scale, &scaling_factor}};
  if (dropout_probability != 0.0f) {
    variant_pack[Seed] = dropoutseed.data_ptr();
    variant_pack[Offset] = dropoutoffset.data_ptr();
  }
  if (attn_bias.has_value()) {
    variant_pack[bias.value()] = attn_bias.value().data_ptr();
  }
  auto workspace_size = mha_graph->get_workspace_size();
  auto workspace_ptr =
      c10::cuda::CUDACachingAllocator::get()->allocate(workspace_size);
  TORCH_CHECK(!workspace_size || workspace_ptr.get());
  TORCH_CHECK(
      mha_graph->execute(handle, variant_pack, workspace_ptr.get()).is_good());
  mhagraphbackwardcache.update(key, graph_and_tensors_backward_values);
}

} // namespace native
} // namespace at
#endif
