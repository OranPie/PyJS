from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any


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


def _init_number_cache():
    """Populate the small integer cache and special float singletons."""
    global _JS_NAN, _JS_POS_INF, _JS_NEG_INF
    JsValue = _js_value_class()
    for i in range(-1, 256):
        _JS_SMALL_INTS[i] = JsValue("number", float(i))
    _JS_NAN = JsValue("number", float('nan'))
    _JS_POS_INF = JsValue("number", float('inf'))
    _JS_NEG_INF = JsValue("number", float('-inf'))


def py_to_js(val: Any):
    """Convert a Python value to a JsValue."""
    JsValue = _js_value_class()
    if isinstance(val, JsValue):
        return val
    if val is None:
        return JsValue("null", None)
    if isinstance(val, bool):
        return JsValue("boolean", val)
    if isinstance(val, int) and not isinstance(val, bool):
        if _JS_SMALL_INTS and -1 <= val <= 255:
            return _JS_SMALL_INTS[val]
        return JsValue("number", float(val))
    if isinstance(val, float):
        if _JS_NAN is not None:
            if math.isnan(val):
                return _JS_NAN
            if math.isinf(val):
                return _JS_POS_INF if val > 0 else _JS_NEG_INF
            ival = int(val)
            if val == ival and -1 <= ival <= 255:
                return _JS_SMALL_INTS[ival]
        return JsValue("number", val)
    if isinstance(val, str):
        return JsValue("string", val)
    if isinstance(val, list):
        return JsValue("array", [py_to_js(v) for v in val])
    if isinstance(val, dict):
        return JsValue("object", {k: py_to_js(v) for k, v in val.items()})
    return JsValue("undefined", None)


def js_to_py(val: 'JsValue'):
    """Convert a JsValue to a plain Python value."""
    if val.type in ("null", "undefined"):
        return None
    if val.type == "boolean":
        return bool(val.value)
    if val.type == "number":
        return val.value
    if val.type == "string":
        return val.value
    if val.type == "array":
        return [js_to_py(v) for v in val.value]
    if val.type == "object":
        return {k: js_to_py(v) for k, v in val.value.items()}
    return val.value
