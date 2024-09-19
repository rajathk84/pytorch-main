# Owner(s): ["module: inductor"]
import logging
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import torch
from torch._inductor import config, test_operators
from torch._inductor.utils import fresh_inductor_cache
from torch.testing._internal.inductor_utils import GPU_TYPE, HAS_GPU


try:
    try:
        from . import test_torchinductor
    except ImportError:
        import test_torchinductor  # @manual=fbcode//caffe2/test/inductor:test_inductor-library
except unittest.SkipTest:
    if __name__ == "__main__":
        sys.exit(0)
    raise


def filesize(filename: Path):
    assert filename.exists(), f"{filename} is missing"
    return os.stat(filename).st_size


@config.patch("trace.enabled", True)
class TestDebugTrace(test_torchinductor.TestCase):
    def test_debug_trace(self):
        @torch.compile
        def fn(a, b):
            a = test_operators.realize(a + 1) + 2
            return torch.matmul(a, b)

        # TODO(aakhundov): make this work with fresh_inductor_cache
        # instead of force_disable_caches. currently, with the latter
        # enabled, we get `inductor [('fxgraph_cache_hit', 1)]` in
        # the counters: so the cache is actually hit and the test fails.
        with config.patch(
            {
                "trace.debug_dir": tempfile.mkdtemp(),
                "force_disable_caches": True,
            }
        ):
            with self.assertLogs(
                logging.getLogger("torch._inductor.debug"), level=logging.WARNING
            ) as cm:
                fn(torch.randn(16, 16), torch.randn(16, 16))

        self.assertEqual(len(cm.output), 1)
        m = re.match(r"WARNING.* debug trace: (.*)", cm.output[0])
        self.assertTrue(m)
        filename = Path(m.group(1))
        self.assertTrue(filename.is_dir())
        self.assertGreater(filesize(filename / "fx_graph_readable.py"), 512)
        self.assertGreater(filesize(filename / "fx_graph_runnable.py"), 512)
        self.assertGreater(filesize(filename / "fx_graph_transformed.py"), 512)
        self.assertGreater(filesize(filename / "output_code.py"), 1024)
        self.assertExpectedInline(
            open(filename / "ir_pre_fusion.txt").read().rstrip(),
            """\
op0: SchedulerNode(ComputedBuffer)
op0.writes = [MemoryDep('buf0', c0, {c0: 256}, None)]
op0.unmet_dependencies = []
op0.met_dependencies = [MemoryDep('arg0_1', c0, {c0: 256}, None)]
op0.outputs = [
    buf0: ComputedBuffer
    buf0.layout = FixedLayout('cpu', torch.float32, size=[16, 16], stride=[16, 1])
    buf0.users = [NodeUser(node=SchedulerNode(name='op1'), can_inplace=True, is_weak=False)]
]
op0.group.device = cpu
op0.group.iteration = ((256,), ())
op0.sizes = ([256], [])
arg0_1_layout = FixedLayout('cpu', torch.float32, size=[16, 16], stride=[16, 1])
buf0_layout = FixedLayout('cpu', torch.float32, size=[16, 16], stride=[16, 1])
class op0_loop_body:
    var_ranges = {z0: 256}
    index0 = z0
    def body(self, ops):
        get_index = self.get_index('index0')
        load = ops.load('arg0_1', get_index)
        constant = ops.constant(1.0, torch.float32)
        add = ops.add(load, constant)
        get_index_1 = self.get_index('index0')
        store = ops.store('buf0', get_index_1, add, None)
        return store


op1: SchedulerNode(ComputedBuffer)
op1.writes = [MemoryDep('buf1', c0, {c0: 256}, None)]
op1.unmet_dependencies = [MemoryDep('buf0', c0, {c0: 256}, None)]
op1.met_dependencies = []
op1.outputs = [
    buf1: ComputedBuffer
    buf1.layout = FixedLayout('cpu', torch.float32, size=[16, 16], stride=[16, 1])
    buf1.users = [NodeUser(node=ExternKernelSchedulerNode(name='op2'), can_inplace=False, is_weak=False)]
]
op1.group.device = cpu
op1.group.iteration = ((256,), ())
op1.sizes = ([256], [])
buf0_layout = FixedLayout('cpu', torch.float32, size=[16, 16], stride=[16, 1])
buf1_layout = FixedLayout('cpu', torch.float32, size=[16, 16], stride=[16, 1])
class op1_loop_body:
    var_ranges = {z0: 256}
    index0 = z0
    def body(self, ops):
        get_index = self.get_index('index0')
        load = ops.load('buf0', get_index)
        constant = ops.constant(2.0, torch.float32)
        add = ops.add(load, constant)
        get_index_1 = self.get_index('index0')
        store = ops.store('buf1', get_index_1, add, None)
        return store


op2: ExternKernelSchedulerNode(ExternKernelOut)
op2.writes = [StarDep(name='buf2', mode=None)]
op2.unmet_dependencies = [StarDep(name='buf1', mode=None)]
op2.met_dependencies = [StarDep(name='arg1_1', mode=None)]
op2.outputs = [
    buf2: ExternKernelOut
    buf2.layout = FixedLayout('cpu', torch.float32, size=[16, 16], stride=[16, 1])
    buf2.users = [NodeUser(node=OUTPUT, can_inplace=False, is_weak=False)]
]
op2.node.kernel = extern_kernels.mm""",
        )
        self.assertExpectedInline(
            open(filename / "ir_post_fusion.txt").read().rstrip(),
            """\
op0_op1: FusedSchedulerNode(SchedulerNode,SchedulerNode)
op0_op1.writes = [MemoryDep('buf0', c0, {c0: 256}, None), MemoryDep('buf1', c0, {c0: 256}, None)]
op0_op1.unmet_dependencies = []
op0_op1.met_dependencies = [MemoryDep('arg0_1', c0, {c0: 256}, None)]
op0_op1.outputs = [
    buf0: ComputedBuffer
    buf0.layout = FixedLayout('cpu', torch.float32, size=[16, 16], stride=[16, 1])
    buf0.users = [NodeUser(node=SchedulerNode(name='op1'), can_inplace=True, is_weak=False)]
    buf1: ComputedBuffer
    buf1.layout = FixedLayout('cpu', torch.float32, size=[16, 16], stride=[16, 1])
    buf1.users = [NodeUser(node=ExternKernelSchedulerNode(name='op2'), can_inplace=False, is_weak=False)]
]
op0_op1.snodes[0] =
op0: SchedulerNode(ComputedBuffer)
op0.writes = [MemoryDep('buf0', c0, {c0: 256}, None)]
op0.unmet_dependencies = []
op0.met_dependencies = [MemoryDep('arg0_1', c0, {c0: 256}, None)]
op0.outputs = [
    buf0: ComputedBuffer
    buf0.layout = FixedLayout('cpu', torch.float32, size=[16, 16], stride=[16, 1])
    buf0.users = [NodeUser(node=SchedulerNode(name='op1'), can_inplace=True, is_weak=False)]
]
op0.group.device = cpu
op0.group.iteration = ((256,), ())
op0.sizes = ([256], [])
arg0_1_layout = FixedLayout('cpu', torch.float32, size=[16, 16], stride=[16, 1])
buf0_layout = FixedLayout('cpu', torch.float32, size=[16, 16], stride=[16, 1])
class op0_loop_body:
    var_ranges = {z0: 256}
    index0 = z0
    def body(self, ops):
        get_index = self.get_index('index0')
        load = ops.load('arg0_1', get_index)
        constant = ops.constant(1.0, torch.float32)
        add = ops.add(load, constant)
        get_index_1 = self.get_index('index0')
        store = ops.store('buf0', get_index_1, add, None)
        return store
op0_op1.snodes[1] =
op1: SchedulerNode(ComputedBuffer)
op1.writes = [MemoryDep('buf1', c0, {c0: 256}, None)]
op1.unmet_dependencies = [MemoryDep('buf0', c0, {c0: 256}, None)]
op1.met_dependencies = []
op1.outputs = [
    buf1: ComputedBuffer
    buf1.layout = FixedLayout('cpu', torch.float32, size=[16, 16], stride=[16, 1])
    buf1.users = [NodeUser(node=ExternKernelSchedulerNode(name='op2'), can_inplace=False, is_weak=False)]
]
op1.group.device = cpu
op1.group.iteration = ((256,), ())
op1.sizes = ([256], [])
buf0_layout = FixedLayout('cpu', torch.float32, size=[16, 16], stride=[16, 1])
buf1_layout = FixedLayout('cpu', torch.float32, size=[16, 16], stride=[16, 1])
class op1_loop_body:
    var_ranges = {z0: 256}
    index0 = z0
    def body(self, ops):
        get_index = self.get_index('index0')
        load = ops.load('buf0', get_index)
        constant = ops.constant(2.0, torch.float32)
        add = ops.add(load, constant)
        get_index_1 = self.get_index('index0')
        store = ops.store('buf1', get_index_1, add, None)
        return store


op2: ExternKernelSchedulerNode(ExternKernelOut)
op2.writes = [StarDep(name='buf2', mode=None)]
op2.unmet_dependencies = [StarDep(name='buf1', mode=None)]
op2.met_dependencies = [StarDep(name='arg1_1', mode=None)]
op2.outputs = [
    buf2: ExternKernelOut
    buf2.layout = FixedLayout('cpu', torch.float32, size=[16, 16], stride=[16, 1])
    buf2.users = [NodeUser(node=OUTPUT, can_inplace=False, is_weak=False)]
]
op2.node.kernel = extern_kernels.mm""",
        )
        # intentionally only cleanup on success so debugging test is easier
        shutil.rmtree(filename)

    @unittest.skipIf(not HAS_GPU, "requires GPU")
    def test_debug_multi_tempalte(self):
        class ToyModel(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.l = torch.nn.Linear(100, 100)
                self.relu = torch.nn.ReLU()

            def forward(self, x):
                return self.relu(self.l(x))

        # no failure
        with self.assertLogs(
            logging.getLogger("torch._inductor.debug"), level=logging.WARNING
        ), fresh_inductor_cache():
            m = ToyModel().to(device=GPU_TYPE)
            m = torch.compile(m, mode="max-autotune")
            input_tensor = torch.randn(100).to(device=GPU_TYPE)
            m(input_tensor)


if __name__ == "__main__":
    from torch._inductor.test_case import run_tests
    from torch.testing._internal.inductor_utils import HAS_CPU

    if HAS_CPU:
        run_tests(needs="filelock")
