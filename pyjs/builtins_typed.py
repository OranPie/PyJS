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


def register_typed_builtins(interp, g, intr):
    """Register typed builtins into environment g."""
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
    ab_ctor = interp._make_intrinsic(_ab_ctor, 'ArrayBuffer')
    ab_ctor.value['isView'] = interp._make_intrinsic(
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

        ta_ctor = interp._make_intrinsic(_ctor, ta_name)
        ta_ctor.value['BYTES_PER_ELEMENT'] = JsValue('number', float(ta_itemsize))
        ta_ctor.value['from'] = interp._make_intrinsic(
            lambda tv, a, i, _c=_ctor: _c(UNDEFINED, [a[0]] if a else [], i),
            f'{ta_name}.from',
        )
        ta_ctor.value['of'] = interp._make_intrinsic(
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
    g.declare('DataView', interp._make_intrinsic(_dv_ctor, 'DataView'), 'var')

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
    _log.info("registered typed array builtins")

