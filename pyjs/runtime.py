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
from .lexer import Lexer
from .parser import N, Parser


def _js_regex_to_python(pattern: str) -> str:
    """Convert JS regex named-group syntax to Python re syntax."""
    result = re.sub(r'\(\?<([^>]+)>', r'(?P<\1>', pattern)
    result = re.sub(r'\\k<([^>]+)>', r'(?P=\1)', result)
    return result

class JsProxy:
    """Wraps a JsValue so that property access goes through handler traps."""
    __slots__ = ('target', 'handler')
    def __init__(self, target: 'JsValue', handler: 'JsValue'):
        self.target = target
        self.handler = handler


class JsValue:
    __slots__ = ('type', 'value', 'extras')
    def __init__(self, tp: str, val: Any):
        self.type = tp; self.value = val; self.extras = None
    def __repr__(self):
        if self.type == 'null': return 'null'
        if self.type == 'undefined': return 'undefined'
        return f"JsValue({self.type}, {self.value!r})"

UNDEFINED = JsValue("undefined", None)
JS_NULL   = JsValue("null", None)
JS_TRUE   = JsValue("boolean", True)
JS_FALSE  = JsValue("boolean", False)

# Well-known Symbol IDs (fixed)
SYMBOL_ITERATOR           = 1
SYMBOL_TO_PRIMITIVE       = 2
SYMBOL_HAS_INSTANCE       = 3
SYMBOL_TO_STRING_TAG      = 4
SYMBOL_ASYNC_ITERATOR     = 5
SYMBOL_SPECIES            = 6
SYMBOL_MATCH              = 7
SYMBOL_REPLACE            = 8
SYMBOL_SPLIT              = 9
SYMBOL_SEARCH             = 10
SYMBOL_IS_CONCAT_SPREADABLE = 11

_symbol_id_counter = [11]  # incremented for each new Symbol()
_symbol_registry   = {}    # for Symbol.for()

# ============================================================================
#  Environment
# ============================================================================

class Environment:
    __slots__ = ('parent', 'bindings', '_this', '_fn_args', '_is_arrow', '_is_fn_env', '_generator', '_fn_val')

    def __init__(self, parent: Optional['Environment'] = None):
        self.parent = parent
        self.bindings: Dict[str, Any] = {}       # name -> (keyword, JsValue)
        self._this = UNDEFINED
        self._fn_args: List[JsValue] = []
        self._is_arrow: bool = False
        self._is_fn_env: bool = False
        self._generator = None
        self._fn_val = None

    def declare(self, name, value, keyword='var'):
        if keyword == 'const':
            if name in self.bindings:
                raise JSTypeError(f"Identifier '{name}' has already been declared")
            self.bindings[name] = ('const', value)
        elif keyword == 'let':
            if name in self.bindings:
                raise JSTypeError(f"Identifier '{name}' has already been declared")
            self.bindings[name] = ('let', value)
        else:  # var
            self.bindings[name] = ('var', value)

    def has(self, name):
        if name in self.bindings: return True
        return self.parent.has(name) if self.parent else False

    def _find(self, name):
        e = self
        while e:
            if name in e.bindings: return e
            e = e.parent
        return None

    def get(self, name):
        e = self._find(name)
        if not e:
            raise ReferenceError(f"{name} is not defined")
        return e.bindings[name][1]

    def set(self, name, value):
        e = self._find(name)
        if not e:
            raise ReferenceError(f"{name} is not defined")
        if e.bindings[name][0] == 'const':
            raise JSTypeError(f"Assignment to constant variable '{name}'")
        e.bindings[name] = (e.bindings[name][0], value)

    def set_own(self, name, value):
        if name not in self.bindings:
            raise ReferenceError(f"{name} is not defined")
        if self.bindings[name][0] == 'const':
            raise JSTypeError(f"Assignment to constant variable '{name}'")
        self.bindings[name] = (self.bindings[name][0], value)


# ============================================================================
#  JS Generator (thread-based coroutine)
# ============================================================================

class JsGenerator:
    """JS generator object backed by a Python thread."""

    def __init__(self, fn_val, args, interp):
        self._fn_val = fn_val
        self._args = args
        self._interp = interp
        self._done = False
        self._to_gen   = _queue_mod.Queue()
        self._from_gen = _queue_mod.Queue()
        self._thread = threading.Thread(target=self._body, daemon=True)
        self._thread.start()

    def _body(self):
        interp = self._interp
        fn_val = self._fn_val
        info   = fn_val.value
        node   = info['node']
        env    = info['env']

        # Wait for the first next() call before running the body
        msg = self._to_gen.get()
        if msg['type'] != 'next':
            self._from_gen.put({'type': 'return', 'value': UNDEFINED})
            return

        call_env = Environment(env)
        call_env._this     = UNDEFINED
        call_env._fn_args  = list(self._args)
        call_env._is_fn_env = True
        call_env._generator = self

        super_proto = info.get('super_proto')
        if isinstance(super_proto, JsValue):
            call_env.declare('super', interp._make_super_proxy(super_proto, call_env._this), 'const')

        params = node.get('params', [])
        for i, p in enumerate(params):
            if isinstance(p, dict) and p.get('type') == 'RestElement':
                interp._bind_pattern(p['argument'], JsValue('array', list(self._args[i:])), call_env, 'var', True)
                break
            interp._bind_pattern(p, self._args[i] if i < len(self._args) else UNDEFINED, call_env, 'var', True)

        old_env = interp.env
        try:
            interp._exec(node['body'], call_env)
            self._from_gen.put({'type': 'return', 'value': UNDEFINED})
        except _JSReturn as e:
            self._from_gen.put({'type': 'return', 'value': e.value})
        except _JSError as e:
            self._from_gen.put({'type': 'throw', 'value': e.value})
        except Exception as e:
            self._from_gen.put({'type': 'throw', 'value': py_to_js(str(e))})
        finally:
            interp.env = old_env

    def yield_value(self, value):
        """Called from the generator thread when a yield is encountered."""
        self._from_gen.put({'type': 'yield', 'value': value})
        msg = self._to_gen.get()
        if msg['type'] == 'next':
            return msg.get('value', UNDEFINED)
        elif msg['type'] == 'return':
            raise _JSReturn(msg.get('value', UNDEFINED))
        elif msg['type'] == 'throw':
            raise _JSError(msg.get('value', UNDEFINED))
        return UNDEFINED

    def next(self, value=None):
        if value is None:
            value = UNDEFINED
        if self._done:
            return JsValue('object', {'value': UNDEFINED, 'done': JS_TRUE})
        self._to_gen.put({'type': 'next', 'value': value})
        msg = self._from_gen.get()
        return self._handle_msg(msg)

    def js_return(self, value=None):
        if value is None:
            value = UNDEFINED
        if self._done:
            return JsValue('object', {'value': value, 'done': JS_TRUE})
        self._done = True
        self._to_gen.put({'type': 'return', 'value': value})
        try:
            self._from_gen.get(timeout=2.0)
        except _queue_mod.Empty:
            pass
        return JsValue('object', {'value': value, 'done': JS_TRUE})

    def js_throw(self, error):
        if self._done:
            raise _JSError(error)
        self._to_gen.put({'type': 'throw', 'value': error})
        msg = self._from_gen.get()
        return self._handle_msg(msg)

    def _handle_msg(self, msg):
        if msg['type'] == 'yield':
            return JsValue('object', {'value': msg['value'], 'done': JS_FALSE})
        elif msg['type'] == 'return':
            self._done = True
            return JsValue('object', {'value': msg['value'], 'done': JS_TRUE})
        elif msg['type'] == 'throw':
            self._done = True
            raise _JSError(msg['value'])
        self._done = True
        return JsValue('object', {'value': UNDEFINED, 'done': JS_TRUE})


class JsAsyncGenerator(JsGenerator):
    """Async generator: next() returns a Promise instead of {value, done} directly."""

    def next(self, value=None):
        try:
            sync_result = super().next(value)
            return self._interp._resolved_promise(sync_result)
        except _JSError as e:
            return self._interp._rejected_promise(e.value)

    def js_return(self, value=None):
        try:
            sync_result = super().js_return(value)
            return self._interp._resolved_promise(sync_result)
        except _JSError as e:
            return self._interp._rejected_promise(e.value)

    def js_throw(self, error):
        try:
            sync_result = super().js_throw(error)
            return self._interp._resolved_promise(sync_result)
        except _JSError as e:
            return self._interp._rejected_promise(e.value)


# ============================================================================
#  Interpreter
# ============================================================================

class Interpreter:
    ARRAY_METHODS = frozenset({'push', 'pop', 'shift', 'unshift', 'indexOf', 'includes', 'join', 'slice', 'splice', 'concat', 'reverse', 'sort', 'forEach', 'map', 'filter', 'reduce', 'find', 'flat', 'flatMap', 'every', 'some', 'fill', 'copyWithin', 'toString', 'at', 'findIndex', 'findLast', 'findLastIndex', 'reduceRight', 'lastIndexOf', 'toSorted', 'toReversed', 'toSpliced', 'with'})
    STRING_METHODS = frozenset({'charAt', 'charCodeAt', 'indexOf', 'includes', 'slice', 'substring', 'toLowerCase', 'toUpperCase', 'trim', 'split', 'replace', 'replaceAll', 'startsWith', 'endsWith', 'padStart', 'padEnd', 'repeat', 'match', 'search', 'concat', 'lastIndexOf', 'normalize', 'at', 'matchAll', 'trimStart', 'trimLeft', 'trimEnd', 'trimRight', 'codePointAt'})
    NUMBER_METHODS = frozenset({'toFixed', 'toPrecision', 'toString', 'toLocaleString', 'valueOf', 'toExponential'})
    PROMISE_METHODS = frozenset({'then', 'catch', 'finally'})
    EVENT_LOOP_LIMIT = 10000

    def __init__(self):
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
        self.genv = self._global_env()
        self.env  = self.genv
        self._module_exports: dict = {}
        self._module_loader = None
        self._module_file: str | None = None
        self._module_url: str | None = None


    def _make_intrinsic(self, fn, name='?'):
        def wrapper(this_val, args, interp):
            try:
                return fn(this_val, args, interp)
            except (_JSReturn, _JSError):
                raise
            except Exception as exc:
                raise _JSError(py_to_js(str(exc)))
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
        if promise.value['state'] != 'pending':
            return promise
        if value is promise:
            return self._reject_promise(promise, py_to_js('Chaining cycle detected for promise'))
        if isinstance(value, JsValue) and value.type == 'promise':
            self._chain_promise(value, promise)
            return promise
        return self._settle_promise(promise, 'fulfilled', value)

    def _reject_promise(self, promise, value):
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

        # -- helper to wrap a Python fn as an intrinsic --
        def intr(fn, name='?'):
            return self._make_intrinsic(lambda this_val, args, interp: fn(args, interp), name)

        # -- parseInt / parseFloat --
        def _parseInt(args, interp):
            s = interp._to_str(args[0]) if args else 'undefined'
            base = int(args[1].value) if len(args)>1 and args[1].type=='number' else 10
            try:
                if base == 0:
                    s2 = s.lstrip()
                    if s2.startswith(('0x','0X')): base=16
                    elif s2.startswith(('0o','0O')): base=8
                    elif s2.startswith(('0b','0B')): base=2
                    else: base=10
                return JsValue("number", int(s, base))
            except: return JsValue("number", float('nan'))

        def _parseFloat(args, interp):
            s = interp._to_str(args[0]) if args else 'undefined'
            try:
                m = re.match(r'^\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?', s)
                return JsValue("number", float(m.group()) if m else float('nan'))
            except: return JsValue("number", float('nan'))

        g.declare('parseInt',   intr(_parseInt, 'parseInt'),   'var')
        g.declare('parseFloat', intr(_parseFloat, 'parseFloat'),'var')
        g.declare('isNaN',      intr(lambda a,i: JS_TRUE if a and i._to_num(a[0])!=i._to_num(a[0]) else JS_FALSE, 'isNaN'), 'var')
        g.declare('isFinite',   intr(lambda a,i: py_to_js(bool(a and a[0].type=='number' and math.isfinite(a[0].value))), 'isFinite'), 'var')
        import urllib.parse as _urlparse
        _URI_SAFE = "-_.!~*'();/?:@&=+$,#"
        _URI_COMPONENT_SAFE = "-_.!~*'()"
        def _encode_uri(args, interp):
            s = interp._to_str(args[0]) if args else 'undefined'
            return JsValue('string', _urlparse.quote(s, safe=_URI_SAFE))
        def _decode_uri(args, interp):
            s = interp._to_str(args[0]) if args else 'undefined'
            return JsValue('string', _urlparse.unquote(s))
        def _encode_uri_component(args, interp):
            s = interp._to_str(args[0]) if args else 'undefined'
            return JsValue('string', _urlparse.quote(s, safe=_URI_COMPONENT_SAFE))
        def _decode_uri_component(args, interp):
            s = interp._to_str(args[0]) if args else 'undefined'
            return JsValue('string', _urlparse.unquote(s))
        g.declare('encodeURI',  intr(_encode_uri, 'encodeURI'), 'var')
        g.declare('decodeURI',  intr(_decode_uri, 'decodeURI'), 'var')
        g.declare('encodeURIComponent', intr(_encode_uri_component, 'encodeURIComponent'), 'var')
        g.declare('decodeURIComponent', intr(_decode_uri_component, 'decodeURIComponent'), 'var')

        # -- atob / btoa --
        import base64 as _b64
        def _btoa(args, interp):
            s = interp._to_str(args[0]) if args else ''
            try:
                return JsValue('string', _b64.b64encode(s.encode('latin-1')).decode('ascii'))
            except (UnicodeEncodeError, ValueError):
                raise _JSError(py_to_js("InvalidCharacterError: String contains characters outside of the Latin1 range"))
        def _atob(args, interp):
            s = interp._to_str(args[0]) if args else ''
            try:
                return JsValue('string', _b64.b64decode(s).decode('latin-1'))
            except Exception:
                raise _JSError(py_to_js("InvalidCharacterError: Invalid base64 string"))
        g.declare('btoa', intr(_btoa, 'btoa'), 'var')
        g.declare('atob', intr(_atob, 'atob'), 'var')

        # -- console --
        console = JsValue("object", {})
        def _log(args, interp):
            parts = [interp._to_str(a) for a in args]
            line = ' '.join(parts)
            indent = '  ' * interp._console_indent
            interp.output.append(indent + line)
            print(indent + line)
        def _make_log_method(fn_name):
            return intr(lambda a,i: _log(a,i), fn_name)
        console.value['log']   = _make_log_method('log')
        console.value['error'] = _make_log_method('error')
        console.value['warn']  = _make_log_method('warn')
        console.value['info']  = _make_log_method('info')
        console.value['table'] = _make_log_method('table')
        def _assert(args, interp):
            if not args or not interp._truthy(args[0]):
                msgs = [interp._to_str(a) for a in args[1:]] if len(args) > 1 else []
                msg = 'Assertion failed: ' + ' '.join(msgs) if msgs else 'Assertion failed'
                indent = '  ' * interp._console_indent
                interp.output.append(indent + msg)
                print(indent + msg)
        console.value['assert'] = intr(_assert, 'assert')
        def _dir(args, interp):
            if args:
                v = args[0]
                if v.type == 'object':
                    print(json.dumps({k: interp._to_str(val) for k,val in v.value.items()}, indent=2))
                else:
                    print(interp._to_str(v))
        console.value['dir'] = intr(_dir, 'dir')
        def _count(args, interp):
            label = interp._to_str(args[0]) if args else 'default'
            interp._console_counts[label] = interp._console_counts.get(label, 0) + 1
            line = f"{label}: {interp._console_counts[label]}"
            indent = '  ' * interp._console_indent
            interp.output.append(indent + line)
            print(indent + line)
        console.value['count'] = intr(_count, 'count')
        def _count_reset(args, interp):
            label = interp._to_str(args[0]) if args else 'default'
            interp._console_counts[label] = 0
        console.value['countReset'] = intr(_count_reset, 'countReset')
        def _time(args, interp):
            label = interp._to_str(args[0]) if args else 'default'
            interp._console_timers[label] = time.time()
        console.value['time'] = intr(_time, 'time')
        def _time_end(args, interp):
            label = interp._to_str(args[0]) if args else 'default'
            start = interp._console_timers.pop(label, None)
            elapsed = (time.time() - start) * 1000 if start is not None else 0.0
            line = f"{label}: {elapsed:.3f}ms"
            indent = '  ' * interp._console_indent
            interp.output.append(indent + line)
            print(indent + line)
        console.value['timeEnd'] = intr(_time_end, 'timeEnd')
        def _time_log(args, interp):
            label = interp._to_str(args[0]) if args else 'default'
            start = interp._console_timers.get(label)
            elapsed = (time.time() - start) * 1000 if start is not None else 0.0
            line = f"{label}: {elapsed:.3f}ms"
            indent = '  ' * interp._console_indent
            interp.output.append(indent + line)
            print(indent + line)
        console.value['timeLog'] = intr(_time_log, 'timeLog')
        def _group(args, interp):
            label = interp._to_str(args[0]) if args else ''
            if label:
                indent = '  ' * interp._console_indent
                interp.output.append(indent + label)
                print(indent + label)
            interp._console_indent += 1
        console.value['group'] = intr(_group, 'group')
        console.value['groupCollapsed'] = intr(_group, 'groupCollapsed')
        def _group_end(args, interp):
            if interp._console_indent > 0:
                interp._console_indent -= 1
        console.value['groupEnd'] = intr(_group_end, 'groupEnd')
        def _trace(args, interp):
            parts = [interp._to_str(a) for a in args]
            line = 'Trace: ' + ' '.join(parts) if parts else 'Trace'
            indent = '  ' * interp._console_indent
            interp.output.append(indent + line)
            print(indent + line)
        console.value['trace'] = intr(_trace, 'trace')
        console.value['clear'] = intr(lambda a, i: UNDEFINED, 'clear')
        g.declare('console', console, 'var')

        # -- Math --
        math_obj = JsValue("object", {})
        for cn, cv in [('PI',math.pi),('E',math.e),('LN2',math.log(2)),
                       ('LN10',math.log(10)),('LOG2E',math.log2(math.e)),
                       ('LOG10E',math.log10(math.e)),('SQRT1_2',math.sqrt(0.5)),
                       ('SQRT2',math.sqrt(2))]:
            math_obj.value[cn] = JsValue("number", cv)
        for fn_name, py_fn in [
            ('abs',abs),('ceil',math.ceil),('floor',math.floor),('round',round),
            ('sqrt',math.sqrt),('log',math.log),('log2',math.log2),
            ('log10',math.log10),('exp',math.exp),
            ('sin',math.sin),('cos',math.cos),('tan',math.tan),
            ('asin',math.asin),('acos',math.acos),('atan',math.atan),
            ('sinh',math.sinh),('cosh',math.cosh),('tanh',math.tanh),
        ]:
            _f = py_fn
            math_obj.value[fn_name] = intr(lambda a,i,f=_f: JsValue("number", f(*[i._to_num(x) for x in a])), fn_name)
        math_obj.value['atan2'] = intr(lambda a,i: JsValue("number", math.atan2(i._to_num(a[0]),i._to_num(a[1]))), 'atan2')
        math_obj.value['pow']   = intr(lambda a,i: JsValue("number", i._to_num(a[0])**i._to_num(a[1])), 'pow')
        math_obj.value['min']   = intr(lambda a,i: JsValue("number", min(i._to_num(x) for x in a) if a else float('inf')), 'min')
        math_obj.value['max']   = intr(lambda a,i: JsValue("number", max(i._to_num(x) for x in a) if a else float('-inf')), 'max')
        math_obj.value['random']= intr(lambda a,i: JsValue("number", random.random()), 'random')
        math_obj.value['sign']  = intr(lambda a,i: JsValue("number", (lambda n: 1 if n>0 else -1 if n<0 else 0)(i._to_num(a[0]))), 'sign')
        math_obj.value['trunc'] = intr(lambda a,i: JsValue("number", math.trunc(i._to_num(a[0]))), 'trunc')
        math_obj.value['imul']  = intr(lambda a,i: JsValue("number", (int(i._to_num(a[0]))*int(i._to_num(a[1])))&0xFFFFFFFF), 'imul')
        math_obj.value['clz32'] = intr(lambda a,i: JsValue("number", (32-(int(i._to_num(a[0]))&0xFFFFFFFF).bit_length()) if int(i._to_num(a[0]))&0xFFFFFFFF else 32), 'clz32')
        math_obj.value['fround']= intr(lambda a,i: JsValue("number", float(struct.pack('f',i._to_num(a[0])))), 'fround') if False else intr(lambda a,i: JsValue("number", i._to_num(a[0])), 'fround')
        math_obj.value['hypot'] = intr(lambda a, i: JsValue("number", math.hypot(*[i._to_num(x) for x in a])), 'hypot')
        def _math_cbrt(args, interp):
            x = interp._to_num(args[0]) if args else 0
            if x < 0:
                return JsValue("number", -((-x) ** (1.0/3.0)))
            return JsValue("number", x ** (1.0/3.0))
        math_obj.value['cbrt'] = intr(_math_cbrt, 'cbrt')
        g.declare('Math', math_obj, 'var')

        # -- JSON --
        json_obj = JsValue("object", {})
        def _stringify(args, interp):
            if not args or args[0].type in ('null', 'undefined'):
                return JS_NULL
            val = args[0]
            replacer = args[1] if len(args) > 1 else UNDEFINED
            space_arg = args[2] if len(args) > 2 else UNDEFINED

            # Determine indent
            indent = None
            if space_arg.type == 'number':
                n = int(interp._to_num(space_arg))
                if n > 0:
                    indent = n
            elif space_arg.type == 'string' and space_arg.value:
                indent = space_arg.value

            # Build allowed keys set from array replacer
            allowed_keys = None
            if replacer.type == 'array':
                allowed_keys = [interp._to_str(k) for k in replacer.value]

            _OMIT = object()

            def _convert(key, v):
                # Check for toJSON method
                if v.type in ('object', 'array') and isinstance(v.value, (dict, list)):
                    to_json = v.value.get('toJSON') if isinstance(v.value, dict) else None
                    if to_json and isinstance(to_json, JsValue) and to_json.type in ('function', 'arrow', 'intrinsic'):
                        v = interp._call_js(to_json, [py_to_js(key)], v)
                # Apply function replacer if provided
                if interp._is_callable(replacer):
                    v = interp._call_js(replacer, [py_to_js(key), v], UNDEFINED)
                if v.type == 'undefined':
                    return _OMIT
                if v.type in ('null',):
                    return None
                if v.type == 'boolean':
                    return v.value
                if v.type == 'number':
                    n = v.value
                    if math.isnan(n) or math.isinf(n):
                        return None
                    if n == int(n) and abs(n) < 1e15:
                        return int(n)
                    return n
                if v.type == 'string':
                    return v.value
                if v.type == 'array':
                    return [_convert(str(i), x) for i, x in enumerate(v.value)]
                if v.type == 'object':
                    keys = [k for k in v.value.keys()
                            if not k.startswith('__') and not (k.startswith('@@') and k.endswith('@@'))]
                    if allowed_keys is not None and not interp._is_callable(replacer):
                        keys = [k for k in allowed_keys if k in v.value]
                    result = {}
                    for k in keys:
                        pv = _convert(k, v.value[k])
                        if pv is not _OMIT:
                            result[k] = pv
                    return result
                return None

            py_val = _convert('', val)
            if py_val is _OMIT:
                return UNDEFINED
            if indent is None:
                return JsValue("string", json.dumps(py_val, separators=(',', ':'), default=str))
            return JsValue("string", json.dumps(py_val, indent=indent, default=str))

        def _parse(args, interp):
            if not args:
                raise _JSError(py_to_js('JSON.parse requires argument'))
            parsed = interp._from_py(json.loads(args[0].value))
            reviver = args[1] if len(args) > 1 else UNDEFINED
            if not interp._is_callable(reviver):
                return parsed
            def _walk(key, v):
                if v.type == 'object':
                    for k in list(v.value.keys()):
                        new_val = _walk(k, v.value[k])
                        if new_val.type == 'undefined':
                            del v.value[k]
                        else:
                            v.value[k] = new_val
                elif v.type == 'array':
                    for i in range(len(v.value)):
                        new_val = _walk(str(i), v.value[i])
                        v.value[i] = new_val
                return interp._call_js(reviver, [py_to_js(key), v], UNDEFINED)
            return _walk('', parsed)
        json_obj.value['stringify'] = intr(_stringify, 'stringify')
        json_obj.value['parse']     = intr(_parse, 'parse')
        g.declare('JSON', json_obj, 'var')

        # -- Object statics --
        obj_ctor = JsValue("object", {})

        def _public_keys(d, obj=None):
            keys = [k for k in d.keys() if not k.startswith('__') and not (k.startswith('@@') and k.endswith('@@'))]
            if obj is not None:
                keys = [k for k in keys if interp._is_enumerable(obj, k)]
            return keys

        obj_ctor.value['keys']   = intr(lambda a,i: py_to_js(_public_keys(a[0].value, a[0])) if a and a[0].type=='object' else py_to_js([]), 'keys')
        obj_ctor.value['values'] = intr(lambda a,i: py_to_js([a[0].value[k] for k in _public_keys(a[0].value, a[0])]) if a and a[0].type=='object' else py_to_js([]), 'values')
        obj_ctor.value['entries']= intr(lambda a,i: py_to_js([[k,a[0].value[k]] for k in _public_keys(a[0].value, a[0])]) if a and a[0].type=='object' else py_to_js([]), 'entries')
        obj_ctor.value['assign'] = intr(lambda a,i: _obj_assign(a,i), 'assign')
        def _obj_assign(args, interp):
            target = args[0] if args else py_to_js({})
            for src in args[1:]:
                if src.type not in ('object', 'array') or not isinstance(src.value, dict):
                    continue
                # Collect own keys: regular keys + getter-only keys from __get__xxx
                all_keys = set()
                for key in src.value.keys():
                    if key.startswith('__') and key.endswith('__'):
                        continue
                    if key.startswith('@@') and key.endswith('@@'):
                        continue
                    if key.startswith('__get__'):
                        all_keys.add(key[len('__get__'):])
                        continue
                    if key.startswith('__set__'):
                        continue
                    all_keys.add(key)
                for key in all_keys:
                    if not interp._is_enumerable(src, key):
                        continue
                    val = interp._get_prop(src, key)
                    interp._set_prop(target, key, val)
            return target
        obj_ctor.value['hasOwn'] = intr(
            lambda a, i: JS_TRUE if len(a) > 1 and a[0].type == 'object' and i._to_key(a[1]) in a[0].value else JS_FALSE,
            'hasOwn',
        )
        def _obj_from_entries(args, interp):
            if not args:
                return py_to_js({})
            src = args[0]
            out = {}
            for entry in interp._array_like_items(src):
                if isinstance(entry, JsValue) and entry.type == 'array' and len(entry.value) >= 2:
                    out[interp._to_key(entry.value[0])] = entry.value[1]
            return JsValue('object', out)
        obj_ctor.value['fromEntries'] = intr(_obj_from_entries, 'fromEntries')
        def _obj_group_by(args, interp):
            iterable = args[0] if args else UNDEFINED
            callback = args[1] if len(args) > 1 else UNDEFINED
            if not interp._is_callable(callback):
                raise _JSError(py_to_js('TypeError: callback is not a function'))
            items = interp._array_like_items(iterable)
            result = JsValue('object', {})
            for idx, item in enumerate(items):
                key = interp._to_str(interp._call_js(callback, [item, JsValue('number', float(idx))], UNDEFINED))
                existing = result.value.get(key)
                if existing is None:
                    result.value[key] = JsValue('array', [item])
                else:
                    existing.value.append(item)
            return result
        obj_ctor.value['groupBy'] = intr(_obj_group_by, 'Object.groupBy')
        def _obj_create(args, interp):
            proto = args[0] if args else UNDEFINED
            new_obj = JsValue('object', {})
            if proto.type not in ('undefined', 'null'):
                new_obj.value['__proto__'] = proto
            return new_obj
        obj_ctor.value['create'] = intr(_obj_create, 'Object.create')
        def _obj_define_property(args, interp):
            if len(args) < 3: return args[0] if args else UNDEFINED
            obj, key, desc = args[0], interp._to_key(args[1]), args[2]
            if obj.type not in ('object', 'function', 'intrinsic', 'class'): return obj
            if desc.type == 'object':
                getter = desc.value.get('get')
                setter = desc.value.get('set')
                if getter and interp._is_callable(getter):
                    obj.value[f"__get__{key}"] = getter
                if setter and interp._is_callable(setter):
                    obj.value[f"__set__{key}"] = setter
                value = desc.value.get('value')
                if value is not None:
                    obj.value[key] = value
                # Build and store descriptor dict
                existing = interp._get_desc(obj, key) or {}
                w_raw = desc.value.get('writable')
                e_raw = desc.value.get('enumerable')
                c_raw = desc.value.get('configurable')
                if w_raw is not None:
                    existing['writable'] = interp._truthy(w_raw)
                if e_raw is not None:
                    existing['enumerable'] = interp._truthy(e_raw)
                if c_raw is not None:
                    existing['configurable'] = interp._truthy(c_raw)
                if getter:
                    existing['get'] = getter
                if setter:
                    existing['set'] = setter
                # Only store descriptor if any flag was explicitly set
                if existing:
                    interp._set_desc(obj, key, existing)
            return obj
        obj_ctor.value['defineProperty'] = intr(_obj_define_property, 'defineProperty')
        obj_ctor.value['getOwnPropertyNames'] = intr(
            lambda a, i: py_to_js([k for k in (a[0].value.keys() if a and a[0].type == 'object' else []) if not k.startswith('__')]),
            'getOwnPropertyNames',
        )

        def _obj_is(args, interp):
            a = args[0] if args else UNDEFINED
            b = args[1] if len(args) > 1 else UNDEFINED
            if a.type != b.type:
                return JS_FALSE
            if a.type == 'number':
                av, bv = a.value, b.value
                if math.isnan(av) and math.isnan(bv):
                    return JS_TRUE
                if av == 0.0 and bv == 0.0:
                    return JS_TRUE if math.copysign(1, av) == math.copysign(1, bv) else JS_FALSE
                return JS_TRUE if av == bv else JS_FALSE
            return JS_TRUE if interp._strict_eq(a, b) else JS_FALSE

        obj_ctor.value['is'] = intr(_obj_is, 'Object.is')
        obj_ctor.value['getPrototypeOf'] = intr(lambda a, i: i._get_proto(a[0]) if a else UNDEFINED, 'Object.getPrototypeOf')

        def _obj_set_prototype_of(args, interp):
            obj = args[0] if args else UNDEFINED
            proto = args[1] if len(args) > 1 else UNDEFINED
            if isinstance(obj.value, dict):
                if proto.type == 'null':
                    obj.value['__proto__'] = JS_NULL
                elif proto.type in ('object', 'function', 'class', 'intrinsic'):
                    obj.value['__proto__'] = proto
            return obj

        obj_ctor.value['setPrototypeOf'] = intr(_obj_set_prototype_of, 'Object.setPrototypeOf')

        def _obj_proto_to_string(this_val, args, interp):
            target = this_val
            if target.type == 'null': return py_to_js('[object Null]')
            if target.type == 'undefined': return py_to_js('[object Undefined]')
            if target.type == 'number': return py_to_js('[object Number]')
            if target.type == 'string': return py_to_js('[object String]')
            if target.type == 'boolean': return py_to_js('[object Boolean]')
            if target.type == 'array': return py_to_js('[object Array]')
            if target.type == 'function': return py_to_js('[object Function]')
            if target.type in ('object', 'intrinsic', 'class'):
                if isinstance(target.value, dict):
                    tag_key = f"@@{SYMBOL_TO_STRING_TAG}@@"
                    tag = target.value.get(tag_key)
                    if tag and isinstance(tag, JsValue) and tag.type == 'string':
                        return py_to_js(f'[object {tag.value}]')
                    kind = target.value.get('__kind__')
                    if isinstance(kind, JsValue) and kind.type == 'string':
                        return py_to_js(f'[object {kind.value}]')
                return py_to_js('[object Object]')
            return py_to_js('[object Object]')

        proto_obj = JsValue('object', {})
        proto_obj.value['toString'] = self._make_intrinsic(_obj_proto_to_string, 'Object.prototype.toString')
        obj_ctor.value['prototype'] = proto_obj

        def _obj_get_own_prop_desc(args, interp):
            if len(args) < 2:
                return UNDEFINED
            obj, key = args[0], interp._to_key(args[1])
            if obj.type not in ('object', 'function', 'intrinsic', 'class'):
                return UNDEFINED
            getter_key = f"__get__{key}"
            setter_key = f"__set__{key}"
            stored = interp._get_desc(obj, key) or {}
            if getter_key in obj.value or setter_key in obj.value:
                desc = JsValue('object', {})
                if getter_key in obj.value:
                    desc.value['get'] = obj.value[getter_key]
                if setter_key in obj.value:
                    desc.value['set'] = obj.value[setter_key]
                desc.value['enumerable'] = JS_TRUE if stored.get('enumerable', True) else JS_FALSE
                desc.value['configurable'] = JS_TRUE if stored.get('configurable', True) else JS_FALSE
                return desc
            if key in obj.value:
                desc = JsValue('object', {})
                desc.value['value'] = obj.value[key]
                desc.value['writable'] = JS_TRUE if stored.get('writable', True) else JS_FALSE
                desc.value['enumerable'] = JS_TRUE if stored.get('enumerable', True) else JS_FALSE
                desc.value['configurable'] = JS_TRUE if stored.get('configurable', True) else JS_FALSE
                return desc
            return UNDEFINED

        obj_ctor.value['getOwnPropertyDescriptor'] = intr(_obj_get_own_prop_desc, 'Object.getOwnPropertyDescriptor')

        def _obj_get_own_prop_descs(args, interp):
            if not args or args[0].type not in ('object', 'function', 'intrinsic', 'class'):
                return py_to_js({})
            obj = args[0]
            result = JsValue('object', {})
            for k in list(obj.value.keys()):
                desc = _obj_get_own_prop_desc([obj, py_to_js(k)], interp)
                if desc.type != 'undefined':
                    result.value[k] = desc
            return result

        obj_ctor.value['getOwnPropertyDescriptors'] = intr(_obj_get_own_prop_descs, 'Object.getOwnPropertyDescriptors')

        def _obj_get_own_prop_symbols(args, interp):
            if not args or args[0].type not in ('object', 'function', 'intrinsic', 'class'):
                return py_to_js([])
            obj = args[0]
            syms = [JsValue('string', k) for k in obj.value.keys()
                    if k.startswith('@@') and k.endswith('@@') and len(k) > 4]
            return JsValue('array', syms)

        obj_ctor.value['getOwnPropertySymbols'] = intr(_obj_get_own_prop_symbols, 'Object.getOwnPropertySymbols')
        def _obj_freeze(args, interp):
            if not args: return UNDEFINED
            obj = args[0]
            if isinstance(obj.value, dict):
                obj.value['__extensible__'] = False
                for k in list(obj.value.keys()):
                    if k.startswith('__') and k.endswith('__'):
                        continue
                    existing = interp._get_desc(obj, k) or {}
                    existing['writable'] = False
                    existing['configurable'] = False
                    if 'enumerable' not in existing:
                        existing['enumerable'] = True
                    interp._set_desc(obj, k, existing)
            return obj

        def _obj_seal(args, interp):
            if not args: return UNDEFINED
            obj = args[0]
            if isinstance(obj.value, dict):
                obj.value['__extensible__'] = False
                for k in list(obj.value.keys()):
                    if k.startswith('__') and k.endswith('__'):
                        continue
                    existing = interp._get_desc(obj, k) or {}
                    existing['configurable'] = False
                    if 'enumerable' not in existing:
                        existing['enumerable'] = True
                    interp._set_desc(obj, k, existing)
            return obj

        def _obj_prevent_extensions(args, interp):
            if not args: return UNDEFINED
            obj = args[0]
            if isinstance(obj.value, dict):
                obj.value['__extensible__'] = False
            return obj

        def _obj_is_extensible(args, interp):
            if not args: return JS_TRUE
            obj = args[0]
            if not isinstance(getattr(obj, 'value', None), dict):
                return JS_TRUE
            return JS_FALSE if obj.value.get('__extensible__') is False else JS_TRUE

        def _obj_is_frozen(args, interp):
            if not args: return JS_FALSE
            obj = args[0]
            if not isinstance(getattr(obj, 'value', None), dict):
                return JS_FALSE
            if obj.value.get('__extensible__', True) is not False:
                return JS_FALSE
            for k in obj.value:
                if k.startswith('__') and k.endswith('__'):
                    continue
                desc = interp._get_desc(obj, k) or {}
                if desc.get('writable', True) or desc.get('configurable', True):
                    return JS_FALSE
            return JS_TRUE

        def _obj_is_sealed(args, interp):
            if not args: return JS_FALSE
            obj = args[0]
            if not isinstance(getattr(obj, 'value', None), dict):
                return JS_FALSE
            if obj.value.get('__extensible__', True) is not False:
                return JS_FALSE
            for k in obj.value:
                if k.startswith('__') and k.endswith('__'):
                    continue
                desc = interp._get_desc(obj, k) or {}
                if desc.get('configurable', True):
                    return JS_FALSE
            return JS_TRUE

        obj_ctor.value['freeze'] = intr(_obj_freeze, 'Object.freeze')
        obj_ctor.value['seal'] = intr(_obj_seal, 'Object.seal')
        obj_ctor.value['preventExtensions'] = intr(_obj_prevent_extensions, 'Object.preventExtensions')
        obj_ctor.value['isExtensible'] = intr(_obj_is_extensible, 'Object.isExtensible')
        obj_ctor.value['isFrozen'] = intr(_obj_is_frozen, 'Object.isFrozen')
        obj_ctor.value['isSealed'] = intr(_obj_is_sealed, 'Object.isSealed')
        g.declare('Object', obj_ctor, 'var')

        # -- Array constructor (used for Array.isArray etc.) --
        arr_ctor = JsValue("object", {})
        arr_ctor.value['isArray'] = intr(lambda a,i: JS_TRUE if a and a[0].type=='array' else JS_FALSE, 'isArray')
        def _array_from(args, interp):
            if not args:
                return py_to_js([])
            src = args[0]
            map_fn = args[1] if len(args) > 1 and interp._is_callable(args[1]) else None
            it = interp._get_js_iterator(src)
            if it is not None:
                items = []
                seen = 0
                while seen < 100000:
                    r = it()
                    seen += 1
                    done = interp._get_prop(r, 'done')
                    if interp._truthy(done):
                        break
                    items.append(interp._get_prop(r, 'value'))
            elif src.type == 'array':
                items = list(src.value)
            elif src.type == 'string':
                items = [JsValue('string', ch) for ch in src.value]
            elif src.type == 'object':
                raw_length = src.value.get('length', UNDEFINED)
                if isinstance(raw_length, JsValue) and raw_length.type != 'undefined':
                    length = max(0, int(interp._to_num(raw_length)))
                    items = [src.value.get(str(index), UNDEFINED) for index in range(length)]
                else:
                    items = []
            else:
                items = []
            if map_fn:
                items = [interp._call_js(map_fn, [item, JsValue('number', i)], UNDEFINED) for i, item in enumerate(items)]
            return JsValue('array', items)
        arr_ctor.value['from']    = intr(_array_from, 'from')
        arr_ctor.value['of']      = intr(lambda a, i: JsValue('array', list(a)), 'of')
        g.declare('Array', arr_ctor, 'var')

        number_ctor = intr(lambda a,i: py_to_js(i._to_num(a[0]) if a else 0), 'Number')
        number_ctor.value['isNaN'] = self._make_intrinsic(
            lambda this_val, args, interp: JS_TRUE if args and math.isnan(interp._to_num(args[0])) else JS_FALSE,
            'Number.isNaN',
        )
        number_ctor.value['isFinite'] = self._make_intrinsic(
            lambda this_val, args, interp: JS_TRUE if args and math.isfinite(interp._to_num(args[0])) else JS_FALSE,
            'Number.isFinite',
        )
        number_ctor.value['isInteger'] = self._make_intrinsic(
            lambda this_val, args, interp: JS_TRUE if args and interp._to_num(args[0]) == int(interp._to_num(args[0])) and math.isfinite(interp._to_num(args[0])) else JS_FALSE,
            'Number.isInteger',
        )
        number_ctor.value['EPSILON'] = JsValue('number', 2.220446049250313e-16)
        number_ctor.value['MAX_SAFE_INTEGER'] = JsValue('number', float(2**53 - 1))
        number_ctor.value['MIN_SAFE_INTEGER'] = JsValue('number', float(-(2**53 - 1)))
        number_ctor.value['MAX_VALUE'] = JsValue('number', 1.7976931348623157e+308)
        number_ctor.value['MIN_VALUE'] = JsValue('number', 5e-324)
        number_ctor.value['POSITIVE_INFINITY'] = JsValue('number', float('inf'))
        number_ctor.value['NEGATIVE_INFINITY'] = JsValue('number', float('-inf'))
        number_ctor.value['NaN'] = JsValue('number', float('nan'))
        number_ctor.value['isSafeInteger'] = self._make_intrinsic(
            lambda this_val, args, interp: (
                JS_TRUE if args and math.isfinite(interp._to_num(args[0]))
                and interp._to_num(args[0]) == int(interp._to_num(args[0]))
                and abs(interp._to_num(args[0])) <= 2**53 - 1
                else JS_FALSE
            ),
            'Number.isSafeInteger',
        )
        number_ctor.value['parseFloat'] = intr(_parseFloat, 'Number.parseFloat')
        number_ctor.value['parseInt'] = intr(_parseInt, 'Number.parseInt')

        string_ctor = intr(lambda a,i: py_to_js(i._to_str(a[0]) if a else ''), 'String')
        def _string_raw(args, interp):
            if not args:
                return JsValue('string', '')
            template = args[0]
            raw = interp._get_prop(template, 'raw')
            parts = interp._array_like_items(raw)
            out = []
            for index, part in enumerate(parts):
                out.append(interp._to_str(part))
                if index + 1 < len(args):
                    out.append(interp._to_str(args[index + 1]))
            return JsValue('string', ''.join(out))
        string_ctor.value['raw'] = self._make_intrinsic(lambda this_val, args, interp: _string_raw(args, interp), 'String.raw')
        string_ctor.value['fromCodePoint'] = self._make_intrinsic(
            lambda this_val, args, interp: JsValue('string', ''.join(chr(int(interp._to_num(a))) for a in args)),
            'String.fromCodePoint',
        )

        def _make_regexp(source, flags=''):
            flag_text = ''.join(sorted(set(flags)))
            py_flags = 0
            if 'i' in flag_text:
                py_flags |= re.IGNORECASE
            if 'm' in flag_text:
                py_flags |= re.MULTILINE
            if 's' in flag_text:
                py_flags |= re.DOTALL
            if 'u' in flag_text or 'v' in flag_text:
                py_flags |= re.UNICODE
            py_source = _js_regex_to_python(source)

            def _regexp_test(args, interp):
                text = interp._to_str(args[0]) if args else ''
                return JS_TRUE if re.search(py_source, text, py_flags) else JS_FALSE

            def _regexp_exec(args, interp):
                text = interp._to_str(args[0]) if args else ''
                match = re.search(py_source, text, py_flags)
                if not match:
                    return JS_NULL
                values = [JsValue('string', match.group(0))]
                values.extend(JsValue('string', group) if group is not None else UNDEFINED for group in match.groups())
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
            regexp.value['test'] = intr(_regexp_test, 'RegExp.test')
            regexp.value['exec'] = intr(_regexp_exec, 'RegExp.exec')
            return regexp

        def _regexp_ctor(args, interp):
            source = interp._to_str(args[0]) if args else ''
            flags = interp._to_str(args[1]) if len(args) > 1 else ''
            return _make_regexp(source, flags)

        g.declare('RegExp', self._make_intrinsic(lambda this_val, args, interp: _regexp_ctor(args, interp), 'RegExp'), 'var')

        def _make_date(value_ms=None):
            _ms = [float(value_ms if value_ms is not None else time.time() * 1000.0)]

            def _get_dt():
                return datetime.fromtimestamp(_ms[0] / 1000.0, tz=timezone.utc)

            def _date_get_time(args, interp):
                return JsValue('number', _ms[0])

            def _date_to_iso(args, interp):
                text = _get_dt().isoformat().replace('+00:00', 'Z')
                return JsValue('string', text)

            def _date_to_string(args, interp):
                return JsValue('string', _get_dt().ctime())

            def _date_get_full_year(args, interp):
                return JsValue('number', float(_get_dt().year))

            def _date_get_month(args, interp):
                return JsValue('number', float(_get_dt().month - 1))

            def _date_get_date(args, interp):
                return JsValue('number', float(_get_dt().day))

            def _date_get_day(args, interp):
                return JsValue('number', float((_get_dt().weekday() + 1) % 7))

            def _date_get_hours(args, interp):
                return JsValue('number', float(_get_dt().hour))

            def _date_get_minutes(args, interp):
                return JsValue('number', float(_get_dt().minute))

            def _date_get_seconds(args, interp):
                return JsValue('number', float(_get_dt().second))

            def _date_get_milliseconds(args, interp):
                return JsValue('number', float(_ms[0] % 1000))

            def _date_get_timezone_offset(args, interp):
                return JsValue('number', 0.0)

            def _date_set_full_year(args, interp):
                if not args: return JsValue('number', _ms[0])
                new_year = int(interp._to_num(args[0]))
                _ms[0] = _get_dt().replace(year=new_year).timestamp() * 1000
                return JsValue('number', _ms[0])

            def _date_set_month(args, interp):
                if not args: return JsValue('number', _ms[0])
                new_month = int(interp._to_num(args[0])) + 1
                _ms[0] = _get_dt().replace(month=new_month).timestamp() * 1000
                return JsValue('number', _ms[0])

            def _date_set_date(args, interp):
                if not args: return JsValue('number', _ms[0])
                _ms[0] = _get_dt().replace(day=int(interp._to_num(args[0]))).timestamp() * 1000
                return JsValue('number', _ms[0])

            def _date_set_hours(args, interp):
                if not args: return JsValue('number', _ms[0])
                _ms[0] = _get_dt().replace(hour=int(interp._to_num(args[0]))).timestamp() * 1000
                return JsValue('number', _ms[0])

            def _date_set_minutes(args, interp):
                if not args: return JsValue('number', _ms[0])
                _ms[0] = _get_dt().replace(minute=int(interp._to_num(args[0]))).timestamp() * 1000
                return JsValue('number', _ms[0])

            def _date_set_seconds(args, interp):
                if not args: return JsValue('number', _ms[0])
                _ms[0] = _get_dt().replace(second=int(interp._to_num(args[0]))).timestamp() * 1000
                return JsValue('number', _ms[0])

            def _date_set_milliseconds(args, interp):
                if not args: return JsValue('number', _ms[0])
                new_ms_part = int(interp._to_num(args[0]))
                _ms[0] = math.floor(_ms[0] / 1000) * 1000 + new_ms_part
                return JsValue('number', _ms[0])

            def _date_to_locale_date_string(args, interp):
                dt = _get_dt()
                return JsValue('string', f'{dt.month}/{dt.day}/{dt.year}')

            def _date_to_locale_time_string(args, interp):
                s = _get_dt().strftime('%I:%M:%S %p').lstrip('0')
                return JsValue('string', s)

            def _date_to_locale_string(args, interp):
                dt = _get_dt()
                date_part = f'{dt.month}/{dt.day}/{dt.year}'
                time_part = dt.strftime('%I:%M:%S %p').lstrip('0')
                return JsValue('string', f'{date_part}, {time_part}')

            obj = JsValue('object', {})
            obj.value['__kind__'] = JsValue('string', 'Date')
            obj.value['__date_ts__'] = _ms
            obj.value['getTime'] = intr(_date_get_time, 'Date.getTime')
            obj.value['toISOString'] = intr(_date_to_iso, 'Date.toISOString')
            obj.value['toJSON'] = intr(_date_to_iso, 'Date.toJSON')
            obj.value['toString'] = intr(_date_to_string, 'Date.toString')
            obj.value['valueOf'] = intr(_date_get_time, 'Date.valueOf')
            obj.value['getFullYear'] = intr(_date_get_full_year, 'Date.getFullYear')
            obj.value['getMonth'] = intr(_date_get_month, 'Date.getMonth')
            obj.value['getDate'] = intr(_date_get_date, 'Date.getDate')
            obj.value['getDay'] = intr(_date_get_day, 'Date.getDay')
            obj.value['getHours'] = intr(_date_get_hours, 'Date.getHours')
            obj.value['getMinutes'] = intr(_date_get_minutes, 'Date.getMinutes')
            obj.value['getSeconds'] = intr(_date_get_seconds, 'Date.getSeconds')
            obj.value['getMilliseconds'] = intr(_date_get_milliseconds, 'Date.getMilliseconds')
            obj.value['getTimezoneOffset'] = intr(_date_get_timezone_offset, 'Date.getTimezoneOffset')
            obj.value['setFullYear'] = intr(_date_set_full_year, 'Date.setFullYear')
            obj.value['setMonth'] = intr(_date_set_month, 'Date.setMonth')
            obj.value['setDate'] = intr(_date_set_date, 'Date.setDate')
            obj.value['setHours'] = intr(_date_set_hours, 'Date.setHours')
            obj.value['setMinutes'] = intr(_date_set_minutes, 'Date.setMinutes')
            obj.value['setSeconds'] = intr(_date_set_seconds, 'Date.setSeconds')
            obj.value['setMilliseconds'] = intr(_date_set_milliseconds, 'Date.setMilliseconds')
            obj.value['toLocaleDateString'] = intr(_date_to_locale_date_string, 'Date.toLocaleDateString')
            obj.value['toLocaleTimeString'] = intr(_date_to_locale_time_string, 'Date.toLocaleTimeString')
            obj.value['toLocaleString'] = intr(_date_to_locale_string, 'Date.toLocaleString')
            return obj

        def _date_ctor_fn(this_val, args, interp):
            if len(args) >= 2:
                year = int(interp._to_num(args[0]))
                month = int(interp._to_num(args[1])) + 1
                day = int(interp._to_num(args[2])) if len(args) > 2 else 1
                hours = int(interp._to_num(args[3])) if len(args) > 3 else 0
                minutes = int(interp._to_num(args[4])) if len(args) > 4 else 0
                seconds = int(interp._to_num(args[5])) if len(args) > 5 else 0
                ms_part = int(interp._to_num(args[6])) if len(args) > 6 else 0
                from datetime import datetime as _dt2
                dt = _dt2(year, month, day, hours, minutes, seconds, ms_part * 1000, tzinfo=timezone.utc)
                return _make_date(dt.timestamp() * 1000)
            return _make_date(interp._to_num(args[0]) if args else None)

        date_ctor = self._make_intrinsic(_date_ctor_fn, 'Date')
        date_ctor.value['now'] = self._make_intrinsic(
            lambda this_val, args, interp: JsValue('number', time.time() * 1000.0),
            'Date.now',
        )
        def _date_parse(this_val, args, interp):
            s = interp._to_str(args[0]) if args else ''
            from datetime import datetime as _dt_parse
            for fmt in ('%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S.%f',
                        '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d', '%b %d, %Y', '%d %b %Y',
                        '%Y/%m/%d', '%m/%d/%Y'):
                try:
                    dt = _dt_parse.strptime(s.strip(), fmt)
                    return JsValue('number', dt.replace(tzinfo=timezone.utc).timestamp() * 1000.0)
                except ValueError:
                    continue
            return JsValue('number', float('nan'))
        date_ctor.value['parse'] = self._make_intrinsic(_date_parse, 'Date.parse')
        def _date_utc(this_val, args, interp):
            nums = [int(interp._to_num(a)) for a in args]
            year = nums[0] if len(nums) > 0 else 1970
            if 0 <= year <= 99:
                year += 1900
            month = (nums[1] if len(nums) > 1 else 0) + 1
            day = nums[2] if len(nums) > 2 else 1
            hours = nums[3] if len(nums) > 3 else 0
            minutes = nums[4] if len(nums) > 4 else 0
            seconds = nums[5] if len(nums) > 5 else 0
            ms = nums[6] if len(nums) > 6 else 0
            from datetime import datetime as _dt_utc
            try:
                dt = _dt_utc(year, month, day, hours, minutes, seconds, ms * 1000, tzinfo=timezone.utc)
                return JsValue('number', dt.timestamp() * 1000.0)
            except (ValueError, OverflowError):
                return JsValue('number', float('nan'))
        date_ctor.value['UTC'] = self._make_intrinsic(_date_utc, 'Date.UTC')
        g.declare('Date', date_ctor, 'var')

        # -- task queue / timers --
        def _queue_microtask(args, interp):
            fn = args[0] if args else UNDEFINED
            if not interp._is_callable(fn):
                raise _JSError(py_to_js('queueMicrotask callback must be a function'))
            interp._enqueue_microtask(lambda fn=fn: interp._call_js(fn, [], UNDEFINED))
            return UNDEFINED

        def _set_timeout(args, interp):
            fn = args[0] if args else UNDEFINED
            if not interp._is_callable(fn):
                raise _JSError(py_to_js('setTimeout callback must be a function'))
            delay = interp._to_num(args[1]) if len(args) > 1 else 0
            return JsValue('number', interp._schedule_timer(fn, delay, False, args[2:]))

        def _set_interval(args, interp):
            fn = args[0] if args else UNDEFINED
            if not interp._is_callable(fn):
                raise _JSError(py_to_js('setInterval callback must be a function'))
            delay = interp._to_num(args[1]) if len(args) > 1 else 0
            return JsValue('number', interp._schedule_timer(fn, delay, True, args[2:]))

        def _clear_timer(args, interp):
            if args:
                interp._clear_timer(int(interp._to_num(args[0])))
            return UNDEFINED

        g.declare('queueMicrotask', intr(_queue_microtask, 'queueMicrotask'), 'var')
        g.declare('setTimeout', intr(_set_timeout, 'setTimeout'), 'var')
        g.declare('setInterval', intr(_set_interval, 'setInterval'), 'var')
        g.declare('clearTimeout', intr(_clear_timer, 'clearTimeout'), 'var')
        g.declare('clearInterval', intr(_clear_timer, 'clearInterval'), 'var')

        # -- performance --
        _perf_origin = time.perf_counter()
        perf_obj = JsValue('object', {})
        perf_obj.value['now'] = intr(lambda a, i: JsValue('number', (time.perf_counter() - _perf_origin) * 1000.0), 'performance.now')
        g.declare('performance', perf_obj, 'var')

        # -- String / Number / Boolean / Symbol constructors (minimal) --
        g.declare('String',  string_ctor, 'var')
        g.declare('Number',  number_ctor, 'var')
        g.declare('Boolean', intr(lambda a,i: JS_TRUE if a and i._truthy(a[0]) else JS_FALSE, 'Boolean'), 'var')

        # -- Full Symbol implementation --
        def _make_symbol(desc=''):
            _symbol_id_counter[0] += 1
            return JsValue('symbol', {'id': _symbol_id_counter[0], 'desc': str(desc)})

        def _symbol_ctor(args, interp):
            desc = interp._to_str(args[0]) if args and args[0].type != 'undefined' else ''
            return _make_symbol(desc)

        sym_ctor = intr(_symbol_ctor, 'Symbol')
        sym_ctor.value['iterator']     = JsValue('symbol', {'id': SYMBOL_ITERATOR,       'desc': 'Symbol.iterator'})
        sym_ctor.value['toPrimitive']  = JsValue('symbol', {'id': SYMBOL_TO_PRIMITIVE,   'desc': 'Symbol.toPrimitive'})
        sym_ctor.value['hasInstance']  = JsValue('symbol', {'id': SYMBOL_HAS_INSTANCE,   'desc': 'Symbol.hasInstance'})
        sym_ctor.value['toStringTag']  = JsValue('symbol', {'id': SYMBOL_TO_STRING_TAG,  'desc': 'Symbol.toStringTag'})
        sym_ctor.value['asyncIterator']= JsValue('symbol', {'id': SYMBOL_ASYNC_ITERATOR, 'desc': 'Symbol.asyncIterator'})
        sym_ctor.value['species']      = JsValue('symbol', {'id': SYMBOL_SPECIES,        'desc': 'Symbol.species'})
        sym_ctor.value['match']        = JsValue('symbol', {'id': SYMBOL_MATCH,          'desc': 'Symbol.match'})
        sym_ctor.value['replace']      = JsValue('symbol', {'id': SYMBOL_REPLACE,        'desc': 'Symbol.replace'})
        sym_ctor.value['split']        = JsValue('symbol', {'id': SYMBOL_SPLIT,          'desc': 'Symbol.split'})
        sym_ctor.value['search']       = JsValue('symbol', {'id': SYMBOL_SEARCH,         'desc': 'Symbol.search'})
        sym_ctor.value['isConcatSpreadable'] = JsValue('symbol', {'id': SYMBOL_IS_CONCAT_SPREADABLE, 'desc': 'Symbol.isConcatSpreadable'})

        def _sym_for(args, interp):
            if not args:
                return _make_symbol('')
            key = interp._to_str(args[0])
            if key in _symbol_registry:
                return _symbol_registry[key]
            sym = _make_symbol(key)
            _symbol_registry[key] = sym
            return sym

        def _sym_key_for(args, interp):
            if not args or args[0].type != 'symbol':
                return UNDEFINED
            sym_id = args[0].value['id']
            for key, val in _symbol_registry.items():
                if val.value['id'] == sym_id:
                    return JsValue('string', key)
            return UNDEFINED

        sym_ctor.value['for']    = intr(_sym_for, 'Symbol.for')
        sym_ctor.value['keyFor'] = intr(_sym_key_for, 'Symbol.keyFor')
        g.declare('Symbol', sym_ctor, 'var')

        # -- Proxy (real implementation) --
        g.declare('Proxy', self._make_intrinsic(
            lambda this_val, args, interp: JsValue('proxy', JsProxy(
                args[0] if args else py_to_js({}),
                args[1] if len(args) > 1 else py_to_js({})
            )),
            'Proxy',
        ), 'var')

        # -- WeakMap / WeakSet --
        def _make_weakmap():
            store = {}  # {id(key_pyobj): (key_pyobj, value)}
            wm = JsValue('object', {})

            def _wm_set(args, interp):
                key = args[0] if args else UNDEFINED
                val = args[1] if len(args) > 1 else UNDEFINED
                if key.type not in ('object', 'function', 'intrinsic', 'array', 'proxy'):
                    raise _JSError(py_to_js("Invalid value used as weak map key"))
                store[id(key)] = (key, val)
                return wm

            def _wm_get(args, interp):
                key = args[0] if args else UNDEFINED
                entry = store.get(id(key))
                return entry[1] if entry else UNDEFINED

            def _wm_has(args, interp):
                key = args[0] if args else UNDEFINED
                return JS_TRUE if id(key) in store else JS_FALSE

            def _wm_delete(args, interp):
                key = args[0] if args else UNDEFINED
                if id(key) in store:
                    del store[id(key)]
                    return JS_TRUE
                return JS_FALSE

            wm.value['set']    = intr(_wm_set,    'WeakMap.set')
            wm.value['get']    = intr(_wm_get,    'WeakMap.get')
            wm.value['has']    = intr(_wm_has,    'WeakMap.has')
            wm.value['delete'] = intr(_wm_delete, 'WeakMap.delete')
            return wm

        def _make_weakset():
            store = {}  # {id(key_pyobj): key_pyobj}
            ws = JsValue('object', {})

            def _ws_add(args, interp):
                key = args[0] if args else UNDEFINED
                if key.type not in ('object', 'function', 'intrinsic', 'array', 'proxy'):
                    raise _JSError(py_to_js("Invalid value used in weak set"))
                store[id(key)] = key
                return ws

            def _ws_has(args, interp):
                key = args[0] if args else UNDEFINED
                return JS_TRUE if id(key) in store else JS_FALSE

            def _ws_delete(args, interp):
                key = args[0] if args else UNDEFINED
                if id(key) in store:
                    del store[id(key)]
                    return JS_TRUE
                return JS_FALSE

            ws.value['add']    = intr(_ws_add,    'WeakSet.add')
            ws.value['has']    = intr(_ws_has,    'WeakSet.has')
            ws.value['delete'] = intr(_ws_delete, 'WeakSet.delete')
            return ws

        g.declare('WeakMap', self._make_intrinsic(lambda this_val, args, interp: _make_weakmap(), 'WeakMap'), 'var')
        g.declare('WeakSet', self._make_intrinsic(lambda this_val, args, interp: _make_weakset(), 'WeakSet'), 'var')

        # -- WeakRef --
        def _make_weakref(args, interp):
            target = args[0] if args else UNDEFINED
            return JsValue('object', {'__type__': py_to_js('WeakRef'), '__target__': target})

        g.declare('WeakRef', intr(_make_weakref, 'WeakRef'), 'var')

        # -- FinalizationRegistry --
        def _make_finalization_registry(args, interp):
            callback = args[0] if args else UNDEFINED
            obj = JsValue('object', {
                '__type__': py_to_js('FinalizationRegistry'),
                '__cb__': callback,
                '__entries__': JsValue('array', []),
            })

            def _fr_register(rargs, rinterp):
                target = rargs[0] if rargs else UNDEFINED
                held = rargs[1] if len(rargs) > 1 else UNDEFINED
                token = rargs[2] if len(rargs) > 2 else UNDEFINED
                entry = JsValue('object', {'target': target, 'held': held, 'token': token})
                obj.value['__entries__'].value.append(entry)
                return UNDEFINED

            def _fr_unregister(rargs, rinterp):
                token = rargs[0] if rargs else UNDEFINED
                entries = obj.value['__entries__'].value
                obj.value['__entries__'].value = [
                    e for e in entries
                    if not (e.type == 'object' and e.value.get('token') is token)
                ]
                return UNDEFINED

            obj.value['register'] = intr(_fr_register, 'FinalizationRegistry.register')
            obj.value['unregister'] = intr(_fr_unregister, 'FinalizationRegistry.unregister')
            return obj

        g.declare('FinalizationRegistry', intr(_make_finalization_registry, 'FinalizationRegistry'), 'var')

        # -- Reflect --
        reflect_obj = JsValue('object', {})

        def _reflect_get(args, interp):
            if not args: return UNDEFINED
            target = args[0]
            prop = args[1] if len(args) > 1 else UNDEFINED
            return interp._get_prop(target, prop)

        def _reflect_set(args, interp):
            if len(args) < 3: return JS_FALSE
            interp._set_prop(args[0], args[1], args[2])
            return JS_TRUE

        def _reflect_has(args, interp):
            if len(args) < 2: return JS_FALSE
            target, key = args[0], interp._to_key(args[1])
            if target.type == 'proxy':
                proxy = target.value
                trap = interp._get_trap(proxy.handler, 'has')
                if trap:
                    return interp._call_js(trap, [proxy.target, py_to_js(key)], UNDEFINED)
                return _reflect_has([proxy.target, args[1]], interp)
            if target.type in ('object', 'function', 'intrinsic', 'class'):
                return JS_TRUE if key in target.value else JS_FALSE
            if target.type == 'array':
                try: return JS_TRUE if 0 <= int(key) < len(target.value) else JS_FALSE
                except: return JS_FALSE
            return JS_FALSE

        def _reflect_delete(args, interp):
            if len(args) < 2: return JS_FALSE
            return JS_TRUE if interp._del_prop(args[0], args[1]) else JS_FALSE

        def _reflect_apply(args, interp):
            fn = args[0] if args else UNDEFINED
            this_arg = args[1] if len(args) > 1 else UNDEFINED
            fn_args = list(args[2].value) if len(args) > 2 and args[2].type == 'array' else []
            return interp._call_js(fn, fn_args, this_arg)

        def _reflect_construct(args, interp):
            fn = args[0] if args else UNDEFINED
            fn_args = list(args[1].value) if len(args) > 1 and args[1].type == 'array' else []
            new_obj = JsValue('object', {})
            proto = fn.value.get('prototype') if isinstance(fn.value, dict) else None
            if proto and proto.type == 'object':
                new_obj.value['__proto__'] = proto
            result = interp._call_js(fn, fn_args, new_obj, is_new_call=True)
            if isinstance(result, JsValue) and result.type in ('object', 'array', 'function', 'intrinsic', 'class', 'promise', 'proxy'):
                return result
            return new_obj

        def _reflect_own_keys(args, interp):
            if not args: return py_to_js([])
            target = args[0]
            if target.type in ('object', 'function', 'intrinsic', 'class'):
                return py_to_js([k for k in target.value.keys() if not k.startswith('__')])
            if target.type == 'array':
                return py_to_js([str(i) for i in range(len(target.value))])
            return py_to_js([])

        def _reflect_define_property(args, interp):
            if len(args) < 3: return JS_FALSE
            obj, key, desc = args[0], interp._to_key(args[1]), args[2]
            if obj.type in ('object', 'function', 'intrinsic', 'class') and desc.type == 'object':
                getter = desc.value.get('get')
                setter = desc.value.get('set')
                if getter and interp._is_callable(getter):
                    obj.value[f"__get__{key}"] = getter
                if setter and interp._is_callable(setter):
                    obj.value[f"__set__{key}"] = setter
                value = desc.value.get('value')
                if value is not None:
                    obj.value[key] = value
            return JS_TRUE

        def _reflect_get_own_prop_desc(args, interp):
            if len(args) < 2: return UNDEFINED
            target, key = args[0], interp._to_key(args[1])
            if target.type not in ('object', 'function', 'intrinsic', 'class'): return UNDEFINED
            getter_key = f"__get__{key}"
            setter_key = f"__set__{key}"
            if getter_key in target.value or setter_key in target.value:
                desc = JsValue('object', {})
                if getter_key in target.value:
                    desc.value['get'] = target.value[getter_key]
                if setter_key in target.value:
                    desc.value['set'] = target.value[setter_key]
                desc.value['enumerable'] = JS_TRUE
                desc.value['configurable'] = JS_TRUE
                return desc
            if key in target.value:
                desc = JsValue('object', {})
                desc.value['value'] = target.value[key]
                desc.value['writable'] = JS_TRUE
                desc.value['enumerable'] = JS_TRUE
                desc.value['configurable'] = JS_TRUE
                return desc
            return UNDEFINED

        reflect_obj.value['get']                      = intr(_reflect_get,              'Reflect.get')
        reflect_obj.value['set']                      = intr(_reflect_set,              'Reflect.set')
        reflect_obj.value['has']                      = intr(_reflect_has,              'Reflect.has')
        reflect_obj.value['deleteProperty']           = intr(_reflect_delete,           'Reflect.deleteProperty')
        reflect_obj.value['apply']                    = intr(_reflect_apply,            'Reflect.apply')
        reflect_obj.value['construct']                = intr(_reflect_construct,         'Reflect.construct')
        reflect_obj.value['ownKeys']                  = intr(_reflect_own_keys,         'Reflect.ownKeys')
        reflect_obj.value['defineProperty']           = intr(_reflect_define_property,  'Reflect.defineProperty')
        reflect_obj.value['getOwnPropertyDescriptor'] = intr(_reflect_get_own_prop_desc,'Reflect.getOwnPropertyDescriptor')
        reflect_obj.value['getPrototypeOf']           = intr(
            lambda a, i: i._get_proto(a[0]) if a else UNDEFINED, 'Reflect.getPrototypeOf',
        )
        reflect_obj.value['setPrototypeOf']           = intr(lambda a, i: JS_TRUE, 'Reflect.setPrototypeOf')
        reflect_obj.value['isExtensible']             = intr(lambda a, i: JS_TRUE, 'Reflect.isExtensible')
        reflect_obj.value['preventExtensions']        = intr(lambda a, i: JS_TRUE, 'Reflect.preventExtensions')
        g.declare('Reflect', reflect_obj, 'var')

        # -- BigInt constructor --
        def _bigint_ctor(args, interp):
            if not args:
                raise _JSError(py_to_js('BigInt requires an argument'))
            v = args[0]
            if v.type == 'bigint': return v
            if v.type == 'number':
                if not math.isfinite(v.value):
                    raise _JSError(py_to_js('Cannot convert non-finite number to BigInt'))
                return JsValue('bigint', int(v.value))
            if v.type == 'boolean': return JsValue('bigint', 1 if v.value else 0)
            if v.type == 'string':
                try: return JsValue('bigint', int(v.value.strip()))
                except: raise _JSError(py_to_js(f'Cannot convert "{v.value}" to a BigInt'))
            raise _JSError(py_to_js('Cannot convert to BigInt'))

        bigint_ctor = intr(_bigint_ctor, 'BigInt')

        def _bigint_as_int_n(args, interp):
            if len(args) < 2: return UNDEFINED
            n = int(interp._to_num(args[0]))
            val = args[1].value if args[1].type == 'bigint' else int(interp._to_num(args[1]))
            mod = 1 << n
            result = val % mod
            if result >= mod // 2:
                result -= mod
            return JsValue('bigint', result)

        def _bigint_as_uint_n(args, interp):
            if len(args) < 2: return UNDEFINED
            n = int(interp._to_num(args[0]))
            val = args[1].value if args[1].type == 'bigint' else int(interp._to_num(args[1]))
            return JsValue('bigint', val % (1 << n))

        bigint_ctor.value['asIntN']  = intr(_bigint_as_int_n,  'BigInt.asIntN')
        bigint_ctor.value['asUintN'] = intr(_bigint_as_uint_n, 'BigInt.asUintN')
        g.declare('BigInt', bigint_ctor, 'var')
        def _make_map(entries=None):
            store = []

            def _find_index(key):
                for index, (existing_key, _value) in enumerate(store):
                    if self._strict_eq(existing_key, key):
                        return index
                return -1

            map_obj = JsValue('object', {})

            def _map_set(args, interp):
                key = args[0] if args else UNDEFINED
                value = args[1] if len(args) > 1 else UNDEFINED
                index = _find_index(key)
                if index >= 0:
                    store[index] = (key, value)
                else:
                    store.append((key, value))
                return map_obj

            def _map_get(args, interp):
                key = args[0] if args else UNDEFINED
                index = _find_index(key)
                return store[index][1] if index >= 0 else UNDEFINED

            def _map_has(args, interp):
                return JS_TRUE if _find_index(args[0] if args else UNDEFINED) >= 0 else JS_FALSE

            def _map_delete(args, interp):
                index = _find_index(args[0] if args else UNDEFINED)
                if index >= 0:
                    del store[index]
                    return JS_TRUE
                return JS_FALSE

            def _map_clear(args, interp):
                store.clear()
                return UNDEFINED

            map_obj.value['__kind__'] = JsValue('string', 'Map')
            map_obj.value['set'] = intr(_map_set, 'Map.set')
            map_obj.value['get'] = intr(_map_get, 'Map.get')
            map_obj.value['has'] = intr(_map_has, 'Map.has')
            map_obj.value['delete'] = intr(_map_delete, 'Map.delete')
            map_obj.value['clear'] = intr(_map_clear, 'Map.clear')
            map_obj.value['__size_fn__'] = intr(lambda a, i: JsValue('number', len(store)), 'Map.__size__')
            def _make_map_iter_fn(get_items_fn):
                def _make_it(a, i):
                    items = get_items_fn()
                    idx = [0]
                    it_obj = JsValue('object', {})
                    def _next(tv, a2, intp, _items=items, _idx=idx):
                        if _idx[0] >= len(_items):
                            return JsValue('object', {'value': UNDEFINED, 'done': JS_TRUE})
                        val = _items[_idx[0]]; _idx[0] += 1
                        return JsValue('object', {'value': val, 'done': JS_FALSE})
                    it_obj.value['next'] = i._make_intrinsic(_next, 'MapIterator.next')
                    sym_k = f"@@{SYMBOL_ITERATOR}@@"
                    it_obj.value[sym_k] = i._make_intrinsic(lambda tv, a2, intp, it=it_obj: it, '[Symbol.iterator]')
                    i._add_iterator_helpers(it_obj)
                    return it_obj
                return intr(_make_it, 'MapIterator')
            map_obj.value['keys'] = _make_map_iter_fn(lambda: [key for key, _ in store])
            map_obj.value['values'] = _make_map_iter_fn(lambda: [value for _, value in store])
            map_obj.value['entries'] = _make_map_iter_fn(lambda: [JsValue('array', [key, value]) for key, value in store])
            map_obj.value[f"@@{SYMBOL_ITERATOR}@@"] = _make_map_iter_fn(lambda: [JsValue('array', [key, value]) for key, value in store])
            def _map_for_each(args, interp):
                callback = args[0] if args else UNDEFINED
                this_arg = args[1] if len(args) > 1 else UNDEFINED
                if not interp._is_callable(callback):
                    raise _JSError(py_to_js('TypeError: callback is not a function'))
                for key, value in list(store):
                    interp._call_js(callback, [value, key, map_obj], this_arg)
                return UNDEFINED
            map_obj.value['forEach'] = intr(_map_for_each, 'Map.forEach')
            if entries is not None:
                for entry in self._array_like_items(entries):
                    if isinstance(entry, JsValue) and entry.type == 'array' and len(entry.value) >= 2:
                        store.append((entry.value[0], entry.value[1]))
            return map_obj

        def _make_set(values=None):
            store = []

            def _find_index(value):
                for index, existing in enumerate(store):
                    if self._strict_eq(existing, value):
                        return index
                return -1

            set_obj = JsValue('object', {})

            def _set_add(args, interp):
                value = args[0] if args else UNDEFINED
                if _find_index(value) < 0:
                    store.append(value)
                return set_obj

            def _set_has(args, interp):
                return JS_TRUE if _find_index(args[0] if args else UNDEFINED) >= 0 else JS_FALSE

            def _set_delete(args, interp):
                index = _find_index(args[0] if args else UNDEFINED)
                if index >= 0:
                    del store[index]
                    return JS_TRUE
                return JS_FALSE

            def _set_clear(args, interp):
                store.clear()
                return UNDEFINED

            set_obj.value['__kind__'] = JsValue('string', 'Set')
            set_obj.value['add'] = intr(_set_add, 'Set.add')
            set_obj.value['has'] = intr(_set_has, 'Set.has')
            set_obj.value['delete'] = intr(_set_delete, 'Set.delete')
            set_obj.value['clear'] = intr(_set_clear, 'Set.clear')
            set_obj.value['__size_fn__'] = intr(lambda a, i: JsValue('number', len(store)), 'Set.__size__')
            def _make_set_iter_fn(get_items_fn):
                def _make_it(a, i):
                    items = get_items_fn()
                    idx = [0]
                    it_obj = JsValue('object', {})
                    def _next(tv, a2, intp, _items=items, _idx=idx):
                        if _idx[0] >= len(_items):
                            return JsValue('object', {'value': UNDEFINED, 'done': JS_TRUE})
                        val = _items[_idx[0]]; _idx[0] += 1
                        return JsValue('object', {'value': val, 'done': JS_FALSE})
                    it_obj.value['next'] = i._make_intrinsic(_next, 'SetIterator.next')
                    sym_k = f"@@{SYMBOL_ITERATOR}@@"
                    it_obj.value[sym_k] = i._make_intrinsic(lambda tv, a2, intp, it=it_obj: it, '[Symbol.iterator]')
                    i._add_iterator_helpers(it_obj)
                    return it_obj
                return intr(_make_it, 'SetIterator')
            set_obj.value['values'] = _make_set_iter_fn(lambda: list(store))
            set_obj.value['keys'] = _make_set_iter_fn(lambda: list(store))
            set_obj.value['entries'] = _make_set_iter_fn(lambda: [JsValue('array', [v, v]) for v in store])
            set_obj.value[f"@@{SYMBOL_ITERATOR}@@"] = _make_set_iter_fn(lambda: list(store))

            def _set_for_each(args, interp):
                callback = args[0] if args else UNDEFINED
                this_arg = args[1] if len(args) > 1 else UNDEFINED
                if not interp._is_callable(callback):
                    raise _JSError(py_to_js('TypeError: callback is not a function'))
                for value in list(store):
                    interp._call_js(callback, [value, value, set_obj], this_arg)
                return UNDEFINED
            set_obj.value['forEach'] = intr(_set_for_each, 'Set.forEach')

            # ES2025 Set methods
            def _drain_set_iterable(other):
                """Drain any JS iterable to a Python list of JsValues."""
                if other.type == 'array':
                    return list(other.value)
                it_fn = self._get_js_iterator(other)
                if it_fn is None:
                    return []
                items = []
                while True:
                    r = it_fn()
                    if not isinstance(r, JsValue) or r.type != 'object':
                        break
                    done = r.value.get('done', JS_FALSE)
                    if isinstance(done, JsValue) and done.value is True:
                        break
                    items.append(r.value.get('value', UNDEFINED))
                return items

            def _make_new_set(items):
                new_s = _make_set()
                new_add = new_s.value['add']
                for item in items:
                    self._call_js(new_add, [item], new_s)
                return new_s

            def _other_contains(other_items, value):
                return any(self._strict_eq(item, value) for item in other_items)

            def _set_union(args, interp):
                other = args[0] if args else UNDEFINED
                other_items = _drain_set_iterable(other)
                result_items = list(store)
                for item in other_items:
                    if not _other_contains(result_items, item):
                        result_items.append(item)
                return _make_new_set(result_items)

            def _set_intersection(args, interp):
                other = args[0] if args else UNDEFINED
                other_items = _drain_set_iterable(other)
                return _make_new_set([v for v in store if _other_contains(other_items, v)])

            def _set_difference(args, interp):
                other = args[0] if args else UNDEFINED
                other_items = _drain_set_iterable(other)
                return _make_new_set([v for v in store if not _other_contains(other_items, v)])

            def _set_symmetric_difference(args, interp):
                other = args[0] if args else UNDEFINED
                other_items = _drain_set_iterable(other)
                result = [v for v in store if not _other_contains(other_items, v)]
                for item in other_items:
                    if not _other_contains(store, item):
                        result.append(item)
                return _make_new_set(result)

            def _set_is_subset_of(args, interp):
                other = args[0] if args else UNDEFINED
                other_items = _drain_set_iterable(other)
                return JS_TRUE if all(_other_contains(other_items, v) for v in store) else JS_FALSE

            def _set_is_superset_of(args, interp):
                other = args[0] if args else UNDEFINED
                other_items = _drain_set_iterable(other)
                return JS_TRUE if all(_other_contains(store, v) for v in other_items) else JS_FALSE

            def _set_is_disjoint_from(args, interp):
                other = args[0] if args else UNDEFINED
                other_items = _drain_set_iterable(other)
                return JS_FALSE if any(_other_contains(other_items, v) for v in store) else JS_TRUE

            set_obj.value['union'] = intr(_set_union, 'Set.union')
            set_obj.value['intersection'] = intr(_set_intersection, 'Set.intersection')
            set_obj.value['difference'] = intr(_set_difference, 'Set.difference')
            set_obj.value['symmetricDifference'] = intr(_set_symmetric_difference, 'Set.symmetricDifference')
            set_obj.value['isSubsetOf'] = intr(_set_is_subset_of, 'Set.isSubsetOf')
            set_obj.value['isSupersetOf'] = intr(_set_is_superset_of, 'Set.isSupersetOf')
            set_obj.value['isDisjointFrom'] = intr(_set_is_disjoint_from, 'Set.isDisjointFrom')
            if values is not None:
                for value in self._array_like_items(values):
                    if _find_index(value) < 0:
                        store.append(value)
            return set_obj

        map_ctor = self._make_intrinsic(lambda this_val, args, interp: _make_map(args[0] if args else None), 'Map')
        def _map_group_by(args, interp):
            iterable = args[0] if args else UNDEFINED
            callback = args[1] if len(args) > 1 else UNDEFINED
            if not interp._is_callable(callback):
                raise _JSError(py_to_js('TypeError: callback is not a function'))
            items = interp._array_like_items(iterable)
            result = _make_map()
            map_set_fn = result.value['set']
            for idx, item in enumerate(items):
                key = interp._call_js(callback, [item, JsValue('number', float(idx))], UNDEFINED)
                map_get_fn = result.value['get']
                map_has_fn = result.value['has']
                has = interp._call_js(map_has_fn, [key], result)
                if self._truthy(has):
                    existing = interp._call_js(map_get_fn, [key], result)
                    existing.value.append(item)
                else:
                    interp._call_js(map_set_fn, [key, JsValue('array', [item])], result)
            return result
        map_ctor.value['groupBy'] = intr(_map_group_by, 'Map.groupBy')
        g.declare('Map', map_ctor, 'var')
        g.declare('Set', self._make_intrinsic(lambda this_val, args, interp: _make_set(args[0] if args else None), 'Set'), 'var')

        def _promise_ctor(this_val, args, interp):
            executor = args[0] if args else UNDEFINED
            if not interp._is_callable(executor):
                raise _JSError(py_to_js('Promise executor must be a function'))
            promise = interp._new_promise()
            resolve_fn = interp._make_intrinsic(
                lambda _this, call_args, inner: inner._resolve_promise(promise, call_args[0] if call_args else UNDEFINED),
                'Promise.resolve',
            )
            reject_fn = interp._make_intrinsic(
                lambda _this, call_args, inner: inner._reject_promise(promise, call_args[0] if call_args else UNDEFINED),
                'Promise.reject',
            )
            try:
                interp._call_js(executor, [resolve_fn, reject_fn], UNDEFINED)
            except _JSError as exc:
                interp._reject_promise(promise, exc.value)
            return promise

        promise_ctor = self._make_intrinsic(_promise_ctor, 'Promise')
        promise_ctor.value['resolve'] = self._make_intrinsic(
            lambda this_val, args, interp: interp._to_promise(args[0] if args else UNDEFINED),
            'Promise.resolve',
        )
        promise_ctor.value['reject'] = self._make_intrinsic(
            lambda this_val, args, interp: interp._rejected_promise(args[0] if args else UNDEFINED),
            'Promise.reject',
        )
        promise_ctor.value['all'] = self._make_intrinsic(
            lambda this_val, args, interp: interp._promise_all(args[0].value if args and args[0].type == 'array' else []),
            'Promise.all',
        )
        promise_ctor.value['race'] = self._make_intrinsic(
            lambda this_val, args, interp: interp._promise_race(args[0].value if args and args[0].type == 'array' else []),
            'Promise.race',
        )
        promise_ctor.value['allSettled'] = self._make_intrinsic(
            lambda this_val, args, interp: interp._promise_all_settled(
                args[0].value if args and args[0].type == 'array' else []
            ),
            'Promise.allSettled',
        )
        promise_ctor.value['any'] = self._make_intrinsic(
            lambda this_val, args, interp: interp._promise_any(
                args[0].value if args and args[0].type == 'array' else []
            ),
            'Promise.any',
        )

        def _promise_with_resolvers(args, interp):
            promise = interp._new_promise()
            resolve_fn = interp._make_intrinsic(
                lambda _this, call_args, inner: inner._resolve_promise(
                    promise, call_args[0] if call_args else UNDEFINED
                ),
                'resolve',
            )
            reject_fn = interp._make_intrinsic(
                lambda _this, call_args, inner: inner._reject_promise(
                    promise, call_args[0] if call_args else UNDEFINED
                ),
                'reject',
            )
            return JsValue('object', {
                'promise': promise,
                'resolve': resolve_fn,
                'reject': reject_fn,
            })

        promise_ctor.value['withResolvers'] = intr(_promise_with_resolvers, 'Promise.withResolvers')

        def _promise_try(args, interp):
            fn = args[0] if args else UNDEFINED
            try:
                result = interp._call_js(fn, args[1:], UNDEFINED)
                return interp._to_promise(result)
            except _JSError as e:
                return interp._rejected_promise(e.value)
            except Exception as e:
                js_err = interp._make_js_error('Error', str(e))
                return interp._rejected_promise(js_err)

        promise_ctor.value['try'] = intr(_promise_try, 'Promise.try')
        g.declare('Promise', promise_ctor, 'var')

        pyvm_obj = JsValue('object', {})

        def _pyvm_args(value, interp):
            if value is None or value is UNDEFINED:
                return []
            if value.type != 'array':
                raise _JSError(py_to_js('PyVM argv must be an array'))
            return [interp._to_str(item) for item in value.value]

        def _pyvm_arg_list(value, interp, label):
            if value is None or value is UNDEFINED:
                return []
            if value.type == 'array':
                return [interp._to_str(item) for item in value.value]
            if value.type == 'string':
                return [value.value]
            raise _JSError(py_to_js(f'{label} must be a string or array'))

        def _pyvm_timeout(value, interp):
            if value is None or value is UNDEFINED:
                return None
            timeout_ms = interp._to_num(value)
            return max(0.0, timeout_ms) / 1000.0

        def _pyvm_options(value, interp):
            if value is None or value is UNDEFINED:
                return {}
            if value.type != 'object':
                raise _JSError(py_to_js('PyVM options must be an object'))
            raw = interp._to_py(value)
            options = {}
            if raw.get('cwd') is not None:
                options['cwd'] = str(raw['cwd'])
            timeout_ms = raw.get('timeoutMs')
            if timeout_ms is not None:
                options['timeout'] = max(0.0, float(timeout_ms)) / 1000.0
            env = raw.get('env')
            if env is not None:
                merged = dict(os.environ)
                for key, item in env.items():
                    merged[str(key)] = '' if item is None else str(item)
                options['env'] = merged
            return options

        def _pyvm_result(proc, command):
            return py_to_js({
                'ok': proc.returncode == 0,
                'code': proc.returncode,
                'stdout': proc.stdout,
                'stderr': proc.stderr,
                'command': command,
            })

        def _pyvm_run_command(command, options):
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                cwd=options.get('cwd'),
                env=options.get('env'),
                timeout=options.get('timeout'),
            )

        def _pyvm_exec(args, interp):
            if not args:
                raise _JSError(py_to_js('PyVM.exec requires Python source'))
            code = interp._to_str(args[0])
            argv = _pyvm_args(args[1] if len(args) > 1 else UNDEFINED, interp)
            options = _pyvm_options(args[2] if len(args) > 2 else UNDEFINED, interp)
            command = [sys.executable, '-c', code, *argv]
            proc = _pyvm_run_command(command, options)
            return _pyvm_result(proc, command)

        def _pyvm_run_file(args, interp):
            if not args:
                raise _JSError(py_to_js('PyVM.runFile requires a path'))
            path = interp._to_str(args[0])
            argv = _pyvm_args(args[1] if len(args) > 1 else UNDEFINED, interp)
            options = _pyvm_options(args[2] if len(args) > 2 else UNDEFINED, interp)
            command = [sys.executable, path, *argv]
            proc = _pyvm_run_command(command, options)
            return _pyvm_result(proc, command)

        def _pyvm_run_module(args, interp):
            if not args:
                raise _JSError(py_to_js('PyVM.runModule requires a module name'))
            module = interp._to_str(args[0])
            argv = _pyvm_args(args[1] if len(args) > 1 else UNDEFINED, interp)
            options = _pyvm_options(args[2] if len(args) > 2 else UNDEFINED, interp)
            command = [sys.executable, '-m', module, *argv]
            proc = _pyvm_run_command(command, options)
            return _pyvm_result(proc, command)

        def _pyvm_pip_install(args, interp):
            packages = _pyvm_arg_list(args[0] if args else UNDEFINED, interp, 'PyVM.pipInstall packages')
            if not packages:
                raise _JSError(py_to_js('PyVM.pipInstall requires at least one package'))
            extra_args = _pyvm_args(args[1] if len(args) > 1 else UNDEFINED, interp)
            options = _pyvm_options(args[2] if len(args) > 2 else UNDEFINED, interp)
            command = [sys.executable, '-m', 'pip', 'install', *packages, *extra_args]
            proc = _pyvm_run_command(command, options)
            return _pyvm_result(proc, command)

        def _pyvm_pip_show(args, interp):
            packages = _pyvm_arg_list(args[0] if args else UNDEFINED, interp, 'PyVM.pipShow packages')
            if not packages:
                raise _JSError(py_to_js('PyVM.pipShow requires at least one package'))
            options = _pyvm_options(args[1] if len(args) > 1 else UNDEFINED, interp)
            command = [sys.executable, '-m', 'pip', 'show', *packages]
            proc = _pyvm_run_command(command, options)
            return _pyvm_result(proc, command)

        def _pyvm_pip_list(args, interp):
            options = _pyvm_options(args[0] if args else UNDEFINED, interp)
            command = [sys.executable, '-m', 'pip', 'list', '--format=json']
            proc = _pyvm_run_command(command, options)
            result = _pyvm_result(proc, command)
            if proc.returncode == 0:
                result.value['packages'] = py_to_js(json.loads(proc.stdout or '[]'))
            else:
                result.value['packages'] = py_to_js([])
            return result

        def _host_os_cwd(args, interp):
            return py_to_js(os.getcwd())

        def _host_os_chdir(args, interp):
            if not args:
                raise _JSError(py_to_js('os.chdir requires a path'))
            os.chdir(interp._to_str(args[0]))
            return py_to_js(os.getcwd())

        def _host_os_listdir(args, interp):
            path = interp._to_str(args[0]) if args else '.'
            return py_to_js(sorted(os.listdir(path)))

        def _host_os_exists(args, interp):
            if not args:
                raise _JSError(py_to_js('os.exists requires a path'))
            return py_to_js(os.path.exists(interp._to_str(args[0])))

        def _host_os_mkdir(args, interp):
            if not args:
                raise _JSError(py_to_js('os.mkdir requires a path'))
            path = interp._to_str(args[0])
            parents = bool(args[1].value) if len(args) > 1 and args[1].type == 'boolean' else False
            if parents:
                os.makedirs(path, exist_ok=True)
            else:
                os.mkdir(path)
            return py_to_js(path)

        def _host_os_read_text(args, interp):
            if not args:
                raise _JSError(py_to_js('os.readText requires a path'))
            path = interp._to_str(args[0])
            encoding = interp._to_str(args[1]) if len(args) > 1 else 'utf-8'
            with open(path, encoding=encoding) as handle:
                return py_to_js(handle.read())

        def _host_os_write_text(args, interp):
            if len(args) < 2:
                raise _JSError(py_to_js('os.writeText requires path and content'))
            path = interp._to_str(args[0])
            content = interp._to_str(args[1])
            encoding = interp._to_str(args[2]) if len(args) > 2 else 'utf-8'
            with open(path, 'w', encoding=encoding) as handle:
                handle.write(content)
            return py_to_js(path)

        def _host_os_stat(args, interp):
            if not args:
                raise _JSError(py_to_js('os.stat requires a path'))
            path = interp._to_str(args[0])
            info = os.stat(path)
            return py_to_js({
                'size': info.st_size,
                'mode': info.st_mode,
                'mtimeMs': info.st_mtime * 1000.0,
                'isFile': os.path.isfile(path),
                'isDir': os.path.isdir(path),
            })

        def _host_os_getenv(args, interp):
            if not args:
                raise _JSError(py_to_js('os.getenv requires a key'))
            key = interp._to_str(args[0])
            default = interp._to_str(args[1]) if len(args) > 1 else None
            value = os.environ.get(key, default)
            return UNDEFINED if value is None else py_to_js(value)

        def _host_os_setenv(args, interp):
            if len(args) < 2:
                raise _JSError(py_to_js('os.setenv requires key and value'))
            key = interp._to_str(args[0])
            value = interp._to_str(args[1])
            os.environ[key] = value
            return py_to_js(value)

        def _host_os_unsetenv(args, interp):
            if not args:
                raise _JSError(py_to_js('os.unsetenv requires a key'))
            key = interp._to_str(args[0])
            existed = key in os.environ
            os.environ.pop(key, None)
            return py_to_js(existed)

        def _host_os_which(args, interp):
            if not args:
                raise _JSError(py_to_js('os.which requires a command name'))
            result = shutil.which(interp._to_str(args[0]))
            return UNDEFINED if result is None else py_to_js(result)

        def _host_os_join(args, interp):
            if not args:
                return py_to_js('')
            return py_to_js(os.path.join(*(interp._to_str(arg) for arg in args)))

        def _host_os_dirname(args, interp):
            if not args:
                raise _JSError(py_to_js('os.dirname requires a path'))
            return py_to_js(os.path.dirname(interp._to_str(args[0])))

        def _host_os_basename(args, interp):
            if not args:
                raise _JSError(py_to_js('os.basename requires a path'))
            return py_to_js(os.path.basename(interp._to_str(args[0])))

        def _host_os_abspath(args, interp):
            if not args:
                raise _JSError(py_to_js('os.abspath requires a path'))
            return py_to_js(os.path.abspath(interp._to_str(args[0])))

        def _host_os_remove(args, interp):
            if not args:
                raise _JSError(py_to_js('os.remove requires a path'))
            path = interp._to_str(args[0])
            os.remove(path)
            return py_to_js(path)

        def _host_os_rmdir(args, interp):
            if not args:
                raise _JSError(py_to_js('os.rmdir requires a path'))
            path = interp._to_str(args[0])
            recursive = bool(args[1].value) if len(args) > 1 and args[1].type == 'boolean' else False
            if recursive:
                shutil.rmtree(path)
            else:
                os.rmdir(path)
            return py_to_js(path)

        def _host_os_rename(args, interp):
            if len(args) < 2:
                raise _JSError(py_to_js('os.rename requires source and destination'))
            src = interp._to_str(args[0])
            dst = interp._to_str(args[1])
            os.replace(src, dst)
            return py_to_js(dst)

        def _pyvm_async_runner(sync_fn, label):
            def runner(args, interp):
                promise = interp._new_promise()

                def task():
                    try:
                        interp._resolve_promise(promise, sync_fn(args, interp))
                    except _JSError as exc:
                        interp._reject_promise(promise, exc.value)
                    except subprocess.TimeoutExpired as exc:
                        interp._reject_promise(promise, py_to_js(f'{label} timed out: {exc}'))
                    except Exception as exc:
                        interp._reject_promise(promise, py_to_js(str(exc)))

                interp._enqueue_microtask(task)
                return promise

            return runner

        pyvm_obj.value['executable'] = py_to_js(sys.executable)
        pyvm_obj.value['version'] = py_to_js(sys.version)
        pyvm_obj.value['exec'] = intr(_pyvm_exec, 'PyVM.exec')
        pyvm_obj.value['runFile'] = intr(_pyvm_run_file, 'PyVM.runFile')
        pyvm_obj.value['runModule'] = intr(_pyvm_run_module, 'PyVM.runModule')
        pyvm_obj.value['pipInstall'] = intr(_pyvm_pip_install, 'PyVM.pipInstall')
        pyvm_obj.value['pipShow'] = intr(_pyvm_pip_show, 'PyVM.pipShow')
        pyvm_obj.value['pipList'] = intr(_pyvm_pip_list, 'PyVM.pipList')
        pyvm_obj.value['execAsync'] = intr(_pyvm_async_runner(_pyvm_exec, 'PyVM.execAsync'), 'PyVM.execAsync')
        pyvm_obj.value['runFileAsync'] = intr(_pyvm_async_runner(_pyvm_run_file, 'PyVM.runFileAsync'), 'PyVM.runFileAsync')
        pyvm_obj.value['runModuleAsync'] = intr(_pyvm_async_runner(_pyvm_run_module, 'PyVM.runModuleAsync'), 'PyVM.runModuleAsync')
        pyvm_obj.value['pipInstallAsync'] = intr(_pyvm_async_runner(_pyvm_pip_install, 'PyVM.pipInstallAsync'), 'PyVM.pipInstallAsync')
        pyvm_obj.value['pipShowAsync'] = intr(_pyvm_async_runner(_pyvm_pip_show, 'PyVM.pipShowAsync'), 'PyVM.pipShowAsync')
        pyvm_obj.value['pipListAsync'] = intr(_pyvm_async_runner(_pyvm_pip_list, 'PyVM.pipListAsync'), 'PyVM.pipListAsync')

        g.declare('PyVM', pyvm_obj, 'var')

        host_os = JsValue('object', {})
        host_os.value['name'] = py_to_js(os.name)
        host_os.value['sep'] = py_to_js(os.sep)
        host_os.value['linesep'] = py_to_js(os.linesep)
        host_os.value['cwd'] = intr(_host_os_cwd, 'os.cwd')
        host_os.value['chdir'] = intr(_host_os_chdir, 'os.chdir')
        host_os.value['listdir'] = intr(_host_os_listdir, 'os.listdir')
        host_os.value['exists'] = intr(_host_os_exists, 'os.exists')
        host_os.value['mkdir'] = intr(_host_os_mkdir, 'os.mkdir')
        host_os.value['readText'] = intr(_host_os_read_text, 'os.readText')
        host_os.value['writeText'] = intr(_host_os_write_text, 'os.writeText')
        host_os.value['stat'] = intr(_host_os_stat, 'os.stat')
        host_os.value['getenv'] = intr(_host_os_getenv, 'os.getenv')
        host_os.value['setenv'] = intr(_host_os_setenv, 'os.setenv')
        host_os.value['unsetenv'] = intr(_host_os_unsetenv, 'os.unsetenv')
        host_os.value['which'] = intr(_host_os_which, 'os.which')
        host_os.value['join'] = intr(_host_os_join, 'os.join')
        host_os.value['dirname'] = intr(_host_os_dirname, 'os.dirname')
        host_os.value['basename'] = intr(_host_os_basename, 'os.basename')
        host_os.value['abspath'] = intr(_host_os_abspath, 'os.abspath')
        host_os.value['remove'] = intr(_host_os_remove, 'os.remove')
        host_os.value['rmdir'] = intr(_host_os_rmdir, 'os.rmdir')
        host_os.value['rename'] = intr(_host_os_rename, 'os.rename')
        g.declare('os', host_os, 'var')

        host_sys = JsValue('object', {})
        host_sys.value['executable'] = py_to_js(sys.executable)
        host_sys.value['platform'] = py_to_js(sys.platform)
        host_sys.value['version'] = py_to_js(sys.version)
        host_sys.value['versionInfo'] = py_to_js(list(sys.version_info[:5]))
        host_sys.value['path'] = py_to_js(list(sys.path))
        g.declare('sys', host_sys, 'var')

        # -- structuredClone --
        def _structured_clone(args, interp):
            val = args[0] if args else UNDEFINED
            seen = {}
            def _typed_array_type(d):
                t = d.get('__type__')
                if isinstance(t, JsValue): return t.value
                return t
            def clone(v):
                if v.type in ('undefined', 'null', 'boolean', 'number', 'string', 'bigint', 'symbol'):
                    return v
                oid = id(v)
                if oid in seen:
                    raise _JSError(interp._make_js_error('DOMException', 'Circular reference'))
                seen[oid] = True
                try:
                    if isinstance(v.value, dict) and _typed_array_type(v.value) == 'TypedArray':
                        return JsValue('object', {
                            '__type__': v.value['__type__'],
                            '__name__': v.value['__name__'],
                            '__bytes__': bytearray(v.value['__bytes__']),
                            '__fmt__': v.value['__fmt__'],
                            '__itemsize__': v.value['__itemsize__'],
                            '__byteoffset__': v.value.get('__byteoffset__', 0),
                            '__length__': v.value['__length__'],
                        })
                    if isinstance(v.value, dict) and _typed_array_type(v.value) == 'ArrayBuffer':
                        return JsValue('object', {
                            '__type__': v.value['__type__'],
                            '__bytes__': bytearray(v.value['__bytes__']),
                        })
                    # Map
                    if isinstance(v.value, dict):
                        kind = v.value.get('__kind__')
                        if isinstance(kind, JsValue) and kind.value == 'Map':
                            entries_fn = v.value.get('entries')
                            if entries_fn:
                                it = interp._call_js(entries_fn, [], v)
                                pairs = []
                                while True:
                                    r = interp._call_js(it.value['next'], [], it)
                                    done = r.value.get('done', JS_FALSE)
                                    if isinstance(done, JsValue) and done.value is True:
                                        break
                                    pair = r.value.get('value', UNDEFINED)
                                    if pair.type == 'array' and len(pair.value) >= 2:
                                        pairs.append(JsValue('array', [clone(pair.value[0]), clone(pair.value[1])]))
                                return _make_map(JsValue('array', pairs))
                        # Set
                        if isinstance(kind, JsValue) and kind.value == 'Set':
                            values_fn = v.value.get('values')
                            if values_fn:
                                it = interp._call_js(values_fn, [], v)
                                items = []
                                while True:
                                    r = interp._call_js(it.value['next'], [], it)
                                    done = r.value.get('done', JS_FALSE)
                                    if isinstance(done, JsValue) and done.value is True:
                                        break
                                    items.append(clone(r.value.get('value', UNDEFINED)))
                                return _make_set(JsValue('array', items))
                    # Date
                    if v.type == 'object' and isinstance(v.value, dict) and '__date_ts__' in v.value:
                        ts = v.value['__date_ts__']
                        ts_val = ts[0] if isinstance(ts, list) else (ts.value if isinstance(ts, JsValue) else ts)
                        return _make_date(ts_val)
                    # RegExp
                    if v.type == 'regexp':
                        src = v.value.get('source', '')
                        flags = v.value.get('flags', '')
                        if isinstance(src, JsValue): src = src.value
                        if isinstance(flags, JsValue): flags = flags.value
                        return interp._make_regexp_val(src, flags)
                    # Error
                    if v.type == 'object' and isinstance(v.value, dict) and 'name' in v.value and 'message' in v.value and 'stack' in v.value:
                        name_v = v.value.get('name', UNDEFINED)
                        if isinstance(name_v, JsValue) and name_v.type == 'string' and name_v.value.endswith('Error'):
                            return JsValue('object', {k: clone(vv) for k, vv in v.value.items() if isinstance(vv, JsValue)})
                    if v.type == 'array':
                        return JsValue('array', [clone(el) for el in v.value])
                    if v.type == 'object':
                        return JsValue('object', {k: clone(vv) for k, vv in v.value.items()
                                                    if isinstance(vv, JsValue)})
                    return v
                finally:
                    seen.pop(oid, None)
            return clone(val)
        g.declare('structuredClone', intr(_structured_clone, 'structuredClone'), 'var')

        # -- Error constructors --
        _error_names = ('Error', 'TypeError', 'RangeError', 'SyntaxError', 'ReferenceError', 'URIError', 'EvalError')
        def _make_error_ctor(err_name):
            def _ctor(args, interp):
                msg = args[0] if args else UNDEFINED
                opts = args[1] if len(args) > 1 else UNDEFINED
                obj = JsValue('object', {
                    'message': msg if msg.type != 'undefined' else py_to_js(''),
                    'name': py_to_js(err_name),
                    'stack': py_to_js(f"{err_name}: {interp._to_str(msg) if msg.type != 'undefined' else ''}"),
                    '__error_type__': py_to_js(err_name),
                })
                if opts and opts.type == 'object' and 'cause' in opts.value:
                    obj.value['cause'] = opts.value['cause']
                return obj
            fn = intr(_ctor, err_name)
            return fn
        for _ename in _error_names:
            g.declare(_ename, _make_error_ctor(_ename), 'var')

        # -- AggregateError constructor --
        def _agg_error_ctor(args, interp):
            errors = args[0] if args else UNDEFINED
            msg = args[1] if len(args) > 1 else py_to_js('')
            return JsValue('object', {
                'message': msg if msg.type != 'undefined' else py_to_js(''),
                'name': py_to_js('AggregateError'),
                'errors': errors if errors.type == 'array' else JsValue('array', []),
                'stack': py_to_js(f"AggregateError: {interp._to_str(msg)}"),
                '__error_type__': py_to_js('AggregateError'),
            })
        g.declare('AggregateError', intr(_agg_error_ctor, 'AggregateError'), 'var')

        # -- URL and URLSearchParams --
        def _make_url_search_params(query_string=''):
            params = []
            if query_string:
                for part in query_string.split('&'):
                    if '=' in part:
                        k, v = part.split('=', 1)
                        params.append((k, v))
                    elif part:
                        params.append((part, ''))

            sp = JsValue('object', {})

            def _usp_get(args, interp):
                key = interp._to_str(args[0]) if args else ''
                for k, v in params:
                    if k == key:
                        return py_to_js(v)
                return UNDEFINED

            def _usp_get_all(args, interp):
                key = interp._to_str(args[0]) if args else ''
                return JsValue('array', [py_to_js(v) for k, v in params if k == key])

            def _usp_set(args, interp):
                nonlocal params
                key = interp._to_str(args[0]) if args else ''
                val = interp._to_str(args[1]) if len(args) > 1 else ''
                params = [(k, v) for k, v in params if k != key]
                params.append((key, val))
                return UNDEFINED

            def _usp_append(args, interp):
                key = interp._to_str(args[0]) if args else ''
                val = interp._to_str(args[1]) if len(args) > 1 else ''
                params.append((key, val))
                return UNDEFINED

            def _usp_delete(args, interp):
                nonlocal params
                key = interp._to_str(args[0]) if args else ''
                params = [(k, v) for k, v in params if k != key]
                return UNDEFINED

            def _usp_has(args, interp):
                key = interp._to_str(args[0]) if args else ''
                return JS_TRUE if any(k == key for k, v in params) else JS_FALSE

            def _usp_to_string(args, interp):
                return py_to_js('&'.join(f"{k}={v}" for k, v in params))

            def _usp_keys(args, interp):
                return JsValue('array', [py_to_js(k) for k, v in params])

            def _usp_values(args, interp):
                return JsValue('array', [py_to_js(v) for k, v in params])

            def _usp_entries(args, interp):
                return JsValue('array', [JsValue('array', [py_to_js(k), py_to_js(v)]) for k, v in params])

            def _usp_for_each(args, interp):
                fn = args[0] if args else UNDEFINED
                for k, v in params:
                    interp._call_js(fn, [py_to_js(v), py_to_js(k), sp], UNDEFINED)
                return UNDEFINED

            sp.value['get'] = intr(_usp_get, 'URLSearchParams.get')
            sp.value['getAll'] = intr(_usp_get_all, 'URLSearchParams.getAll')
            sp.value['set'] = intr(_usp_set, 'URLSearchParams.set')
            sp.value['append'] = intr(_usp_append, 'URLSearchParams.append')
            sp.value['delete'] = intr(_usp_delete, 'URLSearchParams.delete')
            sp.value['has'] = intr(_usp_has, 'URLSearchParams.has')
            sp.value['toString'] = intr(_usp_to_string, 'URLSearchParams.toString')
            sp.value['keys'] = intr(_usp_keys, 'URLSearchParams.keys')
            sp.value['values'] = intr(_usp_values, 'URLSearchParams.values')
            sp.value['entries'] = intr(_usp_entries, 'URLSearchParams.entries')
            sp.value['forEach'] = intr(_usp_for_each, 'URLSearchParams.forEach')
            return sp

        def _make_url(href_str):
            from urllib.parse import urlparse
            parsed = urlparse(href_str)
            url_obj = JsValue('object', {})
            url_obj.value['href'] = py_to_js(href_str)
            url_obj.value['protocol'] = py_to_js(parsed.scheme + ':' if parsed.scheme else '')
            url_obj.value['hostname'] = py_to_js(parsed.hostname or '')
            url_obj.value['port'] = py_to_js(str(parsed.port) if parsed.port else '')
            url_obj.value['host'] = py_to_js(parsed.netloc or '')
            url_obj.value['pathname'] = py_to_js(parsed.path or '/')
            url_obj.value['search'] = py_to_js(('?' + parsed.query) if parsed.query else '')
            url_obj.value['hash'] = py_to_js(('#' + parsed.fragment) if parsed.fragment else '')
            url_obj.value['origin'] = py_to_js(
                f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else 'null'
            )
            url_obj.value['searchParams'] = _make_url_search_params(parsed.query)
            url_obj.value['toString'] = intr(lambda a, i: url_obj.value['href'], 'URL.toString')
            return url_obj

        def _url_ctor(args, interp):
            href = interp._to_str(args[0]) if args else ''
            if len(args) > 1:
                from urllib.parse import urljoin
                base = interp._to_str(args[1])
                if base and not href.startswith('http'):
                    href = urljoin(base, href)
            return _make_url(href)

        def _usp_ctor(args, interp):
            init = args[0] if args else UNDEFINED
            qs = init.value.lstrip('?') if init.type == 'string' else ''
            return _make_url_search_params(qs)

        g.declare('URL', intr(_url_ctor, 'URL'), 'var')
        g.declare('URLSearchParams', intr(_usp_ctor, 'URLSearchParams'), 'var')

        # -- TextEncoder / TextDecoder --
        def _make_text_encoder():
            enc = JsValue('object', {})
            enc.value['encoding'] = py_to_js('utf-8')
            def _encode(args, interp):
                s = interp._to_str(args[0]) if args else ''
                data = s.encode('utf-8')
                return JsValue('array', [py_to_js(float(b)) for b in data])
            enc.value['encode'] = intr(_encode, 'TextEncoder.encode')
            return enc

        def _make_text_decoder(label='utf-8'):
            dec = JsValue('object', {})
            dec.value['encoding'] = py_to_js(label)
            def _decode(args, interp):
                buf = args[0] if args else UNDEFINED
                if buf.type == 'array':
                    data = bytes(int(v.value) for v in buf.value)
                    return py_to_js(data.decode('utf-8', errors='replace'))
                return py_to_js('')
            dec.value['decode'] = intr(_decode, 'TextDecoder.decode')
            return dec

        g.declare('TextEncoder', intr(lambda a, i: _make_text_encoder(), 'TextEncoder'), 'var')
        g.declare('TextDecoder', intr(
            lambda a, i: _make_text_decoder(i._to_str(a[0]) if a else 'utf-8'), 'TextDecoder'
        ), 'var')

        # -- crypto object --
        def _make_crypto():
            import os as _os
            crypto_obj = JsValue('object', {})

            def _random_uuid(args, interp):
                data = bytearray(_os.urandom(16))
                data[6] = (data[6] & 0x0f) | 0x40
                data[8] = (data[8] & 0x3f) | 0x80
                h = data.hex()
                return py_to_js(f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}")

            def _get_random_values(args, interp):
                arr = args[0] if args else UNDEFINED
                if arr.type == 'array':
                    random_bytes = _os.urandom(len(arr.value))
                    for i, b in enumerate(random_bytes):
                        arr.value[i] = py_to_js(float(b))
                return arr

            crypto_obj.value['randomUUID'] = intr(_random_uuid, 'crypto.randomUUID')
            crypto_obj.value['getRandomValues'] = intr(_get_random_values, 'crypto.getRandomValues')
            return crypto_obj

        g.declare('crypto', _make_crypto(), 'var')

        # -- AbortController / AbortSignal --
        def _make_abort_controller_fn(args, interp):
            signal = JsValue('object', {
                'aborted': JS_FALSE,
                'reason': UNDEFINED,
            })
            listeners = []

            def _add_event_listener(a, i):
                event_type = i._to_str(a[0]) if a else ''
                fn = a[1] if len(a) > 1 else UNDEFINED
                if event_type == 'abort':
                    listeners.append(fn)
                return UNDEFINED

            signal.value['addEventListener'] = intr(_add_event_listener, 'AbortSignal.addEventListener')

            ctrl = JsValue('object', {'signal': signal})

            def _abort(a, i):
                reason = a[0] if a else py_to_js('AbortError')
                if signal.value['aborted'].value is False:
                    signal.value['aborted'] = JS_TRUE
                    signal.value['reason'] = reason
                    for listener in listeners:
                        i._call_js(listener, [py_to_js({'type': 'abort'})], signal)
                return UNDEFINED

            ctrl.value['abort'] = intr(_abort, 'AbortController.abort')
            return ctrl

        g.declare('AbortController', intr(_make_abort_controller_fn, 'AbortController'), 'var')

        # -- ArrayBuffer --
        def _ab_ctor(this_val, args, interp):
            n = int(interp._to_num(args[0])) if args else 0
            if n < 0:
                raise _JSError(py_to_js('Invalid ArrayBuffer length'))
            return JsValue('object', {
                '__type__': py_to_js('ArrayBuffer'),
                '__bytes__': bytearray(n),
            })
        ab_ctor = self._make_intrinsic(_ab_ctor, 'ArrayBuffer')
        ab_ctor.value['isView'] = self._make_intrinsic(
            lambda tv, a, i: JS_TRUE if (
                a and isinstance(a[0], JsValue) and isinstance(a[0].value, dict) and
                isinstance(a[0].value.get('__type__'), JsValue) and
                a[0].value['__type__'].value in ('TypedArray', 'DataView')
            ) else JS_FALSE,
            'ArrayBuffer.isView',
        )
        g.declare('ArrayBuffer', ab_ctor, 'var')

        # -- TypedArray constructors --
        _TA_SPECS = [
            ('Int8Array',         'b', 1),
            ('Uint8Array',        'B', 1),
            ('Uint8ClampedArray', 'B', 1),
            ('Int16Array',        'h', 2),
            ('Uint16Array',       'H', 2),
            ('Int32Array',        'i', 4),
            ('Uint32Array',       'I', 4),
            ('Float32Array',      'f', 4),
            ('Float64Array',      'd', 8),
            ('BigInt64Array',     'q', 8),
            ('BigUint64Array',    'Q', 8),
        ]

        def _make_ta_ctor(ta_name, ta_fmt, ta_itemsize):
            def _ctor(this_val, args, interp):
                if not args:
                    return JsValue('object', {
                        '__type__': py_to_js('TypedArray'),
                        '__name__': ta_name,
                        '__bytes__': bytearray(),
                        '__fmt__': ta_fmt,
                        '__itemsize__': ta_itemsize,
                        '__byteoffset__': 0,
                        '__length__': 0,
                    })
                arg0 = args[0]
                # Case 1: integer -> allocate zeroed buffer
                if arg0.type == 'number':
                    length = max(0, int(interp._to_num(arg0)))
                    return JsValue('object', {
                        '__type__': py_to_js('TypedArray'),
                        '__name__': ta_name,
                        '__bytes__': bytearray(length * ta_itemsize),
                        '__fmt__': ta_fmt,
                        '__itemsize__': ta_itemsize,
                        '__byteoffset__': 0,
                        '__length__': length,
                    })
                # Case 2: ArrayBuffer -> view into it
                if isinstance(arg0.value, dict):
                    _at = arg0.value.get('__type__')
                    _at_s = _at.value if isinstance(_at, JsValue) else _at
                    if _at_s == 'ArrayBuffer':
                        buf = arg0.value.get('__bytes__', bytearray())
                        byte_off = int(interp._to_num(args[1])) if len(args) > 1 else 0
                        if len(args) > 2:
                            length = int(interp._to_num(args[2]))
                        else:
                            length = (len(buf) - byte_off) // ta_itemsize
                        ta = JsValue('object', {
                            '__type__': py_to_js('TypedArray'),
                            '__name__': ta_name,
                            '__bytes__': buf,
                            '__fmt__': ta_fmt,
                            '__itemsize__': ta_itemsize,
                            '__byteoffset__': byte_off,
                            '__length__': length,
                            '__buffer_jv__': arg0,
                        })
                        return ta
                    # Case 3: TypedArray -> copy from
                    if _at_s == 'TypedArray':
                        src_d = arg0.value
                        src_fmt = src_d.get('__fmt__', 'B')
                        src_is = src_d.get('__itemsize__', 1)
                        src_off = src_d.get('__byteoffset__', 0)
                        src_len = src_d.get('__length__', 0)
                        src_buf = src_d.get('__bytes__', bytearray())
                        buf = bytearray(src_len * ta_itemsize)
                        for i in range(src_len):
                            (raw,) = struct.unpack_from('=' + src_fmt, src_buf, src_off + i * src_is)
                            if ta_fmt in ('f', 'd'):
                                coerced = float(raw)
                            elif ta_name == 'Uint8ClampedArray':
                                coerced = max(0, min(255, int(float(raw))))
                            elif ta_fmt in ('q', 'Q'):
                                coerced = int(raw)
                            else:
                                coerced = _ta_coerce(JsValue('number', float(raw)), ta_fmt, interp)
                            struct.pack_into('=' + ta_fmt, buf, i * ta_itemsize, coerced)
                        return JsValue('object', {
                            '__type__': py_to_js('TypedArray'),
                            '__name__': ta_name,
                            '__bytes__': buf,
                            '__fmt__': ta_fmt,
                            '__itemsize__': ta_itemsize,
                            '__byteoffset__': 0,
                            '__length__': src_len,
                        })
                # Case 4: array / iterable -> copy
                items = list(arg0.value) if arg0.type == 'array' else interp._array_like_items(arg0)
                length = len(items)
                buf = bytearray(length * ta_itemsize)
                ta = JsValue('object', {
                    '__type__': py_to_js('TypedArray'),
                    '__name__': ta_name,
                    '__bytes__': buf,
                    '__fmt__': ta_fmt,
                    '__itemsize__': ta_itemsize,
                    '__byteoffset__': 0,
                    '__length__': length,
                })
                for i, item in enumerate(items):
                    interp._set_prop(ta, str(i), item)
                return ta

            ta_ctor = self._make_intrinsic(_ctor, ta_name)
            ta_ctor.value['BYTES_PER_ELEMENT'] = JsValue('number', float(ta_itemsize))
            ta_ctor.value['from'] = self._make_intrinsic(
                lambda tv, a, i, _c=_ctor: _c(UNDEFINED, [a[0]] if a else [], i),
                f'{ta_name}.from',
            )
            ta_ctor.value['of'] = self._make_intrinsic(
                lambda tv, a, i, _c=_ctor: _c(UNDEFINED, [JsValue('array', list(a))], i),
                f'{ta_name}.of',
            )
            return ta_ctor

        for _ta_name, _ta_fmt, _ta_itemsize in _TA_SPECS:
            g.declare(_ta_name, _make_ta_ctor(_ta_name, _ta_fmt, _ta_itemsize), 'var')

        # -- DataView --
        def _dv_ctor(this_val, args, interp):
            if not args:
                raise _JSError(py_to_js('DataView constructor requires ArrayBuffer argument'))
            buf_jv = args[0]
            _at = buf_jv.value.get('__type__') if isinstance(buf_jv.value, dict) else None
            _at_s = _at.value if isinstance(_at, JsValue) else _at
            if _at_s != 'ArrayBuffer':
                raise _JSError(py_to_js('DataView requires an ArrayBuffer'))
            buf = buf_jv.value.get('__bytes__', bytearray())
            byte_off = int(interp._to_num(args[1])) if len(args) > 1 else 0
            byte_len = int(interp._to_num(args[2])) if len(args) > 2 else len(buf) - byte_off
            return JsValue('object', {
                '__type__': py_to_js('DataView'),
                '__buffer__': buf_jv,
                '__byteoffset__': byte_off,
                '__bytelength__': byte_len,
            })
        g.declare('DataView', self._make_intrinsic(_dv_ctor, 'DataView'), 'var')

        # -- Intl (best-effort, stdlib only) --
        def _make_intl():
            from datetime import datetime as _dt, timezone as _tz

            def _make_dtf(args, interp):
                locale_str = interp._to_str(args[0]) if args and args[0].type != 'undefined' else 'en'
                opts = args[1] if len(args) > 1 and args[1].type == 'object' else UNDEFINED
                opts_dict = opts.value if opts.type == 'object' and isinstance(opts.value, dict) else {}

                def _get_opt(key, default=None):
                    v = opts_dict.get(key)
                    if isinstance(v, JsValue):
                        return interp._to_str(v)
                    return default

                def _format(a, i):
                    date_obj = a[0] if a else UNDEFINED
                    ms = None
                    if date_obj.type == 'number':
                        ms = date_obj.value
                    elif date_obj.type == 'object' and isinstance(date_obj.value, dict):
                        gt = date_obj.value.get('getTime')
                        if gt:
                            ms = i._to_num(i._call_js(gt, [], date_obj))
                    if ms is None:
                        ms = _dt.now(_tz.utc).timestamp() * 1000
                    d = _dt.fromtimestamp(ms / 1000.0, tz=_tz.utc)
                    year_style = _get_opt('year')
                    month_style = _get_opt('month')
                    day_style = _get_opt('day')
                    if year_style or month_style or day_style:
                        parts = []
                        if month_style == 'long':
                            parts.append(d.strftime('%B'))
                        elif month_style == 'short':
                            parts.append(d.strftime('%b'))
                        elif month_style == 'numeric' or month_style == '2-digit':
                            parts.append(str(d.month) if month_style == 'numeric' else f'{d.month:02d}')
                        if day_style == 'numeric':
                            parts.append(str(d.day))
                        elif day_style == '2-digit':
                            parts.append(f'{d.day:02d}')
                        if year_style == 'numeric':
                            return py_to_js(f'{", ".join(parts)}, {d.year}' if parts else str(d.year))
                        elif year_style == '2-digit':
                            return py_to_js(f'{", ".join(parts)}, {d.year % 100:02d}' if parts else f'{d.year % 100:02d}')
                        return py_to_js(', '.join(parts))
                    return py_to_js(d.strftime('%m/%d/%Y'))

                def _format_to_parts(a, i):
                    formatted = interp._to_str(_format(a, i))
                    part = JsValue('object', {'type': py_to_js('literal'), 'value': py_to_js(formatted)})
                    return JsValue('array', [part])

                def _resolved_options(a, i):
                    obj = JsValue('object', {})
                    obj.value['locale'] = py_to_js(locale_str)
                    for k, v in opts_dict.items():
                        obj.value[k] = v if isinstance(v, JsValue) else py_to_js(v)
                    return obj

                dtf = JsValue('object', {})
                dtf.value['format'] = intr(_format, 'DateTimeFormat.format')
                dtf.value['formatToParts'] = intr(_format_to_parts, 'DateTimeFormat.formatToParts')
                dtf.value['resolvedOptions'] = intr(_resolved_options, 'DateTimeFormat.resolvedOptions')
                return dtf

            def _make_nf(args, interp):
                locale_str = interp._to_str(args[0]) if args and args[0].type != 'undefined' else 'en'
                opts = args[1] if len(args) > 1 and args[1].type == 'object' else UNDEFINED
                opts_dict = opts.value if opts.type == 'object' and isinstance(opts.value, dict) else {}

                def _get_opt(key, default=None):
                    v = opts_dict.get(key)
                    if isinstance(v, JsValue):
                        return interp._to_str(v)
                    return default

                style = _get_opt('style', 'decimal')
                currency = _get_opt('currency', 'USD')

                def _format(a, i):
                    n = i._to_num(a[0]) if a else 0.0
                    if style == 'percent':
                        pct = n * 100
                        if pct == int(pct):
                            return py_to_js(f'{int(pct):,}%')
                        return py_to_js(f'{pct:,.2f}%')
                    if style == 'currency':
                        symbols = {'USD': '$', 'EUR': '€', 'GBP': '£', 'JPY': '¥'}
                        sym = symbols.get(currency, currency + ' ')
                        return py_to_js(f'{sym}{n:,.2f}')
                    # decimal (default)
                    if n == int(n):
                        return py_to_js(f'{int(n):,}')
                    return py_to_js(f'{n:,.3f}'.rstrip('0').rstrip('.'))

                nf = JsValue('object', {})
                nf.value['format'] = intr(_format, 'NumberFormat.format')
                return nf

            def _make_collator(args, interp):
                def _compare(a, i):
                    s1 = i._to_str(a[0]) if a else ''
                    s2 = i._to_str(a[1]) if len(a) > 1 else ''
                    if s1 < s2: return py_to_js(-1.0)
                    if s1 > s2: return py_to_js(1.0)
                    return py_to_js(0.0)

                coll = JsValue('object', {})
                coll.value['compare'] = intr(_compare, 'Collator.compare')
                return coll

            def _make_rtf(args, interp):
                def _format(a, i):
                    val = int(i._to_num(a[0])) if a else 0
                    unit = i._to_str(a[1]) if len(a) > 1 else 'second'
                    # strip plural 's' if present
                    unit = unit.rstrip('s') if unit.endswith('s') and unit != 'ss' else unit
                    abs_val = abs(val)
                    plural = 's' if abs_val != 1 else ''
                    if val < 0:
                        return py_to_js(f'{abs_val} {unit}{plural} ago')
                    return py_to_js(f'in {val} {unit}{plural}')

                rtf = JsValue('object', {})
                rtf.value['format'] = intr(_format, 'RelativeTimeFormat.format')
                return rtf

            def _make_lf(args, interp):
                opts = args[1] if len(args) > 1 and args[1].type == 'object' else UNDEFINED
                opts_dict = opts.value if opts.type == 'object' and isinstance(opts.value, dict) else {}
                lf_type_v = opts_dict.get('type')
                lf_type = interp._to_str(lf_type_v) if isinstance(lf_type_v, JsValue) else 'conjunction'

                def _format(a, i):
                    arr = a[0] if a else UNDEFINED
                    if arr.type != 'array':
                        return py_to_js('')
                    items = [i._to_str(v) for v in arr.value]
                    if len(items) == 0:
                        return py_to_js('')
                    if len(items) == 1:
                        return py_to_js(items[0])
                    if lf_type == 'disjunction':
                        if len(items) == 2:
                            return py_to_js(f'{items[0]} or {items[1]}')
                        return py_to_js(', '.join(items[:-1]) + f', or {items[-1]}')
                    if lf_type == 'unit':
                        return py_to_js(', '.join(items))
                    # conjunction (default)
                    if len(items) == 2:
                        return py_to_js(f'{items[0]} and {items[1]}')
                    return py_to_js(', '.join(items[:-1]) + f', and {items[-1]}')

                lf = JsValue('object', {})
                lf.value['format'] = intr(_format, 'ListFormat.format')
                return lf

            intl_obj = JsValue('object', {})
            intl_obj.value['DateTimeFormat'] = intr(_make_dtf, 'Intl.DateTimeFormat')
            intl_obj.value['NumberFormat'] = intr(_make_nf, 'Intl.NumberFormat')
            intl_obj.value['Collator'] = intr(_make_collator, 'Intl.Collator')
            intl_obj.value['RelativeTimeFormat'] = intr(_make_rtf, 'Intl.RelativeTimeFormat')
            intl_obj.value['ListFormat'] = intr(_make_lf, 'Intl.ListFormat')
            return intl_obj

        g.declare('Intl', _make_intl(), 'var')

        global_obj = JsValue('object', {})
        g.declare('globalThis', global_obj, 'var')
        self._global_object = global_obj
        for name, (_keyword, value) in g.bindings.items():
            global_obj.value[name] = value

        return g

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
            except: return float('nan')
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
            return UNDEFINED
        if obj.type == 'promise':
            if key in self.PROMISE_METHODS:
                return self._promise_method(obj, key)
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
            return UNDEFINED
        if obj.type == 'number':
            if key in self.NUMBER_METHODS:
                return self._num_method(obj, key)
        if obj.type == 'symbol':
            sym_str = self._to_str(obj)
            if key == 'toString':
                return self._make_intrinsic(lambda tv, a, i: JsValue('string', sym_str), 'Symbol.toString')
            if key == 'description':
                return JsValue('string', obj.value.get('desc', ''))
        return UNDEFINED

    def _set_prop(self, obj: JsValue, prop, val: JsValue):
        key = self._to_key(prop)
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
            except: pass
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
    def _exec(self, node, env=None):
        if env is None: env = self.env
        tp = node["type"]

        if tp == "Program":
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
                    if handler.get("param"):
                        catch_env.declare(handler["param"], e.value, 'let')
                    self._exec(handler["body"], catch_env)
            except Exception:
                handler = node.get("handler")
                if handler:
                    catch_env = Environment(env)
                    if handler.get("param"):
                        catch_env.declare(handler["param"], py_to_js("Python exception"), 'let')
                    self._exec(handler["body"], catch_env)
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
            except ReferenceError:
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



# ============================================================================
#  Internal control-flow exceptions
# ============================================================================

class _JSBreak(Exception):
    def __init__(self, label=None): self.label = label
class _JSContinue(Exception):
    def __init__(self, label=None): self.label = label
class _JSReturn(Exception):
    def __init__(self, value): self.value = value
class _JSError(Exception):
    def __init__(self, value: JsValue): self.value = value


# ============================================================================
#  flatten helper
# ============================================================================

def flatten_one(lst):
    result = []
    for x in lst:
        if isinstance(x, JsValue) and x.type == "array":
            result.extend(x.value)
        else:
            result.append(x)
    return result


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
