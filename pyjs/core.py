from __future__ import annotations

from typing import TYPE_CHECKING, Any


class JSTypeError(Exception):
    """Raised for type errors during interpretation."""


if TYPE_CHECKING:
    from .values import JsValue


def _js_value_class():
    from .values import JsValue

    return JsValue


_JS_SMALL_INTS: dict = {}  # interned number JsValues for integers -1..255


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
        return JsValue("number", float(val))
    if isinstance(val, float):
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
