# Owner(s): ["oncall: distributed"]


import contextlib
import copy
import functools
import itertools
import logging
import unittest
from collections import defaultdict
from unittest import mock

import torch
import torch._dynamo.testing
import torch.distributed._composable.fsdp._fsdp_param
import torch.nn.functional as F
from torch import nn
from torch._dynamo import compiled_autograd
from torch._inductor import comms
from torch._inductor.utils import is_fallback_op, run_and_get_code
from torch.distributed._composable.fsdp import fully_shard
from torch.distributed._composable.fsdp._fsdp_common import TrainingState
from torch.distributed._composable.fsdp._fsdp_param_group import FSDPParamGroup
from torch.distributed._tensor import init_device_mesh
from torch.testing import FileCheck
from torch.testing._internal.common_distributed import at_least_x_gpu, skip_if_lt_x_gpu
from torch.testing._internal.common_fsdp import FSDPTest, MLP
from torch.testing._internal.common_utils import run_tests, skipIfRocm
from torch.testing._internal.distributed._tensor.common_dtensor import (
    ModelArgs,
    Transformer,
)
from torch.utils._triton import has_triton


log = logging.getLogger(__name__)


def _count_op_in_graph(graph, op):
    return sum(1 for node in graph.nodes if node.target is op)


def _is_fallback_op_in_snodes(snodes, op):
    return any(is_fallback_op(snode.node, op) for snode in snodes)


class TestFullyShardCompileCompute(FSDPTest):
    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    @skip_if_lt_x_gpu(2)
    def test_disable_compiling_hooks(self):
        self.run_subtests(
            {
                "skip_fsdp_hooks": [False, True],
            },
            self._test_disable_compiling_hooks,
        )

    def _test_disable_compiling_hooks(
        self,
        skip_fsdp_hooks: bool,
    ):
        torch._dynamo.reset()
        trace_rules_check_count = 0
        HOOKS_FILE_NAME = "torch/distributed/_composable/fsdp/_fsdp_state.py"
        HOOK_WRAPPER_NAME = "fsdp_hook_wrapper"

        def patched_trace_rules_check(*args, **kwargs):
            nonlocal trace_rules_check_count
            f_code = args[0]
            if (
                hasattr(f_code, "co_filename")
                and f_code.co_filename.endswith(HOOKS_FILE_NAME)
                and f_code.co_name != HOOK_WRAPPER_NAME
            ):
                trace_rules_check_count += 1
            return orig_trace_rules_check(*args, **kwargs)

        original_skip_fsdp_hooks = torch._dynamo.config.skip_fsdp_hooks
        orig_trace_rules_check = torch._dynamo.trace_rules.check
        torch.distributed.barrier()
        torch._dynamo.config.skip_fsdp_hooks = skip_fsdp_hooks
        torch._dynamo.trace_rules.check = patched_trace_rules_check
        model = MLP(4)
        fully_shard(model)
        model.compile()
        model(torch.randn((4, 4), device="cuda"))
        torch.distributed.barrier()
        torch._dynamo.config.skip_fsdp_hooks = original_skip_fsdp_hooks
        torch._dynamo.trace_rules.check = orig_trace_rules_check
        if skip_fsdp_hooks:
            self.assertEqual(trace_rules_check_count, 0)
        else:
            self.assertTrue(trace_rules_check_count > 0)


class TestFullyShardCompile(FSDPTest):
    fake_pg = not at_least_x_gpu(2)

    @property
    def world_size(self) -> int:
        return 2

    def test_dynamo_trace_use_training_state(self):
        torch._dynamo.reset()
        # Construct a dummy FSDPParamGroup, since we just want to test the `use_training_state` ctx manager.
        param_group = FSDPParamGroup(
            [],  # params: List[nn.Parameter],
            (torch.nn.Linear(1, 1),),  # module: Tuple[nn.Module, ...],
            None,  # mesh_info: FSDPMeshInfo,
            None,  # post_forward_mesh_info: Optional[FSDPMeshInfo],
            None,  # device: torch.device,
            None,  # mp_policy: MixedPrecisionPolicy,
            None,  # offload_policy: OffloadPolicy,
        )

        def f(x):
            param_group._training_state = TrainingState.IDLE
            with param_group.use_training_state(TrainingState.FORWARD):
                if param_group._training_state == TrainingState.FORWARD:
                    return x + 1
                else:
                    return x

        inp = torch.zeros(1)
        self.assertEqual(param_group._training_state, TrainingState.IDLE)

        eager_out = f(inp)
        self.assertEqual(param_group._training_state, TrainingState.IDLE)
        self.assertEqual(eager_out, inp + 1)

        cnt = torch._dynamo.testing.CompileCounterWithBackend("aot_eager")
        compiled_out = torch.compile(f, backend=cnt, fullgraph=True)(inp)
        self.assertEqual(param_group._training_state, TrainingState.IDLE)
        self.assertEqual(eager_out, compiled_out)
        self.assertEqual(cnt.frame_count, 1)
        self.assertEqual(cnt.op_count, 1)
        self.assertEqual(len(cnt.graphs), 1)

    def test_trace_fsdp_copy_(self):
        @torch.library.custom_op("mylib::add_one_out", mutates_args={"out"})
        def add_one_out(x: torch.Tensor, out: torch.Tensor) -> None:
            torch.add(x, 1, out=out)

        def f(x):
            buf = torch.zeros(2)
            buf_view = buf.view(-1)
            torch.ops.mylib.add_one_out(x, out=buf_view)
            buf_view2 = buf.view(-1)
            torch.ops.fsdp.copy_(x, buf_view2)

        ref_x = torch.zeros(2)
        x = copy.deepcopy(ref_x)
        f(ref_x)
        torch.compile(f, backend="aot_eager")(x)
        self.assertEqual(x, ref_x)

    def _assert_no_aliased_unsharded_params_in_graph_inputs(
        self, model, graph: torch.fx.Graph
    ) -> None:
        # FSDP2 unsharded params are mutated in the graph without going through functionalization.
        # Therefore, we want to make sure they don't have aliases in the graph inputs, to make it easier
        # for us to do the replacement of unsharded params with the all-gathered temporary buffer directly
        # in downstream users in the graph.
        storage_id_to_graph_inputs = defaultdict(list)
        unsharded_param_graph_inputs = set()
        for node in graph.nodes:
            if (
                node.op == "call_function"
                and node.target
                in [
                    torch.ops.inductor.resize_storage_bytes_.default,
                    torch.ops.fsdp.copy_.default,
                ]
                and node.args[0].op == "placeholder"
            ):
                unsharded_param_graph_inputs.add(node.args[0])
        assert len(unsharded_param_graph_inputs) > 0
        assert len(unsharded_param_graph_inputs) == len(
            list(model.parameters())
        ), """\
Expected all model parameters to be wrapped by FSDP2 and
have their unsharded version as graph input, but it's not true!
"""
        no_aliased_unsharded_params_in_graph_inputs = True
        err_msg = ""
        for aliased_graph_inputs in storage_id_to_graph_inputs.values():
            if len(aliased_graph_inputs) > 1 and any(
                x in unsharded_param_graph_inputs for x in aliased_graph_inputs
            ):
                no_aliased_unsharded_params_in_graph_inputs = False
                err_msg += f"""\n
Found aliased unsharded param in graph inputs: {aliased_graph_inputs},
val.shape: {[node.meta['val'].shape for node in aliased_graph_inputs]},
"""
        self.assertTrue(no_aliased_unsharded_params_in_graph_inputs, err_msg)

    def _remove_fsdp2_unsharded_param_graph_input_usage_with_optional_checks(
        self, model, fullgraph
    ):
        def _run_with_checks(graph, orig_fn):
            self._assert_no_aliased_unsharded_params_in_graph_inputs(model, graph)
            orig_fn(graph)

        if fullgraph:
            return mock.patch.object(
                comms,
                "remove_fsdp2_unsharded_param_graph_input_usage",
                functools.partial(
                    _run_with_checks,
                    orig_fn=comms.remove_fsdp2_unsharded_param_graph_input_usage,
                ),
            )
        else:
            return contextlib.nullcontext()

    def _check_fsdp_copy_and_resize_ops_count_in_graph(
        self,
        graph,
        *,
        fwd_copy_count,
        fwd_resize_count,
        bwd_copy_count,
        bwd_resize_count,
    ):
        def _check_count(copy_count, resize_count):
            actual_copy_count = _count_op_in_graph(graph, torch.ops.fsdp.copy_.default)
            self.assertEqual(
                actual_copy_count,
                copy_count,
                f"Unexpected number of `fsdp.copy_` ops (expected {copy_count}, got {actual_copy_count}) in graph: {graph}",
            )

            actual_resize_count = _count_op_in_graph(
                graph, torch.ops.inductor.resize_storage_bytes_.default
            )
            self.assertEqual(
                actual_resize_count,
                resize_count,
                f"Unexpected number of `inductor.resize_storage_bytes_` ops (expected {resize_count}, got {actual_resize_count}) in graph: {graph}",  # noqa: B950
            )

        if not torch._dynamo.compiled_autograd.in_compiled_autograd_region:
            _check_count(fwd_copy_count, fwd_resize_count)  # fwd graph
        else:
            _check_count(bwd_copy_count, bwd_resize_count)  # bwd graph

    def _reinplace_all_gather_with_optional_checks(self, fullgraph):
        def _run_with_checks(graph, orig_fn):
            self.assertGreater(
                _count_op_in_graph(
                    graph, torch.ops._c10d_functional.all_gather_into_tensor.default
                ),
                0,
            )

            orig_fn(graph)

            self.assertEqual(
                _count_op_in_graph(
                    graph, torch.ops._c10d_functional.all_gather_into_tensor.default
                ),
                0,
            )

            self.assertGreater(
                _count_op_in_graph(
                    graph, torch.ops._c10d_functional.all_gather_into_tensor_out.default
                ),
                0,
            )

        if fullgraph:
            return mock.patch.object(
                comms,
                "reinplace_fsdp_all_gather",
                functools.partial(
                    _run_with_checks,
                    orig_fn=comms.reinplace_fsdp_all_gather,
                ),
            )
        else:
            return contextlib.nullcontext()

    def _is_fwd_graph(self, snodes):
        ag_copy_in_snode = None
        for snode in snodes:
            if is_fallback_op(snode.node, torch.ops.fsdp.all_gather_copy_in.default):
                ag_copy_in_snode = snode
                break
        self.assertTrue(ag_copy_in_snode is not None)
        if any(
            dep.name.startswith("primals_")
            for dep in ag_copy_in_snode.read_writes.reads
        ):
            return True
        else:
            return False

    def _maybe_run_decide_global_ordering_of_comms_with_checks(self, fullgraph):
        def _check_fsdp_ops_in_snodes(snodes, is_fwd_graph, expect=True):
            assert_method = self.assertTrue if expect else self.assertFalse
            common_ops = {
                torch.ops.fsdp.all_gather_copy_in.default,
                torch.ops._c10d_functional.all_gather_into_tensor_out.default,
                torch.ops.fsdp.split_with_sizes_copy.default,
            }
            bwd_only_ops = {
                torch.ops.fsdp.chunk_cat.default,
                torch.ops._c10d_functional.reduce_scatter_tensor.default,
            }
            for op in common_ops:
                assert_method(
                    _is_fallback_op_in_snodes(
                        snodes,
                        op,
                    ),
                    msg=f"{op}",
                )
            if not is_fwd_graph:
                for op in bwd_only_ops:
                    assert_method(
                        _is_fallback_op_in_snodes(
                            snodes,
                            op,
                        ),
                        msg=f"{op}",
                    )

        def _decide_global_ordering_of_comms_with_checks(
            snodes, name_to_buf, name_to_fused_node, orig_fn
        ):
            is_fwd_graph = self._is_fwd_graph(snodes)
            _check_fsdp_ops_in_snodes(snodes, is_fwd_graph, expect=True)
            new_snodes = orig_fn(snodes, name_to_buf, name_to_fused_node)
            _check_fsdp_ops_in_snodes(new_snodes, is_fwd_graph, expect=False)
            return new_snodes

        if fullgraph:
            return mock.patch.object(
                comms,
                "decide_global_ordering_of_comms",
                functools.partial(
                    _decide_global_ordering_of_comms_with_checks,
                    orig_fn=comms.decide_global_ordering_of_comms,
                ),
            )
        else:
            return contextlib.nullcontext()

    def inductor_code_check_no_compute_op(self, file_check):
        return (
            file_check.check_not(" = aten.")
            .check_not(" = extern_kernels.")
            .check_not(" = triton_")
            .check_not(" = torch.ops.")
            .check_not(" = inductor_ops.")
            .check_not("    aten.")
            .check_not("    extern_kernels.")
            .check_not("    triton_")
            .check_not("    torch.ops.")
            .check_not("    inductor_ops.")
        )

    def inductor_code_check_fsdp_all_gather(
        self,
        file_check,
        overlapped_compute_op_str,
        last_all_gather=False,
    ):
        file_check = file_check.check("torch.ops.fsdp.all_gather_copy_in.")
        file_check = self.inductor_code_check_no_compute_op(file_check)
        file_check = file_check.check(
            "torch.ops._c10d_functional.all_gather_into_tensor_out."
        )
        # Checks that AGWait is delayed, making the AG overlap with some compute op.
        if overlapped_compute_op_str is not None:
            file_check = file_check.check(f"{overlapped_compute_op_str}")
        file_check = file_check.check("torch.ops._c10d_functional.wait_tensor.")
        file_check = self.inductor_code_check_no_compute_op(file_check)
        file_check = file_check.check("torch.ops.fsdp.split_with_sizes_copy.")
        if not last_all_gather:
            # Checks that there is no compute op between this AGWait and next AG.
            file_check = self.inductor_code_check_no_compute_op(file_check)
        return file_check

    def inductor_code_check_fsdp_reduce_scatter(
        self, file_check, overlapped_compute_op_str
    ):
        file_check = file_check.check("torch.ops.fsdp.chunk_cat.")
        file_check = self.inductor_code_check_no_compute_op(file_check)
        file_check = file_check.check(
            "torch.ops._c10d_functional.reduce_scatter_tensor."
        )
        # Checks that RSWait is delayed, making the RS overlap with some compute op.
        if overlapped_compute_op_str is not None:
            file_check = file_check.check(f"{overlapped_compute_op_str}")
        file_check = file_check.check("torch.ops._c10d_functional.wait_tensor.")
        return file_check

    def _test_traceable_fsdp(
        self,
        model_init_fn,
        input_creation_fn,
        backend,
        fullgraph,
    ):
        def compiler_fn(compiled_autograd_backend):
            def _fn(gm):
                # fullgraph=True because graph-break in Compiled Autograd BWD graph is not supported by Traceable FSDP2 yet
                # (main difficulty comes from queue_callback not working well when BWD has graph break).
                return torch.compile(
                    gm, backend=compiled_autograd_backend, fullgraph=True
                )

            return _fn

        def run_iters(
            model,
            optim,
            n_iter=10,
            compiled_autograd_backend=None,
        ):
            torch.manual_seed(42)
            losses = []
            for i in range(n_iter):
                inp = input_creation_fn()
                if compiled_autograd_backend is not None:
                    maybe_compiled_autograd_ctx = compiled_autograd.enable(
                        compiler_fn(compiled_autograd_backend)
                    )
                else:
                    maybe_compiled_autograd_ctx = contextlib.nullcontext()
                with maybe_compiled_autograd_ctx:
                    out = model(inp)
                    loss = out.sum()
                    losses.append(loss.item())
                    loss.backward()
                optim.step()
                optim.zero_grad(set_to_none=True)
            return losses

        def test_compiled():
            model, optim = model_init_fn()
            # FSDP2 does lazy init using 1st run, so run it once to init using eager mode
            run_iters(model, optim, n_iter=1)

            with self._remove_fsdp2_unsharded_param_graph_input_usage_with_optional_checks(
                model, fullgraph
            ):
                model_compiled = torch.compile(
                    model, backend=backend, fullgraph=fullgraph
                )
                res = run_iters(
                    model_compiled,
                    optim,
                    compiled_autograd_backend=backend,
                )
                return res

        def test_eager():
            model, optim = model_init_fn()
            # FSDP2 does lazy init using 1st run, so run it once to init using eager mode
            run_iters(model, optim, n_iter=1)

            res = run_iters(model, optim)
            return res

        with torch._dynamo.config.patch(
            inline_inbuilt_nn_modules=True,
            skip_fsdp_hooks=False,
        ), torch._functorch.config.patch(
            recompute_views=True,
            cse=False,
        ), torch._inductor.config.patch(
            reorder_for_compute_comm_overlap=True,
            reorder_for_compute_comm_overlap_passes=[
                "sink_waits",
                "raise_comms",
                "reorder_compute_for_overlap",
            ],
        ):
            losses_compiled = test_compiled()
        losses_eager = test_eager()
        if not self.fake_pg:
            for loss_compiled, loss_eager in zip(losses_compiled, losses_eager):
                self.assertTrue(
                    torch.allclose(
                        torch.tensor(loss_compiled),
                        torch.tensor(loss_eager),
                        rtol=1e-5,
                        atol=1e-8,
                    ),
                    f"{loss_compiled} vs {loss_eager}",
                )

    def _create_simple_mlp_factory_fns(self):
        hidden_dim = 16

        def model_init_fn():
            torch.manual_seed(self.rank)
            fsdp_config = {}
            model = nn.Sequential(
                nn.Linear(hidden_dim, hidden_dim, device="cuda"),
                nn.ReLU(),
                nn.Linear(hidden_dim, hidden_dim, device="cuda"),
                nn.ReLU(),
                nn.Linear(hidden_dim, hidden_dim, device="cuda"),
            )
            fully_shard(model, reshard_after_forward=True, **fsdp_config)
            optim = torch.optim.SGD(model.parameters(), lr=1e-4)
            return model, optim

        def input_creation_fn():
            torch.manual_seed(self.rank)
            inp = torch.randn((2, hidden_dim), device="cuda", requires_grad=False)
            return inp

        return model_init_fn, input_creation_fn

    @skipIfRocm
    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    def test_simple_mlp_fullgraph_backend_aot_eager(self):
        self._test_traceable_fsdp(
            *self._create_simple_mlp_factory_fns(), "aot_eager", fullgraph=True
        )

    @skipIfRocm
    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    def test_simple_mlp_fullgraph_backend_aot_eager_decomp_partition(self):
        self._test_traceable_fsdp(
            *self._create_simple_mlp_factory_fns(),
            "aot_eager_decomp_partition",
            fullgraph=True,
        )

    @skipIfRocm
    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    def test_simple_mlp_fullgraph_backend_inductor(self):
        self._test_traceable_fsdp(
            *self._create_simple_mlp_factory_fns(), "inductor", fullgraph=True
        )

    def _create_nested_fully_shard_factory_fns(self, fullgraph):
        hidden_dim = 16

        class TestSubmodule(nn.Module):
            def __init__(self, hidden_dim):
                super().__init__()
                self.param1 = nn.Parameter(
                    torch.zeros(
                        hidden_dim, hidden_dim, dtype=torch.float, device="cuda"
                    )
                )
                self.param2 = nn.Parameter(
                    torch.zeros(hidden_dim, dtype=torch.float, device="cuda")
                )

            def forward(self, x):
                ret = torch.matmul(x, self.param1)
                if not fullgraph:
                    torch._dynamo.graph_break()
                ret = ret * self.param2
                ret = torch.relu(ret)
                return ret

        class TestModule(nn.Module):
            def __init__(self, n_layers):
                super().__init__()
                self.layers = torch.nn.ModuleList()
                for layer_id in range(n_layers):
                    self.layers.append(TestSubmodule(hidden_dim))

            def forward(self, x):
                # Intentionally reusing all layers a few times,
                # to test "multiple all-gathers for the same parameter" case.
                for layer in self.layers:
                    x = layer(x)
                for layer in self.layers:
                    x = layer(x)
                for layer in self.layers:
                    x = layer(x)
                return x

        def model_init_fn():
            torch.manual_seed(self.rank)
            fsdp_config = {}
            mesh = init_device_mesh("cuda", (self.world_size,))
            model = TestModule(n_layers=3)
            for layer_id, mod in enumerate(model.layers):
                fully_shard(mod, mesh=mesh, reshard_after_forward=True, **fsdp_config)
            model = fully_shard(
                model, mesh=mesh, reshard_after_forward=True, **fsdp_config
            )
            optim = torch.optim.SGD(model.parameters(), lr=1e-4)
            return model, optim

        def input_creation_fn():
            torch.manual_seed(self.rank)
            inp = torch.randn((2, hidden_dim), device="cuda", requires_grad=False)
            return inp

        return model_init_fn, input_creation_fn

    @skipIfRocm
    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    def test_nested_fully_shard_backend_aot_eager(self):
        for fullgraph in [True, False]:
            self._test_traceable_fsdp(
                *self._create_nested_fully_shard_factory_fns(fullgraph=fullgraph),
                "aot_eager",
                fullgraph=fullgraph,
            )

    @skipIfRocm
    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    def test_nested_fully_shard_backend_aot_eager_decomp_partition(self):
        for fullgraph in [True, False]:
            self._test_traceable_fsdp(
                *self._create_nested_fully_shard_factory_fns(fullgraph=fullgraph),
                "aot_eager_decomp_partition",
                fullgraph=fullgraph,
            )

    @skipIfRocm
    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    def test_nested_fully_shard_backend_inductor(self):
        for fullgraph in [True, False]:
            with self._reinplace_all_gather_with_optional_checks(
                fullgraph
            ), self._maybe_run_decide_global_ordering_of_comms_with_checks(
                fullgraph
            ), torch._inductor.config.patch(
                post_grad_custom_post_pass=functools.partial(
                    self._check_fsdp_copy_and_resize_ops_count_in_graph,
                    fwd_copy_count=0,
                    fwd_resize_count=0,
                    bwd_copy_count=0,
                    bwd_resize_count=0,
                )
                if fullgraph
                else None
            ):
                _, triton_codes = run_and_get_code(
                    lambda: self._test_traceable_fsdp(
                        *self._create_nested_fully_shard_factory_fns(
                            fullgraph=fullgraph
                        ),
                        "inductor",
                        fullgraph=fullgraph,
                    )
                )
            if fullgraph:
                self.assertTrue(
                    len(triton_codes) == 2,
                    "Expected two separate lowerings to Triton code, one from FWD graph and one from Compiled Autograd BWD graph",
                )
                fwd_code = triton_codes[0]
                file_check = FileCheck().check("def call(args):")
                for fwd_ag_block_info in [
                    dict(overlapped_compute_op_str=None),
                    dict(
                        overlapped_compute_op_str="extern_kernels.mm(",
                    ),
                    dict(
                        overlapped_compute_op_str="extern_kernels.mm(",
                    ),
                    dict(
                        overlapped_compute_op_str="extern_kernels.mm(",
                    ),
                    dict(
                        overlapped_compute_op_str="extern_kernels.mm(",
                    ),
                    dict(
                        overlapped_compute_op_str="extern_kernels.mm(",
                    ),
                    dict(
                        overlapped_compute_op_str="extern_kernels.mm(",
                    ),
                    dict(
                        overlapped_compute_op_str="extern_kernels.mm(",
                    ),
                    dict(
                        overlapped_compute_op_str="extern_kernels.mm(",
                        last_all_gather=True,
                    ),
                ]:
                    file_check = self.inductor_code_check_fsdp_all_gather(
                        file_check, **fwd_ag_block_info
                    )
                file_check.run(fwd_code)

                bwd_code = triton_codes[1]
                file_check = FileCheck().check("def call(args):")
                for bwd_ag_block_info in [
                    dict(overlapped_compute_op_str=None),
                    dict(
                        overlapped_compute_op_str="extern_kernels.mm(",
                    ),
                    dict(
                        overlapped_compute_op_str="extern_kernels.mm(",
                        last_all_gather=True,
                    ),
                ]:
                    file_check = self.inductor_code_check_fsdp_all_gather(
                        file_check, **bwd_ag_block_info
                    )
                for bwd_rs_block_info in [
                    dict(overlapped_compute_op_str="extern_kernels.addmm("),
                    dict(
                        overlapped_compute_op_str=None
                    ),  # TODO: improve compute/comm overlap, so that `overlapped_compute_op_str` is not None
                    dict(overlapped_compute_op_str=None),
                ]:
                    file_check = self.inductor_code_check_fsdp_reduce_scatter(
                        file_check, **bwd_rs_block_info
                    )
                file_check.run(bwd_code)
            else:
                # TODO: when fullgraph=False and there is graph break in FWD graph,
                # there are several recompiles, need to figure out why.
                self.assertTrue(
                    len(triton_codes) > 2,
                    "Expected at least 3 separate lowerings to Triton code, which means at least 1 graph break in FWD graph",
                )

    def _create_transformer_factory_fns(
        self, all_requires_grad, *, activation_checkpoint=False
    ):
        seq_len = 16
        vocab_size = 8
        n_layers = 3

        def model_init_fn():
            torch.manual_seed(self.rank)
            fsdp_config = {}
            mesh = init_device_mesh("cuda", (self.world_size,))
            model_args = ModelArgs(
                vocab_size=vocab_size,
                n_layers=n_layers,
                checkpoint_activations=activation_checkpoint,
            )
            model = Transformer(model_args)
            if not all_requires_grad:
                requires_grad_params = ["attention.wq", "attention.wv"]
                requires_grad_param_count = 0
                for k, v in model.named_parameters():
                    for substring in requires_grad_params:
                        if substring in k:
                            v.requires_grad_(True)
                            requires_grad_param_count += 1
                        else:
                            v.requires_grad_(False)
                assert requires_grad_param_count == n_layers * len(requires_grad_params)
            for layer_id, mod in enumerate(model.layers):
                fully_shard(mod, mesh=mesh, reshard_after_forward=True, **fsdp_config)
            model = fully_shard(
                model, mesh=mesh, reshard_after_forward=True, **fsdp_config
            )
            optim = torch.optim.SGD(model.parameters(), lr=1e-4)
            return model, optim

        def input_creation_fn():
            torch.manual_seed(self.rank)
            inp = torch.randint(
                0, vocab_size, (2, seq_len), device="cuda", requires_grad=False
            )
            return inp

        return model_init_fn, input_creation_fn

    def _maybe_add_graph_break_to_sdpa(self, fullgraph):
        def _sdpa_with_graph_break(orig_fn, fullgraph, *args, **kwargs):
            if not fullgraph:
                torch._dynamo.graph_break()
            return orig_fn(*args, **kwargs)

        return mock.patch.object(
            F,
            "scaled_dot_product_attention",
            functools.partial(
                _sdpa_with_graph_break,
                F.scaled_dot_product_attention,
                fullgraph,
            ),
        )

    @skipIfRocm
    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    def test_transformer_backend_aot_eager(self):
        for fullgraph, all_requires_grad in itertools.product(
            [True, False], [True, False]
        ):
            with self._maybe_add_graph_break_to_sdpa(
                fullgraph
            ), self._reinplace_all_gather_with_optional_checks(fullgraph):
                self._test_traceable_fsdp(
                    *self._create_transformer_factory_fns(
                        all_requires_grad=all_requires_grad
                    ),
                    "aot_eager",
                    fullgraph=fullgraph,
                )

    @skipIfRocm
    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    # TODO: native_dropout has worse accuracy after decomp, need to figure out why
    @torch._inductor.config.patch(fallback_random=True)
    def test_transformer_backend_aot_eager_decomp_partition(self):
        for fullgraph, all_requires_grad in itertools.product(
            [True, False], [True, False]
        ):
            with self._maybe_add_graph_break_to_sdpa(fullgraph):
                self._test_traceable_fsdp(
                    *self._create_transformer_factory_fns(
                        all_requires_grad=all_requires_grad
                    ),
                    "aot_eager_decomp_partition",
                    fullgraph=fullgraph,
                )

    @skipIfRocm
    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    # TODO: native_dropout causes CUDA IMA error, need to figure out why
    @torch._inductor.config.patch(fallback_random=True)
    def test_transformer_backend_inductor(self):
        # TODO: enable fullgraph=False case
        for fullgraph, all_requires_grad, activation_checkpoint in itertools.product(
            [True], [True, False], [True, False]
        ):
            log.warning(
                f"fullgraph={fullgraph}, all_requires_grad={all_requires_grad}, activation_checkpoint={activation_checkpoint}"  # noqa: G004, G001
            )
            with self._maybe_add_graph_break_to_sdpa(
                fullgraph
            ), self._reinplace_all_gather_with_optional_checks(
                fullgraph
            ), self._maybe_run_decide_global_ordering_of_comms_with_checks(
                fullgraph
            ), torch._inductor.config.patch(
                post_grad_custom_post_pass=functools.partial(
                    self._check_fsdp_copy_and_resize_ops_count_in_graph,
                    # NOTE: For the root unsharded params, we don't reshard after forward since for training,
                    # the parameters would be freed and all-gathered immediately. Hence we still have
                    # their resize and copy ops in the graph.
                    fwd_copy_count=4,
                    fwd_resize_count=4,
                    bwd_copy_count=0,
                    bwd_resize_count=4,
                )
                if fullgraph
                else None
            ):
                _, triton_codes = run_and_get_code(
                    lambda: self._test_traceable_fsdp(
                        *self._create_transformer_factory_fns(
                            all_requires_grad=all_requires_grad,
                            activation_checkpoint=activation_checkpoint,
                        ),
                        "inductor",
                        fullgraph=fullgraph,
                    )
                )
            if fullgraph:
                self.assertTrue(
                    len(triton_codes) == 2,
                    "Expected two separate lowerings to Triton code, one from FWD graph and one from Compiled Autograd BWD graph",
                )
                fwd_code = triton_codes[0]
                file_check = FileCheck().check("def call(args):")
                for fwd_ag_block_info in [
                    dict(
                        overlapped_compute_op_str="triton_"
                        if all_requires_grad
                        else None,
                    ),
                    dict(
                        overlapped_compute_op_str="aten.native_dropout.",
                    ),
                    dict(
                        overlapped_compute_op_str="aten._scaled_dot_product_efficient_attention.",
                    ),
                    dict(
                        overlapped_compute_op_str="aten._scaled_dot_product_efficient_attention.",
                        last_all_gather=True,
                    ),
                ]:
                    file_check = self.inductor_code_check_fsdp_all_gather(
                        file_check, **fwd_ag_block_info
                    )
                file_check.run(fwd_code)

                bwd_code = triton_codes[1]
                file_check = FileCheck().check("def call(args):")
                for bwd_ag_block_info in [
                    dict(
                        overlapped_compute_op_str="extern_kernels.mm(",
                    ),
                    dict(
                        overlapped_compute_op_str="aten._scaled_dot_product_efficient_attention_backward.",
                    ),
                    dict(
                        overlapped_compute_op_str="aten._scaled_dot_product_efficient_attention_backward.",
                        last_all_gather=True,
                    ),
                ]:
                    if bwd_ag_block_info is not None:
                        file_check = self.inductor_code_check_fsdp_all_gather(
                            file_check, **bwd_ag_block_info
                        )
                for bwd_rs_block_info in [
                    dict(overlapped_compute_op_str="extern_kernels.mm(")
                    if all_requires_grad
                    else None,
                    dict(
                        overlapped_compute_op_str=None
                    ),  # TODO: improve compute/comm overlap, so that `overlapped_compute_op_str` is not None
                    dict(overlapped_compute_op_str=None),
                    dict(overlapped_compute_op_str=None) if all_requires_grad else None,
                ]:
                    if bwd_rs_block_info is not None:
                        file_check = self.inductor_code_check_fsdp_reduce_scatter(
                            file_check, **bwd_rs_block_info
                        )
                file_check.run(bwd_code)
            else:
                # TODO: when fullgraph=False and there is graph break in FWD graph,
                # there are several recompiles, need to figure out why.
                self.assertTrue(
                    len(triton_codes) > 2,
                    "Expected at least 3 separate lowerings to Triton code, which means at least 1 graph break in FWD graph",
                )


if __name__ == "__main__":
    run_tests()
