"""
pyjs.completer — DevTools-style property-aware tab-completion for the REPL.

Features
--------
* Property completion after ``obj.`` — evaluates ``obj`` live in the JS env and
  lists its enumerable keys / built-in method names.
* Chained access: ``a.b.c.`` works correctly.
* Type-aware method lists: string/array/number literals complete built-in methods.
* Keyword / dot-command completion for bare text.
* Custom ``display_matches_hook``: each completion is annotated with its JS
  type in brackets, mirroring Chrome DevTools' completion popup.
* Eager-evaluation preview: dim preview line shown when the buffer contains a
  syntactically-complete expression.
"""
from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .runtime import Interpreter
from .values import JsValue, UNDEFINED


# ── Type tag helpers ─────────────────────────────────────────────────────────

def _type_tag(val: JsValue) -> str:
    """Return a short display tag for a JsValue (used in completion popup)."""
    t = val.type
    if t in ('undefined', 'null'):
        return t
    if t == 'boolean':
        return 'bool'
    if t == 'number':
        return 'num'
    if t == 'bigint':
        return 'bigint'
    if t == 'string':
        return 'str'
    if t == 'symbol':
        return 'sym'
    if t == 'regexp':
        return 'RegExp'
    if t in ('function', 'intrinsic'):
        name = ''
        if isinstance(val.value, dict):
            name = val.value.get('name', '') or ''
        return f'f {name}' if name else 'f'
    if t == 'class':
        name = val.value.get('name', '') if isinstance(val.value, dict) else ''
        return f'class {name}' if name else 'class'
    if t == 'array':
        n = len(val.value) if isinstance(val.value, list) else '?'
        return f'Array({n})'
    if t == 'object':
        if isinstance(val.value, dict):
            kind = val.value.get('__kind__')
            if isinstance(kind, JsValue):
                return kind.value
            err_type = val.value.get('__error_type__')
            if isinstance(err_type, JsValue):
                return err_type.value
            cn = val.value.get('__class_name__')
            if isinstance(cn, JsValue) and cn.type == 'string':
                return cn.value
        return 'obj'
    if t == 'promise':
        return 'Promise'
    return t


# ── Built-in method name lists ────────────────────────────────────────────────

_STRING_METHODS = sorted([
    'charAt', 'charCodeAt', 'codePointAt', 'concat', 'endsWith',
    'includes', 'indexOf', 'lastIndexOf', 'localeCompare', 'match',
    'matchAll', 'normalize', 'padEnd', 'padStart', 'repeat', 'replace',
    'replaceAll', 'search', 'slice', 'split', 'startsWith', 'substring',
    'toLocaleLowerCase', 'toLocaleUpperCase', 'toLowerCase', 'toString',
    'toUpperCase', 'toWellFormed', 'trim', 'trimEnd', 'trimStart',
    'valueOf', 'at', 'isWellFormed', 'length',
])

_ARRAY_METHODS = sorted([
    'at', 'concat', 'copyWithin', 'entries', 'every', 'fill', 'filter',
    'find', 'findIndex', 'findLast', 'findLastIndex', 'flat', 'flatMap',
    'forEach', 'from', 'includes', 'indexOf', 'isArray', 'join', 'keys',
    'lastIndexOf', 'length', 'map', 'of', 'pop', 'push', 'reduce',
    'reduceRight', 'reverse', 'shift', 'slice', 'some', 'sort', 'splice',
    'toReversed', 'toSorted', 'toSpliced', 'unshift', 'values', 'with',
])

_NUMBER_METHODS = sorted([
    'toExponential', 'toFixed', 'toLocaleString', 'toPrecision',
    'toString', 'valueOf',
])

_OBJECT_PROTO_METHODS = sorted([
    'hasOwnProperty', 'toString', 'valueOf',
])

_PROMISE_METHODS = sorted([
    'then', 'catch', 'finally',
])

_JS_KEYWORDS = sorted([
    'async', 'await', 'break', 'case', 'catch', 'class', 'const',
    'continue', 'debugger', 'default', 'delete', 'do', 'else', 'export',
    'extends', 'false', 'finally', 'for', 'function', 'if', 'import',
    'in', 'instanceof', 'let', 'new', 'null', 'of', 'return', 'static',
    'super', 'switch', 'this', 'throw', 'true', 'try', 'typeof', 'undefined',
    'var', 'void', 'while', 'yield',
])

_DOT_COMMANDS = [
    '.help', '.exit', '.clear', '.break', '.stack', '.version',
    '.load ', '.save ',
]


# ── Completer ─────────────────────────────────────────────────────────────────

# Matches `something.prefix` where `something` can be a chained access
_DOT_RE = re.compile(
    r'((?:[a-zA-Z_$][\w$]*'
    r'(?:\.[\w$]+|\[(?:"[^"]*"|\'[^\']*\'|\d+)\])*'
    r'(?:\([^)]*\))?'
    r'))\.([\w$]*)$'
)

_STR_LIT_DOT_RE = re.compile(r'(?:"[^"]*"|\'[^\']*\')\.([\w$]*)$')
_ARR_LIT_DOT_RE = re.compile(r'\[[^\]]*\]\.([\w$]*)$')
_NUM_LIT_DOT_RE = re.compile(r'\(?\d+(?:\.\d+)?\)?\.([\w$]*)$')


class JsCompleter:
    """Property-aware tab completer for PyJS REPL — mirrors Chrome DevTools."""

    def __init__(self, interp: 'Interpreter') -> None:
        self.interp = interp
        self._cache: dict[str, Optional[JsValue]] = {}
        self._matches: list[str] = []
        self._last_base_expr: str = ''
        self._use_color = sys.stdout.isatty()

    def invalidate(self) -> None:
        """Clear cached evaluations — call after each REPL execution."""
        self._cache.clear()

    # ── Public readline interface ─────────────────────────────────────────────

    def complete(self, text: str, state: int) -> str | None:
        """readline completer callback — called with state=0,1,2,... until None."""
        if state == 0:
            try:
                import readline as _rl
                buf = _rl.get_line_buffer()
            except ImportError:
                buf = text
            try:
                self._matches, self._last_base_expr = self._build_matches(text, buf)
            except Exception:
                self._matches, self._last_base_expr = [], ''
        return self._matches[state] if state < len(self._matches) else None

    def display_matches_hook(self, substitution: str, matches: list[str],
                             max_len: int) -> None:
        """Custom completion display — shows type annotations beside each match."""
        try:
            self._display_matches_impl(substitution, matches, max_len)
        except Exception:
            # Fallback: just print names plainly so the REPL doesn't crash
            try:
                import readline as _rl
                sys.stdout.write('\n')
                for m in matches:
                    sys.stdout.write(f'  {m}\n')
                sys.stdout.flush()
                _rl.redisplay()
            except Exception:
                pass

    def _display_matches_impl(self, substitution: str, matches: list[str],
                              max_len: int) -> None:
        """Inner implementation of display_matches_hook."""
        import readline as _rl

        use_color = self._use_color
        _R = '\033[0m'
        _DIM = '\033[2m'
        _CYAN = '\033[36m'

        if not matches:
            return

        # Annotate each match with a type tag
        annotated: list[tuple[str, str]] = []
        for name in matches:
            tag = self._tag_for_match(name)
            annotated.append((name, tag))

        # Column layout using plain-text widths (no ANSI)
        name_w = max(len(n) for n, _ in annotated) + 2
        tag_w = max((len(t) + 2 for _, t in annotated if t), default=0)
        cell_w = name_w + tag_w + 2
        term_w = _get_terminal_width()
        cols = max(1, term_w // cell_w)
        rows = (len(annotated) + cols - 1) // cols

        sys.stdout.write('\n')
        for row in range(rows):
            parts: list[str] = []
            for col in range(cols):
                idx = col * rows + row
                if idx >= len(annotated):
                    break
                name, tag = annotated[idx]
                if use_color and tag:
                    parts.append(f'  {_CYAN}{name:<{name_w}}{_DIM}[{tag}]{_R}')
                elif use_color:
                    parts.append(f'  {_CYAN}{name:<{name_w}}{_R}')
                elif tag:
                    parts.append(f'  {name:<{name_w}}[{tag}]')
                else:
                    parts.append(f'  {name:<{name_w}}')
            sys.stdout.write(''.join(parts) + '\n')

        # Eager-evaluation preview
        preview = self._eager_preview()
        if preview:
            if use_color:
                sys.stdout.write(f'  {_DIM}\u2192 {preview}{_R}\n')
            else:
                sys.stdout.write(f'  \u2192 {preview}\n')

        sys.stdout.flush()
        _rl.redisplay()

    # ── Match builder ─────────────────────────────────────────────────────────

    def _build_matches(self, text: str, buf: str) -> tuple[list[str], str]:
        """Return (completions, base_expression) for the current buffer."""

        # Dot commands (.exit, .help)
        if text.startswith('.'):
            return [d for d in _DOT_COMMANDS if d.startswith(text)], ''

        # String literal followed by `.`
        m = _STR_LIT_DOT_RE.search(buf)
        if m:
            prefix = m.group(1)
            return [n for n in _STRING_METHODS if n.startswith(prefix)], ''

        # Array literal followed by `.`
        m = _ARR_LIT_DOT_RE.search(buf)
        if m:
            prefix = m.group(1)
            return [n for n in _ARRAY_METHODS if n.startswith(prefix)], ''

        # Number literal followed by `.`
        m = _NUM_LIT_DOT_RE.search(buf)
        if m:
            prefix = m.group(1)
            return [n for n in _NUMBER_METHODS if n.startswith(prefix)], ''

        # Identifier chain followed by `.prefix`
        m = _DOT_RE.search(buf)
        if m:
            obj_expr, prefix = m.group(1), m.group(2)
            props = self._props_from_expr(obj_expr)
            return sorted(p for p in props if p.startswith(prefix)), obj_expr

        # Bare identifier / keyword completion
        candidates = list(self._all_globals()) + _JS_KEYWORDS
        return sorted(set(c for c in candidates if c.startswith(text))), ''

    # ── Type tagging for display hook ─────────────────────────────────────────

    def _tag_for_match(self, name: str) -> str:
        """Return a type tag string for a completion match name."""
        base = self._last_base_expr

        if base:
            val = self._resolve_deep(base, name)
            if val is not None and val.type != 'undefined':
                return _type_tag(val)
            return ''

        # Bare global: look up the name directly
        val = self._eval_expr_safe(name)
        if val is not None and val.type != 'undefined':
            return _type_tag(val)
        return ''

    def _resolve_deep(self, base_expr: str, prop: str) -> JsValue | None:
        """Evaluate `base_expr` and try to get property `prop` from the result."""
        base_val = self._eval_expr_safe(base_expr)
        if base_val is None:
            return None

        # Direct dict lookup (fast path for objects)
        if isinstance(base_val.value, dict):
            raw = base_val.value.get(prop)
            if isinstance(raw, JsValue):
                return raw

        # Evaluate the full expression for non-dict types and missing dict keys
        full = f'({base_expr}).{prop}'
        return self._eval_expr_safe(full)

    # ── Property helpers ─────────────────────────────────────────────────────

    def _all_globals(self) -> list[str]:
        env = self.interp.genv
        names: list[str] = []
        cur = env
        while cur is not None:
            names.extend(cur.bindings.keys())
            cur = getattr(cur, 'parent', None)
        return names

    def _eval_expr_safe(self, expr: str) -> JsValue | None:
        """Evaluate a JS expression in the current env; return None on any error."""
        if expr in self._cache:
            return self._cache[expr]
        try:
            from .lexer import Lexer
            from .parser import Parser
            tokens = Lexer(expr).tokenize()
            ast = Parser(tokens).parse()
            if not ast.get('body'):
                return None
            stmt = ast['body'][0]
            if stmt.get('type') != 'ExpressionStatement':
                return None
            val = self.interp._eval(stmt['expression'], self.interp.genv)
            self._cache[expr] = val
            return val
        except Exception:
            self._cache[expr] = None
            return None

    def _props_from_expr(self, obj_expr: str) -> list[str]:
        """Return property names of the object that `obj_expr` evaluates to."""
        val = self._eval_expr_safe(obj_expr)
        if val is None:
            return []
        return self._props_of(val)

    def _props_of(self, val: JsValue) -> list[str]:
        """Return all JS-visible property/method names for a value."""
        t = val.type

        if t == 'string':
            return list(_STRING_METHODS)
        if t == 'number':
            return list(_NUMBER_METHODS)
        if t == 'array':
            props = list(_ARRAY_METHODS)
            if isinstance(val.value, list):
                props += [str(i) for i in range(min(len(val.value), 20))]
            return props
        if t in ('function', 'intrinsic', 'class'):
            props = ['call', 'apply', 'bind', 'name', 'length', 'prototype']
            if isinstance(val.value, dict):
                props += [k for k in val.value.keys()
                          if not k.startswith('__') and not k.startswith('@@')
                          and k not in ('fn', 'params', 'body', 'env')]
            return sorted(set(props))
        if t == 'promise':
            return list(_PROMISE_METHODS)
        if t == 'object' and isinstance(val.value, dict):
            kind = val.value.get('__kind__')
            if isinstance(kind, JsValue) and kind.value == 'Map':
                return sorted(['get', 'set', 'has', 'delete', 'clear',
                                'keys', 'values', 'entries', 'forEach', 'size'])
            if isinstance(kind, JsValue) and kind.value == 'Set':
                return sorted(['add', 'has', 'delete', 'clear',
                                'keys', 'values', 'entries', 'forEach', 'size'])
            user_keys = [k for k in val.value.keys()
                         if not (k.startswith('__') and k.endswith('__'))
                         and not k.startswith('@@')]
            return sorted(set(user_keys + _OBJECT_PROTO_METHODS))
        return []

    # ── Eager evaluation preview ──────────────────────────────────────────────

    def _eager_preview(self) -> str:
        """Try to evaluate the current readline buffer and return a short preview."""
        try:
            import readline as _rl
            buf = _rl.get_line_buffer().strip()
        except ImportError:
            return ''

        if not buf or buf.startswith('.'):
            return ''

        val = self._eval_expr_safe(buf)
        if val is None:
            return ''

        try:
            from .inspect_val import js_inspect
            preview = js_inspect(val, self.interp, depth=1, colors=False, compact=True)
            if len(preview) > 60:
                preview = preview[:57] + '\u2026'
            return preview
        except Exception:
            return ''


def _get_terminal_width() -> int:
    """Get terminal width, defaulting to 80."""
    try:
        import shutil
        return shutil.get_terminal_size((80, 24)).columns
    except Exception:
        return 80
