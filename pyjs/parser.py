from __future__ import annotations

from typing import List

from .lexer import Lexer, Token
from .trace import get_logger, TRACE

_log = get_logger("parser")

IDENTIFIER_NAME_TOKENS = {'IDENTIFIER', *(keyword.upper() for keyword in Lexer.KEYWORDS)}

class N:
    """Namespace for AST-node constructors (each returns a dict)."""
    @staticmethod
    def _n(tp, **kw):
        d = {"type": tp}; d.update(kw); return d

    Program        = lambda body:           N._n("Program", body=body)
    Block          = lambda body:           N._n("BlockStatement", body=body)
    VarDecl        = lambda kind,decls:     N._n("VariableDeclaration", kind=kind, declarations=decls)
    VarDeclarator  = lambda id,init=None,line=0: N._n("VariableDeclarator", id=id, init=init, line=line)
    FnDecl         = lambda name,params,body,async_=False,generator_=False: N._n("FunctionDeclaration", id=name, params=params, body=body, async_=async_, generator_=generator_)
    ClassDecl      = lambda name,super_,body: N._n("ClassDeclaration", id=name, superClass=super_, body=body)
    ClassField     = lambda key, value, static_=False: N._n("ClassField", key=key, value=value, static_=static_)
    StaticBlock    = lambda body: N._n("StaticBlock", body=body)
    RetStmt        = lambda arg=None:       N._n("ReturnStatement", argument=arg)
    ThrowStmt      = lambda arg:            N._n("ThrowStatement", argument=arg)
    IfStmt         = lambda test,cons,alt:  N._n("IfStatement", test=test, consequent=cons, alternate=alt)
    WhileStmt      = lambda test,body:      N._n("WhileStatement", test=test, body=body)
    DoWhileStmt    = lambda body,test:      N._n("DoWhileStatement", body=body, test=test)
    ForStmt        = lambda init,test,upd,body: N._n("ForStatement", init=init, test=test, update=upd, body=body)
    ForInStmt      = lambda left,right,body: N._n("ForInStatement", left=left, right=right, body=body)
    ForOfStmt      = lambda left,right,body,await_=False: N._n("ForOfStatement", left=left, right=right, body=body, await_=await_)
    SwitchStmt     = lambda disc,cases,defb: N._n("SwitchStatement", discriminant=disc, cases=cases, defaultCase=default_case if (default_case := defb) else None)
    SwitchCase     = lambda test,body,deflt=False: N._n("SwitchCase", test=test, consequent=body, default=deflt)
    TryStmt        = lambda blk,cb,fb:     N._n("TryStatement", block=blk, handler=cb, finalizer=fb)
    CatchClause    = lambda param,body:     N._n("CatchClause", param=param, body=body)
    BreakStmt      = lambda label=None:     N._n("BreakStatement", label=label)
    ContStmt       = lambda label=None:     N._n("ContinueStatement", label=label)
    EmptyStmt      = lambda:                N._n("EmptyStatement")
    ExprStmt       = lambda expr:           N._n("ExpressionStatement", expression=expr)
    Lit            = lambda val,tp,line=0:  N._n("Literal", value=val, raw=tp, line=line)
    Id             = lambda name,line=0:    N._n("Identifier", name=name, line=line)
    ArrExpr        = lambda elems:          N._n("ArrayExpression", elements=elems)
    ObjExpr        = lambda props:          N._n("ObjectExpression", properties=props)
    Prop           = lambda key,val,comp=False,short=False: N._n("Property", key=key, value=val, computed=comp, shorthand=short)
    FnExpr         = lambda name,params,body,arrow=False,async_=False,generator_=False: N._n("FunctionExpression", id=name, params=params, body=body, arrow=arrow, async_=async_, generator_=generator_)
    UnaryExpr      = lambda op,arg,pfx:    N._n("UnaryExpression", operator=op, argument=arg, prefix=pfx)
    BinExpr        = lambda op,l,r,line=0:  N._n("BinaryExpression", operator=op, left=l, right=r, line=line)
    LogExpr        = lambda op,l,r:         N._n("LogicalExpression", operator=op, left=l, right=r)
    UpdateExpr     = lambda op,arg,pfx:     N._n("UpdateExpression", operator=op, argument=arg, prefix=pfx)
    AssignExpr     = lambda op,l,r:         N._n("AssignmentExpression", operator=op, left=l, right=r)
    CondExpr       = lambda test,alt,cons:  N._n("ConditionalExpression", test=test, alternate=alt, consequent=cons)
    MemberExpr     = lambda obj,prop,comp,optional=False:  N._n("MemberExpression", object=obj, property=prop, computed=comp, optional=optional)
    CallExpr       = lambda callee,args,line=0,optional=False: N._n("CallExpression", callee=callee, arguments=args, line=line, optional=optional)
    NewExpr        = lambda callee,args:    N._n("NewExpression", callee=callee, arguments=args)
    SpreadExpr     = lambda arg:            N._n("SpreadElement", argument=arg)
    ThisExpr       = lambda:                N._n("ThisExpression")
    TemplateExpr   = lambda parts:          N._n("TemplateLiteral", quasis=parts)
    AwaitExpr      = lambda arg:            N._n("AwaitExpression", argument=arg)
    ForAwaitOfStmt = lambda left,right,body: N._n("ForOfStatement", left=left, right=right, body=body, await_=True)
    ImportDecl     = lambda specifiers,source: N._n("ImportDeclaration", specifiers=specifiers, source=source)
    ImportDefault  = lambda local: N._n("ImportDefaultSpecifier", local=local)
    ImportNs       = lambda local: N._n("ImportNamespaceSpecifier", local=local)
    ImportSpec     = lambda imported,local: N._n("ImportSpecifier", imported=imported, local=local)
    ExportDecl     = lambda decl: N._n("ExportNamedDeclaration", declaration=decl, specifiers=[], source=None)
    ExportList     = lambda specifiers,source: N._n("ExportNamedDeclaration", declaration=None, specifiers=specifiers, source=source)
    ExportDefault  = lambda decl: N._n("ExportDefaultDeclaration", declaration=decl)
    UsingDecl      = lambda is_async, decls: N._n("UsingDeclaration", is_async=is_async, declarations=decls)

# convenience aliases
CallExpr = lambda callee, args, line=0, optional=False: N._n("CallExpression", callee=callee, arguments=args, line=line, optional=optional)

# ============================================================================
#  Parser
# ============================================================================

class Parser:
    def __init__(self, tokens: List[Token]):
        self.toks = tokens
        self.pos = 0

    # -- helpers ------------------------------------------------------------
    def _cur(self):       return self.toks[self.pos]
    def _peek(self, n=1): return self.toks[min(self.pos+n, len(self.toks)-1)]
    def _advance(self):
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def _check(self, *types):
        return self._cur().type in types

    def _expect(self, tt, msg=""):
        if self._cur().type == tt:
            return self._advance()
        t = self._cur()
        _log.warning("parse error at line %d: expected %s, got %s %r", t.line, tt, t.type, t.value)
        raise SyntaxError(f"Line {t.line}:{t.col} — expected {tt}, got {t.type} {t.value!r}  {msg}")

    def _optional(self, tt):
        if self._cur().type == tt:
            return self._advance()
        return None

    def _is_nl(self):
        """True if current token starts on a new line vs previous token."""
        if self.pos == 0: return False
        p, c = self.toks[self.pos-1], self._cur()
        return c.line > p.line

    def _is_identifier_name(self):
        return self._cur().type in IDENTIFIER_NAME_TOKENS

    def _consume_identifier_name(self):
        if not self._is_identifier_name():
            t = self._cur()
            raise SyntaxError(f"Line {t.line}:{t.col} — expected property name, got {t.type} {t.value!r}")
        return self._advance().value

    def _consume_binding_identifier(self):
        return self._expect('IDENTIFIER').value

    def _normalize_number_key(self, value):
        if isinstance(value, float) and value == int(value):
            return str(int(value))
        return str(value)

    def _binding_target(self):
        if self._check('IDENTIFIER'):
            tok = self._advance()
            return N.Id(tok.value, tok.line)
        if self._check('LBRACKET'):
            return self._array_pattern()
        if self._check('LBRACE'):
            return self._object_pattern()
        t = self._cur()
        raise SyntaxError(f"Line {t.line}:{t.col} — expected binding target, got {t.type} {t.value!r}")

    def _binding_element(self):
        target = self._binding_target()
        if self._check('ASSIGN'):
            self._advance()
            return {'type': 'AssignmentPattern', 'left': target, 'right': self._assign()}
        return target

    def _array_pattern(self):
        self._expect('LBRACKET')
        elems = []
        while not self._check('RBRACKET') and not self._check('EOF'):
            if self._check('COMMA'):
                elems.append(None)
                self._advance()
                continue
            if self._check('ELLIPSIS'):
                self._advance()
                elems.append({'type': 'RestElement', 'argument': self._binding_target()})
                break
            elems.append(self._binding_element())
            if not self._optional('COMMA'):
                break
        self._expect('RBRACKET')
        return {'type': 'ArrayPattern', 'elements': elems}

    def _object_pattern(self):
        self._expect('LBRACE')
        props = []
        while not self._check('RBRACE') and not self._check('EOF'):
            if self._check('ELLIPSIS'):
                self._advance()
                props.append({'type': 'RestElement', 'argument': self._binding_target()})
                self._optional('COMMA')
                continue
            key = None
            if self._check('LBRACKET'):
                self._advance()
                key = self._expr()
                self._expect('RBRACKET')
                computed = True
            elif self._check('STRING'):
                key = self._advance().value
                computed = False
            elif self._check('NUMBER'):
                key = self._normalize_number_key(self._advance().value)
                computed = False
            elif self._is_identifier_name():
                key = self._advance().value
                computed = False
            else:
                break
            if self._check('COLON'):
                self._advance()
                value = self._binding_element()
            else:
                value = N.Id(key)
                if self._check('ASSIGN'):
                    self._advance()
                    value = {'type': 'AssignmentPattern', 'left': value, 'right': self._assign()}
            props.append({'type': 'Property', 'key': key, 'value': value, 'computed': computed, 'shorthand': not computed and isinstance(key, str) and isinstance(value, dict) and value.get('type') == 'Identifier' and value['name'] == key})
            self._optional('COMMA')
        self._expect('RBRACE')
        return {'type': 'ObjectPattern', 'properties': props}

    # -- top-level ----------------------------------------------------------
    def parse(self) -> dict:
        body = []
        while not self._check('EOF'):
            body.append(self._stmt())
        _log.debug("parsed %d top-level statements", len(body))
        return N.Program(body)

    def _semi(self):
        """Semicolon or automatic semicolon insertion."""
        if self._check('SEMICOLON'):
            self._advance()
        elif self._is_nl() or self._check('RBRACE', 'EOF'):
            pass
        else:
            if not self._check('RPAREN'):
                pass  # lenient: allow missing semicolons

    # -- statements ---------------------------------------------------------
    def _stmt(self):
        t = self._cur().type
        if _log.isEnabledFor(TRACE):
            _log.log(TRACE, "node %s", t)
        if t in ('VAR','LET','CONST'):        return self._var_decl()
        if t == 'IF':                         return self._if()
        if t == 'WHILE':                      return self._while()
        if t == 'DO':                         return self._do_while()
        if t == 'FOR':                        return self._for()
        if t == 'SWITCH':                     return self._switch()
        if t == 'BREAK':                      return self._break()
        if t == 'CONTINUE':                   return self._continue()
        if t == 'RETURN':                     return self._return()
        if t == 'THROW':                      return self._throw()
        if t == 'TRY':                        return self._try()
        if t == 'ASYNC' and self._peek().type == 'FUNCTION':
            return self._fn_decl(async_=True)
        if t == 'FUNCTION':                   return self._fn_decl()
        if t == 'CLASS':                      return self._class_decl()
        if t == 'IMPORT':                     return self._import_decl()
        if t == 'EXPORT':                     return self._export_decl()
        if t == 'LBRACE':                     return self._block()
        if t == 'SEMICOLON':                  self._advance(); return N.EmptyStmt()
        # Labeled statement: IDENTIFIER COLON stmt
        if t == 'IDENTIFIER' and self._peek().type == 'COLON':
            label = self._advance().value
            self._expect('COLON')
            body = self._stmt()
            return {'type': 'LabeledStatement', 'label': label, 'body': body}
        # `using` contextual keyword: `using x = expr` or `await using x = expr`
        if t == 'IDENTIFIER' and self._cur().value == 'using' and \
                self._peek().type == 'IDENTIFIER':
            return self._using_decl(is_async=False)
        if t == 'AWAIT' and self._peek().type == 'IDENTIFIER' and \
                self._peek().value == 'using' and self._peek(2).type == 'IDENTIFIER':
            return self._using_decl(is_async=True)
        return self._expr_stmt()

    def _var_decl(self):
        kind = self._advance().value                     # var / let / const
        decls = []
        while True:
            name = self._binding_target()
            init = None
            if self._check('ASSIGN'):
                self._advance()
                init = self._assign()
            decls.append(N.VarDeclarator(name, init, self._cur().line))
            if not self._optional('COMMA'):
                break
        self._semi()
        return N.VarDecl(kind, decls)

    def _using_decl(self, is_async=False):
        if is_async:
            self._advance()  # skip 'await'
        self._advance()      # skip 'using'
        decls = []
        while True:
            name = self._binding_target()
            init = None
            if self._check('ASSIGN'):
                self._advance()
                init = self._assign()
            decls.append(N.VarDeclarator(name, init, self._cur().line))
            if not self._optional('COMMA'):
                break
        self._semi()
        return N.UsingDecl(is_async, decls)

    def _if(self):
        self._advance()
        self._expect('LPAREN'); test = self._expr(); self._expect('RPAREN')
        cons = self._stmt()
        alt = None
        if self._check('ELSE'):
            self._advance(); alt = self._stmt()
        return N.IfStmt(test, cons, alt)

    def _while(self):
        self._advance()
        self._expect('LPAREN'); test = self._expr(); self._expect('RPAREN')
        return N.WhileStmt(test, self._stmt())

    def _do_while(self):
        self._advance()
        body = self._stmt()
        self._expect('WHILE'); self._expect('LPAREN')
        test = self._expr(); self._expect('RPAREN'); self._semi()
        return N.DoWhileStmt(body, test)

    def _for(self):
        self._advance()
        is_await = False
        if self._check('AWAIT'):
            self._advance()
            is_await = True
        self._expect('LPAREN')
        # -- for ( … in … ) / for ( … of … ) --
        if self._check('VAR','LET','CONST'):
            sv = self.pos
            kind = self._advance().value
            name = self._expect('IDENTIFIER').value
            if self._check('IN','OF'):
                tp = self._advance().type
                right = self._expr(); self._expect('RPAREN')
                left = N.VarDecl(kind, [N.VarDeclarator(name)])
                body = self._stmt()
                return N.ForInStmt(left, right, body) if tp=='IN' else N.ForOfStmt(left, right, body, is_await)
            self.pos = sv                         # backtrack
        # -- regular for --
        init = None
        if self._check('VAR','LET','CONST'):
            init = self._var_decl()
        elif not self._check('SEMICOLON'):
            sv = self.pos
            try:
                init = self._simple_assignment_target()
                if self._check('IN','OF'):
                    tp = self._advance().type
                    right = self._expr(); self._expect('RPAREN')
                    body = self._stmt()
                    return N.ForInStmt(init, right, body) if tp=='IN' else N.ForOfStmt(init, right, body, is_await)
                self.pos = sv
            except Exception:
                self.pos = sv
            init = self._assign()
            if self._check('IN','OF'):
                tp = self._advance().type
                right = self._expr(); self._expect('RPAREN')
                body = self._stmt()
                return N.ForInStmt(init, right, body) if tp=='IN' else N.ForOfStmt(init, right, body, is_await)
            self._semi()
        else:
            self._semi()
        test = self._expr() if not self._check('SEMICOLON') else None
        self._semi()
        upd = self._comma_expr() if not self._check('RPAREN') else None
        self._expect('RPAREN')
        return N.ForStmt(init, test, upd, self._stmt())

    def _switch(self):
        self._advance()
        self._expect('LPAREN'); disc = self._expr(); self._expect('RPAREN')
        self._expect('LBRACE')
        cases, def_body = [], None
        while not self._check('RBRACE') and not self._check('EOF'):
            if self._check('CASE'):
                self._advance(); test = self._expr(); self._expect('COLON')
                body = []
                while not self._check('CASE','DEFAULT','RBRACE','EOF'):
                    body.append(self._stmt())
                cases.append(N.SwitchCase(test, body))
            elif self._check('DEFAULT'):
                self._advance(); self._expect('COLON')
                def_body = []
                while not self._check('CASE','RBRACE','EOF'):
                    def_body.append(self._stmt())
                cases.append(N.SwitchCase(None, def_body, True))
            else:
                break
        self._expect('RBRACE')
        return N.SwitchStmt(disc, cases, def_body)

    def _block(self):
        self._advance()
        body = []
        while not self._check('RBRACE') and not self._check('EOF'):
            body.append(self._stmt())
        self._expect('RBRACE')
        return N.Block(body)

    def _break(self):
        self._advance()
        label = None
        if self._check('IDENTIFIER') and not self._is_nl():
            label = self._advance().value
        self._semi()
        return N.BreakStmt(label)

    def _continue(self):
        self._advance()
        label = None
        if self._check('IDENTIFIER') and not self._is_nl():
            label = self._advance().value
        self._semi()
        return N.ContStmt(label)

    def _return(self):
        self._advance()
        arg = None
        if not self._check('SEMICOLON','RBRACE','EOF') and not self._is_nl():
            arg = self._assign()
        self._semi()
        return N.RetStmt(arg)

    def _throw(self):
        self._advance(); arg = self._assign(); self._semi(); return N.ThrowStmt(arg)

    def _try(self):
        self._advance()
        blk = self._block()
        handler = None
        if self._check('CATCH'):
            self._advance()
            param = None
            if self._check('LPAREN'):
                self._advance()
                if self._check('LBRACE'):
                    param = self._object_pattern()
                elif self._check('LBRACKET'):
                    param = self._array_pattern()
                else:
                    param = self._expect('IDENTIFIER').value
                self._expect('RPAREN')
            handler = N.CatchClause(param, self._block())
        finalizer = None
        if self._check('FINALLY'):
            self._advance(); finalizer = self._block()
        return N.TryStmt(blk, handler, finalizer)

    def _fn_decl(self, async_=False):
        if _log.isEnabledFor(TRACE):
            _log.log(TRACE, "node FunctionDeclaration")
        if self._check('ASYNC'):
            self._advance()
            async_ = True
        self._expect('FUNCTION')
        generator = self._optional('STAR') is not None
        name = self._expect('IDENTIFIER').value
        params, body = self._fn_sig_body()
        return N.FnDecl(name, params, body, async_, generator)

    def _fn_sig_body(self):
        self._expect('LPAREN')
        params = []
        if not self._check('RPAREN'):
            while True:
                if self._check('ELLIPSIS'):
                    self._advance(); params.append({'type':'RestElement','argument':self._binding_target()})
                    break
                params.append(self._binding_element())
                if not self._optional('COMMA'): break
        self._expect('RPAREN')
        return params, self._block()

    def _class_decl(self):
        if _log.isEnabledFor(TRACE):
            _log.log(TRACE, "node ClassDeclaration")
        self._advance()
        name = self._expect('IDENTIFIER').value
        super_ = None
        if self._check('EXTENDS'):
            self._advance(); super_ = self._expect('IDENTIFIER').value
        self._expect('LBRACE')
        members = []
        while not self._check('RBRACE') and not self._check('EOF'):
            # skip stray semicolons (empty class body entries)
            while self._optional('SEMICOLON'):
                pass
            if self._check('RBRACE'): break
            static = bool(self._optional('STATIC'))
            # static block: static { ... }
            if static and self._check('LBRACE'):
                body = self._block()
                members.append(N.StaticBlock(body))
                continue
            is_async = bool(self._optional('ASYNC'))
            # Detect getter/setter: 'get'/'set' as IDENTIFIER followed by a property name (not '(')
            kind = 'method'
            if self._check('IDENTIFIER') and self._cur().value in ('get', 'set'):
                next_tok = self._peek()
                if next_tok.type in IDENTIFIER_NAME_TOKENS or next_tok.type in ('STRING', 'NUMBER', 'PRIVATE_NAME'):
                    kind = self._advance().value
            generator = self._optional('STAR') is not None
            # get key
            computed = False
            computed_key_node = None
            if self._check('PRIVATE_NAME'):
                key = self._advance().value
            elif self._check('STRING', 'NUMBER'):
                key = self._advance().value
            elif self._check('LBRACKET'):
                self._advance()
                computed_key_node = self._assign()
                self._expect('RBRACKET')
                key = '__computed__'
                computed = True
            else:
                key = self._consume_identifier_name()
            # decide: field vs method
            is_field = kind == 'method' and not generator and not is_async and not self._check('LPAREN') and not computed
            if is_field:
                value = None
                if self._optional('ASSIGN'):
                    value = self._assign()
                self._optional('SEMICOLON')
                members.append(N.ClassField(key, value, static))
            else:
                self._expect('LPAREN')
                params = []
                if not self._check('RPAREN'):
                    while True:
                        if self._check('ELLIPSIS'):
                            self._advance(); params.append({'type':'RestElement','argument':self._binding_target()}); break
                        params.append(self._binding_element())
                        if not self._optional('COMMA'): break
                self._expect('RPAREN')
                body = self._block()
                members.append({'key':key,'params':params,'body':body,'static':static,'kind':kind,'async':is_async,'generator':generator,'computed':computed,'computed_key':computed_key_node})
        self._expect('RBRACE')
        return N.ClassDecl(name, super_, members)

    def _import_decl(self):
        self._expect('IMPORT')
        # import './mod' (side-effect only)
        if self._check('STRING'):
            source = self._advance().value
            self._semi()
            return N.ImportDecl([], source)
        specifiers = []
        if self._check('STAR'):
            self._advance()  # *
            if self._check('IDENTIFIER') and self._cur().value == 'as':
                self._advance()
            name = self._expect('IDENTIFIER').value
            specifiers.append(N.ImportNs(name))
        elif self._check('LBRACE'):
            specifiers.extend(self._parse_import_specifiers())
        elif self._check('IDENTIFIER'):
            default_name = self._advance().value
            specifiers.append(N.ImportDefault(default_name))
            if self._check('COMMA'):
                self._advance()
                if self._check('STAR'):
                    self._advance()
                    if self._check('IDENTIFIER') and self._cur().value == 'as':
                        self._advance()
                    ns_name = self._expect('IDENTIFIER').value
                    specifiers.append(N.ImportNs(ns_name))
                elif self._check('LBRACE'):
                    specifiers.extend(self._parse_import_specifiers())
        self._expect('FROM')
        source = self._expect('STRING').value
        self._semi()
        return N.ImportDecl(specifiers, source)

    def _parse_import_specifiers(self):
        self._expect('LBRACE')
        specifiers = []
        while not self._check('RBRACE') and not self._check('EOF'):
            imported = self._consume_identifier_name()
            local = imported
            if self._check('IDENTIFIER') and self._cur().value == 'as':
                self._advance()
                local = self._expect('IDENTIFIER').value
            specifiers.append(N.ImportSpec(imported, local))
            if not self._optional('COMMA'):
                break
        self._expect('RBRACE')
        return specifiers

    def _export_decl(self):
        self._expect('EXPORT')
        if self._check('DEFAULT'):
            self._advance()
            if self._check('FUNCTION') or (self._check('ASYNC') and self._peek().type == 'FUNCTION'):
                decl = self._fn_expr()
                return N.ExportDefault(decl)
            elif self._check('CLASS'):
                decl = self._class_decl()
                return N.ExportDefault(decl)
            else:
                expr = self._assign()
                self._semi()
                return N.ExportDefault(expr)
        if self._check('STAR'):
            self._advance()
            self._expect('FROM')
            source = self._expect('STRING').value
            self._semi()
            return N.ExportList([], source)
        if self._check('LBRACE'):
            self._advance()
            specifiers = []
            while not self._check('RBRACE') and not self._check('EOF'):
                local = self._consume_identifier_name()
                exported = local
                if self._check('IDENTIFIER') and self._cur().value == 'as':
                    self._advance()
                    exported = self._consume_identifier_name()
                specifiers.append({'local': local, 'exported': exported})
                if not self._optional('COMMA'):
                    break
            self._expect('RBRACE')
            source = None
            if self._check('FROM'):
                self._advance()
                source = self._expect('STRING').value
            self._semi()
            return N.ExportList(specifiers, source)
        if self._check('VAR', 'LET', 'CONST'):
            return N.ExportDecl(self._var_decl())
        if self._check('FUNCTION'):
            return N.ExportDecl(self._fn_decl())
        if self._check('ASYNC') and self._peek().type == 'FUNCTION':
            return N.ExportDecl(self._fn_decl(async_=True))
        if self._check('CLASS'):
            return N.ExportDecl(self._class_decl())
        t = self._cur()
        raise SyntaxError(f"Line {t.line}:{t.col} — unexpected export token {t.type} {t.value!r}")

    def _expr_stmt(self):
        expr = self._expr()
        self._semi()
        return N.ExprStmt(expr)

    # -- expressions --------------------------------------------------------
    def _expr(self):
        return self._assign()

    def _assign(self):
        left = self._ternary()
        if self._check('ASSIGN','ASSIGN_ADD','ASSIGN_SUB','ASSIGN_MUL',
                       'ASSIGN_DIV','ASSIGN_MOD','ASSIGN_EXP',
                       'ASSIGN_AND','ASSIGN_OR','ASSIGN_BOOL_AND','ASSIGN_NULLISH','ASSIGN_BIT_OR','ASSIGN_XOR',
                       'ASSIGN_LSHIFT','ASSIGN_RSHIFT','ASSIGN_URSHIFT'):
            op = self._advance().value
            right = self._assign()
            if op == '=':
                left = self._assignment_target(left)
            return N.AssignExpr(op, left, right)
        return left

    def _assignment_target(self, node):
        tp = node.get('type')
        if tp in ('Identifier', 'MemberExpression', 'ObjectPattern', 'ArrayPattern'):
            return node
        if tp == 'ArrayExpression':
            elems = []
            for item in node['elements']:
                if item is None:
                    elems.append(None)
                elif item.get('type') == 'SpreadElement':
                    elems.append({'type': 'RestElement', 'argument': self._assignment_target(item['argument'])})
                elif item.get('type') == 'AssignmentExpression' and item.get('operator') == '=':
                    elems.append({'type': 'AssignmentPattern', 'left': self._assignment_target(item['left']), 'right': item['right']})
                else:
                    elems.append(self._assignment_target(item))
            return {'type': 'ArrayPattern', 'elements': elems}
        if tp == 'ObjectExpression':
            props = []
            for prop in node['properties']:
                if prop.get('type') == 'SpreadElement':
                    props.append({'type': 'RestElement', 'argument': self._assignment_target(prop['argument'])})
                    continue
                value = prop['value']
                if value.get('type') == 'AssignmentExpression' and value.get('operator') == '=':
                    value = {'type': 'AssignmentPattern', 'left': self._assignment_target(value['left']), 'right': value['right']}
                else:
                    value = self._assignment_target(value)
                props.append({'type': 'Property', 'key': prop['key'], 'value': value, 'computed': prop.get('computed', False), 'shorthand': prop.get('shorthand', False)})
            return {'type': 'ObjectPattern', 'properties': props}
        raise SyntaxError(f"Invalid assignment target: {tp}")

    def _simple_assignment_target(self):
        if self._check('IDENTIFIER'):
            tok = self._advance()
            node = N.Id(tok.value, tok.line)
        elif self._check('LBRACKET', 'LBRACE'):
            node = self._assignment_target(self._primary())
        else:
            raise SyntaxError('not a simple assignment target')
        while True:
            if self._check('LBRACKET'):
                self._advance()
                prop = self._expr()
                self._expect('RBRACKET')
                node = N.MemberExpr(node, prop, True)
            elif self._check('DOT'):
                self._advance()
                node = N.MemberExpr(node, N.Id(self._consume_identifier_name()), False)
            else:
                break
        return node

    def _ternary(self):
        test = self._nullish()
        if self._check('QUESTION'):
            self._advance()
            cons = self._assign()
            self._expect('COLON')
            alt = self._assign()
            return N.CondExpr(test, alt, cons)
        return test

    def _nullish(self):
        left = self._or()
        while self._check('NULLISH'):
            self._advance(); left = N.LogExpr('??', left, self._or())
        return left

    def _or(self):
        left = self._and()
        while self._check('OR'):
            self._advance(); left = N.LogExpr('||', left, self._and())
        return left

    def _and(self):
        left = self._bit_or()
        while self._check('AND'):
            self._advance(); left = N.LogExpr('&&', left, self._bit_or())
        return left

    def _bit_or(self):
        left = self._bit_xor()
        while self._check('BIT_OR'):
            self._advance(); left = N.BinExpr('|', left, self._bit_xor())
        return left

    def _bit_xor(self):
        left = self._bit_and()
        while self._check('BIT_XOR'):
            self._advance(); left = N.BinExpr('^', left, self._bit_and())
        return left

    def _bit_and(self):
        left = self._equality()
        while self._check('BIT_AND'):
            self._advance(); left = N.BinExpr('&', left, self._equality())
        return left

    def _equality(self):
        left = self._rel()
        while self._check('==','!=','===','!=='):
            op = self._advance().value; left = N.BinExpr(op, left, self._rel())
        return left

    def _rel(self):
        left = self._shift()
        while self._check('LT','GT','LTE','GTE','INSTANCEOF'):
            op = self._advance().value
            if op == 'instanceof':
                right = self._shift()
            else:
                right = self._shift()
            left = N.BinExpr(op, left, right)
        if self._check('IN'):
            self._advance(); left = N.BinExpr('in', left, self._shift())
        return left

    def _shift(self):
        left = self._add()
        while self._check('LSHIFT','RSHIFT','URSHIFT'):
            op = self._advance().value; left = N.BinExpr(op, left, self._add())
        return left

    def _add(self):
        left = self._mul()
        while self._check('PLUS','MINUS'):
            op = self._advance().value; left = N.BinExpr(op, left, self._mul())
        return left

    def _mul(self):
        left = self._exp()
        while self._check('STAR','SLASH','MOD'):
            op = self._advance().value; left = N.BinExpr(op, left, self._exp())
        return left

    def _exp(self):
        left = self._unary()
        if self._check('EXP'):
            self._advance(); left = N.BinExpr('**', left, self._exp())   # right-assoc
        return left

    def _unary(self):
        if self._check('INCREMENT','DECREMENT'):
            op = self._advance().value; return N.UpdateExpr(op, self._unary(), True)
        if self._check('MINUS'):
            self._advance(); return N.UnaryExpr('-', self._unary(), True)
        if self._check('PLUS'):
            self._advance(); return N.UnaryExpr('+', self._unary(), True)
        if self._check('BANG'):
            self._advance(); return N.UnaryExpr('!', self._unary(), True)
        if self._check('TILDE'):
            self._advance(); return N.UnaryExpr('~', self._unary(), True)
        if self._check('TYPEOF'):
            self._advance(); return N.UnaryExpr('typeof', self._unary(), True)
        if self._check('VOID'):
            self._advance(); return N.UnaryExpr('void', self._unary(), True)
        if self._check('DELETE'):
            self._advance(); return N.UnaryExpr('delete', self._unary(), True)
        if self._check('AWAIT'):
            self._advance(); return N.AwaitExpr(self._unary())
        if self._check('YIELD'):
            self._advance()
            delegate = bool(self._optional('STAR'))
            arg = None
            if not self._is_nl() and not self._check('SEMICOLON', 'RBRACE', 'RPAREN', 'RBRACKET', 'COLON', 'COMMA', 'EOF'):
                arg = self._assign()
            return {'type': 'YieldExpression', 'argument': arg, 'delegate': delegate}
        return self._postfix()

    def _postfix(self):
        node = self._call()
        if not self._is_nl():
            if self._check('INCREMENT'):
                self._advance(); return N.UpdateExpr('++', node, False)
            if self._check('DECREMENT'):
                self._advance(); return N.UpdateExpr('--', node, False)
        return node

    def _call(self):
        node = self._primary()
        while True:
            if self._check('LPAREN'):
                self._advance()
                args = []
                if not self._check('RPAREN'):
                    while True:
                        if self._check('ELLIPSIS'):
                            self._advance(); args.append(N.SpreadExpr(self._assign()))
                        else:
                            args.append(self._assign())
                        if not self._optional('COMMA'): break
                self._expect('RPAREN')
                node = CallExpr(node, args)
            elif self._check('LBRACKET'):
                self._advance(); prop = self._expr(); self._expect('RBRACKET')
                node = N.MemberExpr(node, prop, True)
            elif self._check('QDOT'):
                self._advance()
                if self._check('LPAREN'):
                    self._advance()
                    args = []
                    if not self._check('RPAREN'):
                        while True:
                            if self._check('ELLIPSIS'):
                                self._advance(); args.append(N.SpreadExpr(self._assign()))
                            else:
                                args.append(self._assign())
                            if not self._optional('COMMA'):
                                break
                    self._expect('RPAREN')
                    node = CallExpr(node, args, optional=True)
                elif self._check('LBRACKET'):
                    self._advance(); prop = self._expr(); self._expect('RBRACKET')
                    node = N.MemberExpr(node, prop, True, True)
                else:
                    node = N.MemberExpr(node, N.Id(self._consume_identifier_name()), False, True)
            elif self._check('DOT'):
                self._advance()
                if self._check('PRIVATE_NAME'):
                    prop = self._advance().value
                else:
                    prop = self._consume_identifier_name()
                node = N.MemberExpr(node, N.Id(prop), False)
            elif self._check('TEMPLATE'):
                tmpl_tok = self._advance()
                node = N._n("TaggedTemplateExpression", tag=node, quasi=N.TemplateExpr(tmpl_tok.value))
            else:
                break
        return node

    # -- primary ------------------------------------------------------------
    def _primary(self):
        t = self._cur()

        # literals
        if t.type == 'NUMBER':
            self._advance(); return N.Lit(t.value, 'number', t.line)
        if t.type == 'BIGINT':
            self._advance(); return N.Lit(t.value, 'bigint', t.line)
        if t.type == 'STRING':
            self._advance(); return N.Lit(t.value, 'string', t.line)
        if t.type == 'TRUE':
            self._advance(); return N.Lit(True, 'boolean', t.line)
        if t.type == 'FALSE':
            self._advance(); return N.Lit(False, 'boolean', t.line)
        if t.type == 'NULL':
            self._advance(); return N.Lit(None, 'null', t.line)
        if t.type == 'UNDEFINED':
            self._advance(); return N.Lit(None, 'undefined', t.line)

        # this
        if t.type == 'THIS':
            self._advance(); return N.ThisExpr()

        if t.type == 'SUPER':
            self._advance(); return N.Id('super', t.line)

        if t.type == 'ASYNC':
            if self._peek().type == 'FUNCTION':
                return self._fn_expr(async_=True)
            if self._peek().type == 'IDENTIFIER' and self._peek(2).type == 'ARROW':
                return self._parse_async_arrow()
            if self._peek().type == 'LPAREN' and self._is_async_arrow():
                return self._parse_async_arrow()

        # identifier  (maybe single-param arrow)
        if t.type == 'IDENTIFIER':
            self._advance()
            if self._check('ARROW'):
                self._advance()
                if self._check('LBRACE'):
                    return N.FnExpr(None, [t.value], self._block(), True)
                return N.FnExpr(None, [t.value], N.Block([N.RetStmt(self._assign())]), True)
            return N.Id(t.value, t.line)

        # parenthesised  (grouping or arrow function)
        if t.type == 'LPAREN':
            return self._paren_or_arrow()

        # array literal
        if t.type == 'LBRACKET':
            return self._array()

        # object literal
        if t.type == 'LBRACE':
            return self._object()

        # regex literal
        if t.type == 'REGEX':
            self._advance()
            source, flags = t.value
            return N._n("RegexLiteral", source=source, flags=flags)

        # function expression
        if t.type == 'FUNCTION':
            return self._fn_expr()

        # template literal
        if t.type == 'TEMPLATE':
            self._advance(); return N.TemplateExpr(t.value)

        # new
        if t.type == 'NEW':
            self._advance()
            # Check for new.target meta-property
            if self._check('DOT'):
                self._advance()
                if self._check('IDENTIFIER') and self._cur().value == 'target':
                    self._advance()
                    return {'type': 'MetaProperty', 'meta': 'new', 'property': 'target'}
            callee = self._primary()
            # Support member expressions in new callee: new Foo.Bar(...)
            while self._check('DOT') or self._check('LBRACKET'):
                if self._check('DOT'):
                    self._advance()
                    prop = self._consume_identifier_name()
                    callee = N.MemberExpr(callee, N.Id(prop), False)
                else:
                    self._advance()
                    prop = self._assign()
                    self._expect('RBRACKET')
                    callee = N.MemberExpr(callee, prop, True)
            self._expect('LPAREN')
            args = []
            if not self._check('RPAREN'):
                while True:
                    args.append(self._assign())
                    if not self._optional('COMMA'): break
            self._expect('RPAREN')
            return N.NewExpr(callee, args)

        # import.meta or dynamic import()
        if t.type == 'IMPORT':
            self._advance()
            if self._check('DOT'):
                self._advance()
                if self._check('IDENTIFIER') and self._cur().value == 'meta':
                    self._advance()
                    return {'type': 'ImportMeta'}
            if self._check('LPAREN'):
                self._advance()
                src = self._assign()
                self._expect('RPAREN')
                return {'type': 'DynamicImport', 'source': src}
            raise SyntaxError(f"Line {t.line}:{t.col} — unexpected token after import")

        raise SyntaxError(f"Line {t.line}:{t.col} — unexpected token {t.type} {t.value!r}")

    # -- parenthesised / arrow lookahead ------------------------------------
    def _comma_expr(self):
        """Parse expression list (comma operator), returning SequenceExpression or single expr."""
        expr = self._assign()
        if not self._check('COMMA'):
            return expr
        exprs = [expr]
        while self._optional('COMMA'):
            exprs.append(self._assign())
        return {'type': 'SequenceExpression', 'expressions': exprs}

    def _paren_or_arrow(self):
        # lookahead: try to detect ( p1, p2, ... ) =>
        if self._is_arrow_after_paren():
            return self._parse_arrow_params()
        # normal grouped expression
        self._advance()           # (
        expr = self._comma_expr()
        self._expect('RPAREN')
        return expr

    def _is_async_arrow(self):
        sv = self.pos
        ok = False
        try:
            self._expect('ASYNC')
            if self._check('IDENTIFIER'):
                self._advance()
                ok = self._check('ARROW')
            elif self._check('LPAREN'):
                ok = self._is_arrow_after_paren()
        except Exception:
            ok = False
        finally:
            self.pos = sv
        return ok

    def _parse_async_arrow(self):
        self._expect('ASYNC')
        if self._check('IDENTIFIER'):
            name = self._advance().value
            self._expect('ARROW')
            if self._check('LBRACE'):
                return N.FnExpr(None, [name], self._block(), True, True)
            return N.FnExpr(None, [name], N.Block([N.RetStmt(self._assign())]), True, True)
        return self._parse_arrow_params(async_=True)

    def _is_arrow_after_paren(self):
        """Lookahead without consuming tokens: is this (ident, ...) => ?"""
        sv = self.pos
        ok = False
        try:
            self._advance()       # (
            if self._check('RPAREN'):
                ok = self._peek().type == 'ARROW'
            else:
                while True:
                    if self._check('ELLIPSIS'):
                        self._advance()
                    if not self._check('IDENTIFIER'):
                        break
                    self._advance()
                    if self._check('ASSIGN'):
                        self._advance(); self._assign()   # skip default
                    if self._check('RPAREN'):
                        ok = self._peek().type == 'ARROW'
                        break
                    if not self._check('COMMA'):
                        break
                    self._advance()
        except Exception:
            ok = False
        finally:
            self.pos = sv
        return ok

    def _parse_arrow_params(self, async_=False):
        self._advance()           # (
        params = []
        if not self._check('RPAREN'):
            while True:
                if self._check('ELLIPSIS'):
                    self._advance()
                    params.append({'type':'RestElement','argument':self._binding_target()})
                    break
                params.append(self._binding_element())
                if not self._optional('COMMA'): break
        self._expect('RPAREN')
        self._expect('ARROW')
        if self._check('LBRACE'):
            return N.FnExpr(None, params, self._block(), True, async_)
        return N.FnExpr(None, params, N.Block([N.RetStmt(self._assign())]), True, async_)

    # -- array / object / function expression --------------------------------
    def _array(self):
        self._advance()
        elems = []
        while not self._check('RBRACKET') and not self._check('EOF'):
            if self._check('COMMA'):
                elems.append(None); self._advance(); continue
            if self._check('ELLIPSIS'):
                self._advance(); elems.append(N.SpreadExpr(self._assign()))
            else:
                elems.append(self._assign())
            if not self._optional('COMMA'): break
        self._expect('RBRACKET')
        return N.ArrExpr(elems)

    def _object(self):
        self._advance()
        props = []
        while not self._check('RBRACE') and not self._check('EOF'):
            if self._check('ELLIPSIS'):
                self._advance(); props.append({'type':'SpreadElement','argument':self._assign()})
                self._optional('COMMA'); continue
            # key
            if self._check('LBRACKET'):
                self._advance(); key = self._expr(); self._expect('RBRACKET'); comp = True
                if self._check('LPAREN'):
                    params, body = self._fn_sig_body()
                    props.append(N.Prop(key, N.FnExpr(None, params, body), True))
                    self._optional('COMMA')
                    continue
            elif self._check('STRING'):
                key = self._advance().value; comp = False
            elif self._check('NUMBER'):
                key = self._normalize_number_key(self._advance().value); comp = False
            elif self._is_identifier_name():
                ident = self._advance()
                # Check for getter/setter accessor
                if ident.value in ('get', 'set') and (self._is_identifier_name() or self._check('STRING', 'NUMBER')) and not self._check('LPAREN'):
                    accessor_kind = ident.value
                    if self._check('STRING'): acc_key = self._advance().value
                    elif self._check('NUMBER'): acc_key = self._normalize_number_key(self._advance().value)
                    else: acc_key = self._consume_identifier_name()
                    params, body = self._fn_sig_body()
                    props.append({'type': 'Property', 'key': acc_key, 'value': N.FnExpr(acc_key, params, body), 'computed': False, 'kind': accessor_kind})
                    self._optional('COMMA')
                    continue
                if self._check('LPAREN'):
                    # method shorthand
                    params, body = self._fn_sig_body()
                    props.append(N.Prop(ident.value, N.FnExpr(ident.value, params, body), False))
                    self._optional('COMMA'); continue
                if self._check('ASSIGN'):
                    self._advance()
                    props.append(N.Prop(ident.value, N.AssignExpr('=', N.Id(ident.value), self._assign()), False))
                    self._optional('COMMA'); continue
                if self._check('COMMA','RBRACE'):
                    props.append(N.Prop(ident.value, N.Id(ident.value), False, True))
                    self._optional('COMMA'); continue
                key = ident.value; comp = False
            else:
                break
            # value
            if self._check('COLON'):
                self._advance(); val = self._assign()
                props.append(N.Prop(key, val, comp))
            self._optional('COMMA')
        self._expect('RBRACE')
        return N.ObjExpr(props)

    def _fn_expr(self, async_=False):
        if self._check('ASYNC'):
            self._advance()
            async_ = True
        self._expect('FUNCTION')
        generator = self._optional('STAR') is not None
        name = self._expect('IDENTIFIER').value if self._check('IDENTIFIER') else None
        params, body = self._fn_sig_body()
        return N.FnExpr(name, params, body, False, async_, generator)
