"""Node.js-like util module plugin for PyJS."""
from __future__ import annotations

import json
import math

from ..plugin import PyJSPlugin, PluginContext
from ..values import JsValue, UNDEFINED, JS_NULL, JS_TRUE, JS_FALSE
from ..core import py_to_js, js_to_py


class UtilPlugin(PyJSPlugin):
    name = "util"
    version = "1.0.0"

    def setup(self, ctx: PluginContext) -> None:
        interp = ctx.get_interpreter()

        # -- util.inspect ---------------------------------------------------

        def _inspect_value(val, depth, max_depth, colors, seen):
            """Recursively format a JsValue for display."""
            if not isinstance(val, JsValue):
                return repr(val)

            if val.type == 'undefined':
                return 'undefined'
            if val.type == 'null':
                return 'null'
            if val.type == 'boolean':
                return 'true' if val.value else 'false'
            if val.type == 'number':
                if math.isnan(val.value):
                    return 'NaN'
                if math.isinf(val.value):
                    return '-Infinity' if val.value < 0 else 'Infinity'
                v = val.value
                return str(int(v)) if v == int(v) and not math.isinf(v) else str(v)
            if val.type == 'string':
                return f"'{val.value}'"
            if val.type == 'bigint':
                return f'{val.value}n'
            if val.type == 'symbol':
                return f'Symbol({val.value})'
            if val.type == 'regexp':
                return str(val.value) if val.value else '/(?:)/'
            if val.type in ('function', 'intrinsic'):
                name = ''
                if isinstance(val.value, dict):
                    name = val.value.get('name', '')
                return f'[Function: {name}]' if name else '[Function (anonymous)]'

            obj_id = id(val)
            if obj_id in seen:
                return '[Circular *]'

            if depth >= max_depth:
                if val.type == 'array':
                    return f'[Array({len(val.value)})]' if isinstance(val.value, list) else '[Array]'
                return '[Object]'

            seen = seen | {obj_id}

            if val.type == 'array' and isinstance(val.value, list):
                if not val.value:
                    return '[]'
                items = [_inspect_value(v, depth + 1, max_depth, colors, seen)
                         for v in val.value]
                inner = ', '.join(items)
                if len(inner) < 72:
                    return f'[ {inner} ]'
                pad = '  ' * (depth + 1)
                lines = [f'{pad}{item}' for item in items]
                return '[\n' + ',\n'.join(lines) + '\n' + '  ' * depth + ']'

            if val.type == 'object' and isinstance(val.value, dict):
                if not val.value:
                    return '{}'
                parts = []
                for k, v in val.value.items():
                    if isinstance(k, str) and k.startswith('__'):
                        continue
                    formatted = _inspect_value(v, depth + 1, max_depth, colors, seen)
                    parts.append(f'{k}: {formatted}')
                if not parts:
                    return '{}'
                inner = ', '.join(parts)
                if len(inner) < 72:
                    return '{ ' + inner + ' }'
                pad = '  ' * (depth + 1)
                lines = [f'{pad}{p}' for p in parts]
                return '{\n' + ',\n'.join(lines) + '\n' + '  ' * depth + '}'

            if val.type == 'promise':
                state = val.value.get('state', 'pending') if isinstance(val.value, dict) else 'pending'
                return f'Promise {{ <{state}> }}'

            return interp._to_str(val)

        def inspect_fn(this_val, args, interp_inner):
            obj = args[0] if args else UNDEFINED
            max_depth = 2
            colors = False
            if len(args) > 1 and args[1].type == 'object' and isinstance(args[1].value, dict):
                opts = args[1].value
                d = opts.get('depth')
                if isinstance(d, JsValue) and d.type == 'number':
                    max_depth = int(d.value)
                c = opts.get('colors')
                if isinstance(c, JsValue) and c.type == 'boolean':
                    colors = c.value
            return py_to_js(_inspect_value(obj, 0, max_depth, colors, set()))

        # -- util.format ----------------------------------------------------

        def format_fn(this_val, args, interp_inner):
            if not args:
                return py_to_js('')
            fmt_val = args[0]
            if fmt_val.type != 'string':
                parts = [interp_inner._to_str(a) for a in args]
                return py_to_js(' '.join(parts))

            fmt = fmt_val.value
            rest = list(args[1:])
            result = []
            i = 0
            arg_idx = 0
            while i < len(fmt):
                if fmt[i] == '%' and i + 1 < len(fmt):
                    spec = fmt[i + 1]
                    if spec == '%':
                        result.append('%')
                        i += 2
                        continue
                    if arg_idx < len(rest):
                        a = rest[arg_idx]
                        arg_idx += 1
                        if spec == 's':
                            result.append(interp_inner._to_str(a))
                        elif spec == 'd':
                            try:
                                result.append(str(int(float(js_to_py(a)))))
                            except (ValueError, TypeError):
                                result.append('NaN')
                        elif spec == 'i':
                            try:
                                result.append(str(int(float(js_to_py(a)))))
                            except (ValueError, TypeError):
                                result.append('NaN')
                        elif spec == 'f':
                            try:
                                result.append(str(float(js_to_py(a))))
                            except (ValueError, TypeError):
                                result.append('NaN')
                        elif spec == 'j':
                            try:
                                result.append(interp_inner._json_serialize(a))
                            except Exception:
                                result.append('[Circular]')
                        elif spec == 'o':
                            result.append(_inspect_value(a, 0, 2, False, set()))
                        else:
                            result.append('%' + spec)
                            arg_idx -= 1
                    else:
                        result.append('%' + spec)
                    i += 2
                else:
                    result.append(fmt[i])
                    i += 1

            # Append remaining args separated by spaces
            for remaining in rest[arg_idx:]:
                result.append(' ')
                result.append(interp_inner._to_str(remaining))

            return py_to_js(''.join(result))

        # -- util.promisify -------------------------------------------------

        def promisify_fn(this_val, args, interp_inner):
            if not args or args[0].type not in ('function', 'intrinsic'):
                raise Exception('The "original" argument must be of type Function')
            original = args[0]

            def promisified(this_val2, args2, interp2):
                promise = interp2._new_promise()

                def callback(cb_this, cb_args, cb_interp):
                    err = cb_args[0] if cb_args else UNDEFINED
                    result = cb_args[1] if len(cb_args) > 1 else UNDEFINED
                    if err.type not in ('undefined', 'null'):
                        interp2._reject_promise(promise, err)
                    else:
                        interp2._resolve_promise(promise, result)
                    return UNDEFINED

                cb_js = interp2._make_intrinsic(callback, 'promisify_callback')
                call_args = list(args2) + [cb_js]
                interp2._call_function(original, call_args, UNDEFINED)
                return promise

            return ctx.make_function(promisified, 'promisified')

        # -- util.isDeepStrictEqual -----------------------------------------

        def _deep_equal(a, b, seen):
            if not isinstance(a, JsValue) or not isinstance(b, JsValue):
                return a is b
            if a.type != b.type:
                return False
            if a.type in ('undefined', 'null'):
                return True
            if a.type in ('boolean', 'number', 'string', 'bigint', 'symbol'):
                if a.type == 'number':
                    if math.isnan(a.value) and math.isnan(b.value):
                        return True
                return a.value == b.value
            if a.type == 'regexp':
                return str(a.value) == str(b.value)

            pair = (id(a), id(b))
            if pair in seen:
                return True
            seen = seen | {pair}

            if a.type == 'array' and isinstance(a.value, list) and isinstance(b.value, list):
                if len(a.value) != len(b.value):
                    return False
                return all(_deep_equal(a.value[i], b.value[i], seen)
                           for i in range(len(a.value)))

            if a.type == 'object' and isinstance(a.value, dict) and isinstance(b.value, dict):
                a_keys = {k for k in a.value if not (isinstance(k, str) and k.startswith('__'))}
                b_keys = {k for k in b.value if not (isinstance(k, str) and k.startswith('__'))}
                if a_keys != b_keys:
                    return False
                return all(_deep_equal(a.value[k], b.value[k], seen) for k in a_keys)

            return False

        def deep_equal_fn(this_val, args, interp_inner):
            a = args[0] if args else UNDEFINED
            b = args[1] if len(args) > 1 else UNDEFINED
            return JS_TRUE if _deep_equal(a, b, set()) else JS_FALSE

        # -- util.types -----------------------------------------------------

        def _is_promise(this_val, args, interp_inner):
            val = args[0] if args else UNDEFINED
            return JS_TRUE if isinstance(val, JsValue) and val.type == 'promise' else JS_FALSE

        def _is_regexp(this_val, args, interp_inner):
            val = args[0] if args else UNDEFINED
            if isinstance(val, JsValue) and val.type == 'regexp':
                return JS_TRUE
            if (isinstance(val, JsValue) and val.type == 'object'
                    and isinstance(val.value, dict)):
                kind = val.value.get('__kind__')
                if isinstance(kind, JsValue) and kind.value == 'RegExp':
                    return JS_TRUE
            return JS_FALSE

        def _is_date(this_val, args, interp_inner):
            val = args[0] if args else UNDEFINED
            if isinstance(val, JsValue) and val.type == 'object' and isinstance(val.value, dict):
                return JS_TRUE if val.value.get('__is_date__') else JS_FALSE
            return JS_FALSE

        def _is_map(this_val, args, interp_inner):
            val = args[0] if args else UNDEFINED
            if isinstance(val, JsValue) and val.type == 'object' and isinstance(val.value, dict):
                kind = val.value.get('__kind__')
                if isinstance(kind, JsValue) and kind.value == 'Map':
                    return JS_TRUE
            return JS_FALSE

        def _is_set(this_val, args, interp_inner):
            val = args[0] if args else UNDEFINED
            if isinstance(val, JsValue) and val.type == 'object' and isinstance(val.value, dict):
                kind = val.value.get('__kind__')
                if isinstance(kind, JsValue) and kind.value == 'Set':
                    return JS_TRUE
            return JS_FALSE

        types_obj = JsValue('object', {
            'isPromise': ctx.make_function(_is_promise, 'util.types.isPromise'),
            'isRegExp': ctx.make_function(_is_regexp, 'util.types.isRegExp'),
            'isDate': ctx.make_function(_is_date, 'util.types.isDate'),
            'isMap': ctx.make_function(_is_map, 'util.types.isMap'),
            'isSet': ctx.make_function(_is_set, 'util.types.isSet'),
        })

        util_methods = {
            'inspect': inspect_fn,
            'format': format_fn,
            'promisify': promisify_fn,
            'isDeepStrictEqual': deep_equal_fn,
        }

        util_obj = ctx.add_global_object('util', util_methods)
        util_obj.value['types'] = types_obj
