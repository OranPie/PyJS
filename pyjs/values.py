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

_symbol_id_counter = [11]  # incremented for each new Symbol()
_symbol_registry   = {}    # for Symbol.for()
