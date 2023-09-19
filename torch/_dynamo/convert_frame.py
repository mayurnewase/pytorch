import functools
import itertools
import logging
import os
import random
import types
import weakref
from typing import Dict, Optional, Set

import torch
import torch._logging
from torch._guards import tracing
from torch._utils_internal import signpost_event
from torch.fx.experimental.symbolic_shapes import (
    ConstraintViolationError,
    GuardOnDataDependentSymNode,
)
from torch.fx.graph_module import _forward_from_src as original_forward_from_src
from torch.utils._traceback import format_traceback_short

from . import config, exc
from .allowed_functions import is_allowed
from .backends.registry import CompilerFn
from .bytecode_analysis import remove_dead_code, remove_pointless_jumps
from .bytecode_transformation import (
    check_inst_exn_tab_entries_valid,
    is_generator,
    propagate_inst_exn_table_entries,
    transform_code_object,
)
from .eval_frame import always_optimize_code_objects, skip_code, TorchPatcher
from .exc import (
    augment_exc_message,
    BackendCompilerFailed,
    format_error_msg,
    InternalTorchDynamoError,
    TorchRuntimeError,
    unimplemented,
    Unsupported,
)
from .guards import CheckFunctionManager, GuardedCode
from .hooks import Hooks
from .output_graph import OutputGraph
from .replay_record import ExecutionRecord
from .symbolic_convert import InstructionTranslator
from .utils import (
    CleanupManager,
    counters,
    dynamo_timed,
    format_bytecode,
    gen_record_file_name,
    guard_failures,
    increment_frame,
    is_guard_failure_reporting_enabled,
    is_namedtuple,
    istype,
    LazyString,
    orig_code_map,
    reset_graph_break_dup_checker,
    setup_compile_debug,
    troubleshooting_url,
    write_record_to_file,
)

log = logging.getLogger(__name__)
guards_log = torch._logging.getArtifactLogger(__name__, "guards")
bytecode_log = torch._logging.getArtifactLogger(__name__, "bytecode")
recompiles_log = torch._logging.getArtifactLogger(__name__, "recompiles")


class Tracker:
    def __init__(self):
        self.seen = []
        self.seen_ids = set()

    def add(self, strong_obj):
        idx = id(strong_obj)
        if idx not in self.seen_ids:
            obj = weakref.ref(strong_obj, lambda _: self.seen_ids.remove(idx))
            self.seen.append(obj)
            self.seen_ids.add(idx)

    def __contains__(self, item):
        return id(item) in self.seen_ids

    def clear(self):
        self.seen.clear()
        self.seen_ids.clear()


input_codes = Tracker()
output_codes = Tracker()


initial_grad_state = None
initial_deterministic_algorithms_state = None
initial_torch_function_state = None


@functools.wraps(original_forward_from_src)
def fx_forward_from_src_skip_result(*args, **kwargs):
    # we monkey patch FX to prevent infinite loop of trying to convert
    # our generated code
    result: types.FunctionType = original_forward_from_src(*args, **kwargs)
    skip_code(result.__code__)
    return result


def wrap_convert_context(fn):
    """
    Context manager to:
        1) Save/restore torch.is_grad_enabled() state
        2) Save/restore python random state
        3) Save/restore torch random state
        4) Monkey patch torch.fx.graph_module._forward_from_src
    """

    @functools.wraps(fn)
    def _fn(*args, **kwargs):
        prior_grad_mode = torch.is_grad_enabled()
        py_rng_state = random.getstate()
        torch_rng_state = torch.random.get_rng_state()
        if torch.cuda.is_available():
            cuda_rng_state = torch.cuda.get_rng_state()
        prior_fwd_from_src = torch.fx.graph_module._forward_from_src
        torch.fx.graph_module._forward_from_src = fx_forward_from_src_skip_result
        cleanup = setup_compile_debug()
        try:
            return fn(*args, **kwargs)
        finally:
            cleanup.close()
            torch._C._set_grad_enabled(prior_grad_mode)
            random.setstate(py_rng_state)
            torch.random.set_rng_state(torch_rng_state)
            if torch.cuda.is_available():
                torch.cuda.set_rng_state(cuda_rng_state)
            torch.fx.graph_module._forward_from_src = prior_fwd_from_src

    _fn._torchdynamo_orig_callable = fn  # type: ignore[attr-defined]
    return _fn


@TorchPatcher.suppress_torch_distributed_warnings
def has_tensor_in_frame(frame):
    """Check if the frame has torch.* related bits"""
    # Check if the function was decorated using torch._dynamo.optimize
    if frame.f_code in always_optimize_code_objects:
        return True

    # Check if there is global import of torch.*
    for co_name in frame.f_code.co_names:
        if co_name in frame.f_globals:
            if is_allowed(frame.f_globals[co_name]):
                return True

    seen_ids: Dict[int, bool] = dict()

    def has_tensor(obj):
        """Recursively check if the obj has a tensor"""
        obj_id = id(obj)
        if obj_id in seen_ids:
            return seen_ids[obj_id]
        seen_ids[obj_id] = False

        if isinstance(obj, (torch.Tensor, torch.nn.Module)):
            seen_ids[obj_id] = True
            return seen_ids[obj_id]
        elif istype(obj, (list, tuple)):
            seen_ids[obj_id] = any(has_tensor(v) for v in obj)
            return seen_ids[obj_id]
        elif istype(obj, dict):
            # Some packages like pytest can be updated during runtime. So, make a
            # copy of values to avoid issues like "RuntimeError: dictionary
            # changed size during iteration"
            values = list(obj.values())
            seen_ids[obj_id] = any(has_tensor(v) for v in values)
            return seen_ids[obj_id]
        elif istype(obj, (str, int, float, type(None), bool)):
            seen_ids[obj_id] = False
            return seen_ids[obj_id]
        elif is_namedtuple(obj):
            seen_ids[obj_id] = any(has_tensor(getattr(obj, v)) for v in obj._fields)
            return seen_ids[obj_id]
        else:
            # if config.debug:
            #     print(
            #         f"Assuming that object of type {type(obj)} does not have a tensor"
            #     )
            return False

    # Check if the passed arguments are of type Tensor
    for value in frame.f_locals.values():
        if has_tensor(value):
            return True

    log.debug(
        "skipping because no torch.* %s \
            %s %s",
        frame.f_code.co_name,
        frame.f_code.co_filename,
        frame.f_code.co_firstlineno,
    )

    return False


def exception_handler(e, code, frame=None):
    record_filename = None
    if hasattr(e, "exec_record"):
        record_filename = gen_record_file_name(e, code)
        write_record_to_file(record_filename, e.exec_record)
        e.record_filename = record_filename

    augment_exc_message(e)
    # Only log the exception if we are going to suppress it
    # if aren't suppressing it, a higher level except block will handle it
    if config.suppress_errors:
        log.error(format_error_msg(e, code, record_filename, frame))


FRAME_COUNTER = 0


def convert_frame_assert(
    compiler_fn: CompilerFn,
    one_graph: bool = True,
    export: bool = False,
    export_constraints=None,
):
    """Fully convert a frame into an FX graph
        compiler_fn is inductor, trace that again in eval_frame.optimize function if needed
    """
    reset_graph_break_dup_checker()

    def _convert_frame_assert(
        frame: types.FrameType, cache_size: int, hooks: Hooks, frame_state
    ): 
        """
        # this function should optimize our function, this is optional can be bypassed but check what and how it does this.
        DONE debugging this beast
            genearates python bytecode of c++ code
        """
        # breakpoint()

        increment_frame()
        global FRAME_COUNTER
        if "_id" not in frame_state:
            frame_state["_id"] = FRAME_COUNTER          # 0
            FRAME_COUNTER += 1

        code = frame.f_code

        if code in input_codes and (
            recompiles_log.isEnabledFor(logging.DEBUG) or config.error_on_recompile
        ):
            if is_guard_failure_reporting_enabled():
                message = (
                    f"Recompiling function {code.co_name} in {code.co_filename}:{code.co_firstlineno}",
                    f"triggered by the following guard failure: {str(guard_failures[code][-1])}",
                )
            else:
                message = (
                    f"Recompiling function {code.co_name} in {code.co_filename}:{code.co_firstlineno}",
                    "set env var TORCHDYNAMO_REPORT_GUARD_FAILURES=1 to debug further",
                )

            if recompiles_log.isEnabledFor(logging.DEBUG):
                recompiles_log.debug(message, stack_info=True)

            if config.error_on_recompile:
                raise exc.RecompileError(message)

        input_codes.add(code)
        if code in output_codes:
            return None
        if (
            os.environ.get("TORCHDYNAMO_DEBUG_FUNCTION")
            and os.environ.get("TORCHDYNAMO_DEBUG_FUNCTION") != code.co_name
        ):
            return None
        if code.co_name == "<genexpr>" and code.co_filename.endswith(
            ("transformers/file_utils.py", "transformers/utils/generic.py")
        ):
            # not needed, but cleans up torchbench error stats
            return None
        if code.co_name == "__setattr__":
            # setattr could be tricky to handle generally,
            # but also not likely useful to compile- skip the whole frame
            return None
        if code.co_name == "__init__" and code.co_filename.startswith(
            os.path.dirname(torch.optim.__file__)
        ):
            # optimizer support is still incomplete see
            # test_state_dict in test/dynamo/test_optimizers.py
            return None

        # Check if the frame is generated by an exec builtin call
        # TODO - Running exec generated frame seems propagates f_globals to the
        # next frames.
        if code.co_name == "<module>" and code.co_filename == "<string>":
            return None

        if (
            code.co_name == "<lambda>"
            and code.co_filename == "<string>"
            and not bool(frame.f_builtins)
        ):
            # namedtuple subclass constructor. Empty builtins cause issue with
            # len keyword in LIST_LEN guard.
            return None

        if is_generator(code):
            unimplemented("generator")
        if cache_size >= config.cache_size_limit:

            def format_func_info(code):
                return f"'{code.co_name}' ({code.co_filename}:{code.co_firstlineno})"

            def format_guard_failures(code):
                # For the common case, it's sufficient to see just the most recent failure.
                # We could add a verbose mode if needed
                return f"  reasons: {str(guard_failures[code][-1])}\n"

            if config.report_guard_failures:
                assert code in guard_failures, "TODO(whc) any other recompile reasons?"

                log.warning(
                    "torch._dynamo hit config.cache_size_limit (%s)\n"
                    "   function: %s\n"
                    "   reasons:  %s\n"
                    "to diagnose recompilation issues, see %s.",
                    config.cache_size_limit,
                    format_func_info(code),
                    format_guard_failures(code),
                    troubleshooting_url,
                )
            else:
                log.warning(
                    "torch._dynamo hit config.cache_size_limit (%s)\n"
                    "   function: %s\n"
                    "to diagnose recompilation issues, set env variable TORCHDYNAMO_REPORT_GUARD_FAILURES=1"
                    " and also see %s.",
                    config.cache_size_limit,
                    format_func_info(code),
                    troubleshooting_url,
                )
            unimplemented("cache_size_limit reached")

        if not has_tensor_in_frame(frame):
            return None

        global initial_grad_state
        initial_grad_state = torch.is_grad_enabled()   # true for training, false for inference

        global initial_deterministic_algorithms_state
        initial_deterministic_algorithms_state = (          # false
            torch.are_deterministic_algorithms_enabled()
        )

        global initial_torch_function_state                 # true
        initial_torch_function_state = torch._C._is_torch_function_enabled()        # true, interface function, but don't know where its defined

        # signpost_event(           # stupid meta logger
        #     "dynamo",
        #     "_convert_frame_assert._compile",
        #     {
        #         "co_name": code.co_name,
        #         "co_filename": code.co_filename,
        #         "co_firstlineno": code.co_firstlineno,
        #         "cache_size": cache_size,
        #     },
        # )

        return _compile(
            frame.f_code,
            frame.f_globals,
            frame.f_locals,
            frame.f_builtins,
            compiler_fn,
            one_graph,
            export,
            export_constraints,
            hooks,
            frame,
            frame_state=frame_state,
        )

    _convert_frame_assert._torchdynamo_orig_callable = compiler_fn  # type: ignore[attr-defined]
    return wrap_convert_context(_convert_frame_assert)


@dynamo_timed(phase_name="entire_frame_compile")
def _compile(
    code: types.CodeType,
    globals: Dict[str, object],
    locals: Dict[str, object],
    builtins: Dict[str, object],
    compiler_fn: CompilerFn,
    one_graph: bool,
    export: bool,
    export_constraints,
    hooks: Hooks,
    frame: Optional[types.FrameType] = None,
    frame_state=None,
) -> Optional[GuardedCode]:
    output: Optional[OutputGraph] = None
    # This is shared across restarts
    mutated_closure_cell_contents: Set[str] = set()

    # from .utils import print_once;  print_once(code.co_filename)
    breakpoint()

    def transform(instructions, code_options):
        nonlocal output
        tracer = InstructionTranslator(
            instructions,
            code,
            locals,
            globals,
            builtins,
            code_options,
            compiler_fn,
            one_graph,
            export,
            export_constraints,
            mutated_closure_cell_contents,
            frame_state=frame_state,
        )
        with tracing(tracer.output.tracing_context):        # DEBUG:this output object is a big wrapper on fx-graph to track graph's output, check later as needed
            tracer.run()                                    # DEBUG: this fills up tracer's output.output_instructions list with calling the c++ function and returning the results
        output = tracer.output
        assert output is not None
        assert output.output_instructions
        breakpoint()
        instructions[:] = output.output_instructions
        code_options.update(output.code_options)        # {'co_argcount': 2, 'co_posonlyargcount': 0, 'co_kwonlyargcount': 0, 'co_nlocals': 3, 'co_stacksize': 3, 'co_flags': 67, 'co_code': b't\x00\xa0\x01|\x00\xa1\x01}\x02|\x02S\x00', 'co_consts': (None,), 'co_names': ('torch', 'sin', '__compiled_fn_0', 'size'), 'co_varnames': ('x', 'y', 'a'), 'co_filename': '/home/mayur/projects/pytorch/examples/105524/compile_demo.py', 'co_name': 'foo', 'co_firstlineno': 8, 'co_linetable': b'\n\x01\x04\x02', 'co_freevars': (), 'co_cellvars': ()}

        if config.dead_code_elimination:                #DEBUG: ignore for now
            propagate_inst_exn_table_entries(instructions)
            check_inst_exn_tab_entries_valid(instructions)
            instructions[:] = remove_pointless_jumps(remove_dead_code(instructions))

    try:
        for attempt in itertools.count():
            try:
                out_code = transform_code_object(code, transform)       # code = user script bytecde, out_code => CompiledFXGrpah's bytecode 
                orig_code_map[out_code] = code
                break
                """
                    code = 
                          9           0 LOAD_GLOBAL              0 (torch)
                                        2 LOAD_METHOD              1 (sin)
                                        4 LOAD_FAST                0 (x)
                                        6 CALL_METHOD              1
                                        8 STORE_FAST               2 (a)

                            11          10 LOAD_FAST                2 (a)
                                        12 RETURN_VALUE
                    
                    out_code = 
                         8           0 LOAD_GLOBAL              2 (__compiled_fn_0)
                                    2 LOAD_FAST                0 (x)
                                    4 LOAD_ATTR                3 (size)
                                    6 LOAD_CONST               1 (0)
                                    8 CALL_FUNCTION            1
                                    10 LOAD_FAST                0 (x)
                                    12 CALL_FUNCTION            2
                                    14 UNPACK_SEQUENCE          1
                                    16 RETURN_VALUE
                """

            except exc.RestartAnalysis as e:
                log.info(
                    "Restarting analysis due to %s",
                    LazyString(format_traceback_short, e.__traceback__),
                )
                if attempt > 100:
                    unimplemented("100+ RestartAnalysis() calls")
            except exc.SkipFrame as e:
                log.debug(
                    "Skipping frame %s %s \
                    %s %s",
                    e,
                    code.co_name,
                    code.co_filename,
                    code.co_firstlineno,
                )
                if one_graph:
                    log.debug("No graph captured with one_graph=True")
                return None
        output_codes.add(out_code)

        def log_bytecode(prefix, name, filename, line_no, code):
            if bytecode_log.isEnabledFor(logging.DEBUG):
                bytecode_log.debug(
                    format_bytecode(prefix, name, filename, line_no, code)
                )

        log_bytecode(
            "ORIGINAL BYTECODE",
            code.co_name,
            code.co_filename,
            code.co_firstlineno,
            code,
        )
        log_bytecode(
            "MODIFIED BYTECODE",
            code.co_name,
            code.co_filename,
            code.co_firstlineno,
            out_code,
        )

        assert output is not None

        # Skipping Dynamo on a frame without any extracted graph.
        # This does not affect eager functionality. But this is necessary
        # for export for cases where Dynamo-reconstructed bytecode can create
        # new function frames, confusing export in thinking that there
        # are extra graphs now.

        if output.export and output.is_empty_graph():
            return None

        assert output.guards is not None
        CleanupManager.instance[out_code] = output.cleanups
        check_fn = CheckFunctionManager(
            output,
            hooks.guard_fail_fn if hooks else None,
        )

        guarded_code = GuardedCode(out_code, check_fn.check_fn)     # just dataclass for out_code and guard_fn which I don't know about

        if guards_log.isEnabledFor(logging.DEBUG):
            guard_str = "GUARDS:\n"
            guard_str += "\n".join(
                [
                    f"  {code}"
                    for guard in sorted(output.guards)
                    if guard.code_list is not None
                    for code in guard.code_list
                ]
            )
            guards_log.debug(guard_str)

        if not output.is_empty_graph() and hooks.guard_export_fn is not None:
            # We should not run the guard_export_fn when Dynamo does not
            # generate any graph. This can happen in export when TorchDynamo
            # generated bytecode has some reconstruction logic for mutated
            # variables which can trigger TorchDynamo on the children frames but
            # they are benign and do not generate any new graphs.
            hooks.guard_export_fn(output.guards)

        output.local_scope.clear()
        return guarded_code
    except (
        Unsupported,
        TorchRuntimeError,
        BackendCompilerFailed,
        AssertionError,
        ConstraintViolationError,
        GuardOnDataDependentSymNode,
    ) as e:
        exception_handler(e, code, frame)
        raise
    except Exception as e:
        exception_handler(e, code, frame)
        raise InternalTorchDynamoError(str(e)).with_traceback(e.__traceback__) from None


def convert_frame(compiler_fn: CompilerFn, hooks: Hooks):
    """Try to convert a frame into an FX graph, if error leave frame unmodified"""
    inner_convert = convert_frame_assert(compiler_fn, one_graph=False)      # DEBUG: this is very big and main, looks like it converts the frame to fx graph

    def _convert_frame(
        frame: types.FrameType, cache_size: int, hooks: Hooks, frame_state
    ):
        """
            called on inference

            (Pdb) bt
                /home/mayur/projects/pytorch/examples/105524/compile_demo.py(18)<module>()
                -> pred = opt_foo1(torch.randn(10, 10), torch.randn(10, 10))
                /home/mayur/projects/pytorch/torch/_dynamo/eval_frame.py(314)_fn()
                -> return fn(*args, **kwargs)
                /home/mayur/projects/pytorch/torch/_dynamo/eval_frame.py(474)catch_errors()
                -> return callback(frame, cache_size, hooks, frame_state)
                > /home/mayur/projects/pytorch/torch/_dynamo/convert_frame.py(541)_convert_frame()
                -> def _convert_frame()

                args
                    frame = our foo function
                    cache_size = 0
                    frame_state = {}

                when called after graph is compiled
                    frame = <frame at 0x7f9f1a38cdc0, file '/home/mayur/projects/pytorch/examples/105524/compile_demo.py', line 8, code foo>
                    cache_size = 0
                    frame_state = {'_id': 0, "L['x']": FrameStateSizeEntry(scalar=None, size=[2, 2]), "L['y']": FrameStateSizeEntry(scalar=None, size=[2, 2])}
        """
        # breakpoint()
        counters["frames"]["total"] += 1            # {'frames': Counter({'total': 30, 'ok': 30})}
        # return None                                 # TODO: remove this to debug optimizations in inner_convert
        try:
            result = inner_convert(frame, cache_size, hooks, frame_state)       # DEBUG -> this outputs a guarded code -> which is python bytecode for c++ code
            breakpoint()                                                        # TODO: see what happens to guarded code after this, and how it gets called with real input
            counters["frames"]["ok"] += 1                                       # DEBUG: its bit confusing call goes to eval_frame function multiple times after this.

            return result                                                       # after this call goes back to torchdynamocontext.__call__ function with a prior frame and empty callback.
        except (NotImplementedError, Unsupported):                              # CURRENT: try raising this manually, then trace the calls then trace without the errors.
            log.info("converting frame raised unsupported, leaving it unconverted")
        except Exception:
            if not config.suppress_errors:
                raise
            log.info("converting frame raised error, suppressing error")
        return None

    _convert_frame._torchdynamo_orig_callable = compiler_fn  # type: ignore[attr-defined]
    return _convert_frame


# TODO mlazos: add support for same args, or record them
def replay(filename):
    from .backends.debugging import eager

    original_replay_val = config.replay_record_enabled
    config.replay_record_enabled = False
    with open(filename, "rb") as in_file:
        record = ExecutionRecord.load(in_file)
    record.globals = dict(itertools.chain(record.globals.items(), globals().items()))

    try:
        _compile(
            record.code,
            record.globals,
            record.locals,
            record.builtins,
            compiler_fn=eager,
            one_graph=False,
            export=False,
            hooks=Hooks(),
            frame=None,
        )
    except Exception:
        pass
    finally:
        config.replay_record_enabled = original_replay_val
