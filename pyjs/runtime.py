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

from .core import JSTypeError, js_to_py, py_to_js
from .plugin import PyJSPlugin, PluginContext
from .lexer import Lexer
from .parser import N, Parser
from .trace import configure as _configure_trace, get_logger
from .values import (
    JsValue, JsProxy, UNDEFINED, JS_NULL, JS_TRUE, JS_FALSE,
    SYMBOL_ITERATOR, SYMBOL_TO_PRIMITIVE, SYMBOL_HAS_INSTANCE,
    SYMBOL_TO_STRING_TAG, SYMBOL_ASYNC_ITERATOR, SYMBOL_SPECIES,
    SYMBOL_MATCH, SYMBOL_REPLACE, SYMBOL_SPLIT, SYMBOL_SEARCH,
    SYMBOL_IS_CONCAT_SPREADABLE,
    _symbol_id_counter, _symbol_registry,
    _js_regex_to_python,
)
from .environment import Environment
from .exceptions import _JSBreak, _JSContinue, _JSReturn, _JSError, flatten_one
from .generators import JsGenerator, JsAsyncGenerator
from .builtins_core import register_core_builtins
from .builtins_object import register_object_builtins
from .builtins_advanced import register_advanced_builtins
from .builtins_promise import register_promise_builtins
from .builtins_typed import register_typed_builtins

_log_exec = get_logger("exec")
_log_eval = get_logger("eval")
_log_call = get_logger("call")
_log_prop = get_logger("prop")
_log_event = get_logger("event")
_log_promise = get_logger("promise")


# ============================================================================
#  Interpreter
# ============================================================================
# ============================================================================

class Interpreter:
    ARRAY_METHODS = frozenset({'push', 'pop', 'shift', 'unshift', 'indexOf', 'includes', 'join', 'slice', 'splice', 'concat', 'reverse', 'sort', 'forEach', 'map', 'filter', 'reduce', 'find', 'flat', 'flatMap', 'every', 'some', 'fill', 'copyWithin', 'toString', 'at', 'findIndex', 'findLast', 'findLastIndex', 'reduceRight', 'lastIndexOf', 'toSorted', 'toReversed', 'toSpliced', 'with'})
    STRING_METHODS = frozenset({'charAt', 'charCodeAt', 'indexOf', 'includes', 'slice', 'substring', 'toLowerCase', 'toUpperCase', 'trim', 'split', 'replace', 'replaceAll', 'startsWith', 'endsWith', 'padStart', 'padEnd', 'repeat', 'match', 'search', 'concat', 'lastIndexOf', 'normalize', 'at', 'matchAll', 'trimStart', 'trimLeft', 'trimEnd', 'trimRight', 'codePointAt'})
    NUMBER_METHODS = frozenset({'toFixed', 'toPrecision', 'toString', 'toLocaleString', 'valueOf', 'toExponential'})
    PROMISE_METHODS = frozenset({'then', 'catch', 'finally'})
    EVENT_LOOP_LIMIT = 10000

    MAX_CALL_DEPTH = 200
    MAX_EXEC_STEPS = 10_000_000

    def __init__(self, log_level: str | None = None, plugins: list | None = None):
        _configure_trace(log_level)
        # Ensure Python's recursion limit can accommodate our JS call depth
        _min_py_limit = self.MAX_CALL_DEPTH * 6
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
        self.genv = self._global_env()
        self.env  = self.genv
        self._module_exports: dict = {}
        self._module_loader = None
        self._module_file: str | None = None
        self._module_url: str | None = None
        self._plugins: list[PyJSPlugin] = []
        self._plugin_contexts: list[PluginContext] = []
        self._plugin_methods: dict = {}
        if plugins:
            for plugin in plugins:
                self.use(plugin)


    def use(self, plugin: PyJSPlugin) -> 'Interpreter':
        """Register a plugin with this interpreter.

        The plugin's setup() method is called immediately.
        Returns self for chaining: interp.use(A()).use(B())
        """
        ctx = PluginContext(self)
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

        def _make_intr(fn, name):
            return self._make_intrinsic(lambda tv, args, interp: fn(args, interp), name)

        def _regexp_test(args, interp):
            text = interp._to_str(args[0]) if args else ''
            return JS_TRUE if re.search(py_source, text, py_flags) else JS_FALSE

        def _regexp_exec(args, interp):
            text = interp._to_str(args[0]) if args else ''
            match = re.search(py_source, text, py_flags)
            if not match:
                return JS_NULL
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
            if 'd' in flag_text:
                indices_arr = []
                for i in range(len(match.regs)):
                    start, end = match.regs[i]
                    if start == -1:
                        indices_arr.append(UNDEFINED)
                    else:
                        indices_arr.append(JsValue('array', [py_to_js(float(start)), py_to_js(float(end))]))
                result.extras['indices'] = JsValue('array', indices_arr)
            return result

        regexp = JsValue('object', {})
        regexp.value['__kind__'] = JsValue('string', 'RegExp')
        regexp.value['source'] = JsValue('string', source)
        regexp.value['flags'] = JsValue('string', flag_text)
        regexp.value['global'] = JS_TRUE if 'g' in flag_text else JS_FALSE
        regexp.value['ignoreCase'] = JS_TRUE if 'i' in flag_text else JS_FALSE
        regexp.value['multiline'] = JS_TRUE if 'm' in flag_text else JS_FALSE
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
        self._active_timers[timer_id] = task
        self._push_timer(task, self._clock + task['delay'])
        return timer_id

    def _clear_timer(self, timer_id):
        self._active_timers.pop(timer_id, None)

    def _new_promise(self):
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
        except Exception:
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
        _log_event.debug("event loop start")
        while True:
            if until_promise and until_promise.value['state'] != 'pending':
                break
            if self._microtasks:
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
                self._call_js(candidate['fn'], list(candidate['args']), UNDEFINED)
                steps += 1
                if candidate['repeat'] and candidate['id'] in self._active_timers:
                    self._push_timer(candidate, self._clock + candidate['delay'])
                else:
                    self._active_timers.pop(candidate['id'], None)
            if steps > self.EVENT_LOOP_LIMIT:
                raise _JSError(py_to_js('Event loop exceeded limit; possible unbounded interval or promise recursion'))
        return not until_promise or until_promise.value['state'] != 'pending'

    # --------------------------------------------------------- global env
    def _global_env(self) -> Environment:
        g = Environment()

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
    def _make_js_error(self, name, msg):
        return JsValue('object', {
            'message': py_to_js(msg),
            'name': py_to_js(name),
            'stack': py_to_js(f"{name}: {msg}"),
            '__error_type__': py_to_js(name),
        })

    # --------------------------------------------------------- coercion helpers
    def _truthy(self, v: JsValue) -> bool:
        if v.type in ('null','undefined'): return False
        if v.type == 'boolean':   return v.value
        if v.type == 'number':    return v.value != 0 and not math.isnan(v.value)
        if v.type == 'bigint':    return v.value != 0
        if v.type == 'string':    return len(v.value) > 0
        return True

    def _to_num(self, v: JsValue) -> float:
        if v.type == 'number':    return v.value
        if v.type == 'bigint':    return float(v.value)
        if v.type == 'boolean':   return 1.0 if v.value else 0.0
        if v.type in ('null',):   return 0.0
        if v.type == 'undefined': return float('nan')
        if v.type == 'string':
            try: return float(v.value.strip() or 0)
            except (ValueError, TypeError, OverflowError): return float('nan')
        return float('nan')

    def _to_str(self, v: JsValue) -> str:
        if v.type == 'null':      return 'null'
        if v.type == 'undefined': return 'undefined'
        if v.type == 'boolean':   return 'true' if v.value else 'false'
        if v.type == 'bigint':    return str(v.value)
        if v.type == 'symbol':    return f"Symbol({v.value.get('desc', '')})"
        if v.type == 'number':
            n = v.value
            if math.isnan(n): return 'NaN'
            if math.isinf(n): return 'Infinity' if n>0 else '-Infinity'
            if n == int(n) and abs(n) < 1e15: return str(int(n))
            return str(n)
        if v.type == 'string':    return v.value
        if v.type == 'array':     return ','.join(self._to_str(e) for e in v.value)
        if v.type == 'promise':   return '[object Promise]'
        if v.type == 'proxy':     return self._to_str(v.value.target)
        if v.type in ('object', 'function', 'intrinsic', 'class'):
            if isinstance(v.value, dict):
                kind = v.value.get('__kind__')
                if isinstance(kind, JsValue) and kind.value == 'Generator':
                    return '[object Generator]'
                # Check Symbol.toStringTag
                tag_key = f"@@{SYMBOL_TO_STRING_TAG}@@"
                tag = v.value.get(tag_key)
                if tag and isinstance(tag, JsValue) and tag.type == 'string':
                    return f'[object {tag.value}]'
            if v.type in ('function', 'intrinsic', 'class'):
                return f'function {v.value.get("name","")}() {{ [native code] }}'
            return '[object Object]'
        return str(v.value)

    def _to_primitive(self, val, hint='default'):
        """Convert a JS value to a primitive."""
        if val.type in ('undefined','null','boolean','number','string','bigint','symbol'):
            return val
        # Check Symbol.toPrimitive
        sym_key = f"@@{SYMBOL_TO_PRIMITIVE}@@"
        tp_fn = val.value.get(sym_key) if isinstance(val.value, dict) else None
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
        return js_to_py(v)

    def _from_py(self, val):
        return py_to_js(val)

    def _typeof(self, v: JsValue) -> str:
        if v.type == 'undefined': return 'undefined'
        if v.type == 'null':      return 'object'
        if v.type == 'number':    return 'number'
        if v.type == 'bigint':    return 'bigint'
        if v.type == 'string':    return 'string'
        if v.type == 'boolean':   return 'boolean'
        if v.type == 'symbol':    return 'symbol'
        if v.type in ('function','intrinsic','class'): return 'function'
        if v.type == 'proxy':
            t = v.value.target
            if t.type in ('function','intrinsic','class'): return 'function'
            return 'object'
        if v.type in ('object','array','promise'): return 'object'
        return 'undefined'

    def _is_nullish(self, v: JsValue) -> bool:
        return v.type in ('null', 'undefined')

    def _to_key(self, value):
        if isinstance(value, JsValue):
            if value.type == 'symbol':
                return f"@@{value.value['id']}@@"
            if value.type == 'number':
                if value.value == int(value.value):
                    return str(int(value.value))
                return str(value.value)
            if value.type == 'string':
                return value.value
            return self._to_str(value)
        return str(value)

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
        if value.type == 'array':
            items = list(value.value)
            idx = [0]
            def _arr_next():
                if idx[0] >= len(items):
                    return JsValue('object', {'value': UNDEFINED, 'done': JS_TRUE})
                v = items[idx[0]]; idx[0] += 1
                return JsValue('object', {'value': v, 'done': JS_FALSE})
            return _arr_next

        if value.type == 'string':
            chars = list(value.value)
            idx = [0]
            def _str_next():
                if idx[0] >= len(chars):
                    return JsValue('object', {'value': UNDEFINED, 'done': JS_TRUE})
                v = JsValue('string', chars[idx[0]]); idx[0] += 1
                return JsValue('object', {'value': v, 'done': JS_FALSE})
            return _str_next

        if value.type in ('object', 'function', 'intrinsic', 'class'):
            sym_key = f"@@{SYMBOL_ITERATOR}@@"
            iter_fn = self._get_prop(value, sym_key)
            if not (iter_fn and self._is_callable(iter_fn)):
                iter_fn = None
            if iter_fn and self._is_callable(iter_fn):
                iterator = self._call_js(iter_fn, [], value)
                next_fn = self._get_prop(iterator, 'next')
                if self._is_callable(next_fn):
                    def _obj_next(nf=next_fn, it=iterator):
                        return self._call_js(nf, [], it)
                    return _obj_next

            # Already an iterator (has .next method but no [Symbol.iterator])
            next_fn = value.value.get('next') if isinstance(value.value, dict) else None
            if next_fn and self._is_callable(next_fn):
                def _iter_next(nf=next_fn, it=value):
                    return self._call_js(nf, [], it)
                return _iter_next

        return None

    def _get_async_iterator(self, value):
        """Returns a callable () -> Promise<{value, done}> or None."""
        if value.type in ('object', 'function', 'intrinsic', 'class'):
            sym_async_key = f"@@{SYMBOL_ASYNC_ITERATOR}@@"
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
        self._sync_global_binding(name, value, env)

    def _sync_global_binding(self, name, value, env):
        if getattr(self, '_global_object', None) is not None and env is self.genv and name != 'globalThis':
            self._global_object.value[name] = value

    def _get_proto(self, obj: JsValue):
        if obj.type in ('object', 'function', 'intrinsic', 'class'):
            proto = obj.value.get('__proto__')
            if isinstance(proto, JsValue) and proto.type == 'object':
                return proto
        return None

    def _make_super_proxy(self, proto, this_val):
        proxy = JsValue('object', {})
        proxy.value['__super_target__'] = proto
        proxy.value['__super_this__'] = this_val
        return proxy

    def _bind_pattern(self, pattern, value, env, keyword='var', declare=True):
        if pattern is None:
            return
        if isinstance(pattern, str):
            self._bind_value(pattern, value, env, keyword, declare)
            return
        if pattern.get('type') == 'Identifier':
            self._bind_value(pattern['name'], value, env, keyword, declare)
            return
        if pattern.get('type') == 'AssignmentPattern':
            next_value = value
            if next_value.type == 'undefined':
                next_value = self._eval(pattern['right'], env)
            self._bind_pattern(pattern['left'], next_value, env, keyword, declare)
            return
        if pattern.get('type') == 'RestElement':
            self._bind_pattern(pattern['argument'], value, env, keyword, declare)
            return
        if pattern.get('type') == 'ArrayPattern':
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
        if pattern.get('type') == 'ObjectPattern':
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
                    prop_value = source.value.get(key, UNDEFINED)
                elif source.type == 'array':
                    try:
                        idx = int(key)
                        prop_value = source.value[idx] if 0 <= idx < len(source.value) else UNDEFINED
                    except Exception:
                        prop_value = UNDEFINED
                elif source.type == 'string':
                    try:
                        idx = int(key)
                        prop_value = JsValue('string', source.value[idx]) if 0 <= idx < len(source.value) else UNDEFINED
                    except Exception:
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
            return (
                lambda target_env=target_env, name=name: target_env.bindings[name][1],
                lambda val, target_env=target_env, name=name: (
                    target_env.set_own(name, val),
                    self._sync_global_binding(name, val, target_env),
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

    def _get_prop(self, obj: JsValue, prop):
        key = self._to_key(prop)
        _log_prop.debug("get %s.%s", obj.type, key)
        if getattr(self, '_global_object', None) is obj and self.genv.has(key):
            return self.genv.get(key)
        if obj.type == 'proxy':
            proxy = obj.value
            trap = self._get_trap(proxy.handler, 'get')
            if trap:
                return self._call_js(trap, [proxy.target, py_to_js(key), obj], UNDEFINED)
            return self._get_prop(proxy.target, key)
        if obj.type == 'object' and '__super_target__' in obj.value:
            target = obj.value.get('__super_target__')
            if isinstance(target, JsValue):
                return self._get_prop(target, key)
            return UNDEFINED
        if obj.type == 'array':
            if key == 'length':
                return JsValue("number", len(obj.value))
            sym_iter_key = f"@@{SYMBOL_ITERATOR}@@"
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
            if key in self.ARRAY_METHODS:
                return self._arr_method(obj, key)
            try:
                idx = int(key)
                if 0 <= idx < len(obj.value):
                    return obj.value[idx]
            except ValueError:
                pass
            if obj.extras and key in obj.extras:
                return obj.extras[key]
            # Check plugin-registered methods
            plugin_key = ('array', key)
            if self._plugin_methods and plugin_key in self._plugin_methods:
                handler = self._plugin_methods[plugin_key]
                return self._make_intrinsic(lambda tv, a, i, h=handler: h(tv, a, i), key)
            return UNDEFINED
        if obj.type == 'string':
            if key == 'length':
                return JsValue("number", len(obj.value))
            sym_iter_key = f"@@{SYMBOL_ITERATOR}@@"
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
            # Check plugin-registered methods
            plugin_key = ('string', key)
            if self._plugin_methods and plugin_key in self._plugin_methods:
                handler = self._plugin_methods[plugin_key]
                return self._make_intrinsic(lambda tv, a, i, h=handler: h(tv, a, i), key)
            return UNDEFINED
        if obj.type == 'promise':
            if key in self.PROMISE_METHODS:
                return self._promise_method(obj, key)
            # Check plugin-registered methods
            plugin_key = ('promise', key)
            if self._plugin_methods and plugin_key in self._plugin_methods:
                handler = self._plugin_methods[plugin_key]
                return self._make_intrinsic(lambda tv, a, i, h=handler: h(tv, a, i), key)
            return UNDEFINED
        if obj.type in ('object', 'function', 'intrinsic', 'class'):
            # WeakRef: deref() returns stored target
            obj_type = obj.value.get('__type__') if isinstance(obj.value, dict) else None
            if isinstance(obj_type, JsValue) and obj_type.value == 'WeakRef':
                if key == 'deref':
                    target = obj.value.get('__target__', UNDEFINED)
                    return self._make_intrinsic(lambda tv, a, i, t=target: t, 'WeakRef.deref')
            # TypedArray / DataView / ArrayBuffer special handling
            _obj_type_str = obj_type.value if isinstance(obj_type, JsValue) else obj_type
            if _obj_type_str == 'TypedArray':
                return self._typed_array_get_prop(obj, key)
            if _obj_type_str == 'DataView':
                return self._dataview_get_prop(obj, key)
            if _obj_type_str == 'ArrayBuffer':
                return self._arraybuffer_get_prop(obj, key)
            kind = obj.value.get('__kind__')
            if isinstance(kind, JsValue) and kind.type == 'string':
                if kind.value == 'Map' and key == 'size':
                    size_fn = obj.value.get('__size_fn__')
                    return self._call_js(size_fn, [], obj) if size_fn else JsValue('number', 0)
                if kind.value == 'Set' and key == 'size':
                    size_fn = obj.value.get('__size_fn__')
                    return self._call_js(size_fn, [], obj) if size_fn else JsValue('number', 0)
            # Function.name and Function.length (must intercept before value-dict lookup)
            if obj.type in ('function', 'intrinsic') and key == 'name':
                raw_name = obj.value.get('name', '') if isinstance(obj.value, dict) else ''
                return JsValue('string', raw_name if isinstance(raw_name, str) else '')
            if obj.type == 'function' and key == 'length':
                node = obj.value.get('node', {}) if isinstance(obj.value, dict) else {}
                params = node.get('params', []) if isinstance(node, dict) else []
                count = 0
                for p in params:
                    if isinstance(p, dict) and p.get('type') in ('RestElement', 'AssignmentPattern'):
                        break
                    count += 1
                return JsValue('number', float(count))
            if obj.type == 'intrinsic' and key == 'length':
                return JsValue('number', 0.0)
            current = obj
            while isinstance(current, JsValue) and current.type in ('object', 'function', 'intrinsic', 'class'):
                getter_key = f"__get__{key}"
                if getter_key in current.value:
                    return self._call_js(current.value[getter_key], [], obj)
                if key in current.value:
                    return current.value[key]
                current = self._get_proto(current)
            # Object.prototype methods: hasOwnProperty, toString, valueOf
            if key == 'hasOwnProperty':
                obj_ref = obj
                def _has_own_property(tv, call_args, interp, _obj=obj_ref):
                    prop_name = interp._to_key(call_args[0]) if call_args else ''
                    if isinstance(_obj.value, dict) and prop_name in _obj.value:
                        return JS_TRUE
                    return JS_FALSE
                return self._make_intrinsic(_has_own_property, 'Object.hasOwnProperty')
            if key == 'valueOf':
                obj_ref = obj
                return self._make_intrinsic(lambda tv, a, i, o=obj_ref: o, 'Object.valueOf')
            if key == 'toString' and obj.type == 'object':
                tag_key = f"@@{SYMBOL_TO_STRING_TAG}@@"
                tag = obj.value.get(tag_key, None) if isinstance(obj.value, dict) else None
                if isinstance(tag, JsValue) and tag.type == 'string':
                    label = tag.value
                else:
                    label = 'Object'
                return self._make_intrinsic(lambda tv, a, i, l=label: JsValue('string', f'[object {l}]'), 'Object.toString')
            # Function.prototype methods: call, apply, bind
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
            if obj.type in ('function', 'intrinsic') and key == 'toString':
                name = obj.value.get('name', '') if isinstance(obj.value, dict) else ''
                return self._make_intrinsic(lambda tv, a, i, n=name: JsValue('string', f'function {n}() {{ [native code] }}'), 'Function.toString')
            # Check plugin-registered methods for object type
            plugin_key = ('object', key)
            if self._plugin_methods and plugin_key in self._plugin_methods:
                handler = self._plugin_methods[plugin_key]
                return self._make_intrinsic(lambda tv, a, i, h=handler: h(tv, a, i), key)
            return UNDEFINED
        if obj.type == 'number':
            if key in self.NUMBER_METHODS:
                return self._num_method(obj, key)
            # Check plugin-registered methods
            plugin_key = ('number', key)
            if self._plugin_methods and plugin_key in self._plugin_methods:
                handler = self._plugin_methods[plugin_key]
                return self._make_intrinsic(lambda tv, a, i, h=handler: h(tv, a, i), key)
        if obj.type == 'symbol':
            sym_str = self._to_str(obj)
            if key == 'toString':
                return self._make_intrinsic(lambda tv, a, i: JsValue('string', sym_str), 'Symbol.toString')
            if key == 'description':
                return JsValue('string', obj.value.get('desc', ''))
        return UNDEFINED

    def _set_prop(self, obj: JsValue, prop, val: JsValue):
        key = self._to_key(prop)
        _log_prop.debug("set %s.%s", obj.type, key)
        if getattr(self, '_global_object', None) is obj and self.genv.has(key):
            self.genv.set(key, val)
            self._sync_global_binding(key, val, self.genv)
            return
        if obj.type == 'proxy':
            proxy = obj.value
            trap = self._get_trap(proxy.handler, 'set')
            if trap:
                self._call_js(trap, [proxy.target, py_to_js(key), val, obj], UNDEFINED)
                return
            self._set_prop(proxy.target, key, val)
            return
        if obj.type == 'array':
            try:
                idx = int(key)
                while len(obj.value) <= idx:
                    obj.value.append(UNDEFINED)
                obj.value[idx] = val
            except ValueError:
                obj.value[key] = val if key != 'length' else val
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
            while isinstance(current, JsValue):
                setter_key = f"__set__{key}"
                if setter_key in current.value:
                    self._call_js(current.value[setter_key], [val], obj)
                    return
                current = self._get_proto(current)
            # Check non-extensible
            if obj.value.get('__extensible__') is False and key not in obj.value:
                return  # silently ignore new property on non-extensible object
            # Check writable descriptor
            desc = self._get_desc(obj, key)
            if desc is not None and not desc.get('writable', True):
                return  # silently ignore write to non-writable property
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
                a.extend(args); return JsValue("number", len(a))
            if name == 'pop':
                return a.pop() if a else UNDEFINED
            if name == 'shift':
                return a.pop(0) if a else UNDEFINED
            if name == 'unshift':
                for i,x in enumerate(args): a.insert(i,x)
                return JsValue("number", len(a))
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
                for x in a[start:]:
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
                    sym_ics_key = f"@@{SYMBOL_IS_CONCAT_SPREADABLE}@@"
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
                def sort_key(x):
                    if x.type == 'number': return (0, x.value)
                    if x.type == 'string': return (1, x.value)
                    return (2, interp._to_str(x))
                a.sort(key=sort_key); return arr
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
                idx = 0
                acc = args[1] if len(args)>1 else (a[0] if a else None)
                if acc is None and not a:
                    raise _JSError(py_to_js('Reduce of empty array with no initial value'))
                if acc is None:
                    acc = a[0]; idx = 1
                for i in range(idx, len(a)):
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
                sym_split_key = f"@@{SYMBOL_SPLIT}@@"
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
                parts = s.split(sep, lim) if sep is not None else list(s)
                if lim is not None: parts = parts[:lim]
                return py_to_js(parts)
            if name == 'replace':
                pat_arg = args[0] if args else UNDEFINED
                repl_arg = args[1] if len(args) > 1 else UNDEFINED
                str_jsval = JsValue('string', s)
                # Symbol.replace delegation
                sym_replace_key = f"@@{SYMBOL_REPLACE}@@"
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
                        def _fn_repl(m):
                            call_args = [JsValue('string', m.group(0))]
                            call_args.extend(JsValue('string', g) if g is not None else UNDEFINED for g in m.groups())
                            call_args.append(JsValue('number', float(m.start())))
                            call_args.append(JsValue('string', s))
                            return interp._to_str(interp._call_js(repl_arg, call_args, None))
                        return JsValue('string', re.sub(py_src, _fn_repl, s, count=count, flags=py_flg))
                    else:
                        repl_str = interp._to_str(repl_arg) if isinstance(repl_arg, JsValue) and repl_arg.type != 'undefined' else ''
                        def _str_repl(m):
                            r = repl_str
                            def _named(nm):
                                try: return m.group(nm.group(1)) or ''
                                except: return nm.group(0)
                            r = re.sub(r'\$<([^>]+)>', _named, r)
                            def _numbered(ng):
                                n = int(ng.group(1))
                                try: return m.group(n) or ''
                                except: return ng.group(0)
                            r = re.sub(r'\$(\d+)', _numbered, r)
                            r = r.replace('$$', '\x00')
                            r = r.replace('$&', m.group(0))
                            r = r.replace('\x00', '$')
                            return r
                        return JsValue('string', re.sub(py_src, _str_repl, s, count=count, flags=py_flg))
                else:
                    old = _pattern_text(pat_arg)
                    new = interp._to_str(repl_arg) if isinstance(repl_arg, JsValue) else ''
                    return JsValue("string", s.replace(old, new, 1))
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
                    repl_str = interp._to_str(repl_arg) if isinstance(repl_arg, JsValue) and repl_arg.type != 'undefined' else ''
                    def _str_repl_all(m):
                        r = repl_str
                        def _named(nm):
                            try: return m.group(nm.group(1)) or ''
                            except: return nm.group(0)
                        r = re.sub(r'\$<([^>]+)>', _named, r)
                        def _numbered(ng):
                            n = int(ng.group(1))
                            try: return m.group(n) or ''
                            except: return ng.group(0)
                        r = re.sub(r'\$(\d+)', _numbered, r)
                        r = r.replace('$$', '\x00')
                        r = r.replace('$&', m.group(0))
                        r = r.replace('\x00', '$')
                        return r
                    return JsValue('string', re.sub(py_src, _str_repl_all, s, flags=py_flg))
                else:
                    old = _pattern_text(pat_arg)
                    new = interp._to_str(repl_arg) if isinstance(repl_arg, JsValue) else ''
                    return JsValue("string", s.replace(old, new))
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
                sym_match_key = f"@@{SYMBOL_MATCH}@@"
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
                        result = [m.group(0)]
                        result.extend(g if g is not None else None for g in m.groups())
                        return py_to_js(result)
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
                pat = _pattern_text(args[0]) if args else ''
                m = re.search(pat, s)
                return JsValue("number", m.start() if m else -1)
            if name == 'concat':
                return JsValue("string", s + ''.join(interp._to_str(a) for a in args))
            if name == 'normalize':
                return JsValue("string", s)
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
            # Check plugin-registered methods
            plugin_key = ('string', name)
            if interp._plugin_methods and plugin_key in interp._plugin_methods:
                return interp._plugin_methods[plugin_key](this_val, args, interp)
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
                except: return JsValue('string', 'NaN')
            if name == 'toFixed':
                d = int(args[0].value) if args else 0
                try:
                    return JsValue("string", f"{n:.{d}f}")
                except: return JsValue("string", "NaN")
            if name == 'toPrecision':
                d = int(args[0].value) if args else None
                try:
                    if d is None: return JsValue("string", str(n))
                    return JsValue("string", f"{n:.{d}g}")
                except: return JsValue("string", "NaN")
            if name == 'toString':
                base = int(args[0].value) if args else 10
                if base == 10: return JsValue("string", str(int(n)) if n==int(n) else str(n))
                _fmts = {2: 'b', 8: 'o', 16: 'x'}
                _fmt = _fmts.get(base)
                if _fmt:
                    return JsValue("string", format(int(n), _fmt))
                return JsValue("string", format(int(n), f'{base}'))
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

        sym_iter_key = f"@@{SYMBOL_ITERATOR}@@"
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
            'Float32': ('f', 4), 'Float64': ('d', 8),
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
        if (a.type == 'string' or a.type == 'number') and b.type == 'object': return self._eq(a, JsValue("number", self._to_num(b)))
        if a.type == 'object' and (b.type == 'string' or b.type == 'number'): return self._eq(JsValue("number", self._to_num(a)), b)
        return False

    def _cmp(self, op, a: JsValue, b: JsValue):
        if op == '<':  return self._to_num(a) <  self._to_num(b)
        if op == '>':  return self._to_num(a) >  self._to_num(b)
        if op == '<=': return self._to_num(a) <= self._to_num(b)
        if op == '>=': return self._to_num(a) >= self._to_num(b)
        return False

    # --------------------------------------------------------- execution
    @staticmethod
    def _hoist_tdz(stmts, block_env):
        """Pre-scan a block body and create TDZ entries for let/const declarations."""
        from .environment import _TDZ_SENTINEL
        for s in stmts:
            if s.get("type") == "VariableDeclaration" and s["kind"] in ("let", "const"):
                for d in s["declarations"]:
                    name = d["id"]
                    if isinstance(name, str):
                        block_env.declare_tdz(name, s["kind"])
                    elif isinstance(name, dict) and name.get("type") == "Identifier":
                        block_env.declare_tdz(name.get("name", name), s["kind"])

    @staticmethod
    def _collect_var_names(node):
        """Recursively collect all var-declared names in a subtree, skipping nested functions."""
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
                    fn_env.bindings[name] = ('var', UNDEFINED)

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

    def _exec(self, node, env=None):
        if env is None: env = self.env
        tp = node["type"]
        _log_exec.debug("exec %s", tp)

        if tp == "Program":
            self._hoist_vars(node["body"], env)
            if self._has_use_strict(node["body"]):
                env._strict = True
            for s in node["body"]:
                r = self._exec(s, env)
                if r is not None: return r
            return None

        if tp == "VariableDeclaration":
            for d in node["declarations"]:
                val = UNDEFINED
                if d["init"]:
                    val = self._eval(d["init"], env)
                try:
                    self._bind_pattern(d["id"], val, env, node["kind"], True)
                except JSTypeError as e:
                    raise _JSError(py_to_js(str(e)))
            return None

        if tp == "FunctionDeclaration":
            fn = self._make_fn(node, env)
            env.declare(node["id"], fn, 'var')
            self._sync_global_binding(node["id"], fn, env)
            return None

        if tp == "ClassDeclaration":
            cname = node["id"]
            super_name = node.get("superClass")
            parent_class = env.get(super_name) if super_name else None
            methods = node["body"]
            proto = JsValue("object", {})
            if isinstance(parent_class, JsValue):
                parent_proto = parent_class.value.get("prototype")
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
            if not ctor:
                def _default_ctor(this_val, args, interp):
                    if parent_class:
                        interp._call_js(parent_class, args, this_val)
                    return this_val
                ctor = JsValue("intrinsic", {"fn": _default_ctor, "name": cname})
            ctor.value["prototype"] = proto
            proto.value["constructor"] = ctor
            ctor.value["superClass"] = parent_class
            ctor.value.update(static_methods)
            if isinstance(parent_class, JsValue):
                ctor.value["__proto__"] = parent_class
            # apply static fields
            for sf in static_fields:
                sf_val = self._eval(sf["value"], env) if sf.get("value") else UNDEFINED
                ctor.value[sf["key"]] = sf_val
            # store instance fields for use in NewExpression
            if instance_fields:
                ctor.value["__instance_fields__"] = instance_fields
            # run static blocks
            for sb in static_blocks:
                sb_env = Environment(env)
                sb_env._is_fn_env = True
                sb_env._is_arrow = False
                sb_env._this = ctor
                self._exec(sb["body"], sb_env)
            env.declare(cname, ctor, 'let')
            self._sync_global_binding(cname, ctor, env)
            return None

        if tp == "BlockStatement":
            block_env = Environment(env)
            self._hoist_tdz(node["body"], block_env)
            for s in node["body"]:
                r = self._exec(s, block_env)
                if r is not None: return r
            return None

        if tp == "ExpressionStatement":
            self._eval(node["expression"], env)
            return None

        if tp == "IfStatement":
            test = self._eval(node["test"], env)
            if self._truthy(test):
                return self._exec(node["consequent"], env)
            elif node.get("alternate"):
                return self._exec(node["alternate"], env)
            return None

        if tp == "WhileStatement":
            while self._truthy(self._eval(node["test"], env)):
                self._check_step_limit()
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
            return None

        if tp == "DoWhileStatement":
            while True:
                self._check_step_limit()
                try:
                    r = self._exec(node["body"], env)
                    if r is not None:
                        if r == _BREAK: break
                        if r == _CONTINUE:
                            if not self._truthy(self._eval(node["test"], env)): break
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
                if not self._truthy(self._eval(node["test"], env)): break
            return None

        if tp == "ForStatement":
            loop_env = Environment(env)
            init = node.get("init")
            uses_lex = (init is not None and
                        init.get("type") == "VariableDeclaration" and
                        init.get("kind") in ("let","const"))
            if init:
                self._exec(init, loop_env)
            # Get loop variable names for per-iteration copying
            lex_vars = []
            if uses_lex and init:
                for decl in init.get("declarations", []):
                    id_node = decl.get("id")
                    if id_node and id_node.get("type") == "Identifier":
                        lex_vars.append(id_node["name"])
            while True:
                if node.get("test") and not self._truthy(self._eval(node["test"], loop_env)):
                    break
                self._check_step_limit()
                # Create per-iteration env for let/const (closures capture this)
                if uses_lex:
                    iter_env = Environment(loop_env)
                    for v in lex_vars:
                        try:
                            iter_env.declare(v, loop_env.get(v), 'let')
                        except Exception:
                            pass
                else:
                    iter_env = loop_env
                try:
                    r = self._exec(node["body"], iter_env)
                    if r is not None:
                        if r == _BREAK: break
                        if r == _CONTINUE:
                            # Update runs in loop_env so closures from this iter keep their values
                            if uses_lex:
                                for v in lex_vars:
                                    try: loop_env.set(v, iter_env.get(v))
                                    except Exception: pass
                            if node.get("update"): self._eval(node["update"], loop_env)
                            continue
                        return r
                except _JSBreak as e:
                    _lbl = node.get('__label__')
                    if e.label is None or (_lbl and e.label == _lbl): break
                    raise
                except _JSContinue as e:
                    _lbl = node.get('__label__')
                    if e.label is None or (_lbl and e.label == _lbl):
                        if uses_lex:
                            for v in lex_vars:
                                try: loop_env.set(v, iter_env.get(v))
                                except Exception: pass
                        if node.get("update"): self._eval(node["update"], loop_env)
                        continue
                    raise
                # Update runs in loop_env so closures from this iter keep their values
                if uses_lex:
                    for v in lex_vars:
                        try: loop_env.set(v, iter_env.get(v))
                        except Exception: pass
                if node.get("update"): self._eval(node["update"], loop_env)
            return None

        if tp == "ForInStatement":
            right = self._eval(node["right"], env)
            keys = []
            seen = set()
            if right.type == "object":
                cur = right
                while isinstance(cur, JsValue) and cur.type == 'object':
                    for k in cur.value.keys():
                        if k not in seen and not k.startswith('__') and not k.startswith('@@'):
                            if self._is_enumerable(cur, k):
                                seen.add(k)
                                keys.append(k)
                            else:
                                seen.add(k)  # still skip in derived objects
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

        if tp == "ForOfStatement":
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
            iterator = self._get_js_iterator(right)
            if iterator is not None:
                while True:
                    self._check_step_limit()
                    result = iterator()
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

        if tp == "SwitchStatement":
            disc = self._eval(node["discriminant"], env)
            matched = False
            default_body = None
            try:
                for case in node["cases"]:
                    if case.get("default"):
                        default_body = case["consequent"]
                        if matched:
                            for s in case["consequent"]:
                                r = self._exec(s, env)
                                if r is not None: return r
                        continue
                    if not matched:
                        test_val = self._eval(case["test"], env)
                        if self._strict_eq(disc, test_val):
                            matched = True
                            for s in case["consequent"]:
                                r = self._exec(s, env)
                                if r is not None:
                                    if r == _BREAK: return None
                                    return r
                if not matched and default_body:
                    for s in default_body:
                        r = self._exec(s, env)
                        if r is not None:
                            if r == _BREAK: return None
                            return r
            except _JSBreak as e:
                if e.label is None: return None
                raise
            return None

        if tp == "TryStatement":
            try:
                self._exec(node["block"], env)
            except _JSError as e:
                handler = node.get("handler")
                if handler:
                    catch_env = Environment(env)
                    param = handler.get("param")
                    if param:
                        if isinstance(param, dict) and param.get("type") in ("ObjectPattern", "ArrayPattern"):
                            self._bind_pattern(param, e.value, catch_env, 'let', True)
                        else:
                            catch_env.declare(param, e.value, 'let')
                    self._exec(handler["body"], catch_env)
            except (_JSReturn, _JSBreak, _JSContinue):
                raise
            except Exception as e:
                handler = node.get("handler")
                if handler:
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
                    raise
            finally:
                if node.get("finalizer"):
                    self._exec(node["finalizer"], env)
            return None

        if tp == "BreakStatement":    raise _JSBreak(node.get('label'))
        if tp == "ContinueStatement": raise _JSContinue(node.get('label'))

        if tp == "LabeledStatement":
            label = node["label"]
            body = node["body"]
            # Inject the label into loop bodies so they can handle labeled continue/break
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

        if tp == "ReturnStatement":
            val = UNDEFINED
            if node.get("argument"):
                val = self._eval(node["argument"], env)
            raise _JSReturn(val)

        if tp == "ThrowStatement":
            val = self._eval(node["argument"], env)
            raise _JSError(val)

        if tp == "EmptyStatement":
            return None

        if tp == "ImportDeclaration":
            if getattr(self, '_module_loader', None) is not None:
                source_spec = node["source"]
                resolved = self._module_loader.resolve(source_spec, getattr(self, '_module_file', None))
                exports = self._module_loader.load(resolved)
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

        if tp == "ExportNamedDeclaration":
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
                            try:
                                self._module_exports[name] = env.get(name)
                            except ReferenceError:
                                pass
                elif decl["type"] in ("FunctionDeclaration", "ClassDeclaration"):
                    name = decl.get("id")
                    if name:
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

        if tp == "ExportDefaultDeclaration":
            if not hasattr(self, '_module_exports'):
                self._module_exports = {}
            decl = node["declaration"]
            if decl.get("type") in ("FunctionDeclaration",):
                val = self._make_fn(decl, env)
                if decl.get("id"):
                    try:
                        env.declare(decl["id"], val, 'var')
                    except Exception:
                        pass
            elif decl.get("type") == "FunctionExpression":
                val = self._make_fn(decl, env)
            elif decl.get("type") == "ClassDeclaration":
                self._exec(decl, env)
                val = env.get(decl["id"]) if decl.get("id") else UNDEFINED
            else:
                val = self._eval(decl, env)
            self._module_exports["default"] = val
            return None

        return None

    def _eval_arguments(self, arg_nodes, env):
        args = []
        for arg in arg_nodes:
            value = self._eval(arg, env)
            if arg.get("type") == "SpreadElement":
                it = self._get_js_iterator(value)
                if it is not None:
                    while True:
                        r = it()
                        done = self._get_prop(r, 'done')
                        if self._truthy(done): break
                        args.append(self._get_prop(r, 'value'))
                elif value.type == "array":
                    args.extend(value.value)
                else:
                    args.append(value)
            else:
                args.append(value)
        return args

    # --------------------------------------------------------- evaluation
    def _eval(self, node, env=None):
        if env is None: env = self.env
        tp = node["type"]
        _log_eval.debug("eval %s", tp)

        if tp == "Literal":
            kind = node.get("raw", "undefined")
            if kind == "bigint":
                return JsValue('bigint', node["value"])
            return JsValue(kind, node["value"]) if node["value"] is not None else (JS_NULL if kind=="null" else UNDEFINED)

        if tp == "RegexLiteral":
            return self._make_regexp_val(node["source"], node.get("flags", ""))

        if tp == "Identifier":
            if node["name"] == "arguments":
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
            try:
                return env.get(node["name"])
            except ReferenceError as re:
                msg = str(re)
                if "before initialization" in msg:
                    raise _JSError(self._make_js_error('ReferenceError', msg))
                return UNDEFINED

        if tp == "ThisExpression":
            e = env
            while e is not None:
                if e._is_fn_env and not e._is_arrow:
                    return e._this
                e = e.parent
            return UNDEFINED

        if tp == "ArrayExpression":
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

        if tp == "ObjectExpression":
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
            return JsValue("object", obj)

        if tp == "FunctionExpression":
            return self._make_fn(node, env)

        if tp == "UnaryExpression":
            arg = self._eval(node["argument"], env)
            op = node["operator"]
            if op == "typeof":
                try:
                    if node["argument"]["type"] == "Identifier":
                        if not env.has(node["argument"]["name"]):
                            return JsValue("string", "undefined")
                except: pass
                return JsValue("string", self._typeof(arg))
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
                        return JS_FALSE
                    self._del_prop(obj, prop)
                    return JS_TRUE
                return JS_TRUE
            return arg

        if tp == "BinaryExpression":
            op = node["operator"]
            l = self._eval(node["left"], env)
            if op in ("||","&&"):
                if op == "||":
                    if self._truthy(l): return l
                    return self._eval(node["right"], env)
                else:
                    if not self._truthy(l): return l
                    return self._eval(node["right"], env)
            r = self._eval(node["right"], env)
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
                return JsValue("number", self._to_num(l) / self._to_num(r))
            if op == "%":
                if l.type == "bigint" and r.type == "bigint":
                    return JsValue("bigint", l.value % r.value)
                return JsValue("number", self._to_num(l) % self._to_num(r))
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
                _error_ctor_names = {'Error','TypeError','RangeError','SyntaxError','ReferenceError','URIError','EvalError','AggregateError'}
                if r.type in ("function","intrinsic","class"):
                    ctor_name = r.value.get("name") if isinstance(r.value, dict) else None
                    if isinstance(ctor_name, str) and ctor_name in _error_ctor_names:
                        if l.type == 'object':
                            err_type = l.value.get('__error_type__')
                            if isinstance(err_type, JsValue) and err_type.type == 'string':
                                if ctor_name == 'Error' or err_type.value == ctor_name:
                                    return JS_TRUE
                        return JS_FALSE
                    proto = r.value.get("prototype")
                    if proto and l.type == "object":
                        lp = self._get_proto(l)
                        while lp:
                            if lp is proto: return JS_TRUE
                            lp = self._get_proto(lp) if isinstance(lp, JsValue) else None
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
                if target.type == "object":
                    # Walk prototype chain
                    cur = target
                    while cur and cur.type == 'object':
                        if key in cur.value:
                            return JS_TRUE
                        cur = self._get_proto(cur)
                    return JS_FALSE
                if target.type == "array":
                    try: return JS_TRUE if 0 <= int(key) < len(target.value) else JS_FALSE
                    except: return JS_FALSE
                if target.type == "string": return JS_TRUE if 0 <= int(key) < len(target.value) else JS_FALSE
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

        if tp == "LogicalExpression":
            l = self._eval(node["left"], env)
            op = node["operator"]
            if op == "&&":
                return self._eval(node["right"], env) if self._truthy(l) else l
            if op == "||":
                return l if self._truthy(l) else self._eval(node["right"], env)
            if op == "??":
                return l if l.type not in ("null","undefined") else self._eval(node["right"], env)
            return l

        if tp == "UpdateExpression":
            arg = node["argument"]
            prefix = node.get("prefix", True)
            op = node["operator"]
            if arg["type"] == "Identifier":
                old = env.get(arg["name"])
                new = JsValue("number", self._to_num(old) + (1 if op=="++" else -1))
                env.set(arg["name"], new)
                return old if not prefix else new
            if arg["type"] == "MemberExpression":
                obj = self._eval(arg["object"], env)
                prop = self._eval(arg["property"], env) if arg["computed"] else arg["property"]["name"]
                old = self._get_prop(obj, prop)
                new = JsValue("number", self._to_num(old) + (1 if op=="++" else -1))
                self._set_prop(obj, prop, new)
                return old if not prefix else new
            return UNDEFINED

        if tp == "AssignmentExpression":
            left = node["left"]
            op = node["operator"]
            if op == "=" and left.get("type") in ("ObjectPattern", "ArrayPattern"):
                right = self._eval(node["right"], env)
                self._bind_pattern(left, right, env, 'let', False)
                return right
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

        if tp == "ConditionalExpression":
            test = self._eval(node["test"], env)
            return self._eval(node["consequent"] if self._truthy(test) else node["alternate"], env)

        if tp == "MemberExpression":
            obj = self._eval(node["object"], env)
            if node.get("optional") and self._is_nullish(obj):
                return UNDEFINED
            if obj.type in ('null', 'undefined'):
                prop_name = node["property"].get("name", "?") if not node["computed"] else "?"
                raise _JSError(self._make_js_error('TypeError',
                    f"Cannot read properties of {obj.type} (reading '{prop_name}')"))
            prop = self._eval(node["property"], env) if node["computed"] else node["property"]["name"]
            return self._get_prop(obj, prop)

        if tp == "CallExpression":
            args = self._eval_arguments(node["arguments"], env)
            this_val = UNDEFINED
            if node["callee"]["type"] == "MemberExpression":
                obj = self._eval(node["callee"]["object"], env)
                if node["callee"].get("optional") and self._is_nullish(obj):
                    return UNDEFINED
                prop = self._eval(node["callee"]["property"], env) if node["callee"]["computed"] else node["callee"]["property"]["name"]
                callee = self._get_prop(obj, prop)
                if obj.type == 'object' and '__super_this__' in obj.value:
                    this_val = obj.value['__super_this__']
                else:
                    this_val = obj
            else:
                callee = self._eval(node["callee"], env)
            if node.get("optional") and self._is_nullish(callee):
                return UNDEFINED
            return self._call_js(callee, args, this_val)

        if tp == "NewExpression":
            callee = self._eval(node["callee"], env)
            args = self._eval_arguments(node["arguments"], env)
            if callee.type == 'proxy':
                proxy = callee.value
                trap = self._get_trap(proxy.handler, 'construct')
                if trap:
                    return self._call_js(trap, [proxy.target, JsValue('array', args), callee], UNDEFINED)
                callee = proxy.target
            if callee.type in ("function","intrinsic","class"):
                new_obj = JsValue("object", {})
                proto = callee.value.get("prototype")
                if proto and proto.type == "object":
                    new_obj.value["__proto__"] = proto
                # initialize instance fields before constructor
                instance_fields = callee.value.get("__instance_fields__", []) if isinstance(callee.value, dict) else []
                if instance_fields:
                    field_env = Environment(env)
                    field_env._is_fn_env = True
                    field_env._is_arrow = False
                    field_env._this = new_obj
                    for field in instance_fields:
                        fval = self._eval(field["value"], field_env) if field.get("value") else UNDEFINED
                        new_obj.value[field["key"]] = fval
                result = self._call_js(callee, args, new_obj, is_new_call=True)
                if isinstance(result, JsValue) and result.type in ('object', 'array', 'function', 'intrinsic', 'class', 'promise', 'proxy'):
                    return result
                return new_obj
            return py_to_js({})

        if tp == "SequenceExpression":
            result = UNDEFINED
            for expr in node["expressions"]:
                result = self._eval(expr, env)
            return result

        if tp == "TemplateLiteral":
            parts = []
            for part in node["quasis"]:
                if isinstance(part, tuple) and part[0] == "expr":
                    val = self._eval(Parser(Lexer(part[1]).tokenize()).parse()["body"][0]["expression"], env)
                    parts.append(self._to_str(self._to_primitive(val, 'string')))
                else:
                    parts.append(str(part))
            return JsValue("string", "".join(parts))

        if tp == "TaggedTemplateExpression":
            tag_fn = self._eval(node["tag"], env)
            quasis = node["quasi"]["quasis"]
            strs = []
            vals = []
            current_str_parts = []
            for part in quasis:
                if isinstance(part, tuple) and part[0] == "expr":
                    strs.append(JsValue("string", "".join(current_str_parts)))
                    current_str_parts = []
                    vals.append(self._eval(Parser(Lexer(part[1]).tokenize()).parse()["body"][0]["expression"], env))
                else:
                    current_str_parts.append(str(part))
            strs.append(JsValue("string", "".join(current_str_parts)))
            strings_arr = JsValue("array", strs)
            strings_arr.extras = {"raw": JsValue("array", list(strs))}
            return self._call_js(tag_fn, [strings_arr] + vals, UNDEFINED)

        if tp == "AwaitExpression":
            awaited = self._eval(node["argument"], env)
            if awaited.type != 'promise':
                return awaited
            if not self._run_event_loop(awaited):
                raise _JSError(py_to_js('Awaited promise did not settle'))
            if awaited.value['state'] == 'rejected':
                raise _JSError(awaited.value['value'])
            return awaited.value['value']

        if tp == "SpreadElement":
            return self._eval(node["argument"], env)

        if tp == "MetaProperty":
            if node["meta"] == "new" and node["property"] == "target":
                e = env
                while e is not None:
                    if '__new_target__' in e.bindings:
                        return e.bindings['__new_target__'][1]
                    e = e.parent
                return UNDEFINED

        if tp == "ImportMeta":
            meta = JsValue('object', {})
            url = self._module_url or (
                f"file://{self._module_file}" if self._module_file else 'file:///unknown'
            )
            meta.value['url'] = py_to_js(url)
            return meta

        if tp == "YieldExpression":
            arg = UNDEFINED
            if node.get('argument'):
                arg = self._eval(node['argument'], env)
            gen = self._find_generator(env)
            if gen is None:
                raise _JSError(py_to_js('yield used outside generator'))
            if node.get('delegate'):
                # yield* — delegate to inner iterable, yielding each value
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
                return gen.yield_value(arg)

        if tp == "DynamicImport":
            src = self._eval(node['source'], env) if node.get('source') else py_to_js('')
            if self._module_loader is not None:
                try:
                    mod_path = self._to_str(src)
                    ns = self._module_loader.load(mod_path, self._module_file)
                    return self._resolved_promise(ns)
                except Exception as e:
                    return self._rejected_promise(self._make_js_error('Error', str(e)))
            return self._resolved_promise(JsValue('object', {}))

        return UNDEFINED

    def _do_assign_op(self, op, old, val):
        if op == "+=":
            if old.type == "string" or val.type == "string":
                return JsValue("string", self._to_str(old) + self._to_str(val))
            return JsValue("number", self._to_num(old) + self._to_num(val))
        if op == "-=":  return JsValue("number", self._to_num(old) - self._to_num(val))
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
        return JsValue("function", {"node": node, "env": closure_env, "name": node.get("id") or ""})

    def _add_iterator_helpers(self, iter_obj):
        """Add ES2025 iterator helper methods to an iterator object in-place."""

        def _make_intr(fn, name):
            return self._make_intrinsic(lambda tv, args, interp: fn(args, interp), name)

        def _iter_to_list(it_obj):
            items = []
            while True:
                r = self._call_js(it_obj.value['next'], [], it_obj)
                if r.type == 'object' and r.value.get('done', JS_FALSE).value is True:
                    break
                val = r.value.get('value', UNDEFINED) if r.type == 'object' else UNDEFINED
                items.append(val)
            return items

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
            items = _iter_to_list(iter_obj)
            mapped = [self._call_js(fn, [el, py_to_js(i)], None) for i, el in enumerate(items)]
            return _make_list_iter(mapped)

        def _filter(args, interp):
            fn = args[0] if args else UNDEFINED
            items = _iter_to_list(iter_obj)
            filtered = [el for i, el in enumerate(items)
                        if self._truthy(self._call_js(fn, [el, py_to_js(i)], None))]
            return _make_list_iter(filtered)

        def _take(args, interp):
            n = int(self._to_num(args[0])) if args else 0
            items = _iter_to_list(iter_obj)
            return _make_list_iter(items[:n])

        def _drop(args, interp):
            n = int(self._to_num(args[0])) if args else 0
            items = _iter_to_list(iter_obj)
            return _make_list_iter(items[n:])

        def _flat_map(args, interp):
            fn = args[0] if args else UNDEFINED
            items = _iter_to_list(iter_obj)
            result = []
            for i, el in enumerate(items):
                mapped = self._call_js(fn, [el, py_to_js(i)], None)
                sub_it = self._get_js_iterator(mapped)
                if sub_it is not None:
                    while True:
                        r = sub_it()
                        if r.type == 'object' and r.value.get('done', JS_FALSE).value is True:
                            break
                        result.append(r.value.get('value', UNDEFINED) if r.type == 'object' else r)
                else:
                    result.append(mapped)
            return _make_list_iter(result)

        def _to_array(args, interp):
            return JsValue('array', _iter_to_list(iter_obj))

        def _for_each(args, interp):
            fn = args[0] if args else UNDEFINED
            for i, el in enumerate(_iter_to_list(iter_obj)):
                self._call_js(fn, [el, py_to_js(i)], None)
            return UNDEFINED

        def _some(args, interp):
            fn = args[0] if args else UNDEFINED
            for i, el in enumerate(_iter_to_list(iter_obj)):
                if self._truthy(self._call_js(fn, [el, py_to_js(i)], None)):
                    return JS_TRUE
            return JS_FALSE

        def _every(args, interp):
            fn = args[0] if args else UNDEFINED
            for i, el in enumerate(_iter_to_list(iter_obj)):
                if not self._truthy(self._call_js(fn, [el, py_to_js(i)], None)):
                    return JS_FALSE
            return JS_TRUE

        def _find(args, interp):
            fn = args[0] if args else UNDEFINED
            for i, el in enumerate(_iter_to_list(iter_obj)):
                if self._truthy(self._call_js(fn, [el, py_to_js(i)], None)):
                    return el
            return UNDEFINED

        def _reduce(args, interp):
            fn = args[0] if args else UNDEFINED
            items = _iter_to_list(iter_obj)
            if not items:
                return args[1] if len(args) > 1 else UNDEFINED
            acc = args[1] if len(args) > 1 else items[0]
            start = 0 if len(args) > 1 else 1
            for el in items[start:]:
                acc = self._call_js(fn, [acc, el], None)
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

        sym_iter_key = f"@@{SYMBOL_ITERATOR}@@"
        if sym_iter_key not in iter_obj.value:
            iter_obj.value[sym_iter_key] = self._make_intrinsic(
                lambda tv, a, i: iter_obj, '[Symbol.iterator]')

        return iter_obj

    def _make_generator_obj(self, fn_val, args):
        gen = JsGenerator(fn_val, args, self)
        sym_iter_key = f"@@{SYMBOL_ITERATOR}@@"
        gen_obj = JsValue('object', {
            '__kind__': JsValue('string', 'Generator'),
            '__gen__': gen,
            'next':   self._make_intrinsic(lambda tv, a, i: gen.next(a[0] if a else UNDEFINED), 'Generator.next'),
            'return': self._make_intrinsic(lambda tv, a, i: gen.js_return(a[0] if a else UNDEFINED), 'Generator.return'),
            'throw':  self._make_intrinsic(lambda tv, a, i: gen.js_throw(a[0] if a else UNDEFINED), 'Generator.throw'),
        })
        gen_obj.value[sym_iter_key] = self._make_intrinsic(lambda tv, a, i: gen_obj, '[Symbol.iterator]')
        self._add_iterator_helpers(gen_obj)
        return gen_obj

    def _make_async_generator_obj(self, fn_val, args):
        gen = JsAsyncGenerator(fn_val, args, self)
        sym_async_iter_key = f"@@{SYMBOL_ASYNC_ITERATOR}@@"
        gen_obj = JsValue('object', {
            '__kind__': JsValue('string', 'AsyncGenerator'),
            '__gen__': gen,
            'next':   self._make_intrinsic(lambda tv, a, i: gen.next(a[0] if a else UNDEFINED), 'AsyncGenerator.next'),
            'return': self._make_intrinsic(lambda tv, a, i: gen.js_return(a[0] if a else UNDEFINED), 'AsyncGenerator.return'),
            'throw':  self._make_intrinsic(lambda tv, a, i: gen.js_throw(a[0] if a else UNDEFINED), 'AsyncGenerator.throw'),
        })
        gen_obj.value[sym_async_iter_key] = self._make_intrinsic(lambda tv, a, i: gen_obj, '[Symbol.asyncIterator]')
        return gen_obj

    def _call_js(self, fn_val, args, this_val=None, extra_args=None, is_new_call=False):
        _log_call.debug("call %s (new=%s, nargs=%d)", fn_val.type, is_new_call, len(args))
        self._call_depth += 1
        if self._call_depth > self.MAX_CALL_DEPTH:
            self._call_depth -= 1
            raise _JSError(self._make_js_error('RangeError', 'Maximum call stack size exceeded'))
        try:
            return self._call_js_impl(fn_val, args, this_val, extra_args, is_new_call)
        finally:
            self._call_depth -= 1

    def _call_js_impl(self, fn_val, args, this_val=None, extra_args=None, is_new_call=False):
        if fn_val.type == 'proxy':
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
        if fn_val.type == "intrinsic":
            info = fn_val.value
            return info["fn"](this_val, args, self)
        if fn_val.type == "function":
            info = fn_val.value
            node = info["node"]
            # Generator function — return generator object immediately without running body
            if node.get("generator_"):
                if node.get("async_"):
                    return self._make_async_generator_obj(fn_val, args)
                return self._make_generator_obj(fn_val, args)
            env = info["env"]
            call_env = Environment(env)
            call_env._this = this_val if this_val is not None else UNDEFINED
            call_env._is_arrow = bool(node.get("arrow"))
            call_env._is_fn_env = True
            if not node.get("arrow"):
                call_env._fn_args = list(args)
                call_env._fn_val = fn_val
            if is_new_call:
                call_env.declare('__new_target__', fn_val, 'const')
            super_proto = info.get('super_proto')
            if isinstance(super_proto, JsValue):
                call_env.declare('super', self._make_super_proxy(super_proto, call_env._this), 'const')
            params = node.get("params", [])
            arg_index = 0
            for p in params:
                if isinstance(p, dict) and p.get("type") == "RestElement":
                    rest = args[arg_index:] if arg_index < len(args) else []
                    self._bind_pattern(p["argument"], JsValue("array", list(rest)), call_env, 'var', True)
                    break
                val = args[arg_index] if arg_index < len(args) else UNDEFINED
                self._bind_pattern(p, val, call_env, 'var', True)
                arg_index += 1
            promise = self._new_promise() if node.get("async_") else None
            body = node["body"]
            # Hoist var declarations to function scope
            body_stmts = body.get("body", []) if isinstance(body, dict) and body.get("type") == "BlockStatement" else []
            if body_stmts:
                self._hoist_vars(body_stmts, call_env)
                if self._has_use_strict(body_stmts):
                    call_env._strict = True
            try:
                self.env = call_env
                self._exec(node["body"], call_env)
                result = UNDEFINED
            except _JSReturn as e:
                result = e.value
            except _JSError as exc:
                if promise is not None:
                    return self._reject_promise(promise, exc.value)
                raise
            except Exception as exc:
                if promise is not None:
                    return self._reject_promise(promise, py_to_js(str(exc)))
                raise
            finally:
                self.env = env
            if promise is not None:
                return self._resolve_promise(promise, result)
            return result
        if fn_val.type == "class":
            # class constructor call
            return fn_val
        raise _JSError(self._make_js_error('TypeError', f"{self._to_str(fn_val)} is not a function"))

    # --------------------------------------------------------- main run
    def run(self, source: str) -> str:
        self._exec_steps = 0
        start = len(self.output)
        try:
            tokens = Lexer(source).tokenize()
            ast = Parser(tokens).parse()
            self._exec(ast, self.genv)
            self._run_event_loop()
        except _JSReturn:
            pass
        except _JSError as e:
            self.output.append(f"Error: {self._to_str(e.value)}")
        except Exception as e:
            self.output.append(f"Python Error: {e}")
        return '\n'.join(self.output[start:])

    def run_module(self, source: str) -> None:
        """Execute source as a module (supports import/export statements)."""
        tokens = Lexer(source).tokenize()
        ast = Parser(tokens).parse()
        self._exec(ast, self.genv)
        self._run_event_loop()



def _ta_coerce(jsv, fmt, interp):
    """Coerce a JsValue to the appropriate Python type for struct.pack_into."""
    if fmt in ('f', 'd'):
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
