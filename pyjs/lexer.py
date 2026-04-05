from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List

from .trace import get_logger, TRACE, _any_enabled as _TRACE_ACTIVE

_log = get_logger("lexer")

class Token:
    __slots__ = ('type', 'value', 'line', 'col', 'end_col')
    def __init__(self, type, value, line, col, end_col=0):
        self.type = type
        self.value = value
        self.line = line
        self.col = col
        self.end_col = end_col

_SIMPLE_TOKENS = {
    '(':'LPAREN',  ')':'RPAREN',  '{':'LBRACE',  '}':'RBRACE',
    '[':'LBRACKET',']':'RBRACKET', ';':'SEMICOLON',',':'COMMA',
    ':':'COLON',   '~':'TILDE',
}

_NO_REGEX_PREV = frozenset({
    'IDENTIFIER', 'NUMBER', 'STRING', 'BIGINT',
    'RPAREN', 'RBRACKET', 'INCREMENT', 'DECREMENT',
    'TRUE', 'FALSE', 'NULL', 'UNDEFINED',
})

# ============================================================================
#  Lexer
# ============================================================================

class Lexer:
    KEYWORDS = {
        'var','let','const','if','else','for','while','do','switch','case',
        'default','break','continue','return','function','class','new','this',
        'typeof','instanceof','void','delete','throw','try','catch','finally',
        'true','false','null','undefined','in','of','extends','super','static',
        'async','await','import','export','from','yield',
    }

    ESCAPE_MAP = {
        'n':'\n','t':'\t','r':'\r','\\':'\\',"'":"'",'"':'"',
        '0':'\0','b':'\b','f':'\f','v':'\v',
    }

    def __init__(self, source: str):
        self.s = source
        self.length = len(source)
        self.i = 0
        self.line = 1
        self.col = 1

    # -- helpers -----------------------------------------------------------
    def _end(self) -> bool:
        return self.i >= self.length

    def _ch(self) -> str:
        return self.s[self.i] if self.i < self.length else '\0'

    def _nxt(self) -> str:
        ch = self.s[self.i]
        self.i += 1
        if ch == '\n':
            self.line += 1; self.col = 1
        else:
            self.col += 1
        return ch

    def _peek(self, off=0):
        j = self.i + off
        return self.s[j] if j < self.length else '\0'

    def _match(self, ch):
        if self.i < self.length and self.s[self.i] == ch:
            self.i += 1; self.col += 1
            return True
        return False

    def _mk(self, tt, val, sc, sl):
        return Token(tt, val, sl, sc, self.col)

    # -- skip whitespace / comments ----------------------------------------
    def _skip(self):
        _s = self.s
        _len = self.length
        i = self.i
        _col = self.col
        _line = self.line
        while i < _len:
            c = _s[i]
            if c == ' ' or c == '\t' or c == '\r':
                i += 1
                _col += 1
            elif c == '\n':
                i += 1
                _line += 1
                _col = 1
            elif c == '/' and i + 1 < _len:
                c2 = _s[i + 1]
                if c2 == '/':
                    i += 2
                    while i < _len and _s[i] != '\n':
                        i += 1
                    _col = 1  # next char is \n or EOF
                elif c2 == '*':
                    i += 2
                    _col += 2
                    while i < _len:
                        ch = _s[i]
                        if ch == '*' and i + 1 < _len and _s[i + 1] == '/':
                            i += 2
                            _col += 2
                            break
                        if ch == '\n':
                            _line += 1
                            _col = 1
                        else:
                            _col += 1
                        i += 1
                else:
                    break
            else:
                break
        self.i = i
        self.col = _col
        self.line = _line

    # -- readers -----------------------------------------------------------
    def _read_number(self):
        sc, sl = self.col, self.line
        _s = self.s
        _len = self.length
        start = self.i
        i = start
        if _s[i] == '0' and i + 1 < _len and _s[i + 1] in 'xXoObB':
            i += 2
            while i < _len and (_s[i] in '0123456789abcdefABCDEF' or _s[i] == '_'):
                i += 1
            self.col += i - start
            self.i = i
            raw = _s[start:i].replace('_', '')
            return self._mk('NUMBER', int(raw, 0), sc, sl)
        while i < _len and (_s[i].isdigit() or _s[i] == '_'):
            i += 1
        if i < _len and _s[i] == '.' and i + 1 < _len and _s[i + 1].isdigit():
            i += 1
            while i < _len and (_s[i].isdigit() or _s[i] == '_'):
                i += 1
        if i < _len and _s[i] in 'eE':
            i += 1
            if i < _len and _s[i] in '+-':
                i += 1
            while i < _len and (_s[i].isdigit() or _s[i] == '_'):
                i += 1
        self.col += i - start
        self.i = i
        raw = _s[start:i]
        clean = raw.replace('_', '')
        if i < _len and _s[i] == 'n' and '.' not in clean and 'e' not in clean.lower():
            self.i = i + 1
            self.col += 1
            return self._mk('BIGINT', int(clean), sc, sl)
        return self._mk('NUMBER', float(clean), sc, sl)

    def _read_ident(self):
        sc, sl = self.col, self.line
        _s = self.s
        _len = self.length
        start = self.i
        i = start
        while i < _len:
            c = _s[i]
            if c.isalnum() or c == '_' or c == '$':
                i += 1
            else:
                break
        self.col += i - start
        self.i = i
        w = _s[start:i]
        if w in self.KEYWORDS:
            return self._mk(w.upper(), w, sc, sl)
        return self._mk('IDENTIFIER', w, sc, sl)

    def _read_string(self, q):
        sc, sl = self.col, self.line
        _s = self.s
        _len = self.length
        start = self.i + 1  # after opening quote
        # Fast path: scan for closing quote without escape
        j = _s.find(q, start)
        if j != -1 and '\\' not in _s[start:j] and '\n' not in _s[start:j]:
            self.i = j + 1
            self.col += j + 1 - (start - 1)
            return self._mk('STRING', _s[start:j], sc, sl)
        # Slow path: handle escapes
        self._nxt()                                    # skip opening quote
        buf = []
        while not self._end() and self._ch() != q:
            if self._ch() == '\\':
                self._nxt()
                esc = self._ch()
                if esc == 'u':
                    self._nxt()
                    if self._ch() == '{':
                        # \u{HHHH} variable-length Unicode escape
                        self._nxt()
                        hex_buf = []
                        while not self._end() and self._ch() != '}':
                            hex_buf.append(self._nxt())
                        if not self._end():
                            self._nxt()  # skip '}'
                        try:
                            buf.append(chr(int(''.join(hex_buf), 16)))
                        except ValueError:
                            buf.append('u{' + ''.join(hex_buf) + '}')
                    else:
                        # \uXXXX fixed 4-hex Unicode escape
                        hex_chars = []
                        for _ in range(4):
                            if not self._end():
                                hex_chars.append(self._nxt())
                        try:
                            buf.append(chr(int(''.join(hex_chars), 16)))
                        except ValueError:
                            buf.append('u' + ''.join(hex_chars))
                elif esc == 'x':
                    # \xHH hex escape
                    self._nxt()
                    hex_chars = []
                    for _ in range(2):
                        if not self._end():
                            hex_chars.append(self._nxt())
                    try:
                        buf.append(chr(int(''.join(hex_chars), 16)))
                    except ValueError:
                        buf.append('x' + ''.join(hex_chars))
                else:
                    buf.append(self.ESCAPE_MAP.get(esc, esc)); self._nxt()
            else:
                buf.append(self._nxt())
        if not self._end():
            self._nxt()                                # skip closing quote
        return self._mk('STRING', ''.join(buf), sc, sl)

    def _read_template(self):
        sc, sl = self.col, self.line
        self._nxt()                                    # skip backtick
        buf = []
        cooked_parts = []
        raw_parts = []
        while not self._end() and self._ch() != '`':
            if self._ch() == '\\':
                self._nxt()                            # consume backslash
                c = self._nxt()                        # char after backslash
                cooked_parts.append(self.ESCAPE_MAP.get(c, c))
                raw_parts.append('\\' + c)
            elif self._ch() == '$' and self._peek(1) == '{':
                # Flush accumulated text as ('text', cooked, raw) tuple
                buf.append(('text', ''.join(cooked_parts), ''.join(raw_parts)))
                cooked_parts = []
                raw_parts = []
                self._nxt(); self._nxt()               # consume '${'
                depth, expr_buf = 1, []
                while not self._end() and depth > 0:
                    c = self._ch()
                    if c == '{': depth += 1
                    elif c == '}':
                        depth -= 1
                        if depth == 0: self._nxt(); break
                    expr_buf.append(self._nxt())
                buf.append(('expr', ''.join(expr_buf)))
            else:
                ch = self._nxt()
                cooked_parts.append(ch)
                raw_parts.append(ch)
        # Flush final text segment
        buf.append(('text', ''.join(cooked_parts), ''.join(raw_parts)))
        if not self._end():
            self._nxt()                                # consume closing backtick
        return self._mk('TEMPLATE', buf, sc, sl)

    def _read_regex(self, sc, sl):
        """Read a regex literal starting right after the opening /."""
        buf = []
        in_char_class = False
        while not self._end():
            c = self._ch()
            if c == '\\':
                buf.append(self._nxt())
                if not self._end():
                    buf.append(self._nxt())
                continue
            if c == '[':
                in_char_class = True
            elif c == ']':
                in_char_class = False
            elif c == '/' and not in_char_class:
                self._nxt()  # consume closing /
                break
            elif c == '\n':
                break
            buf.append(self._nxt())
        pattern = ''.join(buf)
        flags = []
        while not self._end() and (self._ch().isalpha() or self._ch() == '_'):
            flags.append(self._nxt())
        return self._mk('REGEX', (pattern, ''.join(flags)), sc, sl)

    # -- main tokenize loop -------------------------------------------------
    def tokenize(self) -> List[Token]:
        toks: List[Token] = []
        _s = self.s
        _len = self.length
        while True:
            self._skip()
            if self.i >= _len:
                toks.append(self._mk('EOF', None, self.col, self.line))
                return toks
            sc, sl = self.col, self.line
            c = _s[self.i]
            if c.isdigit() or (c == '.' and self.i + 1 < _len and _s[self.i + 1].isdigit()):
                toks.append(self._read_number()); continue
            if c.isalpha() or c in ('_', '$'):
                toks.append(self._read_ident()); continue
            if c == '#' and self.i + 1 < _len and (_s[self.i + 1].isalpha() or _s[self.i + 1] == '_'):
                self._nxt()  # skip '#'
                name_start = self.i
                while self.i < _len and (_s[self.i].isalnum() or _s[self.i] in ('_', '$')):
                    self._nxt()
                toks.append(self._mk('PRIVATE_NAME', '#' + _s[name_start:self.i], sc, sl))
                continue
            if c in ('"',"'"):
                toks.append(self._read_string(c)); continue
            if c == '`':
                toks.append(self._read_template()); continue
            if c == '=' and self.i + 1 < _len and _s[self.i + 1] == '>':
                self._nxt(); self._nxt()
                toks.append(self._mk('ARROW','=>',sc,sl)); continue
            self._nxt()
            if c in _SIMPLE_TOKENS:
                toks.append(self._mk(_SIMPLE_TOKENS[c], c, sc, sl)); continue
            if c == '.':
                _i = self.i
                if _i < _len and _i + 1 < _len and _s[_i] == '.' and _s[_i + 1] == '.':
                    self._nxt(); self._nxt()
                    toks.append(self._mk('ELLIPSIS','...',sc,sl))
                else:
                    toks.append(self._mk('DOT','.',sc,sl))
                continue
            if c == '=':
                if self._match('='):
                    toks.append(self._mk('===' if self._match('=') else '==',
                                         '===' if toks[-1].value=='==' else '==', sc, sl))
                else:
                    toks.append(self._mk('ASSIGN','=',sc,sl))
                continue
            if c == '!':
                if self._match('='):
                    toks.append(self._mk('!==' if self._match('=') else '!=',
                                         '!==' if toks[-1].value=='!=' else '!=', sc, sl))
                else:
                    toks.append(self._mk('BANG','!',sc,sl))
                continue
            if c == '<':
                if self._match('='): toks.append(self._mk('LTE','<=',sc,sl))
                elif self._match('<'):
                    if self._match('='): toks.append(self._mk('ASSIGN_LSHIFT','<<=',sc,sl))
                    else: toks.append(self._mk('LSHIFT','<<',sc,sl))
                else: toks.append(self._mk('LT','<',sc,sl))
                continue
            if c == '>':
                if self._match('='): toks.append(self._mk('GTE','>=',sc,sl))
                elif self._match('>'):
                    if self._match('>'):
                        if self._match('='): toks.append(self._mk('ASSIGN_URSHIFT','>>>=',sc,sl))
                        else: toks.append(self._mk('URSHIFT','>>>',sc,sl))
                    elif self._match('='): toks.append(self._mk('ASSIGN_RSHIFT','>>=',sc,sl))
                    else: toks.append(self._mk('RSHIFT','>>',sc,sl))
                else: toks.append(self._mk('GT','>',sc,sl))
                continue
            if c == '+':
                if self._match('+'): toks.append(self._mk('INCREMENT','++',sc,sl))
                elif self._match('='): toks.append(self._mk('ASSIGN_ADD','+=',sc,sl))
                else: toks.append(self._mk('PLUS','+',sc,sl))
                continue
            if c == '-':
                if self._match('-'): toks.append(self._mk('DECREMENT','--',sc,sl))
                elif self._match('='): toks.append(self._mk('ASSIGN_SUB','-=',sc,sl))
                else: toks.append(self._mk('MINUS','-',sc,sl))
                continue
            if c == '*':
                if self._match('*'):
                    if self._match('='): toks.append(self._mk('ASSIGN_EXP','**=',sc,sl))
                    else: toks.append(self._mk('EXP','**',sc,sl))
                elif self._match('='): toks.append(self._mk('ASSIGN_MUL','*=',sc,sl))
                else: toks.append(self._mk('STAR','*',sc,sl))
                continue
            if c == '/':
                if self._match('='): toks.append(self._mk('ASSIGN_DIV','/=',sc,sl))
                else:
                    prev_type = toks[-1].type if toks else None
                    if prev_type in _NO_REGEX_PREV:
                        toks.append(self._mk('SLASH','/',sc,sl))
                    else:
                        toks.append(self._read_regex(sc, sl))
                continue
            if c == '%':
                if self._match('='): toks.append(self._mk('ASSIGN_MOD','%=',sc,sl))
                else: toks.append(self._mk('MOD','%',sc,sl))
                continue
            if c == '^':
                if self._match('='): toks.append(self._mk('ASSIGN_XOR','^=',sc,sl))
                else: toks.append(self._mk('BIT_XOR','^',sc,sl))
                continue
            # handle ?? (nullish coalescing) when not caught above
            if c == '?':
                if self._match('.'):
                    toks.append(self._mk('QDOT', '?.', sc, sl))
                elif self._match('?'):
                    if self._match('='):
                        toks.append(self._mk('ASSIGN_NULLISH', '??=', sc, sl))
                    else:
                        toks.append(self._mk('NULLISH','??',sc,sl))
                else:
                    toks.append(self._mk('QUESTION','?',sc,sl))
                continue
            if c == '&':
                if self._match('&'):
                    if self._match('='):
                        toks.append(self._mk('ASSIGN_BOOL_AND','&&=',sc,sl))
                    else:
                        toks.append(self._mk('AND','&&',sc,sl))
                elif self._match('='):
                    toks.append(self._mk('ASSIGN_AND','&=',sc,sl))
                else:
                    toks.append(self._mk('BIT_AND','&',sc,sl))
                continue
            if c == '|':
                if self._match('|'):
                    if self._match('='):
                        toks.append(self._mk('ASSIGN_OR','||=',sc,sl))
                    else:
                        toks.append(self._mk('OR','||',sc,sl))
                elif self._match('='):
                    toks.append(self._mk('ASSIGN_BIT_OR','|=',sc,sl))
                else:
                    toks.append(self._mk('BIT_OR','|',sc,sl))
                continue
            if c == '@':
                toks.append(self._mk('AT', '@', sc, sl)); continue
            toks.append(self._mk('UNKNOWN', c, sc, sl))
