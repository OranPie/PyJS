"""Lexical environment / scope chain for the PyJS interpreter."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .core import JSTypeError
from .trace import get_logger, TRACE, _any_enabled as _TRACE_ACTIVE
from .values import JsValue, UNDEFINED

_log = get_logger("scope")

# Sentinel for Temporal Dead Zone — let/const declared but not yet initialized
_TDZ_SENTINEL = object()


class Environment:
    __slots__ = ('parent', 'bindings', '_this', '_fn_args', '_is_arrow', '_is_fn_env', '_generator', '_fn_val', '_strict', '_using_stack')

    def __init__(self, parent: Optional['Environment'] = None):
        self.parent = parent
        self.bindings: Dict[str, Any] = {}       # name -> (keyword, JsValue)
        self._this = UNDEFINED
        self._fn_args: List[JsValue] = []
        self._is_arrow: bool = False
        self._is_fn_env: bool = False
        self._generator = None
        self._fn_val = None
        self._strict: bool = parent._strict if parent else False
        self._using_stack = None  # list of (val, sym_key, is_async) for `using` declarations

    def declare(self, name, value, keyword='var'):
        if _TRACE_ACTIVE[0]:
            _log.debug("declare %s %s", keyword, name)
        if keyword == 'const':
            if name in self.bindings:
                if self.bindings[name][1] is not _TDZ_SENTINEL:
                    raise JSTypeError(f"Identifier '{name}' has already been declared")
            self.bindings[name] = ['const', value]
        elif keyword == 'let':
            if name in self.bindings:
                if self.bindings[name][1] is not _TDZ_SENTINEL:
                    raise JSTypeError(f"Identifier '{name}' has already been declared")
            self.bindings[name] = ['let', value]
        else:  # var — hoist to nearest function/program scope
            target = self
            while target.parent and not target._is_fn_env:
                target = target.parent
            target.bindings[name] = ['var', value]

    def declare_tdz(self, name, keyword='let'):
        """Declare a let/const binding in TDZ (uninitialized)."""
        self.bindings[name] = [keyword, _TDZ_SENTINEL]

    def has(self, name):
        if name in self.bindings: return True
        return self.parent.has(name) if self.parent else False

    def _find(self, name):
        e = self
        while e:
            if name in e.bindings: return e
            e = e.parent
        return None

    def get(self, name):
        _bindings = self.bindings
        if name in _bindings:
            b = _bindings[name]
            if b[1] is _TDZ_SENTINEL:
                raise ReferenceError(f"Cannot access '{name}' before initialization")
            return b[1]
        e = self.parent
        while e is not None:
            _b = e.bindings
            if name in _b:
                b = _b[name]
                if b[1] is _TDZ_SENTINEL:
                    raise ReferenceError(f"Cannot access '{name}' before initialization")
                return b[1]
            e = e.parent
        raise ReferenceError(f"{name} is not defined")

    def set(self, name, value):
        _bindings = self.bindings
        if name in _bindings:
            b = _bindings[name]
            if b[0] == 'const' and b[1] is not _TDZ_SENTINEL:
                raise JSTypeError(f"Assignment to constant variable '{name}'")
            b[1] = value
            return
        e = self.parent
        while e is not None:
            _b = e.bindings
            if name in _b:
                b = _b[name]
                if b[0] == 'const' and b[1] is not _TDZ_SENTINEL:
                    raise JSTypeError(f"Assignment to constant variable '{name}'")
                b[1] = value
                return
            e = e.parent
        raise ReferenceError(f"{name} is not defined")

    def set_own(self, name, value):
        _bindings = self.bindings
        if name not in _bindings:
            raise ReferenceError(f"{name} is not defined")
        b = _bindings[name]
        if b[0] == 'const' and b[1] is not _TDZ_SENTINEL:
            raise JSTypeError(f"Assignment to constant variable '{name}'")
        b[1] = value
