#define PY_SSIZE_T_CLEAN
#include <torch/csrc/dynamo/cache_entry.h>
#include <torch/csrc/dynamo/cpp_shim.h>
#include <torch/csrc/dynamo/cpython_defs.h>
#include <torch/csrc/dynamo/cpython_includes.h>
#include <torch/csrc/dynamo/debug_macros.h>
#include <torch/csrc/dynamo/extra_state.h>
#include <torch/csrc/dynamo/framelocals_mapping.h>
#include <torch/csrc/utils/python_compat.h>
#include <opcode.h>
#include <stdbool.h>

#define MAX_COMPILE_CONTEXT_SIZE 100

PyObject* guard_error_hook = NULL;
const char* cache_lookup_profiler_str = "TorchDynamo Cache Lookup";
static char compile_context[MAX_COMPILE_CONTEXT_SIZE];
static int active_dynamo_threads = 0;

static Py_tss_t eval_frame_callback_key = Py_tss_NEEDS_INIT;

inline static PyObject* eval_frame_callback_get(void) {
  void* result = PyThread_tss_get(&eval_frame_callback_key);
  if (unlikely(result == NULL)) {
    return (PyObject*)Py_None;
  } else {
    return (PyObject*)result;
  }
}

inline static void eval_frame_callback_set(PyObject* obj) {
  PyThread_tss_set(&eval_frame_callback_key, obj);
}

// 3.14 Not supported at all. See cpython_defs.c for hints
#if !(IS_PYTHON_3_14_PLUS)

// All the eval APIs change in 3.11 so we need to decide which one to use on the fly
// https://docs.python.org/3/c-api/init.html#c._PyFrameEvalFunction
#if IS_PYTHON_3_11_PLUS
#define THP_EVAL_API_FRAME_OBJECT _PyInterpreterFrame

// We need to be able to return the _PyInterpreterFrame to python so create
// a python binding for it

typedef struct THPPyInterpreterFrame {
  PyObject_HEAD
  _PyInterpreterFrame* frame; // Borrowed reference
  PyObject* locals;
} THPPyInterpreterFrame;

THPPyInterpreterFrame* THPPyInterpreterFrame_New(_PyInterpreterFrame* frame);

#define DECLARE_PYOBJ_ATTR(name) \
static PyObject* THPPyInterpreterFrame_##name(THPPyInterpreterFrame* self, PyObject* _noargs) { \
  PyObject* res = (PyObject*)self->frame->name; \
  Py_XINCREF(res); \
  return res; \
}

#if IS_PYTHON_3_12_PLUS
DECLARE_PYOBJ_ATTR(f_funcobj)
#else
DECLARE_PYOBJ_ATTR(f_func)
#endif

DECLARE_PYOBJ_ATTR(f_globals)
DECLARE_PYOBJ_ATTR(f_builtins)

static PyObject* THPPyInterpreterFrame_f_locals(THPPyInterpreterFrame* self, PyObject* _noargs) {
  DEBUG_NULL_CHECK(self->locals);
  Py_XINCREF(self->locals);
  return self->locals;
}

#if IS_PYTHON_3_13_PLUS
DECLARE_PYOBJ_ATTR(f_executable)
#else
DECLARE_PYOBJ_ATTR(f_code)
#endif

DECLARE_PYOBJ_ATTR(frame_obj)

#undef DECLARE_PYOBJ_ATTR

static THPPyInterpreterFrame* THPPyInterpreterFrame_previous(THPPyInterpreterFrame* self, PyObject* _noargs) {
  THPPyInterpreterFrame* res = THPPyInterpreterFrame_New(self->frame->previous);
  return res;
}

// This is not a true attribute of the class but we do access it in python and it is hard to implement
// on the python side, so do it here:
static PyObject* THPPyInterpreterFrame_f_lasti(THPPyInterpreterFrame* self, PyObject* _noargs) {
  return PyLong_FromLong(_PyInterpreterFrame_LASTI(self->frame));
}

static PyObject* THPPyInterpreterFrame_f_lineno(THPPyInterpreterFrame* self, PyObject* _noargs) {
  if (!self->frame->frame_obj) {
    return PyLong_FromLong(F_CODE(self->frame)->co_firstlineno);
  }
  int lineno = PyFrame_GetLineNumber(self->frame->frame_obj);
  if (lineno < 0) {
    Py_RETURN_NONE;
  }
  return PyLong_FromLong(lineno);
}

static PyObject* THPPyInterpreterFrame_f_back(THPPyInterpreterFrame* self, PyObject* _noargs) {
  if (!self->frame->frame_obj) {
    Py_RETURN_NONE;
  }
  return (PyObject*)PyFrame_GetBack(self->frame->frame_obj);
}

// NOLINTNEXTLINE(cppcoreguidelines-avoid-c-arrays,cppcoreguidelines-avoid-non-const-global-variables,modernize-avoid-c-arrays)
static struct PyGetSetDef THPPyInterpreterFrame_properties[] = {
#if IS_PYTHON_3_12_PLUS
    {"f_func", (getter)THPPyInterpreterFrame_f_funcobj, NULL, NULL, NULL},
#else
    {"f_func", (getter)THPPyInterpreterFrame_f_func, NULL, NULL, NULL},
#endif
    {"f_globals", (getter)THPPyInterpreterFrame_f_globals, NULL, NULL, NULL},
    {"f_builtins", (getter)THPPyInterpreterFrame_f_builtins, NULL, NULL, NULL},
    {"f_locals", (getter)THPPyInterpreterFrame_f_locals, NULL, NULL, NULL},
#if IS_PYTHON_3_13_PLUS
    {"f_code", (getter)THPPyInterpreterFrame_f_executable, NULL, NULL, NULL},
#else
    {"f_code", (getter)THPPyInterpreterFrame_f_code, NULL, NULL, NULL},
#endif
    {"frame_obj", (getter)THPPyInterpreterFrame_frame_obj, NULL, NULL, NULL},
    {"previous", (getter)THPPyInterpreterFrame_previous, NULL, NULL, NULL},
    {"f_lasti", (getter)THPPyInterpreterFrame_f_lasti, NULL, NULL, NULL},
    {"f_lineno", (getter)THPPyInterpreterFrame_f_lineno, NULL, NULL, NULL},
    {"f_back", (getter)THPPyInterpreterFrame_f_back, NULL, NULL, NULL},
    {NULL}};

static PyTypeObject THPPyInterpreterFrameType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "torch._C._dynamo.eval_frame._PyInterpreterFrame",
    .tp_basicsize = sizeof(THPPyInterpreterFrame),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_getset = THPPyInterpreterFrame_properties,
};


THPPyInterpreterFrame* THPPyInterpreterFrame_New(_PyInterpreterFrame* frame) {
  PyTypeObject* type = (PyTypeObject*)&THPPyInterpreterFrameType;
  THPPyInterpreterFrame* self = (THPPyInterpreterFrame*)type->tp_alloc(type, 0);
  if (!self)
    return NULL;
  self->frame = frame;
  self->locals = NULL;
  return self;
}


#else
#define THP_EVAL_API_FRAME_OBJECT PyFrameObject

static int
THP_PyFrame_FastToLocalsWithError(THP_EVAL_API_FRAME_OBJECT *frame, int *free_vars_copied) {
  return PyFrame_FastToLocalsWithError(frame);
}
#endif

static PyObject* _custom_eval_frame_shim(
    PyThreadState* tstate,
    THP_EVAL_API_FRAME_OBJECT* frame,
    int throw_flag);
static PyObject* _custom_eval_frame(
    PyThreadState* tstate,
    THP_EVAL_API_FRAME_OBJECT* frame,
    int throw_flag,
    PyObject* callback,
    int* should_clear_frame);
static PyObject *(*previous_eval_frame)(PyThreadState *tstate,
                                        THP_EVAL_API_FRAME_OBJECT* frame, int throw_flag) = NULL;

#if PY_VERSION_HEX >= 0x03090000
static PyObject* custom_eval_frame_shim(
    PyThreadState* tstate,
    THP_EVAL_API_FRAME_OBJECT* frame,
    int throw_flag) {
  return _custom_eval_frame_shim(tstate, frame, throw_flag);
}
#else
static PyObject* custom_eval_frame_shim(THP_EVAL_API_FRAME_OBJECT* frame, int throw_flag) {
  PyThreadState* tstate = PyThreadState_GET();
  return _custom_eval_frame_shim(tstate, frame, throw_flag);
}
#endif

inline static PyObject* eval_frame_default(
    PyThreadState* tstate,
    THP_EVAL_API_FRAME_OBJECT* frame,
    int throw_flag) {
#if PY_VERSION_HEX >= 0x03090000
  if (tstate == NULL) {
    tstate = PyThreadState_GET();
  }
  if (previous_eval_frame) {
    return previous_eval_frame(tstate, frame, throw_flag);
  }
  else {
    return _PyEval_EvalFrameDefault(tstate, frame, throw_flag);
  }
#else
  return _PyEval_EvalFrameDefault(frame, throw_flag);
#endif
}

inline static void enable_eval_frame_shim(PyThreadState* tstate) {
#if PY_VERSION_HEX >= 0x03090000
  if (_PyInterpreterState_GetEvalFrameFunc(tstate->interp) !=
      &custom_eval_frame_shim) {
    DEBUG_CHECK(previous_eval_frame == NULL);
    previous_eval_frame = _PyInterpreterState_GetEvalFrameFunc(tstate->interp);
    _PyInterpreterState_SetEvalFrameFunc(tstate->interp,
                                         &custom_eval_frame_shim);
  }
#else
  if (tstate->interp->eval_frame != &custom_eval_frame_shim) {
    // First call
    tstate->interp->eval_frame = &custom_eval_frame_shim;
  }
#endif
}

inline static void enable_eval_frame_default(PyThreadState* tstate) {
#if PY_VERSION_HEX >= 0x03090000
  if (_PyInterpreterState_GetEvalFrameFunc(tstate->interp) !=
      previous_eval_frame) {
    DEBUG_CHECK(previous_eval_frame != NULL);
    _PyInterpreterState_SetEvalFrameFunc(tstate->interp,
                                         previous_eval_frame);
    previous_eval_frame = NULL;
  }
#else
  if (tstate->interp->eval_frame != &_PyEval_EvalFrameDefault) {
    // First call
    tstate->interp->eval_frame = &_PyEval_EvalFrameDefault;
  }
#endif
}


inline static const char* get_frame_name(THP_EVAL_API_FRAME_OBJECT* frame) {
  // Returns the C string name of the current frame.
  DEBUG_CHECK(PyUnicode_Check(F_CODE(frame)->co_name));
  return PyUnicode_AsUTF8(F_CODE(frame)->co_name);
}

static inline PyObject* call_callback(
    PyObject* callable,
    THP_EVAL_API_FRAME_OBJECT* _frame,
    PyObject* locals,
    CacheEntry* cache_entry,
    FrameState* frame_state) {

// remember to update the type signature for DynamoCallbackFn.__call__ in torch/_dynamo/types.py
// if this function changes
#if IS_PYTHON_3_11_PLUS
  THPPyInterpreterFrame* frame = THPPyInterpreterFrame_New(_frame);
  if (frame == NULL) {
    return NULL;
  }
  frame->locals = locals;
#else
  PyObject* frame = Py_NewRef(_frame);
#endif

  PyObject* cache_entry_pyobj = CacheEntry_to_obj(cache_entry);
  PyObject* res = PyObject_CallFunction(
    callable,
    "OOO",
    frame,
    cache_entry_pyobj,
    frame_state);
  Py_DECREF(frame);
  Py_DECREF(cache_entry_pyobj);
  return res;
}

static inline void clear_old_frame_if_python_312_plus(
  PyThreadState* tstate,
  THP_EVAL_API_FRAME_OBJECT* frame) {
#if IS_PYTHON_3_12_PLUS

  THP_PyFrame_Clear(frame);
  THP_PyThreadState_PopFrame(tstate, frame);

#endif
}

inline static PyObject* eval_custom_code_impl(
    PyThreadState* tstate,
    THP_EVAL_API_FRAME_OBJECT* frame,
    PyCodeObject* code,
    int throw_flag,
    int free_vars_copied) {

  DEBUG_NULL_CHECK(tstate);
  DEBUG_NULL_CHECK(frame);
  DEBUG_NULL_CHECK(code);

#if IS_PYTHON_3_11_PLUS

  // Generate Python function object and _PyInterpreterFrame in a way similar to
  // https://github.com/python/cpython/blob/e715da6db1d1d70cd779dc48e1ba8110c51cc1bf/Python/ceval.c#L1130
#if IS_PYTHON_3_12_PLUS
  PyFunctionObject* old_func = (PyFunctionObject*) frame->f_funcobj;
  size_t size = code->co_framesize;
#else
  PyFunctionObject* old_func = frame->f_func;
  size_t size = code->co_nlocalsplus + code->co_stacksize + FRAME_SPECIALS_SIZE;
#endif

  PyFunctionObject* func = _PyFunction_CopyWithNewCode(old_func, code);
  if (func == NULL) {
    return NULL;
  }

  THP_EVAL_API_FRAME_OBJECT* shadow = THP_PyThreadState_BumpFramePointerSlow(tstate, size);
  if (shadow == NULL) {
    Py_DECREF(func);
    return NULL;
  }

  Py_INCREF(func);
  // consumes reference to func
#if IS_PYTHON_3_12_PLUS
  _PyFrame_Initialize(shadow, func, NULL, code, 0);
#else
  _PyFrame_InitializeSpecials(shadow, func, NULL, code->co_nlocalsplus);
#endif

  PyObject** fastlocals_old = frame->localsplus;
  PyObject** fastlocals_new = shadow->localsplus;
  Py_ssize_t n_old = F_CODE(frame)->co_nlocalsplus;
  Py_ssize_t n_new = code->co_nlocalsplus;

  // localsplus are XINCREF'd by default eval frame, so all values must be valid.
#if !(IS_PYTHON_3_12_PLUS)
  // _PyFrame_Initialize in 3.12 already does this
  for (int i = 0; i < code->co_nlocalsplus; i++) {
    fastlocals_new[i] = NULL;
  }
#endif

  // for 3.11+, if free_vars_copied is true, we do not need to
  // run the first COPY_FREE_VARS since THP_PyFrame_FastToLocalsWithError
  // already did the equivalent action.
  if (free_vars_copied && _Py_OPCODE(_PyCode_CODE(F_CODE(shadow))[0]) == COPY_FREE_VARS) {
    PREV_INSTR(shadow) = _PyCode_CODE(F_CODE(shadow));
  }

#else

  THP_EVAL_API_FRAME_OBJECT* shadow = PyFrame_New(tstate, code, frame->f_globals, NULL);
  if (shadow == NULL) {
    return NULL;
  }

  PyObject** fastlocals_old = frame->f_localsplus;
  PyObject** fastlocals_new = shadow->f_localsplus;
  Py_ssize_t n_old = F_CODE(frame)->co_nlocals + PyCode_GetNFreevars(F_CODE(frame)) + PyCode_GetNCellvars(F_CODE(frame));
  Py_ssize_t n_new = code->co_nlocals + PyCode_GetNFreevars(code) + PyCode_GetNCellvars(code);

#endif

  // ============== Initialize new frame from old frame ============
  // Python internal for executing a function:
  //  1. CPython interpreter first creates an empty frame according to the code object
  //  2. CPython interpreter initializes the frame by filling arguments/free variables into frame and initializing cell variables
  //  3. CPython interpreter executes the code object
  //
  // Dynamo hooks the 3th step: before executing the code object, Dynamo transforms the code object into a new code object. Then, the old frame is not suitable for executing the new code. Therefore, Dynamo needs to manually create and initialize a new frame to execute the new code.
  // The main task is to copy data in old frame to new frame, concerning a storage space named `localsplus`.
  //
  // localsplus storage is an array with the following layout:
  // |   args   |   new_locals    |    cell_variables |   free_variables    |
  // | <--- from left to right, index from 0 to n - 1 ---> |
  // code.co_varnames == args + new_locals, code.co_nlocals == len(code.co_varnames)
  // code.co_freevars == free_variables
  // In Python 3.10 and lower, `n == code.co_nlocals + len(code.co_cellvars) + len(code.co_freevars)` (Python expression)
  // In Python 3.11 and higher, `n <= code.co_nlocals + len(code.co_cellvars) + len(code.co_freevars)` (Python expression). There is an extra field in Python C-API: `n == code->co_nlocalsplus` (C expression) to retrieve the length of array.
  // The complexity happens if an argument becomes a cell variable:
  //  In Python 3.10 and lower, `code.co_cellvars == cell_variables`, and the corresponding slot in args becomes `NULL`.
  //  In Python 3.11 and higher, `code.co_cellvars > cell_variables`, that cell variable is still stored in args, with a flag set in corresponding item's `co_localspluskinds` .
  //
  // ideally, we need to look up new localsplus from old localsplus by name:
  // for i, name, value in enumerate(localsplusnames_old):
  //   if value != NULL: (NULL happens for new local variables and arguments that becomes cell variables)
  //     name_to_idx[name] = i
  // for i, name in enumerate(localsplusnames_new):
  //  if name in name_to_idx:
  //    fastlocals_new[i] = fastlocals_old[name_to_idx[name]]
  //
  // The above process of building a `name_to_idx` mapping is expensive.
  // Dynamo makes the following assumptions:
  //  1. new code has the same arguments as the old code (both the number and the order)
  //  2. new code has the same cell variables as the old code (both the number and the order)
  //  3. new code has the same free variables as the old code (both the number and the order)
  //  The only flexibility lies in new local variables: new code can introduce their own variables.
  // With these assumptions, Dynamo can copy data directly by index. Dynamo just needs to take care of copying cell variables correctly.
  // To avoid runtime cost, the assumptions are checked when we first generate the code object in pytorch/torch/_dynamo/convert_frame.py .


  // copy args
  // according to https://docs.python.org/3/library/inspect.html , `co_argcount` is the number of arguments (not including keyword only arguments, * or ** args). so we need to add `co_kwonlyargcount` and `co_flags` to get the total number of arguments.
  // !!(F_CODE(frame)->co_flags & CO_VARARGS) is 1 if the function has *args, 0 otherwise
  // !!(F_CODE(frame)->co_flags & CO_VARKEYWORDS) is 1 if the function has **kwargs, 0 otherwise
  // they convert bit flags to 0 or 1, and avoid branching.
  // This is performance critical code, so we really care about performance.
  Py_ssize_t total_argcount_old = F_CODE(frame)->co_argcount + F_CODE(frame)->co_kwonlyargcount + !!(F_CODE(frame)->co_flags & CO_VARARGS) + !!(F_CODE(frame)->co_flags & CO_VARKEYWORDS);

  for (Py_ssize_t i = 0; i < total_argcount_old; i++) {
    Py_XINCREF(fastlocals_old[i]);
    fastlocals_new[i] = fastlocals_old[i];
  }

  // copy free vars
  Py_ssize_t nfrees_old = PyCode_GetNFreevars(F_CODE(frame));

  for (Py_ssize_t i = 0; i < nfrees_old; i++) {
    Py_XINCREF(fastlocals_old[n_old - 1 - i]);
    fastlocals_new[n_new - 1 - i] = fastlocals_old[n_old - 1 - i];
  }

  // copy cell vars, from high index to low index, until it meets a variable that is not cell variable.
  for (Py_ssize_t i = n_old - nfrees_old - 1, j = n_new - nfrees_old - 1; i >= total_argcount_old; i--, j--) {

  // conditional test to tell if a variable is not a cell variable
  // this is straightforward in Python 3.11 and higher, as there are bit flags in `co_localspluskinds` to tell if a variable is a cell variable.
  // in Python 3.10 and lower, essentially we are checking if a variable is a new local variable (because of the layout mentioned above, the first variable that is not cell variable is the first new local variable). the corresponding slot in `flocalsplus` is NULL for new local variables.
#if IS_PYTHON_3_11_PLUS
    if(!(_PyLocals_GetKind(F_CODE(frame)->co_localspluskinds, i) & CO_FAST_CELL))
    {
      break;
    }
#else
    if(fastlocals_old[i] == NULL)
    {
      break;
    }
#endif

    Py_XINCREF(fastlocals_old[i]);
    fastlocals_new[j] = fastlocals_old[i];
  }

  // NOTE: if you want to evaluate frame instead of shadow in 3.12+,
  // you need to clear_old_frame_if_python_312_plus the shadow frame BEFORE
  // calling eval_frame_default (i.e. here) and comment out the
  // clear_old_frame_if_python_312_plus call on the original frame.

  PyObject* result = eval_frame_default(tstate, shadow, throw_flag);

#if IS_PYTHON_3_12_PLUS

  // frame is cleared by caller
  Py_DECREF(func);

#elif IS_PYTHON_3_11_PLUS

  // In 3.11, shadow has is_entry set to true, so _PyEvalFrameClearAndPop is not called,
  // so we manually clear and pop the shadow frame.
  THP_PyFrame_Clear(shadow);
  THP_PyThreadState_PopFrame(tstate, shadow);
  Py_DECREF(func);

#else

  Py_DECREF(shadow);

#endif

  return result;
}

// This wrapper function adds a profiler event
inline static PyObject* eval_custom_code(
    PyThreadState* tstate,
    THP_EVAL_API_FRAME_OBJECT* frame,
    PyCodeObject* code,
    int throw_flag,
    int free_vars_copied) {
  const char* trace_id = compile_context;
  _PytorchRecordFunctionState* rf = _pytorch_record_function_enter_with_context("Torch-Compiled Region", trace_id);
  PyObject* result = eval_custom_code_impl(
    tstate,
    frame,
    code,
    throw_flag,
    free_vars_copied
  );
  _pytorch_record_function_exit(rf);
  return result;
}

static PyObject* _custom_eval_frame_shim(
    PyThreadState* tstate,
    THP_EVAL_API_FRAME_OBJECT* frame,
    int throw_flag) {
  // Shims logic into one of three states. Can probably be refactored into a
  // single func, later:
  //  - None: disables TorchDynamo
  //  - False: run-only mode (reuse existing compiles)
  //  - Python callable(): enables TorchDynamo
  PyObject* callback = eval_frame_callback_get();

  if (callback == Py_None) {
    return eval_frame_default(tstate, frame, throw_flag);
  }

  int should_clear_frame = 0;
  PyObject* result = _custom_eval_frame(tstate, frame, throw_flag, callback, &should_clear_frame);
  if (should_clear_frame) {
    clear_old_frame_if_python_312_plus(tstate, frame);
  }
  return result;
}

static PyObject* skip_code_recursive_flag;

// NOTE: In 3.12+, the frame evaluation function (callee) is responsible for clearing/popping
// the frame, meaning that unless we default evaluate the original frame,
// we are responsible for clearing it - via clear_old_frame_if_python_312_plus.
// The should_clear_frame flag is used to indicate whether the frame should be
// cleared by _custom_eval_frame's caller.
// Generally should_clear_frame should be set if and only we don't eval_frame_default.
static PyObject* _custom_eval_frame(
    PyThreadState* tstate,
    THP_EVAL_API_FRAME_OBJECT* frame,
    int throw_flag,
    PyObject* callback,
    int* should_clear_frame) {
#if IS_PYTHON_3_11_PLUS
  DEBUG_TRACE(
      "begin %s %s %i %i",
      get_frame_name(frame),
      PyUnicode_AsUTF8(F_CODE(frame)->co_filename),
      F_CODE(frame)->co_firstlineno,
      _PyInterpreterFrame_LASTI(frame));
#else
  DEBUG_TRACE(
      "begin %s %s %i %i %i",
      get_frame_name(frame),
      PyUnicode_AsUTF8(F_CODE(frame)->co_filename),
      frame->f_lineno,
      frame->f_lasti,
      frame->f_iblock);
#endif

  if (throw_flag) {
    // When unwinding generators, eval frame is called with throw_flag ==
    // true.  Frame evaluation is supposed to continue unwinding by propagating
    // the exception.  Dynamo doesn't really know how to do this, nor does it
    // really want to do this, because there's unlikely any code to capture
    // (you're going to immediately quit out of the frame, perhaps running
    // some unwinding logic along the way).  So we just run the default
    // handler in this case.
    //
    // NB: A previous version of this patch returned NULL.  This is wrong,
    // because returning NULL is *different* from unwinding an exception.
    // In particular, you will not execute things like context manager
    // __exit__ if you just return NULL.
    //
    // NB: It's /conceivable/ that you might want to actually still call the
    // Dynamo callback when throw_flag == TRUE, to give Dynamo a chance to
    // do any stack unwinding code.  But this is not really useful because
    // (1) Dynamo doesn't actually know how to do stack unwinding, so it would
    // immediately skip the frame, and (2) even if it did, this would only
    // be profitable if there was tensor code in the unwinding code.  Seems
    // unlikely.
    DEBUG_TRACE("throw %s", get_frame_name(frame));
    return eval_frame_default(tstate, frame, throw_flag);
  }

  ExtraState* extra = get_extra_state(F_CODE(frame));
  if (extra == SKIP_CODE || (callback == Py_False && extra == NULL)) {
    DEBUG_TRACE("skip %s", get_frame_name(frame));
    return eval_frame_default(tstate, frame, throw_flag);
  }
  if (extra == SKIP_CODE_RECURSIVE) {
    DEBUG_TRACE("skip recursive %s", get_frame_name(frame));
    eval_frame_callback_set(Py_None);
    PyObject* result = eval_frame_default(tstate, frame, throw_flag);
    eval_frame_callback_set(callback);
    return result;
  }

  if (extra == NULL) {
    extra = init_and_set_extra_state(F_CODE(frame));
  }


  int free_vars_copied = 0;
  #if IS_PYTHON_3_12_PLUS
  PyObject *locals = get_framelocals_mapping(frame);
  #else
  if (THP_PyFrame_FastToLocalsWithError(frame, &free_vars_copied) < 0) {
    DEBUG_TRACE("error %s", get_frame_name(frame));
    *should_clear_frame = 1;
    return NULL;
  }
  PyObject *locals = frame->f_locals;
  Py_INCREF(locals);
  #endif

  PyObject* backend = get_backend(callback);

  // A callback of Py_False indicates "run only" mode, the cache is checked, but
  // we never compile.
  if (callback == Py_False) {
    DEBUG_TRACE("In run only mode %s", get_frame_name(frame));
    _PytorchRecordFunctionState* rf = _pytorch_record_function_enter(cache_lookup_profiler_str);
    PyObject* maybe_cached_code = lookup(extra, locals, backend);
    _pytorch_record_function_exit(rf);

    Py_DECREF(locals);

    if (maybe_cached_code == NULL) {
      // guard eval failed, keep propagating
      *should_clear_frame = 1;
      return NULL;
    } else if (maybe_cached_code == Py_None) {
      DEBUG_TRACE("cache miss %s", get_frame_name(frame));
      return eval_frame_default(tstate, frame, throw_flag);
    }
    PyCodeObject* cached_code = (PyCodeObject*)maybe_cached_code;
    // used cached version
    DEBUG_TRACE("cache hit %s", get_frame_name(frame));
    *should_clear_frame = 1;
    return eval_custom_code(tstate, frame, cached_code, throw_flag, 0);
  }
  DEBUG_CHECK(PyDict_CheckExact(locals));
  DEBUG_CHECK(PyDict_CheckExact(frame->f_globals));
  DEBUG_CHECK(PyDict_CheckExact(frame->f_builtins));

  // We don't run the current custom_eval_frame behavior for guards.
  // So we temporarily set the callback to Py_None to drive the correct behavior
  // in the shim.
  eval_frame_callback_set(Py_None);

  _PytorchRecordFunctionState* rf = _pytorch_record_function_enter(cache_lookup_profiler_str);
  PyObject* maybe_cached_code = lookup(extra, locals, backend);
  _pytorch_record_function_exit(rf);
  if (maybe_cached_code == NULL) {
    // Python error
    *should_clear_frame = 1;
    Py_DECREF(locals);
    return NULL;
  } else if (maybe_cached_code != Py_None) {
    PyCodeObject* cached_code = (PyCodeObject*)maybe_cached_code;
    // used cached version
    DEBUG_TRACE("cache hit %s", get_frame_name(frame));
    // Re-enable custom behavior
    eval_frame_callback_set(callback);
    *should_clear_frame = 1;
    Py_DECREF(locals);
    return eval_custom_code(tstate, frame, cached_code, throw_flag, free_vars_copied);
  }
  // cache miss
  CacheEntry* cache_entry = extract_cache_entry(extra);
  FrameState* frame_state = extract_frame_state(extra);
  PyObject* result =
      call_callback(callback, frame, locals, cache_entry, frame_state);
  Py_DECREF(locals);
  if (result == NULL) {
    // internal exception, returning here will leak the exception into user code
    // this is useful for debugging -- but we dont want it to happen outside of
    // testing
    // NB: we intentionally DO NOT re-enable custom behavior to prevent
    // cascading failure from internal exceptions.  The upshot is if
    // Dynamo barfs, that's it for Dynamo, even if you catch the exception
    // inside the torch.compile block we won't try to Dynamo anything else.
    *should_clear_frame = 1;
    return NULL;
  } else if (result == skip_code_recursive_flag) {
    // Dynamo returned skip_code_recursive_flag, so we should recursively skip code.
    DEBUG_TRACE("create skip recursive %s", get_frame_name(frame));
    set_extra_state(F_CODE(frame), SKIP_CODE_RECURSIVE);
    PyObject* r = eval_frame_default(tstate, frame, throw_flag);
    // Re-enable custom behavior
    eval_frame_callback_set(callback);
    return r;
  } else if (result != Py_None) {
    DEBUG_TRACE("create cache %s", get_frame_name(frame));

    // NB: We could use extract_cache_entry to get the cache_entry, but
    // extract_cache_entry returns a borrowed reference. Modifying a borrowed
    // reference seems wrong. Therefore, we directly access the
    // extra->cache_entry. extra wont be NULL here.
    CacheEntry* new_cache_entry = create_cache_entry(extra, result, backend);
    Py_DECREF(result);

    // Update the existing cache_entry on the extra object. This extra object is
    // sitting on the extra scratch space, we are just changing the cache_entry
    // ptr. As a result, extra now becomes the owner of CacheEntry object. This
    // will be cleaned up when set_extra_state is called.
    // Re-enable custom behavior
    eval_frame_callback_set(callback);
    *should_clear_frame = 1;
    return eval_custom_code(tstate, frame, CacheEntry_get_code(new_cache_entry), throw_flag, free_vars_copied);
  } else {
    DEBUG_TRACE("create skip %s", get_frame_name(frame));
    Py_DECREF(result);
    set_extra_state(F_CODE(frame), SKIP_CODE);
    // Re-enable custom behavior
    eval_frame_callback_set(callback);
    return eval_frame_default(tstate, frame, throw_flag);
  }
}

#else // IS_PYTHON_3_14_PLUS

// Fake definitions for everything we removed

typedef struct THPPyInterpreterFrame {
  PyObject_HEAD
  _PyInterpreterFrame* frame; // Borrowed reference
} THPPyInterpreterFrame;

inline static void enable_eval_frame_shim(PyThreadState* tstate) {}
inline static void enable_eval_frame_default(PyThreadState* tstate) {}

static struct PyGetSetDef THPPyInterpreterFrame_properties[] = {NULL};

static PyTypeObject THPPyInterpreterFrameType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "torch._C._dynamo.eval_frame._PyInterpreterFrame",
    .tp_basicsize = sizeof(THPPyInterpreterFrame),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_getset = THPPyInterpreterFrame_properties,
};

#endif // CPython 3.14

static PyObject* increment_working_threads(PyThreadState* tstate) {
  active_dynamo_threads = active_dynamo_threads + 1;
  if (active_dynamo_threads > 0) {
    enable_eval_frame_shim(tstate);
  }
  Py_RETURN_NONE;
}

static PyObject* decrement_working_threads(PyThreadState* tstate) {
  if (active_dynamo_threads > 0) {
    active_dynamo_threads = active_dynamo_threads - 1;
    if (active_dynamo_threads == 0) {
      enable_eval_frame_default(tstate);
    }
  }
  Py_RETURN_NONE;
}

static PyObject* set_eval_frame(PyObject* new_callback, PyThreadState* tstate) {
  // Change the eval frame callback and return the old one
  //  - None: disables TorchDynamo
  //  - False: run-only mode (reuse existing compiles)
  //  - Python callable(): enables TorchDynamo
  PyObject* old_callback = eval_frame_callback_get();

  // owned by caller
  Py_INCREF(old_callback);

  if (old_callback != Py_None && new_callback == Py_None) {
    decrement_working_threads(tstate);
  } else if (old_callback == Py_None && new_callback != Py_None) {
    increment_working_threads(tstate);
  }

  Py_INCREF(new_callback);
  Py_DECREF(old_callback);

  // Set thread local callback. This will drive behavior of our shim, if/when it
  // is installed.
  eval_frame_callback_set(new_callback);

  return old_callback;
}

static PyObject* set_eval_frame_py(PyObject* dummy, PyObject* callback) {
  if (callback != Py_None && callback != Py_False &&
      !PyCallable_Check(callback)) {
    DEBUG_TRACE0("arg error");
    PyErr_SetString(PyExc_TypeError, "expected a callable");
    return NULL;
  }
  DEBUG_TRACE(
      "python enabled=%d and is run_only=%d",
      callback != Py_None,
      callback == Py_False);
  return set_eval_frame(callback, PyThreadState_GET());
}

static PyObject* reset_code(PyObject* dummy, PyObject* code) {
  if (!PyCode_Check(code)) {
    DEBUG_TRACE0("arg error");
    PyErr_SetString(PyExc_TypeError, "expected a code object");
    return NULL;
  }

  // set_extra_state destroys the existing object on extra scratch space.
  set_extra_state((PyCodeObject*)code, NULL);
  Py_RETURN_NONE;
}

static PyObject* unsupported(PyObject* dummy, PyObject* args) {
  // a dummy C function used in testing
  PyObject* obj1 = NULL;
  PyObject* obj2 = NULL;
  if (!PyArg_ParseTuple(args, "OO", &obj1, &obj2)) {
    return NULL;
  }
  Py_INCREF(obj2);
  return obj2;
}

static PyObject* skip_code(PyObject* dummy, PyObject* obj) {
  if (!PyCode_Check(obj)) {
    PyErr_SetString(PyExc_TypeError, "expected a code object");
    return NULL;
  }

  // set_extra_state destroys the existing object on extra scratch space.
  set_extra_state((PyCodeObject*)obj, SKIP_CODE);
  Py_RETURN_NONE;
}

static PyObject* set_guard_error_hook(PyObject* dummy, PyObject* obj) {
  if (obj == Py_None) {
    obj = NULL;
  }
  Py_XSETREF(guard_error_hook, Py_XNewRef(obj));
  Py_RETURN_NONE;
}

static PyObject* set_context_frame(PyObject* dummy, PyObject* obj) {
    int frame_id, frame_compile_id, attempt;
    if (!PyArg_ParseTuple(obj, "iii", &frame_id, &frame_compile_id, &attempt)) {
        PyErr_SetString(PyExc_TypeError, "Expected three integers");
        return NULL;
    }
    if (attempt == 0) {
      sprintf(compile_context, "%d/%d", frame_id, frame_compile_id);
    } else {
      sprintf(compile_context, "%d/%d_%d", frame_id, frame_compile_id, attempt);
    }
    Py_RETURN_NONE;
}

static PyMethodDef _methods[] = {
    {"set_eval_frame", set_eval_frame_py, METH_O, NULL},
    {"reset_code", reset_code, METH_O, NULL},
    {"unsupported", unsupported, METH_VARARGS, NULL},
    {"skip_code", skip_code, METH_O, NULL},
    {"set_guard_error_hook", set_guard_error_hook, METH_O, NULL},
    {"set_context_frame", set_context_frame, METH_O, NULL},
    {NULL, NULL, 0, NULL}};

static struct PyModuleDef _module = {
    PyModuleDef_HEAD_INIT,
    "torch._C._dynamo.eval_frame",
    "Module containing hooks to override eval_frame",
    -1,
    _methods};

#if IS_PYTHON_3_12_PLUS
#define _PyEval_RequestCodeExtraIndex PyUnstable_Eval_RequestCodeExtraIndex
#endif

PyObject* torch_c_dynamo_eval_frame_init(void) {
  extra_index = _PyEval_RequestCodeExtraIndex(destroy_extra_state);
  if (extra_index < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "dynamo: unable to register extra index");
    return NULL;
  }

  int result = PyThread_tss_create(&eval_frame_callback_key);
  CHECK(result == 0);

  Py_INCREF(Py_None);
  eval_frame_callback_set(Py_None);

  PyObject* module = PyModule_Create(&_module);
  if (module == NULL) {
    return NULL;
  }

#if IS_PYTHON_3_11_PLUS
  if (PyType_Ready(&THPPyInterpreterFrameType) < 0) {
    return NULL;
  }
  Py_INCREF(&THPPyInterpreterFrameType);
  if (PyModule_AddObject(module, "_PyInterpreterFrame", (PyObject*)&THPPyInterpreterFrameType) != 0) {
    return NULL;
  }
#endif

  skip_code_recursive_flag = PyObject_New(PyObject, &PyBaseObject_Type);
  if (skip_code_recursive_flag == NULL) {
    return NULL;
  }
  if (PyModule_AddObject(module, "skip_code_recursive_flag", skip_code_recursive_flag) != 0) {
    return NULL;
  }

  return module;
}
