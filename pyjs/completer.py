"""
pyjs.completer — DevTools-style property-aware tab-completion for the REPL.

Features
--------
* Property completion after `obj.` — evaluates `obj` live in the JS env and
  lists its enumerable keys / built-in method names.
* Chained access: `a.b.c.` works correctly.
* Bracket string literals: `obj["` shows string-key suggestions.
* Type-aware method lists: bare `"`.`, `[`.`, `(`.` before any identifier
  fall back to String / Array / Number built-in methods.
* Keyword / dot-command completion for bare text.
* Custom ``display_matches_hook``: each completion is annotated with its JS
  type in brackets, mirroring Chrome DevTools' completion popup.
* Eager-evaluation preview: when Tab is pressed on a syntactically-complete
  expression, a dim preview line is shown *above* the cursor (DevTools style).
"""
from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING

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
        return f'fn:{name}' if name else 'fn'
    if t == 'class':
        name = val.value.get('name', '') if isinstance(val.value, dict) else ''
        return f'class:{name}' if name else 'class'
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
        return 'obj'
    if t == 'promise':
        return f'Promise<{val.value.get("state","?")}>' if isinstance(val.value, dict) else 'Promise'
    return t


# ── Built-in method name lists ────────────────────────────────────────────────

_STRING_METHODS = [
    'charAt', 'charCodeAt', 'codePointAt', 'concat', 'endsWith',
    'includes', 'indexOf', 'lastIndexOf', 'localeCompare', 'match',
    'matchAll', 'normalize', 'padEnd', 'padStart', 'repeat', 'replace',
    'replaceAll', 'search', 'slice', 'split', 'startsWith', 'substring',
    'toLocaleLowerCase', 'toLocaleUpperCase', 'toLowerCase', 'toString',
    'toUpperCase', 'toWellFormed', 'trim', 'trimEnd', 'trimStart',
    'valueOf', 'at', 'isWellFormed',
    # property
    'length',
]

_ARRAY_METHODS = [
    'at', 'concat', 'copyWithin', 'entries', 'every', 'fill', 'filter',
    'find', 'findIndex', 'findLast', 'findLastIndex', 'flat', 'flatMap',
    'forEach', 'from', 'includes', 'indexOf', 'isArray', 'join', 'keys',
    'lastIndexOf', 'map', 'of', 'pop', 'push', 'reduce', 'reduceRight',
    'reverse', 'shift', 'slice', 'some', 'sort', 'splice', 'toReversed',
    'toSorted', 'toSpliced', 'unshift', 'values', 'with',
    # property
    'length',
]

_NUMBER_METHODS = [
    'toExponential', 'toFixed', 'toLocaleString', 'toPrecision',
    'toString', 'valueOf',
    # statics  (on Number object)
    'isFinite', 'isInteger', 'isNaN', 'isSafeInteger',
    'parseFloat', 'parseInt', 'MAX_VALUE', 'MIN_VALUE',
    'NEGATIVE_INFINITY', 'POSITIVE_INFINITY', 'NaN',
    'MAX_SAFE_INTEGER', 'MIN_SAFE_INTEGER', 'EPSILON',
]

_OBJECT_STATIC_METHODS = [
    'assign', 'create', 'defineProperties', 'defineProperty', 'entries',
    'freeze', 'fromEntries', 'getOwnPropertyDescriptor',
    'getOwnPropertyDescriptors', 'getOwnPropertyNames',
    'getOwnPropertySymbols', 'getPrototypeOf', 'hasOwn', 'is',
    'isFrozen', 'isSealed', 'keys', 'seal', 'setPrototypeOf', 'values',
]

_PROMISE_METHODS = [
    'then', 'catch', 'finally',
    # statics
    'all', 'allSettled', 'any', 'race', 'reject', 'resolve',
]

_JS_KEYWORDS = [
    'async', 'await', 'break', 'case', 'catch', 'class', 'const',
    'continue', 'debugger', 'default', 'delete', 'do', 'else', 'export',
    'extends', 'false', 'finally', 'for', 'function', 'if', 'import',
    'in', 'instanceof', 'let', 'new', 'null', 'of', 'return', 'static',
    'super', 'switch', 'this', 'throw', 'true', 'try', 'typeof', 'undefined',
    'var', 'void', 'while', 'yield',
]

_DOT_COMMANDS = [
    '.help', '.exit', '.clear', '.break', '.stack', '.version',
    '.load ', '.save ',
]


# ── Completer ─────────────────────────────────────────────────────────────────

# Matches `something.prefix` where `something` can be a chained access
_DOT_RE = re.compile(
    r'((?:[a-zA-Z_$][\w$]*'            # identifier start
    r'(?:\.[\w$]+|\[(?:"[^"]*"|\'[^\']*\'|\d+)\])*)'  # optional chain
    r')\.([\w$]*)$'
)

# Matches `"string literal".prefix`
_STR_LIT_DOT_RE = re.compile(r'(?:"[^"]*"|\'[^\']*\')\.([\w$]*)$')
# Matches `[...array literal...].prefix`
_ARR_LIT_DOT_RE = re.compile(r'\[[^\]]*\]\.([\w$]*)$')
# Matches `(number).prefix`
_NUM_LIT_DOT_RE = re.compile(r'\(?\d+(?:\.\d+)?\)?\.([\w$]*)$')


class JsCompleter:
    """Property-aware tab completer for PyJS REPL — mirrors Chrome DevTools."""

    def __init__(self, interp: 'Interpreter') -> None:
        self.interp = interp
        self._cache: dict = {}     # maps expr_text -> (result_str, [props])
        self._matches: list[str] = []
        self._preview: str | None = None   # set during complete(), used by display hook
        self._use_color = sys.stdout.isatty()

    # ── Public readline interface ─────────────────────────────────────────────

    def complete(self, text: str, state: int) -> str | None:
        """readline completer callback — called with state=0,1,2,... until None."""
        if state == 0:
            try:
                import readline as _rl
                buf = _rl.get_line_buffer()
            except ImportError:
                buf = text
            self._matches = self._build_matches(text, buf)
            self._preview = None
        return self._matches[state] if state < len(self._matches) else None

    def display_matches_hook(self, substitution: str, matches: list[str], max_len: int) -> None:
        """Custom completion display — shows type annotations beside each match.

        Called by readline instead of its default match display.
        """
        import readline as _rl

        use_color = self._use_color
        _R = '\033[0m'
        _DIM = '\033[2m'
        _CYAN = '\033[36m'
        _YEL = '\033[33m'

        if not matches:
            return

        # Annotate each match with its type tag
        annotated: list[tuple[str, str]] = []
        for m in matches:
            val = self._resolve_prop_val(substitution, m)
            tag = _type_tag(val) if val is not None else ''
            annotated.append((m, tag))

        # Format as columns
        col_w = max(len(m) for m, _ in annotated) + 2
        tag_w = max((len(t) for _, t in annotated), default=0)
        cols = max(1, 72 // (col_w + tag_w + 4))
        rows = (len(annotated) + cols - 1) // cols

        sys.stdout.write('\n')
        for row in range(rows):
            line = ''
            for col in range(cols):
                idx = col * rows + row
                if idx >= len(annotated):
                    break
                name, tag = annotated[idx]
                if use_color:
                    entry = f'{_CYAN}{name:<{col_w}}{_R}'
                    if tag:
                        entry += f'{_DIM}[{tag}]{_R}'
                else:
                    entry = f'{name:<{col_w}}'
                    if tag:
                        entry += f'[{tag}]'
                line += f'  {entry}'
            sys.stdout.write(line + '\n')

        # Eager-evaluation preview line
        preview = self._eager_preview()
        if preview:
            if use_color:
                sys.stdout.write(f'\n  {_DIM}→ {preview}{_R}\n')
            else:
                sys.stdout.write(f'\n  → {preview}\n')

        sys.stdout.write('\n')
        # Force readline to redraw the prompt+buffer
        _rl.redisplay()

    # ── Match builder ─────────────────────────────────────────────────────────

    def _build_matches(self, text: str, buf: str) -> list[str]:
        """Compute candidate completions for the current readline buffer."""

        # ── Dot commands (.exit, .help …) ──────────────────────────────────
        if text.startswith('.'):
            return [d for d in _DOT_COMMANDS if d.startswith(text)]

        # ── String literal followed by `.` ─────────────────────────────────
        m = _STR_LIT_DOT_RE.search(buf)
        if m:
            prefix = m.group(1)
            return [n for n in _STRING_METHODS if n.startswith(prefix)]

        # ── Array literal followed by `.` ──────────────────────────────────
        m = _ARR_LIT_DOT_RE.search(buf)
        if m:
            prefix = m.group(1)
            return [n for n in _ARRAY_METHODS if n.startswith(prefix)]

        # ── Number literal followed by `.` ─────────────────────────────────
        m = _NUM_LIT_DOT_RE.search(buf)
        if m:
            prefix = m.group(1)
            return [n for n in _NUMBER_METHODS if n.startswith(prefix)]

        # ── Identifier chain followed by `.prefix` ─────────────────────────
        m = _DOT_RE.search(buf)
        if m:
            obj_expr, prefix = m.group(1), m.group(2)
            props = self._props_from_expr(obj_expr)
            return sorted(p for p in props if p.startswith(prefix))

        # ── Bare identifier / keyword completion ───────────────────────────
        candidates = list(self._all_globals()) + _JS_KEYWORDS
        return sorted(set(c for c in candidates if c.startswith(text)))

    # ── Property helpers ─────────────────────────────────────────────────────

    def _all_globals(self) -> list[str]:
        env = self.interp.genv
        names = []
        cur = env
        while cur is not None:
            names.extend(cur.bindings.keys())
            cur = getattr(cur, 'parent', None)
        return names

    def _eval_expr_safe(self, expr: str) -> JsValue | None:
        """Evaluate a JS expression string in the current env, return None on any error."""
        cache_key = (expr, id(self.interp.genv))
        if cache_key in self._cache:
            return self._cache[cache_key]
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
            self._cache[cache_key] = val
            return val
        except Exception:
            return None

    def _props_from_expr(self, obj_expr: str) -> list[str]:
        """Return property names of the object that `obj_expr` evaluates to."""
        val = self._eval_expr_safe(obj_expr)
        if val is None:
            return []
        return self._props_of(val)

    def _props_of(self, val: JsValue) -> list[str]:
        """Return all JS-visible property/method names for a value."""
        props: list[str] = []
        t = val.type

        if t == 'string':
            return list(_STRING_METHODS)
        if t == 'number':
            return list(_NUMBER_METHODS)
        if t == 'array':
            props = list(_ARRAY_METHODS)
            if isinstance(val.value, list):
                props += [str(i) for i in range(len(val.value))]
            return props
        if t in ('function', 'intrinsic', 'class'):
            props = ['call', 'apply', 'bind', 'name', 'length', 'prototype']
            if isinstance(val.value, dict):
                props += [k for k in val.value.keys()
                          if not k.startswith('__') and not k.startswith('@@')]
            return props
        if t == 'promise':
            return list(_PROMISE_METHODS)
        if t == 'object':
            if isinstance(val.value, dict):
                kind = val.value.get('__kind__')
                if isinstance(kind, JsValue) and kind.value == 'Map':
                    props = ['get', 'set', 'has', 'delete', 'clear',
                             'keys', 'values', 'entries', 'forEach', 'size']
                elif isinstance(kind, JsValue) and kind.value == 'Set':
                    props = ['add', 'has', 'delete', 'clear',
                             'keys', 'values', 'entries', 'forEach', 'size']
                else:
                    props = [k for k in val.value.keys()
                             if not k.startswith('__') and not k.startswith('@@')]
                    props += ['hasOwnProperty', 'toString', 'valueOf',
                              'constructor', 'isPrototypeOf', 'propertyIsEnumerable']
            return props
        return []

    def _resolve_prop_val(self, base_expr: str, prop_name: str) -> JsValue | None:
        """Get the value of `base_expr.prop_name` for annotation."""
        val = self._eval_expr_safe(base_expr)
        if val is None or not isinstance(val.value, dict):
            return None
        raw = val.value.get(prop_name)
        return raw if isinstance(raw, JsValue) else None

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
            # Keep it short
            if len(preview) > 60:
                preview = preview[:57] + '...'
            return preview
        except Exception:
            return ''
