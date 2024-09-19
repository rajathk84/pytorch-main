# mypy: allow-untyped-defs
"""
Utils for caching the outputs of AOTAutograd
"""
from __future__ import annotations

import json
import logging
import os
import pickle
import shutil
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, TYPE_CHECKING, Union

import torch
from torch._dynamo.utils import counters, get_chromium_event_logger
from torch._functorch import config
from torch._inductor.codecache import (
    _ident,
    BypassFxGraphCache,
    CompiledFxGraph,
    extract_tensor_metadata_for_cache_key,
    FxGraphCache,
    FxGraphCachePickler,
    FxGraphHashDetails,
    write_atomic,
)
from torch._inductor.runtime.runtime_utils import cache_dir
from torch._logging import LazyString

from .runtime_wrappers import (
    AOTDispatchAutograd,
    AOTDispatchSubclassWrapper,
    CompilerWrapper,
    FunctionalizedRngRuntimeWrapper,
    post_compile,
    RuntimeWrapper,
    SubclassMeta,
)
from .schemas import AOTConfig, ViewAndMutationMeta  # noqa: F401


if TYPE_CHECKING:
    from torch._inductor.utils import BoxedBool
    from torch.fx.node import Node
log = logging.getLogger(__name__)


class BypassAOTAutogradCache(Exception):
    pass


# Used to signify when FXGraphCache missed when AOTAutogradCache uses it
class FXGraphCacheMiss(BypassAOTAutogradCache):
    pass


def check_node_safe(node: Node):
    """
    Checks that the node only uses supported operators. We are starting with very
    conservative cacheability constraints, and incrementally adding more support as we expand.

    [Note: AOTAutograd Cacheability checks]
    - Our cache key is computed from the FX graph produced by Dynamo and the input example values
    - A node is "safe" if the same cache key results in a compiled artifact that has the same behavior
        (i.e, the set of inputs that go into our cache key is sufficient to distinguish its behavior)

    To accomplish this safety check, we consider the following functions to be safe:
        - Public functions under modules torch, torch.functional, and torch.nn.functional: these are
        allowed in the graph by dynamo, so we can assume they are safe to cache.
        - method calls on base tensor types
        - Any call_module that dynamo deemed safe to allow AOTAutograd to trace
        - Non callable nodes, such as placeholder, output, get_attr

    The test suite test_aot_autograd_cache.py::AOTAutogradCachePicklerTests tries its best to fully cover/specify this behavior.
    """
    SAFE_TORCH_MODULES = ("torch.functional", "torch.nn.functional")

    def is_public_torch_api(target):
        # Don't blindly allow private functions in the torch namespace
        is_private = target.__name__.startswith("_")
        return (
            getattr(target, "__module__", None) in SAFE_TORCH_MODULES and not is_private
        )

    def is_torch_function(target):
        if isinstance(target, torch._ops.OpOverload):
            return True
        if is_public_torch_api(target):
            return True
        is_builtin_fun_or_type = type(target).__name__ == "builtin_function_or_method"
        return is_builtin_fun_or_type

    def is_tensor(target):
        # Tensors always have example values in meta field
        return "example_value" in target.meta

    # I'd love to use a match statement here, but it wasn't introduced until py3.10
    if node.op == "call_function":
        # We support only torch.* functions for now
        # We can probably add an allowlist of safe non-torch implementations as well
        if not is_torch_function(node.target):
            raise BypassAOTAutogradCache(
                f"Unsupported call_function target {node.target}"
            )
    elif node.op == "call_method":
        method_name = node.target
        method_target = node.args[0]
        # Only support method calls on base tensors
        if not is_tensor(method_target):
            raise BypassAOTAutogradCache(
                f"Unsupported call_method target {method_target}"
            )
        if (
            type(method_name) != str
            and type(method_name).__name__ != "method_descriptor"
        ):
            raise BypassAOTAutogradCache(
                f"Unsupported call_method method {node.target}: {method_name}"
            )
    # Cache safe
    elif node.op in ("placeholder", "get_attr", "call_module", "output"):
        # Assumption today for call_module being a safe op:
        # (1) today the only call_module ops that can show up in a graph come from "built-in-nn-modules"
        # that dynamo assumes are safe to trace. If dynamo assumes they are safely to blindly trace, then
        # they should be safe to cache as well.
        # (2) in the steady-state (some time in H2?) we shouldn't see these anymore, once inline builtin nn modules by default
        # (3) We do not allow user made nn modules in the graph today, only function calls.
        pass
    else:
        raise BypassAOTAutogradCache(f"Unsupported node op {node.op}")


def check_cacheable(gm: torch.fx.GraphModule):
    """
    Checks that the graph module only uses supported operators
    """
    nodes = gm.graph.nodes
    if torch._dynamo.compiled_autograd.in_compiled_autograd_region:
        raise BypassAOTAutogradCache(
            "Cannot cache a graph with compiled autograd enabled"
        )

    if not torch._inductor.config.fx_graph_cache:
        raise BypassAOTAutogradCache("FX graph cache is not enabled")

    tracing_context = torch._guards.TracingContext.try_get()
    if tracing_context and tracing_context.fakify_first_call:
        raise BypassAOTAutogradCache(
            "Won't cache a graph with fakify_first_call enabled"
        )
    for node in nodes:
        check_node_safe(node)


class AOTAutogradCacheDetails(FxGraphHashDetails):
    """
    Object to capture all the details for a dynamo graph module relevant to computing
    a safe and stable cache key for AOTAutograd.
    """

    def __init__(
        self,
        gm: torch.fx.GraphModule,
        example_inputs,
        aot_config: AOTConfig,
        fx_config: Dict[str, BoxedBool],
    ):
        # FxGraphHashDetails contains all the keys related to inductor. Also includes some system info
        self.aot_config = aot_config
        self.grad_enabled = torch.is_grad_enabled()
        self.disable_amp = torch._C._is_any_autocast_enabled()
        self.deterministic_algorithms = torch.are_deterministic_algorithms_enabled()
        self.autograd_config = config.save_config()
        try:
            # TODO: example_inputs causes more cache misses than necessary
            # with dynamic shapes, because this is before we add
            # symints to tensor metadata. Improve this later.
            super().__init__(gm, example_inputs, fx_config, [])
        except BypassFxGraphCache as e:
            # Sometimes inductor configs are unpickleable and can fail
            raise BypassAOTAutogradCache from e

    def debug_lines(self) -> List[str]:
        return AOTAutogradCachePickler.debug_lines(self)


def _reduce_aot_config(aot_config: AOTConfig):
    """
    Reduce the config to a stable key for caching.
    """
    return (
        _ident,
        (
            aot_config.num_params_buffers,
            aot_config.keep_inference_input_mutations,
            aot_config.is_export,
            aot_config.no_tangents,
            aot_config.dynamic_shapes,
            aot_config.aot_autograd_arg_pos_to_source,
            aot_config.enable_log,
            aot_config.pre_dispatch,
        ),
    )


def _reduce_tensor(tensor):
    """
    Reduce the tensor to a stable key for caching.
    """
    return (
        _ident,
        (
            extract_tensor_metadata_for_cache_key(
                FxGraphCachePickler._device_map, tensor
            ),
        ),
    )


class AOTAutogradCachePickler(FxGraphCachePickler):
    dispatch_table = FxGraphCachePickler.dispatch_table.copy()
    dispatch_table[AOTConfig] = _reduce_aot_config
    dispatch_table[torch.Tensor] = _reduce_tensor


def autograd_cache_key(
    gm: torch.fx.GraphModule,
    example_inputs,
    config: AOTConfig,
    fx_config: Dict[str, BoxedBool],
    # TODO: add args and parameters
) -> Tuple[str, List[str]]:
    """
    Generate a unique hash of the FX graph for caching.
    """
    check_cacheable(gm)
    details = AOTAutogradCacheDetails(gm, example_inputs, config, fx_config)
    # The prefix distinguishes among the other kinds of objects we cache
    key = "a" + AOTAutogradCachePickler.get_hash(details)
    debug_lines = details.debug_lines()
    log.debug(
        "Autograd graph cache hash details for key %s:\n%s",
        key,
        LazyString(lambda: "\n".join(debug_lines)),
    )
    return key, debug_lines


@dataclass
class FXGraphCacheLoadable:
    fx_graph_cache_key: str

    def load(self, example_inputs, fx_config: Dict[str, BoxedBool]) -> CompiledFxGraph:
        # [Note: AOTAutogradCache and FXGraphCache Guard interactions]
        # As mentioned, AOTAutograd takes in the symint inputs from dynamo's list of arguments.
        # FXGraphCache serializes guards that are needed in the shape_env based on these symint inputs to the graph.
        # The invariant that AOTAutograd uses here is that the sources for symints given to it by dynamo are exactly
        # the same as the ones it passes to inductor, for both the forward and backward passes.
        # (This does not mean that the tensor values passed in are the same: only that their symints are).
        # That is, AOTAutograd and Inductor never create new guards based on symints with different sources
        # than those passed to it by inductor.
        result = FxGraphCache._lookup_graph(
            self.fx_graph_cache_key, example_inputs, local=True, remote_cache=None
        )
        if result is None:
            log.info("FXGraphCache cache miss for key %s", self.fx_graph_cache_key)
            counters["inductor"]["fxgraph_cache_miss"] += 1
            raise FXGraphCacheMiss
        FxGraphCache.post_compile(result, example_inputs, fx_config["cudagraphs"])
        counters["inductor"]["fxgraph_cache_hit"] += 1
        result._boxed_call = True
        return result


@dataclass
class CompiledForward(FXGraphCacheLoadable):
    """
    Cacheable entry for a forward function
    """


@dataclass
class CompiledBackward(FXGraphCacheLoadable):
    """
    Cacheable entry for a forward function
    """

    # Used by AOTDispatchAutograd.post_compile
    backward_state_indices: List[int]
    num_symints_saved_for_bw_: int


@dataclass
class AOTAutogradCacheEntry:
    """A single entry into the cache."""

    # Forward and Backward info
    compiled_fw: CompiledForward
    compiled_bw: Optional[CompiledBackward]

    # Runtime_metadata saved right before compilation
    runtime_metadata: ViewAndMutationMeta

    # Wrappers that run after each aot_dispatch_* function
    dispatch_wrappers: List[CompilerWrapper]

    # Used by AOTSubclassWrapper
    maybe_subclass_meta: Optional[SubclassMeta]
    num_fw_outs_saved_for_bw: Optional[int]

    # Used by RuntimeWrapepr
    indices_of_inps_to_detach: List[int]

    # Turn cache entry into the original callable
    def wrap_post_compile(
        self,
        args: List[torch.Tensor],
        aot_config: AOTConfig,
        fx_config: Dict[str, BoxedBool],
    ) -> Callable:
        """
        This function takes a cache entry and carefully reconstructs the original callable
        that AOTAutograd returned the first time it was run. It does this by running the various
        post compile steps that AOTAutograd runs on its compiled artifact after running the fw/bw compilers.

        In the inference path, this consists of the Subclass, FunctionalzedRngRuntime, and RuntimeWrappers.
        In the autograd path, this consists of AOTAutogradDispatch.post_compile.

        The steps here should match exactly the steps that are run in aot_dispatch_base and aot_dispatch_autograd.

        Notably absent from the cached path are:
        - DebugAssertWrapper
        - FakifiedOutWrapper

        Which we'll handle separately later on, if necessary.
        """
        compiled_fw_func = self.compiled_fw.load(args, fx_config)
        compiled_bw_func = None
        if self.compiled_bw is not None:
            compiled_bw_func = self.compiled_bw.load(args, fx_config)
            needs_autograd = True
        else:
            needs_autograd = False

        # Wrap the forward function in post compile wrappers
        compiled_fw_func = AOTDispatchSubclassWrapper(
            trace_joint=needs_autograd,
            fw_only=None,
            maybe_subclass_meta=self.maybe_subclass_meta,
            num_fw_outs_saved_for_bw=self.num_fw_outs_saved_for_bw,
        ).post_compile(
            compiled_fw_func, aot_config, runtime_metadata=self.runtime_metadata
        )

        # In autograd case, functionalizedRngWrapper should not modify outs
        return_new_outs = not needs_autograd
        compiled_fw_func = FunctionalizedRngRuntimeWrapper(
            return_new_outs=return_new_outs
        ).post_compile(
            compiled_fw_func, aot_config, runtime_metadata=self.runtime_metadata
        )
        disable_amp = torch._C._is_any_autocast_enabled()

        if needs_autograd:
            assert self.compiled_bw is not None
            # This function is run on both cache miss and cache hit, either here
            # or in aot_dispatch_autograd. On a cache hit,
            # 1. the bw is already compiled
            # 2. we don't need to save to the cache again
            # so those corresponding arguments are set to None.
            compiled_function = AOTDispatchAutograd.post_compile(
                compiled_fw_func,
                compiled_bw_func,
                self.maybe_subclass_meta,
                self.compiled_bw.num_symints_saved_for_bw_,
                self.compiled_bw.backward_state_indices,
                disable_amp,
                self.indices_of_inps_to_detach,
                None,  # lazy_backward_info
                aot_config,
                fw_metadata=self.runtime_metadata,
                try_save_cache_entry=None,
            )
        else:
            compiled_function = RuntimeWrapper(
                indices_of_inps_to_detach=self.indices_of_inps_to_detach,
                trace_joint=False,
                disable_amp=disable_amp,
            ).post_compile(
                compiled_fw_func, aot_config, runtime_metadata=self.runtime_metadata
            )

        compiled_function, _ = post_compile(
            self.dispatch_wrappers,
            compiled_function,
            aot_config,
            runtime_metadata=self.runtime_metadata,
        )

        return compiled_function


class AOTAutogradCache:
    """
    Caches the results of running AOTAutograd. This class mostly handles the save and load logic, whereas
    AOTAutogradCacheEntry handles the wrapping/unwrapping logic.

    Cache Inputs (AOTAutogradCacheDetails)
    - AOTAutogradCache takes in the following inputs, which are analogous to inputs given
        to AOTAutograd by dynamo:
        - A fx graph module generated by dynamo
        - A list of args, which consists of:
            - Symint inputs to the graph, generated by dynamo
            - The **real tensor** inputs, which inductor uses for cudagraphs
            - Notably, the real tensor inputs don't have symints in their metadata.
        AOTAutograd then retraces those real tensor arguments into FakeTensors later during execution.
        - A set of global configurations that affect AOTAutograd or Inductor behavior.

    It then generates a cache key given these values. Notably, this means AOTAutogradCache currently
    specializes on the sizes and strides of the real tensor inputs when dynamic shapes are turned on.
    In a later PR, we'll likely generate the cache key based on the FakeTensors AOTAutograd generates
    based on the real tensor inputs, which can contain symints.

    # Cache Outputs (AOTAutogradCacheEntry)
    - AOTAutogradCache caches the following values:
        - The compiled forward and backward functions from inductor, via keys to the FXGraphCache
        - Metadata to reconstruct the AOTModule from the compiled inductor artifacts
        - See AOTAutogradCacheEntry for more info

    [Note: Caching guards generated by AOTAutograd and Inductor]
    AOTAutograd and inductor both can introduce new guards to the shape environment. FXGraphCache saves guards with each
    compiled graph inductor generates. On a cache hit, AOTAutograd reloads the compiled forward and backward functions
    from FXGraphCache, giving it new symint arguments from the input args.
    FXGraphCache uses those symints and its saved guards to repopulate the ShapeEnv with guards.
    **No new guards are generated into the shape env after inductor finishes compiling**, so the guards
    saved by inductor are sufficient for correctness for both AOTAutograd and Inductor's caches.
    """

    @staticmethod
    def clear():
        """Clear the cache"""
        try:
            shutil.rmtree(AOTAutogradCache._get_tmp_dir())
        except FileNotFoundError:
            pass

    @staticmethod
    def load(
        dispatch_and_compile: Callable,
        mod: Union[torch.fx.GraphModule, torch._dynamo.utils.GmWrapper],
        args,
        aot_config: AOTConfig,
        cudagraphs: BoxedBool,
    ) -> Callable:
        """
        Load a result from the cache, and reconstruct a runtime wrapper around the object
        """
        gm = mod.gm if isinstance(mod, torch._dynamo.utils.GmWrapper) else mod
        compiled_fn = None
        cache_key = None
        debug_lines: List[str] = []
        cache_event_time = time.time_ns()
        cache_state = None
        fx_config = {"cudagraphs": cudagraphs}
        try:
            cache_key, debug_lines = autograd_cache_key(gm, args, aot_config, fx_config)
            entry: Optional[AOTAutogradCacheEntry] = AOTAutogradCache._lookup(cache_key)
            if entry is not None:
                compiled_fn = entry.wrap_post_compile(args, aot_config, fx_config)
                log.info("AOTAutograd cache hit for key %s", cache_key)
                counters["aot_autograd"]["autograd_cache_hit"] += 1
                cache_state = "hit"
                cache_event_time = time.time_ns()
            if compiled_fn is None:
                log.info("AOTAutograd cache miss for key %s", cache_key)
                counters["aot_autograd"]["autograd_cache_miss"] += 1
                cache_state = "miss"
                cache_event_time = time.time_ns()
        # Count missing the FXGraphCache as a miss not a bypass
        except FXGraphCacheMiss as e:
            counters["aot_autograd"]["autograd_cache_miss"] += 1
            # Special counter when we pass autograd cache but
            # fail when on inductor guards
            counters["aot_autograd"]["autograd_cache_guard_miss"] += 1
            if config.strict_autograd_cache:
                raise e
        except BypassAOTAutogradCache as e:
            cache_key = None
            counters["aot_autograd"]["autograd_cache_bypass"] += 1
            cache_state = "bypass"
            cache_event_time = time.time_ns()
            if config.strict_autograd_cache:
                raise e
        if compiled_fn is None:
            # Set the cache key so we can save a cache result later
            aot_config.cache_key = cache_key
            compiled_fn = dispatch_and_compile()
        cache_args = {
            "key": cache_key,
            "cache_state": cache_state,
            "components": debug_lines,
        }
        chromium_log = get_chromium_event_logger()
        chromium_log.log_instant_event(
            f"autograd_cache_{cache_state}", cache_event_time, metadata=cache_args
        )
        torch._logging.trace_structured(
            "artifact",
            metadata_fn=lambda: {
                "name": "aotautograd_cache_hash",
                "encoding": "json",
            },
            payload_fn=lambda: json.dumps(cache_args),
        )
        return compiled_fn

    @staticmethod
    def _get_tmp_dir() -> str:
        """
        Get the toplevel temporary directory for storing compiled graphs.
        """
        return os.path.join(cache_dir(), "aotautograd")

    @staticmethod
    def _lookup(key: str) -> Optional[AOTAutogradCacheEntry]:
        """Given a key generated by AOTAutogradCachePickler, look up its location in the cache."""
        subdir = os.path.join(AOTAutogradCache._get_tmp_dir(), key)
        if not os.path.exists(subdir):
            return None
        path = os.path.join(subdir, "entry")
        try:
            with open(path, "rb") as f:
                entry: AOTAutogradCacheEntry = pickle.load(f)
            return entry
        except Exception as e:
            log.warning("AOTAutograd cache unable to load compiled graph: %s", e)
            if config.strict_autograd_cache:
                raise e
            return None

    @staticmethod
    def save(key: str, entry: AOTAutogradCacheEntry):
        """Save a single entry into the cache."""
        try:
            content = pickle.dumps(entry)
        except Exception as e:
            log.warning("AOTAutograd cache unable to serialize compiled graph: %s", e)
            if config.strict_autograd_cache:
                raise e
            return None
        subdir = os.path.join(AOTAutogradCache._get_tmp_dir(), key)
        if not os.path.exists(subdir):
            os.makedirs(subdir, exist_ok=True)
        path = os.path.join(subdir, "entry")
        log.info("Writing AOTAutograd cache entry to %s", path)
        write_atomic(path, content)
        counters["aot_autograd"]["autograd_cache_saved"] += 1
