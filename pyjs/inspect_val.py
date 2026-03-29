"""
pyjs.inspect_val — Node.js-style `util.inspect` for JsValue objects.

Used by the REPL to display expression results in a human-readable,
colourised format similar to Node.js's interactive shell.
"""
from __future__ import annotations
import math
from .values import JsValue, UNDEFINED, JS_NULL, JS_TRUE, JS_FALSE

# ANSI colour codes (disabled when not a TTY)
_C = {
    'reset':   '\033[0m',
    'bold':    '\033[1m',
    'dim':     '\033[2m',
    'red':     '\033[31m',
    'green':   '\033[32m',
    'yellow':  '\033[33m',
    'blue':    '\033[34m',
    'magenta': '\033[35m',
    'cyan':    '\033[36m',
    'white':   '\033[37m',
}

def _c(color: str, text: str, use_color: bool) -> str:
    if not use_color:
        return text
    return _C.get(color, '') + text + _C['reset']


def js_inspect(
    val: JsValue,
    interp=None,
    *,
    depth: int = 2,
    colors: bool = True,
    _seen: set | None = None,
    _current_depth: int = 0,
    compact: bool = True,
) -> str:
    """Return a Node.js-like string representation of a JsValue.

    Parameters
    ----------
    val:            The JsValue to inspect.
    interp:         Interpreter instance (needed for _to_str fallback).
    depth:          Max nesting depth for objects/arrays.
    colors:         Whether to emit ANSI colour codes.
    compact:        Use compact single-line form for small collections.
    """
    if _seen is None:
        _seen = set()

    t = val.type

    # Primitives
    if t == 'undefined':
        return _c('dim', 'undefined', colors)
    if t == 'null':
        return _c('bold', 'null', colors)
    if t == 'boolean':
        return _c('yellow', str(val.value).lower(), colors)
    if t == 'number':
        n = val.value
        if isinstance(n, float):
            if math.isnan(n):
                s = 'NaN'
            elif math.isinf(n):
                s = 'Infinity' if n > 0 else '-Infinity'
            elif n == int(n) and abs(n) < 1e15:
                s = str(int(n))
            else:
                s = str(n)
        else:
            s = str(n)
        return _c('yellow', s, colors)
    if t == 'bigint':
        return _c('yellow', f"{val.value}n", colors)
    if t == 'string':
        # Escape and quote like Node.js
        escaped = (val.value
                   .replace('\\', '\\\\')
                   .replace("'", "\\'")
                   .replace('\n', '\\n')
                   .replace('\r', '\\r')
                   .replace('\t', '\\t'))
        return _c('green', f"'{escaped}'", colors)
    if t == 'symbol':
        return _c('green', f"Symbol({val.value})", colors)
    if t == 'regexp':
        return _c('red', f"/{val.value.get('source','')}/{val.value.get('flags','')}", colors)
    if t == 'promise':
        state = val.value.get('state', 'pending')
        if state == 'fulfilled':
            inner = js_inspect(val.value.get('value', UNDEFINED), interp,
                               depth=depth, colors=colors, _seen=_seen,
                               _current_depth=_current_depth + 1, compact=compact)
            return _c('cyan', f"Promise {{ {inner} }}", colors)
        elif state == 'rejected':
            inner = js_inspect(val.value.get('value', UNDEFINED), interp,
                               depth=depth, colors=colors, _seen=_seen,
                               _current_depth=_current_depth + 1, compact=compact)
            return _c('cyan', f"Promise {{ <rejected> {inner} }}", colors)
        return _c('cyan', 'Promise { <pending> }', colors)

    if t in ('function', 'intrinsic', 'class'):
        name = ''
        if isinstance(val.value, dict):
            name = val.value.get('name', '') or ''
        if t == 'class':
            label = f"[class {name}]" if name else '[class (anonymous)]'
        else:
            label = f"[Function: {name}]" if name else '[Function (anonymous)]'
        return _c('cyan', label, colors)

    if t == 'array':
        obj_id = id(val)
        if obj_id in _seen:
            return _c('cyan', '[Circular *]', colors)
        if not isinstance(val.value, list):
            return '[]'
        n = len(val.value)
        # DevTools-style length prefix: (3) [ 1, 2, 3 ]
        len_tag = _c('dim', f'({n}) ', colors) if n else ''
        if n == 0:
            return '[]'
        if _current_depth >= depth:
            return len_tag + _c('cyan', f'[ ... {n} more items ]', colors)
        _seen.add(obj_id)
        try:
            items = [
                js_inspect(item, interp, depth=depth, colors=colors,
                           _seen=_seen, _current_depth=_current_depth + 1, compact=compact)
                for item in val.value
            ]
        finally:
            _seen.discard(obj_id)
        inner = ', '.join(items)
        if compact and len(inner) <= 72:
            return f'{len_tag}[ {inner} ]'
        indent = '  ' * (_current_depth + 1)
        lines = ',\n'.join(f"{indent}{item}" for item in items)
        close_indent = '  ' * _current_depth
        return f'{len_tag}[\n{lines}\n{close_indent}]'

    if t == 'object':
        obj_id = id(val)
        if obj_id in _seen:
            return _c('cyan', '[Circular *]', colors)
        if not isinstance(val.value, dict):
            return '{}'

        # Error objects
        err_type = val.value.get('__error_type__')
        if isinstance(err_type, JsValue) and err_type.type == 'string':
            msg = val.value.get('message')
            msg_str = msg.value if isinstance(msg, JsValue) else ''
            stack = val.value.get('stack')
            stack_str = stack.value if isinstance(stack, JsValue) else ''
            header = _c('red', f"{err_type.value}: {msg_str}", colors)
            if stack_str and '\n' in stack_str:
                frames = stack_str.split('\n')[1:]
                frame_lines = '\n'.join(_c('dim', f"  {f.strip()}", colors) for f in frames if f.strip())
                return f"{header}\n{frame_lines}" if frame_lines else header
            return header

        # Generator / special kinds
        kind = val.value.get('__kind__')
        if isinstance(kind, JsValue):
            if kind.value == 'Generator':
                return _c('cyan', 'Object [Generator] {}', colors)
            if kind.value == 'Map':
                store = val.value.get('__store__', [])
                n = len(store)
                if n == 0:
                    return _c('cyan', 'Map(0) {}', colors)
                if _current_depth >= depth:
                    return _c('cyan', f'Map({n}) {{ ... }}', colors)
                pairs = []
                for k_val, v_val in store:
                    ks = js_inspect(k_val, interp, depth=depth, colors=colors,
                                    _seen=_seen, _current_depth=_current_depth+1, compact=compact)
                    vs = js_inspect(v_val, interp, depth=depth, colors=colors,
                                    _seen=_seen, _current_depth=_current_depth+1, compact=compact)
                    pairs.append(f"{ks} => {vs}")
                inner = ', '.join(pairs)
                header = _c('cyan', f'Map({n})', colors)
                if compact and len(inner) <= 60:
                    return f'{header} {{ {inner} }}'
                indent = '  ' * (_current_depth + 1)
                lines = ',\n'.join(f"{indent}{p}" for p in pairs)
                ci = '  ' * _current_depth
                return f'{header} {{\n{lines}\n{ci}}}'
            if kind.value == 'Set':
                store = val.value.get('__store__', [])
                n = len(store)
                if n == 0:
                    return _c('cyan', 'Set(0) {}', colors)
                if _current_depth >= depth:
                    return _c('cyan', f'Set({n}) {{ ... }}', colors)
                items_str = ', '.join(
                    js_inspect(item, interp, depth=depth, colors=colors,
                               _seen=_seen, _current_depth=_current_depth+1, compact=compact)
                    for item in store
                )
                header = _c('cyan', f'Set({n})', colors)
                return f'{header} {{ {items_str} }}'

        # Class instance — show constructor name prefix like DevTools: `ClassName { ... }`
        class_name = ''
        cn_tag = val.value.get('__class_name__')
        if isinstance(cn_tag, JsValue) and cn_tag.type == 'string':
            class_name = cn_tag.value

        if _current_depth >= depth:
            prefix = _c('cyan', class_name + ' ', colors) if class_name else ''
            return prefix + _c('dim', '[Object]', colors)

        _seen.add(obj_id)
        try:
            pairs = []
            for k, v in val.value.items():
                if k.startswith('__') and k.endswith('__'):
                    continue
                if k.startswith('@@'):
                    continue
                key_str = _c('white', k, colors) if colors else k
                val_str = js_inspect(v, interp, depth=depth, colors=colors,
                                     _seen=_seen, _current_depth=_current_depth + 1, compact=compact)
                pairs.append(f"{key_str}: {val_str}")
        finally:
            _seen.discard(obj_id)

        name_prefix = (_c('cyan', class_name, colors) + ' ') if class_name else ''

        if not pairs:
            return f'{name_prefix}{{}}'
        inner = ', '.join(pairs)
        if compact and len(inner) <= 72:
            return f'{name_prefix}{{ {inner} }}'
        indent = '  ' * (_current_depth + 1)
        lines = ',\n'.join(f"{indent}{p}" for p in pairs)
        close_indent = '  ' * _current_depth
        return f'{name_prefix}{{\n{lines}\n{close_indent}}}'

    # Fallback
    if interp is not None:
        return interp._to_str(val)
    return str(val.value)
