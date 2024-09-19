# mypy: allow-untyped-defs
from __future__ import annotations


__all__ = [
    "DiagnosticOptions",
    "ExportOptions",
    "ONNXProgram",
    "ONNXRuntimeOptions",
    "InvalidExportOptionsError",
    "OnnxRegistry",
    "UnsatisfiedDependencyError",
    "dynamo_export",
    "enable_fake_mode",
]


import abc
import contextlib
import dataclasses
import logging
import os
import tempfile
import warnings
from collections import defaultdict
from typing import Any, Callable, Final, Mapping, Sequence, TYPE_CHECKING, TypeVar
from typing_extensions import Self

import torch
import torch._ops
import torch.utils._pytree as pytree
from torch.onnx import errors
from torch.onnx._internal import io_adapter
from torch.onnx._internal.diagnostics import infra
from torch.onnx._internal.fx import (
    decomposition_table,
    patcher as patcher,
    registration,
    serialization as fx_serialization,
)


# We can only import onnx from this module in a type-checking context to ensure that
# 'import torch.onnx' continues to work without having 'onnx' installed. We fully
# 'import onnx' inside of dynamo_export (by way of _assert_dependencies).
if TYPE_CHECKING:
    import io

    import onnx

    import onnxruntime
    import onnxscript

    from torch._subclasses import fake_tensor
    from torch.onnx._internal.fx import diagnostics

_DEFAULT_OPSET_VERSION: Final[int] = 18
"""The default ONNX opset version the exporter will use if one is not specified explicitly
through :class:`ExportOptions`. This should NEVER be accessed outside of this module! Users
should reference :attr:`ExportOptions.opset_version`."""

_PYTORCH_GITHUB_ISSUES_URL = "https://github.com/pytorch/pytorch/issues"
"""The URL to the PyTorch GitHub issues page."""

_DEFAULT_FAILED_EXPORT_SARIF_LOG_PATH = "report_dynamo_export.sarif"
"""The default path to write the SARIF log to if the export fails."""

_PROTOBUF_SIZE_MAX_LIMIT = 2 * 1024 * 1024 * 1024
"""The maximum size of a Protobuf file in bytes. This is used to determine whether to
serialize the model with external data or not."""

log = logging.getLogger(__name__)


DiagnosticOptions = infra.DiagnosticOptions


@dataclasses.dataclass
class ONNXFakeContext:
    """A dataclass used to store context for model export using FakeTensor.

    This dataclass stores the FakeTensorMode instance used to convert
    real tensors and model parameters into fake tensors. This :attr:`ONNXFakeContext.fake_mode` is
    reused internally during tracing of a :class:`torch.nn.Module` into a FX :class:`GraphModule`.
    """

    fake_mode: fake_tensor.FakeTensorMode
    """The fake tensor mode used for tracing model using fake tensors and parameters."""

    state_dict_paths: tuple[str | io.BytesIO | dict[str, Any]] | None = None
    """List of paths of files that contain the model :meth:`state_dict`"""


class OnnxRegistry:
    """Registry for ONNX functions.

    The registry maintains a mapping from qualified names to symbolic functions under a
    fixed opset version. It supports registering custom onnx-script functions and for
    dispatcher to dispatch calls to the appropriate function.

    """

    def __init__(self) -> None:
        """Initializes the registry"""

        # NOTE: _registry is the registry maps OpNameto a list of ONNXFunctions. It is important
        # not to directly modify this variable. Instead, access to it should be done through
        # the public methods: register_custom_op, get_ops, and is_registered_op.
        self._registry: dict[registration.OpName, list[registration.ONNXFunction]] = (
            defaultdict(list)
        )

        # opset_version is unused for now, since torchlib only supports opset18.
        # TODO: get opset version from torchlib
        self._opset_version = _DEFAULT_OPSET_VERSION
        warnings.warn(
            f"torch.onnx.dynamo_export only implements opset version {self._opset_version} for now. If you need to use a "
            "different opset version, please register them with register_custom_op."
        )

        self._initiate_registry_from_torchlib()

    @property
    def opset_version(self) -> int:
        """The ONNX opset version the exporter should target. Defaults to the latest
        supported ONNX opset version: 18. The default version will increment over time as
        ONNX continues to evolve."""

        return self._opset_version

    def _initiate_registry_from_torchlib(self) -> None:
        """Populates the registry with ATen functions from torchlib.

        Args:
            torchlib_registry: The torchlib registry to use for populating the registry.
        """
        import onnxscript._framework_apis.torch_2_5 as onnxscript_apis

        for meta in onnxscript_apis.get_torchlib_ops():
            internal_name_instance = registration.OpName.from_qualified_name(
                meta.qualified_name
            )
            symbolic_function = registration.ONNXFunction(
                onnx_function=meta.function,  # type: ignore[arg-type]
                op_full_name=internal_name_instance.qualified_name(),
                is_custom=False,
                is_complex=meta.is_complex,
            )
            self._register(internal_name_instance, symbolic_function)

    def _register(
        self,
        internal_qualified_name: registration.OpName,
        symbolic_function: registration.ONNXFunction,
    ) -> None:
        """Registers a ONNXFunction to an operator.

        Args:
            internal_qualified_name: The qualified name of the operator to register: OpName.
            symbolic_function: The ONNXFunction to register.
        """
        self._registry[internal_qualified_name].append(symbolic_function)

    def register_op(
        self,
        function: onnxscript.OnnxFunction | onnxscript.TracedOnnxFunction,
        namespace: str,
        op_name: str,
        overload: str | None = None,
        is_complex: bool = False,
    ) -> None:
        """Registers a custom operator: torch.ops.<namespace>.<op_name>.<overload>.

        Args:
            function: The onnx-sctip function to register.
            namespace: The namespace of the operator to register.
            op_name: The name of the operator to register.
            overload: The overload of the operator to register. If it's default overload,
                leave it to None.
            is_complex: Whether the function is a function that handles complex valued inputs.

        Raises:
            ValueError: If the name is not in the form of 'namespace::op'.
        """
        internal_name_instance = registration.OpName.from_name_parts(
            namespace=namespace, op_name=op_name, overload=overload
        )
        symbolic_function = registration.ONNXFunction(
            onnx_function=function,
            op_full_name=internal_name_instance.qualified_name(),
            is_custom=True,
            is_complex=is_complex,
        )
        self._register(internal_name_instance, symbolic_function)

    def get_op_functions(
        self, namespace: str, op_name: str, overload: str | None = None
    ) -> list[registration.ONNXFunction] | None:
        """Returns a list of ONNXFunctions for the given op: torch.ops.<namespace>.<op_name>.<overload>.

        The list is ordered by the time of registration. The custom operators should be
        in the second half of the list.

        Args:
            namespace: The namespace of the operator to get.
            op_name: The name of the operator to get.
            overload: The overload of the operator to get. If it's default overload,
                leave it to None.
        Returns:
            A list of ONNXFunctions corresponding to the given name, or None if
            the name is not in the registry.
        """
        internal_name_instance = registration.OpName.from_name_parts(
            namespace=namespace, op_name=op_name, overload=overload
        )
        return self._registry.get(internal_name_instance)

    def is_registered_op(
        self, namespace: str, op_name: str, overload: str | None = None
    ) -> bool:
        """Returns whether the given op is registered: torch.ops.<namespace>.<op_name>.<overload>.

        Args:
            namespace: The namespace of the operator to check.
            op_name: The name of the operator to check.
            overload: The overload of the operator to check. If it's default overload,
                leave it to None.

        Returns:
            True if the given op is registered, otherwise False.
        """
        functions = self.get_op_functions(
            namespace=namespace, op_name=op_name, overload=overload
        )
        return functions is not None

    def _all_registered_ops(self) -> set[str]:
        """Returns the set of all registered function names."""
        return {
            op_name_class.qualified_name() for op_name_class in self._registry.keys()
        }


class ExportOptions:
    """Options to influence the TorchDynamo ONNX exporter.

    Attributes:
        dynamic_shapes: Shape information hint for input/output tensors.
            When ``None``, the exporter determines the most compatible setting.
            When ``True``, all input shapes are considered dynamic.
            When ``False``, all input shapes are considered static.
        diagnostic_options: The diagnostic options for the exporter.
        fake_context: The fake context used for symbolic tracing.
        onnx_registry: The ONNX registry used to register ATen operators to ONNX functions.
    """

    dynamic_shapes: bool | None = None
    """Shape information hint for input/output tensors.

    - ``None``: the exporter determines the most compatible setting.
    - ``True``: all input shapes are considered dynamic.
    - ``False``: all input shapes are considered static.
    """

    diagnostic_options: DiagnosticOptions
    """The diagnostic options for the exporter."""

    fake_context: ONNXFakeContext | None = None
    """The fake context used for symbolic tracing."""

    onnx_registry: OnnxRegistry | None = None
    """The ONNX registry used to register ATen operators to ONNX functions."""

    def __init__(
        self,
        *,
        dynamic_shapes: bool | None = None,
        fake_context: ONNXFakeContext | None = None,
        onnx_registry: OnnxRegistry | None = None,
        diagnostic_options: DiagnosticOptions | None = None,
    ):
        self.dynamic_shapes = dynamic_shapes
        self.fake_context = fake_context
        self.onnx_registry = onnx_registry
        self.diagnostic_options = diagnostic_options or DiagnosticOptions()


class ResolvedExportOptions(ExportOptions):
    """Consolidates :class:`ExportOptions` with default values.
    All unspecified options from :class:`ExportOptions` are assigned a default value.
    This is an internal class and its API may be changed at any time without notice.
    """

    # Public attributes MUST be redefined below without ``Optional[]`` from ``ExportOptions``
    dynamic_shapes: bool
    diagnostic_options: DiagnosticOptions
    fake_context: ONNXFakeContext
    onnx_registry: OnnxRegistry

    # Private only attributes
    decomposition_table: dict[torch._ops.OpOverload, Callable]
    """A dictionary that maps operators to their decomposition functions."""

    onnxfunction_dispatcher: (
        torch.onnx._internal.fx.onnxfunction_dispatcher.OnnxFunctionDispatcher
    )
    """The ONNX dispatcher used to dispatch ATen operators to ONNX functions."""

    fx_tracer: FXGraphExtractor
    """The FXGraphExtractor instance used to extract the FX graph from the model."""

    diagnostic_context: diagnostics.DiagnosticContext
    """The diagnostics context for the export. Responsible for recording diagnostics,
    logging diagnostics, and generating the SARIF log."""

    def __init__(
        self,
        options: ExportOptions | ResolvedExportOptions,
        model: torch.nn.Module | Callable | None = None,  # type: ignore[name-defined]
    ):
        from torch.onnx._internal.fx import (  # TODO: Prevent circular dep
            diagnostics,
            dynamo_graph_extractor,
        )

        if isinstance(options, ResolvedExportOptions):
            self.dynamic_shapes = options.dynamic_shapes
            self.diagnostic_options = options.diagnostic_options
            self.fake_context = options.fake_context
            self.fx_tracer = options.fx_tracer
            self.onnx_registry = options.onnx_registry
            self.onnxfunction_dispatcher = options.onnxfunction_dispatcher
            self.decomposition_table = options.decomposition_table
            self.diagnostic_context = options.diagnostic_context
        else:
            T = TypeVar("T")

            def resolve(value: T | None, fallback: T | Callable[[], T]) -> T:
                if value is not None:
                    return value
                if callable(fallback):
                    return fallback()
                return fallback

            self.dynamic_shapes = resolve(options.dynamic_shapes, False)

            self.diagnostic_options = resolve(
                options.diagnostic_options, DiagnosticOptions()
            )

            self.fx_tracer = dynamo_graph_extractor.DynamoExport()

            self.fake_context = resolve(options.fake_context, None)  # type: ignore[arg-type]
            self.diagnostic_context = diagnostics.DiagnosticContext(
                "torch.onnx.dynamo_export",
                torch.__version__,
                self.diagnostic_options,
            )

            self.onnx_registry = resolve(options.onnx_registry, OnnxRegistry())
            self.decomposition_table = (
                decomposition_table.create_onnx_friendly_decomposition_table(  # type: ignore[assignment]
                    self.onnx_registry
                )
            )

            from torch.onnx._internal.fx import onnxfunction_dispatcher

            self.onnxfunction_dispatcher = (
                onnxfunction_dispatcher.OnnxFunctionDispatcher(
                    self.onnx_registry,
                    self.diagnostic_context,
                )
            )

            for key in dir(options):
                if not key.startswith("_"):  # skip private attributes
                    assert hasattr(self, key), f"Unresolved option '{key}'"


@contextlib.contextmanager
def enable_fake_mode():
    """Enable fake mode for the duration of the context.

    Internally it instantiates a :class:`torch._subclasses.fake_tensor.FakeTensorMode` context manager
    that converts user input and model parameters into :class:`torch._subclasses.fake_tensor.FakeTensor`.

    A :class:`torch._subclasses.fake_tensor.FakeTensor`
    is a :class:`torch.Tensor` with the ability to run PyTorch code without having to
    actually do computation through tensors allocated on a ``meta`` device. Because
    there is no actual data being allocated on the device, this API allows for
    exporting large models without the actual memory footprint needed for executing it.

    It is highly recommended to enable fake mode when exporting models that
    are too large to fit into memory.

    Returns:
        A :class:`ONNXFakeContext` object.

    Example::

        # xdoctest: +REQUIRES(env:TORCH_DOCTEST_ONNX)
        >>> import torch
        >>> import torch.onnx
        >>> class MyModel(torch.nn.Module):  # Dummy model
        ...     def __init__(self) -> None:
        ...         super().__init__()
        ...         self.linear = torch.nn.Linear(2, 2)
        ...     def forward(self, x):
        ...         out = self.linear(x)
        ...         return out
        >>> with torch.onnx.enable_fake_mode() as fake_context:
        ...     my_nn_module = MyModel()
        ...     arg1 = torch.randn(2, 2, 2)  # positional input 1
        >>> export_options = torch.onnx.ExportOptions(fake_context=fake_context)
        >>> onnx_program = torch.onnx.export(my_nn_module, (arg1,), dynamo=True)
        >>> onnx_program.apply_weights(MyModel().state_dict())
        >>> # Saving model WITHOUT initializers
        >>> onnx_program.save(
        ...     "my_model_without_initializers.onnx",
        ...     include_initializers=False,
        ...     keep_initializers_as_inputs=True,
        ... )
        >>> # Saving model WITH initializers
        >>> onnx_program.save("my_model_with_initializers.onnx")

    .. warning::
        This API is experimental and is *NOT* backward-compatible.

    """
    from torch._subclasses import fake_tensor
    from torch.fx.experimental.symbolic_shapes import ShapeEnv

    # This overrides the internal `FakeTensorMode` instance created by `torch._dynamo.export`[1].
    # It is a good idea to keep them in sync (constructor args) to maintain the same default behavior
    # [1] `torch/_dynamo/output_graph.py::InstructionTranslator::OutputGraph.__init__`
    # Mixed fake/real tensors are only allowed when `torch.onnx.dynamo_export` is not called within `FakeTensorMode`
    # This is needed because models can create new parameters during `forward(self, *args, **kwargs)` run
    fake_mode = fake_tensor.FakeTensorMode(
        allow_non_fake_inputs=not torch._guards.detect_fake_mode(),
        shape_env=ShapeEnv(
            allow_scalar_outputs=False, allow_dynamic_output_shape_ops=False
        ),
    )
    # The patcher is needed for when user calls `fake_model.load_state_dict(...)` within fake mode
    patcher_context = patcher.ONNXTorchPatcher()
    fake_context = ONNXFakeContext(fake_mode=fake_mode)
    with fake_mode, patcher_context:
        yield fake_context
    fake_context.state_dict_paths = tuple(
        patcher_context.paths,
    )  # type: ignore[assignment]


class ONNXRuntimeOptions:
    """Options to influence the execution of the ONNX model through ONNX Runtime.

    Attributes:
        session_options: ONNX Runtime session options.
        execution_providers: ONNX Runtime execution providers to use during model execution.
        execution_provider_options: ONNX Runtime execution provider options.
    """

    session_options: Sequence[onnxruntime.SessionOptions] | None = None
    """ONNX Runtime session options."""

    execution_providers: Sequence[str | tuple[str, dict[Any, Any]]] | None = None
    """ONNX Runtime execution providers to use during model execution."""

    execution_provider_options: Sequence[dict[Any, Any]] | None = None
    """ONNX Runtime execution provider options."""

    def __init__(
        self,
        *,
        session_options: Sequence[onnxruntime.SessionOptions] | None = None,
        execution_providers: Sequence[str | tuple[str, dict[Any, Any]]] | None = None,
        execution_provider_options: Sequence[dict[Any, Any]] | None = None,
    ):
        self.session_options = session_options
        self.execution_providers = execution_providers
        self.execution_provider_options = execution_provider_options


class ONNXProgram:
    """An in-memory representation of a PyTorch model that has been exported to ONNX.

    Args:
        model_proto: The exported ONNX model as an :py:obj:`onnx.ModelProto`.
        input_adapter: The input adapter used to convert PyTorch inputs into ONNX inputs.
        output_adapter: The output adapter used to convert PyTorch outputs into ONNX outputs.
        diagnostic_context: Context object for the SARIF diagnostic system responsible for logging errors and metadata.
        fake_context: The fake context used for symbolic tracing.
        export_exception: The exception that occurred during export, if any.
    """

    _model_proto: Final[onnx.ModelProto]  # type: ignore[name-defined, misc]
    _input_adapter: Final[io_adapter.InputAdapter]  # type: ignore[misc]
    _output_adapter: Final[io_adapter.OutputAdapter]  # type: ignore[misc]
    _diagnostic_context: Final[diagnostics.DiagnosticContext]  # type: ignore[misc]
    _fake_context: Final[ONNXFakeContext | None]  # type: ignore[misc]
    _export_exception: Final[Exception | None]  # type: ignore[misc]
    _model_torch: Final[  # type: ignore[misc]
        torch.nn.Module | Callable | None
    ]

    def __init__(
        self,
        model_proto: onnx.ModelProto,  # type: ignore[name-defined]
        input_adapter: io_adapter.InputAdapter,
        output_adapter: io_adapter.OutputAdapter,
        diagnostic_context: diagnostics.DiagnosticContext,
        *,
        fake_context: ONNXFakeContext | None = None,
        export_exception: Exception | None = None,
        model_torch: torch.nn.Module | Callable | None = None,
    ):
        self._model_proto = model_proto
        self._model_torch = model_torch
        self._input_adapter = input_adapter
        self._output_adapter = output_adapter
        self._diagnostic_context = diagnostic_context
        self._fake_context = fake_context
        self._export_exception = export_exception
        self._state_dict: dict[str, torch.Tensor] = {}

    def __call__(
        self,
        *args: Any,
        model_with_state_dict: torch.nn.Module | Callable | None = None,
        options: ONNXRuntimeOptions | None = None,
        **kwargs: Any,
    ) -> Any:
        """Runs the ONNX model using ONNX Runtime

        Args:
            args: The positional inputs to the model.
            kwargs: The keyword inputs to the model.
            model_with_state_dict: The PyTorch model to fetch state from.
                Required when :func:`enable_fake_mode` is used to extract real initializers as needed by the ONNX graph.
            options: The options to use for running the model with ONNX Runtime.

        Returns:
            The model output as computed by ONNX Runtime
        """

        # TODO: If ONNX used absolute paths on the initializers external data files,
        # users could call ONNXProgram.save and use ONNXProgram.__call__ without the internal save below
        with contextlib.ExitStack() as stack:
            # model specified by the user has precedence, when specified
            model_with_state_dict = model_with_state_dict or self._model_torch

            if self.fake_context:
                tmpdir_path = stack.enter_context(tempfile.TemporaryDirectory())
                warnings.warn(
                    "Cannot run model directly from `ONNXProgram` because"
                    " the model was exported using `enable_fake_mode`."
                    " The model will be serialized to disk using a temporary folder ({tmpdir_path})"
                    " to populate the model with initializers before being execution."
                )
                # TODO: Revisit the need of `model_with_state_dict` being a real model and not just its state
                onnx_model = os.path.join(tmpdir_path, "model.onnx")
                if isinstance(model_with_state_dict, torch.nn.Module):
                    model_state = model_with_state_dict.state_dict()
                else:
                    model_state = self._state_dict
                self.save(
                    onnx_model,
                    model_state=model_state,
                )
            else:
                onnx_model = self.model_proto.SerializeToString()  # type: ignore[assignment]

            import onnxruntime  # type: ignore[import]

            onnx_input = self.adapt_torch_inputs_to_onnx(
                *args, model_with_state_dict=model_with_state_dict, **kwargs
            )
            options = options or ONNXRuntimeOptions()
            providers = (
                options.execution_providers or onnxruntime.get_available_providers()
            )
            ort_session = onnxruntime.InferenceSession(onnx_model, providers=providers)

            onnxruntime_input = {
                k.name: v.numpy(force=True)  # type: ignore[union-attr]
                for k, v in zip(ort_session.get_inputs(), onnx_input)
            }

            return ort_session.run(None, onnxruntime_input)

    @property
    def model_proto(self) -> onnx.ModelProto:  # type: ignore[name-defined]
        """The exported ONNX model as an :py:obj:`onnx.ModelProto`."""

        if self._export_exception is not None:
            raise self._export_exception
        return self._model_proto

    @property
    def diagnostic_context(self) -> diagnostics.DiagnosticContext:
        """The diagnostic context associated with the export."""

        return self._diagnostic_context

    @property
    def fake_context(self) -> ONNXFakeContext | None:
        """The fake context associated with the export."""

        return self._fake_context

    def adapt_torch_inputs_to_onnx(
        self,
        *model_args,
        model_with_state_dict: torch.nn.Module | Callable | None = None,
        **model_kwargs,
    ) -> Sequence[torch.Tensor | int | float | bool | torch.dtype]:
        """Converts the PyTorch model inputs to exported ONNX model inputs format.

        Due to design differences, input/output format between PyTorch model and exported
        ONNX model are often not the same. E.g., None is allowed for PyTorch model, but are
        not supported by ONNX. Nested constructs of tensors are allowed for PyTorch model,
        but only flattened tensors are supported by ONNX, etc.

        The actual adapting steps are associated with each individual export. It
        depends on the PyTorch model, the particular set of model_args and model_kwargs
        used for the export, and export options.

        This method replays the adapting steps recorded during export.

        Args:
            model_args: The PyTorch model inputs.
            model_with_state_dict: The PyTorch model to get extra state from.
                If not specified, the model used during export is used.
                Required when :func:`enable_fake_mode` is used to extract real initializers as needed by the ONNX graph.
            model_kwargs: The PyTorch model keyword inputs.

        Returns:
            A sequence of tensors converted from PyTorch model inputs.

        Example::

            # xdoctest: +REQUIRES(env:TORCH_DOCTEST_ONNX)
            >>> import torch
            >>> import torch.onnx
            >>> from typing import Dict, Tuple
            >>> def func_nested_input(
            ...     x_dict: Dict[str, torch.Tensor],
            ...     y_tuple: Tuple[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]
            ... ):
            ...     if "a" in x_dict:
            ...         x = x_dict["a"]
            ...     elif "b" in x_dict:
            ...         x = x_dict["b"]
            ...     else:
            ...         x = torch.randn(3)
            ...
            ...     y1, (y2, y3) = y_tuple
            ...
            ...     return x + y1 + y2 + y3
            >>> x_dict = {"a": torch.tensor(1.)}
            >>> y_tuple = (torch.tensor(2.), (torch.tensor(3.), torch.tensor(4.)))
            >>> onnx_program = torch.onnx.dynamo_export(func_nested_input, x_dict, y_tuple)
            >>> print(x_dict, y_tuple)
            {'a': tensor(1.)} (tensor(2.), (tensor(3.), tensor(4.)))
            >>> print(onnx_program.adapt_torch_inputs_to_onnx(x_dict, y_tuple, model_with_state_dict=func_nested_input))
            (tensor(1.), tensor(2.), tensor(3.), tensor(4.))

        .. warning::
            This API is experimental and is *NOT* backward-compatible.

        """
        # model specified by the user has precedence, when specified
        model_with_state_dict = model_with_state_dict or self._model_torch
        assert (
            model_with_state_dict is not None
        ), "model_with_state_dict must be specified."
        return self._input_adapter.apply(  # type: ignore[return-value]
            *model_args, model=model_with_state_dict, **model_kwargs
        )

    def adapt_torch_outputs_to_onnx(
        self,
        model_outputs: Any,
        model_with_state_dict: torch.nn.Module | Callable | None = None,
    ) -> Sequence[torch.Tensor | int | float | bool]:
        """Converts the PyTorch model outputs to exported ONNX model outputs format.

        Due to design differences, input/output format between PyTorch model and exported
        ONNX model are often not the same. E.g., None is allowed for PyTorch model, but are
        not supported by ONNX. Nested constructs of tensors are allowed for PyTorch model,
        but only flattened tensors are supported by ONNX, etc.

        The actual adapting steps are associated with each individual export. It
        depends on the PyTorch model, the particular set of model_args and model_kwargs
        used for the export, and export options.

        This method replays the adapting steps recorded during export.

        Args:
            model_outputs: The PyTorch model outputs.
            model_with_state_dict: The PyTorch model to get extra state from.
                If not specified, the model used during export is used.
                Required when :func:`enable_fake_mode` is used to extract real initializers as needed by the ONNX graph.

        Returns:
            PyTorch model outputs in exported ONNX model outputs format.

        Example::

            # xdoctest: +REQUIRES(env:TORCH_DOCTEST_ONNX)
            >>> import torch
            >>> import torch.onnx
            >>> def func_returning_tuples(x, y, z):
            ...     x = x + y
            ...     y = y + z
            ...     z = x + y
            ...     return (x, (y, z))
            >>> x = torch.tensor(1.)
            >>> y = torch.tensor(2.)
            >>> z = torch.tensor(3.)
            >>> onnx_program = torch.onnx.dynamo_export(func_returning_tuples, x, y, z)
            >>> pt_output = func_returning_tuples(x, y, z)
            >>> print(pt_output)
            (tensor(3.), (tensor(5.), tensor(8.)))
            >>> print(onnx_program.adapt_torch_outputs_to_onnx(pt_output, model_with_state_dict=func_returning_tuples))
            [tensor(3.), tensor(5.), tensor(8.)]

        .. warning::
            This API is experimental and is *NOT* backward-compatible.

        """
        # model specified by the user has precedence, when specified
        model_with_state_dict = model_with_state_dict or self._model_torch
        assert (
            model_with_state_dict is not None
        ), "model_with_state_dict must be specified."
        return self._output_adapter.apply(model_outputs, model=model_with_state_dict)  # type: ignore[return-value]

    def apply_weights(self, state_dict: dict[str, torch.Tensor]) -> None:
        """Apply the weights from the specified state dict to the ONNX model.
        Args:
            state_dict: The state dict containing the weights to apply to the ONNX model.
        """
        self._state_dict = state_dict

    def save(
        self,
        destination: str | io.BufferedIOBase,
        *,
        include_initializers: bool = True,
        model_state: dict[str, Any] | str | None = None,
    ) -> None:
        """Saves the in-memory ONNX model to ``destination`` using specified ``serializer``.

        Args:
            destination: The destination to save the ONNX model. It can be either a string or a file-like object.
                When used with ``model_state``, it must be a string with a full path to the destination.
                If `destination` is a string, besides saving the ONNX model into a file, model weights are also stored
                in separate files in the same directory as the ONNX model. E.g. for `destination="/path/model.onnx"`,
                the initializers are saved in "/path/" folder along with "onnx.model".
            include_initializers: Whether to include initializers in the ONNX graph as external data.
                Cannot be combined with `model_state_dict`.
            model_state: The state_dict of the PyTorch model containing all weights on it.
                It can be either a string with the path to a checkpoint or a dictionary with the actual model state.
                The supported file formats are the same as those supported by `torch.load` and `safetensors.safe_open`.
                Required when :func:`enable_fake_mode` is used but real initializers are needed on the ONNX graph.
        """
        import onnx

        assert (
            include_initializers is True or model_state is None
        ), "Cannot specify both `include_initializers=False` and `model_state`."

        if self._state_dict and model_state is None:
            model_state = self._state_dict

        # Add initializers when symbolic tracing is enabled
        _model_state_files: list[str | io.BytesIO | dict[str, Any]] = []
        if include_initializers:
            if model_state is not None:
                assert isinstance(
                    model_state, (dict, str)
                ), "model_state must be a path to the model's state_dict or the actual state_dict"
                # NOTE: For dict, there can be performance penalty or high memory usage that might lead to OOM
                #       if the dict wasn't loaded with torch.load(..., mmap=True, map_location="cpu")
                _model_state_files.append(model_state)
            elif self._fake_context and self._fake_context.state_dict_paths:
                # Load state from previous model.load_state_dict() call within enable_fake_mode() context
                for path in self._fake_context.state_dict_paths:
                    if path in _model_state_files:
                        # ignore duplicate
                        continue
                    if os.path.exists(path):  # type: ignore[arg-type]
                        _model_state_files.append(path)
        else:
            # self.model_proto.graph.initializer.clear() not available in older protobuf versions
            initializer_count = len(self.model_proto.graph.initializer)
            for _ in range(initializer_count):
                del self.model_proto.graph.initializer[0]

        if _model_state_files:
            if not isinstance(destination, str):
                raise RuntimeError(
                    "`destination` must be a string with a path when `model_state` is specified."
                )
            destination_path, destination_filename = os.path.split(destination)
            destination_path = destination_path or os.getcwd()
            onnx_model_location = destination_filename

            # TODO: Should this be part of the serializer?
            fx_serialization.save_model_with_external_data(
                destination_path,
                onnx_model_location,
                "",  # When initializers >2GB, must be in the same folder as the model
                tuple(_model_state_files),
                self.model_proto,
            )
        else:
            if isinstance(destination, str):
                with open(destination, "wb") as f:
                    if self.model_proto.ByteSize() < _PROTOBUF_SIZE_MAX_LIMIT:
                        onnx.save_model(self.model_proto, destination)  # type: ignore[attr-defined]
                    else:
                        # ValueError: Message onnx.ModelProto exceeds maximum protobuf size of 2GB
                        # Fallback to serializing the model with external data.
                        onnx.save_model(  # type: ignore[attr-defined]
                            self.model_proto,
                            destination,
                            save_as_external_data=True,
                            all_tensors_to_one_file=True,
                        )
            else:
                try:
                    destination.write(self.model_proto.SerializeToString())
                except ValueError as exc:
                    raise ValueError(
                        "'destination' should be provided as a path-like string when saving a model larger than 2GB. "
                        "External tensor data will be saved alongside the model on disk."
                    ) from exc

    def save_diagnostics(self, destination: str) -> None:
        """Saves the export diagnostics as a SARIF log to the specified destination path.

        Args:
            destination: The destination to save the diagnostics SARIF log.
                It must have a `.sarif` extension.

        Raises:
            ValueError: If the destination path does not end with `.sarif` extension.
        """
        if not destination.endswith(".sarif"):
            message = f"'destination' must have a .sarif extension, got {destination}"
            log.fatal(message)
            raise ValueError(message)

        self.diagnostic_context.dump(destination)

    @classmethod
    def _from_failure(
        cls,
        export_exception: Exception,
        diagnostic_context: diagnostics.DiagnosticContext,
    ) -> Self:
        """
        Creates an instance of :class:`ONNXProgram` when the export process encounters a failure.

        In case of a failed export, this method is used to encapsulate the exception
        and associated diagnostic context within an :class:`ONNXProgram` instance for
        easier handling and debugging.

        Args:
            export_exception: The exception raised during the export process.
            diagnostic_context: The context associated with diagnostics during export.

        Returns:
            An instance of :class:`ONNXProgram` representing the failed ONNX program.
        """
        # Defer `import onnx` out of `import torch` path
        # https://github.com/pytorch/pytorch/issues/103764
        import onnx

        return cls(
            onnx.ModelProto(),  # type: ignore[attr-defined]
            io_adapter.InputAdapter(),
            io_adapter.OutputAdapter(),
            diagnostic_context,
            export_exception=export_exception,
        )


class FXGraphExtractor(abc.ABC):
    """Abstract interface for FX graph extractor engines.
    This class isolates FX extraction logic from the rest of the export logic.
    That allows a single ONNX exporter that can leverage different FX graphs."""

    def __init__(self) -> None:
        super().__init__()
        self.input_adapter: io_adapter.InputAdapter = io_adapter.InputAdapter()
        self.output_adapter: io_adapter.OutputAdapter = io_adapter.OutputAdapter()

    @abc.abstractmethod
    def generate_fx(
        self,
        options: ResolvedExportOptions,
        model: torch.nn.Module | Callable,
        model_args: Sequence[Any],
        model_kwargs: Mapping[str, Any],
    ) -> torch.fx.GraphModule:
        """Analyzes user ``model`` and generates a FX graph.
        Args:
            options: The export options.
            model: The user model.
            model_args: The model's positional input arguments.
            model_kwargs: The model's keyword input arguments.
        Returns:
            The generated FX Graph.
        """
        ...

    # TODO: Design the passes API
    @abc.abstractmethod
    def pre_export_passes(
        self,
        options: ResolvedExportOptions,
        original_model: torch.nn.Module | Callable,
        fx_module: torch.fx.GraphModule,
        fx_module_args: Sequence[Any],
    ):
        """Applies pre-export passes to the FX graph.

        Pre-export passes are FX-to-FX graph transformations that make the graph
        more palatable for the FX-to-ONNX conversion.
        For example, it can be used to flatten model input/output, add explicit
        casts to the graph, replace/decompose operators, functionalize the graph, etc.
        """
        ...


class Exporter:
    def __init__(
        self,
        options: ResolvedExportOptions,
        model: torch.nn.Module | Callable,
        model_args: Sequence[Any],
        model_kwargs: Mapping[str, Any],
    ):
        self.options = options
        assert self.options is not None

        self.model = model
        self.model_args = model_args
        self.model_kwargs = model_kwargs

        # TODO: https://github.com/pytorch/pytorch/issues/107714
        # NOTE: FXSymbolicTracer would fail in this assert, as it does not use `enable_fake_mode`
        from torch.onnx._internal.fx import fx_symbolic_graph_extractor

        if not isinstance(
            self.options.fx_tracer, fx_symbolic_graph_extractor.FXSymbolicTracer
        ):
            self._assert_fake_tensor_mode()

    def export(self) -> ONNXProgram:
        from torch.export._trace import (  # TODO: Prevent circular dependency
            DEFAULT_EXPORT_DYNAMO_CONFIG,
        )

        # TODO: Defer `import onnxscript` out of `import torch` path
        # https://github.com/pytorch/pytorch/issues/103764
        from torch.onnx._internal.fx import decomposition_skip

        with self.options.diagnostic_context, decomposition_skip.enable_decomposition_skips(
            self.options
        ), torch._dynamo.config.patch(dataclasses.asdict(DEFAULT_EXPORT_DYNAMO_CONFIG)):
            graph_module = self.options.fx_tracer.generate_fx(
                self.options, self.model, self.model_args, self.model_kwargs
            )
            # TODO: Defer `import onnxscript` out of `import torch` path
            # https://github.com/pytorch/pytorch/issues/103764
            from torch.onnx._internal.fx import fx_onnx_interpreter

            fx_interpreter = fx_onnx_interpreter.FxOnnxInterpreter(
                diagnostic_context=self.options.diagnostic_context
            )
            onnxscript_graph = fx_interpreter.run(
                fx_graph_module=graph_module,
                onnxfunction_dispatcher=self.options.onnxfunction_dispatcher,
            )

            # NOTE: Filter out the initializers with fake tensors when it's fake_mode exporting.
            # Otherwise, the ONNX exporter will fail: RuntimeError: basic_string::_M_construct null
            # not valid.
            # Concrete data is expected to be filled for those initializers later during `ONNXProgram.save`.
            if self.options.fake_context is not None:
                initializers_with_real_tensors: dict[str, torch.Tensor] = {}
                for (
                    initializer_name,
                    initializer,
                ) in onnxscript_graph.initializers.items():
                    if not isinstance(initializer, torch._subclasses.FakeTensor):
                        initializers_with_real_tensors[initializer_name] = initializer
                onnxscript_graph.initializers = initializers_with_real_tensors

            # Export TorchScript graph to ONNX ModelProto.
            onnx_model = onnxscript_graph.to_model_proto(
                self.options.onnx_registry.opset_version,
            )

            try:
                from onnxscript import optimizer

                onnx_model = optimizer.optimize(onnx_model)
            except ImportError:
                warnings.warn(
                    "ONNXScript optimizer is not available. Skipping optimization. "
                    "Please `pip install onnxscript -U` to enable post-export optimization."
                )
            except Exception as e:
                warnings.warn(
                    "ONNXScript optimizer failed. Skipping optimization. "
                    "\n\nPLEASE REPORT A BUG AT https://github.com/microsoft/onnxscript/issues "
                    f"\n\nDetail:\n{e}"
                )

            return torch.onnx.ONNXProgram(
                onnx_model,
                self.options.fx_tracer.input_adapter,
                self.options.fx_tracer.output_adapter,
                self.options.diagnostic_context,
                fake_context=self.options.fake_context,
                model_torch=self.model,
            )

    def _assert_fake_tensor_mode(self):
        """Asserts that the model and its input do not contain fake tensors."""

        # Case 1: Model with fake inputs/weights and without enabling fake mode
        has_any_fake_tensor = pytree.tree_any(
            lambda x: isinstance(x, torch._subclasses.FakeTensor),
            (self.model_args, self.model_kwargs),
        )
        has_any_fake_param_or_buffer = False
        if isinstance(self.model, torch.nn.Module):
            has_any_fake_param_or_buffer = pytree.tree_any(
                lambda x: isinstance(x, torch._subclasses.FakeTensor),
                (self.model.parameters(), self.model.buffers()),
            )
        if (
            has_any_fake_tensor or has_any_fake_param_or_buffer
        ) and not self.options.fake_context:
            raise RuntimeError(
                "Cannot export a model with fake inputs/weights without enabling fake mode.",
            )
        # Case 2: Model with non fake inputs/weights and enabled fake mode
        has_any_non_fake_tensors = pytree.tree_any(
            lambda x: isinstance(x, torch.Tensor)
            and not isinstance(x, torch._subclasses.FakeTensor),
            (self.model_args, self.model_kwargs),
        )
        has_any_non_fake_param_or_buffer = False
        if isinstance(self.model, torch.nn.Module):
            has_any_non_fake_param_or_buffer = pytree.tree_any(
                lambda x: isinstance(x, torch.Tensor)
                and not isinstance(x, torch._subclasses.FakeTensor),
                (self.model.parameters(), self.model.buffers()),
            )
        if (
            has_any_non_fake_tensors or has_any_non_fake_param_or_buffer
        ) and self.options.fake_context:
            raise RuntimeError(
                "Cannot export a model with non fake inputs/weights and enabled fake mode.",
            )


class UnsatisfiedDependencyError(RuntimeError):
    """Raised when an ONNX exporter dependency cannot be satisfied."""

    def __init__(self, package_name: str, message: str):
        super().__init__(message)
        self.package_name = package_name


class InvalidExportOptionsError(RuntimeError):
    """Raised when user specified an invalid value for the :class:`ExportOptions`."""


def _assert_dependencies(export_options: ResolvedExportOptions):
    opset_version = export_options.onnx_registry.opset_version

    def missing_package(package_name: str, exc_info: logging._ExcInfoType):
        message = (
            f"Please install the `{package_name}` package "
            f"(e.g. `python -m pip install {package_name}`)."
        )
        log.fatal(message, exc_info=exc_info)
        return UnsatisfiedDependencyError(package_name, message)

    def missing_opset(package_name: str):
        message = (
            f"The installed `{package_name}` does not support the specified ONNX opset "
            f"version {opset_version}. Install a newer `{package_name}` package or "
            f"specify an older opset version."
        )
        log.fatal(message)
        return UnsatisfiedDependencyError(package_name, message)

    try:
        import onnx
    except ImportError as e:
        raise missing_package("onnx", e) from e

    if onnx.defs.onnx_opset_version() < opset_version:
        raise missing_opset("onnx")

    try:
        # PyTorch runs lintrunner in CI without onnxscript installed
        import onnxscript  # type: ignore[import]
    except ImportError as e:
        raise missing_package("onnxscript", e) from e

    if not isinstance(
        onnxscript.onnx_opset.all_opsets[("", opset_version)],
        onnxscript.values.Opset,
    ):
        raise missing_opset("onnxscript")


def dynamo_export(
    model: torch.nn.Module | Callable,
    /,
    *model_args,
    export_options: ExportOptions | None = None,
    **model_kwargs,
) -> ONNXProgram | Any:
    """Export a torch.nn.Module to an ONNX graph.

    Args:
        model: The PyTorch model to be exported to ONNX.
        model_args: Positional inputs to ``model``.
        model_kwargs: Keyword inputs to ``model``.
        export_options: Options to influence the export to ONNX.

    Returns:
        An in-memory representation of the exported ONNX model.

    **Example 1 - Simplest export**
    ::

        class MyModel(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.linear = torch.nn.Linear(2, 2)

            def forward(self, x, bias=None):
                out = self.linear(x)
                out = out + bias
                return out


        model = MyModel()
        kwargs = {"bias": 3.0}
        args = (torch.randn(2, 2, 2),)
        onnx_program = torch.onnx.dynamo_export(model, *args, **kwargs).save(
            "my_simple_model.onnx"
        )

    **Example 2 - Exporting with dynamic shapes**
    ::

        # The previous model can be exported with dynamic shapes
        export_options = torch.onnx.ExportOptions(dynamic_shapes=True)
        onnx_program = torch.onnx.dynamo_export(
            model, *args, **kwargs, export_options=export_options
        )
        onnx_program.save("my_dynamic_model.onnx")


    By printing input dynamic dimensions we can see the input shape is no longer (2,2,2)
    ::

        >>> print(onnx_program.model_proto.graph.input[0])
        name: "arg0"
        type {
          tensor_type {
            elem_type: 1
            shape {
              dim {
                dim_param: "arg0_dim_0"
              }
              dim {
                dim_param: "arg0_dim_1"
              }
              dim {
                dim_param: "arg0_dim_2"
              }
            }
          }
        }
    """

    if export_options is not None:
        resolved_export_options = (
            export_options
            if isinstance(export_options, ResolvedExportOptions)
            else ResolvedExportOptions(export_options, model=model)
        )
    else:
        resolved_export_options = ResolvedExportOptions(ExportOptions(), model=model)

    _assert_dependencies(resolved_export_options)

    try:
        from torch._dynamo import config as _dynamo_config

        with _dynamo_config.patch(do_not_emit_runtime_asserts=True):
            return Exporter(
                options=resolved_export_options,
                model=model,
                model_args=model_args,
                model_kwargs=model_kwargs,
            ).export()
    except Exception as e:
        sarif_report_path = _DEFAULT_FAILED_EXPORT_SARIF_LOG_PATH
        resolved_export_options.diagnostic_context.dump(sarif_report_path)
        message = (
            f"Failed to export the model to ONNX. Generating SARIF report at '{sarif_report_path}'. "
            "SARIF is a standard format for the output of static analysis tools. "
            "SARIF logs can be loaded in VS Code SARIF viewer extension, "
            "or SARIF web viewer (https://microsoft.github.io/sarif-web-component/). "
            f"Please report a bug on PyTorch Github: {_PYTORCH_GITHUB_ISSUES_URL}"
        )
        raise errors.OnnxExporterError(message) from e


def common_pre_export_passes(
    options: ResolvedExportOptions,
    original_model: torch.nn.Module | Callable,
    fx_module: torch.fx.GraphModule,
    fx_module_args: Sequence[Any],
):
    # TODO: Import here to prevent circular dependency
    from torch.onnx._internal.fx import analysis, passes

    diagnostic_context = options.diagnostic_context

    # Apply decomposition table to the input graph.
    module = passes.Decompose(
        diagnostic_context,
        fx_module,
        options.decomposition_table,
        enable_dynamic_axes=options.dynamic_shapes,
        allow_fake_constant=options.fake_context is not None,
    ).run(*fx_module_args)

    # ONNX does not support views and mutations.
    # Functionalize to get a semantically equivalent graph without mutations.
    module = passes.Functionalize(
        diagnostic_context,
        module,
        enable_dynamic_axes=options.dynamic_shapes,
        allow_fake_constant=options.fake_context is not None,
    ).run(*fx_module_args)

    # Input mutations are detected and distilled after `Functionalize` pass.
    # Remove them since ONNX inference does not need them.
    module = passes.RemoveInputMutation(diagnostic_context, module).run(*fx_module_args)

    # ONNX does not support concept of (implicit) type promotion.
    # Insert type casts explicitly where needed.
    module = passes.InsertTypePromotion(diagnostic_context, module).run()

    analysis.UnsupportedFxNodesAnalysis(
        diagnostic_context, module, options.onnxfunction_dispatcher
    ).analyze(infra.levels.ERROR)

    if isinstance(original_model, torch.nn.Module):
        module = passes.RestoreParameterAndBufferNames(
            diagnostic_context, module, original_model
        ).run()

    # This operation should be invoked as the last pre export pass.
    # See [NOTE: Modularize pass ordering]
    module = passes.Modularize(diagnostic_context, module).run()

    # ONNX does not support None inputs. During graph building, all None inputs
    # are removed. Here we register this step to input adapter.
    options.fx_tracer.input_adapter.append_step(io_adapter.RemoveNoneInputStep())

    # NOTE: temp workaround for https://github.com/pytorch/pytorch/issues/99534
    # Dynamo doesn't support non-tensor inputs.
    options.fx_tracer.input_adapter.append_step(io_adapter.RemoveNonTensorInputStep())

    # ONNX does not support complex inputs. During graph building, all complex inputs
    # are converted to real representation inputs. Here we register this step to
    # input/output adapter.
    options.fx_tracer.input_adapter.append_step(
        io_adapter.ConvertComplexToRealRepresentationInputStep()
    )

    # ONNX can't represent collection types (e.g., dictionary, tuple of tuple of
    # tensor, etc), we flatten the collection and register each element as output.
    options.fx_tracer.output_adapter.append_step(io_adapter.FlattenOutputStep())

    # Output post-processing steps should happen after `FlattenOutputStep`.
    options.fx_tracer.output_adapter.append_step(
        io_adapter.ConvertComplexToRealRepresentationOutputStep()
    )

    return module
