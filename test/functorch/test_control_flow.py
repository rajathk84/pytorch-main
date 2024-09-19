# Owner(s): ["module: functorch"]
import contextlib
import functools
import unittest

import torch
import torch.utils._pytree as pytree
from functorch.experimental import control_flow
from functorch.experimental.control_flow import cond, UnsupportedAliasMutationException
from torch._higher_order_ops.associative_scan import associative_scan
from torch._higher_order_ops.scan import scan
from torch._higher_order_ops.while_loop import while_loop
from torch._subclasses.functional_tensor import (
    CppFunctionalizeAPI,
    FunctionalTensor,
    FunctionalTensorMode,
    PythonFunctionalizeAPI,
)
from torch.fx.experimental.proxy_tensor import make_fx
from torch.testing._internal.common_cuda import SM70OrLater
from torch.testing._internal.common_quantization import skipIfNoDynamoSupport
from torch.testing._internal.common_utils import (
    decorateIf,
    instantiate_parametrized_tests,
    IS_WINDOWS,
    parametrize,
    requires_cuda,
    run_tests,
    skipIfCrossRef,
    skipIfRocm,
    skipIfTorchDynamo,
    TEST_WITH_TORCHDYNAMO,
    TestCase,
    xfailIfTorchDynamo,
)


# TODO: pull these helpers from AOTAutograd later
def to_fun(t):
    if isinstance(t, torch.Tensor):
        return FunctionalTensor.to_functional(t)
    return t


def from_fun(t):
    if not isinstance(t, FunctionalTensor):
        # quick sanity assert
        if isinstance(t, torch.Tensor):
            assert not torch._is_functional_tensor(t)
        return t
    torch._sync(t)
    return torch._from_functional_tensor(t.elem)


def to_fun_old(t):
    if isinstance(t, torch.Tensor) and not torch._is_functional_tensor(t):
        out = torch._to_functional_tensor(t)
        torch._mirror_autograd_meta_to(t, out)
        return out
    return t


def from_fun_old(t):
    # quick sanity assert
    if isinstance(t, torch.Tensor):
        assert torch._is_functional_tensor(t)
        torch._sync(t)
        return torch._from_functional_tensor(t)
    return t


def _fake_map(f, x, *args):
    from functorch.experimental.control_flow import _stack_pytree, _unstack_pytree

    x_pytrees = _unstack_pytree(x)
    zs = []
    for xp in x_pytrees:
        zs.append(f(xp, *args))
    return _stack_pytree(zs)


def _fake_while_loop(cond_fn, body_fn, operands):
    while cond_fn(*operands):
        operands = body_fn(*operands)
    return operands


def _fake_associative_scan(combine_fn, xs, dim, reverse=False):
    inp_leaves, spec = pytree.tree_flatten(xs)
    result_flat = []
    num_leaves = len(inp_leaves)
    op = reversed if reverse else lambda x: x

    for ind in op(range(inp_leaves[0].size(dim))):
        r = [
            inp_leaves[leave_ind][(slice(None),) * dim + (ind,)]
            for leave_ind in range(num_leaves)
        ]
        if (ind > 0 and not reverse) or (
            ind < (inp_leaves[0].size(dim) - 1) and reverse
        ):
            r = combine_fn(
                pytree.tree_unflatten(result_flat[-1], spec),
                pytree.tree_unflatten(r, spec),
            )
        r_flat, _ = pytree.tree_flatten(r)
        result_flat.append(r_flat)

    results = [
        torch.stack([e[leave_ind] for e in op(result_flat)], dim)
        for leave_ind in range(num_leaves)
    ]
    return pytree.tree_unflatten(results, spec)


def _fake_scan(combine_fn, init, xs=None, dim=0, reverse=False):
    carry_leaves, carry_spec = pytree.tree_flatten(init)
    inp_leaves, inp_spec = pytree.tree_flatten(xs)
    if xs is None or len(inp_leaves) == 0:
        return init, []
    result_flat = []
    carry = carry_leaves
    op = reversed if reverse else lambda x: x

    dummy_carry, dummy_out = combine_fn(
        pytree.tree_unflatten(carry, carry_spec),
        pytree.tree_unflatten(
            [torch._ops.ops.aten.slice(elem, dim, 0, 1, 1) for elem in inp_leaves],
            inp_spec,
        ),
    )
    dummy_out_leaves, dummy_out_spec = pytree.tree_flatten(dummy_out)
    num_leaves = len(dummy_out_leaves)

    for ind in op(range(inp_leaves[0].size(dim))):
        xs = [
            torch._ops.ops.aten.slice(elem, dim, ind, ind + 1, 1) for elem in inp_leaves
        ]

        carry, y = combine_fn(
            pytree.tree_unflatten(carry, carry_spec),
            pytree.tree_unflatten(xs, inp_spec),
        )
        carry, _ = pytree.tree_flatten(carry)
        y, _ = pytree.tree_flatten(y)
        result_flat.append(y)

    results = [
        torch.concatenate([e[leave_ind] for e in op(result_flat)], dim)
        for leave_ind in range(num_leaves)
    ]
    return (
        pytree.tree_unflatten(carry, carry_spec),
        pytree.tree_unflatten(results, dummy_out_spec),
    )


def compile_mode_helper(fct, compile_mode):
    if compile_mode == "compile":
        return torch.compile(fct, fullgraph=True, dynamic=False)
    elif compile_mode == "compile_dynamic_shape":
        return torch.compile(fct, fullgraph=True, dynamic=True)
    elif compile_mode == "eager":
        return torch.compile(fct, fullgraph=True, backend="eager")
    else:
        return fct


def get_scan_combine_fn(name, associative=True):
    def add(x: torch.Tensor, y: torch.Tensor):
        return x + y

    def adds(x: torch.Tensor, y: torch.Tensor):
        return x + x, y + y

    def mul(x: torch.Tensor, y: torch.Tensor):
        return x * y

    def div(x: torch.Tensor, y: torch.Tensor):
        return x / y

    def s5_operator(x, y):
        A_i, Bu_i = x
        A_j, Bu_j = y
        return A_j * A_i, A_j * Bu_i + Bu_j

    def tuple_fct(x, y):
        return (x[0] + y[0], x[1] * y[1])

    def complex_pointwise(x, y):
        return {
            "i": x["i"] * y["i"],
            "j": (
                [x["j"][0][0] * y["j"][0][0]],
                [{"o": x["j"][1][0]["o"] + y["j"][1][0]["o"]}],
            ),
        }

    def non_pointwise(x: torch.Tensor, y: torch.Tensor):
        W = torch.diag(torch.ones(2, device=x.device))
        return x @ W + y @ W

    if name == "add":
        fct = add
    elif name == "adds":
        fct = adds
    elif name == "mul":
        fct = mul
    elif name == "div":
        fct = div
    elif name == "s5_operator":
        fct = s5_operator
    elif name == "tuple_fct":
        fct = tuple_fct
    elif name == "complex_pointwise":
        fct = complex_pointwise
    elif name == "non_pointwise":
        fct = non_pointwise
    else:
        raise ValueError("Combine_fn name unknown!")

    if not associative:
        return lambda x, y: (fct(x, y), fct(x, y))
    else:
        return fct


def _while_loop_tests():
    def simple(x):
        def cond_fn(x):
            return x.sum() < 10

        def body_fn(x):
            return (x + 1,)

        return while_loop(cond_fn, body_fn, (x,))

    def simple_with_mutation(x):
        def cond_fn(x):
            y = x.clone().add_(1).add_(-1)
            return y.sum() < 10

        def body_fn(x):
            y = x.clone().add_(1).add_(-1)
            return (y + 1,)

        return while_loop(cond_fn, body_fn, (x,))

    def nested(out_iter, it, y):
        def cond_fn(out_iter, it, y):
            return it.sum() < 10

        def body_fn(out_iter, it, y):
            return (out_iter.clone(), it + y, y + 1)

        def outer_cond_fn(out_iter, it, y):
            return out_iter.sum() < 2

        def outer_body_fn(out_iter, it, y):
            out_iter, it, y = while_loop(cond_fn, body_fn, (out_iter, it, y))
            return (out_iter + 1, it, y)

        return while_loop(outer_cond_fn, outer_body_fn, (out_iter, it, y))

    class Nested(torch.nn.Module):
        def forward(self, ci, cj, a, b):
            def cond_fn(i1, j1, x1, y1):
                return i1 > 0

            def body_fn(i1, j1, x1, y1):
                def cond_fn_nested(i2, j2, x2, y2):
                    return j2 > 0

                def body_fn_nested(i2, j2, x2, y2):
                    return i2.clone(), j2 - 1, x2 + 3.14, y2 - 2.71

                i1, j1, x1, y1 = while_loop(
                    cond_fn_nested, body_fn_nested, [i1, j1, x1, y1]
                )
                return i1 - 1, j1.clone(), x1 * 2, y1 / 2

            return while_loop(cond_fn, body_fn, (ci, cj, a, b))

    class SimpleWithLinear(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.linear = torch.nn.Linear(2, 2)
            self.dec = torch.nn.Buffer(torch.tensor(1))

        def forward(self, iter, x):
            def cond_fn(it, x):
                return it - self.dec > 0

            def body_fn(it, x):
                return it - 1, self.linear(x)

            return while_loop(cond_fn, body_fn, (iter, x))

    class NestedWithLinear(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.mod = SimpleWithLinear()
            self.outer_linear = torch.nn.Linear(2, 2)
            self.dec = torch.nn.Buffer(torch.tensor(1))

        def forward(self, iter, x):
            def cond_fn(it, x):
                return it - self.dec > 0

            def body_fn(it, x):
                return it - 1, self.outer_linear(self.mod(it, x)[1])

            return while_loop(cond_fn, body_fn, (iter, x))

    nested2 = Nested()
    simple_with_linear = SimpleWithLinear()
    nested_with_linear = NestedWithLinear()

    x = torch.zeros(1)
    y = torch.zeros(1)
    z = torch.zeros(1)
    return {
        "simple": (simple, (x,)),
        "nested": (nested, (x, y, z)),
        "nested2": (
            nested2,
            (torch.tensor(2), torch.tensor(2), torch.ones(2, 2), torch.ones(2, 2)),
        ),
        "simple_with_mutation": (simple_with_mutation, (x,)),
        "simple_with_linear": (
            simple_with_linear,
            (torch.tensor(3), torch.randn(2, 2)),
        ),
        "nested_with_linear": (
            nested_with_linear,
            (torch.tensor(3), torch.randn(2, 2)),
        ),
    }


WHILE_LOOP_TESTS = _while_loop_tests()


def collect_meta_for_filtered_nodes(
    gm: torch.fx.GraphModule, node_names, meta_field_name
):
    ret = []
    for mod in gm.modules():
        for node in mod.graph.nodes:
            if node.name in node_names:
                for field_name in meta_field_name:
                    ret.append(node.meta.get(field_name))
    return ret


def reduce_func(*operands):
    acc = 0
    for operand in operands:
        acc += operand
    return acc


class ReduceObj:
    def __call__(self, *operands):
        return reduce_func(*operands)


class ReduceMod(torch.nn.Module):
    def _reduce(self, *operands):
        return reduce_func(*operands)

    def forward(self, *operands):
        return self._reduce(*operands)


@unittest.skipIf(IS_WINDOWS, "Windows not supported for this test")
@skipIfNoDynamoSupport
class TestControlFlow(TestCase):
    def setUp(self):
        torch._dynamo.reset()
        super().setUp()

    def test_cond_no_trace(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return x.cos()

        x = torch.randn(4)
        result = cond(False, true_fn, false_fn, [x])
        self.assertEqual(result, torch.cos(x))

    @unittest.skipIf(not torch.cuda.is_available(), "Test requires CUDA.")
    def test_cond_gpu(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return x.cos()

        x = torch.randn(4, device="cuda")
        pred = torch.tensor(False, device="cuda")
        result = cond(pred, true_fn, false_fn, [x])
        self.assertEqual(result, torch.cos(x))

    def test_cond_autograd_simple(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return x.cos()

        for pred, fn in zip(
            [torch.tensor(False), torch.tensor(True)], [false_fn, true_fn]
        ):
            x = torch.randn(4, requires_grad=True)
            result = cond(pred, true_fn, false_fn, (x,))
            self.assertEqual(result, fn(x))

            grad_out = torch.ones_like(result)
            grads = torch.autograd.grad(result, (x,), grad_out)
            expected_grads = torch.autograd.grad(fn(x), (x,), grad_out)
            self.assertEqual(expected_grads, grads)

        def f(pred, x):
            result = cond(pred, true_fn, false_fn, (x,))
            grad_out = torch.ones_like(result)
            return torch.autograd.grad(result, (x,), grad_out)

        gm = make_fx(f, tracing_mode="symbolic")(pred, x)

        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, pred_1, x_1):
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    cond = torch.ops.higher_order.cond(pred_1, true_graph_0, false_graph_0, (x_1,));  true_graph_0 = false_graph_0 = None
    getitem = cond[0];  cond = None
    ones_like = torch.ops.aten.ones_like.default(getitem, pin_memory = False);  getitem = None
    true_graph_1 = self.true_graph_1
    false_graph_1 = self.false_graph_1
    cond_1 = torch.ops.higher_order.cond(pred_1, true_graph_1, false_graph_1, (ones_like, x_1));  pred_1 = true_graph_1 = false_graph_1 = ones_like = x_1 = None
    getitem_1 = cond_1[0];  cond_1 = None
    return (getitem_1,)""",  # noqa: B950
        )

    def test_cond_autograd_complex(self):
        def true_fn(x):
            return torch.abs((x**2).sin())

        def false_fn(x):
            return (x + 42).cos()

        for pred, fn in zip(
            [torch.tensor(False), torch.tensor(True)], [false_fn, true_fn]
        ):
            x = torch.randn(4, requires_grad=True)
            result = cond(pred, true_fn, false_fn, (x,))
            self.assertEqual(result, fn(x))

            grad_out = torch.ones_like(result)
            grads = torch.autograd.grad(result, (x,), grad_out)
            expected_grads = torch.autograd.grad(fn(x), (x,), grad_out)
            self.assertEqual(expected_grads, grads)

        def f(pred, x):
            result = cond(pred, true_fn, false_fn, (x,))
            grad_out = torch.ones_like(result)
            return torch.autograd.grad(result, (x,), grad_out)

        gm = make_fx(f, tracing_mode="symbolic")(pred, x)
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, pred_1, x_1):
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    cond = torch.ops.higher_order.cond(pred_1, true_graph_0, false_graph_0, (x_1,));  true_graph_0 = false_graph_0 = None
    getitem = cond[0];  cond = None
    ones_like = torch.ops.aten.ones_like.default(getitem, pin_memory = False);  getitem = None
    true_graph_1 = self.true_graph_1
    false_graph_1 = self.false_graph_1
    cond_1 = torch.ops.higher_order.cond(pred_1, true_graph_1, false_graph_1, (ones_like, x_1));  pred_1 = true_graph_1 = false_graph_1 = ones_like = x_1 = None
    getitem_1 = cond_1[0];  cond_1 = None
    return (getitem_1,)""",  # noqa: B950
        )

    @skipIfTorchDynamo("Skip due to graph break when run with dynamo")
    def test_cond_autograd_nested(self):
        class Nested(torch.nn.Module):
            def forward(self, p0, p1, p2, a, b, c):
                def true_fn(x0, y0, z0):
                    def true_true_fn(x1, y1, z1):
                        return (x1 - y1 * z1) * 3.14

                    def true_false_fn(x1, y1, z1):
                        def true_false_true_fn(x2, y2, z2):
                            return (x2 * y2 * z2) / 2.71

                        def true_false_false_fn(x2, y2, z2):
                            return (x2 + y2 + z2) * 1.23

                        return torch.cond(
                            p2, true_false_true_fn, true_false_false_fn, [x1, y1, z1]
                        )

                    return torch.cond(p1, true_true_fn, true_false_fn, [x0, y0, z0])

                def false_fn(x0, y0, z0):
                    def false_true_fn(x1, y1, z1):
                        def false_true_true_fn(x2, y2, z2):
                            return (x2 - y2 - z2) + 1.23

                        def false_true_false_fn(x2, y2, z2):
                            return (x2 / y2 / z2) - 3.14

                        return torch.cond(
                            p2, false_true_true_fn, false_true_false_fn, [x1, y1, z1]
                        )

                    def false_false_fn(x1, y1, z1):
                        return (x1 - y1 * z1) / 2.71

                    return torch.cond(p1, false_true_fn, false_false_fn, [x0, y0, z0])

                return torch.cond(p0, true_fn, false_fn, [a, b, c])

        nn_module = Nested()

        def true_fn(x):
            return nn_module(
                torch.tensor(False), torch.tensor(True), torch.tensor(False), x, x, x
            )

        def false_fn(x):
            return nn_module(
                torch.tensor(True), torch.tensor(False), torch.tensor(True), x, x, x
            )

        x = torch.randn(4, requires_grad=True)

        for pred, fn in zip(
            [torch.tensor(False), torch.tensor(True)], [false_fn, true_fn]
        ):
            result = cond(pred, true_fn, false_fn, (x,))
            self.assertEqual(result, fn(x))

            grad_out = torch.ones_like(result)
            grads = torch.autograd.grad(result, (x,), grad_out)
            expected_grads = torch.autograd.grad(fn(x), (x,), grad_out)
            self.assertEqual(expected_grads, grads)

    @skipIfTorchDynamo("Skip due to graph break when run with dynamo")
    def test_cond_autograd_mixed_require_grad(self):
        def true_fn(x, y, z):
            return x * y * z

        def false_fn(x, y, z):
            return x + y + z

        x = torch.randn(4, requires_grad=True)
        y = torch.randn(4, requires_grad=False)

        for pred, fn in zip(
            [torch.tensor(False), torch.tensor(True)], [false_fn, true_fn]
        ):
            result = cond(pred, true_fn, false_fn, (x, y, x))
            self.assertEqual(result, fn(x, y, x))

            grad_out = torch.ones_like(result)
            grads = torch.autograd.grad(result, (x,), grad_out)
            expected_grads = torch.autograd.grad(fn(x, y, x), (x,), grad_out)
            self.assertEqual(expected_grads, grads)

        def f(pred, x, y, z):
            result = cond(pred, true_fn, false_fn, (x, y, z))
            grad_out = torch.ones_like(result)
            return torch.autograd.grad(result, (x,), grad_out)

        gm = make_fx(f, tracing_mode="symbolic")(pred, x, y, x)
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, pred_1, x_1, y_1, z_1):
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    cond = torch.ops.higher_order.cond(pred_1, true_graph_0, false_graph_0, (z_1, y_1));  true_graph_0 = false_graph_0 = None
    getitem = cond[0];  cond = None
    ones_like = torch.ops.aten.ones_like.default(getitem, pin_memory = False);  getitem = None
    true_graph_1 = self.true_graph_1
    false_graph_1 = self.false_graph_1
    cond_1 = torch.ops.higher_order.cond(pred_1, true_graph_1, false_graph_1, (ones_like, z_1, y_1));  pred_1 = true_graph_1 = false_graph_1 = ones_like = z_1 = y_1 = None
    getitem_1 = cond_1[0]
    getitem_2 = cond_1[1];  cond_1 = getitem_2 = None
    return (getitem_1,)""",  # noqa: B950
        )

    @skipIfTorchDynamo("Skip due to graph break when run with dynamo")
    def test_cond_autograd_grad_through_cond(self):
        nn_module = torch.nn.Linear(4, 4)

        def true_fn(x):
            return nn_module(x)

        def false_fn(X):
            return x * nn_module(x)

        x = torch.randn(4, requires_grad=True)

        for pred, fn in zip(
            [torch.tensor(False), torch.tensor(True)], [false_fn, true_fn]
        ):
            result = cond(pred, true_fn, false_fn, (x,))
            self.assertEqual(result, fn(x))

            grad_out = torch.ones_like(result)
            grads = torch.autograd.grad(result, (nn_module.weight,), grad_out)
            expected_grads = torch.autograd.grad(
                fn(
                    x,
                ),
                (nn_module.weight,),
                grad_out,
            )
            self.assertEqual(expected_grads, grads)

        def f(pred, x):
            result = cond(pred, true_fn, false_fn, (x,))
            grad_out = torch.ones_like(result)
            return torch.autograd.grad(result, (nn_module.weight,), grad_out)

        # need to set _allow_non_fake_inputs = True because model parameters don't
        # get fakified.
        gm = make_fx(f, tracing_mode="symbolic", _allow_non_fake_inputs=True)(pred, x)
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, pred_1, x_1):
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    _param_constant0 = self._param_constant0
    _param_constant1 = self._param_constant1
    _tensor_constant0 = self._tensor_constant0
    cond = torch.ops.higher_order.cond(pred_1, true_graph_0, false_graph_0, (_param_constant0, _param_constant1, x_1, _tensor_constant0));  true_graph_0 = false_graph_0 = _param_constant0 = _param_constant1 = _tensor_constant0 = None
    getitem = cond[0];  cond = None
    ones_like = torch.ops.aten.ones_like.default(getitem, pin_memory = False);  getitem = None
    true_graph_1 = self.true_graph_1
    false_graph_1 = self.false_graph_1
    _param_constant0_1 = self._param_constant0
    _param_constant1_1 = self._param_constant1
    _tensor_constant0_1 = self._tensor_constant0
    cond_1 = torch.ops.higher_order.cond(pred_1, true_graph_1, false_graph_1, (ones_like, _param_constant0_1, _param_constant1_1, x_1, _tensor_constant0_1));  pred_1 = true_graph_1 = false_graph_1 = ones_like = _param_constant0_1 = _param_constant1_1 = x_1 = _tensor_constant0_1 = None
    getitem_1 = cond_1[0];  getitem_1 = None
    getitem_2 = cond_1[1]
    getitem_3 = cond_1[2];  getitem_3 = None
    getitem_4 = cond_1[3];  cond_1 = getitem_4 = None
    return (getitem_2,)""",  # noqa: B950
        )

    def test_cond_in_forloop(self):
        def for_loop_fake(x):
            for i in range(3):
                x = x * x + 1
            return x

        def for_loop_test(x):
            for i in range(3):
                pred = i < 3

                def true_fn(x):
                    return x * x + 1

                def false_fn(x):
                    return x

                x = cond(pred, true_fn, false_fn, (x,))

            return x

        x = torch.ones(4, requires_grad=True)
        x_new = for_loop_test(x)
        x_exp = for_loop_fake(x)

        self.assertEqual(x_new, x_exp)

        grad_out = torch.ones_like(x_new)
        grads = torch.autograd.grad(x_new, (x,), grad_out)
        expected_grads = torch.autograd.grad(x_exp, (x,), grad_out)
        self.assertEqual(expected_grads, grads)

        def f(x):
            x_new = for_loop_test(x)
            grad_out = torch.ones_like(x_new)
            return torch.autograd.grad(x_new, (x,), grad_out)

        gm = make_fx(f, tracing_mode="symbolic")(x)
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, x_1):
    mul = torch.ops.aten.mul.Tensor(x_1, x_1)
    add = torch.ops.aten.add.Tensor(mul, 1);  mul = None
    mul_1 = torch.ops.aten.mul.Tensor(add, add)
    add_1 = torch.ops.aten.add.Tensor(mul_1, 1);  mul_1 = None
    mul_2 = torch.ops.aten.mul.Tensor(add_1, add_1)
    add_2 = torch.ops.aten.add.Tensor(mul_2, 1);  mul_2 = None
    ones_like = torch.ops.aten.ones_like.default(add_2, pin_memory = False);  add_2 = None
    mul_3 = torch.ops.aten.mul.Tensor(ones_like, add_1)
    mul_4 = torch.ops.aten.mul.Tensor(ones_like, add_1);  ones_like = add_1 = None
    add_3 = torch.ops.aten.add.Tensor(mul_4, mul_3);  mul_4 = mul_3 = None
    mul_5 = torch.ops.aten.mul.Tensor(add_3, add)
    mul_6 = torch.ops.aten.mul.Tensor(add_3, add);  add_3 = add = None
    add_4 = torch.ops.aten.add.Tensor(mul_6, mul_5);  mul_6 = mul_5 = None
    mul_7 = torch.ops.aten.mul.Tensor(add_4, x_1)
    mul_8 = torch.ops.aten.mul.Tensor(add_4, x_1);  add_4 = x_1 = None
    add_5 = torch.ops.aten.add.Tensor(mul_8, mul_7);  mul_8 = mul_7 = None
    return (add_5,)""",  # noqa: B950
        )

    @skipIfTorchDynamo("Skip due to graph break when run with dynamo")
    def test_cond_autograd_pytree_not_all_inputs_used(self):
        def true_fn(x):
            return x["t"][0] + x["t"][1]["b"]

        def false_fn(x):
            return x["t"][0] * (x["t"][2][0] / x["t"][1]["b"])

        a = torch.randn(4, requires_grad=True)
        b = torch.randn(4, requires_grad=True)
        c = torch.randn(4, requires_grad=True)

        for pred, fn in zip(
            [torch.tensor(False), torch.tensor(True)], [false_fn, true_fn]
        ):
            result = cond(pred, true_fn, false_fn, ({"t": [a, {"b": b}, (c,)]},))
            self.assertEqual(result, fn({"t": [a, {"b": b}, (c,)]}))

            grad_out = torch.ones_like(result)
            if pred:
                with self.assertRaisesRegex(Exception, r"."):
                    grads = torch.autograd.grad(result, (a, b, c), grad_out)
                    expected_grads = torch.autograd.grad(
                        fn({"t": [a, {"b": b}, (c,)]}), (a, b, c), grad_out
                    )
                    self.assertEqual(expected_grads, grads)

        def f(pred, a, b, c):
            result = cond(pred, true_fn, false_fn, ({"t": [a, {"b": b}, (c,)]},))
            grad_out = torch.ones_like(result)
            return torch.autograd.grad(result, (a, b), grad_out)

        gm = make_fx(f, tracing_mode="symbolic", _allow_non_fake_inputs=True)(
            pred, a, b, c
        )
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, pred_1, a_1, b_1, c_1):
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    cond = torch.ops.higher_order.cond(pred_1, true_graph_0, false_graph_0, (a_1, b_1, c_1));  true_graph_0 = false_graph_0 = None
    getitem = cond[0];  cond = None
    ones_like = torch.ops.aten.ones_like.default(getitem, pin_memory = False);  getitem = None
    true_graph_1 = self.true_graph_1
    false_graph_1 = self.false_graph_1
    cond_1 = torch.ops.higher_order.cond(pred_1, true_graph_1, false_graph_1, (ones_like, a_1, b_1, c_1));  pred_1 = true_graph_1 = false_graph_1 = ones_like = a_1 = b_1 = c_1 = None
    getitem_1 = cond_1[0]
    getitem_2 = cond_1[1]
    getitem_3 = cond_1[2];  cond_1 = getitem_3 = None
    return (getitem_1, getitem_2)""",  # noqa: B950
        )
        # Forward
        self.assertExpectedInline(
            gm.true_graph_0.code.strip(),
            """\
def forward(self, arg0_1, arg1_1, arg2_1):
    add = torch.ops.aten.add.Tensor(arg0_1, arg1_1);  arg0_1 = arg1_1 = None
    return (add,)""",
        )
        # Backward
        self.assertExpectedInline(
            gm.true_graph_1.code.strip(),
            """\
def forward(self, arg0_1, arg1_1, arg2_1, arg3_1):
    add = torch.ops.aten.add.Tensor(arg1_1, arg2_1);  arg1_1 = arg2_1 = add = None
    clone = torch.ops.aten.clone.default(arg0_1)
    clone_1 = torch.ops.aten.clone.default(arg0_1);  arg0_1 = None
    return [clone, clone_1, None]""",
        )

    def test_cond_autograd_pytree_input(self):
        def true_fn(x):
            return x["t"][0] + x["t"][1]["b"] * x["t"][2][0]

        def false_fn(x):
            return x["t"][0] * (x["t"][2][0] / x["t"][1]["b"])

        a = torch.randn(4, requires_grad=True)
        b = torch.randn(4, requires_grad=True)
        c = torch.randn(4, requires_grad=True)

        for pred, fn in zip(
            [torch.tensor(False), torch.tensor(True)], [false_fn, true_fn]
        ):
            result = cond(pred, true_fn, false_fn, ({"t": [a, {"b": b}, (c,)]},))
            self.assertEqual(result, fn({"t": [a, {"b": b}, (c,)]}))

            grad_out = torch.ones_like(result)
            grads = torch.autograd.grad(result, (a, b), grad_out)
            expected_grads = torch.autograd.grad(
                fn({"t": [a, {"b": b}, (c,)]}), (a, b), grad_out
            )
            self.assertEqual(expected_grads, grads)

        def f(pred):
            result = cond(pred, true_fn, false_fn, ({"t": [a, {"b": b}, (c,)]},))
            grad_out = torch.ones_like(result)
            return torch.autograd.grad(result, (a, b), grad_out)

        # need to set _allow_non_fake_inputs = True because model parameters don't
        # get fakified.
        gm = make_fx(f, tracing_mode="symbolic", _allow_non_fake_inputs=True)(pred)
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, pred_1):
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    _tensor_constant0 = self._tensor_constant0
    _tensor_constant1 = self._tensor_constant1
    _tensor_constant2 = self._tensor_constant2
    cond = torch.ops.higher_order.cond(pred_1, true_graph_0, false_graph_0, (_tensor_constant0, _tensor_constant1, _tensor_constant2));  true_graph_0 = false_graph_0 = _tensor_constant0 = _tensor_constant1 = _tensor_constant2 = None
    getitem = cond[0];  cond = None
    ones_like = torch.ops.aten.ones_like.default(getitem, pin_memory = False);  getitem = None
    true_graph_1 = self.true_graph_1
    false_graph_1 = self.false_graph_1
    _tensor_constant0_1 = self._tensor_constant0
    _tensor_constant1_1 = self._tensor_constant1
    _tensor_constant2_1 = self._tensor_constant2
    cond_1 = torch.ops.higher_order.cond(pred_1, true_graph_1, false_graph_1, (ones_like, _tensor_constant0_1, _tensor_constant1_1, _tensor_constant2_1));  pred_1 = true_graph_1 = false_graph_1 = ones_like = _tensor_constant0_1 = _tensor_constant1_1 = _tensor_constant2_1 = None
    getitem_1 = cond_1[0]
    getitem_2 = cond_1[1]
    getitem_3 = cond_1[2];  cond_1 = getitem_3 = None
    return (getitem_1, getitem_2)""",  # noqa: B950
        )

    def test_cond_autograd_different_pytree_output(self):
        def true_fn(x):
            return x["t"][0], {"r": x["t"][2][0] / x["t"][1]["b"]}, [x["t"][2][0]]

        def false_fn(x):
            return {"res": [x["t"][0] * x["t"][1]["b"], x["t"][2][0]]}

        a = torch.randn(4, requires_grad=True)
        b = torch.randn(4, requires_grad=True)
        c = torch.randn(4, requires_grad=True)

        for pred, fn in zip(
            [torch.tensor(False), torch.tensor(True)], [false_fn, true_fn]
        ):
            with self.assertRaisesRegex(
                torch._dynamo.exc.UncapturedHigherOrderOpError,
                "Cond doesn't work unless it is captured completely with torch.compile",
            ):
                cond(pred, true_fn, false_fn, ({"t": [a, {"b": b}, (c,)]},))

    @skipIfTorchDynamo("Skip due to graph break when run with dynamo")
    def test_cond_autograd_same_pytree_output(self):
        def true_fn(x):
            return {"res": [x["t"][0], (x["t"][2][0],)]}

        def false_fn(x):
            return {"res": [x["t"][1]["b"], (x["t"][2][0],)]}

        a = torch.randn(4, requires_grad=True)
        b = torch.randn(4, requires_grad=True)
        c = torch.randn(4, requires_grad=True)

        for pred, fn in zip(
            [torch.tensor(False), torch.tensor(True)], [false_fn, true_fn]
        ):
            result = cond(pred, true_fn, false_fn, ({"t": [a, {"b": b}, (c,)]},))
            result_exp = fn({"t": [a, {"b": b}, (c,)]})
            self.assertEqual(result, result_exp)

            result_flat, _ = pytree.tree_flatten(result)
            result_exp_flat, _ = pytree.tree_flatten(result_exp)

            grad_out = [torch.ones_like(g) for g in result_flat]
            expected_grads = torch.autograd.grad(result_exp_flat, (c,), grad_out)
            grads = torch.autograd.grad(result_flat, (c,), grad_out)
            self.assertEqual(expected_grads, grads)

        def f(pred):
            result = cond(pred, true_fn, false_fn, ({"t": [a, {"b": b}, (c,)]},))
            return result

        gm = make_fx(f, tracing_mode="symbolic", _allow_non_fake_inputs=True)(pred)
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, pred_1):
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    _tensor_constant0 = self._tensor_constant0
    _tensor_constant1 = self._tensor_constant1
    _tensor_constant2 = self._tensor_constant2
    cond = torch.ops.higher_order.cond(pred_1, true_graph_0, false_graph_0, (_tensor_constant0, _tensor_constant1, _tensor_constant2));  pred_1 = true_graph_0 = false_graph_0 = _tensor_constant0 = _tensor_constant1 = _tensor_constant2 = None
    getitem = cond[0]
    getitem_1 = cond[1];  cond = None
    view = torch.ops.aten.view.default(getitem, [4]);  getitem = None
    view_1 = torch.ops.aten.view.default(getitem_1, [4]);  getitem_1 = None
    return {'res': [view, (view_1,)]}""",  # noqa: B950
        )

    @skipIfTorchDynamo("Skip due to graph break when run with dynamo")
    def test_cond_autograd_torch_nn_module(self):
        nn_module_true = torch.nn.Linear(4, 4)

        def true_fn(x):
            return nn_module_true(torch.abs((x**2).sin()))

        nn_module_false = torch.nn.GRUCell(4, 4)

        def false_fn(x):
            return nn_module_false((x + 42).cos())

        for pred, fn in zip(
            [torch.tensor(False), torch.tensor(True)], [false_fn, true_fn]
        ):
            x = torch.randn(4, requires_grad=True)
            result = cond(pred, true_fn, false_fn, (x,))
            self.assertEqual(result, fn(x))

            grad_out = torch.ones_like(result)
            grads = torch.autograd.grad(result, (x,), grad_out)
            expected_grads = torch.autograd.grad(fn(x), (x,), grad_out)
            self.assertEqual(expected_grads, grads)

        def f(pred, x):
            result = cond(pred, true_fn, false_fn, (x,))
            grad_out = torch.ones_like(result)
            return torch.autograd.grad(result, (x,), grad_out)

        gm = make_fx(f)(pred, x)
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, pred_1, x_1):
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    _param_constant0 = self._param_constant0
    _param_constant1 = self._param_constant1
    _param_constant2 = self._param_constant2
    _param_constant3 = self._param_constant3
    _param_constant4 = self._param_constant4
    _param_constant5 = self._param_constant5
    cond = torch.ops.higher_order.cond(pred_1, true_graph_0, false_graph_0, (x_1, _param_constant0, _param_constant1, _param_constant2, _param_constant3, _param_constant4, _param_constant5));  true_graph_0 = false_graph_0 = _param_constant0 = _param_constant1 = _param_constant2 = _param_constant3 = _param_constant4 = _param_constant5 = None
    getitem = cond[0];  cond = None
    ones_like = torch.ops.aten.ones_like.default(getitem, pin_memory = False);  getitem = None
    true_graph_1 = self.true_graph_1
    false_graph_1 = self.false_graph_1
    _param_constant0_1 = self._param_constant0
    _param_constant1_1 = self._param_constant1
    _param_constant2_1 = self._param_constant2
    _param_constant3_1 = self._param_constant3
    _param_constant4_1 = self._param_constant4
    _param_constant5_1 = self._param_constant5
    cond_1 = torch.ops.higher_order.cond(pred_1, true_graph_1, false_graph_1, (ones_like, x_1, _param_constant0_1, _param_constant1_1, _param_constant2_1, _param_constant3_1, _param_constant4_1, _param_constant5_1));  pred_1 = true_graph_1 = false_graph_1 = ones_like = x_1 = _param_constant0_1 = _param_constant1_1 = _param_constant2_1 = _param_constant3_1 = _param_constant4_1 = _param_constant5_1 = None
    getitem_1 = cond_1[0]
    getitem_2 = cond_1[1];  getitem_2 = None
    getitem_3 = cond_1[2];  getitem_3 = None
    getitem_4 = cond_1[3];  getitem_4 = None
    getitem_5 = cond_1[4];  getitem_5 = None
    getitem_6 = cond_1[5];  getitem_6 = None
    getitem_7 = cond_1[6];  cond_1 = getitem_7 = None
    return (getitem_1,)""",  # noqa: B950
        )

    def test_cond_autograd_user_nn_module(self):
        class User_nn_module(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()

            def forward(self, input):
                return input * input

        nn_module_true = User_nn_module()

        def true_fn(x):
            return nn_module_true(torch.abs((x**2).sin()))

        nn_module_false = torch.nn.ReLU(inplace=False)

        def false_fn(x):
            return nn_module_false((x + 42).cos())

        for pred, fn in zip(
            [torch.tensor(False), torch.tensor(True)], [false_fn, true_fn]
        ):
            x = torch.randn(4, requires_grad=True)
            result = cond(pred, true_fn, false_fn, (x,))
            self.assertEqual(result, fn(x))

            grad_out = torch.ones_like(result)
            grads = torch.autograd.grad(result, (x,), grad_out)
            expected_grads = torch.autograd.grad(fn(x), (x,), grad_out)
            self.assertEqual(expected_grads, grads)

        def f(pred, x):
            result = cond(pred, true_fn, false_fn, (x,))
            grad_out = torch.ones_like(result)
            return torch.autograd.grad(result, (x,), grad_out)

        gm = make_fx(f)(pred, x)
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, pred_1, x_1):
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    cond = torch.ops.higher_order.cond(pred_1, true_graph_0, false_graph_0, (x_1,));  true_graph_0 = false_graph_0 = None
    getitem = cond[0];  cond = None
    ones_like = torch.ops.aten.ones_like.default(getitem, pin_memory = False);  getitem = None
    true_graph_1 = self.true_graph_1
    false_graph_1 = self.false_graph_1
    cond_1 = torch.ops.higher_order.cond(pred_1, true_graph_1, false_graph_1, (ones_like, x_1));  pred_1 = true_graph_1 = false_graph_1 = ones_like = x_1 = None
    getitem_1 = cond_1[0];  cond_1 = None
    return (getitem_1,)""",  # noqa: B950
        )

    def test_cond_autograd_inner_fn(self):
        def true_fn(x):
            return torch.abs((x**2).sin())

        def false_fn(x):
            def inner_fn(x):
                return x**2

            return torch.abs(inner_fn(x).sin())

        x = torch.randn(4, requires_grad=True)
        pred = torch.tensor(False)
        fn = false_fn
        result_false = cond(pred, true_fn, false_fn, (x,))
        self.assertEqual(result_false, fn(x))

        grad_out = torch.ones_like(result_false)
        grads_false = torch.autograd.grad(result_false, (x,), grad_out)
        expected_grads = torch.autograd.grad(fn(x), (x,), grad_out)
        self.assertEqual(expected_grads, grads_false)

        pred = torch.tensor(True)
        fn = true_fn
        result_true = cond(pred, true_fn, false_fn, (x,))
        self.assertEqual(result_true, fn(x))
        self.assertEqual(result_false, result_true)

        grad_out = torch.ones_like(result_true)
        grads_true = torch.autograd.grad(result_true, (x,), grad_out)
        expected_grads = torch.autograd.grad(fn(x), (x,), grad_out)
        self.assertEqual(expected_grads, grads_true)
        self.assertEqual(grads_false, grads_true)

        def f(pred, x):
            result = cond(pred, true_fn, false_fn, (x,))
            grad_out = torch.ones_like(result)
            return torch.autograd.grad(result, (x,), grad_out)

        gm = make_fx(f)(pred, x)
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, pred_1, x_1):
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    cond = torch.ops.higher_order.cond(pred_1, true_graph_0, false_graph_0, (x_1,));  true_graph_0 = false_graph_0 = None
    getitem = cond[0];  cond = None
    ones_like = torch.ops.aten.ones_like.default(getitem, pin_memory = False);  getitem = None
    true_graph_1 = self.true_graph_1
    false_graph_1 = self.false_graph_1
    cond_1 = torch.ops.higher_order.cond(pred_1, true_graph_1, false_graph_1, (ones_like, x_1));  pred_1 = true_graph_1 = false_graph_1 = ones_like = x_1 = None
    getitem_1 = cond_1[0];  cond_1 = None
    return (getitem_1,)""",  # noqa: B950
        )

    def test_cond_autograd_inner_tensor(self):
        def true_fn(x):
            return torch.abs((x**2).sin())

        def false_fn(x):
            y = torch.ones(4, requires_grad=False) * 42
            return (x * y).cos()

        for pred, fn in zip(
            [torch.tensor(False), torch.tensor(True)], [false_fn, true_fn]
        ):
            x = torch.randn(4, requires_grad=True)
            result = cond(pred, true_fn, false_fn, (x,))
            self.assertEqual(result, fn(x))

            grad_out = torch.ones_like(result)
            grads = torch.autograd.grad(result, (x,), grad_out)
            expected_grads = torch.autograd.grad(fn(x), (x,), grad_out)
            self.assertEqual(expected_grads, grads)

        def f(pred, x):
            result = cond(pred, true_fn, false_fn, (x,))
            grad_out = torch.ones_like(result)
            return torch.autograd.grad(result, (x,), grad_out)

        gm = make_fx(f, tracing_mode="symbolic")(pred, x)
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, pred_1, x_1):
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    cond = torch.ops.higher_order.cond(pred_1, true_graph_0, false_graph_0, (x_1,));  true_graph_0 = false_graph_0 = None
    getitem = cond[0];  cond = None
    ones_like = torch.ops.aten.ones_like.default(getitem, pin_memory = False);  getitem = None
    true_graph_1 = self.true_graph_1
    false_graph_1 = self.false_graph_1
    cond_1 = torch.ops.higher_order.cond(pred_1, true_graph_1, false_graph_1, (ones_like, x_1));  pred_1 = true_graph_1 = false_graph_1 = ones_like = x_1 = None
    getitem_1 = cond_1[0];  cond_1 = None
    return (getitem_1,)""",  # noqa: B950
        )

    @unittest.skipIf(not torch.cuda.is_available(), "Test requires CUDA.")
    def test_cond_autograd_gpu(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return x.cos()

        for pred, fn in zip(
            [torch.tensor(False, device="cuda"), torch.tensor(True, device="cuda")],
            [false_fn, true_fn],
        ):
            x = torch.randn(4, requires_grad=True, device="cuda")
            result = cond(pred, true_fn, false_fn, (x,))
            self.assertEqual(result, fn(x))

            grad_out = torch.ones_like(result)
            grads = torch.autograd.grad(result, (x,), grad_out)
            expected_grads = torch.autograd.grad(fn(x), (x,), grad_out)
            self.assertEqual(expected_grads, grads)

    @unittest.skipIf(not torch.cuda.is_available(), "Test requires CUDA.")
    def test_map_gpu(self):
        def f(x, y):
            return x + y

        xs = torch.ones(3, 2, 2, device="cuda")
        y = torch.ones(2, device="cuda")
        res = control_flow.map(f, xs, y)
        expected = _fake_map(f, xs, y)
        self.assertEqual(expected, res)

    @unittest.skipIf(not torch.cuda.is_available(), "Test requires CUDA.")
    def test_while_loop_gpu(self):
        def cond_fn(x):
            return x.sum() < 10

        def body_fn(x):
            return (x + 1,)

        x = torch.zeros(1, device="cuda")
        res = while_loop(cond_fn, body_fn, (x,))
        expected = _fake_while_loop(cond_fn, body_fn, (x,))
        self.assertEqual(expected, res)

    def test_map_illegal_inputs(self):
        def f(x, y):
            return x[0] + x[1] + y

        with self.assertRaisesRegex(
            RuntimeError,
            r"Mapped xs can only consist of tensors\. Got xs \[3, tensor\(\[1\., 1\.\]\)\]\.",
        ):
            _ = control_flow.map(f, (3, torch.ones(2)), torch.ones(2))

        with self.assertRaisesRegex(
            RuntimeError, r"Leading dimensions of mapped xs cannot be 0\."
        ):
            _ = control_flow.map(
                f, (torch.ones(0, 1, 2), torch.ones(0, 1, 2)), torch.ones(2)
            )

        with self.assertRaisesRegex(
            RuntimeError,
            r"Leading dimensions of mapped xs must be consistent\. "
            r"Got shapes \[torch\.Size\(\[3, 4, 5\]\), torch\.Size\(\[4, 4, 5\]\)\]\.",
        ):
            _ = control_flow.map(
                f, (torch.ones(3, 4, 5), torch.ones(4, 4, 5)), torch.ones(5)
            )

    def test_map_illegal_outputs(self):
        def f(x, y):
            return x.item()

        def f1(x, y):
            return y.size()

        def f2(x, y):
            return None

        x = torch.ones([3])
        y = torch.ones([1, 2, 3])
        with self.assertRaisesRegex(
            RuntimeError, r"Expect outputs of map only contains tensors or None\."
        ):
            _ = control_flow.map(f, x, y)

        with self.assertRaisesRegex(
            RuntimeError, r"Expect outputs of map only contains tensors or None\."
        ):
            out = control_flow.map(f1, x, y)

        # return None is OK
        _ = control_flow.map(f2, x, y)

    def test_map_list_in_out(self):
        def f(x, y):
            return [[x[0][0] + y]]

        xs = [[torch.ones(3, 2, 2)]]
        y = torch.ones(2)
        res = control_flow.map(f, xs, y)
        expected = _fake_map(f, xs, y)
        self.assertEqual(len(res), 1)
        self.assertEqual(len(res[0]), 1)
        self.assertEqual(expected, res)

    def test_map_dict_in_out(self):
        def f(x, y):
            return {"c": x["a"]["b"] + y}

        xs = {"a": {"b": torch.ones(3, 2, 2)}}
        y = torch.ones(2)
        res = control_flow.map(f, xs, y)
        expected = _fake_map(f, xs, y)
        self.assertEqual(len(res), 1)
        self.assertTrue("c" in res)
        self.assertEqual(expected, res)

    def test_map_autograd_simple(self):
        def f(x, y):
            return x.sin().cos() * y.cos().sin()

        xs = torch.ones(3, 2, 2, requires_grad=True)
        y = torch.ones(2, requires_grad=True)
        res = control_flow.map(f, xs, y)
        expected_res = _fake_map(f, xs, y)
        grad_out = torch.ones_like(res)
        grads = torch.autograd.grad(res, (xs, y), grad_out)
        expected_grads = torch.autograd.grad(expected_res, (xs, y), grad_out)
        self.assertEqual(expected_res, res)
        self.assertEqual(expected_grads, grads)

    def test_map_autograd_simple_partial_grad(self):
        def f(x, y):
            return x.sin().cos() * y.cos().sin()

        xs = torch.ones(3, 2, 2, requires_grad=True)
        # Disable the gradient computation for y
        y = torch.ones(2, requires_grad=False)
        res = control_flow.map(f, xs, y)
        expected_res = _fake_map(f, xs, y)
        grad_out = torch.ones_like(res)
        grads = torch.autograd.grad(res, (xs,), grad_out)
        expected_grads = torch.autograd.grad(expected_res, (xs,), grad_out)
        self.assertEqual(expected_res, res)
        self.assertEqual(expected_grads, grads)

    def test_map_autograd_no_grad_output(self):
        def f(x, y):
            return x[0].sin().cos() + y, y.cos().sin()

        xs = [torch.ones(3, 2, 2, requires_grad=True), torch.ones(3, 3)]
        # Disable the gradient computation for y
        y = torch.ones(2, requires_grad=False)
        res = control_flow.map(f, xs, y)
        expected_res = _fake_map(f, xs, y)
        grad_out = torch.ones_like(res[0])
        grads = torch.autograd.grad(res[0], (xs[0],), grad_out)
        expected_grads = torch.autograd.grad(expected_res[0], (xs[0],), grad_out)
        self.assertEqual(expected_res, res)
        self.assertEqual(expected_grads, grads)

    def test_map_autograd_nested_list(self):
        import torch.utils._pytree as pytree

        def f(x, y):
            a, b = x
            c, d = a
            return [[b.sin() * c.cos()], d.sin() * y.cos()]

        def fwbw(map_op, f, x, y):
            z = map_op(f, x, y)
            flat_x = pytree.tree_leaves(x)
            flat_z = pytree.tree_leaves(z)
            grads = torch.autograd.grad(
                flat_z, flat_x, [torch.ones_like(z) for z in flat_z]
            )
            return z, grads

        x = [
            [
                torch.randn(3, 2, 2, requires_grad=True),
                torch.randn(3, 2, 1, requires_grad=True),
            ],
            torch.ones(3, 1, 2, requires_grad=True),
        ]
        y = torch.ones(1, requires_grad=True)
        true_outs = fwbw(control_flow.map, f, x, y)
        fake_outs = fwbw(_fake_map, f, x, y)
        self.assertEqual(true_outs, fake_outs)

    # TODO: provide an implementation for all compile modes and re-enable all test
    @unittest.skipIf(not SM70OrLater, "triton")
    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("compile_mode", ["none", "compile", "compile_dynamic_shape"])
    @parametrize("combine_mode", ["pointwise", "generic"])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    # Skipping the combination of combine_mode=pointwise and device=cpu
    # as the current implementation of pointwise does only support CUDA device
    @decorateIf(
        unittest.skip,
        lambda params: (
            params["combine_mode"] == "pointwise"
            and (params["device"] == torch.device("cpu") or torch.version.hip)
        ),
    )
    def test_associative_scan_compile(
        self, combine_mode, reverse, compile_mode, device
    ):
        x = torch.randn(3, 10, 2, device=device)

        scan_fct = compile_mode_helper(associative_scan, compile_mode)

        for op, op_pt in [
            (get_scan_combine_fn("add", True), torch.cumsum),
            (get_scan_combine_fn("mul", True), torch.cumprod),
        ]:
            result = scan_fct(op, x, 0, reverse=reverse, combine_mode=combine_mode)
            result_exp = _fake_associative_scan(op, xs=x, dim=0, reverse=reverse)
            self.assertEqual(result, result_exp)
            if not reverse:
                result_exp_PT = op_pt(x, 0)
                self.assertEqual(result, result_exp_PT)

        # Jax Examples
        x = torch.arange(0, 4, device=device)
        cumsum1 = scan_fct(
            get_scan_combine_fn("add", True),
            x,
            0,
            reverse=reverse,
            combine_mode=combine_mode,
        )
        cumsum_exp = _fake_associative_scan(
            get_scan_combine_fn("add", True), x, 0, reverse=reverse
        )
        if not reverse:
            self.assertEqual(
                cumsum1, torch.tensor([0.0, 1.0, 3.0, 6.0], dtype=torch.int64)
            )
        else:
            self.assertEqual(
                cumsum1, torch.tensor([6.0, 6.0, 5.0, 3.0], dtype=torch.int64)
            )
        self.assertEqual(cumsum1, cumsum_exp)

    # TODO: provide an implementation for all compile modes and re-enable all test
    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("compile_mode", ["none", "eager"])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_compile(self, reverse, compile_mode, device):
        def add2(x: torch.Tensor, y: torch.Tensor):
            return x * y, x + y

        x = torch.randn(3, 10, 2, device=device)

        scan_fct = compile_mode_helper(scan, compile_mode)

        for op, op_pt, init in [
            (
                get_scan_combine_fn("add", False),
                torch.cumsum,
                torch.zeros(1, 10, 2, device=device),
            ),
            (
                get_scan_combine_fn("mul", False),
                torch.cumprod,
                torch.ones(1, 10, 2, device=device),
            ),
        ]:
            result = scan_fct(op, init, x, dim=0, reverse=reverse)
            result_exp = _fake_scan(op, init=init, xs=x, dim=0, reverse=reverse)
            self.assertEqual(result, result_exp)
            if not reverse:
                result_exp_PT = op_pt(x, 0)
                self.assertEqual(result[1], result_exp_PT)

        # Jax Examples
        x = torch.arange(0, 4, device=device, dtype=torch.int64)
        init = torch.zeros(1, device=device, dtype=torch.int64)
        cumsum1 = scan_fct(
            get_scan_combine_fn("add", False),
            init,
            x,
            dim=0,
            reverse=reverse,
        )
        cumsum_exp = _fake_scan(
            get_scan_combine_fn("add", False),
            init=init,
            xs=x,
            dim=0,
            reverse=reverse,
        )
        if not reverse:
            self.assertEqual(
                cumsum1[1], torch.tensor([0.0, 1.0, 3.0, 6.0], dtype=torch.int64)
            )
            self.assertEqual(cumsum1[0], torch.tensor([6.0], dtype=torch.int64))
        else:
            self.assertEqual(
                cumsum1[1], torch.tensor([6.0, 6.0, 5.0, 3.0], dtype=torch.int64)
            )
            self.assertEqual(cumsum1[0], torch.tensor([6.0], dtype=torch.int64))
        self.assertEqual(cumsum1, cumsum_exp)

        # Different carry computation as output computation
        x = torch.arange(1, 5, device=device, dtype=torch.int64)
        init = torch.ones(1, device=device, dtype=torch.int64)
        result = scan_fct(add2, init, x, dim=0, reverse=reverse)
        result_exp = _fake_scan(add2, init=init, xs=x, dim=0, reverse=reverse)
        if not reverse:
            self.assertEqual(
                result[1], torch.tensor([2.0, 3.0, 5.0, 10.0], dtype=torch.int64)
            )
            self.assertEqual(result[0], torch.tensor([24.0], dtype=torch.int64))
        else:
            self.assertEqual(
                result[1], torch.tensor([25.0, 14.0, 7.0, 5.0], dtype=torch.int64)
            )
            self.assertEqual(result[0], torch.tensor([24.0], dtype=torch.int64))
        self.assertEqual(result, result_exp)

        # Non associative operation
        x = torch.arange(0, 5, device=device, dtype=torch.float32)
        init = torch.ones(1, device=device, dtype=torch.float32)
        result = scan_fct(
            get_scan_combine_fn("div", False),
            init,
            x,
            dim=0,
            reverse=reverse,
        )
        result_exp = _fake_scan(
            get_scan_combine_fn("div", False),
            init=init,
            xs=x,
            dim=0,
            reverse=reverse,
        )
        self.assertEqual(result, result_exp)

    # TODO: provide an implementation for all compile modes and re-enable all test
    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("compile_mode", ["none", "eager"])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    @parametrize(
        "dtype",
        [
            torch.float16,
            torch.float32,
            torch.int32,
            torch.int64,
            torch.complex64,
        ],
    )
    def test_scan_dtype(self, reverse, compile_mode, device, dtype):
        scan_fct = compile_mode_helper(scan, compile_mode)

        # Check all outputs and carries on the correct device and with torch.float32
        x = torch.randn(3, 10, 2, device=device).to(dtype=dtype)
        op, init = (
            get_scan_combine_fn("adds"),
            torch.zeros(1, 10, 2, device=device, dtype=dtype),
        )
        result = scan_fct(op, init, x, dim=0, reverse=reverse)
        result_exp = _fake_scan(op, init=init, xs=x, dim=0, reverse=reverse)
        self.assertEqual(result, result_exp)
        self.assertEqual(
            [[r.device.type for r in res] for res in result],
            [[device.type for _ in res] for res in result],
        )
        self.assertEqual(
            [[r.dtype for r in res] for res in result],
            [[dtype for _ in res] for res in result],
        )

        # Check all outputs and carries on the correct device and
        # carry.dtype torch.float32 and output.dtype torch.float16
        x = torch.randn(3, 10, 2, device=device).to(dtype=dtype)
        op, init = (
            get_scan_combine_fn("adds"),
            torch.zeros(1, 10, 2, device=device, dtype=torch.float32),
        )
        result = scan_fct(op, init, x, dim=0, reverse=reverse)
        result_exp = _fake_scan(op, init=init, xs=x, dim=0, reverse=reverse)
        self.assertEqual(result, result_exp)
        self.assertEqual(
            [[r.dtype for r in res] for res in result],
            [
                [torch.float32 for _ in range(len(result[0]))],
                [dtype for _ in range(len(result[1]))],
            ],
        )

        # Check all outputs and carries on the correct device and
        # carry.dtype torch.int64 and output.dtype torch.float32
        x = torch.randn(3, 10, 2, device=device)
        op, init = (
            get_scan_combine_fn("adds"),
            torch.zeros(1, 10, 2, device=device, dtype=dtype),
        )
        result = scan_fct(op, init, x, dim=0, reverse=reverse)
        result_exp = _fake_scan(op, init=init, xs=x, dim=0, reverse=reverse)
        self.assertEqual(result, result_exp)
        self.assertEqual(
            [[r.dtype for r in res] for res in result],
            [
                [dtype for _ in range(len(result[0]))],
                [torch.float32 for _ in range(len(result[1]))],
            ],
        )

    @unittest.skipIf(not SM70OrLater, "triton")
    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("combine_mode", ["pointwise", "generic"])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    # Skipping the combination of combine_mode=pointwise and device=cpu
    # as the current implementation of pointwise does only support CUDA device
    @decorateIf(
        unittest.skip,
        lambda params: (
            params["combine_mode"] == "pointwise"
            and (params["device"] == torch.device("cpu") or torch.version.hip)
        ),
    )
    def test_associative_scan_dim(self, combine_mode, reverse, device):
        import random

        num_dims = [random.randint(2, 5) for _ in range(10)]
        for num_dim in num_dims:
            shapes = [random.randint(1, 10) for _ in range(num_dim)]
            rnd_scan_dim = random.randint(0, num_dim - 1)
            x = torch.randn(*shapes, device=device)

            for op, op_pt in [
                (get_scan_combine_fn("add", True), torch.cumsum),
                (get_scan_combine_fn("mul", True), torch.cumprod),
            ]:
                result = associative_scan(
                    op, x, rnd_scan_dim, reverse=reverse, combine_mode=combine_mode
                )
                result_exp = _fake_associative_scan(
                    op, x, rnd_scan_dim, reverse=reverse
                )
                self.assertEqual(result, result_exp)
                if not reverse:
                    result_exp_PT = op_pt(x, rnd_scan_dim)
                    self.assertEqual(result, result_exp_PT)

    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_dim(self, reverse, device):
        import random

        num_dims = [random.randint(2, 5) for _ in range(10)]
        for num_dim in num_dims:
            shapes = [random.randint(1, 10) for _ in range(num_dim)]
            rnd_scan_dim = random.randint(0, num_dim - 1)
            x = torch.randn(*shapes, device=device)
            init_shapes = shapes
            init_shapes[rnd_scan_dim] = 1

            for op, op_pt, init in [
                (
                    get_scan_combine_fn("add", False),
                    torch.cumsum,
                    torch.zeros(*init_shapes, device=device),
                ),
                (
                    get_scan_combine_fn("mul", False),
                    torch.cumprod,
                    torch.ones(*init_shapes, device=device),
                ),
            ]:
                result = scan(op, init, x, dim=rnd_scan_dim, reverse=reverse)
                result_exp = _fake_scan(
                    op, init=init, xs=x, dim=rnd_scan_dim, reverse=reverse
                )
                self.assertEqual(result, result_exp)
                if not reverse:
                    result_exp_PT = op_pt(x, rnd_scan_dim)
                    self.assertEqual(result[1], result_exp_PT)

    @skipIfRocm(msg="Unsupported on ROCM yet")
    @unittest.skipIf(not SM70OrLater, "triton")
    @requires_cuda
    @parametrize("combine_mode", ["pointwise", "generic"])
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    # Skipping the combination of combine_mode=pointwise and device=cpu
    # as the current implementation of pointwise does only support CUDA device
    @decorateIf(
        unittest.skip,
        lambda params: (
            params["combine_mode"] == "pointwise"
            and (params["device"] == torch.device("cpu") or torch.version.hip)
        ),
    )
    def test_associative_scan_binary_operator(self, combine_mode, reverse, device):
        state_dim = 20
        timesteps = 10
        projected_inputs = torch.randn(
            timesteps, state_dim, requires_grad=True, device=device
        )
        A = torch.randn(state_dim, requires_grad=True, device=device)
        elements = (A.repeat((timesteps, 1)), projected_inputs)

        result1 = associative_scan(
            get_scan_combine_fn("s5_operator", True),
            elements,
            0,
            combine_mode=combine_mode,
            reverse=reverse,
        )
        expected_result = _fake_associative_scan(
            get_scan_combine_fn("s5_operator", True), elements, 0, reverse=reverse
        )
        self.assertEqual(
            result1,
            expected_result,
        )
        self.assertEqual([r.device.type for r in result1], [device.type] * len(result1))

    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_binary_operator(self, reverse, device):
        state_dim = 20
        timesteps = 10
        projected_inputs = torch.randn(
            timesteps, state_dim, requires_grad=True, device=device
        )
        A = torch.randn(state_dim, requires_grad=True, device=device)
        elements = (A.repeat((timesteps, 1)), projected_inputs)
        init = tuple(
            [torch.ones_like(torch._ops.ops.aten.slice(elements[0], 0, 0, 1, 1))]
            + [
                torch.zeros_like(
                    torch._ops.ops.aten.slice(projected_inputs, 0, 0, 1, 1)
                )
            ]
        )

        result = scan(
            get_scan_combine_fn("s5_operator", False),
            init,
            elements,
            dim=0,
            reverse=reverse,
        )
        expected_result = _fake_scan(
            get_scan_combine_fn("s5_operator", False),
            init=init,
            xs=elements,
            dim=0,
            reverse=reverse,
        )
        self.assertEqual(result, expected_result)

    @skipIfRocm(msg="Unsupported on ROCM yet")
    @unittest.skipIf(not SM70OrLater, "triton")
    @requires_cuda
    @parametrize("combine_mode", ["pointwise", "generic"])
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    # Skipping the combination of combine_mode=pointwise and device=cpu
    # as the current implementation of pointwise does only support CUDA device
    @decorateIf(
        unittest.skip,
        lambda params: (
            params["combine_mode"] == "pointwise"
            and (params["device"] == torch.device("cpu") or torch.version.hip)
        ),
    )
    def test_associative_scan_tuple(self, combine_mode, reverse, device):
        x = torch.randn(3, 2, 2, device=device)
        y = torch.randn(3, 2, 2, device=device)
        inp = (x, y)

        result1 = associative_scan(
            get_scan_combine_fn("tuple_fct", True),
            inp,
            0,
            reverse=reverse,
            combine_mode=combine_mode,
        )
        expected_result = _fake_associative_scan(
            get_scan_combine_fn("tuple_fct", True), inp, 0, reverse=reverse
        )
        self.assertEqual(result1, expected_result)

    @skipIfRocm(msg="Unsupported on ROCM yet")
    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_tuple(self, reverse, device):
        x = torch.randn(3, 2, 2, device=device)
        y = torch.randn(3, 2, 2, device=device)
        inp = (x, y)
        init = tuple(torch._ops.ops.aten.slice(e, 0, 0, 1, 1) for e in inp)

        result_same = scan(
            get_scan_combine_fn("tuple_fct", False),
            init,
            inp,
            dim=0,
            reverse=reverse,
        )
        expected_result = _fake_scan(
            get_scan_combine_fn("tuple_fct", False),
            init=init,
            xs=inp,
            dim=0,
            reverse=reverse,
        )
        self.assertEqual(result_same, expected_result)

        def fct_different_output_tuple(x, y):
            return ((x[0] + y[0], x[1] * y[1]), (x[1] * y[1]))

        inp = (x, y)
        init = tuple(torch._ops.ops.aten.slice(e, 0, 0, 1, 1) for e in inp)

        result_diff = scan(
            fct_different_output_tuple, init, inp, dim=0, reverse=reverse
        )
        expected_result = _fake_scan(
            fct_different_output_tuple, init=init, xs=inp, dim=0, reverse=reverse
        )
        self.assertEqual(result_diff, expected_result)
        self.assertEqual(result_diff[1], result_same[1][1])

    @unittest.skipIf(not SM70OrLater, "triton")
    @requires_cuda
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_associative_scan_wrong_pytree(self, device):
        def fct_wrong_pytree(x, y):
            return {
                "i": x["i"] * y["j"][0][0],
                "k": 0.0,
                "j": ([x["j"][1][0]["o"]], [{"o": torch.sin(x["i"])}]),
            }

        x = torch.randn(3, 2, 2, device=device)
        y = torch.randn(3, 2, 2, device=device)
        z = torch.randn(3, 2, 2, device=device)
        inp = {"i": x, "j": ([y], [{"o": z}])}

        with self.assertRaisesRegex(
            # Should be: RuntimeError,
            # r"The number of leaves of the pytree of the output of the operator
            # needs to match the lenght of the pytree of the input",
            torch._dynamo.exc.Unsupported,
            "Observed exception.*",
        ):
            result = associative_scan(fct_wrong_pytree, inp, 0, combine_mode="generic")

    @unittest.skipIf(not SM70OrLater, "triton")
    @requires_cuda
    @parametrize("combine_mode", ["pointwise", "generic"])
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    # Skipping the combination of combine_mode=pointwise and device=cpu
    # as the current implementation of pointwise does only support CUDA device
    @decorateIf(
        unittest.skip,
        lambda params: (
            params["combine_mode"] == "pointwise"
            and (params["device"] == torch.device("cpu") or torch.version.hip)
        ),
    )
    def test_associative_scan_complex_pytree(self, combine_mode, reverse, device):
        def fct_pointwise(x, y):
            return {
                "i": x["i"] * y["i"],
                "j": (
                    [x["j"][0][0] * y["j"][0][0]],
                    [{"o": x["j"][1][0]["o"] + y["j"][1][0]["o"]}],
                ),
            }

        x = torch.randn(3, 2, 2, device=device)
        y = torch.randn(3, 2, 2, device=device)
        z = torch.randn(3, 2, 2, device=device)
        inp = {"i": x, "j": ([y], [{"o": z}])}

        result = associative_scan(
            get_scan_combine_fn("complex_pointwise", True),
            inp,
            0,
            combine_mode=combine_mode,
            reverse=reverse,
        )
        expected_result = _fake_associative_scan(
            get_scan_combine_fn("complex_pointwise", True), inp, 0, reverse=reverse
        )
        self.assertEqual(result, expected_result)

    @requires_cuda
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_wrong_pytree(self, device):
        # Init and input have same pytree
        def fct_wrong_pytree(x, y):
            return (
                {
                    "i": x["i"] * y["j"][0][0],
                    "k": 0.0,
                    "j": ([x["j"][1][0]["o"]], [{"o": torch.sin(x["i"])}]),
                },
                {
                    "i": x["i"] * y["j"][0][0],
                    "k": 0.0,
                    "j": ([x["j"][1][0]["o"]], [{"o": torch.sin(x["i"])}]),
                },
            )

        x = torch.randn(3, 2, 2, device=device)
        y = torch.randn(3, 2, 2, device=device)
        z = torch.randn(3, 2, 2, device=device)
        inp = {"i": x, "j": ([y], [{"o": z}])}
        inp_flat, inp_spec = pytree.tree_flatten(inp)
        init_flat = [torch._ops.ops.aten.slice(e, 0, 0, 1, 1) for e in inp_flat]
        init = pytree.tree_unflatten(init_flat, inp_spec)

        with self.assertRaisesRegex(
            # Should be: RuntimeError,
            # r"The number of leaves of the pytree of the new carry produced by
            # the operator needs to match the length of the pytree of the init",
            torch._dynamo.exc.Unsupported,
            "Observed exception.*",
        ):
            result = scan(fct_wrong_pytree, init, inp, dim=0)

    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_complex_pytree(self, reverse, device):
        # Init and input have same pytree

        x = torch.randn(3, 2, 2, device=device)
        y = torch.randn(3, 2, 2, device=device)
        z = torch.randn(3, 2, 2, device=device)
        inp = {"i": x, "j": ([y], [{"o": z}])}
        inp_flat, inp_spec = pytree.tree_flatten(inp)
        init_flat = [torch._ops.ops.aten.slice(e, 0, 0, 1, 1) for e in inp_flat]
        init = pytree.tree_unflatten(init_flat, inp_spec)

        result = scan(
            get_scan_combine_fn("complex_pointwise", False),
            init,
            inp,
            dim=0,
            reverse=reverse,
        )
        expected_result = _fake_scan(
            get_scan_combine_fn("complex_pointwise", False),
            init=init,
            xs=inp,
            dim=0,
            reverse=reverse,
        )
        self.assertEqual(result, expected_result)

    # TODO: provide an implementation for all compile modes and re-enable all test
    @unittest.skipIf(not SM70OrLater, "triton")
    @requires_cuda
    @parametrize("combine_mode", ["pointwise", "generic"])
    @parametrize("compile_mode", ["none", "compile", "compile_dynamic_shape"])
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    # Skipping the combination of combine_mode=pointwise and device=cpu
    # as the current implementation of pointwise does only support CUDA device
    @decorateIf(
        unittest.skip,
        lambda params: (
            params["combine_mode"] == "pointwise"
            and (params["device"] == torch.device("cpu") or torch.version.hip)
        ),
    )
    def test_associative_scan_downstream_scan_matmul(
        self, combine_mode, compile_mode, reverse, device
    ):
        # Chain with matmul
        def chain_fct(inp):
            W = torch.ones(2, 5, device=device)
            o = associative_scan(
                get_scan_combine_fn("add", True),
                inp,
                1,
                reverse=reverse,
                combine_mode=combine_mode,
            )
            return o @ W

        fct_cmp = compile_mode_helper(chain_fct, compile_mode)

        inp = torch.randn(3, 10, 2, device=device)
        expected_result = _fake_associative_scan(
            get_scan_combine_fn("add", True), inp, 1, reverse=reverse
        ) @ torch.ones(2, 5, device=device)
        result1 = fct_cmp(inp)
        self.assertEqual(result1, expected_result)

    # TODO: provide an implementation for all compile modes and re-enable all test
    @unittest.skipIf(not SM70OrLater, "triton")
    @requires_cuda
    @parametrize("combine_mode", ["pointwise", "generic"])
    @parametrize("compile_mode", ["none", "compile", "compile_dynamic_shape"])
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    # Skipping the combination of combine_mode=pointwise and device=cpu
    # as the current implementation of pointwise does only support CUDA device
    @decorateIf(
        unittest.skip,
        lambda params: (
            params["combine_mode"] == "pointwise"
            and (params["device"] == torch.device("cpu") or torch.version.hip)
        ),
    )
    def test_associative_scan_downstream_scan_scan(
        self, combine_mode, compile_mode, reverse, device
    ):
        # Chain with scan
        def chain_fct_same_dim(inp):
            o1 = associative_scan(
                get_scan_combine_fn("add", True),
                inp,
                1,
                combine_mode=combine_mode,
                reverse=reverse,
            )
            o2 = associative_scan(
                get_scan_combine_fn("add", True),
                o1,
                1,
                combine_mode=combine_mode,
                reverse=reverse,
            )
            return o2

        fct_cmp = compile_mode_helper(chain_fct_same_dim, compile_mode)

        inp = torch.randn(3, 10, 2, device=device)

        expected_result = _fake_associative_scan(
            get_scan_combine_fn("add", True),
            _fake_associative_scan(
                get_scan_combine_fn("add", True), inp, 1, reverse=reverse
            ),
            1,
            reverse=reverse,
        )
        result1 = fct_cmp(inp)
        self.assertEqual(result1, expected_result)

    # TODO: provide an implementation for all compile modes and re-enable all test
    @unittest.skipIf(not SM70OrLater, "triton")
    @requires_cuda
    @parametrize("combine_mode", ["pointwise", "generic"])
    @parametrize("compile_mode", ["none", "compile", "compile_dynamic_shape"])
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    # Skipping the combination of combine_mode=pointwise and device=cpu
    # as the current implementation of pointwise does only support CUDA device
    @decorateIf(
        unittest.skip,
        lambda params: (
            params["combine_mode"] == "pointwise"
            and (params["device"] == torch.device("cpu") or torch.version.hip)
        ),
    )
    def test_associative_scan_downstream_scan_scan_different_dim(
        self, combine_mode, compile_mode, reverse, device
    ):
        # Chain with scan on different dim
        def chain_fct_different_dim(inp):
            o1 = associative_scan(
                get_scan_combine_fn("add", True),
                inp,
                1,
                combine_mode=combine_mode,
                reverse=reverse,
            )
            o2 = associative_scan(
                get_scan_combine_fn("add", True),
                o1,
                0,
                combine_mode=combine_mode,
                reverse=reverse,
            )
            return o2

        fct_cmp = compile_mode_helper(chain_fct_different_dim, compile_mode)

        inp = torch.randn(3, 10, 2, device=device)
        expected_result = _fake_associative_scan(
            get_scan_combine_fn("add", True),
            _fake_associative_scan(
                get_scan_combine_fn("add", True), inp, 1, reverse=reverse
            ),
            0,
            reverse=reverse,
        )
        result1 = fct_cmp(inp)
        self.assertEqual(result1, expected_result)

    # TODO: provide an implementation for all compile modes and re-enable all test
    @requires_cuda
    @parametrize("compile_mode", ["none", "eager"])
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_downstream_scan_matmul(self, compile_mode, reverse, device):
        inp = torch.randn(3, 10, 2, device=device)
        init = torch.randn(3, 1, 2, device=device)

        for ind in range(2):
            # Chain with matmul
            def chain_fct(inp):
                W = torch.ones(2, 5, device=device)
                o = scan(
                    get_scan_combine_fn("add", False),
                    init,
                    inp,
                    dim=1,
                    reverse=reverse,
                )
                return o[ind] @ W

            fct_cmp = compile_mode_helper(chain_fct, compile_mode)

            expected_result = _fake_scan(
                get_scan_combine_fn("add", False),
                init=init,
                xs=inp,
                dim=1,
                reverse=reverse,
            )[ind] @ torch.ones(2, 5, device=device)
            result1 = fct_cmp(inp)
            self.assertEqual(result1, expected_result)

    # TODO: provide an implementation for all compile modes and re-enable all test
    @requires_cuda
    @parametrize("compile_mode", ["none", "eager"])
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_downstream_scan_scan(self, compile_mode, reverse, device):
        inp = torch.randn(3, 10, 2, device=device)
        init = torch.randn(3, 1, 2, device=device)

        # Chain with scan
        def chain_fct_same_dim(inp):
            o1 = scan(
                get_scan_combine_fn("add", False),
                init,
                inp,
                dim=1,
                reverse=reverse,
            )
            o2 = scan(
                get_scan_combine_fn("add", False),
                init,
                o1[1],
                dim=1,
                reverse=reverse,
            )
            return o2

        fct_cmp = compile_mode_helper(chain_fct_same_dim, compile_mode)

        expected_result = _fake_scan(
            get_scan_combine_fn("add", False),
            init=init,
            xs=_fake_scan(
                get_scan_combine_fn("add", False),
                init=init,
                xs=inp,
                dim=1,
                reverse=reverse,
            )[1],
            dim=1,
            reverse=reverse,
        )
        result1 = fct_cmp(inp)
        self.assertEqual(result1, expected_result)

    # TODO: provide an implementation for all compile modes and re-enable all test
    @requires_cuda
    @parametrize("compile_mode", ["none", "eager"])
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_downstream_scan_scan_dim(self, compile_mode, reverse, device):
        inp = torch.randn(3, 10, 2, device=device)
        init = torch.randn(3, 1, 2, device=device)

        # Chain with scan on different dim
        init2 = torch.randn(1, 10, 2, device=device)

        def chain_fct_different_dim(inp):
            o1 = scan(
                get_scan_combine_fn("add", False),
                init,
                inp,
                dim=1,
                reverse=reverse,
            )
            o2 = scan(
                get_scan_combine_fn("add", False),
                init2,
                o1[1],
                dim=0,
                reverse=reverse,
            )
            return o2

        fct_cmp = compile_mode_helper(chain_fct_different_dim, compile_mode)

        expected_result = _fake_scan(
            get_scan_combine_fn("add", False),
            init=init2,
            xs=_fake_scan(
                get_scan_combine_fn("add", False),
                init=init,
                xs=inp,
                dim=1,
                reverse=reverse,
            )[1],
            dim=0,
            reverse=reverse,
        )
        result1 = fct_cmp(inp)
        self.assertEqual(result1, expected_result)

    @unittest.skipIf(not SM70OrLater, "triton")
    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    # Skipping the combination of associative_scan and device=cpu
    # as the current implementation of pointwise does only support CUDA device
    @decorateIf(
        unittest.skip,
        lambda params: (params["device"] == torch.device("cpu")),
    )
    def test_associative_scan_non_pointwise(self, reverse, device):
        x = torch.randn(3, 10, 2, device=device)
        # Expected to fail, as the pointwise combine_mode does not allow non-pointwise operations
        with self.assertRaisesRegex(
            Exception,
            "For combine_mode='pointwise', the combine_fn needs to be pointwise",
        ):
            out = associative_scan(
                get_scan_combine_fn("non_pointwise", True),
                x,
                0,
                reverse=reverse,
                combine_mode="pointwise",
            )

    @unittest.skipIf(not SM70OrLater, "triton")
    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    # Skipping the combination of associative_scan and device=cpu
    # as the current implementation of pointwise does only support CUDA device
    @decorateIf(
        unittest.skip,
        lambda params: (params["device"] == torch.device("cpu")),
    )
    def test_associative_scan_non_pointwise_generic(self, reverse, device):
        x = torch.randn(3, 10, 2, device=device)
        result_expected = _fake_associative_scan(
            get_scan_combine_fn("non_pointwise", True), x, 0, reverse=reverse
        )
        result1 = associative_scan(
            get_scan_combine_fn("non_pointwise", True),
            x,
            0,
            reverse=reverse,
            combine_mode="generic",
        )
        self.assertEqual(result1, result_expected)

    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_non_pointwise(self, reverse, device):
        x = torch.randn(3, 10, 2, device=device)
        init = torch.randn(1, 10, 2, device=device)
        result_expected = _fake_scan(
            get_scan_combine_fn("non_pointwise", False),
            init=init,
            xs=x,
            dim=0,
            reverse=reverse,
        )

        out = scan(
            get_scan_combine_fn("non_pointwise", False),
            init,
            x,
            dim=0,
            reverse=reverse,
        )
        self.assertEqual(out, result_expected)

    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_compile_cnt(self, reverse, device):
        dim = 1

        from torch._dynamo.testing import CompileCounter

        # Tests rely on automatic_dynamic = True
        with torch._dynamo.config.patch(automatic_dynamic_shapes=True):
            cnt = CompileCounter()
            x = torch.randn(3, 2, 5, device=device)
            init = torch.randn(3, 1, 5, device=device)
            # First compilation step
            torch.compile(scan, backend=cnt)(
                get_scan_combine_fn("add", False),
                init,
                x,
                dim=dim,
                reverse=reverse,
            )
            self.assertEqual(cnt.frame_count, 1)

            x = torch.randn(3, 20, 5, device=device)
            init = torch.randn(3, 1, 5, device=device)
            # Recompilation due to first different size
            torch.compile(scan, backend=cnt)(
                get_scan_combine_fn("add", False),
                init,
                x,
                dim=dim,
                reverse=reverse,
            )
            self.assertEqual(cnt.frame_count, 2)

            x = torch.randn(3, 40, 5, device=device)
            init = torch.randn(3, 1, 5, device=device)
            # No recompilation, because of dynamic shape
            torch.compile(scan, backend=cnt)(
                get_scan_combine_fn("add", False),
                init,
                x,
                dim=dim,
                reverse=reverse,
            )
            self.assertEqual(cnt.frame_count, 2)

            x = torch.randn(3, 40, 5, device=device)
            init = torch.randn(3, 40, 1, device=device)
            # Recompilation because of dim change
            torch.compile(scan, backend=cnt)(
                get_scan_combine_fn("add", False),
                init,
                x,
                dim=2,
                reverse=reverse,
            )
            self.assertEqual(cnt.frame_count, 3)

            x = torch.randn(3, 40, 20, device=device)
            init = torch.randn(3, 40, 1, device=device)
            # Recompilation due to first different size on new dim
            torch.compile(scan, backend=cnt)(
                get_scan_combine_fn("add", False),
                init,
                x,
                dim=2,
                reverse=reverse,
            )
            self.assertEqual(cnt.frame_count, 4)

            x = torch.randn(3, 40, 40, device=device)
            init = torch.randn(3, 40, 1, device=device)
            # No recompilation, because of dynamic shape on new dim
            torch.compile(scan, backend=cnt)(
                get_scan_combine_fn("add", False),
                init,
                x,
                dim=2,
                reverse=reverse,
            )
            self.assertEqual(cnt.frame_count, 4)

            x = torch.randn(3, 60, 40, device=device)
            init = torch.randn(3, 1, 40, device=device)
            # Recompilation because of dim change
            torch.compile(scan, backend=cnt)(
                get_scan_combine_fn("add", False),
                init,
                x,
                dim=1,
                reverse=reverse,
            )
            self.assertEqual(cnt.frame_count, 5)

            x = torch.randn(3, 60, 40, device=device)
            init = torch.randn(3, 1, 40, device=device)
            # Recompilation because of reverse change
            torch.compile(scan, backend=cnt)(
                get_scan_combine_fn("add", False),
                init,
                x,
                dim=1,
                reverse=not reverse,
            )
            self.assertEqual(cnt.frame_count, 6)

            x = torch.randn(3, 60, 40, device=device)
            init = torch.randn(3, 1, 40, device=device)
            # No recompilation, as nothing changed
            torch.compile(scan, backend=cnt)(
                get_scan_combine_fn("add", False),
                init,
                x,
                dim=1,
                reverse=not reverse,
            )
            self.assertEqual(cnt.frame_count, 6)

            x = torch.randn(3, 120, 80, device=device)
            init = torch.randn(3, 1, 80, device=device)
            # No recompilation, final test
            torch.compile(scan, backend=cnt)(
                get_scan_combine_fn("add", False),
                init,
                x,
                dim=1,
                reverse=reverse,
            )
            self.assertEqual(cnt.frame_count, 6)

    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("compile_mode", ["none", "eager"])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_init_scanned_0(self, reverse, compile_mode, device):
        scan_fct = compile_mode_helper(scan, compile_mode)

        # Only init and no input
        x = torch.randn(3, 1, 2, device=device)
        init = torch.randn(3, 1, 2, device=device)
        dim = 1

        # Scan dimension is 0
        init = torch._ops.ops.aten.slice(x, dim, 0, 1, 1)
        inp = torch._ops.ops.aten.slice(x, dim, 1, None, 1)
        with self.assertRaisesRegex(
            # Should be: RuntimeError, "Input leaves must have a scan dimension > 0"
            torch._dynamo.exc.Unsupported,
            "Observed exception.*",
        ):
            result_init = scan_fct(
                get_scan_combine_fn("add", False),
                init,
                inp,
                dim=dim,
                reverse=reverse,
            )

    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("compile_mode", ["none", "eager"])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_init_non_tensor(self, reverse, compile_mode, device):
        scan_fct = compile_mode_helper(scan, compile_mode)

        # Only init and no input
        x = torch.randn(3, 1, 2, device=device)
        init = torch.randn(3, 1, 2, device=device)
        dim = 1

        # Init is a float and not a tensor
        inp = torch._ops.ops.aten.slice(x, dim, 1, None, 1)
        init = 1.0
        with self.assertRaisesRegex(
            # Should be: RuntimeError, "Init leaves must be a Tensor"
            torch._dynamo.exc.Unsupported,
            "Observed exception.*",
        ):
            result_init = scan_fct(
                get_scan_combine_fn("add", False), init, inp, dim=dim, reverse=reverse
            )

    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("compile_mode", ["none", "eager"])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_init_wrong_shape(self, reverse, compile_mode, device):
        scan_fct = compile_mode_helper(scan, compile_mode)

        # Only init and no input
        x = torch.randn(3, 1, 2, device=device)
        init = torch.randn(3, 1, 2, device=device)
        dim = 1

        # Init wrong shape (Other dim different)
        inp = torch._ops.ops.aten.slice(x, dim, 1, None, 1)
        init = torch._ops.ops.aten.slice(x, dim, 0, 1, 1)
        init = torch.tile(init, (1, 2, 1))
        with self.assertRaisesRegex(
            # Should be: RuntimeError, "The size of tensor a.*"
            torch._dynamo.exc.Unsupported,
            "Observed exception.*",
        ):
            result_init = scan_fct(
                get_scan_combine_fn("add", False),
                init,
                inp,
                dim=dim,
                reverse=reverse,
            )

    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("compile_mode", ["none", "eager"])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_init_wrong_pytree(self, reverse, compile_mode, device):
        def add_one_carry(x: torch.Tensor, y: torch.Tensor):
            return x[0], x

        scan_fct = compile_mode_helper(scan, compile_mode)

        # Only init and no input
        x = torch.randn(3, 1, 2, device=device)
        init = torch.randn(3, 1, 2, device=device)
        dim = 1

        # Init wrong pytree
        inp = torch._ops.ops.aten.slice(x, dim, 1, None, 1)
        init = (
            torch._ops.ops.aten.slice(x, dim, 0, 1, 1),
            torch._ops.ops.aten.slice(x, dim, 0, 1, 1),
        )

        with self.assertRaisesRegex(
            # Should be: RuntimeError: The number of leaves of the pytree of the new carry produced
            # by the operator needs to match the length of the pytree of the init
            torch._dynamo.exc.Unsupported,
            "Observed exception.*",
        ):
            result_init = scan_fct(add_one_carry, init, inp, dim=dim, reverse=reverse)

    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("compile_mode", ["none", "eager"])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_init(self, reverse, compile_mode, device):
        scan_fct = compile_mode_helper(scan, compile_mode)

        # Only init and no input
        x = torch.randn(3, 1, 2, device=device)
        init = torch.randn(3, 1, 2, device=device)
        dim = 1
        op, op_pt = (get_scan_combine_fn("add", False), torch.cumsum)

        # Only init given
        init = torch._ops.ops.aten.slice(x, dim, 0, 1, 1)
        result = scan_fct(op, init, [], dim=dim, reverse=reverse)
        result_exp = _fake_scan(op, init=init, xs=[], dim=dim, reverse=reverse)
        result_init = scan_fct(op, init, [], dim=dim, reverse=reverse)
        self.assertEqual(result, result_exp)
        self.assertEqual(result_init, result_exp)
        self.assertEqual(result_init[0], init)

        x = torch.randn(3, 5, 2, device=device)
        init = torch.randn(3, 5, 2, device=device)
        dim = 0

        op, op_pt = (get_scan_combine_fn("add", False), torch.cumsum)
        inp = torch._ops.ops.aten.slice(x, dim, 1, None, 1)

        # Init tensor scalar
        init = torch.ones(1, device=device)

        def add_scalar_carry(x: torch.Tensor, y: torch.Tensor):
            return x + 1.0, x + y

        result_init = scan_fct(add_scalar_carry, init, inp, dim=dim, reverse=reverse)
        result_exp = _fake_scan(
            add_scalar_carry, init=init, xs=inp, dim=dim, reverse=reverse
        )
        self.assertEqual(result_init, result_exp)
        self.assertEqual(result_init[0], torch.tensor([3.0], device=device))

        # Init tensor entirely different shape than inp
        init = torch.randn(7, 8, device=device)

        def add_scalar_carry2(x: torch.Tensor, y: torch.Tensor):
            return x + 1.0, x[: y.shape[1], : y.shape[2]] + y

        result_init = scan_fct(add_scalar_carry2, init, inp, dim=dim, reverse=reverse)
        result_exp = _fake_scan(
            add_scalar_carry2, init=init, xs=inp, dim=dim, reverse=reverse
        )
        self.assertEqual(result_init, result_exp)

        # Init with two timestep on dim axis. Should work as y has always 1 on dim axis and
        # hence automatic broadcasting should work
        # I.e., the input shape is 2x5x2, but the carry at each iteration is 2x5x2,
        # thus the output of each iteration is 2x5x2, which results in the total output
        # to be 4x5x2
        init = torch._ops.ops.aten.slice(x, dim, 0, 2, 1)
        result_init = scan_fct(op, init, inp, dim=dim, reverse=reverse)
        result_exp = _fake_scan(op, init=init, xs=inp, dim=dim, reverse=reverse)
        self.assertEqual(result_init, result_exp)
        self.assertEqual(result_init[0].shape, torch.Size([2, 5, 2]))

        init = torch.tile(init, (1, 2, 1))

        def add_scalar_carry_sliced_out(x: torch.Tensor, y: torch.Tensor):
            return x + 1.0, x[:, :1, :] + y

        result_init = scan_fct(
            add_scalar_carry_sliced_out, init, inp, dim=dim, reverse=reverse
        )
        result_exp = _fake_scan(
            add_scalar_carry_sliced_out, init=init, xs=inp, dim=dim, reverse=reverse
        )
        self.assertEqual(result_init, result_exp)
        self.assertEqual(result_init[0].shape, torch.Size([2, 10, 2]))
        self.assertEqual(result_init[1].shape, torch.Size([4, 5, 2]))

        # Correct case
        op, op_pt = (get_scan_combine_fn("add", False), torch.cumsum)
        x = torch.randn(3, 2, 2, device=device)
        dim = 1

        if reverse:
            init = torch.zeros_like(torch._ops.ops.aten.slice(x, dim, -1, None, 1))
            inp = torch._ops.ops.aten.slice(x, dim, 0, -1, 1)
        else:
            init = torch.zeros_like(torch._ops.ops.aten.slice(x, dim, 0, 1, 1))
            inp = torch._ops.ops.aten.slice(x, dim, 1, None, 1)

        result = scan_fct(op, init, x, dim=dim, reverse=reverse)
        result_exp = _fake_scan(op, init=init, xs=x, dim=dim, reverse=reverse)

        self.assertEqual(result, result_exp)
        if not reverse:
            result_exp_PT = op_pt(x, dim)
            self.assertEqual(result[1], result_exp_PT)

    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_carry_wrong_pytree(self, reverse, device):
        def fct_pointwise_carry_wrong_pytree(x, y):
            return (
                (
                    x["i"],
                    {
                        "i": x["i"] * y["i"],
                        "j": (
                            [x["j"][0][0] * y["j"][0][0]],
                            [{"o": x["j"][1][0]["o"] + y["j"][1][0]["o"]}],
                        ),
                    },
                ),
                {
                    "i": x["i"] * y["i"],
                    "j": (
                        [x["j"][0][0] * y["j"][0][0]],
                        [{"o": x["j"][1][0]["o"] + y["j"][1][0]["o"]}],
                    ),
                },
            )

        x = torch.randn(3, 2, 2, device=device)
        y = torch.randn(3, 2, 2, device=device)
        z = torch.randn(3, 2, 2, device=device)
        inp = {"i": x, "j": ([y], [{"o": z}])}
        inp_flat, inp_spec = pytree.tree_flatten(inp)
        init_flat = [torch._ops.ops.aten.slice(e, 0, 0, 1, 1) for e in inp_flat]
        init = pytree.tree_unflatten(init_flat, inp_spec)

        # Wrong pytree of the carry produced by the operation
        with self.assertRaisesRegex(
            # Should be: RuntimeError: The number of leaves of the pytree of the new carry
            # produced by the operator needs to match the length of the pytree of the init
            torch._dynamo.exc.Unsupported,
            "Observed exception.*",
        ):
            result = scan(
                fct_pointwise_carry_wrong_pytree,
                init,
                inp,
                dim=0,
                reverse=reverse,
            )

    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_init_wrong_pytree_complex(self, reverse, device):
        x = torch.randn(3, 2, 2, device=device)
        y = torch.randn(3, 2, 2, device=device)
        z = torch.randn(3, 2, 2, device=device)

        # Wrong pytree fed to the function
        init = {
            "i": torch._ops.ops.aten.slice(x, 0, 0, 1, 1),
            "j": (
                {"a": torch._ops.ops.aten.slice(x, 0, 0, 1, 1)},
                [torch._ops.ops.aten.slice(y, 0, 0, 1, 1)],
                [{"o": torch._ops.ops.aten.slice(z, 0, 0, 1, 1)}],
            ),
        }
        inp = {
            "i": torch._ops.ops.aten.slice(x, 0, 0, None, 1),
            "j": (
                [torch._ops.ops.aten.slice(y, 0, 0, None, 1)],
                [{"o": torch._ops.ops.aten.slice(z, 0, 0, None, 1)}],
            ),
        }
        with self.assertRaisesRegex(
            Exception,
            ".*",
        ):
            result = scan(
                get_scan_combine_fn("complex_pointwise", False),
                init,
                inp,
                dim=0,
                reverse=reverse,
            )

    @requires_cuda
    @parametrize("reverse", [False, True])
    @parametrize("device", [torch.device("cpu"), torch.device("cuda")])
    def test_scan_init_pytree_complex(self, reverse, device):
        def fct_pointwise_different_output(x, y):
            return (
                {
                    "i": x["i"] * y["i"],
                    "j": (
                        [x["j"][0][0] * y["j"][0][0]],
                        [{"o": x["j"][1][0]["o"] + y["j"][1][0]["o"]}],
                    ),
                },
                (
                    y["i"],
                    {
                        "o": x["i"] * y["i"],
                        "j": (
                            [x["j"][0][0] * y["j"][0][0]],
                            [{"o": x["j"][1][0]["o"] + y["j"][1][0]["o"]}],
                        ),
                    },
                ),
            )

        def fct_pointwise_different_carry(x, y):
            return (
                {
                    "i": x["i"] * y["i"],
                    "j": (
                        x["i"],
                        [x["j"][1][0] * y["j"][0][0]],
                        [{"o": x["j"][2][0]["o"] + y["j"][1][0]["o"]}],
                    ),
                },
                (
                    y["i"],
                    {
                        "o": x["i"] * y["i"] + x["j"][0][0],
                        "j": (
                            [x["j"][1][0] * y["j"][0][0]],
                            [{"o": x["j"][2][0]["o"] + y["j"][1][0]["o"]}],
                        ),
                    },
                ),
            )

        x = torch.randn(3, 2, 2, device=device)
        y = torch.randn(3, 2, 2, device=device)
        z = torch.randn(3, 2, 2, device=device)

        if reverse:
            init_start, init_end = -1, None
            inp_start, inp_end = 0, -1
        else:
            init_start, init_end = 0, 1
            inp_start, inp_end = 1, None

        # Regular case
        init = {
            "i": torch._ops.ops.aten.slice(x, 0, init_start, init_end, 1),
            "j": (
                [torch._ops.ops.aten.slice(y, 0, init_start, init_end, 1)],
                [{"o": torch._ops.ops.aten.slice(z, 0, init_start, init_end, 1)}],
            ),
        }
        inp = {
            "i": torch._ops.ops.aten.slice(x, 0, inp_start, inp_end, 1),
            "j": (
                [torch._ops.ops.aten.slice(y, 0, inp_start, inp_end, 1)],
                [{"o": torch._ops.ops.aten.slice(z, 0, inp_start, inp_end, 1)}],
            ),
        }
        result = scan(
            get_scan_combine_fn("complex_pointwise", False),
            init,
            inp,
            dim=0,
            reverse=reverse,
        )
        expected_result = _fake_scan(
            get_scan_combine_fn("complex_pointwise", False),
            init,
            inp,
            dim=0,
            reverse=reverse,
        )
        self.assertEqual(result, expected_result)

        # Pytree of output is different
        result = scan(fct_pointwise_different_output, init, inp, dim=0, reverse=reverse)
        expected_result = _fake_scan(
            fct_pointwise_different_output, init=init, xs=inp, dim=0, reverse=reverse
        )
        self.assertEqual(result, expected_result)

        # Pytree of carry is different
        init = {
            "i": torch._ops.ops.aten.slice(x, 0, init_start, init_end, 1),
            "j": (
                torch._ops.ops.aten.slice(x, 0, init_start, init_end, 1),
                [torch._ops.ops.aten.slice(y, 0, init_start, init_end, 1)],
                [{"o": torch._ops.ops.aten.slice(z, 0, init_start, init_end, 1)}],
            ),
        }
        inp = {
            "i": torch._ops.ops.aten.slice(x, 0, inp_start, inp_end, 1),
            "j": (
                [torch._ops.ops.aten.slice(y, 0, inp_start, inp_end, 1)],
                [{"o": torch._ops.ops.aten.slice(z, 0, inp_start, inp_end, 1)}],
            ),
        }
        result = scan(fct_pointwise_different_carry, init, inp, dim=0, reverse=reverse)
        expected_result = _fake_scan(
            fct_pointwise_different_carry, init=init, xs=inp, dim=0, reverse=reverse
        )
        self.assertEqual(result, expected_result)

    def test_scan_RNN(self):
        dim = 1
        device = torch.device("cpu")

        rnn = torch.nn.RNN(
            input_size=5,
            hidden_size=7,
        )
        rnn = rnn.to(device=device)
        x = torch.randn(1, 2, 5, device=device)
        h = torch.randn(1, 2, 7, device=device)

        new_state_dict = {
            "weight_ih_l0": torch.ones_like(rnn.weight_ih_l0),
            "bias_ih_l0": torch.ones_like(rnn.bias_ih_l0),
            "weight_hh_l0": torch.ones_like(rnn.weight_hh_l0),
            "bias_hh_l0": torch.ones_like(rnn.bias_hh_l0),
        }
        rnn.load_state_dict(new_state_dict)

        def RNN(x: torch.Tensor, y: torch.Tensor):
            W_ih = torch.ones((5, 7), device=device)
            b_ih = torch.ones((7), device=device)
            W_hh = torch.ones((7, 7), device=device)
            b_hh = torch.ones((7), device=device)
            c_new = y @ W_ih + b_ih
            h_new = torch.tanh(c_new + x @ W_hh + b_hh)
            return h_new, h_new

        expected_result = rnn(
            torch.permute(x, (1, 0, 2)), torch.unsqueeze(h[:, 0, :], 0)
        )
        expected_result_out = torch.permute(expected_result[0], (1, 0, 2))
        expected_result_state = torch.permute(expected_result[1], (1, 0, 2))
        result = scan(RNN, h[:, 0:1, :], x, dim=dim)
        self.assertEqual(result[0], expected_result_state)
        self.assertEqual(result[1], expected_result_out)

    @skipIfNoDynamoSupport
    def test_scan_simple_graph_no_carry(self):
        x = torch.randn(3, 10, 2, device=torch.device("cpu"))
        init = torch.randn(1, 10, 2, device=torch.device("cpu"))

        def f(fct, init, xs):
            return scan(fct, init, xs, dim=0, reverse=True)

        # Wrong number of returns from function
        with self.assertRaisesRegex(
            # Should be: RuntimeError: The pytree of the new carry produced
            # by the operator needs to match the pytree of the init
            torch._dynamo.exc.Unsupported,
            "Observed exception.*",
        ):
            gm = make_fx(f, tracing_mode="symbolic")(
                get_scan_combine_fn("add", True), init, x
            )

    @skipIfNoDynamoSupport
    def test_scan_simple_graph_wrong_carry(self):
        def add_wrong_carry(x: torch.Tensor, y: torch.Tensor):
            return (x + y)[0, :], x + y

        x = torch.randn(3, 10, 2, device=torch.device("cpu"))
        init = torch.randn(1, 10, 2, device=torch.device("cpu"))

        def f(fct, init, xs):
            return scan(fct, init, xs, dim=0, reverse=True)

        # Wrong carry shape
        with self.assertRaisesRegex(
            # Should be: RuntimeError: The pytree of the new carry produced by
            # the operator needs to match the pytree of the init
            torch._dynamo.exc.Unsupported,
            "Observed exception.*",
        ):
            gm = make_fx(f, tracing_mode="symbolic")(add_wrong_carry, init, x)

    @skipIfNoDynamoSupport
    def test_scan_simple_graph_wrong_dtype(self):
        def add_wrong_dtype(x: torch.Tensor, y: torch.Tensor):
            return torch.ones_like(x + y, dtype=torch.int64), x + y

        x = torch.randn(3, 10, 2, device=torch.device("cpu"))
        init = torch.randn(1, 10, 2, device=torch.device("cpu"))

        def f(fct, init, xs):
            return scan(fct, init, xs, dim=0, reverse=True)

        # Wrong dtype
        with self.assertRaisesRegex(
            # Should be: RuntimeError: Expected the init and
            # the new carry produced by the operator to be a tensor of
            # torch.int64 but got torch.float32 and torch.int64
            torch._dynamo.exc.UncapturedHigherOrderOpError,
            ".*",
        ):
            gm = make_fx(f, tracing_mode="symbolic")(add_wrong_dtype, init, x)

    @skipIfNoDynamoSupport
    @skipIfCrossRef  # Arg order changes with crossref
    def test_scan_simple_graph(self):
        from torch._dynamo.testing import EagerAndRecordGraphs

        x = torch.randn(3, 10, 2, device=torch.device("cpu"))
        init = torch.randn(1, 10, 2, device=torch.device("cpu"))

        def f(fct, init, xs):
            return scan(fct, init, xs, dim=0, reverse=True)

        # Correct case
        gm = make_fx(f, tracing_mode="symbolic")(
            get_scan_combine_fn("add", False), init, x
        )
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, fct_1, init_1, xs_1):
    slice_1 = torch.ops.aten.slice.Tensor(xs_1, 0, 0, 1)
    add = torch.ops.aten.add.Tensor(init_1, slice_1);  add = None
    add_1 = torch.ops.aten.add.Tensor(init_1, slice_1);  slice_1 = add_1 = None
    sym_size_int = torch.ops.aten.sym_size.int(init_1, 1)
    sym_size_int_1 = torch.ops.aten.sym_size.int(init_1, 2)
    new_empty = torch.ops.aten.new_empty.default(init_1, [1, sym_size_int, sym_size_int_1], dtype = torch.float32, device = device(type='cpu'), pin_memory = False);  new_empty = None
    new_empty_1 = torch.ops.aten.new_empty.default(xs_1, [1, sym_size_int, sym_size_int_1], dtype = torch.float32, device = device(type='cpu'), pin_memory = False);  sym_size_int = sym_size_int_1 = new_empty_1 = None
    scan_combine_graph_0 = self.scan_combine_graph_0
    scan = torch.ops.higher_order.scan(scan_combine_graph_0, [init_1], [xs_1], 0, True);  scan_combine_graph_0 = init_1 = xs_1 = None
    getitem = scan[0]
    getitem_1 = getitem[0];  getitem = None
    getitem_2 = scan[1];  scan = None
    getitem_3 = getitem_2[0];  getitem_2 = None
    return (getitem_1, getitem_3)""",  # noqa: B950
        )

        # Check graph
        backend = EagerAndRecordGraphs()
        torch.compile(f, backend=backend)(get_scan_combine_fn("add", False), init, x)
        gm = backend.graphs[0]

        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, L_init_ : torch.Tensor, L_xs_ : torch.Tensor):
    l_init_ = L_init_
    l_xs_ = L_xs_
    slice_1 = torch.ops.aten.slice(l_xs_, 0, 0, 1, 1)
    out_l = l_init_ + slice_1;  out_l = None
    add_1 = l_init_ + slice_1;  slice_1 = add_1 = None
    child = l_init_.new_empty((1, 10, 2), dtype = torch.float32, device = device(type='cpu'), requires_grad = False);  child = None
    child_1 = l_xs_.new_empty((1, 10, 2), dtype = torch.float32, device = device(type='cpu'), requires_grad = False);  child_1 = None
    scan_combine_fn_0 = self.scan_combine_fn_0
    scan = torch.ops.higher_order.scan(scan_combine_fn_0, [l_init_], [l_xs_], 0, True);  scan_combine_fn_0 = l_init_ = l_xs_ = None
    getitem = scan[0]
    getitem_1 = getitem[0];  getitem = None
    getitem_2 = scan[1];  scan = None
    getitem_3 = getitem_2[0];  getitem_2 = None
    return (getitem_1, getitem_3)""",  # noqa: B950
        )


@unittest.skipIf(IS_WINDOWS, "Windows not supported for this test")
@skipIfNoDynamoSupport
class TestControlFlowTraced(TestCase):
    def setUp(self):
        torch._dynamo.reset()
        super().setUp()

    def _check_tracing(self, fn, args, allow_non_fake_inputs=False):
        graphs = {}
        eager_res = fn(*args)
        for tracing_mode in ["symbolic", "real", "fake"]:
            graph = make_fx(
                fn,
                tracing_mode=tracing_mode,
                _allow_non_fake_inputs=allow_non_fake_inputs,
            )(*args)
            graphs[tracing_mode] = graph
            self.assertEqual(graph(*args), eager_res)
        return graphs

    def _check_compile(self, fn, args, *, backend="eager"):
        eager_res = fn(*args)
        compiled_fn = torch.compile(fn, backend=backend)
        self.assertEqual(compiled_fn(*args), eager_res)

    def test_cond_traced_not_nested(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return x.cos()

        def f(x, y):
            return cond(y, true_fn, false_fn, [x])

        x = torch.randn(4)
        graph = make_fx(f)(x, torch.tensor(False))
        result_true = graph.forward(x, torch.tensor(True))
        result_false = graph.forward(x, torch.tensor(False))
        self.assertFalse(torch.allclose(result_true, result_false))
        self.assertEqual(result_true, torch.sin(x))
        self.assertEqual(result_false, torch.cos(x))

        graph = make_fx(f, tracing_mode="symbolic")(x, torch.tensor(False))
        self.assertEqual(graph(x, torch.tensor(True)), f(x, torch.tensor(True)))

    @skipIfTorchDynamo("Graph is not captured by backend if test with dynamo")
    @skipIfCrossRef  # Arg order changes with crossref
    def test_cond_simple_with_linear_compile_check_graph(self):
        from torch._dynamo.testing import EagerAndRecordGraphs

        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return x.cos()

        x = torch.randn(4, requires_grad=True)

        def f(pred, x):
            result = cond(pred, true_fn, false_fn, (x,))
            grad_out = torch.ones_like(result)
            return torch.autograd.grad(result, (x,), grad_out)

        backend = EagerAndRecordGraphs()
        torch.compile(f, backend=backend)(torch.tensor(False), x)
        self.assertEqual(len(backend.graphs), 2)
        gm = backend.graphs[0]

        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, L_pred_ : torch.Tensor, L_x_ : torch.Tensor):
    l_pred_ = L_pred_
    l_x_ = L_x_
    cond_true_0 = self.cond_true_0
    cond_false_0 = self.cond_false_0
    cond = torch.ops.higher_order.cond(l_pred_, cond_true_0, cond_false_0, [l_x_]);  l_pred_ = cond_true_0 = cond_false_0 = l_x_ = None
    result = cond[0];  cond = None
    grad_out = torch.ones_like(result)
    return (result, grad_out)""",  # noqa: B950
        )

        self.assertExpectedInline(
            gm.cond_true_0.code.strip(),
            """\
def forward(self, l_x_):
    l_x__1 = l_x_
    sin = l_x__1.sin();  l_x__1 = None
    return (sin,)""",  # noqa: B950
        )
        self.assertExpectedInline(
            gm.cond_false_0.code.strip(),
            """\
def forward(self, l_x_):
    l_x__1 = l_x_
    cos = l_x__1.cos();  l_x__1 = None
    return (cos,)""",  # noqa: B950
        )

        backward_gm = backend.graphs[1]
        self.assertExpectedInline(
            backward_gm.code.strip(),
            """\
def forward(self, L_ctx_saved_tensors_0_ : torch.Tensor, L_ctx_pred : torch.Tensor, L_flat_grads_0_ : torch.Tensor):
    l_ctx_saved_tensors_0_ = L_ctx_saved_tensors_0_
    l_ctx_pred = L_ctx_pred
    l_flat_grads_0_ = L_flat_grads_0_
    cond_true_0 = self.cond_true_0
    cond_false_0 = self.cond_false_0
    cond = torch.ops.higher_order.cond(l_ctx_pred, cond_true_0, cond_false_0, [l_ctx_saved_tensors_0_, l_flat_grads_0_]);  l_ctx_pred = cond_true_0 = cond_false_0 = l_ctx_saved_tensors_0_ = l_flat_grads_0_ = None
    getitem = cond[0];  cond = None
    return (getitem,)""",  # noqa: B950
        )

    def test_while_loop_nested_traced(self):
        fn, inp = WHILE_LOOP_TESTS["nested"]
        graphs = self._check_tracing(fn, inp)
        self.assertExpectedInline(
            graphs["symbolic"].code.strip("\n"),
            """\
def forward(self, out_iter_1, it_1, y_1):
    while_loop_cond_graph_0 = self.while_loop_cond_graph_0
    while_loop_body_graph_0 = self.while_loop_body_graph_0
    while_loop = torch.ops.higher_order.while_loop(while_loop_cond_graph_0, while_loop_body_graph_0, (out_iter_1, it_1, y_1), ());  while_loop_cond_graph_0 = while_loop_body_graph_0 = out_iter_1 = it_1 = y_1 = None
    getitem = while_loop[0]
    getitem_1 = while_loop[1]
    getitem_2 = while_loop[2];  while_loop = None
    return (getitem, getitem_1, getitem_2)
    """,  # noqa: B950
        )
        self.assertExpectedInline(
            graphs["symbolic"].while_loop_cond_graph_0.code.strip("\n"),
            """\
def forward(self, arg0_1, arg1_1, arg2_1):
    sum_1 = torch.ops.aten.sum.default(arg0_1);  arg0_1 = None
    lt = torch.ops.aten.lt.Scalar(sum_1, 2);  sum_1 = None
    return lt
    """,
        )
        self.assertExpectedInline(
            graphs["symbolic"].while_loop_body_graph_0.code.strip("\n"),
            """\
def forward(self, arg0_1, arg1_1, arg2_1):
    while_loop_cond_graph_0 = self.while_loop_cond_graph_0
    while_loop_body_graph_0 = self.while_loop_body_graph_0
    while_loop = torch.ops.higher_order.while_loop(while_loop_cond_graph_0, while_loop_body_graph_0, (arg0_1, arg1_1, arg2_1), ());  while_loop_cond_graph_0 = while_loop_body_graph_0 = arg0_1 = arg1_1 = arg2_1 = None
    getitem = while_loop[0]
    getitem_1 = while_loop[1]
    getitem_2 = while_loop[2];  while_loop = None
    add = torch.ops.aten.add.Tensor(getitem, 1);  getitem = None
    return (add, getitem_1, getitem_2)
    """,  # noqa: B950
        )

    def _wrap_with_functionalize(self, fn, func_type):
        mode = None
        if func_type == "cpp":
            fn = CppFunctionalizeAPI().functionalize(fn)
        elif func_type == "python":
            fn = PythonFunctionalizeAPI().functionalize(fn)
            mode = FunctionalTensorMode()
        elif func_type == "functorch":
            fn = torch.func.functionalize(fn)
        else:
            assert func_type == "no"
        return fn, mode

    @parametrize("func_type", ["no", "cpp", "python", "functorch"])
    def test_while_loop_simple_functionalize_check_graph(self, func_type):
        fn, inp = WHILE_LOOP_TESTS["simple_with_mutation"]
        fn, mode = self._wrap_with_functionalize(fn, func_type)
        mode = mode if mode is not None else contextlib.nullcontext()
        with mode:
            graphs = self._check_tracing(fn, inp)
        if func_type == "no":
            self.assertExpectedInline(
                graphs["symbolic"].code.strip("\n"),
                """\
def forward(self, x_1):
    while_loop_cond_graph_0 = self.while_loop_cond_graph_0
    while_loop_body_graph_0 = self.while_loop_body_graph_0
    while_loop = torch.ops.higher_order.while_loop(while_loop_cond_graph_0, while_loop_body_graph_0, (x_1,), ());  while_loop_cond_graph_0 = while_loop_body_graph_0 = x_1 = None
    getitem = while_loop[0];  while_loop = None
    return (getitem,)
    """,  # noqa: B950
            )
            self.assertExpectedInline(
                graphs["symbolic"].while_loop_cond_graph_0.code.strip("\n"),
                """\
def forward(self, arg0_1):
    clone = torch.ops.aten.clone.default(arg0_1);  arg0_1 = None
    add_ = torch.ops.aten.add_.Tensor(clone, 1);  clone = None
    add__1 = torch.ops.aten.add_.Tensor(add_, -1);  add_ = None
    sum_1 = torch.ops.aten.sum.default(add__1);  add__1 = None
    lt = torch.ops.aten.lt.Scalar(sum_1, 10);  sum_1 = None
    return lt
    """,
            )
            self.assertExpectedInline(
                graphs["symbolic"].while_loop_body_graph_0.code.strip("\n"),
                """\
def forward(self, arg0_1):
    clone = torch.ops.aten.clone.default(arg0_1);  arg0_1 = None
    add_ = torch.ops.aten.add_.Tensor(clone, 1);  clone = None
    add__1 = torch.ops.aten.add_.Tensor(add_, -1);  add_ = None
    add = torch.ops.aten.add.Tensor(add__1, 1);  add__1 = None
    return (add,)
    """,
            )
        elif func_type == "python":
            self.assertExpectedInline(
                graphs["symbolic"].code.strip("\n"),
                """\
def forward(self, arg0_1):
    while_loop_cond_graph_0 = self.while_loop_cond_graph_0
    while_loop_body_graph_0 = self.while_loop_body_graph_0
    while_loop = torch.ops.higher_order.while_loop(while_loop_cond_graph_0, while_loop_body_graph_0, (arg0_1,), ());  while_loop_cond_graph_0 = while_loop_body_graph_0 = arg0_1 = None
    getitem = while_loop[0];  while_loop = None
    return (getitem,)
    """,  # noqa: B950
            )
            self.assertExpectedInline(
                graphs["symbolic"].while_loop_cond_graph_0.code.strip("\n"),
                """\
def forward(self, arg0_1):
    clone = torch.ops.aten.clone.default(arg0_1);  arg0_1 = None
    add = torch.ops.aten.add.Tensor(clone, 1);  clone = None
    add_1 = torch.ops.aten.add.Tensor(add, -1);  add = None
    sum_1 = torch.ops.aten.sum.default(add_1);  add_1 = None
    lt = torch.ops.aten.lt.Scalar(sum_1, 10);  sum_1 = None
    return lt
    """,
            )
            self.assertExpectedInline(
                graphs["symbolic"].while_loop_body_graph_0.code.strip("\n"),
                """\
def forward(self, arg0_1):
    clone = torch.ops.aten.clone.default(arg0_1);  arg0_1 = None
    add = torch.ops.aten.add.Tensor(clone, 1);  clone = None
    add_1 = torch.ops.aten.add.Tensor(add, -1);  add = None
    add_2 = torch.ops.aten.add.Tensor(add_1, 1);  add_1 = None
    return (add_2,)
    """,
            )
        else:
            self.assertExpectedInline(
                graphs["symbolic"].code.strip("\n"),
                """\
def forward(self, x_1):
    while_loop_cond_graph_0 = self.while_loop_cond_graph_0
    while_loop_body_graph_0 = self.while_loop_body_graph_0
    while_loop = torch.ops.higher_order.while_loop(while_loop_cond_graph_0, while_loop_body_graph_0, (x_1,), ());  while_loop_cond_graph_0 = while_loop_body_graph_0 = x_1 = None
    getitem = while_loop[0];  while_loop = None
    return (getitem,)
    """,  # noqa: B950
            )
            self.assertExpectedInline(
                graphs["symbolic"].while_loop_cond_graph_0.code.strip("\n"),
                """\
def forward(self, arg0_1):
    clone = torch.ops.aten.clone.default(arg0_1);  arg0_1 = None
    add = torch.ops.aten.add.Tensor(clone, 1);  clone = None
    add_1 = torch.ops.aten.add.Tensor(add, -1);  add = None
    sum_1 = torch.ops.aten.sum.default(add_1);  add_1 = None
    lt = torch.ops.aten.lt.Scalar(sum_1, 10);  sum_1 = None
    return lt
    """,
            )
            self.assertExpectedInline(
                graphs["symbolic"].while_loop_body_graph_0.code.strip("\n"),
                """\
def forward(self, arg0_1):
    clone = torch.ops.aten.clone.default(arg0_1);  arg0_1 = None
    add = torch.ops.aten.add.Tensor(clone, 1);  clone = None
    add_1 = torch.ops.aten.add.Tensor(add, -1);  add = None
    add_2 = torch.ops.aten.add.Tensor(add_1, 1);  add_1 = None
    return (add_2,)
    """,
            )

    @parametrize("func_type", ["no", "cpp", "python", "functorch"])
    @parametrize("while_loop_test", list(WHILE_LOOP_TESTS.keys()))
    def test_while_loop_functionalize(self, func_type, while_loop_test):
        # simple_with_linear doesn't work becaue parameters and buffers
        # are not inputs so they're not wrapped by functionalization and tracing.
        if while_loop_test not in ("simple_with_linear", "nested_with_linear"):
            fn, inp = WHILE_LOOP_TESTS[while_loop_test]
            fn, mode = self._wrap_with_functionalize(fn, func_type)
            mode = mode if mode is not None else contextlib.nullcontext()
            with mode:
                self._check_tracing(fn, inp)

    @parametrize("while_loop_test", list(WHILE_LOOP_TESTS.keys()))
    def test_while_loop_tracing(self, while_loop_test):
        fn, inp = WHILE_LOOP_TESTS[while_loop_test]
        allow_non_fake_inputs = (
            False
            if while_loop_test not in ("simple_with_linear", "nested_with_linear")
            else True
        )
        self._check_tracing(fn, inp, allow_non_fake_inputs)

    @parametrize("backend", ["eager", "aot_eager"])
    @parametrize("while_loop_test", list(WHILE_LOOP_TESTS.keys()))
    def test_while_loop_compile(self, backend, while_loop_test):
        fn, inp = WHILE_LOOP_TESTS[while_loop_test]
        self._check_compile(fn, inp, backend=backend)

    @skipIfTorchDynamo("Graph is not captured by backend if test with dynamo")
    @skipIfCrossRef  # Arg order changes with cross ref
    def test_while_loop_simple_with_linear_compile_check_graph(self):
        fn, inp = WHILE_LOOP_TESTS["simple_with_linear"]
        from torch._dynamo.testing import EagerAndRecordGraphs

        backend = EagerAndRecordGraphs()
        torch.compile(fn, backend=backend)(*inp)
        self.assertEqual(len(backend.graphs), 1)
        gm = backend.graphs[0]
        if torch._dynamo.config.inline_inbuilt_nn_modules:
            self.assertExpectedInline(
                gm.code.strip(),
                """\
def forward(self, L_iter_ : torch.Tensor, L_x_ : torch.Tensor, L_self_buffers_dec_ : torch.Tensor, L_self_modules_linear_parameters_weight_ : torch.nn.parameter.Parameter, L_self_modules_linear_parameters_bias_ : torch.nn.parameter.Parameter):
    l_iter_ = L_iter_
    l_x_ = L_x_
    l_self_buffers_dec_ = L_self_buffers_dec_
    l_self_modules_linear_parameters_weight_ = L_self_modules_linear_parameters_weight_
    l_self_modules_linear_parameters_bias_ = L_self_modules_linear_parameters_bias_
    cond_fn_0 = self.cond_fn_0
    body_fn_0 = self.body_fn_0
    while_loop = torch.ops.higher_order.while_loop(cond_fn_0, body_fn_0, (l_iter_, l_x_), (l_self_buffers_dec_, l_self_modules_linear_parameters_bias_, l_self_modules_linear_parameters_weight_));  cond_fn_0 = body_fn_0 = l_iter_ = l_x_ = l_self_buffers_dec_ = l_self_modules_linear_parameters_bias_ = l_self_modules_linear_parameters_weight_ = None
    getitem = while_loop[0]
    getitem_1 = while_loop[1];  while_loop = None
    return (getitem, getitem_1)""",  # noqa: B950
            )
            self.assertExpectedInline(
                gm.cond_fn_0.code.strip(),
                """\
def forward(self, l_iter_, l_x_, l_self_buffers_dec__cond_fn, l_self_modules_linear_parameters_bias__body_fn, l_self_modules_linear_parameters_weight__body_fn):
    sub = l_iter_ - l_self_buffers_dec__cond_fn;  l_iter_ = l_self_buffers_dec__cond_fn = None
    gt = sub > 0;  sub = None
    return gt""",  # noqa: B950
            )
            self.assertExpectedInline(
                gm.body_fn_0.code.strip(),
                """\
def forward(self, l_iter_, l_x_, l_self_buffers_dec__cond_fn, l_self_modules_linear_parameters_bias__body_fn, l_self_modules_linear_parameters_weight__body_fn):
    child = l_iter_ - 1;  l_iter_ = None
    child_1 = torch._C._nn.linear(l_x_, l_self_modules_linear_parameters_weight__body_fn, l_self_modules_linear_parameters_bias__body_fn);  l_x_ = l_self_modules_linear_parameters_weight__body_fn = l_self_modules_linear_parameters_bias__body_fn = None
    return (child, child_1)""",  # noqa: B950
            )
        else:
            self.assertExpectedInline(
                gm.code.strip(),
                """\
def forward(self, L_iter_ : torch.Tensor, L_x_ : torch.Tensor):
    l_iter_ = L_iter_
    l_x_ = L_x_
    l__self___dec = self.L__self___dec
    l__self___linear_weight = self.L__self___linear_weight
    l__self___linear_bias = self.L__self___linear_bias
    cond_fn_0 = self.cond_fn_0
    body_fn_0 = self.body_fn_0
    while_loop = torch.ops.higher_order.while_loop(cond_fn_0, body_fn_0, (l_iter_, l_x_), (l__self___dec, l__self___linear_bias, l__self___linear_weight));  cond_fn_0 = body_fn_0 = l_iter_ = l_x_ = l__self___dec = l__self___linear_bias = l__self___linear_weight = None
    getitem = while_loop[0]
    getitem_1 = while_loop[1];  while_loop = None
    return (getitem, getitem_1)""",  # noqa: B950
            )
            self.assertExpectedInline(
                gm.cond_fn_0.code.strip(),
                """\
def forward(self, l_iter_, l_x_, l__self___dec_cond_fn, l__self___linear_bias_body_fn, l__self___linear_weight_body_fn):
    sub = l_iter_ - l__self___dec_cond_fn;  l_iter_ = l__self___dec_cond_fn = None
    gt = sub > 0;  sub = None
    return gt""",  # noqa: B950
            )
            self.assertExpectedInline(
                gm.body_fn_0.code.strip(),
                """\
def forward(self, l_iter_, l_x_, l__self___dec_cond_fn, l__self___linear_bias_body_fn, l__self___linear_weight_body_fn):
    child = l_iter_ - 1;  l_iter_ = None
    child_1 = torch._C._nn.linear(l_x_, l__self___linear_weight_body_fn, l__self___linear_bias_body_fn);  l_x_ = l__self___linear_weight_body_fn = l__self___linear_bias_body_fn = None
    return (child, child_1)""",  # noqa: B950
            )

    def test_while_loop_nested2_traced(self):
        fn, inp = WHILE_LOOP_TESTS["nested2"]
        graphs = self._check_tracing(fn, inp)
        gm = graphs["symbolic"]
        outer_body = gm.while_loop_body_graph_0
        outer_cond = gm.while_loop_cond_graph_0
        inner_body = outer_body.while_loop_body_graph_0
        inner_cond = outer_body.while_loop_cond_graph_0
        self.assertExpectedInline(
            gm.code.strip("\n"),
            """\
def forward(self, arg0_1, arg1_1, arg2_1, arg3_1):
    while_loop_cond_graph_0 = self.while_loop_cond_graph_0
    while_loop_body_graph_0 = self.while_loop_body_graph_0
    while_loop = torch.ops.higher_order.while_loop(while_loop_cond_graph_0, while_loop_body_graph_0, (arg0_1, arg1_1, arg2_1, arg3_1), ());  while_loop_cond_graph_0 = while_loop_body_graph_0 = arg0_1 = arg1_1 = arg2_1 = arg3_1 = None
    getitem = while_loop[0]
    getitem_1 = while_loop[1]
    getitem_2 = while_loop[2]
    getitem_3 = while_loop[3];  while_loop = None
    return (getitem, getitem_1, getitem_2, getitem_3)
    """,  # noqa: B950
        )
        self.assertExpectedInline(
            outer_body.code.strip("\n"),
            """\
def forward(self, arg0_1, arg1_1, arg2_1, arg3_1):
    while_loop_cond_graph_0 = self.while_loop_cond_graph_0
    while_loop_body_graph_0 = self.while_loop_body_graph_0
    while_loop = torch.ops.higher_order.while_loop(while_loop_cond_graph_0, while_loop_body_graph_0, (arg0_1, arg1_1, arg2_1, arg3_1), ());  while_loop_cond_graph_0 = while_loop_body_graph_0 = arg0_1 = arg1_1 = arg2_1 = arg3_1 = None
    getitem = while_loop[0]
    getitem_1 = while_loop[1]
    getitem_2 = while_loop[2]
    getitem_3 = while_loop[3];  while_loop = None
    sub = torch.ops.aten.sub.Tensor(getitem, 1);  getitem = None
    clone = torch.ops.aten.clone.default(getitem_1);  getitem_1 = None
    mul = torch.ops.aten.mul.Tensor(getitem_2, 2);  getitem_2 = None
    div = torch.ops.aten.div.Tensor(getitem_3, 2);  getitem_3 = None
    return (sub, clone, mul, div)
    """,  # noqa: B950
        )
        self.assertExpectedInline(
            outer_body.code.strip("\n"),
            """\
def forward(self, arg0_1, arg1_1, arg2_1, arg3_1):
    while_loop_cond_graph_0 = self.while_loop_cond_graph_0
    while_loop_body_graph_0 = self.while_loop_body_graph_0
    while_loop = torch.ops.higher_order.while_loop(while_loop_cond_graph_0, while_loop_body_graph_0, (arg0_1, arg1_1, arg2_1, arg3_1), ());  while_loop_cond_graph_0 = while_loop_body_graph_0 = arg0_1 = arg1_1 = arg2_1 = arg3_1 = None
    getitem = while_loop[0]
    getitem_1 = while_loop[1]
    getitem_2 = while_loop[2]
    getitem_3 = while_loop[3];  while_loop = None
    sub = torch.ops.aten.sub.Tensor(getitem, 1);  getitem = None
    clone = torch.ops.aten.clone.default(getitem_1);  getitem_1 = None
    mul = torch.ops.aten.mul.Tensor(getitem_2, 2);  getitem_2 = None
    div = torch.ops.aten.div.Tensor(getitem_3, 2);  getitem_3 = None
    return (sub, clone, mul, div)
    """,  # noqa: B950
        )
        self.assertExpectedInline(
            inner_body.code.strip("\n"),
            """\
def forward(self, arg0_1, arg1_1, arg2_1, arg3_1):
    clone = torch.ops.aten.clone.default(arg0_1);  arg0_1 = None
    sub = torch.ops.aten.sub.Tensor(arg1_1, 1);  arg1_1 = None
    add = torch.ops.aten.add.Tensor(arg2_1, 3.14);  arg2_1 = None
    sub_1 = torch.ops.aten.sub.Tensor(arg3_1, 2.71);  arg3_1 = None
    return (clone, sub, add, sub_1)
    """,
        )
        self.assertExpectedInline(
            inner_cond.code.strip("\n"),
            """\
def forward(self, arg0_1, arg1_1, arg2_1, arg3_1):
    gt = torch.ops.aten.gt.Scalar(arg1_1, 0);  arg1_1 = None
    return gt
    """,
        )

    def test_cond_nested_traced(self):
        def true_nested(y):
            return y * y

        def false_nested(y):
            return y + y

        def true_fn(x, pred2):
            z = cond(pred2, true_nested, false_nested, [x])
            return x + z

        def false_fn(x, _):
            return x.cos()

        def f(x, pred, pred2):
            return cond(pred, true_fn, false_fn, [x, pred2])

        x = torch.randn(4)
        graph = make_fx(f)(x, torch.tensor(False), torch.tensor(False))

        result_true_true = graph.forward(
            x, torch.tensor(True), torch.tensor(True)
        )  # True + True -> x * x
        result_true_false = graph.forward(
            x, torch.tensor(True), torch.tensor(False)
        )  # True + True -> x + x
        result_false_true = graph.forward(
            x, torch.tensor(False), torch.tensor(True)
        )  # False + either -> cos
        result_false_false = graph.forward(
            x, torch.tensor(False), torch.tensor(False)
        )  # False + either -> cos

        self.assertNotEqual(result_true_true, result_true_false)
        self.assertFalse(torch.allclose(result_false_true, result_true_true))

        self.assertEqual(result_false_true, result_false_false)

        self.assertEqual(result_true_true, (x * x) + x)
        self.assertEqual(result_true_false, x + x + x)

        self.assertEqual(result_false_true, torch.cos(x))

        graph = make_fx(f, tracing_mode="symbolic")(
            x, torch.tensor(False), torch.tensor(False)
        )
        self.assertEqual(
            graph(x, torch.tensor(True), torch.tensor(True)),
            f(x, torch.tensor(True), torch.tensor(True)),
        )

    def test_cond_functionalized(self):
        def true_fn(x):
            y = x.sin()
            y.add_(4)
            return x.sin().max() + y.sum()

        def false_fn(x):
            return x.cos().min()

        def f(x):
            pred = x.shape[0] == 1
            return cond(pred, true_fn, false_fn, [x])

        example_inputs = (torch.ones(4, 5),)
        functional_f = torch.func.functionalize(f)
        self.assertEqual(functional_f(*example_inputs), f(*example_inputs))

        graph_module = make_fx(torch.func.functionalize(f), tracing_mode="symbolic")(
            *example_inputs
        )
        self.assertEqual(graph_module(*example_inputs), f(*example_inputs))

        all_ops_in_true_branch = []
        for node in graph_module.true_graph_0.graph.nodes:
            if node.op == "call_function":
                all_ops_in_true_branch.append(node.target)

        self.assertFalse(any(op._schema.is_mutable for op in all_ops_in_true_branch))

        self.assertEqual(graph_module(*example_inputs), f(*example_inputs))

    def test_cond_accepts_torch_function_as_inputs(self):
        a = torch.randn(3, 4)
        b = torch.randn(3, 4)

        def f(a, b):
            return cond(a.sum() > 0, torch.add, torch.mul, (a, b))

        gm = self._check_tracing(f, (a, b))["symbolic"]
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, a_1, b_1):
    sum_1 = torch.ops.aten.sum.default(a_1)
    gt = torch.ops.aten.gt.Scalar(sum_1, 0);  sum_1 = None
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    cond = torch.ops.higher_order.cond(gt, true_graph_0, false_graph_0, [a_1, b_1]);  gt = true_graph_0 = false_graph_0 = a_1 = b_1 = None
    getitem = cond[0];  cond = None
    return getitem""",  # noqa: B950
        )
        self.assertExpectedInline(
            gm.true_graph_0.code.strip(),
            """\
def forward(self, arg0_1, arg1_1):
    add = torch.ops.aten.add.Tensor(arg0_1, arg1_1);  arg0_1 = arg1_1 = None
    return (add,)""",
        )
        self.assertExpectedInline(
            gm.false_graph_0.code.strip(),
            """\
def forward(self, arg0_1, arg1_1):
    mul = torch.ops.aten.mul.Tensor(arg0_1, arg1_1);  arg0_1 = arg1_1 = None
    return (mul,)""",
        )

    def test_cond_retrace_functionalized(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return x.cos()

        def f(x):
            return cond(x.all(), true_fn, false_fn, (x,))

        inp = torch.ones(1, 2)
        gm_non_functional = make_fx(f, tracing_mode="real")(inp)
        gm_functional = make_fx(
            torch.func.functionalize(gm_non_functional), tracing_mode="real"
        )(inp)
        self.assertEqual(gm_functional(torch.zeros(1, 2)), f(torch.zeros(1, 2)))

    def test_cond_subgraph_same_shape_env_as_parent(self):
        def true_fn(x):
            return x.sin() + 10

        def false_fn(x):
            return x.cos() - 20

        def f(x, pred):
            y = cond(pred, true_fn, false_fn, [x])
            z = torch.add(y, y)
            return z

        symbolic_traced_graph = self._check_tracing(
            f, (torch.ones(4), torch.Tensor([True]))
        )["symbolic"]
        graph_shape_env = symbolic_traced_graph.shape_env

        def _node_shape_env_iter(gm):
            for node in symbolic_traced_graph.graph.nodes:
                if node.op == "call_function":
                    val = node.meta.get("val")
                    if isinstance(val, tuple):
                        for v in val:
                            yield v.fake_mode.shape_env
                    else:
                        yield val.fake_mode.shape_env

        for shape_env in _node_shape_env_iter(symbolic_traced_graph):
            self.assertTrue(shape_env is graph_shape_env)

        for shape_env in _node_shape_env_iter(symbolic_traced_graph.true_graph_0):
            self.assertTrue(shape_env is graph_shape_env)

        for shape_env in _node_shape_env_iter(symbolic_traced_graph.false_graph_0):
            self.assertTrue(shape_env is graph_shape_env)

    def test_cond_functionalized_nested(self):
        def true_true_fn(x):
            y = x.cos()
            y.add_(4)
            return x.sin().max() + y.sin().max()

        def true_false_fn(x):
            return x.cos().min()

        def true_fn(x):
            pred = x.shape[0] == 1
            return cond(pred, true_true_fn, true_false_fn, [x])

        def false_fn(x):
            return x.sum()

        def f(x):
            pred = x.shape[0] == 1
            return cond(pred, true_fn, false_fn, [x])

        example_inputs = (torch.ones(4, 5),)
        functional_f = torch.func.functionalize(f)
        self.assertEqual(functional_f(*example_inputs), f(*example_inputs))

        graph_module = make_fx(torch.func.functionalize(f), tracing_mode="symbolic")(
            *example_inputs
        )
        self.assertEqual(graph_module(*example_inputs), f(*example_inputs))

        gm_true_true_branch = graph_module.true_graph_0.true_graph_0

        self.assertEqual(graph_module(*example_inputs), f(*example_inputs))

        all_ops = []
        for node in gm_true_true_branch.graph.nodes:
            if node.op == "call_function":
                all_ops.append(node.target)

        self.assertFalse(any(op._schema.is_mutable for op in all_ops))

    def test_cond_functionalized_data_dependent_pred(self):
        def true_fn(x):
            return x.sin().sum()

        def false_fn(x):
            return x.cos().sum()

        def f(x):
            pred = x.nonzero().shape[0] == 1
            return cond(pred, true_fn, false_fn, [x])

        example_inputs = (torch.ones(4, 5),)
        functional_f = torch.func.functionalize(f)
        self.assertEqual(functional_f(*example_inputs), f(*example_inputs))

        graph_module = make_fx(torch.func.functionalize(f))(*example_inputs)
        self.assertEqual(graph_module(*example_inputs), f(*example_inputs))

    # https://github.com/pytorch/pytorch/issues/126988
    def test_cond_functionalized_input_mutation_on_true_brancte(self):
        def true_fn(x):
            view_x = x.view(x.shape)
            view_x.add_(1)
            return view_x.sin().sum()

        def false_fn(x):
            return x.cos().sum()

        def f(x):
            pred = x.shape[0] == 4
            return cond(pred, true_fn, false_fn, [x])

        example_inputs = (torch.ones(4, 5),)
        # torch.cond inlines into one of the branches because the predicate
        # is a constant.
        gm = make_fx(torch.func.functionalize(f))(*example_inputs)
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, x_1):
    view = torch.ops.aten.view.default(x_1, [4, 5])
    add = torch.ops.aten.add.Tensor(view, 1);  view = None
    view_1 = torch.ops.aten.view.default(add, [4, 5]);  add = None
    view_2 = torch.ops.aten.view.default(view_1, [4, 5])
    sin = torch.ops.aten.sin.default(view_2);  view_2 = None
    sum_1 = torch.ops.aten.sum.default(sin);  sin = None
    copy_ = torch.ops.aten.copy_.default(x_1, view_1);  x_1 = view_1 = copy_ = None
    return sum_1""",
        )

        # torch.cond triggers the check of the branches because the predicate
        # is a SymBool.
        with self.assertRaisesRegex(
            UnsupportedAliasMutationException, "One of torch.cond branch"
        ):
            make_fx(torch.func.functionalize(f), tracing_mode="symbolic")(
                *example_inputs
            )

    # https://github.com/pytorch/pytorch/issues/126988
    def test_cond_functionalized_input_mutation_on_false_branch(self):
        def true_fn(x):
            return x.sin().sum()

        def false_fn(x):
            view_x = x.view(x.shape)
            view_x.add_(1)
            return view_x.cos().sum()

        def f(x):
            pred = x.shape[0] == 4
            return cond(pred, true_fn, false_fn, [x])

        example_inputs = (torch.ones(5, 5),)
        gm = make_fx(torch.func.functionalize(f))(*example_inputs)
        # torch.cond inlines into one of the branches because the predicate
        # is a constant.
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, x_1):
    view = torch.ops.aten.view.default(x_1, [5, 5])
    add = torch.ops.aten.add.Tensor(view, 1);  view = None
    view_1 = torch.ops.aten.view.default(add, [5, 5]);  add = None
    view_2 = torch.ops.aten.view.default(view_1, [5, 5])
    cos = torch.ops.aten.cos.default(view_2);  view_2 = None
    sum_1 = torch.ops.aten.sum.default(cos);  cos = None
    copy_ = torch.ops.aten.copy_.default(x_1, view_1);  x_1 = view_1 = copy_ = None
    return sum_1""",
        )

        # torch.cond triggers the check of the branches because the predicate
        # is a SymBool.
        with self.assertRaisesRegex(
            UnsupportedAliasMutationException, "One of torch.cond branch"
        ):
            make_fx(torch.func.functionalize(f), tracing_mode="symbolic")(
                *example_inputs
            )

    # https://github.com/pytorch/pytorch/issues/126988
    def test_cond_functionalized_output_alias_input(self):
        def true_fn(x):
            return x

        def false_fn(x):
            view_x = x.view(x.shape)
            return view_x

        def f(x):
            pred = x.shape[0] == 4
            return cond(pred, true_fn, false_fn, [x])

        example_inputs = (torch.ones(5, 5),)
        gm = make_fx(torch.func.functionalize(f))(*example_inputs)
        # torch.cond inlines into one of the branches because the predicate
        # is a constant.
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, x_1):
    view = torch.ops.aten.view.default(x_1, [5, 5]);  x_1 = None
    return view""",
        )

        # torch.cond triggers the check of the branches because the predicate
        # is a SymBool.
        with self.assertRaisesRegex(
            UnsupportedAliasMutationException, "One of torch.cond branch"
        ):
            make_fx(torch.func.functionalize(f), tracing_mode="symbolic")(
                *example_inputs
            )

    # https://github.com/pytorch/pytorch/issues/126988
    def test_cond_functionalized_nested_input_mutation(self):
        def true_true_fn(x):
            x.add_(4)
            return x.sin().max()

        def true_false_fn(x):
            return x.cos().min()

        def true_fn(x):
            pred = x.shape[0] == 1
            return cond(pred, true_true_fn, true_false_fn, [x])

        def false_fn(x):
            return x.sum()

        def f(x):
            pred = x.shape[0] == 1
            return cond(pred, true_fn, false_fn, [x])

        example_inputs = (torch.ones(4, 5),)
        with self.assertRaisesRegex(
            UnsupportedAliasMutationException, "One of torch.cond branch"
        ):
            make_fx(torch.func.functionalize(f), tracing_mode="symbolic")(
                *example_inputs
            )

    # https://github.com/pytorch/pytorch/issues/126988
    def test_cond_functionalized_nested_input_mutation_with_aot_func(self):
        def true_true_fn(x):
            x.add_(4)
            return x.sin().max()

        def true_false_fn(x):
            return x.cos().min()

        def true_fn(x):
            pred = x.shape[0] == 1
            return cond(pred, true_true_fn, true_false_fn, [x])

        def false_fn(x):
            return x.sum()

        def f(x):
            pred = x.shape[0] == 1
            return cond(pred, true_fn, false_fn, [x])

        example_input = torch.ones(4, 5)
        try:
            example_input_func = to_fun_old(example_input)
            torch._enable_functionalization(reapply_views=False)
            f(example_input_func)

            with self.assertRaisesRegex(
                UnsupportedAliasMutationException, "One of torch.cond branch"
            ):
                make_fx(f, tracing_mode="symbolic")(example_input_func)
        finally:
            torch._disable_functionalization()

        def f_wrapper(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                torch._enable_functionalization(reapply_views=False)
                try:
                    return func(*args, **kwargs)
                finally:
                    torch._disable_functionalization()

            return wrapper

        with self.assertRaisesRegex(
            UnsupportedAliasMutationException, "One of torch.cond branch"
        ):
            make_fx(f_wrapper(f), tracing_mode="symbolic")(example_input_func)

    # https://github.com/pytorch/pytorch/issues/126988
    @xfailIfTorchDynamo
    def test_cond_functionalized_input_aliasing_with_aot_func(self):
        def true_fn(x):
            return x

        def false_fn(x):
            view_x = x.view(x.shape)
            return view_x

        def f(x):
            pred = x.sum() > 0
            return cond(pred, true_fn, false_fn, [x])

        example_input = torch.ones(5, 5)
        try:
            example_input_func = to_fun_old(example_input)
            torch._enable_functionalization(reapply_views=False)
            with self.assertRaisesRegex(
                UnsupportedAliasMutationException,
                "One of torch.cond branch might be aliasing",
            ):
                f(example_input_func)
        finally:
            torch._disable_functionalization()

        def f_wrapper(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                torch._enable_functionalization(reapply_views=False)
                try:
                    func_args = pytree.tree_map(
                        lambda x: torch._to_functional_tensor(x)
                        if isinstance(x, torch.Tensor)
                        else x,
                        args,
                    )
                    func_kwargs = pytree.tree_map(
                        lambda x: torch._to_functional_tensor(x)
                        if isinstance(x, torch.Tensor)
                        else x,
                        kwargs,
                    )
                    return func(*func_args, **func_kwargs)
                finally:
                    torch._disable_functionalization()

            return wrapper

        with self.assertRaisesRegex(
            UnsupportedAliasMutationException,
            "One of torch.cond branch might be aliasing",
        ):
            make_fx(f_wrapper(f), tracing_mode="symbolic")(example_input)

    def test_cond_functionalized_aot_func_check_functional(self):
        def true_fn(x):
            return x.cos()

        def false_fn(x):
            y = x.sin()
            y.add_(5)
            return y

        def f(x):
            pred = x.shape[0] == 4
            return cond(pred, true_fn, false_fn, [x])

        example_input = torch.ones(5, 5)

        def f_wrapper(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                torch._enable_functionalization(reapply_views=False)
                try:
                    func_args = pytree.tree_map(
                        lambda x: to_fun_old(x) if isinstance(x, torch.Tensor) else x,
                        args,
                    )
                    func_kwargs = pytree.tree_map(
                        lambda x: to_fun_old(x) if isinstance(x, torch.Tensor) else x,
                        kwargs,
                    )
                    return pytree.tree_map(
                        from_fun_old, func(*func_args, **func_kwargs)
                    )
                finally:
                    torch._disable_functionalization()

            return wrapper

        result_gm = make_fx(f_wrapper(f), tracing_mode="symbolic")(example_input)
        for node in result_gm.true_graph_0.graph.nodes:
            if node.op == "call_function":
                self.assertTrue(not node.target._schema.is_mutable)

        for node in result_gm.false_graph_0.graph.nodes:
            if node.op == "call_function":
                self.assertTrue(not node.target._schema.is_mutable)

        self.assertEqual(result_gm(torch.ones(5, 5)), f(torch.ones(5, 5)))

    def test_cond_nested_traced_other_inputs(self):
        def true_nested(y):
            return y * y

        def false_nested(y):
            return y + y

        def true_fn(k, pred2):
            z = cond(pred2, true_nested, false_nested, [k])
            return torch.add(torch.tensor([0.25, 0.25]), z)

        def false_fn(k, _):
            return k.cos()

        def f(k, pred, pred2):
            return cond(pred, true_fn, false_fn, [k, pred2])

        x = torch.tensor([0.5, 0.5])
        graph = make_fx(f)(x, torch.tensor(False), torch.tensor(False))

        a = torch.tensor([1.0, 1.0])
        result_true_true = graph.forward(a, torch.tensor(True), torch.tensor(True))
        self.assertEqual(result_true_true, (a * a) + torch.tensor([0.25, 0.25]))

        b = torch.tensor([2.0, 2.0])
        result_true_true = graph.forward(b, torch.tensor(True), torch.tensor(True))
        self.assertEqual(result_true_true, (b * b) + torch.tensor([0.25, 0.25]))

    def test_cond_nested_traced_multi(self):
        def true_a(y):
            return y * y

        def false_a(y):
            return y + y

        def true_b(y, z):
            return y + z

        def false_b(y, z):
            return y * z

        def f(x, pred, pred2):
            a_out = cond(pred, true_a, false_a, [x])
            b_out = cond(pred2, true_b, false_b, [x, x])
            return a_out + b_out

        x = torch.randn(4)
        graph = make_fx(f)(x, torch.tensor(False), torch.tensor(False))

        self.assertExpectedInline(
            graph.code.strip(),
            """\
def forward(self, x_1, pred_1, pred2_1):
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    cond = torch.ops.higher_order.cond(pred_1, true_graph_0, false_graph_0, [x_1]);  pred_1 = true_graph_0 = false_graph_0 = None
    getitem = cond[0];  cond = None
    true_graph_1 = self.true_graph_1
    false_graph_1 = self.false_graph_1
    cond_1 = torch.ops.higher_order.cond(pred2_1, true_graph_1, false_graph_1, [x_1]);  pred2_1 = true_graph_1 = false_graph_1 = x_1 = None
    getitem_1 = cond_1[0];  cond_1 = None
    add = torch.ops.aten.add.Tensor(getitem, getitem_1);  getitem = getitem_1 = None
    return add""",  # noqa: B950
        )
        self.assertExpectedInline(
            graph.true_graph_0.code.strip(),
            """\
def forward(self, arg0_1):
    mul = torch.ops.aten.mul.Tensor(arg0_1, arg0_1);  arg0_1 = None
    return (mul,)""",
        )

    def test_raise_error_on_mismatch_type_size(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return (x, x)

        def f(x, y):
            return cond(y, true_fn, false_fn, [x])

        x = torch.randn(4)
        with self.assertRaisesRegex(
            torch._dynamo.exc.CondOpArgsMismatchError,
            "Expected to return same number of outputs but got:",
        ):
            make_fx(f)(x, torch.tensor(False))

    def test_raise_error_on_mismatch_tensor_size(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return torch.zeros([10, 10])

        def f(x, y):
            return cond(y, true_fn, false_fn, [x])

        x = torch.randn(4)
        with self.assertRaisesRegex(
            torch._dynamo.exc.UncapturedHigherOrderOpError,
            "Cond doesn't work unless it is captured completely with torch.compile",
        ):
            make_fx(f)(x, torch.tensor(False))

    def test_cond_traced_not_nested_fake_tensor(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return x.cos()

        def f(x, y):
            return cond(y, true_fn, false_fn, [x])

        x = torch.randn(4)
        graph = make_fx(f, tracing_mode="fake")(x, torch.tensor(False))
        result_true = graph.forward(x, torch.tensor(True))
        result_false = graph.forward(x, torch.tensor(False))
        self.assertFalse(torch.allclose(result_true, result_false))
        self.assertEqual(result_true, torch.sin(x))
        self.assertEqual(result_false, torch.cos(x))

    def test_cond_nested_traced_fake_tensor(self):
        def true_nested(y):
            return y * y

        def false_nested(y):
            return y + y

        def true_fn(x, pred2):
            z = cond(pred2, true_nested, false_nested, [x])
            return x + z

        def false_fn(x, _):
            return x.cos()

        def f(x, pred, pred2):
            return cond(pred, true_fn, false_fn, [x, pred2])

        x = torch.randn(4)
        graph = make_fx(f, tracing_mode="fake")(
            x, torch.tensor(False), torch.tensor(False)
        )

        result_true_true = graph.forward(
            x, torch.tensor(True), torch.tensor(True)
        )  # True + True -> x * x
        result_true_false = graph.forward(
            x, torch.tensor(True), torch.tensor(False)
        )  # True + True -> x + x
        result_false_true = graph.forward(
            x, torch.tensor(False), torch.tensor(True)
        )  # False + either -> cos
        result_false_false = graph.forward(
            x, torch.tensor(False), torch.tensor(False)
        )  # False + either -> cos

        self.assertNotEqual(result_true_true, result_true_false)
        self.assertFalse(torch.allclose(result_false_true, result_true_true))

        self.assertEqual(result_false_true, result_false_false)

        self.assertEqual(result_true_true, (x * x) + x)
        self.assertEqual(result_true_false, x + x + x)

        self.assertEqual(result_false_true, torch.cos(x))

    def test_cond_nested_traced_other_inputs_fake_tensor(self):
        def true_nested(y):
            return y * y

        def false_nested(y):
            return y + y

        def true_fn(k, pred2):
            z = cond(pred2, true_nested, false_nested, [k])
            return torch.add(torch.tensor([0.25, 0.25]), z)

        def false_fn(k, _):
            return k.cos()

        def f(k, pred, pred2):
            return cond(pred, true_fn, false_fn, [k, pred2])

        x = torch.tensor([0.5, 0.5])
        graph = make_fx(f, tracing_mode="fake")(
            x, torch.tensor(False), torch.tensor(False)
        )

        a = torch.tensor([1.0, 1.0])
        result_true_true = graph.forward(a, torch.tensor(True), torch.tensor(True))
        self.assertEqual(result_true_true, (a * a) + torch.tensor([0.25, 0.25]))

        b = torch.tensor([2.0, 2.0])
        result_true_true = graph.forward(b, torch.tensor(True), torch.tensor(True))
        self.assertEqual(result_true_true, (b * b) + torch.tensor([0.25, 0.25]))

    def test_cond_nested_traced_multi_fake_tensor(self):
        def true_a(y):
            return y * y

        def false_a(y):
            return y + y

        def true_b(y, z):
            return y + z

        def false_b(y, z):
            return y * z

        def f(x, pred, pred2):
            a_out = cond(pred, true_a, false_a, [x])
            b_out = cond(pred2, true_b, false_b, [x, x])
            return a_out + b_out

        x = torch.randn(4)
        graph = make_fx(f, tracing_mode="fake")(
            x, torch.tensor(False), torch.tensor(False)
        )

        self.assertExpectedInline(
            graph.code.strip(),
            """\
def forward(self, x_1, pred_1, pred2_1):
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    cond = torch.ops.higher_order.cond(pred_1, true_graph_0, false_graph_0, [x_1]);  pred_1 = true_graph_0 = false_graph_0 = None
    getitem = cond[0];  cond = None
    true_graph_1 = self.true_graph_1
    false_graph_1 = self.false_graph_1
    cond_1 = torch.ops.higher_order.cond(pred2_1, true_graph_1, false_graph_1, [x_1]);  pred2_1 = true_graph_1 = false_graph_1 = x_1 = None
    getitem_1 = cond_1[0];  cond_1 = None
    add = torch.ops.aten.add.Tensor(getitem, getitem_1);  getitem = getitem_1 = None
    return add""",  # noqa: B950
        )
        self.assertExpectedInline(
            graph.true_graph_0.code.strip(),
            """\
def forward(self, arg0_1):
    mul = torch.ops.aten.mul.Tensor(arg0_1, arg0_1);  arg0_1 = None
    return (mul,)""",
        )

    def test_raise_error_on_mismatch_type_size_fake_tensor(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return (x, x)

        def f(x, y):
            return cond(y, true_fn, false_fn, [x])

        x = torch.randn(4)
        with self.assertRaisesRegex(
            torch._dynamo.exc.CondOpArgsMismatchError,
            "Expected to return same number of outputs but got:",
        ):
            make_fx(f, tracing_mode="fake")(x, torch.tensor(False))

    def test_raise_error_on_mismatch_tensor_size_fake_tensor(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return torch.zeros([10, 10])

        def f(x, y):
            return cond(y, true_fn, false_fn, [x])

        x = torch.randn(4)
        with self.assertRaisesRegex(
            torch._dynamo.exc.UncapturedHigherOrderOpError,
            "Cond doesn't work unless it is captured completely with torch.compile",
        ):
            make_fx(f, tracing_mode="fake")(x, torch.tensor(False))

    def check_map_count(self, gm, op_count):
        i = 0
        for m in gm.modules():
            for node in m.graph.nodes:
                if (
                    node.op == "call_function"
                    and node.target == torch.ops.higher_order.map_impl
                ):
                    i += 1
        self.assertEqual(i, op_count)

    def test_tracing_map_real(self):
        def f(x, y):
            return x + y

        def g(xs, y):
            return control_flow.map(f, xs, y)

        gm = make_fx(g, tracing_mode="real")(torch.ones(3, 2, 2), torch.ones(2))
        x = torch.randn(3, 2, 2)
        y = torch.randn(2)
        res = gm(x, y)
        self.assertEqual(res, g(x, y))
        self.check_map_count(gm, 1)

    def test_tracing_map_symbolic_simple(self):
        def f(x, y):
            return x + y

        def g(xs, y):
            return control_flow.map(f, xs, y)

        gm = make_fx(g, tracing_mode="symbolic")(torch.ones(3, 2, 4), torch.ones(4))
        x = torch.randn(3, 2, 2)
        y = torch.randn(2)
        res = gm(x, y)
        self.assertEqual(res, g(x, y))
        self.check_map_count(gm, 1)

    def test_tracing_map_symbolic_list(self):
        def f(x, y):
            return [x[0][0] + y, x[1] * y]

        def g(xs, y, z):
            out = control_flow.map(f, xs, y)
            return out[0] + z, out[1] * z

        example_x = [[torch.ones(3, 4, 5)], torch.ones(3, 4, 5)]
        gm = make_fx(g, tracing_mode="symbolic")(
            example_x, torch.ones(5), torch.ones(5)
        )
        x = [[torch.randn(4, 5, 6)], torch.ones(4, 5, 6)]
        y = torch.randn(6)
        z = torch.ones(6)
        res = gm(x, y, z)
        self.assertEqual(res, g(x, y, z))
        self.check_map_count(gm, 1)

    def test_tracing_map_symbolic_dict(self):
        def f(x, y):
            return {"d": x["b"]["a"] + y, "e": x["c"] * y}

        def g(xs, y, z):
            out = control_flow.map(f, xs, y)
            return {"f": out["d"] + z, "g": out["e"] * z}

        example_x = {"b": {"a": torch.ones(3, 4, 5)}, "c": torch.ones(3, 4, 5)}
        gm = make_fx(g, tracing_mode="symbolic")(
            example_x, torch.ones(5), torch.ones(5)
        )
        x = {"b": {"a": torch.randn(4, 5, 6)}, "c": torch.ones(4, 5, 6)}
        y = torch.randn(6)
        z = torch.ones(6)
        res = gm(x, y, z)
        self.assertEqual(res, g(x, y, z))
        self.check_map_count(gm, 1)

    def test_tracing_map_autograd_symbolic_simple(self):
        def f(x, y):
            return x + y

        def g(xs, y):
            out = control_flow.map(f, xs, y)
            return torch.autograd.grad(out, (xs, y), torch.ones_like(out))

        gm = make_fx(g, tracing_mode="symbolic")(
            torch.ones(3, 4, 5, requires_grad=True), torch.ones(5, requires_grad=True)
        )
        x = torch.randn(4, 5, 6, requires_grad=True)
        y = torch.randn(6, requires_grad=True)
        res = gm(x, y)
        self.assertEqual(res, g(x, y))
        self.check_map_count(gm, 2)

    def test_tracing_map_autograd_symbolic_list(self):
        import torch.utils._pytree as pytree

        def f(x, y):
            return [x[0].cos() + y.sin(), x[1].sin() * y.cos()]

        def g(xs, y):
            out = control_flow.map(f, xs, y)
            flat_out = pytree.tree_leaves(out)
            flat_inp = pytree.tree_leaves((xs, y))
            requires_grad_inp = [inp for inp in flat_inp if inp.requires_grad]
            return torch.autograd.grad(
                flat_out, requires_grad_inp, [torch.ones_like(out) for out in flat_out]
            )

        gm = make_fx(g, tracing_mode="symbolic")(
            [torch.ones(3, 4, 5), torch.ones(3, 4, 5, requires_grad=True)],
            torch.ones(5, requires_grad=True),
        )
        x = [torch.randn(4, 5, 6), torch.ones(4, 5, 6, requires_grad=True)]
        y = torch.randn(6, requires_grad=True)
        res = gm(x, y)
        self.assertEqual(res, g(x, y))
        self.check_map_count(gm, 2)

    def test_tracing_map_autograd_symbolic_dict(self):
        def f(x, y):
            return [x["a"] + y, x["b"] * y]

        def g(xs, y):
            out = control_flow.map(f, xs, y)
            flat_out = pytree.tree_leaves(out)
            flat_inp = pytree.tree_leaves((xs, y))
            requires_grad_inp = [inp for inp in flat_inp if inp.requires_grad]
            return torch.autograd.grad(
                flat_out, requires_grad_inp, [torch.ones_like(out) for out in flat_out]
            )

        traced_x = {
            "a": torch.ones(3, 4, 5, requires_grad=True),
            "b": torch.ones(3, 4, 5, requires_grad=True),
        }
        gm = make_fx(g, tracing_mode="symbolic")(
            traced_x, torch.ones(5, requires_grad=True)
        )
        x = {
            "a": torch.randn(4, 5, 6, requires_grad=True),
            "b": torch.ones(4, 5, 6, requires_grad=True),
        }
        y = torch.randn(6, requires_grad=True)
        res = gm(x, y)
        self.assertEqual(res, g(x, y))
        self.check_map_count(gm, 2)

    def test_tracing_map_autograd_aot_functionalized(self):
        def inner(x, y):
            z = x - 1
            z.add_(1)
            return z * y

        def f(xs, y):
            res = control_flow.map(inner, xs, y)
            grads = torch.autograd.grad(res, (xs, y), torch.ones_like(res))
            return grads

        def f_wrapper(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                torch._enable_functionalization(reapply_views=False)
                try:
                    return pytree.tree_map(from_fun_old, func(*args, **kwargs))
                finally:
                    torch._disable_functionalization()

            return wrapper

        example_inputs = (
            torch.ones(3, 2, 4, requires_grad=True),
            torch.ones(2, 4, requires_grad=True),
        )
        gm = make_fx(f, tracing_mode="symbolic")(*example_inputs)
        fgm = make_fx(f_wrapper(f), tracing_mode="symbolic")(*example_inputs)
        xs = torch.ones(3, 4, 5, requires_grad=True)
        y = torch.ones(4, 5, requires_grad=True)

        self.assertEqual(gm(xs, y), f(xs, y))

        def count_mutable(gm):
            c = 0
            for node in gm.graph.nodes:
                if node.op == "call_function":
                    if node.target == torch.ops.higher_order.map_impl:
                        c += count_mutable(getattr(gm, str(node.args[0])))
                    elif schema := getattr(node.target, "_schema", None):
                        c += int(schema.is_mutable)
            return c

        self.assertEqual(count_mutable(fgm), 0)
        # One for forward, one for recomputation logic in backward
        self.assertEqual(count_mutable(gm), 2)

    def test_map_functionalized(self):
        def map_fn(x, y):
            z = x + y
            z.add_(4)
            return z

        def f(xs, y):
            return control_flow.map(map_fn, xs, y)

        example_inputs = (torch.ones(3, 2, 4), torch.ones(4))
        functional_f = torch.func.functionalize(f)
        self.assertEqual(functional_f(*example_inputs), f(*example_inputs))

        gm = make_fx(torch.func.functionalize(f))(*example_inputs)
        self.assertEqual(gm(*example_inputs), f(*example_inputs))

        gm = make_fx(torch.func.functionalize(f), tracing_mode="symbolic")(
            *example_inputs
        )
        self.assertEqual(gm(*example_inputs), f(*example_inputs))

        for node in gm.body_graph_0.graph.nodes:
            if node.op == "call_function":
                self.assertTrue(not node.target._schema.is_mutable)
        self.check_map_count(gm, 1)

    def test_map_functionalized_aot_func(self):
        def map_fn(x, y):
            z = x + y
            z.add_(4)
            return z

        def f(xs, y):
            return control_flow.map(map_fn, xs, y)

        def f_wrapper(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                torch._enable_functionalization(reapply_views=False)
                try:
                    return pytree.tree_map(from_fun_old, func(*args, **kwargs))
                finally:
                    torch._disable_functionalization()

            return wrapper

        example_inputs = (torch.ones(3, 2, 4), torch.ones(4))

        gm = make_fx(f_wrapper(f))(*example_inputs)

        for node in gm.body_graph_0.graph.nodes:
            if node.op == "call_function":
                self.assertTrue(not node.target._schema.is_mutable)

        self.assertEqual(gm(*example_inputs), f(*example_inputs))

    # https://github.com/pytorch/pytorch/issues/126988
    @xfailIfTorchDynamo
    def test_map_functionalized_arg_mutation(self):
        def map_fn(x, y):
            y.add_(4)
            return x + y

        def f(xs, y):
            return control_flow.map(map_fn, xs, y)

        example_inputs = (torch.ones(3, 2, 4), torch.ones(4))
        functional_f = torch.func.functionalize(f)
        with self.assertRaisesRegex(
            UnsupportedAliasMutationException, "torch.map is mutating the input!"
        ):
            functional_f(*example_inputs)

    # https://github.com/pytorch/pytorch/issues/126988
    @xfailIfTorchDynamo
    def test_map_functionalized_elem_mutation(self):
        def map_fn(x, y):
            x.add_(4)
            return x + y

        def f(xs, y):
            return control_flow.map(map_fn, xs, y)

        example_inputs = (torch.ones(3, 2, 4), torch.ones(4))
        functional_f = torch.func.functionalize(f)
        with self.assertRaisesRegex(
            UnsupportedAliasMutationException, "torch.map is mutating the input!"
        ):
            functional_f(*example_inputs)

    def test_cond_autograd_backward(self):
        def true_fn(x):
            return x.cos()

        def false_fn(x):
            return x.sin()

        def f(x, y):
            return control_flow.cond(x.shape[0] > 4, true_fn, false_fn, [y])

        example_inputs = (
            torch.ones(3, 2, 4, requires_grad=True),
            torch.ones(4, requires_grad=True),
        )
        f(*example_inputs).sum().backward()

        # Ensure no error is thrown when not running backward
        res = f(*example_inputs)

        # Ensure no error is thrown when not running backward
        res_compiled = torch.compile(f)(*example_inputs)
        self.assertEqual(res, res_compiled)

    # https://github.com/pytorch/pytorch/issues/126988
    @xfailIfTorchDynamo
    def test_map_functionalized_elem_alias(self):
        def map_fn(x):
            x.view(x.shape)
            return x

        def f(xs):
            return control_flow.map(map_fn, xs)

        example_inputs = (torch.ones(3, 2, 4),)
        functional_f = torch.func.functionalize(f)
        with self.assertRaisesRegex(
            UnsupportedAliasMutationException, "torch.map is aliasing the input!"
        ):
            functional_f(*example_inputs)

    def test_nested_map_cond_real(self):
        def true_fn(x, y):
            return x * y

        def false_fn(x, y):
            return x + y

        def f(x, pred, y):
            return cond(pred, true_fn, false_fn, [x, y])

        def g(pred, xs, y):
            return control_flow.map(f, xs, pred, y)

        gm = make_fx(g, tracing_mode="real")(
            torch.tensor(True), torch.ones(3, 2, 4), torch.ones(4)
        )
        pred = torch.tensor(False)
        x = torch.randn(3, 2, 4)
        y = torch.randn(4)
        res = gm(pred, x, y)
        self.assertEqual(res, g(pred, x, y))
        self.check_map_count(gm, 1)

    def test_nested_map_cond_symbolic(self):
        def true_fn(x, y):
            return x * y

        def false_fn(x, y):
            return x + y

        def f(x, pred, y):
            return cond(pred, true_fn, false_fn, [x, y])

        def g(pred, xs, y):
            return control_flow.map(f, xs, pred, y)

        gm = make_fx(g, tracing_mode="symbolic")(
            torch.tensor(True), torch.ones(3, 2, 4), torch.ones(4)
        )
        pred = torch.tensor(False)
        x = torch.randn(3, 2, 2)
        y = torch.randn(2)
        res = gm(pred, x, y)
        self.assertEqual(res, g(pred, x, y))
        self.check_map_count(gm, 1)

    def test_nested_cond_map_cond_symbolic(self):
        def true_fn(x, y):
            return x * y

        def false_fn(x, y):
            return x + y

        def f(x, pred, y):
            return cond(pred, true_fn, false_fn, [x, y])

        def g(pred, xs, y):
            return control_flow.map(f, xs, pred, y)

        def main_true_fn(pred, xs, y):
            return g(pred, xs, y) * 2

        def main_false_fn(pred, xs, y):
            return g(pred, xs, y) + 1

        def main(p, pred, xs, y):
            return cond(p, main_true_fn, main_false_fn, [pred, xs, y])

        gm = make_fx(main, tracing_mode="symbolic")(
            torch.tensor(True), torch.tensor(True), torch.ones(3, 2, 4), torch.ones(4)
        )
        p = torch.tensor(False)
        pred = torch.tensor(False)
        xs = torch.randn(3, 2, 2)
        y = torch.randn(2)
        res = gm(p, pred, xs, y)
        self.assertEqual(res, main(p, pred, xs, y))
        self.check_map_count(gm, 2)

    def test_cond_with_sym_pred(self):
        def true_fn(x):
            return x + x

        def false_fn(x):
            return x * x

        def foo(x):
            return cond(x.shape[0] == 4, true_fn, false_fn, [x])

        gm = make_fx(foo, tracing_mode="symbolic")(torch.ones(3, 2, 1))
        # The symbols in make_fx's shape_env should not be specialized.
        self.assertEqual(len(gm.shape_env.guards), 0)

        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, x_1):
    sym_size_int = torch.ops.aten.sym_size.int(x_1, 0)
    eq = sym_size_int == 4;  sym_size_int = None
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    cond = torch.ops.higher_order.cond(eq, true_graph_0, false_graph_0, [x_1]);  eq = true_graph_0 = false_graph_0 = x_1 = None
    getitem = cond[0];  cond = None
    return getitem""",  # noqa: B950
        )

        # We expect the traced graph module to work even if input size changes.
        x = torch.ones(4, 3, 2)
        self.assertEqual(gm(x), true_fn(x))
        self.assertEqual(foo(x), true_fn(x))

    def test_cond_with_unbacked_sym_pred(self):
        def foo(x):
            def true_fn(x):
                return x + x

            def false_fn(x):
                return x * x

            az = x.nonzero()
            return cond(az.shape[0] > 3, true_fn, false_fn, (x,))

        gm = make_fx(foo, tracing_mode="symbolic")(torch.randn(7))
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, x_1):
    nonzero = torch.ops.aten.nonzero.default(x_1)
    sym_size_int = torch.ops.aten.sym_size.int(nonzero, 0);  nonzero = None
    gt = sym_size_int > 3;  sym_size_int = None
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    cond = torch.ops.higher_order.cond(gt, true_graph_0, false_graph_0, [x_1]);  gt = true_graph_0 = false_graph_0 = x_1 = None
    getitem = cond[0];  cond = None
    return getitem""",
        )

    def _check_closure_correctly_lifted(self, f, *, args, exp_res, exp_arg_num):
        assert isinstance(args, (tuple, list))
        self.assertEqual(f(*args), exp_res)
        gm = make_fx(f)(*args)
        self.assertEqual(gm(*args), exp_res)

        def cnt_placeholder(gm):
            return len([node for node in gm.graph.nodes if node.op == "placeholder"])

        placeholder_cnts = [cnt_placeholder(mod) for mod in gm.children()]
        self.assertTrue(all(cnt == exp_arg_num for cnt in placeholder_cnts))

    def _check_closure_correctly_lifted_with_mutation(
        self, f, closures_to_be_mutated, *, args, exp_arg_num
    ):
        exp_res = f(*args)
        self._check_closure_correctly_lifted(
            f, args=args, exp_res=exp_res, exp_arg_num=exp_arg_num
        )

        for closure in closures_to_be_mutated:
            closure.add(-1)
        new_exp_res = f(*args)

        self._check_closure_correctly_lifted(
            f, args=args, exp_res=new_exp_res, exp_arg_num=exp_arg_num
        )

    def test_cond_with_tensor_closure(self):
        a = torch.ones(2, 3)
        b = torch.ones(2, 3) + 1

        def true_fn(x):
            return x + a

        def false_fn(x):
            return x + b

        def foo(x):
            return cond(x.shape[0] == 4, true_fn, false_fn, [x])

        # expected branches takes [x, a, b] as input
        inp = torch.randn(2, 3)
        self._check_closure_correctly_lifted_with_mutation(
            foo, (a, b), args=(inp,), exp_arg_num=3
        )

    def test_cond_with_tensor_closure_graph_module(self):
        a = torch.ones(2, 3)
        b = torch.ones(2, 3) + 1

        def true_fn(x):
            return x + a

        def false_fn(x):
            return x + b

        def foo(x):
            return cond(x.shape[0] == 4, true_fn, false_fn, [x])

        # expected branches takes [x, a, b] as input
        inp = torch.randn(2, 3)

        gm = make_fx(foo, tracing_mode="symbolic", _allow_non_fake_inputs=True)(inp)

        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, x_1):
    sym_size_int = torch.ops.aten.sym_size.int(x_1, 0)
    eq = sym_size_int == 4;  sym_size_int = None
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    _tensor_constant0 = self._tensor_constant0
    _tensor_constant1 = self._tensor_constant1
    cond = torch.ops.higher_order.cond(eq, true_graph_0, false_graph_0, [x_1, _tensor_constant0, _tensor_constant1]);  eq = true_graph_0 = false_graph_0 = x_1 = _tensor_constant0 = _tensor_constant1 = None
    getitem = cond[0];  cond = None
    return getitem""",  # noqa: B950
        )
        self.assertExpectedInline(
            gm.true_graph_0.code.strip(),
            """\
def forward(self, arg0_1, arg1_1, arg2_1):
    add = torch.ops.aten.add.Tensor(arg0_1, arg1_1);  arg0_1 = arg1_1 = None
    return (add,)""",
        )

    def test_cond_with_module_param_closure(self):
        class Mod(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.register_parameter(
                    "param", torch.nn.Parameter(torch.ones(2, 3), requires_grad=False)
                )
                self.buffer = torch.nn.Buffer(torch.ones(2, 3) + 1)

        my_mode = Mod()

        def true_fn(x):
            return x + my_mode.param

        def false_fn(x):
            return x + my_mode.buffer

        def foo(x):
            return cond(x.shape[0] == 4, true_fn, false_fn, [x])

        inp = torch.ones(2, 3)
        # expected both branches takes (x, param, buffer)
        self._check_closure_correctly_lifted_with_mutation(
            foo, (my_mode.param, my_mode.buffer), args=(inp,), exp_arg_num=3
        )

    def test_cond_with_module_python_scalar_closure(self):
        def foo(x):
            a = torch.ones(1, 1)
            b = 1

            def true_fn(x):
                return x + a

            def false_fn(x):
                return x + b

            return cond(x.shape[0] == 4, true_fn, false_fn, [x])

        inp = torch.ones(2, 3)
        res = inp + 1
        # python scalar b is not lifted as input, so both branches take (x, a)
        self._check_closure_correctly_lifted(
            foo, args=(inp,), exp_res=res, exp_arg_num=2
        )

    def test_cond_nested_with_closure(self):
        a = torch.ones(1, 1)
        b = torch.ones(1, 1) + 1

        def inner_true_fn(x):
            return x + a

        def inner_false_fn(x):
            return x + b

        def foo(x):
            def true_fn(x):
                return cond(x.shape[0] == 2, inner_true_fn, inner_false_fn, [x])

            def false_fn(x):
                return cond(x.shape[0] > 4, inner_true_fn, inner_false_fn, [x])

            return cond(x.shape[0] == 4, true_fn, false_fn, [x])

        inp = torch.ones(2, 3)
        # For top-level cond, it take 3 arguments (x, a, b). Dynamo should
        # realize that the nonlocal variables are same for the true and false
        # branches, so it should de-dupe them.
        # For second-level conds, it takes (x, a, b)
        self._check_closure_correctly_lifted_with_mutation(
            foo, (a, b), args=(inp,), exp_arg_num=3
        )

    def test_cond_nested_with_closure_graph_module(self):
        a = torch.ones(1, 1)
        b = torch.ones(1, 1) + 1

        def inner_true_fn(x):
            return x + a

        def inner_false_fn(x):
            return x + b

        def foo(x):
            def true_fn(x):
                return cond(x.shape[0] == 2, inner_true_fn, inner_false_fn, [x])

            def false_fn(x):
                return cond(x.shape[0] > 4, inner_true_fn, inner_false_fn, [x])

            return cond(x.shape[0] == 4, true_fn, false_fn, [x])

    def test_map_unfunc_boolean_tensor_for_nested_map_cond(self):
        def map_fn(pred, x):
            def fn(x, pred):
                return control_flow.cond(pred, lambda x: x * 2, lambda x: x / 2, (x,))

            return control_flow.map(fn, x, pred)

        def f_wrapper(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                torch._enable_functionalization(reapply_views=False)
                try:
                    func_args = pytree.tree_map(
                        lambda x: to_fun_old(x) if isinstance(x, torch.Tensor) else x,
                        args,
                    )
                    func_kwargs = pytree.tree_map(
                        lambda x: to_fun_old(x) if isinstance(x, torch.Tensor) else x,
                        kwargs,
                    )
                    return pytree.tree_map(
                        from_fun_old, func(*func_args, **func_kwargs)
                    )
                finally:
                    torch._disable_functionalization()

            return wrapper

        gm = make_fx(f_wrapper(map_fn))(
            torch.tensor(True), torch.ones([2, 3], requires_grad=False)
        )
        self.assertExpectedInline(
            gm.code.strip(),
            """\
def forward(self, pred_1, x_1):
    body_graph_0 = self.body_graph_0
    map_impl = torch.ops.higher_order.map_impl(body_graph_0, [x_1], [pred_1]);  body_graph_0 = x_1 = pred_1 = None
    getitem = map_impl[0];  map_impl = None
    return getitem""",
        )
        self.assertExpectedInline(
            gm.body_graph_0.code.strip(),
            """\
def forward(self, arg0_1, arg1_1):
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    cond = torch.ops.higher_order.cond(arg1_1, true_graph_0, false_graph_0, [arg0_1]);  arg1_1 = true_graph_0 = false_graph_0 = arg0_1 = None
    getitem = cond[0];  cond = None
    return [getitem]""",  # noqa: B950
        )

    def test_cond_make_fx_preserve_stack_trace_for_nodes_in_subgraph(self):
        def true_fn(x):
            return x + x.cos()

        def false_fn(x):
            return x * x.sin()

        def foo(x):
            return cond(x.shape[0] == 4, true_fn, false_fn, (x,))

        inp = torch.randn([4, 3])
        gm, _ = torch._dynamo.export(foo)(inp)

        def run_with_interpreter(*args):
            with torch.fx.traceback.preserve_node_meta():
                return torch.fx.Interpreter(gm).run(*args)

        new_gm = make_fx(run_with_interpreter)(inp)

        checked_ops = {"add", "mul", "sin", "cos"}
        checked_meta = ["source_fn_stack", "stack_trace"]
        all_source_fns = collect_meta_for_filtered_nodes(gm, checked_ops, checked_meta)
        new_source_fns = collect_meta_for_filtered_nodes(
            new_gm, checked_ops, checked_meta
        )
        self.assertEqual(all_source_fns, new_source_fns)

    @unittest.skipIf(
        TEST_WITH_TORCHDYNAMO,
        "triggers cache limit for foo and changes unique_graphs count.",
    )
    def test_cond_no_dynamo_cache_limit(self):
        torch._dynamo.reset()
        counters = torch._dynamo.utils.counters
        counters.clear()

        def foo(x, true_fn, false_fn):
            return cond(x.sum() < 0, true_fn, false_fn, (x,))

        inp = torch.ones(3, 4)
        exp_out = inp.sin()
        iter_n = torch._dynamo.config.cache_size_limit + 1

        # Need this because Dynamo checks lambda code ID not object itself.
        def make_dummy_fn(op):
            exec(f"temp = lambda x: x.{op}()")
            return locals()["temp"]

        for _ in range(iter_n):
            # each lambda has a different object id thus fails the guard
            self.assertEqual(
                foo(inp, make_dummy_fn("cos"), make_dummy_fn("sin")), exp_out
            )

        # each iteration captures a cond and a getitem from the tuple output
        self.assertEqual(counters["stats"]["calls_captured"], iter_n * 2)
        self.assertEqual(counters["stats"]["unique_graphs"], iter_n)

    def test_cond_with_consecutive_make_fx_symbolic(self):
        def true_fn(x):
            return x - x.cos()

        def false_fn(x):
            return x + x.sin()

        def foo(x):
            return cond(x.shape[0] == 4, true_fn, false_fn, [x])

        inps = (torch.ones(3, 4), torch.ones(3, 5), torch.ones(5, 4), torch.ones(5, 3))
        for inp in inps:
            gm = make_fx(foo, tracing_mode="symbolic")(torch.ones(3, 4))
            self.assertExpectedInline(
                gm.code.strip(),
                """\
def forward(self, x_1):
    sym_size_int = torch.ops.aten.sym_size.int(x_1, 0)
    eq = sym_size_int == 4;  sym_size_int = None
    true_graph_0 = self.true_graph_0
    false_graph_0 = self.false_graph_0
    cond = torch.ops.higher_order.cond(eq, true_graph_0, false_graph_0, [x_1]);  eq = true_graph_0 = false_graph_0 = x_1 = None
    getitem = cond[0];  cond = None
    return getitem""",  # noqa: B950
            )

            self.assertExpectedInline(
                gm.true_graph_0.code.strip(),
                """\
def forward(self, arg0_1):
    cos = torch.ops.aten.cos.default(arg0_1)
    sub = torch.ops.aten.sub.Tensor(arg0_1, cos);  arg0_1 = cos = None
    return (sub,)""",
            )

            self.assertExpectedInline(
                gm.false_graph_0.code.strip(),
                """\
def forward(self, arg0_1):
    sin = torch.ops.aten.sin.default(arg0_1)
    add = torch.ops.aten.add.Tensor(arg0_1, sin);  arg0_1 = sin = None
    return (add,)""",
            )

    def _create_test_fns_for_cond(
        self, pred, inner_most_fn, operands, closure_list, nested_level
    ):
        if nested_level == 0:
            if len(closure_list) > 0:

                def true_fn(*operands):
                    return inner_most_fn(*operands) + inner_most_fn(*closure_list)

                def false_fn(*operands):
                    return inner_most_fn(*operands) - inner_most_fn(*closure_list)

            else:

                def true_fn(*operands):
                    return inner_most_fn(*operands)

                def false_fn(*operands):
                    return inner_most_fn(*operands)

            def fn(*operands):
                if len(operands) == 0 and len(closure_list) == 0:
                    return torch.zeros(1)
                return cond(pred, true_fn, false_fn, operands)

            return operands, fn
        else:
            args, inner_fn = self._create_test_fns_for_cond(
                pred <= 0, inner_most_fn, operands, closure_list, nested_level - 1
            )

            def true_fn(*operands):
                return inner_most_fn(*operands) + inner_fn(*args)

            def false_fn(*operands):
                return inner_most_fn(*operands) - inner_fn(*args)

            def fn(*operands):
                if len(operands) == 0 and len(closure_list) == 0:
                    return torch.ones(1)
                return cond(pred, true_fn, false_fn, operands)

            return operands, fn

    def _init_predicate(self, pred_type):
        if pred_type == "bool":
            return True
        elif pred_type == "intTensor":
            return torch.tensor(1)
        elif pred_type == "floatTensor":
            return torch.tensor(1.0)
        elif pred_type == "boolTensor":
            return torch.tensor(False)
        else:
            raise NotImplementedError

    def _init_fn(self, inner_fn_type):
        if inner_fn_type == "function":
            return reduce_func
        elif inner_fn_type == "module":
            return ReduceMod()
        elif inner_fn_type == "object":
            return ReduceObj()
        else:
            raise NotImplementedError

    @parametrize("predType", ["bool", "intTensor", "floatTensor", "boolTensor"])
    @parametrize("innerFnType", ["function", "module", "object"])
    @parametrize("nOperands", [0, 1])
    @parametrize("nClosure", [0, 1])
    @parametrize("nesting", [0, 2])
    def test_cond_tracing_with_valid_inputs(
        self, predType, innerFnType, nOperands, nClosure, nesting
    ):
        pred = self._init_predicate(predType)
        inner_fn = self._init_fn(innerFnType)
        operands = [torch.ones(2, 3) + i for i in range(nOperands)]
        closure = [torch.ones(2, 3) - i for i in range(nClosure)]
        args, fn = self._create_test_fns_for_cond(
            pred, inner_fn, operands, closure, nesting
        )
        eager_res = fn(*args)
        for tracing_mode in ["symbolic", "fake", "real"]:
            # set _allow_non_fake_inputs = True to allow fake prop through closures
            with self.subTest(tracing_mode=tracing_mode):
                gm = make_fx(
                    fn, tracing_mode=tracing_mode, _allow_non_fake_inputs=True
                )(*args)
                self.assertEqual(gm(*args), eager_res)

    @parametrize("predType", ["boolTensor"])
    @parametrize("innerFnType", ["function", "module", "object"])
    @parametrize("nOperands", [1, 2])
    @parametrize("nClosure", [0, 1])
    @parametrize("nesting", [0])
    def test_cond_vmap(self, predType, innerFnType, nOperands, nClosure, nesting):
        pred = self._init_predicate(predType)
        inner_fn = self._init_fn(innerFnType)
        operands = [torch.ones(2, 3) + i for i in range(nOperands)]
        closure = [torch.ones(2, 3) - i for i in range(nClosure)]
        args, fn = self._create_test_fns_for_cond(
            pred, inner_fn, operands, closure, nesting
        )
        eager_res = fn(*args)
        out = torch.vmap(fn)(*args)
        if nClosure == 0:
            self.assertEqual(eager_res, out)
        else:
            self.assertEqual(eager_res, out[0])
            self.assertEqual(eager_res, out[1])

    def test_cond_vmap_simple(self):
        def fn(x):
            return torch.cond(
                pred=torch.tensor([True]),
                true_fn=lambda x: x + 100,
                false_fn=lambda x: x,
                operands=(x,),
            )

        a = torch.arange(15).reshape((3, 5))
        res = torch.vmap(fn, in_dims=(0,))(a)
        self.assertEqual(res.shape, (3, 5))
        self.assertEqual(res, a + 100)

    def test_cond_vmap_multiple_inputs(self):
        def fn(x, y):
            return torch.cond(
                pred=x.sum() < y.sum(),
                true_fn=lambda x, y: x + 100,
                false_fn=lambda x, y: y,
                operands=(x, y),
            )

        a = torch.arange(15).reshape(3, 5)
        b = torch.ones_like(a) + 3
        res = torch.vmap(fn, in_dims=(0, 0))(a, b)
        expected = torch.tensor(
            [[100, 101, 102, 103, 104], [4, 4, 4, 4, 4], [4, 4, 4, 4, 4]]
        )
        self.assertEqual(res.shape, (3, 5))
        self.assertEqual(expected, res)

    def test_cond_vmap_single_input_with_closure(self):
        a = torch.ones((3, 5)) + 3
        c = torch.arange(5)

        def fn(x):
            return torch.cond(
                pred=torch.tensor([True]),
                true_fn=lambda x: x + c,
                false_fn=lambda x: x - c,
                operands=(x,),
            )

        res = torch.vmap(fn, in_dims=(0,))(
            a,
        )
        with unittest.mock.patch("torch._dynamo.config.error_on_recompile", True):
            res = torch.vmap(fn, in_dims=(0,))(
                a,
            )
        self.assertEqual(a + c, res)

    def test_cond_vmap_multiple_args_with_closure(self):
        a = torch.ones((3, 5), dtype=torch.int64) + 3
        b = torch.arange(15).reshape(3, 5)
        c = torch.arange(5)

        def fn(x, y):
            return torch.cond(
                pred=torch.tensor([False]),
                true_fn=lambda x, y: x + c,
                false_fn=lambda x, y: y - c,
                operands=(x, y),
            )

        res = torch.vmap(fn)(a, b)
        self.assertEqual(b - c, res)

    @parametrize("nClosure", [0, 1])
    def test_cond_vmap_multiple_outputs(self, nClosure):
        if nClosure:
            c = torch.ones(5, dtype=torch.int64) + 5

            def fn(x):
                return torch.cond(
                    pred=torch.tensor([True]),
                    true_fn=lambda x: (x + c, x - c),
                    false_fn=lambda x: (x, x),
                    operands=(x,),
                )

        else:

            def fn(x):
                return torch.cond(
                    pred=torch.tensor([True]),
                    true_fn=lambda x: (x + 1, x - 1),
                    false_fn=lambda x: (x, x),
                    operands=(x,),
                )

        a = torch.arange(15).reshape(3, 5)
        res = torch.vmap(fn)(
            a,
        )
        self.assertEqual(len(res), 2)
        if nClosure:
            self.assertEqual(res, (a + c, a - c))
        else:
            self.assertEqual(res, (a + 1, a - 1))

    def test_vmap_vmap(self):
        def fn(x):
            return torch.cond(
                pred=torch.tensor([True]),
                true_fn=lambda x: x + 1,
                false_fn=lambda x: x - 1,
                operands=(x,),
            )

        def wrapper(x):
            return torch.vmap(fn)(x)

        a = torch.ones((3, 4, 5))
        res = torch.vmap(wrapper)(a)
        self.assertEqual(res, a + 1)

    def test_cond_trace_set__and_mutate_input(self):
        def f(a, tmp):
            a_view = a.view(-1)
            with torch.no_grad():
                a.set_(tmp)
                a_view.mul_(2)
            return a + tmp

        inp = torch.ones(3, 3, requires_grad=True)
        tmp = torch.ones(3, 3, requires_grad=True)
        # graph break: torch._dynamo.exc.Unsupported: call_function DelayGraphBreakVariable() [TensorVariable()] {}
        # due to set_
        with self.assertRaisesRegex(
            torch._dynamo.exc.UncapturedHigherOrderOpError,
            "Cond doesn't work unless it is captured completely with torch.compile",
        ):
            torch.cond(inp.sum() > 0, f, f, (inp, tmp))

    def test_cond_trace_set__and_mutate_intermediate(self):
        def f(a, tmp):
            a = a.clone()
            a_view = a.view(-1)
            tmp = tmp.clone()
            with torch.no_grad():
                a.set_(tmp)
                a_view.mul_(2)
            return a + tmp

        inp = torch.ones(3, 3, requires_grad=True)
        tmp = torch.ones(3, 3, requires_grad=True)

        class Mod(torch.nn.Module):
            def forward(self, inp: torch.Tensor, tmp: torch.Tensor) -> torch.Tensor:
                return torch.cond(inp.sum() > 0, f, f, (inp, tmp))

        with self.assertRaisesRegex(
            RuntimeError, "cannot mutate tensors with frozen storage"
        ):
            out = torch.compile(Mod(), backend="aot_eager")(inp, tmp)

        with self.assertRaisesRegex(
            RuntimeError, "cannot mutate tensors with frozen storage"
        ):
            out = torch.compile(Mod(), backend="inductor")(inp, tmp)

        from torch._dynamo.testing import EagerAndRecordGraphs

        backend = EagerAndRecordGraphs()
        out = torch.compile(Mod(), backend=backend)(inp, tmp)
        self.assertExpectedInline(
            backend.graphs[0].cond_true_0.code.strip("\n"),
            """\
def forward(self, l_inp_, l_tmp_):
    l_inp__1 = l_inp_
    l_tmp__1 = l_tmp_
    a = l_inp__1.clone();  l_inp__1 = None
    a_view = a.view(-1)
    tmp = l_tmp__1.clone();  l_tmp__1 = None
    _set_grad_enabled = torch._C._set_grad_enabled(False);  _set_grad_enabled = None
    set_ = a.set_(tmp);  set_ = None
    mul_ = a_view.mul_(2);  a_view = mul_ = None
    _set_grad_enabled_1 = torch._C._set_grad_enabled(True);  _set_grad_enabled_1 = None
    add = a + tmp;  a = tmp = None
    return (add,)
    """,
        )
        self.assertEqual(out, f(inp, tmp))

    def test_two_hops_not_sharing_code_obj(self):
        pred, args = torch.tensor(True), (torch.ones(3, 3),)

        def fn1(x):
            return x + 1

        def fn2(x):
            return x - 1

        from torch._dynamo.testing import CompileCounter

        # Tests rely on automatic_dynamic = True
        with torch._dynamo.config.patch(automatic_dynamic_shapes=True):
            cnt = CompileCounter()
            torch.compile(torch.cond, backend=cnt)(pred, fn1, fn2, args)
            self.assertEqual(cnt.frame_count, 1)

            args = (torch.randn(3, 3),)
            # No recompilation
            torch.compile(torch.cond, backend=cnt)(pred, fn1, fn2, args)
            self.assertEqual(cnt.frame_count, 1)

            def cond_fn(x):
                return x.sum() > 0

            args = (torch.randn(4, 4),)
            torch.compile(torch.while_loop, backend=cnt)(cond_fn, fn2, args)
            # recompilation
            self.assertEqual(cnt.frame_count, 2)

            args = (torch.randn(4, 4),)
            torch.compile(torch.while_loop, backend=cnt)(cond_fn, fn2, args)
            self.assertEqual(cnt.frame_count, 2)

            # With recompilation due to automatic dynamic
            # This also proves that while_loop doesn't share code obj with cond
            torch.compile(torch.cond, backend=cnt)(pred, fn1, fn2, (torch.randn(4, 4),))
            self.assertEqual(cnt.frame_count, 3)

    def test_hop_raises_if_not_overriding_call(self):
        class WrongHop(torch._ops.HigherOrderOperator):
            pass

        with self.assertRaisesRegex(TypeError, "WrongHop"):
            wrong_hop = WrongHop("wrong_hop")

    def test_scan_functionalized(self):
        def f(init, xs):
            return scan(get_scan_combine_fn("add", False), init, xs, dim=1)

        example_inputs = torch.ones(5, 7, 4)
        example_init = torch.ones(5, 1, 4)
        functional_f = torch.func.functionalize(f)
        self.assertEqual(
            functional_f(example_init, example_inputs), f(example_init, example_inputs)
        )

    # https://github.com/pytorch/pytorch/issues/126988
    @xfailIfTorchDynamo
    def test_scan_functionalized_elem_mutation(self):
        def add1(x, y):
            x.add_(4)
            return x + y, x + y

        def f(init, xs):
            return scan(add1, init, xs, dim=1)

        example_inputs = torch.ones(5, 7, 4)
        example_init = torch.ones(5, 1, 4)
        functional_f = torch.func.functionalize(f)
        with self.assertRaisesRegex(
            UnsupportedAliasMutationException,
            "Combine_fn might be modifying the input!",
        ):
            functional_f(example_init, example_inputs)

        def add2(x, y):
            y.add_(4)
            return x + y, x + y

        def f(init, xs):
            return scan(add2, init, xs, dim=1)

        example_inputs = torch.ones(5, 7, 4)
        example_init = torch.ones(5, 1, 4)
        functional_f = torch.func.functionalize(f)
        with self.assertRaisesRegex(
            UnsupportedAliasMutationException,
            "Combine_fn might be modifying the input!",
        ):
            functional_f(example_init, example_inputs)

    # https://github.com/pytorch/pytorch/issues/126988
    @xfailIfTorchDynamo
    def test_scan_functionalized_elem_alias(self):
        def add(x, y):
            return x, x

        def f(init, xs):
            return scan(add, init, xs, dim=1)

        example_inputs = torch.ones(5, 7, 4)
        example_init = torch.ones(5, 1, 4)
        functional_f = torch.func.functionalize(f)
        with self.assertRaisesRegex(
            UnsupportedAliasMutationException, "Combine_fn might be aliasing the input!"
        ):
            functional_f(example_init, example_inputs)


_hop_schema_test_schema_types = [
    "bool",
    "int",
    "float",
    "str",
    "Tensor",
    "SymInt",
    "SymBool",
    "GraphModule",
    "ScriptObj",
]


@unittest.skipIf(IS_WINDOWS, "Windows not supported for this test")
class TestHopSchema(TestCase):
    def _get_example_val(self, ty: str):
        from torch.fx.experimental.sym_node import SymNode
        from torch.fx.experimental.symbolic_shapes import ShapeEnv

        def create_symtype(cls, pytype, shape_env, val):
            from torch._dynamo.source import ConstantSource

            symbol = shape_env.create_symbol(
                val,
                source=ConstantSource(
                    f"__testing_hop_schema{len(shape_env.var_to_val)}"
                ),
            )
            return cls(SymNode(symbol, shape_env, pytype, hint=val))

        if ty == "bool":
            return True
        elif ty == "int":
            return 1
        elif ty == "float":
            return 1.0
        elif ty == "str":
            return "foo"
        elif ty == "Tensor":
            return torch.tensor(1)
        elif ty == "SymInt":
            shape_env = ShapeEnv()
            return create_symtype(torch.SymInt, int, shape_env, 1)
        elif ty == "SymBool":
            shape_env = ShapeEnv()
            return create_symtype(torch.SymBool, bool, shape_env, True)
        elif ty == "GraphModule":

            def f(x):
                return x.sin()

            return make_fx(f)(torch.ones(1))
        elif ty == "ScriptObj":
            from torch.testing._internal.torchbind_impls import (
                init_torchbind_implementations,
            )

            init_torchbind_implementations()
            foo = torch.classes._TorchScriptTesting._Foo(3, 4)
            return foo
        else:
            raise NotImplementedError(ty)

    @parametrize("schema_type", _hop_schema_test_schema_types)
    def test_type_gen(self, schema_type):
        from torchgen.gen_schema_utils import TypeGen

        example_val = self._get_example_val(schema_type)
        ty = TypeGen.from_example(example_val)
        # Test the generated type can be parsed
        self.assertEqual(ty.parse(str(ty)), ty)

    @parametrize("schema_type", _hop_schema_test_schema_types)
    def test_list_gen(self, schema_type):
        from torchgen.gen_schema_utils import TypeGen

        example_val = self._get_example_val(schema_type)
        li1 = [example_val]
        li2 = [example_val, example_val]
        ty1 = TypeGen.from_example(li1)
        ty2 = TypeGen.from_example(li1)
        self.assertEqual(ty1.parse(str(ty1)), ty1)
        self.assertEqual(ty2.parse(str(ty2)), ty2)

    def test_function_schema_gen(self):
        from torchgen.gen_schema_utils import FunctionSchemaGen

        inps = [
            (schema_type + "_v", self._get_example_val(schema_type))
            for schema_type in _hop_schema_test_schema_types
        ]
        op_name = "test_op"
        schema1 = FunctionSchemaGen.from_example("test_op1", inps, torch.ones(1))
        schema2 = FunctionSchemaGen.from_example(
            "test_op2",
            inps,
            [
                torch.ones(1),
            ],
        )
        schema3 = FunctionSchemaGen.from_example(
            "test_op3", inps, [torch.ones(1), torch.ones(1)]
        )
        self.assertExpectedInline(
            str(schema1),
            """test_op1(bool bool_v, int int_v, float float_v, str str_v, Tensor Tensor_v, SymInt SymInt_v, SymBool SymBool_v, GraphModule GraphModule_v, __torch__.torch.classes._Foo ScriptObj_v) -> Tensor""",  # noqa: B950
        )
        self.assertExpectedInline(
            str(schema2),
            """test_op2(bool bool_v, int int_v, float float_v, str str_v, Tensor Tensor_v, SymInt SymInt_v, SymBool SymBool_v, GraphModule GraphModule_v, __torch__.torch.classes._Foo ScriptObj_v) -> Tensor""",  # noqa: B950
        )
        self.assertExpectedInline(
            str(schema3),
            """test_op3(bool bool_v, int int_v, float float_v, str str_v, Tensor Tensor_v, SymInt SymInt_v, SymBool SymBool_v, GraphModule GraphModule_v, __torch__.torch.classes._Foo ScriptObj_v) -> (Tensor, Tensor)""",  # noqa: B950,
        )
        self.assertEqual(schema1.parse(str(schema1)), schema1)
        self.assertEqual(schema2.parse(str(schema2)), schema2)
        self.assertEqual(schema3.parse(str(schema3)), schema3)

    def test_while_loop_schema_gen(self):
        fn, inp = WHILE_LOOP_TESTS["simple_with_linear"]
        graph = make_fx(fn)(*inp).graph
        while_loop_node = next(
            node
            for node in graph.nodes
            if node.op == "call_function"
            and node.target is torch.ops.higher_order.while_loop
        )
        schema = torch._library.utils.hop_schema_from_fx_node(while_loop_node)
        self.assertExpectedInline(
            str(schema),
            """while_loop(GraphModule cond_fn, GraphModule body_fn, Tensor[2] carried_inputs, Tensor[3] additional_inputs) -> Tensor[2]""",  # noqa: B950
        )
        self.assertEqual(schema.parse(str(schema)), schema)


instantiate_parametrized_tests(TestHopSchema)
instantiate_parametrized_tests(TestControlFlowTraced)

instantiate_parametrized_tests(TestControlFlow)

if __name__ == "__main__":
    run_tests()
