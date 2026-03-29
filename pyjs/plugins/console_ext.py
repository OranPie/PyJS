"""Extended console methods plugin for PyJS."""
from __future__ import annotations

from ..plugin import PyJSPlugin, PluginContext
from ..values import JsValue, UNDEFINED
from ..core import py_to_js


class ConsoleExtPlugin(PyJSPlugin):
    name = "console-ext"
    version = "1.0.0"

    def setup(self, ctx: PluginContext) -> None:
        interp = ctx.get_interpreter()
        console = interp.genv.get('console')

        def _output(interp_inner, line):
            indent = '  ' * interp_inner._console_indent
            interp_inner.output.append(indent + line)
            print(indent + line)

        def table_fn(this_val, args, interp_inner):
            if not args or args[0].type in ('undefined', 'null'):
                _output(interp_inner, interp_inner._to_str(args[0]) if args else 'undefined')
                return UNDEFINED

            data = args[0]

            if data.type == 'array' and isinstance(data.value, list):
                _format_array_table(data, interp_inner, _output)
            elif data.type == 'object' and isinstance(data.value, dict):
                _format_object_table(data, interp_inner, _output)
            else:
                _output(interp_inner, interp_inner._to_str(data))
            return UNDEFINED

        def assert_fn(this_val, args, interp_inner):
            if not args or not interp_inner._truthy(args[0]):
                msgs = [interp_inner._to_str(a) for a in args[1:]] if len(args) > 1 else []
                msg = 'Assertion failed: ' + ' '.join(msgs) if msgs else 'Assertion failed'
                _output(interp_inner, msg)
            return UNDEFINED

        def trace_fn(this_val, args, interp_inner):
            parts = [interp_inner._to_str(a) for a in args]
            label = ' '.join(parts) if parts else ''
            line = 'Trace: ' + label if label else 'Trace'
            _output(interp_inner, line)
            return UNDEFINED

        def dir_fn(this_val, args, interp_inner):
            if not args:
                _output(interp_inner, 'undefined')
                return UNDEFINED

            obj = args[0]
            depth = 2
            if len(args) > 1 and args[1].type == 'object' and isinstance(args[1].value, dict):
                d = args[1].value.get('depth')
                if d and isinstance(d, JsValue) and d.type == 'number':
                    depth = int(d.value)

            formatted = _format_dir(obj, interp_inner, depth, 0)
            _output(interp_inner, formatted)
            return UNDEFINED

        console.value['table'] = interp._make_intrinsic(table_fn, 'console.table')
        console.value['assert'] = interp._make_intrinsic(assert_fn, 'console.assert')
        console.value['trace'] = interp._make_intrinsic(trace_fn, 'console.trace')
        console.value['dir'] = interp._make_intrinsic(dir_fn, 'console.dir')


def _format_array_table(data, interp, output_fn):
    """Format an array as an ASCII table."""
    rows = []
    all_keys = set()

    for i, item in enumerate(data.value):
        if isinstance(item, JsValue) and item.type == 'object' and isinstance(item.value, dict):
            row = {}
            for k, v in item.value.items():
                if k.startswith('__'):
                    continue
                row[k] = interp._to_str(v)
                all_keys.add(k)
            rows.append((str(i), row))
        else:
            rows.append((str(i), {'Values': interp._to_str(item)}))
            all_keys.add('Values')

    if not rows:
        return

    columns = sorted(all_keys)
    _print_table(rows, columns, output_fn, interp)


def _format_object_table(data, interp, output_fn):
    """Format an object as an ASCII table."""
    rows = []
    all_keys = set()

    for k, v in data.value.items():
        if k.startswith('__'):
            continue
        if isinstance(v, JsValue) and v.type == 'object' and isinstance(v.value, dict):
            row = {}
            for ik, iv in v.value.items():
                if ik.startswith('__'):
                    continue
                row[ik] = interp._to_str(iv)
                all_keys.add(ik)
            rows.append((k, row))
        else:
            rows.append((k, {'Values': interp._to_str(v)}))
            all_keys.add('Values')

    if not rows:
        return

    columns = sorted(all_keys)
    _print_table(rows, columns, output_fn, interp)


def _print_table(rows, columns, output_fn, interp):
    """Print rows as an ASCII table."""
    headers = ['(index)'] + columns

    # Compute column widths
    widths = [len(h) for h in headers]
    for idx_str, row_data in rows:
        widths[0] = max(widths[0], len(idx_str))
        for ci, col in enumerate(columns):
            val = row_data.get(col, '')
            widths[ci + 1] = max(widths[ci + 1], len(val))

    def fmt_row(cells):
        parts = []
        for i, cell in enumerate(cells):
            parts.append(cell.ljust(widths[i]))
        return '| ' + ' | '.join(parts) + ' |'

    sep = '+-' + '-+-'.join('-' * w for w in widths) + '-+'

    output_fn(interp, sep)
    output_fn(interp, fmt_row(headers))
    output_fn(interp, sep)
    for idx_str, row_data in rows:
        cells = [idx_str] + [row_data.get(col, '') for col in columns]
        output_fn(interp, fmt_row(cells))
    output_fn(interp, sep)


def _format_dir(val, interp, max_depth, current_depth):
    """Recursively format a value for console.dir."""
    if not isinstance(val, JsValue):
        return repr(val)

    if val.type in ('undefined', 'null', 'boolean', 'number', 'string', 'bigint', 'symbol'):
        if val.type == 'string':
            return f"'{val.value}'"
        return interp._to_str(val)

    if val.type in ('function', 'intrinsic', 'class'):
        name = ''
        if isinstance(val.value, dict):
            name = val.value.get('name', '')
        return f'[Function: {name}]'

    if current_depth >= max_depth:
        if val.type == 'array':
            return '[Array]'
        return '[Object]'

    if val.type == 'array' and isinstance(val.value, list):
        if not val.value:
            return '[]'
        items = [_format_dir(v, interp, max_depth, current_depth + 1) for v in val.value]
        return '[ ' + ', '.join(items) + ' ]'

    if val.type == 'object' and isinstance(val.value, dict):
        if not val.value:
            return '{}'
        parts = []
        for k, v in val.value.items():
            if k.startswith('__'):
                continue
            parts.append(f'{k}: {_format_dir(v, interp, max_depth, current_depth + 1)}')
        if not parts:
            return '{}'
        return '{ ' + ', '.join(parts) + ' }'

    return interp._to_str(val)
