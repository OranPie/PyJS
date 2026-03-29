"""
pyjs.colors — Terminal colour/style utilities for the PyJS CLI.

Respects:
  - NO_COLOR  env var  (https://no-color.org/)
  - FORCE_COLOR env var (force enable even when not a TTY)
  - sys.stdout.isatty() auto-detection
"""
from __future__ import annotations
import os
import sys

# ── Auto-detect colour support ──────────────────────────────────────────────
def _supports_color(stream=None) -> bool:
    if stream is None:
        stream = sys.stdout
    if os.environ.get('NO_COLOR'):
        return False
    if os.environ.get('FORCE_COLOR'):
        return True
    return hasattr(stream, 'isatty') and stream.isatty()


# Runtime-settable flag; CLI sets this early based on --color/--no-color
_enabled: bool = _supports_color()


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = value


def is_enabled() -> bool:
    return _enabled


# ── ANSI escape sequences ────────────────────────────────────────────────────
_CODES: dict[str, str] = {
    # Resets
    'reset':   '\033[0m',
    # Styles
    'bold':    '\033[1m',
    'dim':     '\033[2m',
    'italic':  '\033[3m',
    'under':   '\033[4m',
    'blink':   '\033[5m',
    'reverse': '\033[7m',
    'strike':  '\033[9m',
    # Foreground colours
    'black':   '\033[30m',
    'red':     '\033[31m',
    'green':   '\033[32m',
    'yellow':  '\033[33m',
    'blue':    '\033[34m',
    'magenta': '\033[35m',
    'cyan':    '\033[36m',
    'white':   '\033[37m',
    # Bright foreground
    'bred':      '\033[91m',
    'bgreen':    '\033[92m',
    'byellow':   '\033[93m',
    'bblue':     '\033[94m',
    'bmagenta':  '\033[95m',
    'bcyan':     '\033[96m',
    'bwhite':    '\033[97m',
    # Background
    'bg_black':   '\033[40m',
    'bg_red':     '\033[41m',
    'bg_green':   '\033[42m',
    'bg_yellow':  '\033[43m',
    'bg_blue':    '\033[44m',
    'bg_magenta': '\033[45m',
    'bg_cyan':    '\033[46m',
    'bg_white':   '\033[47m',
    'bg_bright_black': '\033[100m',
}


def c(*styles_and_text) -> str:
    """Apply one or more styles to text.

    Usage::
        c('red', 'bold', 'Hello')
        c('green', 'world')
    """
    if len(styles_and_text) < 2:
        return styles_and_text[0] if styles_and_text else ''
    *styles, text = styles_and_text
    if not _enabled:
        return str(text)
    prefix = ''.join(_CODES[s] for s in styles if s in _CODES)
    if not prefix:
        return str(text)
    return f"{prefix}{text}{_CODES['reset']}"


# Convenience shorthands
def bold(text: str) -> str:       return c('bold', text)
def dim(text: str) -> str:        return c('dim', text)
def italic(text: str) -> str:     return c('italic', text)
def red(text: str) -> str:        return c('red', text)
def bred(text: str) -> str:       return c('bred', text)
def green(text: str) -> str:      return c('green', text)
def bgreen(text: str) -> str:     return c('bgreen', text)
def yellow(text: str) -> str:     return c('yellow', text)
def byellow(text: str) -> str:    return c('byellow', text)
def blue(text: str) -> str:       return c('blue', text)
def bblue(text: str) -> str:      return c('bblue', text)
def magenta(text: str) -> str:    return c('magenta', text)
def bmagenta(text: str) -> str:   return c('bmagenta', text)
def cyan(text: str) -> str:       return c('cyan', text)
def bcyan(text: str) -> str:      return c('bcyan', text)
def white(text: str) -> str:      return c('white', text)
def bwhite(text: str) -> str:     return c('bwhite', text)


# ── Box drawing ──────────────────────────────────────────────────────────────
def box(lines: list[str], *, width: int = 60, style: str = 'double',
        title: str = '', color: str = 'cyan') -> str:
    """Render lines inside a Unicode box."""
    _single = ('─', '│', '┌', '┐', '└', '┘', '├', '┤')
    _double = ('═', '║', '╔', '╗', '╚', '╝', '╠', '╣')
    _heavy  = ('━', '┃', '┏', '┓', '┗', '┛', '┣', '┫')
    chars = {'single': _single, 'double': _double, 'heavy': _heavy}.get(style, _double)
    horiz, vert, tl, tr, bl, br, ml, mr = chars

    inner = width - 2
    rows = []

    def _bar(left, fill, right, mid_text='') -> str:
        if mid_text:
            pad = inner - len(mid_text) - 2
            lpad = pad // 2
            rpad = pad - lpad
            bar = f"{fill * lpad} {mid_text} {fill * rpad}"
        else:
            bar = fill * inner
        return c(color, f"{left}{bar}{right}")

    rows.append(_bar(tl, horiz, tr, title))
    rows.append(c(color, f"{vert}") + ' ' * inner + c(color, f"{vert}"))
    for line in lines:
        padded = line[:inner].ljust(inner)
        rows.append(c(color, f"{vert}") + padded + c(color, f"{vert}"))
    rows.append(c(color, f"{vert}") + ' ' * inner + c(color, f"{vert}"))
    rows.append(_bar(bl, horiz, br))
    return '\n'.join(rows)


def rule(width: int = 60, char: str = '─', color: str = 'dim') -> str:
    """A horizontal rule."""
    return c(color, char * width)


# ── Token type → colour ──────────────────────────────────────────────────────
_KEYWORD_TYPES = frozenset({
    'LET', 'CONST', 'VAR', 'FUNCTION', 'CLASS', 'RETURN', 'IF', 'ELSE',
    'FOR', 'WHILE', 'DO', 'SWITCH', 'CASE', 'DEFAULT', 'BREAK', 'CONTINUE',
    'NEW', 'DELETE', 'TYPEOF', 'INSTANCEOF', 'VOID', 'THROW', 'TRY', 'CATCH',
    'FINALLY', 'IN', 'OF', 'EXTENDS', 'SUPER', 'STATIC', 'ASYNC', 'AWAIT',
    'IMPORT', 'EXPORT', 'FROM', 'YIELD',
})
_LITERAL_TYPES = frozenset({'NUMBER', 'STRING', 'REGEX', 'TEMPLATE'})
_BOOL_NULL_TYPES = frozenset({'TRUE', 'FALSE', 'NULL', 'UNDEFINED'})
_PUNCT_TYPES = frozenset({
    'LPAREN', 'RPAREN', 'LBRACE', 'RBRACE', 'LBRACKET', 'RBRACKET',
    'SEMICOLON', 'COLON', 'COMMA', 'DOT', 'DOTDOTDOT', 'QUESTION',
    'ARROW', 'OPTIONAL_CHAIN',
})
_OP_TYPES = frozenset({
    'PLUS', 'MINUS', 'STAR', 'SLASH', 'PERCENT', 'STARSTAR',
    'AND', 'OR', 'NOT', 'BITAND', 'BITOR', 'BITXOR', 'BITNOT',
    'LSHIFT', 'RSHIFT', 'URSHIFT',
    'EQ', 'NEQ', 'STRICT_EQ', 'STRICT_NEQ', 'LT', 'GT', 'LTE', 'GTE',
    'ASSIGN', 'PLUS_ASSIGN', 'MINUS_ASSIGN', 'STAR_ASSIGN', 'SLASH_ASSIGN',
    'PERCENT_ASSIGN', 'STARSTAR_ASSIGN', 'AND_ASSIGN', 'OR_ASSIGN',
    'BITAND_ASSIGN', 'BITOR_ASSIGN', 'BITXOR_ASSIGN',
    'NULLISH', 'NULLISH_ASSIGN',
    'INC', 'DEC',
})

def token_color(token_type: str) -> str:
    """Return colour code name for a token type."""
    tt = token_type.upper()
    if tt in _KEYWORD_TYPES:     return 'bblue'
    if tt in _LITERAL_TYPES:     return 'bgreen'
    if tt in _BOOL_NULL_TYPES:   return 'byellow'
    if tt == 'IDENTIFIER':       return 'white'
    if tt in _PUNCT_TYPES:       return 'dim'
    if tt in _OP_TYPES:          return 'cyan'
    if tt == 'EOF':              return 'dim'
    if tt == 'COMMENT':          return 'dim'
    return 'white'


# ── JSON syntax highlighting ─────────────────────────────────────────────────
def highlight_json(json_str: str) -> str:
    """Colour a JSON string (keys, strings, numbers, booleans, null)."""
    if not _enabled:
        return json_str
    import re
    # Tokenise JSON roughly with regex
    result = []
    pos = 0
    # Pattern order matters: string → number → bool/null → punctuation
    _pat = re.compile(
        r'("(?:[^"\\]|\\.)*")'          # string (group 1)
        r'|(-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)' # number (group 2)
        r'|(true|false)'                 # bool (group 3)
        r'|(null)'                       # null (group 4)
        r'|([{}\[\],:])'                 # punctuation (group 5)
    )
    prev_end = 0
    _key_pending = [False]
    for m in _pat.finditer(json_str):
        # Whitespace/newlines between matches
        result.append(json_str[prev_end:m.start()])
        prev_end = m.end()
        text = m.group(0)
        if m.group(1):
            # String — check if it's a key (followed by :)
            after = json_str[m.end():m.end()+2].lstrip()
            if after.startswith(':'):
                result.append(c('cyan', text))
            else:
                result.append(c('bgreen', text))
        elif m.group(2):
            result.append(c('byellow', text))
        elif m.group(3):
            result.append(c('byellow', text))
        elif m.group(4):
            result.append(c('dim', text))
        elif m.group(5):
            result.append(c('dim', text))
        else:
            result.append(text)
    result.append(json_str[prev_end:])
    return ''.join(result)


# ── Timing display ────────────────────────────────────────────────────────────
def format_duration(ms: float) -> str:
    """Colour-code a duration: green (fast) → yellow (medium) → red (slow)."""
    if ms < 50:
        col = 'bgreen'
    elif ms < 500:
        col = 'byellow'
    else:
        col = 'bred'
    if ms < 1:
        label = f"{ms * 1000:.0f} µs"
    elif ms < 1000:
        label = f"{ms:.3f} ms"
    else:
        label = f"{ms / 1000:.3f} s"
    return c(col, label)
