"""Auto-extracted builtin registrations for PyJS."""
from __future__ import annotations

import base64 as _b64
import json
import math
import os
import random
import re
import struct
import time
import urllib.parse as _urlparse
from datetime import datetime, timezone

from .core import JSTypeError, py_to_js, js_to_py
from .exceptions import _JSReturn, _JSError
from .trace import get_logger
from .values import (
    JsValue, JsProxy, UNDEFINED, JS_NULL, JS_TRUE, JS_FALSE,
    SYMBOL_ITERATOR, SYMBOL_TO_PRIMITIVE, SYMBOL_HAS_INSTANCE,
    SYMBOL_TO_STRING_TAG, SYMBOL_ASYNC_ITERATOR, SYMBOL_SPECIES,
    SYMBOL_MATCH, SYMBOL_REPLACE, SYMBOL_SPLIT, SYMBOL_SEARCH,
    SYMBOL_IS_CONCAT_SPREADABLE,
    _symbol_id_counter, _symbol_registry,
    _js_regex_to_python,
)

_log = get_logger("scope")


def register_promise_builtins(interp, g, intr):
    """Register promise builtins into environment g."""
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
                            return interp._call_js(g.get('Map'), [JsValue('array', pairs)], UNDEFINED, is_new_call=True)
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
                            return interp._call_js(g.get('Set'), [JsValue('array', items)], UNDEFINED, is_new_call=True)
                # Date
                if v.type == 'object' and isinstance(v.value, dict) and '__date_ts__' in v.value:
                    ts = v.value['__date_ts__']
                    ts_val = ts[0] if isinstance(ts, list) else (ts.value if isinstance(ts, JsValue) else ts)
                    return interp._call_js(g.get('Date'), [JsValue('number', float(ts_val))], UNDEFINED, is_new_call=True)
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
        _fn_ref = [None]  # forward reference to the constructor intrinsic
        def _ctor(args, interp):
            msg = args[0] if args else UNDEFINED
            opts = args[1] if len(args) > 1 else UNDEFINED
            msg_str = interp._to_str(msg) if msg.type != 'undefined' else ''
            # Build stack trace from live call stack
            frames = list(reversed(interp._js_call_stack))
            if frames:
                frame_lines = '\n'.join(
                    f"    at {f['name']} ({f['file']}:{f['line']})"
                    for f in frames
                )
                stack_str = f"{err_name}: {msg_str}\n{frame_lines}"
            else:
                stack_str = f"{err_name}: {msg_str}"
            obj = JsValue('object', {
                'message': py_to_js(msg_str),
                'name': py_to_js(err_name),
                'stack': py_to_js(stack_str),
                '__error_type__': py_to_js(err_name),
                'constructor': _fn_ref[0] or UNDEFINED,
            })
            if opts and opts.type == 'object' and 'cause' in opts.value:
                obj.value['cause'] = opts.value['cause']
            return obj
        fn = intr(_ctor, err_name)
        _fn_ref[0] = fn  # complete the back-reference
        return fn
    for _ename in _error_names:
        g.declare(_ename, _make_error_ctor(_ename), 'var')

    # -- Error.isError (ES2025 Stage 4) --
    def _error_is_error(args, interp):
        val = args[0] if args else UNDEFINED
        if val.type != 'object' or not isinstance(val.value, dict):
            return JS_FALSE
        if '__error_type__' in val.value:
            return JS_TRUE
        return JS_FALSE
    _error_ctor = g.get('Error')
    if isinstance(_error_ctor, JsValue):
        _error_ctor.value['isError'] = intr(_error_is_error, 'Error.isError')

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

    # -- eval throws EvalError (not supported) --
    def _eval_fn(args, interp):
        raise _JSError(interp._make_js_error('EvalError', 'eval is not supported'))
    g.declare('eval', intr(_eval_fn, 'eval'), 'var')

    _log.info("registered promise builtins")


