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

_scope_log = get_logger("scope")


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
    except (ValueError, TypeError, OverflowError): return JsValue("number", float('nan'))

def _parseFloat(args, interp):
    s = interp._to_str(args[0]) if args else 'undefined'
    try:
        m = re.match(r'^\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?', s)
        return JsValue("number", float(m.group()) if m else float('nan'))
    except (ValueError, TypeError, AttributeError): return JsValue("number", float('nan'))


def register_core_builtins(interp, g, intr):
    """Register core builtins into environment g."""
    # -- parseInt / parseFloat --
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
        except (ValueError, UnicodeDecodeError):  # binascii.Error is a ValueError subclass
            raise _JSError(py_to_js("InvalidCharacterError: Invalid base64 string"))
    g.declare('btoa', intr(_btoa, 'btoa'), 'var')
    g.declare('atob', intr(_atob, 'atob'), 'var')

    # -- console --
    import sys as _sys
    console = JsValue("object", {})
    def _log(args, interp):
        parts = [interp._to_str(a) for a in args]
        line = ' '.join(parts)
        indent = '  ' * interp._console_indent
        interp.output.append(indent + line)
        print(indent + line)
    def _log_stderr(args, interp, prefix=''):
        parts = [interp._to_str(a) for a in args]
        line = ' '.join(parts)
        indent = '  ' * interp._console_indent
        full = indent + (prefix + line if prefix else line)
        print(full, file=_sys.stderr)
    def _make_log_method(fn_name):
        return intr(lambda a,i: _log(a,i), fn_name)
    def _make_stderr_method(fn_name, prefix=''):
        return intr(lambda a,i: _log_stderr(a,i,prefix), fn_name)
    console.value['log']   = _make_log_method('log')
    console.value['error'] = _make_stderr_method('error')
    console.value['warn']  = _make_stderr_method('warn', 'Warning: ')
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
        _seen = set()

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
                vid = id(v.value)
                if vid in _seen:
                    raise _JSError(interp._make_js_error('TypeError', 'Converting circular structure to JSON'))
                _seen.add(vid)
                try:
                    return [_convert(str(i), x) for i, x in enumerate(v.value)]
                finally:
                    _seen.discard(vid)
            if v.type == 'object':
                vid = id(v.value)
                if vid in _seen:
                    raise _JSError(interp._make_js_error('TypeError', 'Converting circular structure to JSON'))
                _seen.add(vid)
                try:
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
                finally:
                    _seen.discard(vid)
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
    _scope_log.info("registered core builtins")


