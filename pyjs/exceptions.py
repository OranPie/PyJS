"""Internal control-flow exceptions used by the PyJS interpreter."""
from __future__ import annotations

from .values import JsValue


class _JSBreak(Exception):
    def __init__(self, label=None): self.label = label

class _JSContinue(Exception):
    def __init__(self, label=None): self.label = label

class _JSReturn(Exception):
    __slots__ = ('value',)
    def __init__(self, value): self.value = value

class _JSError(Exception):
    __slots__ = ('value',)
    def __init__(self, value: JsValue): self.value = value


def flatten_one(lst):
    result = []
    for x in lst:
        if isinstance(x, JsValue) and x.type == "array":
            result.extend(x.value)
        else:
            result.append(x)
    return result
