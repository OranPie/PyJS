from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import functools
import heapq
import json
import math
import os
import queue as _queue_mod
import random
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from .core import JSTypeError, js_to_py, py_to_js, _JS_SMALL_INTS, _JS_NAN, _JS_POS_INF, _JS_NEG_INF
from .plugin import PyJSPlugin, PluginContext
from .lexer import Lexer
from .parser import N, Parser
from .trace import configure as _configure_trace, get_logger, push_depth, pop_depth, TRACE, _any_enabled as _TRACE_ACTIVE
from .values import (
    JsValue, JsProxy, UNDEFINED, JS_NULL, JS_TRUE, JS_FALSE,
    SYMBOL_ITERATOR, SYMBOL_TO_PRIMITIVE, SYMBOL_HAS_INSTANCE,
    SYMBOL_TO_STRING_TAG, SYMBOL_ASYNC_ITERATOR, SYMBOL_SPECIES,
    SYMBOL_MATCH, SYMBOL_REPLACE, SYMBOL_SPLIT, SYMBOL_SEARCH,
    SYMBOL_IS_CONCAT_SPREADABLE, SYMBOL_DISPOSE, SYMBOL_ASYNC_DISPOSE,
    _symbol_id_counter, _symbol_registry,
    _js_regex_to_python,
    SK_ITERATOR, SK_TO_PRIMITIVE, SK_HAS_INSTANCE,
    SK_TO_STRING_TAG, SK_ASYNC_ITERATOR, SK_SPECIES,
    SK_MATCH, SK_REPLACE, SK_SPLIT, SK_SEARCH,
    SK_IS_CONCAT_SPREADABLE, SK_DISPOSE, SK_ASYNC_DISPOSE,
)
from .environment import Environment, _TDZ_SENTINEL
from .exceptions import _JSBreak, _JSContinue, _JSReturn, _JSError, flatten_one
from .generators import JsGenerator, JsAsyncGenerator
from .builtins_core import register_core_builtins
from .builtins_object import register_object_builtins
from .builtins_advanced import register_advanced_builtins
from .builtins_promise import register_promise_builtins
from .builtins_typed import register_typed_builtins

# Pre-allocated singleton for `return;` (no argument) — avoids creating new _JSReturn
_RETURN_UNDEFINED = _JSReturn(UNDEFINED)

_log_exec = get_logger("exec")
_log_eval = get_logger("eval")
_log_call = get_logger("call")
_log_prop = get_logger("prop")
_log_event = get_logger("event")
_log_promise = get_logger("promise")
_log_scope = get_logger("scope")
_log_error = get_logger("error")
_log_module = get_logger("module")
_log_async = get_logger("async")
_log_coerce = get_logger("coerce")
_log_timer = get_logger("timer")
_log_proxy = get_logger("proxy")


# ============================================================================
#  Interpreter
# ============================================================================
# ============================================================================

class Interpreter:
    ARRAY_METHODS = frozenset({'push', 'pop', 'shift', 'unshift', 'indexOf', 'includes', 'join', 'slice', 'splice', 'concat', 'reverse', 'sort', 'forEach', 'map', 'filter', 'reduce', 'find', 'flat', 'flatMap', 'every', 'some', 'fill', 'copyWithin', 'toString', 'at', 'findIndex', 'findLast', 'findLastIndex', 'reduceRight', 'lastIndexOf', 'toSorted', 'toReversed', 'toSpliced', 'with', 'keys', 'values', 'entries'})
    STRING_METHODS = frozenset({'charAt', 'charCodeAt', 'indexOf', 'includes', 'slice', 'substring', 'toLowerCase', 'toUpperCase', 'trim', 'split', 'replace', 'replaceAll', 'startsWith', 'endsWith', 'padStart', 'padEnd', 'repeat', 'match', 'search', 'concat', 'lastIndexOf', 'normalize', 'at', 'matchAll', 'trimStart', 'trimLeft', 'trimEnd', 'trimRight', 'codePointAt', 'isWellFormed', 'toWellFormed', 'localeCompare', 'toString', 'valueOf'})
    NUMBER_METHODS = frozenset({'toFixed', 'toPrecision', 'toString', 'toLocaleString', 'valueOf', 'toExponential'})
    PROMISE_METHODS = frozenset({'then', 'catch', 'finally'})
    EVENT_LOOP_LIMIT = 10000

    MAX_CALL_DEPTH = 200
    MAX_EXEC_STEPS = 10_000_000

    def __init__(self, log_level: str | None = None, log_filter: str | None = None,
                 log_verbose: bool = False, plugins: list | None = None):
        _configure_trace(log_level, log_filter=log_filter, verbose=log_verbose)
        # Ensure Python's recursion limit can accommodate our JS call depth
        _min_py_limit = self.MAX_CALL_DEPTH * 10
        if sys.getrecursionlimit() < _min_py_limit:
            sys.setrecursionlimit(_min_py_limit)
        self.output: List[str] = []
        self._clock = 0.0
        self._next_timer_id = 1
        self._task_seq = 0
        self._timers: List[tuple[float, int, dict]] = []
        self._active_timers: Dict[int, dict] = {}
        self._microtasks = deque()
        self._console_counts: Dict[str, int] = {}
        self._console_timers: Dict[str, float] = {}
        self._console_indent: int = 0
        self._call_depth: int = 0
        self._exec_steps: int = 0
        # Built-in prototype objects - must be initialized before _global_env()
        self._array_proto   = JsValue('object', {})
        self._object_proto  = JsValue('object', {})
        self._function_proto = JsValue('object', {})
        self._string_proto  = JsValue('object', {})
        self._number_proto  = JsValue('object', {})
        self._boolean_proto = JsValue('object', {})
        self._regexp_proto  = JsValue('object', {})
        self._map_proto     = JsValue('object', {})
        self._set_proto     = JsValue('object', {})
        self._weakmap_proto = JsValue('object', {})
        self._weakset_proto = JsValue('object', {})
        self._promise_proto = JsValue('object', {})
        self._bigint_proto  = JsValue('object', {})
        self._symbol_proto  = JsValue('object', {})
        # Set up prototype chains
        self._array_proto.value['__proto__']    = self._object_proto
        self._function_proto.value['__proto__']  = self._object_proto
        self._string_proto.value['__proto__']   = self._object_proto
        self._number_proto.value['__proto__']   = self._object_proto
        self._boolean_proto.value['__proto__']  = self._object_proto
        self._regexp_proto.value['__proto__']   = self._object_proto
        self._map_proto.value['__proto__']      = self._object_proto
        self._set_proto.value['__proto__']      = self._object_proto
        self._weakmap_proto.value['__proto__']  = self._object_proto
        self._weakset_proto.value['__proto__']  = self._object_proto
        self._promise_proto.value['__proto__']  = self._object_proto
        self._bigint_proto.value['__proto__']   = self._object_proto
        self._symbol_proto.value['__proto__']   = self._object_proto
        # Object.prototype itself has null prototype (terminates the chain)
        self._object_proto.value['__proto__']   = JS_NULL
        self.genv = self._global_env()
        self.env  = self.genv
        self._module_exports: dict = {}
        self._module_loader = None
        self._module_file: str | None = None
        self._module_url: str | None = None
        self._plugins: list[PyJSPlugin] = []
        self._plugin_contexts: list[PluginContext] = []
        self._plugin_methods: dict = {}
        # Symbol id → JsValue('symbol', ...) registry for Reflect.ownKeys / getOwnPropertySymbols
        self._symbol_id_map: dict = {}
        # JS call stack frames for error.stack traces: list of {'name': str, 'file': str, 'line': int}
        self._js_call_stack: list = []
        # Last error from run() — set on JS throw or internal Python error
        self._last_error: dict | None = None
        if plugins:
            for plugin in plugins:
                self.use(plugin)
        self._init_exec_dispatch()
        self._init_eval_dispatch()
        self._init_prop_dispatch()


    def use(self, plugin: PyJSPlugin) -> 'Interpreter':
        """Register a plugin with this interpreter.

        The plugin's setup() method is called immediately.
        Returns self for chaining: interp.use(A()).use(B())
        """
        ctx = PluginContext(self)
        ctx._plugin_name = plugin.name
        self._plugins.append(plugin)
        self._plugin_contexts.append(ctx)
        plugin.setup(ctx)
        # Sync any new bindings with globalThis
        if hasattr(self, '_global_object'):
            for name in ctx._registered_globals:
                if name in self.genv.bindings:
                    self._global_object.value[name] = self.genv.bindings[name][1]
        return self


    def _make_intrinsic(self, fn, name='?'):
        _self = self  # capture interpreter for closure
        def wrapper(this_val, args, interp):
            try:
                return fn(this_val, args, interp)
            except (_JSReturn, _JSError, _JSBreak, _JSContinue):
                raise
            except TypeError as exc:
                raise _JSError(_self._make_js_error('TypeError', str(exc)))
            except ValueError as exc:
                raise _JSError(_self._make_js_error('RangeError', str(exc)))
            except (KeyError, IndexError) as exc:
                raise _JSError(_self._make_js_error('ReferenceError', str(exc)))
            except OverflowError as exc:
                raise _JSError(_self._make_js_error('RangeError', str(exc)))
            except Exception as exc:
                raise _JSError(_self._make_js_error('Error', str(exc)))
        return JsValue("intrinsic", {"fn": wrapper, "name": name})

    def _is_callable(self, value):
        return isinstance(value, JsValue) and value.type in ('function', 'intrinsic')

    def _make_regexp_val(self, source: str, flags: str = '') -> JsValue:
        """Create a RegExp JsValue from a pattern source and flags string."""
        flag_text = ''.join(sorted(set(flags)))
        py_flags = 0
        if 'i' in flag_text: py_flags |= re.IGNORECASE
        if 'm' in flag_text: py_flags |= re.MULTILINE
        if 's' in flag_text: py_flags |= re.DOTALL
        if 'u' in flag_text or 'v' in flag_text: py_flags |= re.UNICODE
        py_source = _js_regex_to_python(source)
        is_global_or_sticky = 'g' in flag_text or 'y' in flag_text
        # Eagerly validate the pattern so variable-width lookbehind and other
        # Python-unsupported constructs raise JS SyntaxError at definition time.
        try:
            re.compile(py_source, py_flags)
        except re.error as exc:
            raise _JSError(self._make_js_error('SyntaxError', f'Invalid regular expression: {exc}'))

        def _make_intr(fn, name):
            return self._make_intrinsic(lambda tv, args, interp: fn(tv, args, interp), name)

        def _regexp_test(this_re, args, interp):
            text = interp._to_str(args[0]) if args else ''
            if is_global_or_sticky and isinstance(this_re, JsValue) and this_re.type == 'object':
                last_idx_val = this_re.value.get('lastIndex', UNDEFINED)
                start = int(interp._to_num(last_idx_val)) if last_idx_val and last_idx_val.type == 'number' else 0
                start = max(0, start)
                m = re.search(py_source, text[start:] if start > 0 else text, py_flags)
                if m:
                    this_re.value['lastIndex'] = JsValue('number', float(start + m.end()))
                    return JS_TRUE
                else:
                    this_re.value['lastIndex'] = JsValue('number', 0.0)
                    return JS_FALSE
            return JS_TRUE if re.search(py_source, text, py_flags) else JS_FALSE

        def _regexp_exec(this_re, args, interp):
            text = interp._to_str(args[0]) if args else ''
            if is_global_or_sticky and isinstance(this_re, JsValue) and this_re.type == 'object':
                last_idx_val = this_re.value.get('lastIndex', UNDEFINED)
                start = int(interp._to_num(last_idx_val)) if last_idx_val and last_idx_val.type == 'number' else 0
                start = max(0, start)
                search_text = text[start:] if start > 0 else text
                match = re.search(py_source, search_text, py_flags)
            else:
                start = 0
                match = re.search(py_source, text, py_flags)
            if not match:
                if is_global_or_sticky and isinstance(this_re, JsValue) and this_re.type == 'object':
                    this_re.value['lastIndex'] = JsValue('number', 0.0)
                return JS_NULL
            # Update lastIndex for global/sticky regexp
            if is_global_or_sticky and isinstance(this_re, JsValue) and this_re.type == 'object':
                this_re.value['lastIndex'] = JsValue('number', float(start + match.end()))
            values = [JsValue('string', match.group(0))]
            values.extend(JsValue('string', g) if g is not None else UNDEFINED for g in match.groups())
            result = JsValue('array', values)
            groups_dict = match.groupdict()
            if groups_dict:
                groups_obj = JsValue('object', {
                    k: JsValue('string', v) if v is not None else UNDEFINED
                    for k, v in groups_dict.items()
                })
            else:
                groups_obj = UNDEFINED
            if result.extras is None:
                result.extras = {}
            result.extras['groups'] = groups_obj
            result.extras['index'] = JsValue('number', float(start + match.start()))
            if 'd' in flag_text:
                indices_arr = []
                for i in range(len(match.regs)):
                    s, e = match.regs[i]
                    if s == -1:
                        indices_arr.append(UNDEFINED)
                    else:
                        indices_arr.append(JsValue('array', [py_to_js(float(start + s)), py_to_js(float(start + e))]))
                indices_val = JsValue('array', indices_arr)
                # Add indices.groups for named capture groups
                if groups_dict:
                    if indices_val.extras is None: indices_val.extras = {}
                    name_to_idx = {n: i for i, n in enumerate(match.groupdict().keys(), 1)}
                    indices_val.extras['groups'] = JsValue('object', {
                        k: (lambda s2, e2: JsValue('array', [py_to_js(float(start + s2)), py_to_js(float(start + e2))]))(
                            match.span(k)[0], match.span(k)[1]
                        ) if match.group(k) is not None else UNDEFINED
                        for k in groups_dict.keys()
                    })
                else:
                    if indices_val.extras is None: indices_val.extras = {}
                    indices_val.extras['groups'] = UNDEFINED
                result.extras['indices'] = indices_val
            return result

        regexp = JsValue('object', {})
        regexp.value['__kind__'] = JsValue('string', 'RegExp')
        regexp.value['source'] = JsValue('string', source)
        regexp.value['flags'] = JsValue('string', flag_text)
        regexp.value['global'] = JS_TRUE if 'g' in flag_text else JS_FALSE
        regexp.value['ignoreCase'] = JS_TRUE if 'i' in flag_text else JS_FALSE
        regexp.value['multiline'] = JS_TRUE if 'm' in flag_text else JS_FALSE
        regexp.value['sticky'] = JS_TRUE if 'y' in flag_text else JS_FALSE
        regexp.value['lastIndex'] = JsValue('number', 0.0)
        regexp.value['test'] = _make_intr(_regexp_test, 'RegExp.test')
        regexp.value['exec'] = _make_intr(_regexp_exec, 'RegExp.exec')
        return regexp

    def _get_trap(self, handler: JsValue, name: str):
        """Return the trap function JsValue or None."""
        if not isinstance(handler, JsValue): return None
        if handler.type not in ('object', 'function', 'intrinsic'): return None
        if not isinstance(handler.value, dict): return None
        v = handler.value.get(name)
        if v is None or (isinstance(v, JsValue) and v.type == 'undefined'): return None
        if not isinstance(v, JsValue) or not self._is_callable(v): return None
        return v

    def _enqueue_microtask(self, callback):
        self._microtasks.append(callback)

    def _push_timer(self, task, due):
        self._task_seq += 1
        task['due'] = due
        heapq.heappush(self._timers, (due, self._task_seq, task))

    def _schedule_timer(self, fn, delay_ms=0, repeat=False, args=None):
        timer_id = self._next_timer_id
        self._next_timer_id += 1
        task = {
            'id': timer_id,
            'fn': fn,
            'delay': max(0.0, delay_ms),
            'repeat': repeat,
            'args': list(args or []),
        }
        _fn_name = fn.value.get("name", "<anonymous>") if isinstance(fn, JsValue) and isinstance(fn.value, dict) else "<fn>"
        if repeat:
            _log_timer.debug("setInterval(%s, %dms) → id=%s", _fn_name, task['delay'], timer_id)
        else:
            _log_timer.debug("setTimeout(%s, %dms) → id=%s", _fn_name, task['delay'], timer_id)
        self._active_timers[timer_id] = task
        self._push_timer(task, self._clock + task['delay'])
        return timer_id

    def _clear_timer(self, timer_id):
        _log_timer.debug("clearTimer(id=%s)", timer_id)
        self._active_timers.pop(timer_id, None)

    def _new_promise(self):
        _log_promise.debug("create promise")
        return JsValue('promise', {'state': 'pending', 'value': UNDEFINED, 'handlers': []})

    def _resolved_promise(self, value):
        promise = self._new_promise()
        return self._resolve_promise(promise, value)

    def _rejected_promise(self, value):
        promise = self._new_promise()
        return self._reject_promise(promise, value)

    def _to_promise(self, value):
        if isinstance(value, JsValue) and value.type == 'promise':
            return value
        # Thenable assimilation: if value has a .then method, wrap it in a new promise
        if isinstance(value, JsValue) and value.type == 'object' and isinstance(value.value, dict):
            then_fn = self._get_prop(value, 'then')
            if self._is_callable(then_fn):
                promise = self._new_promise()
                resolve_fn = self._make_intrinsic(
                    lambda _this, call_args, inner: inner._resolve_promise(promise, call_args[0] if call_args else UNDEFINED),
                    'Promise.resolve',
                )
                reject_fn = self._make_intrinsic(
                    lambda _this, call_args, inner: inner._reject_promise(promise, call_args[0] if call_args else UNDEFINED),
                    'Promise.reject',
                )
                try:
                    self._call_js(then_fn, [resolve_fn, reject_fn], value)
                except _JSError as exc:
                    self._reject_promise(promise, exc.value)
                return promise
        return self._resolved_promise(value)

    def _settle_promise(self, promise, state, value):
        if promise.value['state'] != 'pending':
            return promise
        promise.value['state'] = state
        promise.value['value'] = value
        handlers = list(promise.value['handlers'])
        promise.value['handlers'].clear()
        for handler in handlers:
            self._enqueue_microtask(lambda source=promise, handler=handler: self._run_promise_handler(source, handler))
        return promise

    def _resolve_promise(self, promise, value):
        _log_promise.debug("resolve promise (state=%s)", promise.value['state'])
        if promise.value['state'] != 'pending':
            return promise
        if value is promise:
            return self._reject_promise(promise, py_to_js('Chaining cycle detected for promise'))
        if isinstance(value, JsValue) and value.type == 'promise':
            self._chain_promise(value, promise)
            return promise
        return self._settle_promise(promise, 'fulfilled', value)

    def _reject_promise(self, promise, value):
        _log_promise.debug("reject promise")
        return self._settle_promise(promise, 'rejected', value)

    def _chain_promise(self, source, next_promise, on_fulfilled=UNDEFINED, on_rejected=UNDEFINED):
        handler = {
            'next_promise': next_promise,
            'on_fulfilled': on_fulfilled,
            'on_rejected': on_rejected,
        }
        if source.value['state'] == 'pending':
            source.value['handlers'].append(handler)
        else:
            self._enqueue_microtask(lambda source=source, handler=handler: self._run_promise_handler(source, handler))
        return next_promise

    def _promise_then(self, source, on_fulfilled=UNDEFINED, on_rejected=UNDEFINED):
        if _log_promise.isEnabledFor(TRACE):
            _log_promise.log(TRACE, "attach .then handler (fulfilled=%s, rejected=%s)",
                             on_fulfilled.type if isinstance(on_fulfilled, JsValue) else "?",
                             on_rejected.type if isinstance(on_rejected, JsValue) else "?")
        return self._chain_promise(source, self._new_promise(), on_fulfilled, on_rejected)

    def _run_promise_handler(self, source, handler):
        next_promise = handler['next_promise']
        state = source.value['state']
        callback = handler['on_fulfilled'] if state == 'fulfilled' else handler['on_rejected']
        if not self._is_callable(callback):
            if state == 'fulfilled':
                self._resolve_promise(next_promise, source.value['value'])
            else:
                self._reject_promise(next_promise, source.value['value'])
            return
        if _log_promise.isEnabledFor(TRACE):
            _log_promise.log(TRACE, "invoke %s-handler (value=%s)", state, self._to_str(source.value['value'])[:60])
        try:
            result = self._call_js(callback, [source.value['value']], UNDEFINED)
            self._resolve_promise(next_promise, result)
        except _JSError as exc:
            self._reject_promise(next_promise, exc.value)

    def _promise_finally(self, source, callback=UNDEFINED):
        if not self._is_callable(callback):
            return self._promise_then(source)

        def _on_fulfilled(this_val, args, interp):
            interp._call_js(callback, [], UNDEFINED)
            return args[0] if args else UNDEFINED

        def _on_rejected(this_val, args, interp):
            interp._call_js(callback, [], UNDEFINED)
            raise _JSError(args[0] if args else UNDEFINED)

        return self._promise_then(
            source,
            self._make_intrinsic(_on_fulfilled, 'Promise.finally'),
            self._make_intrinsic(_on_rejected, 'Promise.finally'),
        )

    def _promise_method(self, promise, name):
        if name == 'then':
            return self._make_intrinsic(
                lambda this_val, args, interp: interp._promise_then(
                    promise,
                    args[0] if len(args) > 0 else UNDEFINED,
                    args[1] if len(args) > 1 else UNDEFINED,
                ),
                'Promise.then',
            )
        if name == 'catch':
            return self._make_intrinsic(
                lambda this_val, args, interp: interp._promise_then(
                    promise,
                    UNDEFINED,
                    args[0] if args else UNDEFINED,
                ),
                'Promise.catch',
            )
        if name == 'finally':
            return self._make_intrinsic(
                lambda this_val, args, interp: interp._promise_finally(
                    promise,
                    args[0] if args else UNDEFINED,
                ),
                'Promise.finally',
            )
        # Check plugin-registered methods
        plugin_key = ('promise', name)
        if self._plugin_methods and plugin_key in self._plugin_methods:
            handler = self._plugin_methods[plugin_key]
            return self._make_intrinsic(lambda tv, a, i, h=handler: h(tv, a, i), name)
        return UNDEFINED

    def _promise_all(self, values):
        result = self._new_promise()
        if not values:
            return self._resolve_promise(result, JsValue('array', []))

        remaining = {'count': len(values), 'done': False}
        resolved = [UNDEFINED] * len(values)

        for index, value in enumerate(values):
            def _on_fulfilled(this_val, args, interp, idx=index):
                if remaining['done']:
                    return UNDEFINED
                resolved[idx] = args[0] if args else UNDEFINED
                remaining['count'] -= 1
                if remaining['count'] == 0:
                    remaining['done'] = True
                    interp._resolve_promise(result, JsValue('array', list(resolved)))
                return resolved[idx]

            def _on_rejected(this_val, args, interp):
                if not remaining['done']:
                    remaining['done'] = True
                    interp._reject_promise(result, args[0] if args else UNDEFINED)
                return UNDEFINED

            self._promise_then(
                self._to_promise(value),
                self._make_intrinsic(_on_fulfilled, 'Promise.all'),
                self._make_intrinsic(_on_rejected, 'Promise.all'),
            )
        return result

    def _promise_race(self, values):
        result = self._new_promise()
        settled = {'done': False}
        for value in values:
            def _on_fulfilled(this_val, args, interp):
                if not settled['done']:
                    settled['done'] = True
                    interp._resolve_promise(result, args[0] if args else UNDEFINED)
                return args[0] if args else UNDEFINED

            def _on_rejected(this_val, args, interp):
                if not settled['done']:
                    settled['done'] = True
                    interp._reject_promise(result, args[0] if args else UNDEFINED)
                return UNDEFINED

            self._promise_then(
                self._to_promise(value),
                self._make_intrinsic(_on_fulfilled, 'Promise.race'),
                self._make_intrinsic(_on_rejected, 'Promise.race'),
            )
        return result

    def _promise_all_settled(self, values):
        result = self._new_promise()
        if not values:
            return self._resolve_promise(result, JsValue('array', []))
        remaining = {'count': len(values)}
        resolved = [UNDEFINED] * len(values)
        for index, value in enumerate(values):
            def make_fulfill(idx):
                def _on_fulfilled(this_val, args, interp):
                    resolved[idx] = py_to_js({'status': 'fulfilled', 'value': args[0] if args else UNDEFINED})
                    remaining['count'] -= 1
                    if remaining['count'] == 0:
                        interp._resolve_promise(result, JsValue('array', list(resolved)))
                    return UNDEFINED
                return _on_fulfilled
            def make_reject(idx):
                def _on_rejected(this_val, args, interp):
                    resolved[idx] = py_to_js({'status': 'rejected', 'reason': args[0] if args else UNDEFINED})
                    remaining['count'] -= 1
                    if remaining['count'] == 0:
                        interp._resolve_promise(result, JsValue('array', list(resolved)))
                    return UNDEFINED
                return _on_rejected
            self._promise_then(
                self._to_promise(value),
                self._make_intrinsic(make_fulfill(index), 'Promise.allSettled'),
                self._make_intrinsic(make_reject(index), 'Promise.allSettled'),
            )
        return result

    def _make_aggregate_error(self, errors_list):
        """Create a proper AggregateError JsValue."""
        msg = 'All promises were rejected'
        try:
            agg_ctor = self.genv.get('AggregateError')
            if self._is_callable(agg_ctor):
                return self._call_js(agg_ctor, [JsValue('array', list(errors_list)), py_to_js(msg)], UNDEFINED)
        except (_JSReturn, _JSBreak, _JSContinue):
            raise
        except Exception:  # Catches non-control-flow errors; falls back to manual AggregateError
            pass
        return JsValue('object', {
            'message': py_to_js(msg),
            'name': py_to_js('AggregateError'),
            'errors': JsValue('array', list(errors_list)),
            'stack': py_to_js(f'AggregateError: {msg}'),
            '__error_type__': py_to_js('AggregateError'),
        })

    def _promise_any(self, values):
        result = self._new_promise()
        if not values:
            return self._reject_promise(result, self._make_aggregate_error([]))
        remaining = {'count': len(values), 'done': False}
        errors = [UNDEFINED] * len(values)
        for index, value in enumerate(values):
            def make_fulfill():
                def _on_fulfilled(this_val, args, interp):
                    if not remaining['done']:
                        remaining['done'] = True
                        interp._resolve_promise(result, args[0] if args else UNDEFINED)
                    return UNDEFINED
                return _on_fulfilled
            def make_reject(idx):
                def _on_rejected(this_val, args, interp):
                    if remaining['done']:
                        return UNDEFINED
                    errors[idx] = args[0] if args else UNDEFINED
                    remaining['count'] -= 1
                    if remaining['count'] == 0:
                        remaining['done'] = True
                        interp._reject_promise(result, interp._make_aggregate_error(list(errors)))
                    return UNDEFINED
                return _on_rejected
            self._promise_then(
                self._to_promise(value),
                self._make_intrinsic(make_fulfill(), 'Promise.any'),
                self._make_intrinsic(make_reject(index), 'Promise.any'),
            )
        return result

    def _run_event_loop(self, until_promise=None):
        steps = 0
        _pending = len(self._microtasks) + len(self._timers)
        _log_event.info("event loop start (%d pending)", _pending)
        while True:
            if until_promise and until_promise.value['state'] != 'pending':
                break
            if self._microtasks:
                _log_event.log(TRACE, "process microtask")
                callback = self._microtasks.popleft()
                callback()
                steps += 1
            else:
                task = None
                while self._timers:
                    due, _seq, candidate = heapq.heappop(self._timers)
                    if self._active_timers.get(candidate['id']) is not candidate:
                        continue
                    task = (due, candidate)
                    break
                if task is None:
                    break
                due, candidate = task
                self._clock = max(self._clock, due)
                _log_timer.log(TRACE, "timer fire (id=%s)", candidate['id'])
                self._call_js(candidate['fn'], list(candidate['args']), UNDEFINED)
                steps += 1
                if candidate['repeat'] and candidate['id'] in self._active_timers:
                    self._push_timer(candidate, self._clock + candidate['delay'])
                else:
                    self._active_timers.pop(candidate['id'], None)
            if _log_event.isEnabledFor(TRACE):
                _log_event.log(TRACE, "event loop tick %d", steps)
            if steps > self.EVENT_LOOP_LIMIT:
                raise _JSError(py_to_js('Event loop exceeded limit; possible unbounded interval or promise recursion'))
        _log_event.info("event loop end (%d ticks)", steps)
        return not until_promise or until_promise.value['state'] != 'pending'

    # --------------------------------------------------------- global env
    def _global_env(self) -> Environment:
        g = Environment()
        if _TRACE_ACTIVE[0]:
            _log_scope.info("scope create (program)")

        # primitives
        g.declare('undefined',  UNDEFINED, 'var')
        g.declare('NaN',        JsValue("number", float('nan')), 'var')
        g.declare('Infinity',   JsValue("number", float('inf')), 'var')

        def intr(fn, name='?'):
            return self._make_intrinsic(lambda this_val, args, interp: fn(args, interp), name)

        register_core_builtins(self, g, intr)
        register_object_builtins(self, g, intr)
        register_advanced_builtins(self, g, intr)
        register_promise_builtins(self, g, intr)
        register_typed_builtins(self, g, intr)

        global_obj = JsValue('object', {})
        g.declare('globalThis', global_obj, 'var')
        self._global_object = global_obj
        for name, (_keyword, value) in g.bindings.items():
            global_obj.value[name] = value

        return g

    # --------------------------------------------------------- step counter
    def _check_step_limit(self):
        """Check execution step counter, raise RangeError if exceeded."""
        self._exec_steps += 1
        if self._exec_steps > self.MAX_EXEC_STEPS:
            raise _JSError(self._make_js_error('RangeError', 'Execution step limit exceeded (possible infinite loop)'))

    # --------------------------------------------------------- error helpers
    def _make_js_error(self, name: str, msg: str) -> JsValue:
        """Build a JS Error object with name, message and stack trace."""
        frames = list(reversed(self._js_call_stack))
        if frames:
            frame_lines = '\n'.join(
                f"    at {f['name']} ({f['file']}:{f['line']})"
                for f in frames
            )
            stack_str = f"{name}: {msg}\n{frame_lines}"
        else:
            stack_str = f"{name}: {msg}"
        err = JsValue('object', {
            'message': py_to_js(msg),
            'name': py_to_js(name),
            'stack': py_to_js(stack_str),
            '__error_type__': py_to_js(name),
        })
        # Add constructor property pointing to the global Error constructor
        try:
            ctor = self.genv.get(name)
            err.value['constructor'] = ctor
        except Exception:
            pass
        return err

    # --------------------------------------------------------- coercion helpers

    def _truthy(self, v: JsValue) -> bool:
        t = v.type
        if t == 'boolean': return v.value
        if t == 'number': return v.value != 0 and v.value == v.value
        if t == 'string': return len(v.value) > 0
        if t == 'undefined' or t == 'null': return False
        if t == 'bigint': return v.value != 0
        return True  # objects, arrays, functions are truthy

    def _to_bool(self, v: JsValue) -> JsValue:
        """Convert a JS value to a boolean JsValue (for Boolean() constructor)."""
        return JS_TRUE if self._truthy(v) else JS_FALSE

    def _to_num(self, v: JsValue) -> float:
        t = v.type
        if t == 'number': return v.value
        if t == 'boolean': return 1.0 if v.value else 0.0
        if t == 'null': return 0.0
        if t == 'undefined': return float('nan')
        if t == 'bigint': return float(v.value)
        if t == 'string':
            s = v.value.strip()
            if not s:
                return 0.0
            try:
                # Handle 0x, 0o, 0b prefixes
                if len(s) > 2 and s[0] == '0' and s[1] in 'xXoObB':
                    return float(int(s, 0))
                return float(s)
            except (ValueError, TypeError, OverflowError):
                return float('nan')
        if t in ('object', 'function', 'intrinsic', 'class', 'array'):
            prim = self._to_primitive(v, 'number')
            return self._to_num(prim)
        return float('nan')

    _TO_STR_SIMPLE = {
        'null': 'null',
        'undefined': 'undefined',
    }
    _TO_STR_LAMBDA = {
        'boolean': lambda v: 'true' if v.value else 'false',
        'bigint': lambda v: str(v.value),
        'symbol': lambda v: f"Symbol({v.value.get('desc', '')})",
        'string': lambda v: v.value,
    }

    def _to_str(self, v: JsValue) -> str:
        _vtype = v.type
        if _vtype == 'string':
            return v.value
        if _vtype == 'number':
            n = v.value
            if n != n: return 'NaN'  # NaN check (faster than math.isnan)
            if math.isinf(n): return 'Infinity' if n>0 else '-Infinity'
            if n == int(n) and abs(n) < 1e21: return str(int(n))
            return str(n)
        simple = self._TO_STR_SIMPLE.get(_vtype)
        if simple is not None:
            return simple
        lam = self._TO_STR_LAMBDA.get(_vtype)
        if lam:
            return lam(v)
        if _vtype == 'array':     return ','.join(self._to_str(e) for e in v.value)
        if _vtype == 'promise':   return '[object Promise]'
        if _vtype == 'proxy':     return self._to_str(v.value.target)
        if _vtype in ('object', 'function', 'intrinsic', 'class'):
            if v.value.__class__ is dict:
                err_type = v.value.get('__error_type__')
                if err_type.__class__ is JsValue and err_type.type == 'string':
                    msg = v.value.get('message')
                    msg_str = msg.value if msg.__class__ is JsValue else ''
                    return f"{err_type.value}: {msg_str}"
                kind = v.value.get('__kind__')
                if kind.__class__ is JsValue and kind.value == 'Generator':
                    return '[object Generator]'
                tag_key = SK_TO_STRING_TAG
                tag = v.value.get(tag_key)
                if tag and tag.__class__ is JsValue and tag.type == 'string':
                    return f'[object {tag.value}]'
                prim = self._to_primitive(v, 'string')
                if prim.type not in ('object', 'function', 'intrinsic', 'class', 'array'):
                    return self._to_str(prim)
            if _vtype in ('function', 'intrinsic', 'class'):
                return f'function {v.value.get("name","")}() {{ [native code] }}'
            return '[object Object]'
        return str(v.value)

    def _to_primitive(self, val, hint='default'):
        """Convert a JS value to a primitive."""
        if val.type in ('undefined','null','boolean','number','string','bigint','symbol'):
            return val
        # Check Symbol.toPrimitive via full prototype chain
        sym_key = SK_TO_PRIMITIVE
        tp_fn = self._get_prop(val, sym_key) if val.type in ('object','function','intrinsic','class','array') else None
        if tp_fn and tp_fn.type not in ('undefined','null'):
            result = self._call_js(tp_fn, [py_to_js(hint)], val)
            if result.type not in ('object','array'):
                return result
        # Default: try valueOf then toString (or reverse for 'string' hint)
        if hint == 'string':
            order = ['toString', 'valueOf']
        else:
            order = ['valueOf', 'toString']
        for method in order:
            fn = self._get_prop(val, method)
            if fn and fn.type in ('function','intrinsic'):
                result = self._call_js(fn, [], val)
                if result.type not in ('object','array'):
                    return result
        # Fallback
        if val.type == 'array':
            return py_to_js(','.join(self._to_str(el) for el in val.value))
        return py_to_js('[object Object]')

    def _from_py(self, val):
        return py_to_js(val)

    _TYPEOF_MAP = {
        'undefined': 'undefined', 'null': 'object', 'boolean': 'boolean',
        'number': 'number', 'string': 'string', 'bigint': 'bigint',
        'symbol': 'symbol', 'function': 'function', 'intrinsic': 'function',
        'class': 'function', 'object': 'object', 'array': 'object',
        'promise': 'object',
    }

    def _typeof(self, v: JsValue) -> str:
        result = self._TYPEOF_MAP.get(v.type)
        if result is not None:
            return result
        if v.type == 'proxy':
            t = v.value.target
            if t.type in ('function','intrinsic','class'): return 'function'
            return 'object'
        return 'undefined'

    def _js_inspect(self, v: JsValue, depth: int = 2, seen: set = None) -> str:
        """Format a JS value for console.log output (Node.js-style)."""
        if seen is None:
            seen = set()
        if v.type == 'undefined': return 'undefined'
        if v.type == 'null': return 'null'
        if v.type == 'boolean': return 'true' if v.value else 'false'
        if v.type == 'number':
            n = v.value
            import math as _math
            if _math.isnan(n): return 'NaN'
            if _math.isinf(n): return 'Infinity' if n > 0 else '-Infinity'
            if n == int(n) and abs(n) < 1e21: return str(int(n))
            return str(n)
        if v.type == 'string': return repr(v.value)
        if v.type == 'bigint': return f'{v.value}n'
        if v.type == 'symbol':
            desc = v.value.get('description','')
            return f"Symbol({desc.value if isinstance(desc, JsValue) else desc})"
        if v.type in ('function', 'intrinsic', 'class'):
            fn_name = ''
            if isinstance(v.value, dict):
                fn_name = v.value.get('name') or ''
            return f'[Function: {fn_name}]' if fn_name else '[Function (anonymous)]'
        if v.type == 'promise':
            return 'Promise { <pending> }'
        if v.type == 'regexp':
            return str(v.value) if v.value else '/(?:)/'
        if v.type == 'proxy':
            return self._js_inspect(v.value.target, depth, seen)
        if v.type == 'array':
            vid = id(v)
            if vid in seen: return '[Circular *]'
            if depth < 0: return '[Array]'
            seen2 = seen | {vid}
            items = [self._js_inspect(e, depth - 1, seen2) for e in v.value]
            return '[ ' + ', '.join(items) + ' ]' if items else '[]'
        if v.type in ('object',):
            if not isinstance(v.value, dict): return '[object Object]'
            vid = id(v)
            if vid in seen: return '[Circular *]'
            err_type = v.value.get('__error_type__')
            if isinstance(err_type, JsValue):
                msg = v.value.get('message')
                msg_str = msg.value if isinstance(msg, JsValue) else ''
                return f"{err_type.value}: {msg_str}"
            kind = v.value.get('__kind__')
            if isinstance(kind, JsValue):
                if kind.value == 'Generator': return '[object Generator]'
                if kind.value == 'Map':
                    store = v.value.get('__store__', [])
                    if depth < 0: return 'Map {}'
                    seen2 = seen | {vid}
                    parts = [f"{self._js_inspect(e[0],depth-1,seen2)} => {self._js_inspect(e[1],depth-1,seen2)}" for e in store if isinstance(e, (list, tuple)) and len(e) == 2]
                    return f"Map({len(parts)})" + ' { ' + ', '.join(parts) + ' }' if parts else f"Map(0) {{}}"
                if kind.value == 'Set':
                    store = v.value.get('__store__', [])
                    if depth < 0: return 'Set {}'
                    seen2 = seen | {vid}
                    parts = [self._js_inspect(e, depth-1, seen2) for e in store]
                    return f"Set({len(parts)})" + ' { ' + ', '.join(parts) + ' }' if parts else f"Set(0) {{}}"
                if kind.value in ('WeakMap', 'WeakSet'): return f"{kind.value} {{ <items unknown> }}"
            if depth < 0: return '[Object]'
            seen2 = seen | {vid}
            parts = []
            for k, val in v.value.items():
                if k.startswith('__') or (k.startswith('@@') and k.endswith('@@')): continue
                if not isinstance(val, JsValue): continue
                parts.append(f'{k}: {self._js_inspect(val, depth-1, seen2)}')
            cn = v.value.get('__class_name__')
            prefix = (cn.value + ' ') if isinstance(cn, JsValue) and cn.type == 'string' else ''
            return prefix + ('{ ' + ', '.join(parts) + ' }' if parts else '{}')
        return self._to_str(v)

    def _is_nullish(self, v: JsValue) -> bool:
        return v.type in ('null', 'undefined')

    def _to_key(self, value):
        if value.__class__ is str:
            return value
        if value.__class__ is JsValue:
            vtype = value.type
            if vtype == 'string':
                return value.value
            if vtype == 'symbol':
                self._symbol_id_map[value.value['id']] = value
                return f"@@{value.value['id']}@@"
            if vtype == 'number':
                nv = value.value
                iv = int(nv)
                if nv == iv:
                    return str(iv)
                return str(nv)
            return self._to_str(value)
        return str(value)

    def _sym_key_to_jsval(self, key: str):
        """Convert an @@N@@ internal symbol key back to a JsValue('symbol', ...) if known."""
        if key.startswith('@@') and key.endswith('@@') and len(key) > 4:
            try:
                sym_id = int(key[2:-2])
                if sym_id in self._symbol_id_map:
                    return self._symbol_id_map[sym_id]
                # Well-known symbols
                _well_known = {
                    1: 'Symbol.iterator', 2: 'Symbol.toPrimitive', 3: 'Symbol.hasInstance',
                    4: 'Symbol.toStringTag', 5: 'Symbol.asyncIterator', 6: 'Symbol.species',
                    7: 'Symbol.match', 8: 'Symbol.replace', 9: 'Symbol.split',
                    10: 'Symbol.search', 11: 'Symbol.isConcatSpreadable',
                    12: 'Symbol.dispose', 13: 'Symbol.asyncDispose',
                }
                desc = _well_known.get(sym_id, f'Symbol({sym_id})')
                sym = JsValue('symbol', {'id': sym_id, 'desc': desc})
                self._symbol_id_map[sym_id] = sym
                return sym
            except (ValueError, TypeError):
                pass
        return None

    def _array_like_items(self, value: JsValue):
        if value.type == 'array':
            return list(value.value)
        if value.type == 'string':
            return [JsValue('string', ch) for ch in value.value]
        # Check for Symbol.iterator on objects
        it = self._get_js_iterator(value)
        if it is not None:
            result = []
            seen = 0
            while seen < 100000:
                r = it()
                seen += 1
                done = self._get_prop(r, 'done')
                if self._truthy(done):
                    break
                result.append(self._get_prop(r, 'value'))
            return result
        if value.type == 'object':
            raw_length = value.value.get('length', UNDEFINED)
            if isinstance(raw_length, JsValue) and raw_length.type != 'undefined':
                length = max(0, int(self._to_num(raw_length)))
                return [value.value.get(str(index), UNDEFINED) for index in range(length)]
        return []

    def _get_js_iterator(self, value):
        """Returns a Python callable () -> JsValue({value, done}) or None."""
        result = self._get_js_iterator_with_obj(value)
        return result[0] if result is not None else None

    def _get_js_iterator_with_obj(self, value):
        """Returns (next_callable, iterator_obj) or None. iterator_obj may be None for built-in iterables."""
        if value.type == 'array':
            items = list(value.value)
            idx = [0]
            def _arr_next():
                if idx[0] >= len(items):
                    return JsValue('object', {'value': UNDEFINED, 'done': JS_TRUE})
                v = items[idx[0]]; idx[0] += 1
                return JsValue('object', {'value': v, 'done': JS_FALSE})
            return (_arr_next, None)

        if value.type == 'string':
            chars = list(value.value)
            idx = [0]
            def _str_next():
                if idx[0] >= len(chars):
                    return JsValue('object', {'value': UNDEFINED, 'done': JS_TRUE})
                v = JsValue('string', chars[idx[0]]); idx[0] += 1
                return JsValue('object', {'value': v, 'done': JS_FALSE})
            return (_str_next, None)

        if value.type in ('object', 'function', 'intrinsic', 'class'):
            sym_key = SK_ITERATOR
            iter_fn = self._get_prop(value, sym_key)
            if not (iter_fn and self._is_callable(iter_fn)):
                iter_fn = None
            if iter_fn and self._is_callable(iter_fn):
                iterator = self._call_js(iter_fn, [], value)
                next_fn = self._get_prop(iterator, 'next')
                if self._is_callable(next_fn):
                    def _obj_next(nf=next_fn, it=iterator):
                        return self._call_js(nf, [], it)
                    return (_obj_next, iterator)

            # Already an iterator (has .next method but no [Symbol.iterator])
            next_fn = value.value.get('next') if isinstance(value.value, dict) else None
            if next_fn and self._is_callable(next_fn):
                def _iter_next(nf=next_fn, it=value):
                    return self._call_js(nf, [], it)
                return (_iter_next, value)

        return None

    def _get_async_iterator(self, value):
        """Returns a callable () -> Promise<{value, done}> or None."""
        if value.type in ('object', 'function', 'intrinsic', 'class'):
            sym_async_key = SK_ASYNC_ITERATOR
            iter_fn = value.value.get(sym_async_key) if isinstance(value.value, dict) else None
            if iter_fn and self._is_callable(iter_fn):
                iterator = self._call_js(iter_fn, [], value)
                next_fn = self._get_prop(iterator, 'next')
                if self._is_callable(next_fn):
                    def _async_next(nf=next_fn, it=iterator):
                        return self._call_js(nf, [], it)
                    return _async_next
        # Fall back to sync iterator, wrapping results in resolved promises
        sync_it = self._get_js_iterator(value)
        if sync_it is not None:
            def _wrapped_next(sit=sync_it):
                return self._resolved_promise(sit())
            return _wrapped_next
        return None

    def _find_generator(self, env):
        e = env
        while e is not None:
            if e._generator is not None:
                return e._generator
            e = e.parent
        return None

    def _bind_value(self, name, value, env, keyword='var', declare=True):
        if declare:
            env.declare(name, value, keyword)
        else:
            env.set(name, value)
        # Inline _sync_global_binding — only sync when env IS the global env
        if env is self.genv and self._global_object is not None and name != 'globalThis':
            self._global_object.value[name] = value

    def _sync_global_binding(self, name, value, env):
        if getattr(self, '_global_object', None) is not None and env is self.genv and name != 'globalThis':
            self._global_object.value[name] = value

    def _get_proto(self, obj: JsValue):
        if obj.type in ('object', 'function', 'intrinsic', 'class'):
            proto = obj.value.get('__proto__')
            if proto.__class__ is JsValue:
                if proto.type in ('object', 'function', 'intrinsic', 'class'):
                    return proto
                if proto.type == 'null':
                    return JS_NULL  # explicit null prototype
            # Fall through to built-in protos for known kinds
            kind = obj.value.get('__kind__')
            if kind.__class__ is JsValue and kind.type == 'string':
                kv = kind.value
                if kv == 'Map': return self._map_proto
                if kv == 'Set': return self._set_proto
                if kv == 'WeakMap': return self._weakmap_proto
                if kv == 'WeakSet': return self._weakset_proto
                if kv == 'RegExp': return self._regexp_proto
            if obj.type == 'function': return self._function_proto
            return self._object_proto
        if obj.type == 'array':
            # Array subclasses store their prototype in extras.__proto__
            if obj.extras:
                sub_proto = obj.extras.get('__proto__')
                if sub_proto.__class__ is JsValue and sub_proto.type == 'object':
                    return sub_proto
            return self._array_proto
        if obj.type == 'string': return self._string_proto
        if obj.type == 'number': return self._number_proto
        if obj.type == 'boolean': return self._boolean_proto
        if obj.type == 'bigint': return self._bigint_proto
        if obj.type == 'symbol': return self._symbol_proto
        if obj.type == 'promise': return self._promise_proto
        return None

    def _make_super_proxy(self, proto, this_val, ctor=None):
        proxy = JsValue('object', {})
        proxy.value['__super_target__'] = proto
        proxy.value['__super_this__'] = this_val
        if ctor is not None:
            proxy.value['__super_ctor__'] = ctor  # parent class ctor for super() calls
        return proxy

    def _bind_pattern(self, pattern, value, env, keyword='var', declare=True):
        if pattern is None:
            return
        if pattern.__class__ is str:
            self._bind_value(pattern, value, env, keyword, declare)
            return
        _ptype = pattern['type']
        if _ptype == 'Identifier':
            self._bind_value(pattern['name'], value, env, keyword, declare)
            return
        if _ptype == 'AssignmentPattern':
            next_value = value
            if next_value.type == 'undefined':
                next_value = self._eval(pattern['right'], env)
            self._bind_pattern(pattern['left'], next_value, env, keyword, declare)
            return
        if _ptype == 'RestElement':
            self._bind_pattern(pattern['argument'], value, env, keyword, declare)
            return
        if _ptype == 'MemberExpression':
            obj = self._eval(pattern['object'], env)
            if pattern.get('computed'):
                key = self._to_key(self._eval(pattern['property'], env))
            else:
                key = pattern['property']['name']
            self._set_prop(obj, key, value)
            return
        if _ptype == 'ArrayPattern':
            if self._is_nullish(value):
                raise _JSError(py_to_js('Cannot destructure null or undefined'))
            items = self._array_like_items(value)
            for index, item in enumerate(pattern['elements']):
                if item is None:
                    continue
                if item.get('type') == 'RestElement':
                    self._bind_pattern(item['argument'], JsValue('array', items[index:]), env, keyword, declare)
                    break
                self._bind_pattern(item, items[index] if index < len(items) else UNDEFINED, env, keyword, declare)
            return
        if _ptype == 'ObjectPattern':
            if self._is_nullish(value):
                raise _JSError(py_to_js('Cannot destructure null or undefined'))
            source = value if value.type in ('object', 'array', 'string') else JsValue('object', {})
            used_keys = set()
            for prop in pattern['properties']:
                if prop.get('type') == 'RestElement':
                    if source.type == 'object':
                        rest = {k: v for k, v in source.value.items() if k not in used_keys}
                    elif source.type == 'array':
                        rest = {str(i): item for i, item in enumerate(source.value) if str(i) not in used_keys}
                    elif source.type == 'string':
                        rest = {str(i): JsValue('string', ch) for i, ch in enumerate(source.value) if str(i) not in used_keys}
                    else:
                        rest = {}
                    self._bind_pattern(prop['argument'], JsValue('object', rest), env, keyword, declare)
                    continue
                key = prop['key'] if not prop.get('computed') else self._to_key(self._eval(prop['key'], env))
                key = str(key)
                used_keys.add(key)
                if source.type == 'object':
                    prop_value = self._get_prop(source, key) if key not in source.value else source.value.get(key, UNDEFINED)
                elif source.type == 'array':
                    try:
                        idx = int(key)
                        prop_value = source.value[idx] if 0 <= idx < len(source.value) else UNDEFINED
                    except (ValueError, TypeError):
                        # For non-integer keys on arrays (like 'groups', 'index'), use _get_prop
                        prop_value = self._get_prop(source, key)
                elif source.type == 'string':
                    try:
                        idx = int(key)
                        prop_value = JsValue('string', source.value[idx]) if 0 <= idx < len(source.value) else UNDEFINED
                    except (ValueError, TypeError):
                        prop_value = UNDEFINED
                else:
                    prop_value = UNDEFINED
                self._bind_pattern(prop['value'], prop_value, env, keyword, declare)

    def _resolve_target(self, node, env):
        if node['type'] == 'Identifier':
            name = node['name']
            target_env = env._find(name)
            if not target_env:
                raise _JSError(self._make_js_error('ReferenceError', f"{name} is not defined"))
            _go = self._global_object
            _genv = self.genv
            return (
                lambda target_env=target_env, name=name: target_env.bindings[name][1],
                lambda val, target_env=target_env, name=name, _go=_go, _genv=_genv: (
                    target_env.set_own(name, val),
                    _go.value.__setitem__(name, val) if (target_env is _genv and _go is not None and name != 'globalThis') else None,
                )[-1],
            )
        if node['type'] == 'MemberExpression':
            if node.get('optional'):
                raise _JSError(py_to_js('Invalid assignment target'))
            obj = self._eval(node['object'], env)
            prop = self._eval(node['property'], env) if node['computed'] else node['property']['name']
            return (
                lambda obj=obj, prop=prop: self._get_prop(obj, prop),
                lambda val, obj=obj, prop=prop: self._set_prop(obj, prop, val),
            )
        raise _JSError(py_to_js('Invalid assignment target'))

    # --------------------------------------------------------- property helpers
    def _get_desc(self, obj, key):
        """Return the raw descriptor dict for key on obj, or None."""
        if not isinstance(getattr(obj, 'value', None), dict):
            return None
        descs = obj.value.get('__descs__')
        if descs is None:
            return None
        return descs.get(key)

    def _set_desc(self, obj, key, desc_dict):
        """Store the raw descriptor dict for key on obj."""
        if not isinstance(getattr(obj, 'value', None), dict):
            return
        if '__descs__' not in obj.value:
            obj.value['__descs__'] = {}
        obj.value['__descs__'][key] = desc_dict

    def _is_enumerable(self, obj, key):
        desc = self._get_desc(obj, key)
        if desc is None:
            return True
        return desc.get('enumerable', True)

    def _get_prop_array(self, obj, key):
        if key == 'length':
            _len = len(obj.value)
            return _JS_SMALL_INTS[_len] if 0 <= _len <= 255 else JsValue("number", _len)
        # Fast path: numeric index (most common in loops) — check before method dispatch
        try:
            idx = int(key)
            if 0 <= idx < len(obj.value):
                return obj.value[idx]
        except (ValueError, OverflowError):
            pass
        sym_iter_key = SK_ITERATOR
        if key == sym_iter_key:
            arr_ref = obj
            def _arr_iter_factory(this_val, call_args, interp):
                items = list(arr_ref.value)
                idx = [0]
                iterator = JsValue('object', {})
                def _next(tv, a, intp):
                    if idx[0] >= len(items):
                        return JsValue('object', {'value': UNDEFINED, 'done': JS_TRUE})
                    v = items[idx[0]]; idx[0] += 1
                    return JsValue('object', {'value': v, 'done': JS_FALSE})
                iterator.value['next'] = interp._make_intrinsic(_next, 'ArrayIterator.next')
                iterator.value[sym_iter_key] = interp._make_intrinsic(lambda tv, a, i: iterator, '[Symbol.iterator]')
                interp._add_iterator_helpers(iterator)
                return iterator
            return self._make_intrinsic(_arr_iter_factory, '[Symbol.iterator]')
        # Subclass prototype overrides take priority over built-in ARRAY_METHODS
        _extras = obj.extras
        sub_proto = _extras["__proto__"] if _extras and "__proto__" in _extras else None
        if isinstance(sub_proto, JsValue) and sub_proto.type == "object":
            v = self._get_prop_object_like(sub_proto, key, receiver=obj)
            if v is not UNDEFINED:
                return v
        if key in self.ARRAY_METHODS:
            # Cache method intrinsics per-array to avoid re-creating closures
            if obj.extras is None:
                obj.extras = {}
            cache_key = f'__m_{key}'
            cached = obj.extras.get(cache_key)
            if cached is not None:
                return cached
            method = self._arr_method(obj, key)
            obj.extras[cache_key] = method
            return method
        if obj.extras and key in obj.extras:
            return obj.extras[key]
        # Check Array.prototype for user-added methods
        _ap = self._array_proto.value
        if key in _ap:
            proto_val = _ap[key]
            if isinstance(proto_val, JsValue) and proto_val.type in ('function', 'intrinsic'):
                # Cache bound proto method wrapper on the array
                if obj.extras is None:
                    obj.extras = {}
                bound = self._make_intrinsic(
                    lambda tv, a, i, fn=proto_val: i._call_js(fn, a, tv),
                    key
                )
                obj.extras[key] = bound
                return bound
            return proto_val
        plugin_key = ('array', key)
        if self._plugin_methods and plugin_key in self._plugin_methods:
            handler = self._plugin_methods[plugin_key]
            return self._make_intrinsic(lambda tv, a, i, h=handler: h(tv, a, i), key)
        return UNDEFINED

    def _get_prop_string(self, obj, key):
        if key == 'length':
            _len = len(obj.value)
            return _JS_SMALL_INTS[_len] if 0 <= _len <= 255 else JsValue("number", _len)
        sym_iter_key = SK_ITERATOR
        if key == sym_iter_key:
            chars = list(obj.value)
            def _str_iter_factory(this_val, call_args, interp):
                idx = [0]
                iterator = JsValue('object', {})
                def _next(tv, a, intp):
                    if idx[0] >= len(chars):
                        return JsValue('object', {'value': UNDEFINED, 'done': JS_TRUE})
                    v = JsValue('string', chars[idx[0]]); idx[0] += 1
                    return JsValue('object', {'value': v, 'done': JS_FALSE})
                iterator.value['next'] = interp._make_intrinsic(_next, 'StringIterator.next')
                iterator.value[sym_iter_key] = interp._make_intrinsic(lambda tv, a, i: iterator, '[Symbol.iterator]')
                interp._add_iterator_helpers(iterator)
                return iterator
            return self._make_intrinsic(_str_iter_factory, '[Symbol.iterator]')
        if key in self.STRING_METHODS:
            return self._str_method(obj, key)
        try:
            idx = int(key)
            if 0 <= idx < len(obj.value):
                return JsValue("string", obj.value[idx])
        except ValueError:
            pass
        # Check String.prototype for user-added methods
        proto_val = self._string_proto.value.get(key)
        if proto_val is not None:
            if isinstance(proto_val, JsValue) and proto_val.type in ('function', 'intrinsic'):
                proto_val_ref = proto_val
                return self._make_intrinsic(
                    lambda tv, a, i, fn=proto_val_ref: i._call_js(fn, a, tv), key)
            return proto_val
        plugin_key = ('string', key)
        if self._plugin_methods and plugin_key in self._plugin_methods:
            handler = self._plugin_methods[plugin_key]
            return self._make_intrinsic(lambda tv, a, i, h=handler: h(tv, a, i), key)
        return UNDEFINED

    def _get_prop_promise(self, obj, key):
        if key in self.PROMISE_METHODS:
            return self._promise_method(obj, key)
        plugin_key = ('promise', key)
        if self._plugin_methods and plugin_key in self._plugin_methods:
            handler = self._plugin_methods[plugin_key]
            return self._make_intrinsic(lambda tv, a, i, h=handler: h(tv, a, i), key)
        return UNDEFINED

    def _get_prop_object_like(self, obj, key, receiver=None):
        if receiver is None:
            receiver = obj
        obj_type = obj.value.get('__type__') if obj.value.__class__ is dict else None
        if obj_type.__class__ is JsValue and obj_type.value == 'WeakRef':
            if key == 'deref':
                target = obj.value.get('__target__', UNDEFINED)
                return self._make_intrinsic(lambda tv, a, i, t=target: t, 'WeakRef.deref')
        _obj_type_str = obj_type.value if obj_type.__class__ is JsValue else obj_type
        if _obj_type_str == 'TypedArray':
            return self._typed_array_get_prop(obj, key)
        if _obj_type_str == 'DataView':
            return self._dataview_get_prop(obj, key)
        if _obj_type_str == 'ArrayBuffer':
            return self._arraybuffer_get_prop(obj, key)
        kind = obj.value.get('__kind__')
        if kind.__class__ is JsValue and kind.type == 'string':
            if kind.value == 'Map' and key == 'size':
                size_fn = obj.value.get('__size_fn__')
                return self._call_js(size_fn, [], obj) if size_fn else JsValue('number', 0)
            if kind.value == 'Set' and key == 'size':
                size_fn = obj.value.get('__size_fn__')
                return self._call_js(size_fn, [], obj) if size_fn else JsValue('number', 0)
        if obj.type in ('function', 'intrinsic') and key == 'name':
            raw_name = obj.value.get('name', '') if obj.value.__class__ is dict else ''
            if raw_name.__class__ is JsValue:
                return raw_name
            return JsValue('string', raw_name if raw_name.__class__ is str else '')
        if obj.type == 'function' and key == 'length':
            node = obj.value.get('node', {}) if obj.value.__class__ is dict else {}
            params = node.get('params', []) if node.__class__ is dict else []
            count = 0
            for p in params:
                if p.__class__ is dict and p.get('type') in ('RestElement', 'AssignmentPattern'):
                    break
                count += 1
            return JsValue('number', float(count))
        if obj.type == 'intrinsic' and key == 'length':
            return JsValue('number', 0.0)
        if obj.type in ('function', 'intrinsic') and key == 'toString':
            def _fn_tostring(tv, a, i, _fn=obj):
                if _fn.type == 'function' and isinstance(_fn.value, dict):
                    node = _fn.value.get('node')
                    fn_name = _fn.value.get('name', '')
                    if node:
                        params = node.get('params', [])
                        param_names = []
                        for p in params:
                            if isinstance(p, dict):
                                pt = p.get('type', '')
                                if pt == 'Identifier':
                                    param_names.append(p.get('name', ''))
                                elif pt == 'RestElement':
                                    inner = p.get('argument', {})
                                    param_names.append('...' + inner.get('name', ''))
                                elif pt == 'AssignmentPattern':
                                    left = p.get('left', {})
                                    param_names.append(left.get('name', ''))
                                else:
                                    param_names.append(str(p))
                            else:
                                param_names.append(str(p))
                        params_str = ', '.join(param_names)
                        is_async = node.get('async', False)
                        is_gen = node.get('generator', False)
                        prefix = ('async ' if is_async else '') + 'function' + ('*' if is_gen else '')
                        return JsValue('string', f'{prefix} {fn_name}({params_str}) {{ [source] }}')
                n = obj.value.get('name', '') if isinstance(obj.value, dict) else ''
                return JsValue('string', f'function {n}() {{ [native code] }}')
            return self._make_intrinsic(_fn_tostring, 'Function.toString')
        current = obj
        while current.__class__ is JsValue and current.type in ('object', 'function', 'intrinsic', 'class'):
            # Array.prototype: dispatch built-in methods to the receiver (supports `super.push` etc.)
            if current.value.__class__ is dict and current.value.get('__is_array_proto__') is JS_TRUE:
                if key in self.ARRAY_METHODS:
                    return self._arr_method(receiver, key)
                if key == 'length':
                    return JsValue('number', float(len(receiver.value)) if isinstance(getattr(receiver, 'value', None), list) else 0)
            getter_key = f"__get__{key}"
            if getter_key in current.value:
                return self._call_js(current.value[getter_key], [], receiver)
            if key in current.value:
                return current.value[key]
            current = self._get_proto(current)
        if key == 'hasOwnProperty':
            def _has_own_property(tv, call_args, interp):
                prop_name = interp._to_key(call_args[0]) if call_args else ''
                target = tv if (isinstance(tv, JsValue) and tv.type not in ('null','undefined')) else UNDEFINED
                if isinstance(getattr(target, 'value', None), dict) and prop_name in target.value:
                    return JS_TRUE
                return JS_FALSE
            return self._make_intrinsic(_has_own_property, 'Object.hasOwnProperty')
        if key == 'propertyIsEnumerable':
            def _prop_is_enum(tv, call_args, interp):
                prop_name = interp._to_key(call_args[0]) if call_args else ''
                target = tv if (isinstance(tv, JsValue) and tv.type not in ('null','undefined')) else UNDEFINED
                if not isinstance(getattr(target, 'value', None), dict) or prop_name not in target.value:
                    return JS_FALSE
                desc = interp._get_desc(target, prop_name)
                if desc is not None and not desc.get('enumerable', True):
                    return JS_FALSE
                return JS_TRUE
            return self._make_intrinsic(_prop_is_enum, 'Object.propertyIsEnumerable')
        if key == 'isPrototypeOf':
            obj_ref = obj
            def _is_proto_of(tv, call_args, interp, _proto=obj_ref):
                target = call_args[0] if call_args else UNDEFINED
                cur = interp._get_proto(target)
                while isinstance(cur, JsValue) and cur.type not in ('null', 'undefined'):
                    if cur is _proto:
                        return JS_TRUE
                    cur = interp._get_proto(cur)
                return JS_FALSE
            return self._make_intrinsic(_is_proto_of, 'Object.isPrototypeOf')
        if key == 'valueOf':
            return self._make_intrinsic(lambda tv, a, i: tv, 'Object.valueOf')
        if key == 'toString' and obj.type == 'object':
            def _obj_to_string(tv, a, interp):
                tag_key = SK_TO_STRING_TAG
                # Use _get_prop to walk prototype chain and invoke getters
                tag = interp._get_prop(tv, tag_key) if isinstance(tv, JsValue) and tv.type not in ('null','undefined') else UNDEFINED
                if isinstance(tag, JsValue) and tag.type == 'string':
                    return JsValue('string', f'[object {tag.value}]')
                if tv is JS_NULL or (isinstance(tv, JsValue) and tv.type == 'null'):
                    return JsValue('string', '[object Null]')
                if tv is UNDEFINED or (isinstance(tv, JsValue) and tv.type == 'undefined'):
                    return JsValue('string', '[object Undefined]')
                if isinstance(tv, JsValue):
                    if tv.type == 'array':    return JsValue('string', '[object Array]')
                    if tv.type == 'promise':  return JsValue('string', '[object Promise]')
                    if tv.type == 'regexp':   return JsValue('string', '[object RegExp]')
                    if tv.type == 'symbol':   return JsValue('string', '[object Symbol]')
                    if tv.type == 'bigint':   return JsValue('string', '[object BigInt]')
                    if tv.type == 'number':   return JsValue('string', '[object Number]')
                    if tv.type == 'string':   return JsValue('string', '[object String]')
                    if tv.type == 'boolean':  return JsValue('string', '[object Boolean]')
                    if tv.type in ('function','intrinsic','class'):
                        return JsValue('string', '[object Function]')
                    if isinstance(tv.value, dict):
                        # TypedArrays, ArrayBuffer etc. use __name__
                        tname = tv.value.get('__name__')
                        if isinstance(tname, JsValue) and tname.type == 'string':
                            return JsValue('string', f'[object {tname.value}]')
                        # ArrayBuffer
                        ttype = tv.value.get('__type__')
                        if isinstance(ttype, JsValue) and ttype.value == 'ArrayBuffer':
                            return JsValue('string', '[object ArrayBuffer]')
                        # Objects with __kind__
                        kind = tv.value.get('__kind__')
                        if isinstance(kind, JsValue) and kind.type == 'string':
                            return JsValue('string', f'[object {kind.value}]')
                return JsValue('string', '[object Object]')
            return self._make_intrinsic(_obj_to_string, 'Object.toString')
        if obj.type in ('function', 'intrinsic') and key == 'call':
            fn_ref = obj
            def _fn_call(this_val, call_args, interp, _fn=fn_ref):
                new_this = call_args[0] if call_args else UNDEFINED
                rest_args = list(call_args[1:]) if len(call_args) > 1 else []
                return interp._call_js(_fn, rest_args, new_this)
            return self._make_intrinsic(_fn_call, 'Function.call')
        if obj.type in ('function', 'intrinsic') and key == 'apply':
            fn_ref = obj
            def _fn_apply(this_val, call_args, interp, _fn=fn_ref):
                new_this = call_args[0] if call_args else UNDEFINED
                rest = call_args[1] if len(call_args) > 1 else JsValue('array', [])
                args_list = list(rest.value) if rest.type == 'array' else []
                return interp._call_js(_fn, args_list, new_this)
            return self._make_intrinsic(_fn_apply, 'Function.apply')
        if obj.type in ('function', 'intrinsic') and key == 'bind':
            fn_ref = obj
            def _fn_bind(this_val, call_args, interp, _fn=fn_ref):
                bound_this = call_args[0] if call_args else UNDEFINED
                bound_args = list(call_args[1:]) if len(call_args) > 1 else []
                def _bound(tv, a, i, _bfn=_fn, _bthis=bound_this, _bargs=bound_args):
                    return i._call_js(_bfn, _bargs + list(a), _bthis)
                return i._make_intrinsic(_bound, 'bound function') if False else interp._make_intrinsic(_bound, 'bound function')
            return self._make_intrinsic(_fn_bind, 'Function.bind')
        plugin_key = ('object', key)
        if self._plugin_methods and plugin_key in self._plugin_methods:
            handler = self._plugin_methods[plugin_key]
            return self._make_intrinsic(lambda tv, a, i, h=handler: h(tv, a, i), key)
        return UNDEFINED

    def _get_prop_number(self, obj, key):
        if key in self.NUMBER_METHODS:
            return self._num_method(obj, key)
        # Check Number.prototype for user-added methods
        proto_val = self._number_proto.value.get(key)
        if proto_val is not None:
            if isinstance(proto_val, JsValue) and proto_val.type in ('function', 'intrinsic'):
                proto_val_ref = proto_val
                return self._make_intrinsic(
                    lambda tv, a, i, fn=proto_val_ref: i._call_js(fn, a, tv), key)
            return proto_val
        plugin_key = ('number', key)
        if self._plugin_methods and plugin_key in self._plugin_methods:
            handler = self._plugin_methods[plugin_key]
            return self._make_intrinsic(lambda tv, a, i, h=handler: h(tv, a, i), key)
        return UNDEFINED

    def _get_prop_symbol(self, obj, key):
        sym_str = self._to_str(obj)
        if key == 'toString':
            return self._make_intrinsic(lambda tv, a, i: JsValue('string', sym_str), 'Symbol.toString')
        if key == 'description':
            return JsValue('string', obj.value.get('desc', ''))
        return UNDEFINED

    def _get_prop_bigint(self, obj, key):
        n = obj.value
        if key == 'toString':
            def _bigint_tostring(tv, args, interp):
                base = int(args[0].value) if args and args[0].type == 'number' else 10
                if base < 2 or base > 36:
                    raise _JSError(interp._make_js_error('RangeError', 'toString() radix must be between 2 and 36'))
                val = tv.value if tv.type == 'bigint' else n
                if base == 10: return JsValue('string', str(val))
                digits = '0123456789abcdefghijklmnopqrstuvwxyz'
                sign = '-' if val < 0 else ''
                v = abs(val)
                if v == 0: return JsValue('string', '0')
                parts = []
                while v: parts.append(digits[v % base]); v //= base
                return JsValue('string', sign + ''.join(reversed(parts)))
            return self._make_intrinsic(_bigint_tostring, 'BigInt.toString')
        if key == 'valueOf':
            return self._make_intrinsic(lambda tv, a, i: JsValue('bigint', tv.value if tv.type == 'bigint' else n), 'BigInt.valueOf')
        if key == 'toLocaleString':
            return self._make_intrinsic(lambda tv, a, i: JsValue('string', str(tv.value if tv.type == 'bigint' else n)), 'BigInt.toLocaleString')
        return UNDEFINED

    _GET_PROP_DISPATCH = None  # initialized in _init_dispatch_tables

    def _init_prop_dispatch(self):
        self._GET_PROP_DISPATCH = {
            'array': self._get_prop_array,
            'string': self._get_prop_string,
            'promise': self._get_prop_promise,
            'object': self._get_prop_object_like,
            'function': self._get_prop_object_like,
            'intrinsic': self._get_prop_object_like,
            'class': self._get_prop_object_like,
            'number': self._get_prop_number,
            'symbol': self._get_prop_symbol,
            'bigint': self._get_prop_bigint,
        }

    def _get_prop(self, obj: JsValue, prop):
        key = prop if prop.__class__ is str else self._to_key(prop)
        if _TRACE_ACTIVE[0]:
            _log_prop.debug("get %s.%s", obj.type, key)
        if self._global_object is obj and self.genv.has(key):
            return self.genv.get(key)
        if obj.type == 'proxy':
            proxy = obj.value
            trap = self._get_trap(proxy.handler, 'get')
            if trap:
                return self._call_js(trap, [proxy.target, py_to_js(key), obj], UNDEFINED)
            return self._get_prop(proxy.target, key)
        if obj.type == 'object' and '__super_target__' in obj.value:
            target = obj.value.get('__super_target__')
            this_val = obj.value.get('__super_this__', target)
            if isinstance(target, JsValue):
                return self._get_prop_object_like(target, key, receiver=this_val)
            return UNDEFINED
        try:
            handler = self._GET_PROP_DISPATCH[obj.type]
        except KeyError:
            return UNDEFINED
        return handler(obj, key)

    def _set_prop(self, obj: JsValue, prop, val: JsValue):
        key = prop if prop.__class__ is str else self._to_key(prop)
        if _TRACE_ACTIVE[0]:
            _log_prop.debug("set %s.%s", obj.type, key)
        if self._global_object is obj and self.genv.has(key):
            self.genv.set(key, val)
            self._global_object.value[key] = val
            return
        if obj.type == 'proxy':
            proxy = obj.value
            trap = self._get_trap(proxy.handler, 'set')
            if trap:
                _log_proxy.debug("proxy trap set(target=%s, prop=%s)", proxy.target.type, key)
                self._call_js(trap, [proxy.target, py_to_js(key), val, obj], UNDEFINED)
                return
            self._set_prop(proxy.target, key, val)
            return
        if obj.type == 'object' and '__super_target__' in obj.value:
            target = obj.value.get('__super_target__')
            this_val = obj.value.get('__super_this__', target)
            if target.__class__ is JsValue:
                # Walk target proto chain for setter
                current = target
                while current.__class__ is JsValue and current.type in ('object', 'function', 'intrinsic', 'class'):
                    setter_key = f"__set__{key}"
                    if setter_key in current.value:
                        self._call_js(current.value[setter_key], [val], this_val)
                        return
                    current = self._get_proto(current)
                # No setter found — set directly on __super_this__
                if this_val.__class__ is JsValue and this_val.type in ('object', 'function', 'intrinsic', 'class'):
                    this_val.value[key] = val
            return
        if obj.type == 'array':
            try:
                idx = int(key)
                while len(obj.value) <= idx:
                    obj.value.append(UNDEFINED)
                obj.value[idx] = val
            except ValueError:
                if key == 'length':
                    pass  # length is read-only via list
                else:
                    # Non-numeric string property on array (e.g. subclass `this.max = n`)
                    if obj.extras is None:
                        obj.extras = {}
                    obj.extras[key] = val
        elif obj.type in ('object', 'function', 'intrinsic', 'class'):
            # TypedArray numeric index write
            if isinstance(obj.value, dict):
                _obj_type_v = obj.value.get('__type__')
                _obj_type_s = _obj_type_v.value if isinstance(_obj_type_v, JsValue) else _obj_type_v
                if _obj_type_s == 'TypedArray':
                    try:
                        idx = int(key)
                        d = obj.value
                        _fmt = d.get('__fmt__', 'B')
                        _itemsize = d.get('__itemsize__', 1)
                        _byteoffset = d.get('__byteoffset__', 0)
                        _length = d.get('__length__', 0)
                        _buf = d.get('__bytes__')
                        if 0 <= idx < _length and _buf is not None:
                            _ta_name = d.get('__name__', 'TypedArray')
                            _is_bigint = _fmt in ('q', 'Q')
                            _is_clamped = _ta_name == 'Uint8ClampedArray'
                            if _is_bigint:
                                raw = val.value if val.type == 'bigint' else int(self._to_num(val))
                            elif _is_clamped:
                                raw = max(0, min(255, int(self._to_num(val))))
                            else:
                                raw = _ta_coerce(val, _fmt, self)
                            struct.pack_into('=' + _fmt, _buf, _byteoffset + idx * _itemsize, raw)
                        return
                    except (ValueError, TypeError):
                        pass
            # Check for setter in proto chain
            current = obj
            while isinstance(current, JsValue) and current.type in ('object', 'function', 'intrinsic', 'class'):
                setter_key = f"__set__{key}"
                if setter_key in current.value:
                    self._call_js(current.value[setter_key], [val], obj)
                    return
                current = self._get_proto(current)
            # Check non-extensible
            if obj.value.get('__extensible__') is False and key not in obj.value:
                if self.env._strict:
                    raise _JSError(self._make_js_error('TypeError',
                        f"Cannot add property {key}, object is not extensible"))
                return  # silently ignore new property on non-extensible object
            # Check writable descriptor
            desc = self._get_desc(obj, key)
            if desc is not None and not desc.get('writable', True):
                if self.env._strict:
                    raise _JSError(self._make_js_error('TypeError',
                        f"Cannot assign to read only property '{key}'"))
                return  # silently ignore write to non-writable property in non-strict
            obj.value[key] = val
        else:
            pass  # setting property on primitive is silent no-op in non-strict

    def _del_prop(self, obj: JsValue, prop):
        key = self._to_key(prop)
        if obj.type == 'proxy':
            proxy = obj.value
            trap = self._get_trap(proxy.handler, 'deleteProperty')
            if trap:
                result = self._call_js(trap, [proxy.target, py_to_js(key)], UNDEFINED)
                return self._truthy(result)
            return self._del_prop(proxy.target, key)
        if obj.type in ('object', 'function', 'intrinsic', 'class') and key in obj.value:
            desc = self._get_desc(obj, key)
            if desc is not None and not desc.get('configurable', True):
                return False  # non-configurable, cannot delete
            del obj.value[key]; return True
        if obj.type == 'array':
            try:
                idx = int(key)
                if 0 <= idx < len(obj.value):
                    obj.value[idx] = UNDEFINED; return True
            except (ValueError, TypeError): pass
        return False

    # --------------------------------------------------------- built-in methods
    def _arr_method(self, arr: JsValue, name: str):
        interp = self
        def fn(this_val, args, extra_args=None):
            a = arr.value
            if name == 'push':
                if len(args) == 1:
                    a.append(args[0])
                else:
                    a.extend(args)
                _len = len(a)
                return _JS_SMALL_INTS[_len] if 0 <= _len <= 255 else JsValue("number", _len)
            if name == 'pop':
                return a.pop() if a else UNDEFINED
            if name == 'shift':
                return a.pop(0) if a else UNDEFINED
            if name == 'unshift':
                for i,x in enumerate(args): a.insert(i,x)
                _len = len(a)
                return _JS_SMALL_INTS[_len] if 0 <= _len <= 255 else JsValue("number", _len)
            if name == 'indexOf':
                target = args[0] if args else UNDEFINED
                start = int(args[1].value) if len(args)>1 else 0
                for i in range(max(0,start), len(a)):
                    if interp._strict_eq(a[i], target): return JsValue("number", i)
                return JsValue("number", -1)
            if name == 'includes':
                target = args[0] if args else UNDEFINED
                start = int(self._to_num(args[1])) if len(args) > 1 else 0
                if start < 0:
                    start = max(len(a) + start, 0)
                target_nan = target.type == 'number' and math.isnan(target.value)
                for x in a[start:]:
                    if target_nan and x.type == 'number' and math.isnan(x.value): return JS_TRUE
                    if interp._strict_eq(x, target): return JS_TRUE
                return JS_FALSE
            if name == 'join':
                sep = args[0].value if args and args[0].type=='string' else ','
                return JsValue("string", sep.join(interp._to_str(x) for x in a))
            if name == 'slice':
                s = int(args[0].value) if args else 0
                e = int(args[1].value) if len(args)>1 else len(a)
                return py_to_js(list(a[s:e]))
            if name == 'splice':
                start = int(args[0].value) if args else 0
                count = int(args[1].value) if len(args)>1 else len(a)
                removed = a[start:start+count]
                del a[start:start+count]
                for i,x in enumerate(args[2:]): a.insert(start+i,x)
                return py_to_js(removed)
            if name == 'concat':
                result = list(a)
                for item in args:
                    sym_ics_key = SK_IS_CONCAT_SPREADABLE
                    if isinstance(item.value, dict) and sym_ics_key in item.value:
                        is_spreadable = interp._truthy(item.value[sym_ics_key])
                    else:
                        is_spreadable = item.type == 'array'
                    if is_spreadable:
                        if isinstance(item.value, list):
                            result.extend(item.value)
                        elif isinstance(item.value, dict):
                            length_v = item.value.get('length')
                            length = int(length_v.value) if isinstance(length_v, JsValue) and length_v.type == 'number' else 0
                            for idx in range(length):
                                v = item.value.get(str(idx), UNDEFINED)
                                result.append(v)
                    else:
                        result.append(item)
                return py_to_js(result)
            if name == 'reverse':
                a.reverse(); return arr
            if name == 'sort':
                cb = args[0] if args else None
                if cb and interp._is_callable(cb):
                    def _sort_cmp(x, y):
                        r = interp._to_num(interp._call_js(cb, [x, y], None))
                        return -1 if r < 0 else (1 if r > 0 else 0)
                    a.sort(key=functools.cmp_to_key(_sort_cmp))
                else:
                    a.sort(key=lambda x: interp._to_str(x))
                return arr
            if name == 'forEach':
                cb = args[0] if args else None
                if cb:
                    for i,x in enumerate(a):
                        interp._call_js(cb, [x, JsValue("number",i), arr], None)
                return UNDEFINED
            if name == 'map':
                cb = args[0] if args else None
                if cb:
                    result = []
                    for i,x in enumerate(a):
                        result.append(interp._call_js(cb, [x, JsValue("number",i), arr], None))
                    return py_to_js(result)
                return py_to_js([])
            if name == 'filter':
                cb = args[0] if args else None
                if cb:
                    result = [x for i,x in enumerate(a)
                              if interp._truthy(interp._call_js(cb,[x,JsValue("number",i),arr],None))]
                    return py_to_js(result)
                return py_to_js([])
            if name == 'reduce':
                cb = args[0] if args else None
                if len(args) > 1:
                    acc = args[1]
                    start_idx = 0
                elif a:
                    acc = a[0]
                    start_idx = 1
                else:
                    raise _JSError(py_to_js('Reduce of empty array with no initial value'))
                for i in range(start_idx, len(a)):
                    acc = interp._call_js(cb, [acc, a[i], JsValue("number",i), arr], None)
                return acc
            if name == 'find':
                cb = args[0] if args else None
                if cb:
                    for i,x in enumerate(a):
                        if interp._truthy(interp._call_js(cb,[x,JsValue("number",i)],None)):
                            return x
                return UNDEFINED
            if name == 'every':
                cb = args[0] if args else None
                if cb:
                    for i,x in enumerate(a):
                        if not interp._truthy(interp._call_js(cb,[x,JsValue("number",i)],None)):
                            return JS_FALSE
                return JS_TRUE
            if name == 'some':
                cb = args[0] if args else None
                if cb:
                    for i,x in enumerate(a):
                        if interp._truthy(interp._call_js(cb,[x,JsValue("number",i)],None)):
                            return JS_TRUE
                return JS_FALSE
            if name == 'flat':
                if args and args[0].type == 'number':
                    v = args[0].value
                    if math.isinf(v):
                        depth = float('inf')
                    else:
                        depth = int(v)
                else:
                    depth = 1
                def flatten(lst, d):
                    r = []
                    for x in lst:
                        if x.type == 'array' and (d > 0 or d == float('inf')):
                            r.extend(flatten(x.value, d - 1 if d != float('inf') else float('inf')))
                        else:
                            r.append(x)
                    return r
                return py_to_js(flatten(a, depth))
            if name == 'flatMap':
                mapped = []
                if args:
                    for i,x in enumerate(a):
                        mapped.append(interp._call_js(args[0],[x,JsValue("number",i)],None))
                return py_to_js(flatten_one(mapped))
            if name == 'fill':
                val = args[0] if args else UNDEFINED
                s = int(args[1].value) if len(args)>1 else 0
                e = int(args[2].value) if len(args)>2 else len(a)
                for i in range(s, e):
                    if 0 <= i < len(a): a[i] = val
                return arr
            if name == 'copyWithin':
                t = int(args[0].value) if args else 0
                s = int(args[1].value) if len(args)>1 else 0
                e = int(args[2].value) if len(args)>2 else len(a)
                sub = a[s:e]
                for i,x in enumerate(sub):
                    if 0 <= t+i < len(a): a[t+i] = x
                return arr
            if name == 'at':
                idx = int(self._to_num(args[0])) if args else 0
                if idx < 0:
                    idx += len(a)
                return a[idx] if 0 <= idx < len(a) else UNDEFINED
            if name == 'toString':
                return JsValue("string", ','.join(interp._to_str(x) for x in a))
            if name == 'findIndex':
                cb = args[0] if args else None
                if cb:
                    for i, x in enumerate(a):
                        if interp._truthy(interp._call_js(cb, [x, JsValue("number", i), arr], None)):
                            return JsValue("number", i)
                return JsValue("number", -1)
            if name == 'findLast':
                cb = args[0] if args else None
                if cb:
                    for i in range(len(a) - 1, -1, -1):
                        if interp._truthy(interp._call_js(cb, [a[i], JsValue("number", i), arr], None)):
                            return a[i]
                return UNDEFINED
            if name == 'findLastIndex':
                cb = args[0] if args else None
                if cb:
                    for i in range(len(a) - 1, -1, -1):
                        if interp._truthy(interp._call_js(cb, [a[i], JsValue("number", i), arr], None)):
                            return JsValue("number", i)
                return JsValue("number", -1)
            if name == 'reduceRight':
                cb = args[0] if args else None
                has_init = len(args) > 1
                acc = args[1] if has_init else (a[-1] if a else None)
                if acc is None and not a:
                    raise _JSError(py_to_js('Reduce of empty array with no initial value'))
                start = len(a) - 1 if has_init else len(a) - 2
                for i in range(start, -1, -1):
                    acc = interp._call_js(cb, [acc, a[i], JsValue("number", i), arr], None)
                return acc
            if name == 'lastIndexOf':
                target = args[0] if args else UNDEFINED
                from_index = int(interp._to_num(args[1])) if len(args) > 1 else len(a) - 1
                if from_index < 0:
                    from_index = len(a) + from_index
                for i in range(min(from_index, len(a) - 1), -1, -1):
                    if interp._strict_eq(a[i], target):
                        return JsValue("number", i)
                return JsValue("number", -1)
            if name == 'toSorted':
                copy = list(a)
                cb = args[0] if args else None
                if cb and interp._is_callable(cb):
                    def _cmp(x, y):
                        r = interp._to_num(interp._call_js(cb, [x, y], None))
                        return -1 if r < 0 else (1 if r > 0 else 0)
                    copy.sort(key=functools.cmp_to_key(_cmp))
                else:
                    def _sort_key(x):
                        if x.type == 'number': return (0, x.value)
                        if x.type == 'string': return (1, x.value)
                        return (2, interp._to_str(x))
                    copy.sort(key=_sort_key)
                return JsValue('array', copy)
            if name == 'toReversed':
                return JsValue('array', list(reversed(a)))
            if name == 'toSpliced':
                copy = list(a)
                start = int(interp._to_num(args[0])) if args else 0
                if start < 0:
                    start = max(len(copy) + start, 0)
                delete_count = int(interp._to_num(args[1])) if len(args) > 1 else len(copy) - start
                items = list(args[2:]) if len(args) > 2 else []
                del copy[start:start + max(0, delete_count)]
                for i, item in enumerate(items):
                    copy.insert(start + i, item)
                return JsValue('array', copy)
            if name == 'with':
                copy = list(a)
                idx = int(interp._to_num(args[0])) if args else 0
                val = args[1] if len(args) > 1 else UNDEFINED
                if idx < 0:
                    idx = len(copy) + idx
                if 0 <= idx < len(copy):
                    copy[idx] = val
                return JsValue('array', copy)
            if name in ('keys', 'values', 'entries'):
                items = list(a)
                idx = [0]
                sym_iter_key = SK_ITERATOR
                iter_obj = JsValue('object', {})
                def _make_iter_next(items=items, idx=idx, kind=name):
                    def _next(tv, nargs, ni):
                        if idx[0] >= len(items):
                            return JsValue('object', {'value': UNDEFINED, 'done': JS_TRUE})
                        i = idx[0]; idx[0] += 1
                        if kind == 'keys':
                            val = JsValue('number', float(i))
                        elif kind == 'values':
                            val = items[i]
                        else:  # entries
                            val = JsValue('array', [JsValue('number', float(i)), items[i]])
                        return JsValue('object', {'value': val, 'done': JS_FALSE})
                    return _next
                iter_obj.value['next'] = interp._make_intrinsic(_make_iter_next(), f'Array.{name}.next')
                iter_obj.value[sym_iter_key] = interp._make_intrinsic(lambda tv, a, ni, o=iter_obj: o, f'Array.{name}[Symbol.iterator]')
                interp._add_iterator_helpers(iter_obj)
                return iter_obj
            # Check plugin-registered methods
            plugin_key = ('array', name)
            if interp._plugin_methods and plugin_key in interp._plugin_methods:
                return interp._plugin_methods[plugin_key](this_val, args, interp)
            return UNDEFINED
        return JsValue("intrinsic", {"fn": fn, "name": f"Array.{name}"})

    def _str_method(self, sval: JsValue, name: str):
        interp = self
        s = sval.value
        def _pattern_text(arg):
            if not isinstance(arg, JsValue):
                return str(arg)
            if arg.type == 'object' and isinstance(arg.value, dict):
                source = arg.value.get('source', py_to_js(''))
                if isinstance(source, JsValue):
                    return interp._to_str(source)
                return str(source)
            if arg.type == 'string':
                return arg.value
            return interp._to_str(arg)
        def fn(this_val, args, extra_args=None):
            if name == 'charAt':
                i = int(args[0].value) if args else 0
                return JsValue("string", s[i] if 0<=i<len(s) else '')
            if name == 'charCodeAt':
                i = int(args[0].value) if args else 0
                return JsValue("number", ord(s[i]) if 0<=i<len(s) else float('nan'))
            if name == 'indexOf':
                sub = args[0].value if args else ''
                start = int(args[1].value) if len(args)>1 else 0
                return JsValue("number", s.find(sub, start))
            if name == 'lastIndexOf':
                sub = args[0].value if args else ''
                start = int(args[1].value) if len(args)>1 else len(s)
                return JsValue("number", s.rfind(sub, 0, start+1))
            if name == 'includes':
                sub = args[0].value if args else ''
                return JS_TRUE if sub in s else JS_FALSE
            if name == 'startsWith':
                sub = args[0].value if args else ''
                return JS_TRUE if s.startswith(sub) else JS_FALSE
            if name == 'endsWith':
                sub = args[0].value if args else ''
                return JS_TRUE if s.endswith(sub) else JS_FALSE
            if name == 'slice':
                start = int(args[0].value) if args else 0
                end = int(args[1].value) if len(args)>1 else len(s)
                return JsValue("string", s[start:end])
            if name == 'substring':
                start = max(0, int(args[0].value)) if args else 0
                end = int(args[1].value) if len(args)>1 else len(s)
                if start > end: start, end = end, start
                return JsValue("string", s[start:end])
            if name == 'toLowerCase':
                return JsValue("string", s.lower())
            if name == 'toUpperCase':
                return JsValue("string", s.upper())
            if name == 'trim':
                return JsValue("string", s.strip())
            if name == 'split':
                sep_arg = args[0] if args else UNDEFINED
                lim_jsval = args[1] if len(args) > 1 else UNDEFINED
                str_jsval = JsValue('string', s)
                # Symbol.split delegation
                sym_split_key = SK_SPLIT
                if isinstance(sep_arg.value, dict) and sym_split_key in sep_arg.value:
                    method = sep_arg.value[sym_split_key]
                    return interp._call_js(method, [str_jsval, lim_jsval], sep_arg)
                # RegExp separator
                if (sep_arg.type == 'object' and isinstance(sep_arg.value, dict) and
                        isinstance(sep_arg.value.get('__kind__'), JsValue) and
                        sep_arg.value['__kind__'].value == 'RegExp'):
                    src = interp._to_str(sep_arg.value.get('source', py_to_js('')))
                    flg_str = interp._to_str(sep_arg.value.get('flags', py_to_js('')))
                    py_src = _js_regex_to_python(src)
                    py_flg = 0
                    if 'i' in flg_str: py_flg |= re.IGNORECASE
                    if 'm' in flg_str: py_flg |= re.MULTILINE
                    if 's' in flg_str: py_flg |= re.DOTALL
                    lim = int(lim_jsval.value) if lim_jsval.type == 'number' else None
                    parts = re.split(py_src, s, flags=py_flg)
                    if lim is not None: parts = parts[:lim]
                    return py_to_js(parts)
                sep = sep_arg.value if sep_arg.type != 'undefined' else None
                lim = int(lim_jsval.value) if lim_jsval.type == 'number' else None
                if sep is None:
                    parts = list(s)
                elif sep == '':
                    # JS: "abc".split("") → ["a","b","c"]
                    parts = list(s)
                elif lim is None:
                    parts = s.split(sep)
                else:
                    parts = s.split(sep, lim)
                if lim is not None: parts = parts[:lim]
                return py_to_js(parts)
            if name == 'replace':
                pat_arg = args[0] if args else UNDEFINED
                repl_arg = args[1] if len(args) > 1 else UNDEFINED
                str_jsval = JsValue('string', s)
                # Symbol.replace delegation
                sym_replace_key = SK_REPLACE
                if isinstance(pat_arg.value, dict) and sym_replace_key in pat_arg.value:
                    method = pat_arg.value[sym_replace_key]
                    return interp._call_js(method, [str_jsval, repl_arg], pat_arg)
                if (isinstance(pat_arg, JsValue) and pat_arg.type == 'object' and
                        isinstance(pat_arg.value, dict) and
                        isinstance(pat_arg.value.get('__kind__'), JsValue) and
                        pat_arg.value['__kind__'].value == 'RegExp'):
                    src = interp._to_str(pat_arg.value.get('source', py_to_js('')))
                    flg_str = interp._to_str(pat_arg.value.get('flags', py_to_js('')))
                    py_src = _js_regex_to_python(src)
                    py_flg = 0
                    if 'i' in flg_str: py_flg |= re.IGNORECASE
                    if 'm' in flg_str: py_flg |= re.MULTILINE
                    if 's' in flg_str: py_flg |= re.DOTALL
                    if 'u' in flg_str or 'v' in flg_str: py_flg |= re.UNICODE
                    count = 0 if 'g' in flg_str else 1
                    if interp._is_callable(repl_arg):
                        def _fn_repl(m, _rf=repl_arg):
                            call_args = [JsValue('string', m.group(0))]
                            call_args.extend(JsValue('string', g) if g is not None else UNDEFINED for g in m.groups())
                            call_args.append(JsValue('number', float(m.start())))
                            call_args.append(JsValue('string', s))
                            gd = m.groupdict()
                            if gd:
                                call_args.append(JsValue('object', {k: JsValue('string', v) if v is not None else UNDEFINED for k, v in gd.items()}))
                            return interp._to_str(interp._call_js(_rf, call_args, None))
                        return JsValue('string', re.sub(py_src, _fn_repl, s, count=count, flags=py_flg))
                    else:
                        repl_str = interp._to_str(repl_arg) if isinstance(repl_arg, JsValue) and repl_arg.type != 'undefined' else ''
                        def _str_repl(m):
                            r = repl_str
                            def _named(nm):
                                try: return m.group(nm.group(1)) or ''
                                except (IndexError, re.error): return nm.group(0)
                            r = re.sub(r'\$<([^>]+)>', _named, r)
                            def _numbered(ng):
                                n = int(ng.group(1))
                                try: return m.group(n) or ''
                                except (IndexError, re.error): return ng.group(0)
                            r = re.sub(r'\$(\d+)', _numbered, r)
                            r = r.replace('$$', '\x00')
                            r = r.replace('$&', m.group(0))
                            r = r.replace('\x00', '$')
                            return r
                        return JsValue('string', re.sub(py_src, _str_repl, s, count=count, flags=py_flg))
                else:
                    old = _pattern_text(pat_arg)
                    if interp._is_callable(repl_arg):
                        idx = s.find(old)
                        if idx == -1:
                            return JsValue("string", s)
                        repl_val = interp._call_js(repl_arg, [JsValue('string', old), JsValue('number', float(idx)), JsValue('string', s)], None)
                        return JsValue("string", s[:idx] + interp._to_str(repl_val) + s[idx+len(old):])
                    new = interp._to_str(repl_arg) if isinstance(repl_arg, JsValue) else ''
                    idx = s.find(old)
                    if idx == -1:
                        return JsValue("string", s)
                    # Process replacement string $ sequences
                    r = new.replace('$$', '\x00')
                    r = r.replace('$&', old)
                    r = r.replace('$`', s[:idx])
                    r = r.replace("$'", s[idx+len(old):])
                    r = r.replace('\x00', '$')
                    return JsValue("string", s[:idx] + r + s[idx+len(old):])
            if name == 'replaceAll':
                pat_arg = args[0] if args else UNDEFINED
                repl_arg = args[1] if len(args) > 1 else UNDEFINED
                if (isinstance(pat_arg, JsValue) and pat_arg.type == 'object' and
                        isinstance(pat_arg.value, dict) and
                        isinstance(pat_arg.value.get('__kind__'), JsValue) and
                        pat_arg.value['__kind__'].value == 'RegExp'):
                    flg_str = interp._to_str(pat_arg.value.get('flags', py_to_js('')))
                    if 'g' not in flg_str and 'y' not in flg_str:
                        raise _JSError(interp._make_js_error('TypeError',
                            'String.prototype.replaceAll called with a non-global RegExp argument'))
                    src = interp._to_str(pat_arg.value.get('source', py_to_js('')))
                    py_src = _js_regex_to_python(src)
                    py_flg = 0
                    if 'i' in flg_str: py_flg |= re.IGNORECASE
                    if 'm' in flg_str: py_flg |= re.MULTILINE
                    if 's' in flg_str: py_flg |= re.DOTALL
                    if 'u' in flg_str or 'v' in flg_str: py_flg |= re.UNICODE
                    if interp._is_callable(repl_arg):
                        def _fn_repl_re_all(m, rf=repl_arg):
                            call_args = [JsValue('string', m.group(0))]
                            call_args.extend(JsValue('string', g) if g is not None else UNDEFINED for g in m.groups())
                            call_args.append(JsValue('number', float(m.start())))
                            call_args.append(JsValue('string', s))
                            gd = m.groupdict()
                            if gd:
                                call_args.append(JsValue('object', {k: JsValue('string', v) if v is not None else UNDEFINED for k, v in gd.items()}))
                            return interp._to_str(interp._call_js(rf, call_args, None))
                        return JsValue('string', re.sub(py_src, _fn_repl_re_all, s, flags=py_flg))
                    repl_str = interp._to_str(repl_arg) if isinstance(repl_arg, JsValue) and repl_arg.type != 'undefined' else ''
                    def _str_repl_all(m):
                        r = repl_str
                        def _named(nm):
                            try: return m.group(nm.group(1)) or ''
                            except (IndexError, re.error): return nm.group(0)
                        r = re.sub(r'\$<([^>]+)>', _named, r)
                        def _numbered(ng):
                            n = int(ng.group(1))
                            try: return m.group(n) or ''
                            except (IndexError, re.error): return ng.group(0)
                        r = re.sub(r'\$(\d+)', _numbered, r)
                        r = r.replace('$$', '\x00')
                        r = r.replace('$&', m.group(0))
                        r = r.replace('\x00', '$')
                        return r
                    return JsValue('string', re.sub(py_src, _str_repl_all, s, flags=py_flg))
                else:
                    old = _pattern_text(pat_arg)
                    if interp._is_callable(repl_arg):
                        def _fn_repl_all(match_str, start_pos, full_str, rf=repl_arg):
                            call_args = [JsValue('string', match_str),
                                         JsValue('number', float(start_pos)),
                                         JsValue('string', full_str)]
                            return interp._to_str(interp._call_js(rf, call_args, None))
                        if not old:
                            return JsValue("string", s)
                        parts = s.split(old)
                        result_parts = []
                        pos = 0
                        for i, part in enumerate(parts):
                            if i > 0:
                                result_parts.append(_fn_repl_all(old, pos - len(old), s))
                            result_parts.append(part)
                            pos += len(part) + len(old)
                        return JsValue("string", "".join(result_parts))
                    else:
                        new = interp._to_str(repl_arg) if isinstance(repl_arg, JsValue) else ''
                        if not old:
                            return JsValue("string", s)
                        # Process $ sequences per-occurrence
                        parts = s.split(old)
                        result_parts = []
                        pos = 0
                        for i, part in enumerate(parts):
                            if i > 0:
                                idx = pos - len(old)
                                r = new.replace('$$', '\x00').replace('$&', old)
                                r = r.replace('$`', s[:idx]).replace("$'", s[idx+len(old):])
                                r = r.replace('\x00', '$')
                                result_parts.append(r)
                            result_parts.append(part)
                            pos += len(part) + len(old)
                        return JsValue("string", "".join(result_parts))
            if name == 'padStart':
                target = int(args[0].value) if args else 0
                fill = args[1].value if len(args)>1 and args[1].type=='string' else ' '
                return JsValue("string", s.rjust(target, fill))
            if name == 'padEnd':
                target = int(args[0].value) if args else 0
                fill = args[1].value if len(args)>1 and args[1].type=='string' else ' '
                return JsValue("string", s.ljust(target, fill))
            if name == 'repeat':
                n = int(args[0].value) if args else 0
                return JsValue("string", s * max(0, n))
            if name == 'match':
                pat_arg = args[0] if args else UNDEFINED
                str_jsval = JsValue('string', s)
                # Symbol.match delegation
                sym_match_key = SK_MATCH
                if isinstance(pat_arg.value, dict) and sym_match_key in pat_arg.value:
                    method = pat_arg.value[sym_match_key]
                    return interp._call_js(method, [str_jsval], pat_arg)
                # RegExp with g flag: return all matches
                if (pat_arg.type == 'object' and isinstance(pat_arg.value, dict) and
                        isinstance(pat_arg.value.get('__kind__'), JsValue) and
                        pat_arg.value['__kind__'].value == 'RegExp'):
                    src = interp._to_str(pat_arg.value.get('source', py_to_js('')))
                    flg_str = interp._to_str(pat_arg.value.get('flags', py_to_js('')))
                    py_src = _js_regex_to_python(src)
                    py_flg = 0
                    if 'i' in flg_str: py_flg |= re.IGNORECASE
                    if 'm' in flg_str: py_flg |= re.MULTILINE
                    if 's' in flg_str: py_flg |= re.DOTALL
                    if 'g' in flg_str:
                        matches = re.findall(py_src, s, py_flg)
                        if not matches:
                            return JS_NULL
                        return py_to_js([m if isinstance(m, str) else m[0] for m in matches])
                    else:
                        m = re.search(py_src, s, py_flg)
                        if not m:
                            return JS_NULL
                        result_arr = [JsValue('string', m.group(0))]
                        result_arr.extend(JsValue('string', g) if g is not None else UNDEFINED for g in m.groups())
                        arr_val = JsValue('array', result_arr)
                        if arr_val.extras is None: arr_val.extras = {}
                        groups_dict = m.groupdict()
                        arr_val.extras['groups'] = JsValue('object', {
                            k: JsValue('string', v) if v is not None else UNDEFINED
                            for k, v in groups_dict.items()
                        }) if groups_dict else UNDEFINED
                        arr_val.extras['index'] = JsValue('number', float(m.start()))
                        return arr_val
                pat = _pattern_text(pat_arg) if pat_arg.type != 'undefined' else ''
                m = re.search(pat, s)
                if m:
                    result = [m.group()]
                    if m.groups():
                        result.extend(m.groups())
                    return py_to_js(result)
                return JS_NULL
            if name == 'matchAll':
                pat_arg = args[0] if args else UNDEFINED
                py_src = None
                py_flg = 0
                if (isinstance(pat_arg, JsValue) and pat_arg.type == 'object' and
                        isinstance(pat_arg.value, dict) and
                        isinstance(pat_arg.value.get('__kind__'), JsValue) and
                        pat_arg.value['__kind__'].value == 'RegExp'):
                    src = interp._to_str(pat_arg.value.get('source', py_to_js('')))
                    flg_str = interp._to_str(pat_arg.value.get('flags', py_to_js('')))
                    if 'g' not in flg_str and 'y' not in flg_str:
                        raise _JSError(interp._make_js_error('TypeError',
                            'String.prototype.matchAll called with a non-global RegExp argument'))
                    py_src = _js_regex_to_python(src)
                    if 'i' in flg_str: py_flg |= re.IGNORECASE
                    if 'm' in flg_str: py_flg |= re.MULTILINE
                    if 's' in flg_str: py_flg |= re.DOTALL
                else:
                    py_src = _pattern_text(pat_arg) if not (isinstance(pat_arg, JsValue) and pat_arg.type == 'undefined') else ''
                results = []
                for m in re.finditer(py_src, s, py_flg):
                    values = [JsValue('string', m.group(0))]
                    values.extend(JsValue('string', g) if g is not None else UNDEFINED for g in m.groups())
                    match_arr = JsValue('array', values)
                    if match_arr.extras is None:
                        match_arr.extras = {}
                    match_arr.extras['index'] = py_to_js(m.start())
                    match_arr.extras['input'] = py_to_js(s)
                    groups_dict = m.groupdict()
                    if groups_dict:
                        match_arr.extras['groups'] = JsValue('object', {
                            k: JsValue('string', v) if v is not None else UNDEFINED
                            for k, v in groups_dict.items()
                        })
                    else:
                        match_arr.extras['groups'] = UNDEFINED
                    results.append(match_arr)
                return py_to_js(results)
            if name == 'search':
                pat_arg = args[0] if args else UNDEFINED
                # Symbol.search delegation
                sym_search_key = SK_SEARCH
                if isinstance(pat_arg.value, dict) and sym_search_key in pat_arg.value:
                    method = pat_arg.value[sym_search_key]
                    return interp._call_js(method, [JsValue('string', s)], pat_arg)
                if (isinstance(pat_arg, JsValue) and pat_arg.type == 'object' and
                        isinstance(pat_arg.value, dict) and
                        isinstance(pat_arg.value.get('__kind__'), JsValue) and
                        pat_arg.value['__kind__'].value == 'RegExp'):
                    src = interp._to_str(pat_arg.value.get('source', py_to_js('')))
                    flg_str = interp._to_str(pat_arg.value.get('flags', py_to_js('')))
                    py_src = _js_regex_to_python(src)
                    py_flg = 0
                    if 'i' in flg_str: py_flg |= re.IGNORECASE
                    if 'm' in flg_str: py_flg |= re.MULTILINE
                    if 's' in flg_str: py_flg |= re.DOTALL
                    m = re.search(py_src, s, py_flg)
                    return JsValue('number', float(m.start()) if m else -1.0)
                pat = _pattern_text(pat_arg) if pat_arg.type != 'undefined' else ''
                m = re.search(pat, s)
                return JsValue("number", float(m.start()) if m else -1.0)
            if name == 'concat':
                return JsValue("string", s + ''.join(interp._to_str(a) for a in args))
            if name == 'normalize':
                import unicodedata as _ud
                form = interp._to_str(args[0]) if args and args[0].type != 'undefined' else 'NFC'
                if form not in ('NFC', 'NFD', 'NFKC', 'NFKD'):
                    raise _JSError(interp._make_js_error('RangeError',
                        f"The normalization form should be one of NFC, NFD, NFKC, NFKD."))
                return JsValue("string", _ud.normalize(form, s))
            if name == 'at':
                i = int(args[0].value) if args else 0
                if i < 0: i = len(s) + i
                return JsValue("string", s[i]) if 0 <= i < len(s) else UNDEFINED
            if name in ('trimStart', 'trimLeft'):
                return JsValue("string", s.lstrip())
            if name in ('trimEnd', 'trimRight'):
                return JsValue("string", s.rstrip())
            if name == 'codePointAt':
                i = int(interp._to_num(args[0])) if args else 0
                return JsValue("number", ord(s[i]) if 0 <= i < len(s) else float('nan'))
            if name == 'isWellFormed':
                i = 0
                while i < len(s):
                    cp = ord(s[i])
                    if 0xD800 <= cp <= 0xDBFF:
                        if i + 1 < len(s) and 0xDC00 <= ord(s[i + 1]) <= 0xDFFF:
                            i += 2
                            continue
                        return JS_FALSE
                    elif 0xDC00 <= cp <= 0xDFFF:
                        return JS_FALSE
                    i += 1
                return JS_TRUE
            if name == 'localeCompare':
                other = self._to_str(args[0]) if args else ''
                if s < other: return JsValue('number', -1.0)
                if s > other: return JsValue('number', 1.0)
                return JsValue('number', 0.0)
            if name == 'toWellFormed':
                result = []
                i = 0
                while i < len(s):
                    cp = ord(s[i])
                    if 0xD800 <= cp <= 0xDBFF:
                        if i + 1 < len(s) and 0xDC00 <= ord(s[i + 1]) <= 0xDFFF:
                            result.append(s[i:i+2])
                            i += 2
                            continue
                        result.append('\uFFFD')
                    elif 0xDC00 <= cp <= 0xDFFF:
                        result.append('\uFFFD')
                    else:
                        result.append(s[i])
                    i += 1
                return JsValue("string", ''.join(result))
            # Check plugin-registered methods
            plugin_key = ('string', name)
            if interp._plugin_methods and plugin_key in interp._plugin_methods:
                return interp._plugin_methods[plugin_key](this_val, args, interp)
            if name == 'toString' or name == 'valueOf':
                return sval
            return UNDEFINED
        return JsValue("intrinsic", {"fn": fn, "name": f"String.{name}"})

    def _num_method(self, nval: JsValue, name: str):
        interp = self
        n = nval.value
        def fn(this_val, args, extra_args=None):
            if name == 'toExponential':
                d = int(args[0].value) if args and args[0].type == 'number' else None
                try:
                    if d is None:
                        py_result = f'{n:e}'
                    else:
                        py_result = f'{n:.{d}e}'
                    # Convert Python e+05 notation to JS e+5 notation
                    py_result = re.sub(r'e([+-])0*(\d+)', r'e\1\2', py_result)
                    return JsValue('string', py_result)
                except (ValueError, TypeError, OverflowError): return JsValue('string', 'NaN')
            if name == 'toFixed':
                d = int(args[0].value) if args else 0
                try:
                    return JsValue("string", f"{n:.{d}f}")
                except (ValueError, TypeError, OverflowError): return JsValue("string", "NaN")
            if name == 'toPrecision':
                d = int(args[0].value) if args else None
                try:
                    if d is None: return JsValue("string", str(int(n)) if n == int(n) else str(n))
                    # Implement ES spec toPrecision
                    import math as _m
                    if _m.isnan(n): return JsValue('string', 'NaN')
                    if _m.isinf(n): return JsValue('string', 'Infinity' if n > 0 else '-Infinity')
                    if n == 0:
                        if d == 1: return JsValue('string', '0')
                        return JsValue('string', '0.' + '0' * (d - 1))
                    sign = '' if n >= 0 else '-'
                    n_abs = abs(n)
                    e = _m.floor(_m.log10(n_abs))  # exponent
                    # Round to d significant digits
                    rounded = round(n_abs, d - 1 - int(e))
                    # Check if rounding caused e to increase
                    if rounded > 0:
                        new_e = _m.floor(_m.log10(rounded))
                        if new_e > e: e = new_e
                    e = int(e)
                    if e < -6 or e >= d:
                        # Exponential notation
                        s = f'{n_abs:.{d - 1}e}'
                        # Normalize exponent: Python uses e+08, JS uses e+8
                        import re as _re
                        s = _re.sub(r'e([+-])0*(\d+)', lambda m: f'e{m.group(1)}{m.group(2)}', s)
                        return JsValue('string', sign + s)
                    else:
                        # Fixed notation with d significant digits
                        decimal_places = d - 1 - e
                        if decimal_places < 0: decimal_places = 0
                        s = f'{n_abs:.{decimal_places}f}'
                        return JsValue('string', sign + s)
                except (ValueError, TypeError, OverflowError): return JsValue("string", "NaN")
            if name == 'toString':
                base = int(args[0].value) if args else 10
                if base < 2 or base > 36:
                    raise _JSError(interp._make_js_error('RangeError', 'toString() radix must be between 2 and 36'))
                if base == 10: return JsValue("string", str(int(n)) if n==int(n) else str(n))
                import math as _math
                def _n_to_base(num, b):
                    """Convert a non-negative number to base-b string (integer + optional fractional part)."""
                    int_part = int(num)
                    frac_part = num - int_part
                    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
                    if int_part == 0:
                        int_str = '0'
                    else:
                        parts = []
                        tmp = int_part
                        while tmp:
                            parts.append(digits[tmp % b])
                            tmp //= b
                        int_str = ''.join(reversed(parts))
                    if frac_part == 0:
                        return int_str
                    frac_str = ''
                    seen = {}
                    pos = 0
                    while frac_part and pos < 52:
                        frac_part *= b
                        digit = int(frac_part)
                        frac_str += digits[digit]
                        frac_part -= digit
                        pos += 1
                    return int_str + '.' + frac_str
                if _math.isnan(n): return JsValue("string", "NaN")
                if _math.isinf(n): return JsValue("string", "Infinity" if n > 0 else "-Infinity")
                sign = '-' if n < 0 else ''
                return JsValue("string", sign + _n_to_base(abs(n), base))
            if name == 'toLocaleString':
                return JsValue("string", str(n))
            if name == 'valueOf':
                return nval
            # Check plugin-registered methods
            plugin_key = ('number', name)
            if interp._plugin_methods and plugin_key in interp._plugin_methods:
                return interp._plugin_methods[plugin_key](this_val, args, interp)
            return UNDEFINED
        return JsValue("intrinsic", {"fn": fn, "name": f"Number.{name}"})

    # --------------------------------------------------------- ArrayBuffer / TypedArray / DataView
    def _arraybuffer_get_prop(self, buf_val: JsValue, key: str):
        d = buf_val.value
        buf = d.get('__bytes__', bytearray())
        if key == 'byteLength':
            return JsValue('number', float(len(buf)))
        if key == 'resizable':
            return JS_TRUE if d.get('__resizable__') else JS_FALSE
        if key == 'maxByteLength':
            if d.get('__resizable__'):
                return JsValue('number', float(d.get('__max_byte_length__', len(buf))))
            return JsValue('number', float(len(buf)))
        if key == 'detached':
            return JS_TRUE if d.get('__detached__') else JS_FALSE
        if key == 'resize':
            def _resize(this_val, args, interp, _d=d):
                if _d.get('__detached__'):
                    raise _JSError(self._make_js_error('TypeError', 'Cannot resize a detached ArrayBuffer'))
                if not _d.get('__resizable__'):
                    raise _JSError(self._make_js_error('TypeError', 'ArrayBuffer is not resizable'))
                new_len = max(0, int(interp._to_num(args[0])) if args else 0)
                max_bl = _d.get('__max_byte_length__', 0)
                if new_len > max_bl:
                    raise _JSError(self._make_js_error('RangeError', f'Invalid ArrayBuffer resize length {new_len}'))
                cur = _d['__bytes__']
                if new_len > len(cur):
                    cur.extend(bytearray(new_len - len(cur)))
                elif new_len < len(cur):
                    del cur[new_len:]
                return UNDEFINED
            return self._make_intrinsic(_resize, 'ArrayBuffer.resize')
        if key == 'transfer':
            def _transfer(this_val, args, interp, _d=d, _bv=buf_val):
                if _d.get('__detached__'):
                    raise _JSError(self._make_js_error('TypeError', 'Cannot transfer a detached ArrayBuffer'))
                new_len = int(interp._to_num(args[0])) if args and args[0].type == 'number' else len(_d['__bytes__'])
                new_bytes = bytearray(_d['__bytes__'][:new_len])
                if new_len > len(_d['__bytes__']):
                    new_bytes.extend(bytearray(new_len - len(_d['__bytes__'])))
                _d['__detached__'] = True
                _d['__bytes__'] = bytearray()
                new_d = {'__type__': py_to_js('ArrayBuffer'), '__bytes__': new_bytes}
                if _d.get('__resizable__'):
                    new_d['__resizable__'] = True
                    new_d['__max_byte_length__'] = max(new_len, _d.get('__max_byte_length__', new_len))
                return JsValue('object', new_d)
            return self._make_intrinsic(_transfer, 'ArrayBuffer.transfer')
        if key == 'transferToFixedLength':
            def _transfer_fixed(this_val, args, interp, _d=d):
                if _d.get('__detached__'):
                    raise _JSError(self._make_js_error('TypeError', 'Cannot transfer a detached ArrayBuffer'))
                new_len = int(interp._to_num(args[0])) if args and args[0].type == 'number' else len(_d['__bytes__'])
                new_bytes = bytearray(_d['__bytes__'][:new_len])
                if new_len > len(_d['__bytes__']):
                    new_bytes.extend(bytearray(new_len - len(_d['__bytes__'])))
                _d['__detached__'] = True
                _d['__bytes__'] = bytearray()
                return JsValue('object', {'__type__': py_to_js('ArrayBuffer'), '__bytes__': new_bytes})
            return self._make_intrinsic(_transfer_fixed, 'ArrayBuffer.transferToFixedLength')
        if key == 'slice':
            def _slice(this_val, args, interp, _bv=buf_val):
                _b = _bv.value.get('__bytes__', bytearray())
                length = len(_b)
                begin = int(interp._to_num(args[0])) if args else 0
                end = int(interp._to_num(args[1])) if len(args) > 1 else length
                if begin < 0: begin = max(length + begin, 0)
                if end < 0: end = max(length + end, 0)
                begin = min(max(begin, 0), length)
                end = min(max(end, 0), length)
                return JsValue('object', {
                    '__type__': py_to_js('ArrayBuffer'),
                    '__bytes__': bytearray(_b[begin:end]),
                })
            return self._make_intrinsic(_slice, 'ArrayBuffer.slice')
        return UNDEFINED

    def _typed_array_get_prop(self, ta: JsValue, key: str):
        d = ta.value
        fmt = d.get('__fmt__', 'B')
        itemsize = d.get('__itemsize__', 1)
        byteoffset = d.get('__byteoffset__', 0)
        length = d.get('__length__', 0)
        buf = d.get('__bytes__', bytearray())
        is_bigint = fmt in ('q', 'Q')

        if key == 'length':
            return JsValue('number', float(length))
        if key == 'byteLength':
            return JsValue('number', float(length * itemsize))
        if key == 'byteOffset':
            return JsValue('number', float(byteoffset))
        if key == 'BYTES_PER_ELEMENT':
            return JsValue('number', float(itemsize))
        if key == 'buffer':
            if '__buffer_jv__' not in d:
                d['__buffer_jv__'] = JsValue('object', {
                    '__type__': py_to_js('ArrayBuffer'),
                    '__bytes__': buf,
                })
            return d['__buffer_jv__']
        # Numeric index
        try:
            idx = int(key)
            if 0 <= idx < length:
                (val,) = struct.unpack_from('=' + fmt, buf, byteoffset + idx * itemsize)
                return JsValue('bigint', val) if is_bigint else JsValue('number', float(val))
            return UNDEFINED
        except (ValueError, TypeError):
            pass
        return self._typed_array_method(ta, key)

    def _typed_array_method(self, ta: JsValue, name: str):
        d = ta.value
        fmt = d.get('__fmt__', 'B')
        itemsize = d.get('__itemsize__', 1)
        byteoffset = d.get('__byteoffset__', 0)
        length = d.get('__length__', 0)
        buf = d.get('__bytes__', bytearray())
        ta_name = d.get('__name__', 'TypedArray')
        is_bigint = fmt in ('q', 'Q')

        def _read(idx, _buf=buf, _off=byteoffset, _fmt=fmt, _itemsize=itemsize, _ib=is_bigint):
            (v,) = struct.unpack_from('=' + _fmt, _buf, _off + idx * _itemsize)
            return JsValue('bigint', v) if _ib else JsValue('number', float(v))

        def _write(idx, jsv, _buf=buf, _off=byteoffset, _fmt=fmt, _itemsize=itemsize, _ib=is_bigint, _ta_name=ta_name):
            if _ib:
                raw = jsv.value if jsv.type == 'bigint' else int(self._to_num(jsv))
            elif _ta_name == 'Uint8ClampedArray':
                raw = max(0, min(255, int(self._to_num(jsv))))
            else:
                raw = _ta_coerce(jsv, _fmt, self)
            struct.pack_into('=' + _fmt, _buf, _off + idx * _itemsize, raw)

        sym_iter_key = SK_ITERATOR
        if name == sym_iter_key:
            def _iter_factory(this_val, args, interp, _r=_read, _len=length):
                idx = [0]
                it = JsValue('object', {})
                def _next(tv, a, intp, _idx=idx, _rlen=_len, _rr=_r):
                    if _idx[0] >= _rlen:
                        return JsValue('object', {'value': UNDEFINED, 'done': JS_TRUE})
                    v = _rr(_idx[0]); _idx[0] += 1
                    return JsValue('object', {'value': v, 'done': JS_FALSE})
                it.value['next'] = interp._make_intrinsic(_next, 'TypedArrayIterator.next')
                it.value[sym_iter_key] = interp._make_intrinsic(lambda tv, a, i: it, '[Symbol.iterator]')
                interp._add_iterator_helpers(it)
                return it
            return self._make_intrinsic(_iter_factory, '[Symbol.iterator]')

        if name == 'set':
            def _set(this_val, args, interp, _w=_write, _len=length):
                src = args[0] if args else UNDEFINED
                off = int(interp._to_num(args[1])) if len(args) > 1 else 0
                items = list(src.value) if src.type == 'array' else interp._array_like_items(src)
                for i, item in enumerate(items):
                    if 0 <= off + i < _len:
                        _w(off + i, item)
                return UNDEFINED
            return self._make_intrinsic(_set, 'TypedArray.set')

        if name == 'subarray':
            def _subarray(this_val, args, interp, _ta=ta):
                _d = _ta.value
                _b = _d.get('__bytes__', bytearray())
                _f = _d.get('__fmt__', 'B')
                _is = _d.get('__itemsize__', 1)
                _bo = _d.get('__byteoffset__', 0)
                _ln = _d.get('__length__', 0)
                begin = int(interp._to_num(args[0])) if args else 0
                end = int(interp._to_num(args[1])) if len(args) > 1 else _ln
                if begin < 0: begin = max(_ln + begin, 0)
                if end < 0: end = max(_ln + end, 0)
                begin = min(max(begin, 0), _ln)
                end = min(max(end, 0), _ln)
                new_len = max(0, end - begin)
                new_off = _bo + begin * _is
                new_ta = JsValue('object', {
                    '__type__': py_to_js('TypedArray'),
                    '__name__': _d.get('__name__', 'TypedArray'),
                    '__bytes__': _b,
                    '__fmt__': _f,
                    '__itemsize__': _is,
                    '__byteoffset__': new_off,
                    '__length__': new_len,
                })
                return new_ta
            return self._make_intrinsic(_subarray, 'TypedArray.subarray')

        if name == 'slice':
            def _slice(this_val, args, interp, _r=_read, _ta=ta):
                _d = _ta.value
                _f = _d.get('__fmt__', 'B')
                _is = _d.get('__itemsize__', 1)
                _ln = _d.get('__length__', 0)
                _ib = _f in ('q', 'Q')
                begin = int(interp._to_num(args[0])) if args else 0
                end = int(interp._to_num(args[1])) if len(args) > 1 else _ln
                if begin < 0: begin = max(_ln + begin, 0)
                if end < 0: end = max(_ln + end, 0)
                begin = min(max(begin, 0), _ln)
                end = min(max(end, 0), _ln)
                new_len = max(0, end - begin)
                new_buf = bytearray(new_len * _is)
                for i in range(new_len):
                    v = _r(begin + i)
                    raw = v.value if _ib else _ta_coerce(v, _f, interp)
                    struct.pack_into('=' + _f, new_buf, i * _is, raw)
                return JsValue('object', {
                    '__type__': py_to_js('TypedArray'),
                    '__name__': _d.get('__name__', 'TypedArray'),
                    '__bytes__': new_buf,
                    '__fmt__': _f,
                    '__itemsize__': _is,
                    '__byteoffset__': 0,
                    '__length__': new_len,
                })
            return self._make_intrinsic(_slice, 'TypedArray.slice')

        if name == 'fill':
            def _fill(this_val, args, interp, _ta=ta, _w=_write, _len=length):
                v = args[0] if args else UNDEFINED
                start = int(interp._to_num(args[1])) if len(args) > 1 else 0
                end = int(interp._to_num(args[2])) if len(args) > 2 else _len
                if start < 0: start = max(_len + start, 0)
                if end < 0: end = max(_len + end, 0)
                for i in range(max(0, start), min(end, _len)):
                    _w(i, v)
                return _ta
            return self._make_intrinsic(_fill, 'TypedArray.fill')

        if name == 'indexOf':
            def _indexOf(this_val, args, interp, _r=_read, _len=length):
                target = args[0] if args else UNDEFINED
                start = int(interp._to_num(args[1])) if len(args) > 1 else 0
                if start < 0: start = max(_len + start, 0)
                for i in range(start, _len):
                    if interp._strict_eq(_r(i), target):
                        return JsValue('number', float(i))
                return JsValue('number', -1.0)
            return self._make_intrinsic(_indexOf, 'TypedArray.indexOf')

        if name == 'includes':
            def _includes(this_val, args, interp, _r=_read, _len=length):
                target = args[0] if args else UNDEFINED
                for i in range(_len):
                    if interp._strict_eq(_r(i), target):
                        return JS_TRUE
                return JS_FALSE
            return self._make_intrinsic(_includes, 'TypedArray.includes')

        if name == 'join':
            def _join(this_val, args, interp, _r=_read, _len=length):
                sep = args[0].value if args and args[0].type == 'string' else ','
                return JsValue('string', sep.join(interp._to_str(_r(i)) for i in range(_len)))
            return self._make_intrinsic(_join, 'TypedArray.join')

        if name == 'reverse':
            def _reverse(this_val, args, interp, _ta=ta, _r=_read, _w=_write, _len=length):
                items = [_r(i) for i in range(_len)]
                items.reverse()
                for i, item in enumerate(items):
                    _w(i, item)
                return _ta
            return self._make_intrinsic(_reverse, 'TypedArray.reverse')

        if name == 'sort':
            def _sort(this_val, args, interp, _ta=ta, _r=_read, _w=_write, _len=length):
                cb = args[0] if args and interp._is_callable(args[0]) else None
                items = [_r(i) for i in range(_len)]
                if cb:
                    def _cmp(x, y):
                        r = interp._to_num(interp._call_js(cb, [x, y], None))
                        return -1 if r < 0 else (1 if r > 0 else 0)
                    items.sort(key=functools.cmp_to_key(_cmp))
                else:
                    items.sort(key=lambda x: x.value if not (isinstance(x.value, float) and math.isnan(x.value)) else float('inf'))
                for i, item in enumerate(items):
                    _w(i, item)
                return _ta
            return self._make_intrinsic(_sort, 'TypedArray.sort')

        if name == 'forEach':
            def _forEach(this_val, args, interp, _r=_read, _len=length, _ta=ta):
                cb = args[0] if args else UNDEFINED
                if interp._is_callable(cb):
                    for i in range(_len):
                        interp._call_js(cb, [_r(i), JsValue('number', float(i)), _ta], UNDEFINED)
                return UNDEFINED
            return self._make_intrinsic(_forEach, 'TypedArray.forEach')

        if name == 'map':
            def _map(this_val, args, interp, _r=_read, _len=length, _ta=ta, _d=d):
                cb = args[0] if args else UNDEFINED
                _f = _d.get('__fmt__', 'B'); _is = _d.get('__itemsize__', 1)
                new_buf = bytearray(_len * _is)
                new_ta = JsValue('object', {
                    '__type__': py_to_js('TypedArray'),
                    '__name__': _d.get('__name__', 'TypedArray'),
                    '__bytes__': new_buf, '__fmt__': _f, '__itemsize__': _is,
                    '__byteoffset__': 0, '__length__': _len,
                })
                if interp._is_callable(cb):
                    for i in range(_len):
                        rv = interp._call_js(cb, [_r(i), JsValue('number', float(i)), _ta], UNDEFINED)
                        interp._set_prop(new_ta, str(i), rv)
                return new_ta
            return self._make_intrinsic(_map, 'TypedArray.map')

        if name == 'filter':
            def _filter(this_val, args, interp, _r=_read, _len=length, _ta=ta, _d=d):
                cb = args[0] if args else UNDEFINED
                items = []
                if interp._is_callable(cb):
                    for i in range(_len):
                        v = _r(i)
                        if interp._truthy(interp._call_js(cb, [v, JsValue('number', float(i)), _ta], UNDEFINED)):
                            items.append(v)
                _f = _d.get('__fmt__', 'B'); _is = _d.get('__itemsize__', 1)
                new_buf = bytearray(len(items) * _is)
                new_ta = JsValue('object', {
                    '__type__': py_to_js('TypedArray'),
                    '__name__': _d.get('__name__', 'TypedArray'),
                    '__bytes__': new_buf, '__fmt__': _f, '__itemsize__': _is,
                    '__byteoffset__': 0, '__length__': len(items),
                })
                for i, item in enumerate(items):
                    interp._set_prop(new_ta, str(i), item)
                return new_ta
            return self._make_intrinsic(_filter, 'TypedArray.filter')

        if name == 'find':
            def _find(this_val, args, interp, _r=_read, _len=length, _ta=ta):
                cb = args[0] if args else UNDEFINED
                if interp._is_callable(cb):
                    for i in range(_len):
                        v = _r(i)
                        if interp._truthy(interp._call_js(cb, [v, JsValue('number', float(i)), _ta], UNDEFINED)):
                            return v
                return UNDEFINED
            return self._make_intrinsic(_find, 'TypedArray.find')

        if name == 'findIndex':
            def _findIndex(this_val, args, interp, _r=_read, _len=length, _ta=ta):
                cb = args[0] if args else UNDEFINED
                if interp._is_callable(cb):
                    for i in range(_len):
                        v = _r(i)
                        if interp._truthy(interp._call_js(cb, [v, JsValue('number', float(i)), _ta], UNDEFINED)):
                            return JsValue('number', float(i))
                return JsValue('number', -1.0)
            return self._make_intrinsic(_findIndex, 'TypedArray.findIndex')

        if name == 'every':
            def _every(this_val, args, interp, _r=_read, _len=length, _ta=ta):
                cb = args[0] if args else UNDEFINED
                if interp._is_callable(cb):
                    for i in range(_len):
                        if not interp._truthy(interp._call_js(cb, [_r(i), JsValue('number', float(i)), _ta], UNDEFINED)):
                            return JS_FALSE
                return JS_TRUE
            return self._make_intrinsic(_every, 'TypedArray.every')

        if name == 'some':
            def _some(this_val, args, interp, _r=_read, _len=length, _ta=ta):
                cb = args[0] if args else UNDEFINED
                if interp._is_callable(cb):
                    for i in range(_len):
                        if interp._truthy(interp._call_js(cb, [_r(i), JsValue('number', float(i)), _ta], UNDEFINED)):
                            return JS_TRUE
                return JS_FALSE
            return self._make_intrinsic(_some, 'TypedArray.some')

        if name == 'reduce':
            def _reduce(this_val, args, interp, _r=_read, _len=length, _ta=ta):
                cb = args[0] if args else UNDEFINED
                has_init = len(args) > 1
                acc = args[1] if has_init else (_r(0) if _len > 0 else None)
                start = 0 if has_init else 1
                if acc is None:
                    raise _JSError(py_to_js('Reduce of empty array with no initial value'))
                if interp._is_callable(cb):
                    for i in range(start, _len):
                        acc = interp._call_js(cb, [acc, _r(i), JsValue('number', float(i)), _ta], UNDEFINED)
                return acc
            return self._make_intrinsic(_reduce, 'TypedArray.reduce')

        if name == 'toString':
            def _toString(this_val, args, interp, _r=_read, _len=length):
                return JsValue('string', ','.join(interp._to_str(_r(i)) for i in range(_len)))
            return self._make_intrinsic(_toString, 'TypedArray.toString')

        if name == 'at':
            def _at(this_val, args, interp, _r=_read, _len=length):
                idx = int(interp._to_num(args[0])) if args else 0
                if idx < 0: idx += _len
                return _r(idx) if 0 <= idx < _len else UNDEFINED
            return self._make_intrinsic(_at, 'TypedArray.at')

        # ES2025: Uint8Array-specific base64 and hex instance methods
        if ta_name == 'Uint8Array':
            import base64 as _b64mod
            raw_bytes = bytes(buf[byteoffset:byteoffset + length])
            if name == 'toBase64':
                def _to_base64(this_val, args, interp, _rb=raw_bytes):
                    opts = args[0] if args and args[0].type == 'object' else None
                    alphabet = interp._to_str(opts.value.get('alphabet', UNDEFINED)) if opts and opts.value.get('alphabet') else 'base64'
                    omit_padding = interp._truthy(opts.value.get('omitPadding', JS_FALSE)) if opts else False
                    if alphabet == 'base64url':
                        encoded = _b64mod.urlsafe_b64encode(_rb).decode('ascii')
                    else:
                        encoded = _b64mod.b64encode(_rb).decode('ascii')
                    if omit_padding:
                        encoded = encoded.rstrip('=')
                    return JsValue('string', encoded)
                return self._make_intrinsic(_to_base64, 'Uint8Array.toBase64')
            if name == 'toHex':
                def _to_hex(this_val, args, interp, _rb=raw_bytes):
                    return JsValue('string', _rb.hex())
                return self._make_intrinsic(_to_hex, 'Uint8Array.toHex')

        return UNDEFINED

    def _dataview_get_prop(self, dv: JsValue, key: str):
        d = dv.value
        buf_jv = d.get('__buffer__')
        byteoffset = d.get('__byteoffset__', 0)
        bytelength = d.get('__bytelength__', 0)

        if key == 'buffer':
            return buf_jv if isinstance(buf_jv, JsValue) else JsValue('object', {
                '__type__': py_to_js('ArrayBuffer'), '__bytes__': bytearray()
            })
        if key == 'byteOffset':
            return JsValue('number', float(byteoffset))
        if key == 'byteLength':
            return JsValue('number', float(bytelength))

        _TYPE_MAP = {
            'Int8': ('b', 1), 'Uint8': ('B', 1),
            'Int16': ('h', 2), 'Uint16': ('H', 2),
            'Int32': ('i', 4), 'Uint32': ('I', 4),
            'Float16': ('e', 2), 'Float32': ('f', 4), 'Float64': ('d', 8),
            'BigInt64': ('q', 8), 'BigUint64': ('Q', 8),
        }

        for type_name, (fmt, size) in _TYPE_MAP.items():
            if key == f'get{type_name}':
                def _getter(this_val, args, interp, _bv=buf_jv, _base=byteoffset, _f=fmt, _ib=fmt in ('q', 'Q')):
                    _b = _bv.value.get('__bytes__', bytearray()) if isinstance(_bv, JsValue) else bytearray()
                    offset = int(interp._to_num(args[0])) if args else 0
                    le = interp._truthy(args[1]) if len(args) > 1 else False
                    endian = '<' if le else '>'
                    try:
                        (val,) = struct.unpack_from(endian + _f, _b, _base + offset)
                        return JsValue('bigint', val) if _ib else JsValue('number', float(val))
                    except struct.error as e:
                        raise _JSError(py_to_js(str(e)))
                return self._make_intrinsic(_getter, f'DataView.get{type_name}')
            if key == f'set{type_name}':
                def _setter(this_val, args, interp, _bv=buf_jv, _base=byteoffset, _f=fmt, _ib=fmt in ('q', 'Q')):
                    _b = _bv.value.get('__bytes__', bytearray()) if isinstance(_bv, JsValue) else bytearray()
                    offset = int(interp._to_num(args[0])) if args else 0
                    val_arg = args[1] if len(args) > 1 else UNDEFINED
                    le = interp._truthy(args[2]) if len(args) > 2 else False
                    endian = '<' if le else '>'
                    if _ib:
                        raw = val_arg.value if val_arg.type == 'bigint' else int(interp._to_num(val_arg))
                    else:
                        raw = _ta_coerce(val_arg, _f, interp)
                    try:
                        struct.pack_into(endian + _f, _b, _base + offset, raw)
                    except struct.error as e:
                        raise _JSError(py_to_js(str(e)))
                    return UNDEFINED
                return self._make_intrinsic(_setter, f'DataView.set{type_name}')

        return UNDEFINED

    # --------------------------------------------------------- comparison
    def _strict_eq(self, a: JsValue, b: JsValue) -> bool:
        if a.type != b.type: return False
        if a.type in ('null','undefined'): return True
        if a.type == 'number':
            if math.isnan(a.value) or math.isnan(b.value): return False
            return a.value == b.value
        if a.type == 'symbol':
            return a.value['id'] == b.value['id']
        return a.value == b.value

    def _eq(self, a: JsValue, b: JsValue) -> bool:
        if a.type == b.type: return self._strict_eq(a, b)
        if a.type in ('null','undefined') and b.type in ('null','undefined'): return True
        if a.type == 'number' and b.type == 'string': return self._eq(a, JsValue("number", self._to_num(b)))
        if a.type == 'string' and b.type == 'number': return self._eq(JsValue("number", self._to_num(a)), b)
        if a.type == 'boolean': return self._eq(JsValue("number", 1 if a.value else 0), b)
        if b.type == 'boolean': return self._eq(a, JsValue("number", 1 if b.value else 0))
        _obj_types = ('object', 'array', 'function', 'intrinsic', 'class')
        if (a.type == 'string' or a.type == 'number') and b.type in _obj_types:
            return self._eq(a, self._to_primitive(b))
        if a.type in _obj_types and (b.type == 'string' or b.type == 'number'):
            return self._eq(self._to_primitive(a), b)
        return False

    def _cmp(self, op, a: JsValue, b: JsValue):
        # ES spec: apply ToPrimitive with 'number' hint, then if both strings compare lexically
        ap = self._to_primitive(a, 'number')
        bp = self._to_primitive(b, 'number')
        if ap.type == 'string' and bp.type == 'string':
            lv, rv = ap.value, bp.value
        elif ap.type == 'bigint' and bp.type == 'bigint':
            lv, rv = ap.value, bp.value
        else:
            lv, rv = self._to_num(ap), self._to_num(bp)
        if op == '<':  return lv <  rv
        if op == '>':  return lv >  rv
        if op == '<=': return lv <= rv
        if op == '>=': return lv >= rv
        return False

    # --------------------------------------------------------- execution
    @staticmethod
    def _hoist_tdz(stmts, block_env):
        """Pre-scan a block body and create TDZ entries for let/const declarations."""
        for s in stmts:
            if s.get("type") == "VariableDeclaration" and s["kind"] in ("let", "const"):
                for d in s["declarations"]:
                    name = d["id"]
                    if isinstance(name, str):
                        block_env.declare_tdz(name, s["kind"])
                    elif isinstance(name, dict) and name.get("type") == "Identifier":
                        block_env.declare_tdz(name.get("name", name), s["kind"])

    @staticmethod
    def _compute_block_scope_info(stmts):
        """Compute and cache (tdz_entries, needs_env) for a block body.
        tdz_entries: list of (name, keyword) for let/const declarations.
        needs_env: True if block needs its own scope (has let/const/using/FunctionDecl).
        """
        tdz_entries = []
        needs_env = False
        for s in stmts:
            if not isinstance(s, dict):
                continue
            stype = s.get("type")
            if stype == "VariableDeclaration" and s.get("kind") in ("let", "const"):
                needs_env = True
                for d in s.get("declarations", []):
                    name = d.get("id")
                    if isinstance(name, str):
                        tdz_entries.append((name, s["kind"]))
                    elif isinstance(name, dict) and name.get("type") == "Identifier":
                        tdz_entries.append((name.get("name", "?"), s["kind"]))
            elif stype in ("FunctionDeclaration", "UsingDeclaration"):
                needs_env = True
        return (tdz_entries, needs_env)

    @staticmethod
    def _collect_var_names(node):
        """Recursively collect all var-declared names in a subtree, skipping nested functions."""
        cached = node.get('__vn__')
        if cached is not None:
            return cached
        names = []
        if not isinstance(node, dict):
            return names
        tp = node.get("type")
        # Stop at function boundaries
        if tp in ("FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression"):
            return names
        if tp == "VariableDeclaration" and node.get("kind") == "var":
            for d in node.get("declarations", []):
                Interpreter._extract_binding_names(d.get("id"), names)
            return names
        # Recurse into statement bodies
        for key in ("body", "consequent", "alternate", "block", "handler", "finalizer",
                     "cases", "declarations", "init", "update"):
            child = node.get(key)
            if isinstance(child, list):
                for item in child:
                    if isinstance(item, dict):
                        names.extend(Interpreter._collect_var_names(item))
            elif isinstance(child, dict):
                names.extend(Interpreter._collect_var_names(child))
        node['__vn__'] = names
        return names

    @staticmethod
    def _extract_binding_names(pattern, names):
        """Extract variable names from a binding pattern (Identifier, ObjectPattern, ArrayPattern)."""
        if isinstance(pattern, str):
            names.append(pattern)
        elif isinstance(pattern, dict):
            tp = pattern.get("type")
            if tp == "Identifier":
                names.append(pattern["name"])
            elif tp == "ObjectPattern":
                for prop in pattern.get("properties", []):
                    Interpreter._extract_binding_names(prop.get("value", prop.get("argument")), names)
            elif tp == "ArrayPattern":
                for elem in pattern.get("elements", []):
                    if elem is not None:
                        Interpreter._extract_binding_names(elem.get("argument", elem) if isinstance(elem, dict) and elem.get("type") == "RestElement" else elem, names)
            elif tp == "AssignmentPattern":
                Interpreter._extract_binding_names(pattern.get("left"), names)
            elif tp == "RestElement":
                Interpreter._extract_binding_names(pattern.get("argument"), names)

    @staticmethod
    def _hoist_vars(stmts, fn_env):
        """Pre-scan function/program body and hoist var declarations to fn_env."""
        for s in stmts:
            for name in Interpreter._collect_var_names(s):
                if name not in fn_env.bindings:
                    fn_env.bindings[name] = ['var', UNDEFINED]

    @staticmethod
    def _has_use_strict(stmts):
        """Check if the first statement is a 'use strict' directive."""
        if not stmts:
            return False
        s = stmts[0]
        if s.get("type") == "ExpressionStatement":
            expr = s.get("expression", {})
            if expr.get("type") == "Literal" and expr.get("value") == "use strict":
                return True
        return False

    # ---- _exec dispatch methods ----

    def _exec_program(self, node, env):
        self._hoist_vars(node["body"], env)
        if self._has_use_strict(node["body"]):
            env._strict = True
        for s in node["body"]:
            r = self._exec(s, env)
            if r is not None: return r
        return None

    def _exec_variable_declaration(self, node, env):
        _kind = node["kind"]
        for d in node["declarations"]:
            val = UNDEFINED
            if d["init"]:
                val = self._eval(d["init"], env)
                # ES2015 function name inference
                _id = d["id"]
                if (_id.__class__ is dict and _id["type"] == "Identifier"
                        and val.type in ('function', 'intrinsic', 'class')
                        and val.value.__class__ is dict
                        and not val.value.get("name")):
                    val.value["name"] = _id["name"]
            if _TRACE_ACTIVE[0]:
                _id = d["id"]
                _vname = _id.get("name", "?") if isinstance(_id, dict) and _id.get("type") == "Identifier" else "<pattern>"
                _log_scope.debug("declare %s %s", _kind, _vname)
                if _log_scope.isEnabledFor(TRACE):
                    _log_scope.log(TRACE, "  %s = %s", _vname, self._to_str(val)[:60])
            try:
                self._bind_pattern(d["id"], val, env, _kind, True)
            except JSTypeError as e:
                raise _JSError(py_to_js(str(e)))
        return None

    def _exec_using_declaration(self, node, env):
        """Execute `using x = expr` or `await using x = expr` (ES2024)."""
        is_async = node.get('is_async', False)
        dispose_sym_key = SK_ASYNC_DISPOSE if is_async else SK_DISPOSE
        for d in node['declarations']:
            val = UNDEFINED
            if d['init']:
                val = self._eval(d['init'], env)
            name = d['id'].get('name', '?') if isinstance(d['id'], dict) else '?'
            # Verify the value has a dispose method (unless null/undefined)
            if val.type not in ('null', 'undefined'):
                dispose_fn = self._get_prop(val, dispose_sym_key)
                if dispose_fn is None or dispose_fn.type in ('null', 'undefined'):
                    raise _JSError(self._make_js_error('TypeError',
                        f"The resource does not have a [Symbol.dispose] method"))
            # Declare binding
            self._bind_pattern(d['id'], val, env, 'const', True)
            # Register for disposal on scope exit (using env's disposal stack)
            if env._using_stack is None:
                env._using_stack = []
            env._using_stack.append((val, dispose_sym_key, is_async))
        return None

    def _run_using_stack(self, env):
        """Run all `using` disposals registered in env's stack (LIFO). Called on scope exit."""
        stack = getattr(env, '_using_stack', [])
        first_err = None
        for val, sym_key, is_async in reversed(stack):
            if val.type in ('null', 'undefined'):
                continue
            dispose_fn = self._get_prop(val, sym_key)
            if not dispose_fn or not isinstance(dispose_fn, JsValue) or dispose_fn.type in ('null', 'undefined'):
                continue
            try:
                result = self._call_js(dispose_fn, [], val)
                if is_async and result.type == 'promise':
                    # For await using: run event loop to settle the dispose promise
                    self._run_event_loop(result)
            except _JSError as e:
                if first_err is None:
                    first_err = e
        if first_err is not None:
            raise first_err

    def _exec_function_declaration(self, node, env):
        fn = self._make_fn(node, env)
        env.declare(node["id"], fn, 'var')
        self._sync_global_binding(node["id"], fn, env)
        return None

    def _exec_class_declaration(self, node, env):
        cname = node["id"]
        super_name = node.get("superClass")
        if isinstance(super_name, str):
            parent_class = env.get(super_name) if super_name else None
        elif isinstance(super_name, dict):
            parent_class = self._eval(super_name, env)
        else:
            parent_class = None
        methods = node["body"]
        proto = JsValue("object", {})
        if isinstance(parent_class, JsValue) and parent_class.type == 'null':
            # class C extends null — inherits from nothing; constructor must call Object.create(null)-style
            parent_proto = None
            proto.value["__proto__"] = JS_NULL  # sentinel: null prototype chain
        elif isinstance(parent_class, JsValue):
            parent_proto = parent_class.value.get("prototype") if isinstance(parent_class.value, dict) else None
            if isinstance(parent_proto, JsValue) and parent_proto.type == "object":
                proto.value["__proto__"] = parent_proto
        else:
            parent_proto = None
        ctor = None
        static_methods = {}
        instance_fields = []
        static_fields = []
        static_blocks = []
        for m in methods:
            mtype = m.get("type") if isinstance(m, dict) else None
            if mtype == "ClassField":
                if m.get("static_"):
                    static_fields.append(m)
                else:
                    instance_fields.append(m)
                continue
            if mtype == "StaticBlock":
                static_blocks.append(m)
                continue
            actual_key = m["key"]
            if m.get("computed") and m.get("computed_key"):
                computed_val = self._eval(m["computed_key"], env)
                if computed_val.type == 'symbol':
                    actual_key = f"@@{computed_val.value['id']}@@"
                else:
                    actual_key = self._to_str(computed_val)
            fn_node = N.FnExpr(actual_key, m["params"], m["body"], False, m.get("async", False), m.get("generator", False))
            fn_val = self._make_fn(fn_node, env)
            if parent_proto is not None:
                fn_val.value['super_proto'] = parent_proto
            if isinstance(parent_class, JsValue) and m.get('static'):
                fn_val.value['super_proto'] = parent_class
            if actual_key == "constructor":
                ctor = fn_val
            elif m.get("static"):
                kind = m.get("kind", "method")
                if kind == "get":
                    static_methods[f"__get__{actual_key}"] = fn_val
                elif kind == "set":
                    static_methods[f"__set__{actual_key}"] = fn_val
                else:
                    static_methods[actual_key] = fn_val
            else:
                kind = m.get("kind", "method")
                if kind == "get":
                    proto.value[f"__get__{actual_key}"] = fn_val
                elif kind == "set":
                    proto.value[f"__set__{actual_key}"] = fn_val
                else:
                    proto.value[actual_key] = fn_val
                    # Class prototype methods are non-enumerable per spec
                    self._set_desc(proto, actual_key, {'enumerable': False, 'writable': True, 'configurable': True})
        if not ctor:
            def _default_ctor(this_val, args, interp):
                if parent_class:
                    interp._call_js(parent_class, args, this_val)
                return this_val
            ctor = JsValue("intrinsic", {"fn": _default_ctor, "name": cname})
        # Always stamp the declared class name onto the constructor
        ctor.value["name"] = cname
        ctor.value["prototype"] = proto
        proto.value["constructor"] = ctor
        # constructor is non-enumerable per spec
        self._set_desc(proto, "constructor", {'enumerable': False, 'writable': True, 'configurable': True})
        ctor.value["superClass"] = parent_class
        ctor.value.update(static_methods)
        if isinstance(parent_class, JsValue):
            ctor.value["__proto__"] = parent_class
        # Declare the class binding BEFORE evaluating static field initializers so that
        # self-referential patterns like `static y = ClassName.x + 1` work correctly.
        env.declare(cname, ctor, 'let')
        self._sync_global_binding(cname, ctor, env)
        for sf in static_fields:
            sf_key = sf["key"]
            if sf.get("computed") and sf.get("computed_key"):
                computed_val = self._eval(sf["computed_key"], env)
                if computed_val.type == 'symbol':
                    sf_key = f"@@{computed_val.value['id']}@@"
                else:
                    sf_key = self._to_str(computed_val)
            sf_val = self._eval(sf["value"], env) if sf.get("value") else UNDEFINED
            ctor.value[sf_key] = sf_val
        if instance_fields:
            ctor.value["__instance_fields__"] = instance_fields

        # Apply member (method/field) decorators
        proto = ctor.value.get("prototype")
        class_initializers = []
        for member in node["body"]:
            member_decs = member.get("decorators", []) if isinstance(member, dict) else []
            if not member_decs:
                continue
            mtype = member.get("type") if isinstance(member, dict) else None
            if mtype == "ClassField":
                key = member["key"]
                is_static = member.get("static_", False)
                for dec_node in reversed(member_decs):
                    dec_fn = self._eval(dec_node["expression"], env)
                    inits = []
                    ctx = JsValue('object', {
                        'kind': JsValue('string', 'field'),
                        'name': JsValue('string', str(key)),
                        'static': JS_TRUE if is_static else JS_FALSE,
                        'addInitializer': self._make_intrinsic(
                            lambda tv, a, i, _l=inits: _l.append(a[0] if a else UNDEFINED) or UNDEFINED,
                            'addInitializer'),
                    })
                    self._call_js(dec_fn, [UNDEFINED, ctx], UNDEFINED)
                    class_initializers.extend(inits)
            else:
                # method decorator
                key = member.get("key", "")
                is_static = member.get("static", False)
                if is_static:
                    method_val = ctor.value.get(key, UNDEFINED)
                else:
                    method_val = proto.value.get(key, UNDEFINED) if proto and proto.type == 'object' else UNDEFINED
                for dec_node in reversed(member_decs):
                    dec_fn = self._eval(dec_node["expression"], env)
                    inits = []
                    ctx = JsValue('object', {
                        'kind': JsValue('string', 'method'),
                        'name': JsValue('string', str(key)),
                        'static': JS_TRUE if is_static else JS_FALSE,
                        'addInitializer': self._make_intrinsic(
                            lambda tv, a, i, _l=inits: _l.append(a[0] if a else UNDEFINED) or UNDEFINED,
                            'addInitializer'),
                    })
                    result = self._call_js(dec_fn, [method_val, ctx], UNDEFINED)
                    if result.type not in ('undefined', 'null'):
                        method_val = result
                        if is_static:
                            ctor.value[key] = method_val
                        elif proto and proto.type == 'object':
                            proto.value[key] = method_val
                    class_initializers.extend(inits)

        # Apply class-level decorators (outermost last → applied in reverse)
        class_decs = node.get("decorators", [])
        for dec_node in reversed(class_decs):
            dec_fn = self._eval(dec_node["expression"], env)
            inits = []
            ctx = JsValue('object', {
                'kind': JsValue('string', 'class'),
                'name': JsValue('string', cname),
                'addInitializer': self._make_intrinsic(
                    lambda tv, a, i, _l=inits: _l.append(a[0] if a else UNDEFINED) or UNDEFINED,
                    'addInitializer'),
            })
            result = self._call_js(dec_fn, [ctor, ctx], UNDEFINED)
            if result.type not in ('undefined', 'null'):
                ctor = result
            class_initializers.extend(inits)

        # Run class-level initializers
        for init_fn in class_initializers:
            if init_fn.type in ('function', 'intrinsic'):
                self._call_js(init_fn, [ctor], ctor)

        # Update the class binding (may have been replaced by class decorator)
        # and declare before running static blocks so they can reference the class.
        try:
            env.set(cname, ctor)
        except Exception:
            env.declare(cname, ctor, 'let')
        self._sync_global_binding(cname, ctor, env)
        for sb in static_blocks:
            sb_env = Environment(env)
            sb_env._is_fn_env = True
            sb_env._is_arrow = False
            sb_env._this = ctor
            self._exec(sb["body"], sb_env)
        return None

    def _exec_block_statement(self, node, env):
        # Get or compute cached block scope info
        try:
            scope_info = node['__scope_info__']
        except KeyError:
            scope_info = Interpreter._compute_block_scope_info(node["body"])
            node['__scope_info__'] = scope_info
        tdz_entries, needs_env = scope_info

        if not needs_env:
            # Fast path: no let/const/using/FunctionDecl — reuse parent env directly
            for s in node["body"]:
                r = self._exec(s, env)
                if r is not None:
                    return r
            return None

        # Slow path: needs its own scope
        block_env = Environment(env)
        if _TRACE_ACTIVE[0]:
            _log_scope.log(TRACE, "scope create (block)")
        for name, keyword in tdz_entries:
            block_env.bindings[name] = [keyword, _TDZ_SENTINEL]
        exc = None
        result = None
        try:
            for s in node["body"]:
                r = self._exec(s, block_env)
                if r is not None:
                    result = r
                    break
        except _JSError as e:
            exc = e
        except _JSReturn as e:
            exc = e
        except _JSBreak as e:
            exc = e
        except _JSContinue as e:
            exc = e
        # Run `using` disposals if any were registered in this block
        if block_env._using_stack:
            try:
                self._run_using_stack(block_env)
            except _JSError as dispose_err:
                if exc is None:
                    exc = dispose_err
        if exc is not None:
            raise exc
        return result

    def _exec_expression_statement(self, node, env):
        # Inline _eval dispatch to avoid one method call layer
        expr = node["expression"]
        try:
            expr['__eh__'](expr, env)
        except KeyError:
            self._eval(expr, env)
        return None

    def _exec_if_statement(self, node, env):
        test = self._eval(node["test"], env)
        _tt = test.type
        if _tt == 'boolean':
            _truthy = test.value
        elif _tt == 'number':
            _v = test.value
            _truthy = _v != 0 and _v == _v
        elif _tt == 'string':
            _truthy = len(test.value) > 0
        elif _tt == 'undefined' or _tt == 'null':
            _truthy = False
        else:
            _truthy = True
        if _truthy:
            return self._exec(node["consequent"], env)
        elif node.get("alternate"):
            return self._exec(node["alternate"], env)
        return None

    def _exec_while_statement(self, node, env):
        _exec_steps = self._exec_steps
        _MAX = self.MAX_EXEC_STEPS
        _test = node["test"]
        while True:
            _tv = self._eval(_test, env)
            _tt = _tv.type
            if _tt == 'boolean':
                if not _tv.value: break
            elif _tt == 'number':
                if _tv.value == 0 or _tv.value != _tv.value: break
            elif _tt == 'string':
                if not _tv.value: break
            elif _tt == 'undefined' or _tt == 'null':
                break
            _exec_steps += 1
            if _exec_steps > _MAX:
                self._exec_steps = _exec_steps
                raise _JSError(self._make_js_error('RangeError', 'Execution step limit exceeded (possible infinite loop)'))
            try:
                r = self._exec(node["body"], env)
                if r is not None:
                    if r == _BREAK: break
                    if r == _CONTINUE: continue
                    return r
            except _JSBreak as e:
                _lbl = node.get('__label__')
                if e.label is None or (_lbl and e.label == _lbl): break
                raise
            except _JSContinue as e:
                _lbl = node.get('__label__')
                if e.label is None or (_lbl and e.label == _lbl): continue
                raise
        self._exec_steps = _exec_steps
        return None

    def _exec_do_while_statement(self, node, env):
        _exec_steps = self._exec_steps
        _MAX = self.MAX_EXEC_STEPS
        _test = node["test"]
        def _test_falsy():
            _tv = self._eval(_test, env)
            _tt = _tv.type
            if _tt == 'boolean': return not _tv.value
            if _tt == 'number': return _tv.value == 0 or _tv.value != _tv.value
            if _tt == 'string': return not _tv.value
            if _tt == 'undefined' or _tt == 'null': return True
            return False
        while True:
            _exec_steps += 1
            if _exec_steps > _MAX:
                self._exec_steps = _exec_steps
                raise _JSError(self._make_js_error('RangeError', 'Execution step limit exceeded (possible infinite loop)'))
            try:
                r = self._exec(node["body"], env)
                if r is not None:
                    if r == _BREAK: break
                    if r == _CONTINUE:
                        if _test_falsy(): break
                        continue
                    return r
            except _JSBreak as e:
                _lbl = node.get('__label__')
                if e.label is None or (_lbl and e.label == _lbl): break
                raise
            except _JSContinue as e:
                _lbl = node.get('__label__')
                if e.label is None or (_lbl and e.label == _lbl): pass
                else: raise
            if _test_falsy(): break
        self._exec_steps = _exec_steps
        return None

    def _exec_for_statement(self, node, env):
        loop_env = Environment(env)
        init = node.get("init")
        # Cache loop metadata on AST node
        try:
            uses_lex, lex_vars, has_test, has_update, _update_ident = node['__for_cache__']
        except KeyError:
            uses_lex = (init is not None and
                        isinstance(init, dict) and
                        init.get("type") == "VariableDeclaration" and
                        init.get("kind") in ("let","const"))
            lex_vars = []
            if uses_lex and init:
                for decl in init.get("declarations", []):
                    id_node = decl.get("id")
                    if id_node and id_node.get("type") == "Identifier":
                        lex_vars.append(id_node["name"])
            has_test = "test" in node and node["test"] is not None
            has_update = "update" in node and node["update"] is not None
            # Detect simple i++ or ++i update pattern
            _update_ident = None
            if has_update:
                _upd = node["update"]
                if (_upd.get("type") == "UpdateExpression" and
                        _upd.get("operator") == "++" and
                        _upd.get("argument", {}).get("type") == "Identifier"):
                    _update_ident = _upd["argument"]["name"]
            node['__for_cache__'] = (uses_lex, lex_vars, has_test, has_update, _update_ident)
        if init:
            self._exec(init, loop_env)
        test_node = node["test"] if has_test else None
        update_node = node["update"] if has_update else None
        body_node = node["body"]
        _label = node.get('__label__')
        _exec_steps = self._exec_steps
        _MAX_EXEC_STEPS = self.MAX_EXEC_STEPS
        # Detect and cache inline test pattern: identifier < literal (number)
        _inline_test = None
        if has_test and test_node is not None:
            _tt = test_node.get("type")
            if _tt == "BinaryExpression":
                _tl = test_node.get("left")
                _tr = test_node.get("right")
                _top = test_node.get("operator")
                if (_tl and _tr and _top in ('<', '<=', '>', '>=', '!=', '!==') and
                        _tl.get("type") == "Identifier" and
                        _tr.get("type") == "Literal" and
                        isinstance(_tr.get("value"), (int, float))):
                    _inline_test = (_tl["name"], _top, _tr["value"])
        # Inline i++ update: directly increment binding, skip _eval dispatch
        _loop_bindings = loop_env.bindings
        if _update_ident is not None:
            _cached_binding = [None]  # mutable cell for caching
            def _do_update():
                _cb = _cached_binding[0]
                if _cb is not None:
                    _old = _cb[1]
                    nv = _old.value + 1 if _old.type == 'number' else self._to_num(_old) + 1
                    _cb[1] = _JS_SMALL_INTS[nv] if (nv.__class__ is int and -1 <= nv <= 255) else JsValue("number", nv)
                    return
                # First call: find binding in scope chain and cache it
                _uid = _update_ident
                _e = loop_env
                while _e is not None:
                    _eb = _e.bindings
                    if _uid in _eb:
                        _b = _eb[_uid]
                        _old = _b[1]
                        nv = _old.value + 1 if _old.type == 'number' else self._to_num(_old) + 1
                        _b[1] = _JS_SMALL_INTS[nv] if (nv.__class__ is int and -1 <= nv <= 255) else JsValue("number", nv)
                        _cached_binding[0] = _b
                        return
                    _e = _e.parent
        elif has_update:
            def _do_update():
                self._eval(update_node, loop_env)
        # Inline test condition for simple `ident OP literal(number)` patterns
        if _inline_test is not None:
            _test_var, _test_op, _test_limit = _inline_test
            _test_cached = [None]  # mutable cell for cached binding
            def _do_test():
                _cb = _test_cached[0]
                if _cb is not None:
                    _val = _cb[1].value
                else:
                    _e = loop_env
                    while _e is not None:
                        _eb = _e.bindings
                        if _test_var in _eb:
                            _b = _eb[_test_var]
                            _test_cached[0] = _b
                            _val = _b[1].value
                            break
                        _e = _e.parent
                    else:
                        return False
                if _test_op == '<': return _val < _test_limit
                if _test_op == '<=': return _val <= _test_limit
                if _test_op == '>': return _val > _test_limit
                if _test_op == '>=': return _val >= _test_limit
                if _test_op == '!=': return _val != _test_limit
                return _val != _test_limit  # !==
        while True:
            if has_test:
                if _inline_test is not None:
                    if not _do_test(): break
                else:
                    _tv = self._eval(test_node, loop_env)
                    _tt = _tv.type
                    if _tt == 'boolean':
                        if not _tv.value: break
                    elif _tt == 'number':
                        if _tv.value == 0 or _tv.value != _tv.value: break
                    elif _tt == 'string':
                        if not _tv.value: break
                    elif _tt == 'undefined' or _tt == 'null':
                        break
            _exec_steps += 1
            if _exec_steps > _MAX_EXEC_STEPS:
                self._exec_steps = _exec_steps
                raise _JSError(self._make_js_error('RangeError', 'Execution step limit exceeded (possible infinite loop)'))
            if uses_lex:
                iter_env = Environment(loop_env)
                _iter_bindings = iter_env.bindings
                _loop_bindings = loop_env.bindings
                for v in lex_vars:
                    if v in _loop_bindings:
                        _iter_bindings[v] = ['let', _loop_bindings[v][1]]
            else:
                iter_env = loop_env
            try:
                r = self._exec(body_node, iter_env)
                if r is not None:
                    if r == _BREAK: break
                    if r == _CONTINUE:
                        if uses_lex:
                            _ib = iter_env.bindings
                            for v in lex_vars:
                                if v in _ib and v in _loop_bindings:
                                    _loop_bindings[v][1] = _ib[v][1]
                        if has_update: _do_update()
                        continue
                    return r
            except _JSBreak as e:
                if e.label is None or (_label and e.label == _label): break
                raise
            except _JSContinue as e:
                if e.label is None or (_label and e.label == _label):
                    if uses_lex:
                        _ib = iter_env.bindings
                        for v in lex_vars:
                            if v in _ib and v in _loop_bindings:
                                _loop_bindings[v][1] = _ib[v][1]
                    if has_update: _do_update()
                    continue
                raise
            if uses_lex:
                _ib = iter_env.bindings
                for v in lex_vars:
                    if v in _ib and v in _loop_bindings:
                        _loop_bindings[v][1] = _ib[v][1]
            if has_update: _do_update()
        self._exec_steps = _exec_steps
        return None

    def _exec_for_in_statement(self, node, env):
        right = self._eval(node["right"], env)
        keys = []
        seen = set()
        if right.type == "object":
            cur = right
            while cur.__class__ is JsValue and cur.type == 'object':
                # Collect own enumerable keys for this level (including accessor properties)
                level_int_keys = []
                level_str_keys = []
                for k in cur.value.keys():
                    if k.startswith('@@') and k.endswith('@@'):
                        continue
                    if k.startswith('__get__'):
                        bare = k[len('__get__'):]
                        if bare in seen:
                            continue
                        seen.add(bare)
                        if not self._is_enumerable(cur, bare):
                            continue
                        try:
                            n = int(bare)
                            if n >= 0 and str(n) == bare:
                                level_int_keys.append((n, bare)); continue
                        except (ValueError, TypeError):
                            pass
                        level_str_keys.append(bare)
                    elif k.startswith('__set__'):
                        bare = k[len('__set__'):]
                        if bare in seen or bare in cur.value:
                            continue
                        seen.add(bare)
                        if not self._is_enumerable(cur, bare):
                            continue
                        level_str_keys.append(bare)
                    elif k.startswith('__') and k.endswith('__'):
                        continue
                    else:
                        if k in seen:
                            continue
                        seen.add(k)
                        if not self._is_enumerable(cur, k):
                            continue
                        try:
                            n = int(k)
                            if n >= 0 and str(n) == k:
                                level_int_keys.append((n, k)); continue
                        except (ValueError, TypeError):
                            pass
                        level_str_keys.append(k)
                level_int_keys.sort()
                keys.extend(k for _, k in level_int_keys)
                keys.extend(level_str_keys)
                cur = self._get_proto(cur)
        elif right.type == "array": keys = [str(i) for i in range(len(right.value))]
        elif right.type == "string": keys = [str(i) for i in range(len(right.value))]
        for key in keys:
            self._check_step_limit()
            loop_env = Environment(env)
            loop_value = JsValue("string", str(key))
            if node["left"]["type"] == "VariableDeclaration":
                self._bind_pattern(node["left"]["declarations"][0]["id"], loop_value, loop_env, node["left"]["kind"], True)
            else:
                self._bind_pattern(node["left"], loop_value, loop_env, 'let', False)
            try:
                r = self._exec(node["body"], loop_env)
                if r is not None:
                    if r in (_BREAK,): break
                    if r in (_CONTINUE,): continue
                    return r
            except _JSBreak as e:
                _lbl = node.get('__label__')
                if e.label is None or (_lbl and e.label == _lbl): break
                raise
            except _JSContinue as e:
                _lbl = node.get('__label__')
                if e.label is None or (_lbl and e.label == _lbl): continue
                raise
        return None

    def _exec_for_of_statement(self, node, env):
        is_await = node.get('await_', False)
        right = self._eval(node["right"], env)
        if is_await:
            async_it = self._get_async_iterator(right)
            if async_it is not None:
                while True:
                    promise = async_it()
                    self._check_step_limit()
                    if isinstance(promise, JsValue) and promise.type == 'promise':
                        if not self._run_event_loop(promise):
                            raise _JSError(py_to_js('for await: iterator did not settle'))
                        if promise.value['state'] == 'rejected':
                            raise _JSError(promise.value['value'])
                        result = promise.value['value']
                    else:
                        result = promise
                    done = self._get_prop(result, 'done')
                    if self._truthy(done):
                        break
                    item = self._get_prop(result, 'value')
                    loop_env = Environment(env)
                    if node["left"]["type"] == "VariableDeclaration":
                        self._bind_pattern(node["left"]["declarations"][0]["id"], item, loop_env, node["left"]["kind"], True)
                    else:
                        self._bind_pattern(node["left"], item, loop_env, 'let', False)
                    try:
                        r = self._exec(node["body"], loop_env)
                        if r is not None:
                            return r
                    except _JSBreak as e:
                        _lbl = node.get('__label__')
                        if e.label is None or (_lbl and e.label == _lbl): break
                        raise
                    except _JSContinue as e:
                        _lbl = node.get('__label__')
                        if e.label is None or (_lbl and e.label == _lbl): continue
                        raise
            return None
        # Get the iterator object (for calling .return() on early exit)
        iterator_obj = None
        iterator = self._get_js_iterator_with_obj(right)
        if iterator is not None:
            next_fn, iterator_obj = iterator
            def _call_iterator_return():
                """Call iterator.return() if it has one (IteratorClose spec operation)."""
                if iterator_obj is not None:
                    ret_fn = self._get_prop(iterator_obj, 'return') if isinstance(iterator_obj, JsValue) else None
                    if ret_fn and self._is_callable(ret_fn):
                        try:
                            self._call_js(ret_fn, [UNDEFINED], iterator_obj)
                        except Exception:
                            pass
            while True:
                self._check_step_limit()
                result = next_fn()
                done = self._get_prop(result, 'done')
                if self._truthy(done):
                    break
                item = self._get_prop(result, 'value')
                loop_env = Environment(env)
                if node["left"]["type"] == "VariableDeclaration":
                    self._bind_pattern(node["left"]["declarations"][0]["id"], item, loop_env, node["left"]["kind"], True)
                else:
                    self._bind_pattern(node["left"], item, loop_env, 'let', False)
                try:
                    r = self._exec(node["body"], loop_env)
                    if r is not None:
                        _call_iterator_return()
                        return r
                except _JSBreak as e:
                    _lbl = node.get('__label__')
                    if e.label is None or (_lbl and e.label == _lbl):
                        _call_iterator_return()
                        break
                    _call_iterator_return()
                    raise
                except _JSContinue as e:
                    _lbl = node.get('__label__')
                    if e.label is None or (_lbl and e.label == _lbl): continue
                    _call_iterator_return()
                    raise
                except _JSError:
                    _call_iterator_return()
                    raise
        else:
            items = self._array_like_items(right)
            for item in items:
                loop_env = Environment(env)
                if node["left"]["type"] == "VariableDeclaration":
                    self._bind_pattern(node["left"]["declarations"][0]["id"], item, loop_env, node["left"]["kind"], True)
                else:
                    self._bind_pattern(node["left"], item, loop_env, 'let', False)
                try:
                    r = self._exec(node["body"], loop_env)
                    if r is not None:
                        return r
                except _JSBreak as e:
                    _lbl = node.get('__label__')
                    if e.label is None or (_lbl and e.label == _lbl): break
                    raise
                except _JSContinue as e:
                    _lbl = node.get('__label__')
                    if e.label is None or (_lbl and e.label == _lbl): continue
                    raise
        return None

    def _exec_switch_statement(self, node, env):
        disc = self._eval(node["discriminant"], env)
        cases = node["cases"]
        matched = False
        default_idx = -1
        # Find matching case index
        match_idx = -1
        for i, case in enumerate(cases):
            if case.get("default"):
                default_idx = i
            elif not matched:
                test_val = self._eval(case["test"], env)
                if self._strict_eq(disc, test_val):
                    match_idx = i
                    matched = True
        start_idx = match_idx if matched else default_idx
        if start_idx < 0:
            return None
        try:
            for case in cases[start_idx:]:
                for s in case["consequent"]:
                    r = self._exec(s, env)
                    if r is not None: return r
        except _JSBreak as e:
            if e.label is None: return None
            raise
        return None

    def _exec_try_statement(self, node, env):
        _pending_exc = None
        try:
            self._exec(node["block"], env)
        except _JSError as e:
            handler = node.get("handler")
            if handler:
                if _TRACE_ACTIVE[0]:
                    _log_error.debug("catch %s", self._to_str(e.value)[:80])
                catch_env = Environment(env)
                if _TRACE_ACTIVE[0]:
                    _log_scope.log(TRACE, "scope create (catch)")
                param = handler.get("param")
                if param:
                    if isinstance(param, dict) and param.get("type") in ("ObjectPattern", "ArrayPattern"):
                        self._bind_pattern(param, e.value, catch_env, 'let', True)
                    else:
                        catch_env.declare(param, e.value, 'let')
                self._exec(handler["body"], catch_env)
            else:
                _pending_exc = e
        except (_JSReturn, _JSBreak, _JSContinue) as e:
            _pending_exc = e
        except Exception as e:
            handler = node.get("handler")
            if handler:
                if _TRACE_ACTIVE[0]:
                    _log_error.debug("catch (python) %s: %s", type(e).__name__, str(e)[:80])
                catch_env = Environment(env)
                param = handler.get("param")
                err_val = self._make_js_error('Error', str(e))
                if param:
                    if isinstance(param, dict) and param.get("type") in ("ObjectPattern", "ArrayPattern"):
                        self._bind_pattern(param, err_val, catch_env, 'let', True)
                    else:
                        catch_env.declare(param, err_val, 'let')
                self._exec(handler["body"], catch_env)
            else:
                _pending_exc = e
        finally:
            if node.get("finalizer"):
                self._exec(node["finalizer"], env)
        if _pending_exc is not None:
            raise _pending_exc
        return None

    def _exec_break_statement(self, node, env):
        raise _JSBreak(node.get('label'))

    def _exec_continue_statement(self, node, env):
        raise _JSContinue(node.get('label'))

    def _exec_labeled_statement(self, node, env):
        label = node["label"]
        body = node["body"]
        if body.get("type") in ("WhileStatement", "DoWhileStatement", "ForStatement", "ForInStatement", "ForOfStatement"):
            body['__label__'] = label
        try:
            return self._exec(body, env)
        except _JSBreak as e:
            if e.label is None or e.label == label:
                return None
            raise
        except _JSContinue as e:
            if e.label == label:
                return None
            raise
        finally:
            body.pop('__label__', None)

    def _exec_return_statement(self, node, env):
        arg = node["argument"]
        if arg is not None:
            raise _JSReturn(self._eval(arg, env))
        raise _RETURN_UNDEFINED

    def _exec_throw_statement(self, node, env):
        val = self._eval(node["argument"], env)
        if _TRACE_ACTIVE[0]:
            _log_error.debug("throw %s", self._to_str(val)[:80])
        raise _JSError(val)

    def _exec_empty_statement(self, node, env):
        return None

    def _exec_import_declaration(self, node, env):
        if getattr(self, '_module_loader', None) is not None:
            source_spec = node["source"]
            _specifiers = ", ".join(s.get("local", "?") for s in node.get("specifiers", []))
            if _TRACE_ACTIVE[0]:
                _log_module.info("import {%s} from %s", _specifiers, source_spec)
            resolved = self._module_loader.resolve(source_spec, getattr(self, '_module_file', None))
            exports = self._module_loader.load(resolved)
            if _TRACE_ACTIVE[0]:
                _log_module.info("module loaded: %s", resolved)
            for spec in node["specifiers"]:
                stype = spec["type"]
                if stype == "ImportDefaultSpecifier":
                    val = exports.get("default", UNDEFINED)
                    env.declare(spec["local"], val, 'let')
                elif stype == "ImportNamespaceSpecifier":
                    ns = JsValue('object', dict(exports))
                    env.declare(spec["local"], ns, 'let')
                elif stype == "ImportSpecifier":
                    val = exports.get(spec["imported"], UNDEFINED)
                    env.declare(spec["local"], val, 'let')
        return None

    def _exec_export_named_declaration(self, node, env):
        if not hasattr(self, '_module_exports'):
            self._module_exports = {}
        decl = node.get("declaration")
        specifiers = node.get("specifiers", [])
        source = node.get("source")
        if decl:
            self._exec(decl, env)
            if decl["type"] == "VariableDeclaration":
                for d in decl["declarations"]:
                    id_node = d["id"]
                    if isinstance(id_node, dict) and id_node.get("type") == "Identifier":
                        name = id_node["name"]
                        _log_module.debug("export %s", name)
                        try:
                            self._module_exports[name] = env.get(name)
                        except ReferenceError:
                            pass
            elif decl["type"] in ("FunctionDeclaration", "ClassDeclaration"):
                name = decl.get("id")
                if name:
                    _log_module.debug("export %s", name)
                    try:
                        self._module_exports[name] = env.get(name)
                    except ReferenceError:
                        pass
        elif source:
            if getattr(self, '_module_loader', None) is not None:
                resolved = self._module_loader.resolve(source, getattr(self, '_module_file', None))
                src_exports = self._module_loader.load(resolved)
                if not specifiers:
                    self._module_exports.update(src_exports)
                else:
                    for spec in specifiers:
                        val = src_exports.get(spec["local"], UNDEFINED)
                        self._module_exports[spec["exported"]] = val
        else:
            for spec in specifiers:
                try:
                    val = env.get(spec["local"])
                except ReferenceError:
                    val = UNDEFINED
                self._module_exports[spec["exported"]] = val
        return None

    def _exec_export_default_declaration(self, node, env):
        if not hasattr(self, '_module_exports'):
            self._module_exports = {}
        decl = node["declaration"]
        if decl.get("type") in ("FunctionDeclaration",):
            val = self._make_fn(decl, env)
            if decl.get("id"):
                try:
                    env.declare(decl["id"], val, 'var')
                except (JSTypeError, ReferenceError):
                    pass
        elif decl.get("type") == "FunctionExpression":
            val = self._make_fn(decl, env)
        elif decl.get("type") == "ClassDeclaration":
            self._exec(decl, env)
            val = env.get(decl["id"]) if decl.get("id") else UNDEFINED
        else:
            val = self._eval(decl, env)
        self._module_exports["default"] = val
        _log_module.debug("export default")
        return None

    _EXEC_DISPATCH = None  # initialized in _init_dispatch_tables

    def _init_exec_dispatch(self):
        self._EXEC_DISPATCH = {
            'Program': self._exec_program,
            'VariableDeclaration': self._exec_variable_declaration,
            'UsingDeclaration': self._exec_using_declaration,
            'FunctionDeclaration': self._exec_function_declaration,
            'ClassDeclaration': self._exec_class_declaration,
            'BlockStatement': self._exec_block_statement,
            'ExpressionStatement': self._exec_expression_statement,
            'IfStatement': self._exec_if_statement,
            'WhileStatement': self._exec_while_statement,
            'DoWhileStatement': self._exec_do_while_statement,
            'ForStatement': self._exec_for_statement,
            'ForInStatement': self._exec_for_in_statement,
            'ForOfStatement': self._exec_for_of_statement,
            'SwitchStatement': self._exec_switch_statement,
            'TryStatement': self._exec_try_statement,
            'BreakStatement': self._exec_break_statement,
            'ContinueStatement': self._exec_continue_statement,
            'LabeledStatement': self._exec_labeled_statement,
            'ReturnStatement': self._exec_return_statement,
            'ThrowStatement': self._exec_throw_statement,
            'EmptyStatement': self._exec_empty_statement,
            'ImportDeclaration': self._exec_import_declaration,
            'ExportNamedDeclaration': self._exec_export_named_declaration,
            'ExportDefaultDeclaration': self._exec_export_default_declaration,
        }

    def _exec(self, node, env=None):
        if env is None: env = self.env
        if _TRACE_ACTIVE[0]:
            _log_exec.debug("exec %s", node["type"])
        # Fast path: use cached handler if available
        try:
            return node['__sh__'](node, env)
        except KeyError:
            ntype = node["type"]
            try:
                handler = self._EXEC_DISPATCH[ntype]
            except KeyError:
                return None
            node['__sh__'] = handler
            return handler(node, env)

    def _eval_arguments(self, arg_nodes, env):
        if not arg_nodes:
            return []
        _eval = self._eval
        # Fast path: single arg, no spread (overwhelmingly common)
        if len(arg_nodes) == 1:
            a0 = arg_nodes[0]
            if a0["type"] != "SpreadElement":
                return [_eval(a0, env)]
        args = []
        _append = args.append
        for arg in arg_nodes:
            if arg["type"] == "SpreadElement":
                value = _eval(arg, env)
                it = self._get_js_iterator(value)
                if it is not None:
                    while True:
                        r = it()
                        done = self._get_prop(r, 'done')
                        if self._truthy(done): break
                        _append(self._get_prop(r, 'value'))
                elif value.type == "array":
                    args.extend(value.value)
                else:
                    _append(value)
            else:
                _append(_eval(arg, env))
        return args

    # --------------------------------------------------------- evaluation
    # ---- _eval dispatch methods ----

    def _eval_literal(self, node, env):
        # Fast path: return cached JsValue if already computed
        try:
            return node['__jv__']
        except KeyError:
            pass
        kind = node.get("raw", "undefined")
        val = node["value"]
        if val is None:
            result = JS_NULL if kind == "null" else UNDEFINED
        elif kind == "bigint":
            result = JsValue('bigint', val)
        elif kind == "number":
            if isinstance(val, float):
                if val != val:
                    result = _JS_NAN
                elif val == int(val) and -1 <= int(val) <= 255:
                    result = _JS_SMALL_INTS[int(val)]
                else:
                    result = JsValue('number', val)
            else:
                result = JsValue('number', val)
        elif kind == "boolean":
            result = JS_TRUE if val else JS_FALSE
        else:
            result = JsValue(kind, val)
        node['__jv__'] = result
        return result

    def _eval_regex_literal(self, node, env):
        return self._make_regexp_val(node["source"], node.get("flags", ""))

    def _eval_identifier(self, node, env):
        name = node["name"]
        if name == "arguments":
            e = env
            while e is not None:
                if not e._is_arrow and e._fn_args:
                    args_obj = py_to_js(e._fn_args)
                    if e._fn_val is not None:
                        if args_obj.extras is None:
                            args_obj.extras = {}
                        args_obj.extras['callee'] = e._fn_val
                    return args_obj
                e = e.parent
            return py_to_js([])
        # Inlined env.get for speed (avoids method call overhead)
        _bindings = env.bindings
        if name in _bindings:
            b = _bindings[name]
            if b[1] is _TDZ_SENTINEL:
                raise _JSError(self._make_js_error('ReferenceError', f"Cannot access '{name}' before initialization"))
            return b[1]
        e = env.parent
        while e is not None:
            _b = e.bindings
            if name in _b:
                b = _b[name]
                if b[1] is _TDZ_SENTINEL:
                    raise _JSError(self._make_js_error('ReferenceError', f"Cannot access '{name}' before initialization"))
                return b[1]
            e = e.parent
        raise _JSError(self._make_js_error('ReferenceError', f"{name} is not defined"))

    def _eval_this_expression(self, node, env):
        e = env
        while e is not None:
            if e._is_fn_env and not e._is_arrow:
                return e._this
            e = e.parent
        return UNDEFINED

    def _eval_array_expression(self, node, env):
        elems = []
        for e in node["elements"]:
            if e is None:
                elems.append(UNDEFINED)
            elif e.get("type") == "SpreadElement":
                v = self._eval(e["argument"], env)
                it = self._get_js_iterator(v)
                if it is not None:
                    while True:
                        r = it()
                        done = self._get_prop(r, 'done')
                        if self._truthy(done): break
                        elems.append(self._get_prop(r, 'value'))
                elif v.type == "array":
                    elems.extend(v.value)
                else:
                    elems.append(v)
            else:
                elems.append(self._eval(e, env))
        return JsValue("array", elems)

    def _eval_object_expression(self, node, env):
        obj = {}
        for p in node["properties"]:
            if p.get("type") == "SpreadElement":
                v = self._eval(p["argument"], env)
                if v.type == "object": obj.update(v.value)
                elif v.type == "array":
                    for i,x in enumerate(v.value): obj[str(i)] = x
                elif v.type == "string":
                    for i, ch in enumerate(v.value): obj[str(i)] = JsValue("string", ch)
                continue
            if p.get("shorthand"):
                k = p["key"]
                obj[k] = env.get(k) if isinstance(k, str) else self._eval(k, env)
            else:
                k = p["key"] if isinstance(p["key"], str) else self._eval(p["key"], env)
                k = self._to_key(k)
                kind = p.get("kind", "init")
                if kind == "get":
                    obj[f"__get__{k}"] = self._eval(p["value"], env)
                elif kind == "set":
                    obj[f"__set__{k}"] = self._eval(p["value"], env)
                else:
                    obj[k] = self._eval(p["value"], env)
        result = JsValue("object", obj)
        # Set super_proto on method functions so `super` works in object literals
        proto = obj.get('__proto__')
        if proto is None:
            # Default: Object.prototype (represented as no-proto sentinel)
            proto = UNDEFINED
        for v in obj.values():
            if isinstance(v, JsValue) and v.type == 'function':
                v.value['super_proto'] = proto
        return result

    def _eval_function_expression(self, node, env):
        return self._make_fn(node, env)

    def _eval_class_expression(self, node, env):
        """Evaluate a class in expression position (anonymous or named class expression)."""
        child_env = Environment(env)
        self._exec_class_declaration(node, child_env)
        cname = node.get("id") or '<anonymous>'
        try:
            return child_env.get(cname)
        except Exception:
            return UNDEFINED

    def _eval_unary_expression(self, node, env):
        op = node["operator"]
        if op == "typeof":
            # typeof on undeclared identifier must not throw (ES5.1 §11.4.3)
            if node["argument"].get("type") == "Identifier":
                if not env.has(node["argument"]["name"]):
                    return JsValue("string", "undefined")
            try:
                arg = self._eval(node["argument"], env)
            except _JSError:
                return JsValue("string", "undefined")
            return JsValue("string", self._typeof(arg))
        arg = self._eval(node["argument"], env)
        if op == "!":  return JS_FALSE if self._truthy(arg) else JS_TRUE
        if op == "-":  return JsValue("number", -self._to_num(arg))
        if op == "+":
            prim = self._to_primitive(arg, 'number')
            return JsValue("number", self._to_num(prim))
        if op == "~":  return JsValue("number", ~int(self._to_num(arg)))
        if op == "void": return UNDEFINED
        if op == "delete":
            na = node["argument"]
            if na["type"] == "MemberExpression":
                obj = self._eval(na["object"], env)
                prop = self._eval(na["property"], env) if na["computed"] else na["property"]["name"]
                key = self._to_key(prop)
                desc = self._get_desc(obj, key)
                if desc is not None and not desc.get('configurable', True):
                    if env._strict:
                        raise _JSError(self._make_js_error('TypeError',
                            f"Cannot delete property '{key}'"))
                    return JS_FALSE
                self._del_prop(obj, prop)
                return JS_TRUE
            return JS_TRUE
        return arg

    def _eval_binary_expression(self, node, env):
        op = node["operator"]
        # Private field brand check: #name in obj  (ES2022)
        if op == 'private_in':
            priv_name = node["left"]["name"]  # PrivateIdentifier node; name includes '#'
            target = self._eval(node["right"], env)
            if target.type not in ('object', 'array', 'function', 'intrinsic'):
                raise _JSError(self._make_js_error('TypeError', f'Cannot use "in" operator to search for "{priv_name}" in {self._to_str(target)}'))
            # Private fields are stored as "{#name}" directly in target.value
            if isinstance(target.value, dict) and priv_name in target.value:
                return JS_TRUE
            return JS_FALSE
        l = self._eval(node["left"], env)
        if op == "||":
            # Inline _truthy
            _lt = l.type
            if _lt == 'boolean':
                if l.value: return l
            elif _lt == 'number':
                _lv = l.value
                if _lv and _lv == _lv: return l
            elif _lt == 'string':
                if l.value: return l
            elif _lt in ('object', 'array', 'function', 'intrinsic', 'symbol', 'regexp', 'bigint'):
                return l
            # falsy — evaluate right
            return self._eval(node["right"], env)
        if op == "&&":
            # Inline _truthy (inverted)
            _lt = l.type
            if _lt == 'boolean':
                if not l.value: return l
            elif _lt == 'number':
                _lv = l.value
                if not _lv or _lv != _lv: return l
            elif _lt == 'string':
                if not l.value: return l
            elif _lt in ('undefined', 'null'):
                return l
            # truthy — evaluate right
            return self._eval(node["right"], env)
        if op == "??":
            if l.type != 'undefined' and l.type != 'null':
                return l
            return self._eval(node["right"], env)
        r = self._eval(node["right"], env)
        # Fast path: number op number (the overwhelmingly common case)
        if l.type == 'number' and r.type == 'number':
            lv, rv = l.value, r.value
            if op == '+':
                result = lv + rv
                ival = int(result)
                if result == ival and -1 <= ival <= 255: return _JS_SMALL_INTS[ival]
                return JsValue("number", result)
            if op == '-':
                result = lv - rv
                ival = int(result)
                if result == ival and -1 <= ival <= 255: return _JS_SMALL_INTS[ival]
                return JsValue("number", result)
            if op == '*':
                result = lv * rv
                ival = int(result)
                if result == ival and -1 <= ival <= 255: return _JS_SMALL_INTS[ival]
                return JsValue("number", result)
            if op == '/':
                if rv == 0:
                    if lv == 0 or lv != lv:
                        return _JS_NAN
                    return _JS_POS_INF if lv > 0 else _JS_NEG_INF
                result = lv / rv
                ival = int(result)
                if result == ival and -1 <= ival <= 255: return _JS_SMALL_INTS[ival]
                return JsValue("number", result)
            if op == '%':
                if rv == 0 or lv != lv:
                    return _JS_NAN
                return JsValue("number", math.fmod(lv, rv))
            if op == '**':
                result = lv ** rv
                ival = int(result)
                if result == ival and -1 <= ival <= 255: return _JS_SMALL_INTS[ival]
                return JsValue("number", result)
            if op == '<':  return JS_TRUE if lv < rv else JS_FALSE
            if op == '>':  return JS_TRUE if lv > rv else JS_FALSE
            if op == '<=': return JS_TRUE if lv <= rv else JS_FALSE
            if op == '>=': return JS_TRUE if lv >= rv else JS_FALSE
            if op == '===': return JS_TRUE if lv == rv else JS_FALSE
            if op == '!==': return JS_FALSE if lv == rv else JS_TRUE
            if op == '==':  return JS_TRUE if lv == rv else JS_FALSE
            if op == '!=':  return JS_FALSE if lv == rv else JS_TRUE
        # Fast path: string op string (common for concatenation and comparison)
        if l.type == 'string' and r.type == 'string':
            lv, rv = l.value, r.value
            if op == '+': return JsValue("string", lv + rv)
            if op == '===': return JS_TRUE if lv == rv else JS_FALSE
            if op == '!==': return JS_FALSE if lv == rv else JS_TRUE
            if op == '==':  return JS_TRUE if lv == rv else JS_FALSE
            if op == '!=':  return JS_FALSE if lv == rv else JS_TRUE
            if op == '<':  return JS_TRUE if lv < rv else JS_FALSE
            if op == '>':  return JS_TRUE if lv > rv else JS_FALSE
            if op == '<=': return JS_TRUE if lv <= rv else JS_FALSE
            if op == '>=': return JS_TRUE if lv >= rv else JS_FALSE
        if op == "+":
            if l.type == "bigint" and r.type == "bigint":
                return JsValue("bigint", l.value + r.value)
            lp = self._to_primitive(l)
            rp = self._to_primitive(r)
            if lp.type == "string" or rp.type == "string":
                return JsValue("string", self._to_str(lp) + self._to_str(rp))
            return JsValue("number", self._to_num(lp) + self._to_num(rp))
        if op == "-":
            if l.type == "bigint" and r.type == "bigint":
                return JsValue("bigint", l.value - r.value)
            return JsValue("number", self._to_num(l) - self._to_num(r))
        if op == "*":
            if l.type == "bigint" and r.type == "bigint":
                return JsValue("bigint", l.value * r.value)
            return JsValue("number", self._to_num(l) * self._to_num(r))
        if op == "/":
            if l.type == "bigint" and r.type == "bigint":
                return JsValue("bigint", l.value // r.value) if r.value != 0 else JsValue("bigint", 0)
            lv, rv = self._to_num(l), self._to_num(r)
            if rv == 0:
                if lv == 0 or (isinstance(lv, float) and math.isnan(lv)):
                    return _JS_NAN
                return JsValue("number", math.copysign(float('inf'), lv * rv if rv != 0 else lv))
            return JsValue("number", lv / rv)
        if op == "%":
            if l.type == "bigint" and r.type == "bigint":
                return JsValue("bigint", l.value % r.value)
            lv, rv = self._to_num(l), self._to_num(r)
            if rv == 0 or (isinstance(lv, float) and math.isnan(lv)):
                return _JS_NAN
            return JsValue("number", math.fmod(lv, rv))
        if op == "**":
            if l.type == "bigint" and r.type == "bigint":
                return JsValue("bigint", l.value ** r.value)
            return JsValue("number", self._to_num(l) ** self._to_num(r))
        if op == "==":  return JS_TRUE if self._eq(l, r) else JS_FALSE
        if op == "!=":  return JS_FALSE if self._eq(l, r) else JS_TRUE
        if op == "===": return JS_TRUE if self._strict_eq(l, r) else JS_FALSE
        if op == "!==": return JS_FALSE if self._strict_eq(l, r) else JS_TRUE
        if op in ("<",">","<=",">="):
            return JS_TRUE if self._cmp(op, l, r) else JS_FALSE
        if op == "instanceof":
            # ES spec: check Symbol.hasInstance first
            if r.type in ("function", "intrinsic", "class") and r.value.__class__ is dict:
                _hi_key = SK_HAS_INSTANCE
                _hi_fn = r.value.get(_hi_key)
                if _hi_fn.__class__ is JsValue and _hi_fn.type in ('function', 'intrinsic'):
                    result = self._call_js(_hi_fn, [l], r)
                    return JS_TRUE if self._truthy(result) else JS_FALSE
            _error_ctor_names = {'Error','TypeError','RangeError','SyntaxError','ReferenceError','URIError','EvalError','AggregateError'}
            # Check built-in globals stored as objects (Array, Object, Function, Promise, Map, Set, etc.)
            if r.type == 'object' and r.value.__class__ is dict:
                _g = self.genv
                _kind_ctors = {'Map', 'Set', 'WeakMap', 'WeakSet'}
                for _gname in ('Array','Object','Function','Promise','RegExp','Map','Set','WeakMap','WeakSet'):
                    if _g.has(_gname) and _g.get(_gname) is r:
                        if _gname == 'Array': return JS_TRUE if l.type == 'array' else JS_FALSE
                        if _gname == 'Object': return JS_TRUE if l.type in ('object','array','function','intrinsic','class','promise','regexp') else JS_FALSE
                        if _gname == 'Function': return JS_TRUE if l.type in ('function','intrinsic','class') else JS_FALSE
                        if _gname == 'Promise': return JS_TRUE if l.type == 'promise' else JS_FALSE
                        if _gname == 'RegExp': return JS_TRUE if l.type == 'regexp' else JS_FALSE
                        if _gname in _kind_ctors:
                            if l.type == 'object' and l.value.__class__ is dict:
                                kind_v = l.value.get('__kind__')
                                kind_s = kind_v.value if kind_v.__class__ is JsValue else kind_v
                                return JS_TRUE if kind_s == _gname else JS_FALSE
                            return JS_FALSE
            if r.type in ("function","intrinsic","class"):
                ctor_name = r.value.get("name") if r.value.__class__ is dict else None
                if ctor_name.__class__ is str and ctor_name in _error_ctor_names:
                    if l.type == 'object':
                        err_type = l.value.get('__error_type__')
                        if err_type.__class__ is JsValue and err_type.type == 'string':
                            if ctor_name == 'Error' or err_type.value == ctor_name:
                                return JS_TRUE
                    return JS_FALSE
                # Built-in function/intrinsic constructors
                _func_builtin_map = {
                    'Map': None, 'Set': None, 'WeakMap': None, 'WeakSet': None,
                    'RegExp': None, 'Promise': ('promise',),
                    'Function': ('function','intrinsic','class'),
                    'Array': ('array',),
                    'Object': ('object','array','function','intrinsic','class','promise','regexp'),
                }
                if ctor_name.__class__ is str and ctor_name in _func_builtin_map:
                    allowed = _func_builtin_map[ctor_name]
                    if allowed is None:
                        if l.type == 'object' and l.value.__class__ is dict:
                            kind_v = l.value.get('__kind__')
                            kind_s = kind_v.value if kind_v.__class__ is JsValue else kind_v
                            return JS_TRUE if kind_s == ctor_name else JS_FALSE
                        return JS_FALSE
                    return JS_TRUE if l.type in allowed else JS_FALSE
                proto = r.value.get("prototype")
                if proto and l.type in ("object", "array", "function", "intrinsic", "class"):
                    lp = self._get_proto(l)
                    while lp:
                        if lp is proto: return JS_TRUE
                        lp = self._get_proto(lp) if lp.__class__ is JsValue else None
            return JS_FALSE
        if op == "in":
            key = self._to_str(l)
            target = r
            if r.type == 'proxy':
                proxy = r.value
                trap = self._get_trap(proxy.handler, 'has')
                if trap:
                    return self._call_js(trap, [proxy.target, py_to_js(key)], UNDEFINED)
                target = proxy.target
            if target.type in ("object", "function", "intrinsic", "class"):
                cur = target
                while cur.__class__ is JsValue and cur.type in ('object', 'function', 'intrinsic', 'class'):
                    if key in cur.value or f"__get__{key}" in cur.value or f"__set__{key}" in cur.value:
                        return JS_TRUE
                    cur = self._get_proto(cur)
                return JS_FALSE
            if target.type == "array":
                if key == "length":
                    return JS_TRUE
                try:
                    idx = int(key)
                    if 0 <= idx < len(target.value):
                        return JS_TRUE
                except (ValueError, TypeError):
                    pass
                # Check extras (e.g. 'groups' on regex match) and array prototype chain
                if target.extras and key in target.extras:
                    return JS_TRUE
                # Built-in array methods are dispatch-based, not stored in proto
                if key in self.ARRAY_METHODS:
                    return JS_TRUE
                # Walk array prototype for inherited properties (like push, pop, etc.)
                cur = self._get_proto(target)
                while cur.__class__ is JsValue and cur.type in ('object', 'function', 'intrinsic', 'class'):
                    if key in cur.value or f"__get__{key}" in cur.value:
                        return JS_TRUE
                    cur = self._get_proto(cur)
                return JS_FALSE
            if target.type == "string":
                if key == "length":
                    return JS_TRUE
                try:
                    return JS_TRUE if 0 <= int(key) < len(target.value) else JS_FALSE
                except (ValueError, TypeError):
                    pass
                # Built-in string methods
                if key in self.STRING_METHODS:
                    return JS_TRUE
                return JS_FALSE
        # bitwise
        li, ri = int(self._to_num(l)), int(self._to_num(r))
        if op == "<<":  return JsValue("number", li << ri)
        if op == ">>":  return JsValue("number", li >> ri)
        if op == ">>>": return JsValue("number", (li & 0xFFFFFFFF) >> ri)
        if op == "&":   return JsValue("number", li & ri)
        if op == "^":   return JsValue("number", li ^ ri)
        if op == "|":   return JsValue("number", li | ri)
        return UNDEFINED

    def _eval_logical_expression(self, node, env):
        l = self._eval(node["left"], env)
        op = node["operator"]
        if op == "&&":
            # Inline _truthy (inverted)
            _lt = l.type
            if _lt == 'boolean':
                if not l.value: return l
            elif _lt == 'number':
                _lv = l.value
                if not _lv or _lv != _lv: return l
            elif _lt == 'string':
                if not l.value: return l
            elif _lt in ('undefined', 'null'):
                return l
            return self._eval(node["right"], env)
        if op == "||":
            # Inline _truthy
            _lt = l.type
            if _lt == 'boolean':
                if l.value: return l
            elif _lt == 'number':
                _lv = l.value
                if _lv and _lv == _lv: return l
            elif _lt == 'string':
                if l.value: return l
            elif _lt in ('object', 'array', 'function', 'intrinsic', 'symbol', 'regexp', 'bigint'):
                return l
            return self._eval(node["right"], env)
        if op == "??":
            if l.type != 'undefined' and l.type != 'null':
                return l
            return self._eval(node["right"], env)
        return l

    def _eval_update_expression(self, node, env):
        arg = node["argument"]
        prefix = node["prefix"]
        op = node["operator"]
        if arg["type"] == "Identifier":
            _name = arg["name"]
            _e = env
            while _e is not None:
                _eb = _e.bindings
                if _name in _eb:
                    _b = _eb[_name]
                    old = _b[1]
                    _delta = 1 if op == "++" else -1
                    # Inline _to_num for number type (most common in loops)
                    nv = (old.value + _delta) if old.type == 'number' else (self._to_num(old) + _delta)
                    new = _JS_SMALL_INTS[nv] if (nv.__class__ is int and -1 <= nv <= 255) else JsValue("number", nv)
                    _b[1] = new
                    return old if not prefix else new
                _e = _e.parent
            raise _JSError(self._make_js_error('ReferenceError', f"{_name} is not defined"))
        if arg["type"] == "MemberExpression":
            obj = self._eval(arg["object"], env)
            prop = self._eval(arg["property"], env) if arg["computed"] else arg["property"]["name"]
            old = self._get_prop(obj, prop)
            _delta = 1 if op == "++" else -1
            nv = (old.value + _delta) if old.type == 'number' else (self._to_num(old) + _delta)
            new = _JS_SMALL_INTS[nv] if (nv.__class__ is int and -1 <= nv <= 255) else JsValue("number", nv)
            self._set_prop(obj, prop, new)
            return old if not prefix else new
        return UNDEFINED

    def _eval_assignment_expression(self, node, env):
        left = node["left"]
        op = node["operator"]
        _ltype = left["type"]
        # Fast path: simple `identifier = expr` (the most common case)
        if op == "=" and _ltype == "Identifier":
            _name = left["name"]
            right = self._eval(node["right"], env)
            # Name inference for functions
            if right.type in ('function', 'intrinsic', 'class') and right.value.__class__ is dict and not right.value.get("name"):
                right.value["name"] = _name
            env.set(_name, right)
            if env is self.genv and self._global_object is not None and _name != 'globalThis':
                self._global_object.value[_name] = right
            return right
        if op == "=" and _ltype in ("ObjectPattern", "ArrayPattern"):
            right = self._eval(node["right"], env)
            self._bind_pattern(left, right, env, 'let', False)
            return right
        # Fast path: `obj.prop = expr` or `obj[key] = expr`
        if op == "=" and _ltype == "MemberExpression":
            if left.get('optional'):
                raise _JSError(py_to_js('Invalid assignment target'))
            right = self._eval(node["right"], env)
            obj = self._eval(left["object"], env)
            prop = self._eval(left["property"], env) if left["computed"] else left["property"]["name"]
            self._set_prop(obj, prop, right)
            return right
        # Fast path: compound assignment on Identifier (+=, -=, etc.)
        # Avoids _resolve_target closure creation + _find scope walk
        if _ltype == "Identifier" and op not in ("&&=", "||=", "??="):
            _name = left["name"]
            right = self._eval(node["right"], env)
            _e = env
            while _e is not None:
                _eb = _e.bindings
                if _name in _eb:
                    _b = _eb[_name]
                    if _b[0] == 'const':
                        raise JSTypeError(f"Assignment to constant variable '{_name}'")
                    old = _b[1]
                    # Inline += for number+number (overwhelmingly common in loops)
                    if op == "+=":
                        if old.type == 'number' and right.type == 'number':
                            result = old.value + right.value
                            ival = int(result)
                            new_value = _JS_SMALL_INTS[ival] if (result == ival and -1 <= ival <= 255) else JsValue("number", result)
                        elif old.type == 'string' or right.type == 'string':
                            new_value = JsValue("string", self._to_str(old) + self._to_str(right))
                        else:
                            new_value = self._do_assign_op(op, old, right)
                    elif op == "-=":
                        if old.type == 'number' and right.type == 'number':
                            result = old.value - right.value
                            ival = int(result)
                            new_value = _JS_SMALL_INTS[ival] if (result == ival and -1 <= ival <= 255) else JsValue("number", result)
                        else:
                            new_value = self._do_assign_op(op, old, right)
                    elif op == "*=":
                        if old.type == 'number' and right.type == 'number':
                            result = old.value * right.value
                            ival = int(result)
                            new_value = _JS_SMALL_INTS[ival] if (result == ival and -1 <= ival <= 255) else JsValue("number", result)
                        else:
                            new_value = self._do_assign_op(op, old, right)
                    else:
                        new_value = self._do_assign_op(op, old, right)
                    _b[1] = new_value
                    if _e is self.genv and self._global_object is not None and _name != 'globalThis':
                        self._global_object.value[_name] = new_value
                    return new_value
                _e = _e.parent
            raise _JSError(self._make_js_error('ReferenceError', f"{_name} is not defined"))
        getter, setter = self._resolve_target(left, env)
        old = getter()
        if op == "&&=":
            if not self._truthy(old):
                return old
            right = self._eval(node["right"], env)
            setter(right)
            return right
        if op == "||=":
            if self._truthy(old):
                return old
            right = self._eval(node["right"], env)
            setter(right)
            return right
        if op == "??=":
            if not self._is_nullish(old):
                return old
            right = self._eval(node["right"], env)
            setter(right)
            return right
        right = self._eval(node["right"], env)
        if op == "=":
            setter(right)
            return right
        new_value = self._do_assign_op(op, old, right)
        setter(new_value)
        return new_value

    def _eval_conditional_expression(self, node, env):
        test = self._eval(node["test"], env)
        # Inline _truthy for common types
        _tt = test.type
        if _tt == 'boolean':
            _truthy = test.value
        elif _tt == 'number':
            _v = test.value
            _truthy = _v != 0 and _v == _v
        elif _tt == 'string':
            _truthy = len(test.value) > 0
        elif _tt == 'undefined' or _tt == 'null':
            _truthy = False
        else:
            _truthy = True
        return self._eval(node["consequent"] if _truthy else node["alternate"], env)

    def _eval_member_expression(self, node, env):
        obj = self._eval(node["object"], env)
        try:
            _opt = node['__me_opt__']
        except KeyError:
            _opt = node.get("optional", False)
            node['__me_opt__'] = _opt
        if _opt and self._is_nullish(obj):
            return UNDEFINED
        _otype = obj.type
        if _otype == 'null' or _otype == 'undefined':
            prop_name = node["property"].get("name", "?") if not node["computed"] else "?"
            raise _JSError(self._make_js_error('TypeError',
                f"Cannot read properties of {_otype} (reading '{prop_name}')"))
        if node["computed"]:
            prop = self._eval(node["property"], env)
            # Fast path: array[number] — direct index, skip _get_prop/_to_key chain
            if _otype == 'array' and prop.type == 'number':
                _idx_f = prop.value
                _idx = int(_idx_f)
                if _idx == _idx_f and 0 <= _idx < len(obj.value):
                    return obj.value[_idx]
        else:
            prop = node["property"]["name"]
            # Fast path: arr.length — most accessed non-computed property on arrays
            if prop == 'length' and _otype == 'array':
                _len = len(obj.value)
                return _JS_SMALL_INTS[_len] if 0 <= _len <= 255 else JsValue("number", _len)
            # Fast path: str.length
            if prop == 'length' and _otype == 'string':
                _len = len(obj.value)
                return _JS_SMALL_INTS[_len] if 0 <= _len <= 255 else JsValue("number", _len)
        return self._get_prop(obj, prop)

    def _eval_call_expression(self, node, env):
        callee_node = node["callee"]
        callee_type = callee_node["type"]
        args = self._eval_arguments(node["arguments"], env)
        this_val = UNDEFINED
        if callee_type == "MemberExpression":
            obj = self._eval(callee_node["object"], env)
            if callee_node.get("optional") and self._is_nullish(obj):
                return UNDEFINED
            prop = self._eval(callee_node["property"], env) if callee_node["computed"] else callee_node["property"]["name"]
            callee = self._get_prop(obj, prop)
            if obj.type == 'object' and '__super_this__' in obj.value:
                this_val = obj.value['__super_this__']
            else:
                this_val = obj
        elif callee_type == "Identifier":
            _cname = callee_node["name"]
            try:
                callee = env.get(_cname)
            except ReferenceError as re:
                raise _JSError(self._make_js_error('ReferenceError', str(re)))
            if callee.type == 'object' and '__super_ctor__' in callee.value:
                super_ctor = callee.value['__super_ctor__']
                this_val = callee.value.get('__super_this__', UNDEFINED)
                self._call_js(super_ctor, args, this_val)
                return this_val
        else:
            callee = self._eval(callee_node, env)
            if callee.type == 'object' and '__super_ctor__' in callee.value:
                super_ctor = callee.value['__super_ctor__']
                this_val = callee.value.get('__super_this__', UNDEFINED)
                self._call_js(super_ctor, args, this_val)
                return this_val
        try:
            _opt = node['__optional__']
        except KeyError:
            _opt = node.get("optional", False)
            node['__optional__'] = _opt
        if _opt and self._is_nullish(callee):
            return UNDEFINED
        # Inline intrinsic call (avoids _call_js → _call_js_impl dispatch for ~50% of calls)
        if callee.type == 'intrinsic':
            return callee.value["fn"](this_val, args, self)
        # Inline _call_js depth tracking for function calls (avoids one method call layer)
        if not _TRACE_ACTIVE[0]:
            self._call_depth += 1
            if self._call_depth > self.MAX_CALL_DEPTH:
                self._call_depth -= 1
                raise _JSError(self._make_js_error('RangeError', 'Maximum call stack size exceeded'))
            try:
                return self._call_js_impl(callee, args, this_val)
            finally:
                self._call_depth -= 1
        return self._call_js(callee, args, this_val)

    def _eval_new_expression(self, node, env):
        callee = self._eval(node["callee"], env)
        args = self._eval_arguments(node["arguments"], env)
        if callee.type == 'proxy':
            proxy = callee.value
            trap = self._get_trap(proxy.handler, 'construct')
            if trap:
                return self._call_js(trap, [proxy.target, JsValue('array', args), callee], UNDEFINED)
            callee = proxy.target
        if callee.type in ("function","intrinsic","class"):
            # Check if this class (or any ancestor) extends the built-in Array.
            # Walk the superClass chain; if Array is found, create an array-typed object.
            _is_array_subclass = False
            _sc = callee.value.get("superClass") if isinstance(callee.value, dict) else None
            while isinstance(_sc, JsValue):
                _sc_name = _sc.value.get("name") if isinstance(_sc.value, dict) else None
                if _sc_name == "Array" and _sc.type in ("intrinsic", "function"):
                    _is_array_subclass = True
                    break
                _sc = _sc.value.get("superClass") if isinstance(_sc.value, dict) else None
            if _is_array_subclass:
                new_obj = JsValue("array", [])
                if new_obj.extras is None:
                    new_obj.extras = {}
            else:
                new_obj = JsValue("object", {})
            proto = callee.value.get("prototype")
            if proto and proto.type == "object":
                if _is_array_subclass:
                    if new_obj.extras is None:
                        new_obj.extras = {}
                    new_obj.extras["__proto__"] = proto
                else:
                    new_obj.value["__proto__"] = proto
            # Tag with constructor/class name for DevTools-style rendering
            ctor_name = callee.value.get("name", "") if isinstance(callee.value, dict) else ""
            if ctor_name and ctor_name not in ("Object", "Array", "Function"):
                if _is_array_subclass:
                    if new_obj.extras is None:
                        new_obj.extras = {}
                    new_obj.extras["__class_name__"] = JsValue("string", ctor_name)
                else:
                    new_obj.value["__class_name__"] = JsValue("string", ctor_name)
            instance_fields = callee.value.get("__instance_fields__", []) if isinstance(callee.value, dict) else []
            if instance_fields:
                field_env = Environment(env)
                field_env._is_fn_env = True
                field_env._is_arrow = False
                field_env._this = new_obj
                for field in instance_fields:
                    fval = self._eval(field["value"], field_env) if field.get("value") else UNDEFINED
                    fkey = field["key"]
                    if field.get("computed") and field.get("computed_key"):
                        computed_val = self._eval(field["computed_key"], field_env)
                        if computed_val.type == 'symbol':
                            fkey = f"@@{computed_val.value['id']}@@"
                        else:
                            fkey = self._to_str(computed_val)
                    new_obj.value[fkey] = fval
            result = self._call_js(callee, args, new_obj, is_new_call=True)
            if isinstance(result, JsValue) and result.type in ('object', 'array', 'function', 'intrinsic', 'class', 'promise', 'proxy'):
                return result
            return new_obj
        return py_to_js({})

    def _eval_sequence_expression(self, node, env):
        result = UNDEFINED
        for expr in node["expressions"]:
            result = self._eval(expr, env)
        return result

    def _eval_template_literal(self, node, env):
        parts = []
        for part in node["quasis"]:
            if isinstance(part, tuple) and part[0] == "expr":
                val = self._eval(Parser(Lexer(part[1]).tokenize()).parse()["body"][0]["expression"], env)
                parts.append(self._to_str(self._to_primitive(val, 'string')))
            elif isinstance(part, tuple) and part[0] == "text":
                parts.append(part[1])  # cooked value
            else:
                parts.append(str(part))
        return JsValue("string", "".join(parts))

    def _eval_tagged_template_expression(self, node, env):
        tag_fn = self._eval(node["tag"], env)
        quasis = node["quasi"]["quasis"]
        strs = []
        raw_strs = []
        vals = []
        current_cooked = []
        current_raw = []
        for part in quasis:
            if isinstance(part, tuple) and part[0] == "expr":
                strs.append(JsValue("string", "".join(current_cooked)))
                raw_strs.append(JsValue("string", "".join(current_raw)))
                current_cooked = []
                current_raw = []
                vals.append(self._eval(Parser(Lexer(part[1]).tokenize()).parse()["body"][0]["expression"], env))
            elif isinstance(part, tuple) and part[0] == "text":
                current_cooked.append(part[1])   # cooked
                current_raw.append(part[2])       # raw
            else:
                current_cooked.append(str(part))
                current_raw.append(str(part))
        strs.append(JsValue("string", "".join(current_cooked)))
        raw_strs.append(JsValue("string", "".join(current_raw)))
        strings_arr = JsValue("array", strs)
        strings_arr.extras = {"raw": JsValue("array", raw_strs)}
        return self._call_js(tag_fn, [strings_arr] + vals, UNDEFINED)

    def _eval_await_expression(self, node, env):
        awaited = self._eval(node["argument"], env)
        if awaited.type != 'promise':
            _log_async.debug("await (non-promise, immediate)")
            return awaited
        _log_async.debug("await (promise state=%s)", awaited.value.get('state', '?'))
        if not self._run_event_loop(awaited):
            raise _JSError(py_to_js('Awaited promise did not settle'))
        if awaited.value['state'] == 'rejected':
            raise _JSError(awaited.value['value'])
        _result = awaited.value['value']
        if _log_async.isEnabledFor(TRACE):
            _log_async.log(TRACE, "await resolved → %s", self._to_str(_result)[:60])
        return _result

    def _eval_spread_element(self, node, env):
        return self._eval(node["argument"], env)

    def _eval_meta_property(self, node, env):
        if node["meta"] == "new" and node["property"] == "target":
            e = env
            while e is not None:
                if '__new_target__' in e.bindings:
                    return e.bindings['__new_target__'][1]
                e = e.parent
            return UNDEFINED
        return UNDEFINED

    def _eval_import_meta(self, node, env):
        meta = JsValue('object', {})
        url = self._module_url or (
            f"file://{self._module_file}" if self._module_file else 'file:///unknown'
        )
        meta.value['url'] = py_to_js(url)
        return meta

    def _eval_yield_expression(self, node, env):
        arg = UNDEFINED
        if node.get('argument'):
            arg = self._eval(node['argument'], env)
        gen = self._find_generator(env)
        if gen is None:
            raise _JSError(py_to_js('yield used outside generator'))
        if node.get('delegate'):
            _log_async.debug("yield* (delegate)")
            it = self._get_js_iterator(arg)
            last = UNDEFINED
            if it is not None:
                while True:
                    r = it()
                    done = self._get_prop(r, 'done')
                    val = self._get_prop(r, 'value')
                    if self._truthy(done):
                        last = val
                        break
                    gen.yield_value(val)
            return last
        else:
            if _log_async.isEnabledFor(TRACE):
                _log_async.log(TRACE, "yield %s", self._to_str(arg)[:60])
            return gen.yield_value(arg)

    def _eval_dynamic_import(self, node, env):
        src = self._eval(node['source'], env) if node.get('source') else py_to_js('')
        if self._module_loader is not None:
            try:
                mod_path = self._to_str(src)
                ns = self._module_loader.load(mod_path, self._module_file)
                return self._resolved_promise(ns)
            except (_JSReturn, _JSBreak, _JSContinue):
                raise
            except _JSError as e:
                return self._rejected_promise(e.value)
            except Exception as e:
                return self._rejected_promise(self._make_js_error('Error', str(e)))
        return self._resolved_promise(JsValue('object', {}))

    _EVAL_DISPATCH = None  # initialized in _init_dispatch_tables

    def _init_eval_dispatch(self):
        self._EVAL_DISPATCH = {
            'Literal': self._eval_literal,
            'RegexLiteral': self._eval_regex_literal,
            'Identifier': self._eval_identifier,
            'ThisExpression': self._eval_this_expression,
            'ArrayExpression': self._eval_array_expression,
            'ObjectExpression': self._eval_object_expression,
            'FunctionExpression': self._eval_function_expression,
            'UnaryExpression': self._eval_unary_expression,
            'BinaryExpression': self._eval_binary_expression,
            'LogicalExpression': self._eval_logical_expression,
            'UpdateExpression': self._eval_update_expression,
            'AssignmentExpression': self._eval_assignment_expression,
            'ConditionalExpression': self._eval_conditional_expression,
            'MemberExpression': self._eval_member_expression,
            'CallExpression': self._eval_call_expression,
            'NewExpression': self._eval_new_expression,
            'SequenceExpression': self._eval_sequence_expression,
            'TemplateLiteral': self._eval_template_literal,
            'TaggedTemplateExpression': self._eval_tagged_template_expression,
            'AwaitExpression': self._eval_await_expression,
            'SpreadElement': self._eval_spread_element,
            'MetaProperty': self._eval_meta_property,
            'ImportMeta': self._eval_import_meta,
            'YieldExpression': self._eval_yield_expression,
            'DynamicImport': self._eval_dynamic_import,
            'ClassDeclaration': self._eval_class_expression,
        }

    def _eval(self, node, env=None):
        if env is None: env = self.env
        if _TRACE_ACTIVE[0]:
            _log_eval.debug("eval %s", node["type"])
        # Fast path: use cached handler if available
        try:
            return node['__eh__'](node, env)
        except KeyError:
            ntype = node["type"]
            try:
                handler = self._EVAL_DISPATCH[ntype]
            except KeyError:
                return UNDEFINED
            node['__eh__'] = handler
            return handler(node, env)

    def _do_assign_op(self, op, old, val):
        if op == "+=":
            if old.type == "string" or val.type == "string":
                return JsValue("string", self._to_str(old) + self._to_str(val))
            result = self._to_num(old) + self._to_num(val)
            if result.__class__ is int and -1 <= result <= 255:
                return _JS_SMALL_INTS[result]
            return JsValue("number", result)
        if op == "-=":
            result = self._to_num(old) - self._to_num(val)
            if result.__class__ is int and -1 <= result <= 255:
                return _JS_SMALL_INTS[result]
            return JsValue("number", result)
        if op == "*=":  return JsValue("number", self._to_num(old) * self._to_num(val))
        if op == "/=":  return JsValue("number", self._to_num(old) / self._to_num(val))
        if op == "%=":  return JsValue("number", self._to_num(old) % self._to_num(val))
        if op == "**=": return JsValue("number", self._to_num(old) ** self._to_num(val))
        if op == "<<=": return JsValue("number", int(self._to_num(old)) << int(self._to_num(val)))
        if op == ">>=": return JsValue("number", int(self._to_num(old)) >> int(self._to_num(val)))
        if op == ">>>=": return JsValue("number", (int(self._to_num(old)) & 0xFFFFFFFF) >> int(self._to_num(val)))
        if op == "&=":  return JsValue("number", int(self._to_num(old)) & int(self._to_num(val)))
        if op == "|=":  return JsValue("number", int(self._to_num(old)) | int(self._to_num(val)))
        if op == "^=":  return JsValue("number", int(self._to_num(old)) ^ int(self._to_num(val)))
        return val

    # --------------------------------------------------------- function creation / calling
    def _make_fn(self, node, closure_env):
        _is_arrow = bool(node.get("arrow"))
        _is_gen = bool(node.get("generator_"))
        _is_async = bool(node.get("async_"))
        _params = node.get("params", [])
        _body = node["body"]
        # Pre-extract body statements for hoisting/strict caching
        _body_stmts = _body.get("body", []) if isinstance(_body, dict) and _body.get("type") == "BlockStatement" else []
        # Detect single-return bodies: {return expr} — skip _exec + exception for these
        _fast_return = None
        if (len(_body_stmts) == 1
                and _body_stmts[0].__class__ is dict
                and _body_stmts[0].get("type") == "ReturnStatement"):
            _fast_return = (_body_stmts[0].get("argument"),)
        # Pre-compute simple param names: tuple of names if all params are plain Identifiers
        _simple_param_names = None
        if _params and all(p.__class__ is dict and p.get("type") == "Identifier" for p in _params):
            _simple_param_names = tuple(p["name"] for p in _params)
        fn_val = JsValue("function", {
            "node": node, "env": closure_env, "name": node.get("id") or "",
            # Pre-computed metadata to avoid repeated .get() in _call_js_impl
            "__meta__": (_is_arrow, _is_gen, _is_async, _params, _body, _body_stmts, _fast_return, _simple_param_names),
        })
        if not _is_arrow and not _is_gen:
            proto = JsValue("object", {"constructor": fn_val})
            self._set_desc(proto, "constructor", {'enumerable': False, 'writable': True, 'configurable': True})
            fn_val.value["prototype"] = proto
        return fn_val

    def _add_iterator_helpers(self, iter_obj):
        """Add ES2025 iterator helper methods to an iterator object in-place."""

        def _make_intr(fn, name):
            return self._make_intrinsic(lambda tv, args, interp: fn(args, interp), name)

        def _iter_next(it_obj):
            """Advance iterator; returns (value, done)."""
            r = self._call_js(it_obj.value['next'], [], it_obj)
            if r.type == 'object':
                done = r.value.get('done', JS_FALSE)
                if done is JS_TRUE or (isinstance(done, JsValue) and done.value is True):
                    return UNDEFINED, True
                return r.value.get('value', UNDEFINED), False
            return UNDEFINED, True

        def _iter_to_list(it_obj):
            items = []
            while True:
                val, done = _iter_next(it_obj)
                if done:
                    break
                items.append(val)
            return items

        def _make_lazy_iter(next_fn):
            """Create a lazy iterator object from a stateful next_fn() → (val, done)."""
            new_obj = JsValue('object', {})
            def _next(tv, a, intp):
                val, done = next_fn()
                if done:
                    return JsValue('object', {'value': UNDEFINED, 'done': JS_TRUE})
                return JsValue('object', {'value': val, 'done': JS_FALSE})
            new_obj.value['next'] = self._make_intrinsic(_next, 'Iterator.next')
            self._add_iterator_helpers(new_obj)
            return new_obj

        def _make_list_iter(items):
            idx = [0]
            new_obj = JsValue('object', {})
            def _next(tv, a, intp):
                if idx[0] >= len(items):
                    return JsValue('object', {'value': UNDEFINED, 'done': JS_TRUE})
                val = items[idx[0]]; idx[0] += 1
                return JsValue('object', {'value': val, 'done': JS_FALSE})
            new_obj.value['next'] = self._make_intrinsic(_next, 'Iterator.next')
            self._add_iterator_helpers(new_obj)
            return new_obj

        def _map(args, interp):
            fn = args[0] if args else UNDEFINED
            idx = [0]
            def _next():
                val, done = _iter_next(iter_obj)
                if done:
                    return UNDEFINED, True
                mapped = self._call_js(fn, [val, py_to_js(idx[0])], None)
                idx[0] += 1
                return mapped, False
            return _make_lazy_iter(_next)

        def _filter(args, interp):
            fn = args[0] if args else UNDEFINED
            idx = [0]
            def _next():
                while True:
                    val, done = _iter_next(iter_obj)
                    if done:
                        return UNDEFINED, True
                    i = idx[0]; idx[0] += 1
                    if self._truthy(self._call_js(fn, [val, py_to_js(i)], None)):
                        return val, False
            return _make_lazy_iter(_next)

        def _take(args, interp):
            n = int(self._to_num(args[0])) if args else 0
            remaining = [n]
            def _next():
                if remaining[0] <= 0:
                    return UNDEFINED, True
                val, done = _iter_next(iter_obj)
                if done:
                    return UNDEFINED, True
                remaining[0] -= 1
                return val, False
            return _make_lazy_iter(_next)

        def _drop(args, interp):
            n = int(self._to_num(args[0])) if args else 0
            skipped = [0]
            def _next():
                while skipped[0] < n:
                    val, done = _iter_next(iter_obj)
                    if done:
                        return UNDEFINED, True
                    skipped[0] += 1
                return _iter_next(iter_obj)
            return _make_lazy_iter(_next)

        def _flat_map(args, interp):
            fn = args[0] if args else UNDEFINED
            idx = [0]
            inner = [None]  # current inner iterator
            def _next():
                while True:
                    if inner[0] is not None:
                        r = self._call_js(inner[0].value['next'], [], inner[0])
                        if r.type == 'object':
                            done = r.value.get('done', JS_FALSE)
                            if not (done is JS_TRUE or (isinstance(done, JsValue) and done.value is True)):
                                return r.value.get('value', UNDEFINED), False
                        inner[0] = None
                    val, done = _iter_next(iter_obj)
                    if done:
                        return UNDEFINED, True
                    i = idx[0]; idx[0] += 1
                    mapped = self._call_js(fn, [val, py_to_js(i)], None)
                    sub_it = self._get_prop(mapped, SK_ITERATOR)
                    if isinstance(sub_it, JsValue) and sub_it.type in ('function', 'intrinsic'):
                        it_obj = self._call_js(sub_it, [], mapped)
                        if isinstance(it_obj, JsValue) and 'next' in it_obj.value:
                            inner[0] = it_obj
                            continue
                    return mapped, False
            return _make_lazy_iter(_next)

        def _to_array(args, interp):
            return JsValue('array', _iter_to_list(iter_obj))

        def _for_each(args, interp):
            fn = args[0] if args else UNDEFINED
            i = 0
            while True:
                val, done = _iter_next(iter_obj)
                if done:
                    break
                self._call_js(fn, [val, py_to_js(i)], None)
                i += 1
            return UNDEFINED

        def _some(args, interp):
            fn = args[0] if args else UNDEFINED
            i = 0
            while True:
                val, done = _iter_next(iter_obj)
                if done:
                    break
                if self._truthy(self._call_js(fn, [val, py_to_js(i)], None)):
                    return JS_TRUE
                i += 1
            return JS_FALSE

        def _every(args, interp):
            fn = args[0] if args else UNDEFINED
            i = 0
            while True:
                val, done = _iter_next(iter_obj)
                if done:
                    break
                if not self._truthy(self._call_js(fn, [val, py_to_js(i)], None)):
                    return JS_FALSE
                i += 1
            return JS_TRUE

        def _find(args, interp):
            fn = args[0] if args else UNDEFINED
            i = 0
            while True:
                val, done = _iter_next(iter_obj)
                if done:
                    break
                if self._truthy(self._call_js(fn, [val, py_to_js(i)], None)):
                    return val
                i += 1
            return UNDEFINED

        def _reduce(args, interp):
            fn = args[0] if args else UNDEFINED
            if len(args) > 1:
                acc = args[1]
                i = 0
            else:
                val, done = _iter_next(iter_obj)
                if done:
                    return UNDEFINED
                acc = val
                i = 1
            while True:
                val, done = _iter_next(iter_obj)
                if done:
                    break
                acc = self._call_js(fn, [acc, val], None)
                i += 1
            return acc

        iter_obj.value['map'] = _make_intr(_map, 'Iterator.map')
        iter_obj.value['filter'] = _make_intr(_filter, 'Iterator.filter')
        iter_obj.value['take'] = _make_intr(_take, 'Iterator.take')
        iter_obj.value['drop'] = _make_intr(_drop, 'Iterator.drop')
        iter_obj.value['flatMap'] = _make_intr(_flat_map, 'Iterator.flatMap')
        iter_obj.value['toArray'] = _make_intr(_to_array, 'Iterator.toArray')
        iter_obj.value['forEach'] = _make_intr(_for_each, 'Iterator.forEach')
        iter_obj.value['some'] = _make_intr(_some, 'Iterator.some')
        iter_obj.value['every'] = _make_intr(_every, 'Iterator.every')
        iter_obj.value['find'] = _make_intr(_find, 'Iterator.find')
        iter_obj.value['reduce'] = _make_intr(_reduce, 'Iterator.reduce')

        sym_iter_key = SK_ITERATOR
        if sym_iter_key not in iter_obj.value:
            iter_obj.value[sym_iter_key] = self._make_intrinsic(
                lambda tv, a, i: iter_obj, '[Symbol.iterator]')

        return iter_obj

    def _drain_async_iter(self, gen_obj):
        """Drain an async iterator to a Python list of JsValues."""
        items = []
        while True:
            next_fn = gen_obj.value.get('next')
            if next_fn is None:
                break
            promise = self._call_js(next_fn, [], gen_obj)
            if not self._run_event_loop(promise):
                break
            if promise.type == 'promise' and promise.value.get('state') == 'rejected':
                raise _JSError(promise.value['value'])
            result = promise.value.get('value', UNDEFINED) if promise.type == 'promise' else promise
            if result.type == 'object' and result.value.get('done', JS_FALSE).value is True:
                break
            val = result.value.get('value', UNDEFINED) if result.type == 'object' else result
            items.append(val)
        return items

    def _make_async_list_iter(self, items):
        """Create an async iterator object from a Python list of JsValues."""
        idx = [0]
        gen_obj = JsValue('object', {})
        def _next(tv, a, intp):
            if idx[0] >= len(items):
                return self._resolved_promise(JsValue('object', {'value': UNDEFINED, 'done': JS_TRUE}))
            val = items[idx[0]]; idx[0] += 1
            return self._resolved_promise(JsValue('object', {'value': val, 'done': JS_FALSE}))
        gen_obj.value['next'] = self._make_intrinsic(_next, 'AsyncIterator.next')
        sym_async_iter_key = SK_ASYNC_ITERATOR
        gen_obj.value[sym_async_iter_key] = self._make_intrinsic(lambda tv, a, i: gen_obj, '[Symbol.asyncIterator]')
        self._add_async_iterator_helpers(gen_obj)
        return gen_obj

    def _add_async_iterator_helpers(self, gen_obj):
        """Add async iterator helper methods to an async iterator object in-place."""

        def _make_intr(fn, name):
            return self._make_intrinsic(lambda tv, args, interp: fn(args, interp), name)

        def _await_val(val):
            if val.type == 'promise':
                if not self._run_event_loop(val):
                    raise _JSError(py_to_js('Promise did not settle'))
                if val.value.get('state') == 'rejected':
                    raise _JSError(val.value['value'])
                return val.value.get('value', UNDEFINED)
            return val

        def _map(args, interp):
            fn = args[0] if args else UNDEFINED
            items = self._drain_async_iter(gen_obj)
            mapped = [_await_val(self._call_js(fn, [el, py_to_js(i)], None)) for i, el in enumerate(items)]
            return self._make_async_list_iter(mapped)

        def _filter(args, interp):
            fn = args[0] if args else UNDEFINED
            items = self._drain_async_iter(gen_obj)
            filtered = [el for i, el in enumerate(items)
                        if self._truthy(_await_val(self._call_js(fn, [el, py_to_js(i)], None)))]
            return self._make_async_list_iter(filtered)

        def _take(args, interp):
            n = int(self._to_num(args[0])) if args else 0
            items = self._drain_async_iter(gen_obj)
            return self._make_async_list_iter(items[:n])

        def _drop(args, interp):
            n = int(self._to_num(args[0])) if args else 0
            items = self._drain_async_iter(gen_obj)
            return self._make_async_list_iter(items[n:])

        def _flat_map(args, interp):
            fn = args[0] if args else UNDEFINED
            items = self._drain_async_iter(gen_obj)
            result = []
            for i, el in enumerate(items):
                mapped = _await_val(self._call_js(fn, [el, py_to_js(i)], None))
                async_iter_key = SK_ASYNC_ITERATOR
                if mapped.type == 'object' and async_iter_key in mapped.value:
                    result.extend(self._drain_async_iter(mapped))
                else:
                    sub_it = self._get_js_iterator(mapped)
                    if sub_it is not None:
                        while True:
                            r = sub_it()
                            if r.type == 'object' and r.value.get('done', JS_FALSE).value is True:
                                break
                            result.append(r.value.get('value', UNDEFINED) if r.type == 'object' else r)
                    else:
                        result.append(mapped)
            return self._make_async_list_iter(result)

        def _to_array(args, interp):
            items = self._drain_async_iter(gen_obj)
            return self._resolved_promise(JsValue('array', items))

        def _for_each(args, interp):
            fn = args[0] if args else UNDEFINED
            for i, el in enumerate(self._drain_async_iter(gen_obj)):
                _await_val(self._call_js(fn, [el, py_to_js(i)], None))
            return self._resolved_promise(UNDEFINED)

        def _some(args, interp):
            fn = args[0] if args else UNDEFINED
            for i, el in enumerate(self._drain_async_iter(gen_obj)):
                if self._truthy(_await_val(self._call_js(fn, [el, py_to_js(i)], None))):
                    return self._resolved_promise(JS_TRUE)
            return self._resolved_promise(JS_FALSE)

        def _every(args, interp):
            fn = args[0] if args else UNDEFINED
            for i, el in enumerate(self._drain_async_iter(gen_obj)):
                if not self._truthy(_await_val(self._call_js(fn, [el, py_to_js(i)], None))):
                    return self._resolved_promise(JS_FALSE)
            return self._resolved_promise(JS_TRUE)

        def _find(args, interp):
            fn = args[0] if args else UNDEFINED
            for i, el in enumerate(self._drain_async_iter(gen_obj)):
                if self._truthy(_await_val(self._call_js(fn, [el, py_to_js(i)], None))):
                    return self._resolved_promise(el)
            return self._resolved_promise(UNDEFINED)

        def _reduce(args, interp):
            fn = args[0] if args else UNDEFINED
            items = self._drain_async_iter(gen_obj)
            if not items:
                return self._resolved_promise(args[1] if len(args) > 1 else UNDEFINED)
            acc = args[1] if len(args) > 1 else items[0]
            start = 0 if len(args) > 1 else 1
            for el in items[start:]:
                acc = _await_val(self._call_js(fn, [acc, el], None))
            return self._resolved_promise(acc)

        gen_obj.value['map'] = _make_intr(_map, 'AsyncIterator.map')
        gen_obj.value['filter'] = _make_intr(_filter, 'AsyncIterator.filter')
        gen_obj.value['take'] = _make_intr(_take, 'AsyncIterator.take')
        gen_obj.value['drop'] = _make_intr(_drop, 'AsyncIterator.drop')
        gen_obj.value['flatMap'] = _make_intr(_flat_map, 'AsyncIterator.flatMap')
        gen_obj.value['toArray'] = _make_intr(_to_array, 'AsyncIterator.toArray')
        gen_obj.value['forEach'] = _make_intr(_for_each, 'AsyncIterator.forEach')
        gen_obj.value['some'] = _make_intr(_some, 'AsyncIterator.some')
        gen_obj.value['every'] = _make_intr(_every, 'AsyncIterator.every')
        gen_obj.value['find'] = _make_intr(_find, 'AsyncIterator.find')
        gen_obj.value['reduce'] = _make_intr(_reduce, 'AsyncIterator.reduce')

        sym_async_iter_key = SK_ASYNC_ITERATOR
        if sym_async_iter_key not in gen_obj.value:
            gen_obj.value[sym_async_iter_key] = self._make_intrinsic(
                lambda tv, a, i: gen_obj, '[Symbol.asyncIterator]')

        return gen_obj

    def _make_generator_obj(self, fn_val, args, this_val=None):
        gen = JsGenerator(fn_val, args, self, this_val)
        sym_iter_key = SK_ITERATOR
        def _gen_next(tv, a, i):
            _val = a[0] if a else UNDEFINED
            if _log_async.isEnabledFor(TRACE):
                _log_async.log(TRACE, "generator.next(%s)", i._to_str(_val)[:60])
            return gen.next(_val)
        def _gen_return(tv, a, i):
            _val = a[0] if a else UNDEFINED
            if _log_async.isEnabledFor(TRACE):
                _log_async.log(TRACE, "generator.return(%s)", i._to_str(_val)[:60])
            return gen.js_return(_val)
        def _gen_throw(tv, a, i):
            _val = a[0] if a else UNDEFINED
            if _log_async.isEnabledFor(TRACE):
                _log_async.log(TRACE, "generator.throw(%s)", i._to_str(_val)[:60])
            return gen.js_throw(_val)
        gen_obj = JsValue('object', {
            '__kind__': JsValue('string', 'Generator'),
            '__gen__': gen,
            'next':   self._make_intrinsic(_gen_next, 'Generator.next'),
            'return': self._make_intrinsic(_gen_return, 'Generator.return'),
            'throw':  self._make_intrinsic(_gen_throw, 'Generator.throw'),
        })
        gen_obj.value[sym_iter_key] = self._make_intrinsic(lambda tv, a, i: gen_obj, '[Symbol.iterator]')
        self._add_iterator_helpers(gen_obj)
        return gen_obj

    def _make_async_generator_obj(self, fn_val, args, this_val=None):
        gen = JsAsyncGenerator(fn_val, args, self, this_val)
        sym_async_iter_key = SK_ASYNC_ITERATOR
        gen_obj = JsValue('object', {
            '__kind__': JsValue('string', 'AsyncGenerator'),
            '__gen__': gen,
            'next':   self._make_intrinsic(lambda tv, a, i: gen.next(a[0] if a else UNDEFINED), 'AsyncGenerator.next'),
            'return': self._make_intrinsic(lambda tv, a, i: gen.js_return(a[0] if a else UNDEFINED), 'AsyncGenerator.return'),
            'throw':  self._make_intrinsic(lambda tv, a, i: gen.js_throw(a[0] if a else UNDEFINED), 'AsyncGenerator.throw'),
        })
        gen_obj.value[sym_async_iter_key] = self._make_intrinsic(lambda tv, a, i: gen_obj, '[Symbol.asyncIterator]')
        self._add_async_iterator_helpers(gen_obj)
        return gen_obj

    def _call_js(self, fn_val, args, this_val=None, extra_args=None, is_new_call=False):
        _tracing = _TRACE_ACTIVE[0]
        if _tracing:
            _log_call.debug("call %s (new=%s, nargs=%d)", fn_val.type, is_new_call, len(args))
        self._call_depth += 1
        if self._call_depth > self.MAX_CALL_DEPTH:
            self._call_depth -= 1
            raise _JSError(self._make_js_error('RangeError', 'Maximum call stack size exceeded'))
        if _tracing:
            fn_name = "<anonymous>"
            if fn_val.type in ("function", "intrinsic") and isinstance(fn_val.value, dict):
                fn_name = fn_val.value.get("name") or "<anonymous>"
            elif fn_val.type == "class" and isinstance(fn_val.value, dict):
                fn_name = fn_val.value.get("name") or "<class>"
            if _log_call.isEnabledFor(TRACE):
                arg_strs = [self._to_str(a)[:40] for a in args[:4]]
                _log_call.log(TRACE, "→ %s(%s)", fn_name, ", ".join(arg_strs))
            push_depth()
            frame = {'name': fn_name, 'file': self._module_file or '<anonymous>', 'line': 0}
            self._js_call_stack.append(frame)
            _call_result = None
            try:
                _call_result = self._call_js_impl(fn_val, args, this_val, extra_args, is_new_call)
                return _call_result
            finally:
                if self._js_call_stack and self._js_call_stack[-1] is frame:
                    self._js_call_stack.pop()
                self._call_depth -= 1
                if _log_call.isEnabledFor(TRACE):
                    _log_call.log(TRACE, "← %s = %s", fn_name, self._to_str(_call_result)[:80] if _call_result is not None else "undefined")
                pop_depth()
        else:
            # Fast path: no tracing overhead
            try:
                return self._call_js_impl(fn_val, args, this_val, extra_args, is_new_call)
            finally:
                self._call_depth -= 1

    def _call_js_impl(self, fn_val, args, this_val=None, extra_args=None, is_new_call=False):
        _fntype = fn_val.type
        if _fntype == 'proxy':
            proxy = fn_val.value
            if is_new_call:
                trap = self._get_trap(proxy.handler, 'construct')
                if trap:
                    return self._call_js(trap, [proxy.target, JsValue('array', args), fn_val], UNDEFINED)
            else:
                trap = self._get_trap(proxy.handler, 'apply')
                if trap:
                    return self._call_js(trap, [proxy.target, this_val if this_val is not None else UNDEFINED, JsValue('array', args)], UNDEFINED)
            return self._call_js(proxy.target, args, this_val, extra_args, is_new_call)
        if _fntype == "intrinsic":
            return fn_val.value["fn"](this_val, args, self)
        if _fntype == "function":
            info = fn_val.value
            # Use pre-computed metadata if available (from _make_fn)
            try:
                meta = info["__meta__"]
                _is_arrow, _is_gen, _is_async, params, body, body_stmts, _fast_return, _simple_param_names = meta
            except KeyError:
                node = info["node"]
                _is_arrow = bool(node.get("arrow"))
                _is_gen = bool(node.get("generator_"))
                _is_async = bool(node.get("async_"))
                params = node.get("params", [])
                body = node["body"]
                body_stmts = body.get("body", []) if isinstance(body, dict) and body.get("type") == "BlockStatement" else []
                _fast_return = None
                _simple_param_names = None
            # Generator function — return generator object immediately
            if _is_gen:
                if _is_async:
                    return self._make_async_generator_obj(fn_val, args, this_val)
                return self._make_generator_obj(fn_val, args, this_val)
            env = info["env"]
            call_env = Environment(env)
            if _TRACE_ACTIVE[0]:
                _fn_name = info.get("name") or "<anonymous>"
                _log_scope.info("scope create (function %s)", _fn_name)
            call_env._this = this_val if this_val is not None else UNDEFINED
            call_env._is_arrow = _is_arrow
            call_env._is_fn_env = True
            if not _is_arrow:
                call_env._fn_args = list(args)
                call_env._fn_val = fn_val
            if is_new_call:
                call_env.declare('__new_target__', fn_val, 'const')
            try:
                super_proto = info['super_proto']
            except KeyError:
                super_proto = None
            if super_proto.__class__ is JsValue:
                super_ctor = info.get('superClass')
                call_env.declare('super', self._make_super_proxy(super_proto, call_env._this, super_ctor), 'const')
            # Bind parameters — fast path for all-Identifier params
            _bindings = call_env.bindings
            _nargs = len(args)
            if _simple_param_names is not None:
                # All params are simple Identifiers — direct binding, no type checks
                for _idx, _pname in enumerate(_simple_param_names):
                    _bindings[_pname] = ['var', args[_idx] if _idx < _nargs else UNDEFINED]
            else:
                arg_index = 0
                for p in params:
                    if p.__class__ is dict:
                        _ptype = p["type"]
                        if _ptype == "RestElement":
                            rest = args[arg_index:] if arg_index < _nargs else []
                            self._bind_pattern(p["argument"], JsValue("array", list(rest)), call_env, 'var', True)
                            break
                        val = args[arg_index] if arg_index < _nargs else UNDEFINED
                        if _ptype == "Identifier":
                            _bindings[p["name"]] = ['var', val]
                        elif _ptype == "AssignmentPattern":
                            if val is UNDEFINED or val.type == 'undefined':
                                val = self._eval(p['right'], call_env)
                            left = p['left']
                            if left.__class__ is dict and left.get('type') == 'Identifier':
                                _bindings[left["name"]] = ['var', val]
                            else:
                                self._bind_pattern(left, val, call_env, 'var', True)
                        else:
                            self._bind_pattern(p, val, call_env, 'var', True)
                    else:
                        val = args[arg_index] if arg_index < _nargs else UNDEFINED
                        self._bind_pattern(p, val, call_env, 'var', True)
                    arg_index += 1
            promise = self._new_promise() if _is_async else None
            # Hoist var declarations (cached on body node)
            if body_stmts:
                try:
                    cached_hoist = body['__hoist__']
                except KeyError:
                    cached_hoist = []
                    for s in body_stmts:
                        cached_hoist.extend(Interpreter._collect_var_names(s))
                    body['__hoist__'] = cached_hoist
                for name in cached_hoist:
                    if name not in _bindings:
                        _bindings[name] = ['var', UNDEFINED]
                try:
                    cached_strict = body['__strict__']
                except KeyError:
                    cached_strict = self._has_use_strict(body_stmts)
                    body['__strict__'] = cached_strict
                if cached_strict:
                    call_env._strict = True
            try:
                self.env = call_env
                if _fast_return is not None:
                    # Single-return fast path: skip _exec + _JSReturn exception
                    _fr_arg = _fast_return[0]
                    result = self._eval(_fr_arg, call_env) if _fr_arg is not None else UNDEFINED
                else:
                    self._exec(body, call_env)
                    result = UNDEFINED
            except _JSReturn as e:
                result = e.value
            except _JSError as exc:
                if promise is not None:
                    return self._reject_promise(promise, exc.value)
                raise
            except Exception as exc:
                if promise is not None:
                    return self._reject_promise(promise, self._make_js_error('Error', str(exc)))
                raise
            finally:
                self.env = env
            if promise is not None:
                return self._resolve_promise(promise, result)
            return result
        if _fntype == "class":
            return fn_val
        raise _JSError(self._make_js_error('TypeError', f"{self._to_str(fn_val)} is not a function"))

    # --------------------------------------------------------- main run
    def run(self, source: str) -> str:
        import sys as _sys
        self._exec_steps = 0
        self._last_value = None
        self._last_error = None
        self._js_call_stack.clear()
        start = len(self.output)
        try:
            tokens = Lexer(source).tokenize()
            ast = Parser(tokens).parse()
            # Track last expression value for REPL display
            body = ast.get('body', [])
            if (len(body) == 1 and
                    body[0].get('type') == 'ExpressionStatement'):
                self._last_value = self._eval(body[0]['expression'], self.genv)
                self._run_event_loop()
            else:
                self._exec(ast, self.genv)
                self._run_event_loop()
        except _JSReturn:
            pass
        except _JSError as e:
            err_str = self._to_str(e.value)
            _log_error.info("uncaught JS error: %s", err_str[:120])
            # Build structured error info
            stack_str = ''
            if isinstance(e.value.value, dict):
                stack_val = e.value.value.get('stack')
                stack_str = stack_val.value if isinstance(stack_val, JsValue) and stack_val.type == 'string' else ''
            err_type = ''
            if isinstance(e.value.value, dict):
                t = e.value.value.get('__error_type__')
                err_type = t.value if isinstance(t, JsValue) else ''
            msg = ''
            if isinstance(e.value.value, dict):
                m = e.value.value.get('message')
                msg = m.value if isinstance(m, JsValue) else self._to_str(e.value)
            else:
                msg = self._to_str(e.value)
            self._last_error = {
                'js_error': True,
                'error_type': err_type or 'Error',
                'message': msg,
                'stack': stack_str,
                'python_exc': None,
            }
            self.output.append(f"{err_type or 'Error'}: {msg}" if err_type else f"Error: {msg}")
        except Exception as e:  # Catches non-control-flow Python errors at top level
            import traceback as _tb
            tb_str = _tb.format_exc()
            _log_error.error("uncaught Python error: %s\n%s", str(e)[:80], tb_str[:500])
            self._last_error = {
                'js_error': False,
                'error_type': type(e).__name__,
                'message': str(e),
                'stack': '',
                'python_exc': e,
                'python_traceback': tb_str,
            }
            self.output.append(f"InternalError: {e}")
        return '\n'.join(self.output[start:])

    def run_module(self, source: str) -> None:
        """Execute source as a module (supports import/export statements)."""
        tokens = Lexer(source).tokenize()
        ast = Parser(tokens).parse()
        self._exec(ast, self.genv)
        self._run_event_loop()



def _ta_coerce(jsv, fmt, interp):
    """Coerce a JsValue to the appropriate Python type for struct.pack_into."""
    if fmt in ('e', 'f', 'd'):
        return interp._to_num(jsv)
    # integer formats
    n = interp._to_num(jsv)
    if math.isnan(n) or math.isinf(n):
        n = 0
    v = int(n)
    bits = {'b': 8, 'B': 8, 'h': 16, 'H': 16, 'i': 32, 'I': 32, 'q': 64, 'Q': 64}.get(fmt, 8)
    v = v & ((1 << bits) - 1)
    if fmt in ('b', 'h', 'i', 'q'):
        if v >= (1 << (bits - 1)):
            v -= (1 << bits)
    return v
