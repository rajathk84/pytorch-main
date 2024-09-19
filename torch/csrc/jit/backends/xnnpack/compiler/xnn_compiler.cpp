// Copyright (c) Meta Platforms, Inc. and affiliates.
//
// This source code is licensed under the BSD-style license found in the
// LICENSE file in the root directory of this source tree.

#include <caffe2/torch/csrc/jit/backends/xnnpack/compiler/xnn_compiler.h>
#include <torch/csrc/jit/backends/xnnpack/serialization/schema_generated.h>

#include <ATen/Utils.h>

namespace torch {
namespace jit {
namespace xnnpack {
namespace delegate {

void XNNCompiler::compileModel(
    const void* buffer_pointer,
    size_t num_bytes,
    XNNExecutor* executor) {
  auto output_min = -std::numeric_limits<float>::infinity();
  auto output_max = std::numeric_limits<float>::infinity();

  auto flatbuffer_graph = fb_xnnpack::GetXNNGraph(buffer_pointer);
  // initialize xnnpack
  xnn_status status = xnn_initialize(/*allocator =*/nullptr);
  TORCH_CHECK(xnn_status_success == status, "Failed to initialize xnnpack");

  // create xnnpack subgraph
  xnn_subgraph_t subgraph_ptr = nullptr;
  status = xnn_create_subgraph(
      /*external_value_ids=*/flatbuffer_graph->num_externs(),
      /*flags=*/0,
      &subgraph_ptr);
  TORCH_CHECK(xnn_status_success == status, "Failed to create xnn subgraph");

  // mapping from old ids to new created value ids
  // The old ids that were serialied were generated AoT, since
  // we are re-defining tensor values, the defined IDs could be
  // different from the ones generated AoT, as a result, we need
  // a new mapping from the old ids to the newly created ones
  std::unordered_map<uint32_t, uint32_t> remapped_ids;

  for (auto value : *flatbuffer_graph->xvalues()) {
    switch (value->xvalue_type()) {
      case fb_xnnpack::XValueUnion::XNNTensorValue: {
        auto tensor_value = value->xvalue_as_XNNTensorValue();

        std::vector<size_t> dims_data;
        for (auto dim : *tensor_value->dims()) {
          dims_data.push_back(static_cast<size_t>(dim));
        }

        uint32_t id = XNN_INVALID_VALUE_ID;
        const auto& constant_buffer = *flatbuffer_graph->constant_buffer();
        auto buffer_idx = tensor_value->constant_buffer_idx();
        const auto buffer_ptr = buffer_idx == 0
            ? nullptr
            : constant_buffer[buffer_idx]->storage()->data();
        status = xnn_define_tensor_value(
            /*subgraph=*/subgraph_ptr,
            /*datatype=*/xnn_datatype_fp32,
            /*num_dims=*/tensor_value->num_dims(),
            /*dims=*/dims_data.data(),
            /*data=*/buffer_ptr,
            /*external_id=*/tensor_value->external_id(),
            /*flags=*/tensor_value->flags(),
            /*id_out=*/&id);
        TORCH_CHECK(
            status == xnn_status_success,
            "Failed to define tensor values in graph")
        // map serialized id to newly generated id
        remapped_ids.emplace(std::make_pair(tensor_value->id_out(), id));
        break;
      }
      default: {
        TORCH_CHECK(false, "Unhandled value type found in deserialization");
      }
    }
  }

  for (auto node : *flatbuffer_graph->xnodes()) {
    switch (node->xnode_type()) {
      case fb_xnnpack::XNodeUnion::XNNAdd: {
        auto graph_node = node->xnode_as_XNNAdd();
        status = xnn_define_add2(
            subgraph_ptr,
            output_min,
            output_max,
            remapped_ids.at(graph_node->input1_id()),
            remapped_ids.at(graph_node->input2_id()),
            remapped_ids.at(graph_node->output_id()),
            graph_node->flags());
        TORCH_CHECK(status == xnn_status_success, "Failed to create add node")
        break;
      }
      default:
        TORCH_CHECK(false, "Unhandled node type found in deserialization");
    }
  }

  xnn_runtime_t runtime_ptr = nullptr;
  status = xnn_create_runtime_v2(subgraph_ptr, nullptr, 0, &runtime_ptr);
  TORCH_CHECK(xnn_status_success == status);

  executor->runtime_ =
      std::unique_ptr<xnn_runtime, decltype(&xnn_delete_runtime)>(
          runtime_ptr, xnn_delete_runtime);

  for (auto old_id : *flatbuffer_graph->input_ids()) {
    executor->input_ids_.emplace_back(remapped_ids.at(old_id));
  }

  for (auto old_id : *flatbuffer_graph->output_ids()) {
    executor->output_ids_.emplace_back(remapped_ids.at(old_id));
  }
};

} // namespace delegate
} // namespace xnnpack
} // namespace jit
} // namespace torch
