"""Centralized logging/tracing for the PyJS interpreter.

Silent by default. Enable via:
  - Environment variable: PYJS_LOG_LEVEL=DEBUG
  - Interpreter kwarg: Interpreter(log_level="DEBUG")
  - Direct call: pyjs.trace.configure("DEBUG")

Named loggers:
  pyjs.lexer   — token production
  pyjs.parser  — AST node construction
  pyjs.exec    — statement execution (_exec dispatch)
  pyjs.eval    — expression evaluation (_eval dispatch)
  pyjs.call    — function invocations (_call_js)
  pyjs.prop    — property get/set
  pyjs.event   — event loop ticks, microtasks, timers
  pyjs.promise — promise state transitions
"""
from __future__ import annotations

import logging
import os

_CONFIGURED = False

LOGGER_NAMES = (
    "pyjs.lexer",
    "pyjs.parser",
    "pyjs.exec",
    "pyjs.eval",
    "pyjs.call",
    "pyjs.prop",
    "pyjs.event",
    "pyjs.promise",
)

_FORMAT = "%(name)s %(levelname)s %(message)s"


def configure(level: str | None = None) -> None:
    """Configure all pyjs loggers. Called once; subsequent calls are no-ops."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    if level is None:
        level = os.environ.get("PYJS_LOG_LEVEL", "WARNING")
    numeric = getattr(logging, level.upper(), logging.WARNING)

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))

    root = logging.getLogger("pyjs")
    root.setLevel(numeric)
    if not root.handlers:
        root.addHandler(handler)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a named pyjs logger (e.g. ``get_logger('exec')``)."""
    full = f"pyjs.{name}" if not name.startswith("pyjs.") else name
    return logging.getLogger(full)
