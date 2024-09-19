# Owner(s): ["module: inductor"]

import os
import shutil
import tempfile

import torch
import torch._export
import torch._inductor
import torch.export._trace
import torch.fx._pytree as fx_pytree
from torch.testing._internal.common_utils import IS_FBCODE
from torch.utils import _pytree as pytree


class WrapperModule(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, *args, **kwargs):
        return self.model(*args, **kwargs)


class AOTIRunnerUtil:
    @staticmethod
    def compile(
        model,
        example_inputs,
        options=None,
        dynamic_shapes=None,
        disable_constraint_solver=False,
    ):
        if not isinstance(model, torch.nn.Module):
            model = WrapperModule(model)
        # The exact API is subject to change
        if torch._inductor.config.is_predispatch:
            ep = torch.export._trace._export(
                model, example_inputs, dynamic_shapes=dynamic_shapes, pre_dispatch=True
            )
            gm = ep.module()
        else:
            gm = torch.export._trace._export_to_torch_ir(
                model,
                example_inputs,
                dynamic_shapes=dynamic_shapes,
                disable_constraint_solver=disable_constraint_solver,
                # Disabling this flag, because instead we can rely on the mapping
                # dynamo_flat_name_to_original_fqn which is coming from Dynamo.
                restore_fqn=False,
            )

        if IS_FBCODE:
            from deeplearning.aot_inductor.extern_node_thrift_serializer import (
                thrift_serializer,
            )

            if options is None:
                options = {}
            options["extern_node_serializer"] = thrift_serializer

        with torch.no_grad():
            so_path = torch._inductor.aot_compile(gm, example_inputs, options=options)  # type: ignore[arg-type]

        return so_path

    @staticmethod
    def load_runner(device, so_path):
        if IS_FBCODE:
            from .fb import test_aot_inductor_model_runner_pybind  # @manual

            with tempfile.TemporaryDirectory() as temp_dir:
                # copy *.so file to a unique path just before loading
                # to avoid stale dlopen handles when an updated *.so
                # from the same path is loaded repetitively in a test
                temp_so_path = os.path.join(temp_dir, "model.so")
                shutil.copy(so_path, temp_so_path)

                # We also need to copy over the serialized extern_kernel_nodes for custom ops
                extern_kernel_nodes_path = f"{so_path[:-3]}.json"
                if os.path.isfile(extern_kernel_nodes_path):
                    temp_extern_kernel_nodes_path = os.path.join(temp_dir, "model.json")
                    shutil.copy(extern_kernel_nodes_path, temp_extern_kernel_nodes_path)

                return test_aot_inductor_model_runner_pybind.Runner(
                    temp_so_path, device == "cpu"
                )
        else:
            return (
                torch._C._aoti.AOTIModelContainerRunnerCpu(so_path, 1)
                if device == "cpu"
                else torch._C._aoti.AOTIModelContainerRunnerCuda(so_path, 1, device)
            )

    @staticmethod
    def load(device, so_path):
        # TODO: unify fbcode and oss behavior to only use torch._export.aot_load
        if IS_FBCODE:
            runner = AOTIRunnerUtil.load_runner(device, so_path)

            def optimized(*args, **kwargs):
                call_spec = runner.get_call_spec()
                in_spec = pytree.treespec_loads(call_spec[0])
                out_spec = pytree.treespec_loads(call_spec[1])
                flat_inputs = fx_pytree.tree_flatten_spec((args, kwargs), in_spec)
                flat_inputs = [x for x in flat_inputs if isinstance(x, torch.Tensor)]
                flat_outputs = runner.run(flat_inputs)
                return pytree.tree_unflatten(flat_outputs, out_spec)

            return optimized
        else:
            return torch._export.aot_load(so_path, device)

    @staticmethod
    def run(
        device,
        model,
        example_inputs,
        options=None,
        dynamic_shapes=None,
        disable_constraint_solver=False,
    ):
        so_path = AOTIRunnerUtil.compile(
            model,
            example_inputs,
            options=options,
            dynamic_shapes=dynamic_shapes,
            disable_constraint_solver=disable_constraint_solver,
        )
        optimized = AOTIRunnerUtil.load(device, so_path)
        return optimized(*example_inputs)

    @staticmethod
    def run_multiple(
        device,
        model,
        list_example_inputs,
        options=None,
        dynamic_shapes=None,
    ):
        so_path = AOTIRunnerUtil.compile(
            model,
            list_example_inputs[0],
            options=options,
            dynamic_shapes=dynamic_shapes,
        )
        optimized = AOTIRunnerUtil.load(device, so_path)
        list_output_tensors = []
        for example_inputs in list_example_inputs:
            list_output_tensors.append(optimized(*example_inputs))
        return list_output_tensors
