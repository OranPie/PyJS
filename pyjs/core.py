from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from .trace import get_logger, TRACE, _any_enabled as _TRACE_ACTIVE

_log = get_logger("coerce")


class JSTypeError(Exception):
    """Raised for type errors during interpretation."""


if TYPE_CHECKING:
    from .values import JsValue


def _js_value_class():
    from .values import JsValue

    return JsValue


_JS_SMALL_INTS: dict = {}  # interned number JsValues for integers -1..255
_JS_NAN = None
_JS_POS_INF = None
_JS_NEG_INF = None
_JsValue = None  # resolved reference to JsValue class (set by _init_number_cache)
_UNDEFINED = None  # resolved reference to UNDEFINED singleton
_JS_NULL_REF = None  # resolved reference to JS_NULL singleton
_JS_TRUE_REF = None  # resolved reference to JS_TRUE singleton
_JS_FALSE_REF = None  # resolved reference to JS_FALSE singleton


def _init_number_cache():
    """Populate the small integer cache, special float singletons, and resolve class refs."""
    global _JS_NAN, _JS_POS_INF, _JS_NEG_INF, _JsValue
    global _UNDEFINED, _JS_NULL_REF, _JS_TRUE_REF, _JS_FALSE_REF
    from .values import JsValue, UNDEFINED, JS_NULL, JS_TRUE, JS_FALSE
    _JsValue = JsValue
    _UNDEFINED = UNDEFINED
    _JS_NULL_REF = JS_NULL
    _JS_TRUE_REF = JS_TRUE
    _JS_FALSE_REF = JS_FALSE
    for i in range(-1, 256):
        _JS_SMALL_INTS[i] = JsValue("number", float(i))
    _JS_NAN = JsValue("number", float('nan'))
    _JS_POS_INF = JsValue("number", float('inf'))
    _JS_NEG_INF = JsValue("number", float('-inf'))


def py_to_js(val: Any):
    """Convert a Python value to a JsValue."""
    _cls = val.__class__
    if _cls is _JsValue:
        return val
    if val is None:
        return _JS_NULL_REF
    if _cls is bool:
        return _JS_TRUE_REF if val else _JS_FALSE_REF
    if _cls is int:
        if -1 <= val <= 255:
            return _JS_SMALL_INTS[val]
        return _JsValue("number", float(val))
    if _cls is float:
        if val != val:  # NaN check (faster than math.isnan)
            return _JS_NAN
        if math.isinf(val):
            return _JS_POS_INF if val > 0 else _JS_NEG_INF
        ival = int(val)
        if val == ival and -1 <= ival <= 255:
            return _JS_SMALL_INTS[ival]
        return _JsValue("number", val)
    if _cls is str:
        return _JsValue("string", val)
    if _cls is list:
        return _JsValue("array", [py_to_js(v) for v in val])
    if _cls is dict:
        return _JsValue("object", {k: py_to_js(v) for k, v in val.items()})
    return _UNDEFINED


def js_to_py(val: 'JsValue'):
    """Convert a JsValue to a plain Python value."""
    t = val.type
    if t == "number":
        return val.value
    if t == "string":
        return val.value
    if t == "boolean":
        return bool(val.value)
    if t in ("null", "undefined"):
        return None
    if t == "array":
        return [js_to_py(v) for v in val.value]
    if t == "object":
        return {k: js_to_py(v) for k, v in val.value.items()}
    return val.value
