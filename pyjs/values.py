"""Core JS value types, singletons, and symbol infrastructure."""
from __future__ import annotations

import re
from typing import Any


def _js_regex_to_python(pattern: str) -> str:
    """Convert JS regex named-group syntax to Python re syntax."""
    result = re.sub(r'\(\?<([^>]+)>', r'(?P<\1>', pattern)
    result = re.sub(r'\\k<([^>]+)>', r'(?P=\1)', result)
    return result


class JsProxy:
    """Wraps a JsValue so that property access goes through handler traps."""
    __slots__ = ('target', 'handler')
    def __init__(self, target: 'JsValue', handler: 'JsValue'):
        self.target = target
        self.handler = handler


class JsValue:
    __slots__ = ('type', 'value', 'extras')
    def __init__(self, tp: str, val: Any):
        self.type = tp; self.value = val; self.extras = None
    def __repr__(self):
        if self.type == 'null': return 'null'
        if self.type == 'undefined': return 'undefined'
        return f"JsValue({self.type}, {self.value!r})"


UNDEFINED = JsValue("undefined", None)
JS_NULL   = JsValue("null", None)
JS_TRUE   = JsValue("boolean", True)
JS_FALSE  = JsValue("boolean", False)

# Well-known Symbol IDs (fixed)
SYMBOL_ITERATOR           = 1
SYMBOL_TO_PRIMITIVE       = 2
SYMBOL_HAS_INSTANCE       = 3
SYMBOL_TO_STRING_TAG      = 4
SYMBOL_ASYNC_ITERATOR     = 5
SYMBOL_SPECIES            = 6
SYMBOL_MATCH              = 7
SYMBOL_REPLACE            = 8
SYMBOL_SPLIT              = 9
SYMBOL_SEARCH             = 10
SYMBOL_IS_CONCAT_SPREADABLE = 11
SYMBOL_DISPOSE            = 12
SYMBOL_ASYNC_DISPOSE      = 13

_symbol_id_counter = [11]  # incremented for each new Symbol()
_symbol_registry   = {}    # for Symbol.for()

# Pre-computed @@N@@ symbol key strings for hot-path property access
SK_ITERATOR           = f"@@{SYMBOL_ITERATOR}@@"
SK_TO_PRIMITIVE       = f"@@{SYMBOL_TO_PRIMITIVE}@@"
SK_HAS_INSTANCE       = f"@@{SYMBOL_HAS_INSTANCE}@@"
SK_TO_STRING_TAG      = f"@@{SYMBOL_TO_STRING_TAG}@@"
SK_ASYNC_ITERATOR     = f"@@{SYMBOL_ASYNC_ITERATOR}@@"
SK_SPECIES            = f"@@{SYMBOL_SPECIES}@@"
SK_MATCH              = f"@@{SYMBOL_MATCH}@@"
SK_REPLACE            = f"@@{SYMBOL_REPLACE}@@"
SK_SPLIT              = f"@@{SYMBOL_SPLIT}@@"
SK_SEARCH             = f"@@{SYMBOL_SEARCH}@@"
SK_IS_CONCAT_SPREADABLE = f"@@{SYMBOL_IS_CONCAT_SPREADABLE}@@"
SK_DISPOSE            = f"@@{SYMBOL_DISPOSE}@@"
SK_ASYNC_DISPOSE      = f"@@{SYMBOL_ASYNC_DISPOSE}@@"

# Populate the small-integer / NaN / Inf number cache now that JsValue exists
from .core import _init_number_cache as _init_nc  # noqa: E402
_init_nc()
del _init_nc
