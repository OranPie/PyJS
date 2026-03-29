"""JS Generator and Async Generator implementations (thread-based)."""
from __future__ import annotations

import queue as _queue_mod
import threading

from .core import py_to_js
from .environment import Environment
from .exceptions import _JSReturn, _JSError
from .values import JsValue, UNDEFINED, JS_TRUE, JS_FALSE


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
