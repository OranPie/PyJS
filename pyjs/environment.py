"""Lexical environment / scope chain for the PyJS interpreter."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .core import JSTypeError
from .trace import get_logger, TRACE
from .values import JsValue, UNDEFINED

_log = get_logger("scope")

# Sentinel for Temporal Dead Zone — let/const declared but not yet initialized
_TDZ_SENTINEL = object()


class Environment:
    __slots__ = ('parent', 'bindings', '_this', '_fn_args', '_is_arrow', '_is_fn_env', '_generator', '_fn_val', '_strict')

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
        if parent is None:
            _log.info("scope create (global)")
        else:
            _log.log(TRACE, "scope create (child)")

    def declare(self, name, value, keyword='var'):
        _log.debug("declare %s %s", keyword, name)
        if keyword == 'const':
            if name in self.bindings:
                if self.bindings[name][1] is not _TDZ_SENTINEL:
                    raise JSTypeError(f"Identifier '{name}' has already been declared")
            self.bindings[name] = ('const', value)
        elif keyword == 'let':
            if name in self.bindings:
                if self.bindings[name][1] is not _TDZ_SENTINEL:
                    raise JSTypeError(f"Identifier '{name}' has already been declared")
            self.bindings[name] = ('let', value)
        else:  # var — hoist to nearest function/program scope
            target = self
            while target.parent and not target._is_fn_env:
                target = target.parent
            target.bindings[name] = ('var', value)

    def declare_tdz(self, name, keyword='let'):
        """Declare a let/const binding in TDZ (uninitialized)."""
        self.bindings[name] = (keyword, _TDZ_SENTINEL)

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
        if _log.isEnabledFor(TRACE):
            _log.log(TRACE, "get %s", name)
        e = self._find(name)
        if not e:
            raise ReferenceError(f"{name} is not defined")
        val = e.bindings[name][1]
        if val is _TDZ_SENTINEL:
            raise ReferenceError(f"Cannot access '{name}' before initialization")
        return val

    def set(self, name, value):
        if _log.isEnabledFor(TRACE):
            _log.log(TRACE, "set %s", name)
        e = self._find(name)
        if not e:
            raise ReferenceError(f"{name} is not defined")
        if e.bindings[name][0] == 'const' and e.bindings[name][1] is not _TDZ_SENTINEL:
            raise JSTypeError(f"Assignment to constant variable '{name}'")
        e.bindings[name] = (e.bindings[name][0], value)

    def set_own(self, name, value):
        if name not in self.bindings:
            raise ReferenceError(f"{name} is not defined")
        if self.bindings[name][0] == 'const' and self.bindings[name][1] is not _TDZ_SENTINEL:
            raise JSTypeError(f"Assignment to constant variable '{name}'")
        self.bindings[name] = (self.bindings[name][0], value)
