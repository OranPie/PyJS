from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List

@dataclass(slots=True)
class Token:
    type: str
    value: Any
    line: int
    col: int
    end_col: int = 0

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
            self._nxt(); return True
        return False

    def _mk(self, tt, val, sc, sl):
        return Token(tt, val, sl, sc, self.col)

    # -- skip whitespace / comments ----------------------------------------
    def _skip(self):
        while not self._end():
            c = self._ch()
            if c in ' \t\r\n':
                self._nxt()
            elif c == '/' and self._peek(1) == '/':
                while not self._end() and self._ch() != '\n':
                    self._nxt()
            elif c == '/' and self._peek(1) == '*':
                self._nxt(); self._nxt()
                while not self._end():
                    if self._ch() == '*' and self._peek(1) == '/':
                        self._nxt(); self._nxt(); break
                    self._nxt()
            else:
                break

    # -- readers -----------------------------------------------------------
    def _read_number(self):
        sc, sl = self.col, self.line
        start = self.i
        if self._ch() == '0' and self._peek(1) in 'xXoObB':
            self._nxt(); self._nxt()
            while not self._end() and (self._ch() in '0123456789abcdefABCDEF' or self._ch() == '_'):
                self._nxt()
            raw = self.s[start:self.i].replace('_', '')
            return self._mk('NUMBER', int(raw, 0), sc, sl)
        while not self._end() and (self._ch().isdigit() or self._ch() == '_'):
            self._nxt()
        if not self._end() and self._ch() == '.' and self._peek(1).isdigit():
            self._nxt()
            while not self._end() and (self._ch().isdigit() or self._ch() == '_'):
                self._nxt()
        if not self._end() and self._ch() in 'eE':
            self._nxt()
            if not self._end() and self._ch() in '+-':
                self._nxt()
            while not self._end() and (self._ch().isdigit() or self._ch() == '_'):
                self._nxt()
        raw = self.s[start:self.i]
        clean = raw.replace('_', '')
        if not self._end() and self._ch() == 'n' and '.' not in clean and 'e' not in clean.lower():
            self._nxt()  # consume 'n'
            return self._mk('BIGINT', int(clean), sc, sl)
        return self._mk('NUMBER', float(clean), sc, sl)

    def _read_ident(self):
        sc, sl = self.col, self.line
        start = self.i
        while not self._end() and (self._ch().isalnum() or self._ch() in ('_', '$')):
            self._nxt()
        w = self.s[start:self.i]
        if w in self.KEYWORDS:
            return self._mk(w.upper(), w, sc, sl)
        return self._mk('IDENTIFIER', w, sc, sl)

    def _read_string(self, q):
        sc, sl = self.col, self.line
        self._nxt()                                    # skip opening quote
        buf = []
        while not self._end() and self._ch() != q:
            if self._ch() == '\\':
                self._nxt()
                esc = self._ch()
                mp = {'n':'\n','t':'\t','r':'\r','\\':'\\',"'":"'",'"':'"',
                      '0':'\0','b':'\b','f':'\f','v':'\v'}
                buf.append(mp.get(esc, esc)); self._nxt()
            else:
                buf.append(self._nxt())
        if not self._end():
            self._nxt()                                # skip closing quote
        return self._mk('STRING', ''.join(buf), sc, sl)

    def _read_template(self):
        sc, sl = self.col, self.line
        self._nxt()                                    # skip backtick
        buf = []
        while not self._end() and self._ch() != '`':
            if self._ch() == '\\':
                self._nxt(); buf.append(self._nxt()); continue
            if self._ch() == '$' and self._peek(1) == '{':
                self._nxt(); self._nxt()
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
                buf.append(self._nxt())
        if not self._end():
            self._nxt()
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
        while True:
            self._skip()
            if self._end():
                toks.append(self._mk('EOF', None, self.col, self.line))
                return toks
            sc, sl = self.col, self.line
            c = self._ch()
            if c.isdigit() or (c == '.' and self._peek(1).isdigit()):
                toks.append(self._read_number()); continue
            if c.isalpha() or c in ('_', '$'):
                toks.append(self._read_ident()); continue
            if c == '#' and not self._end() and (self._peek(1).isalpha() or self._peek(1) == '_'):
                self._nxt()  # skip '#'
                name_start = self.i
                while not self._end() and (self._ch().isalnum() or self._ch() in ('_', '$')):
                    self._nxt()
                toks.append(self._mk('PRIVATE_NAME', '#' + self.s[name_start:self.i], sc, sl))
                continue
            if c in ('"',"'"):
                toks.append(self._read_string(c)); continue
            if c == '`':
                toks.append(self._read_template()); continue
            if c == '=' and self._peek(1) == '>':
                self._nxt(); self._nxt()
                toks.append(self._mk('ARROW','=>',sc,sl)); continue
            self._nxt()
            simple = {
                '(':'LPAREN',  ')':'RPAREN',  '{':'LBRACE',  '}':'RBRACE',
                '[':'LBRACKET',']':'RBRACKET', ';':'SEMICOLON',',':'COMMA',
                ':':'COLON',   '~':'TILDE',
            }
            if c in simple:
                toks.append(self._mk(simple[c], c, sc, sl)); continue
            if c == '.':
                if self._peek(0)==self._peek(1)=='.':
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
                    _no_regex = {
                        'IDENTIFIER', 'NUMBER', 'STRING', 'BIGINT',
                        'RPAREN', 'RBRACKET', 'INCREMENT', 'DECREMENT',
                        'TRUE', 'FALSE', 'NULL', 'UNDEFINED',
                    }
                    if prev_type in _no_regex:
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
            toks.append(self._mk('UNKNOWN', c, sc, sl))
