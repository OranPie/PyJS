"""Centralized logging/tracing for the PyJS interpreter.

Silent by default.  Enable via:
  - Environment variable : PYJS_LOG_LEVEL=DEBUG  (or TRACE / INFO / WARNING)
  - Environment variable : PYJS_LOG_FILTER=exec,call,prop   (comma-separated logger names)
  - Interpreter kwarg    : Interpreter(log_level="DEBUG")
  - Direct call          : pyjs.trace.configure("DEBUG", log_filter="exec,call")

Log levels (in increasing verbosity):
  CRITICAL / ERROR / WARNING  — Python defaults (always silent for tracing)
  INFO     — High-level lifecycle events (module load, plugin init, scope create)
  DEBUG    — Detailed dispatch (every exec/eval/call/prop event)
  TRACE    — Finest detail (variable reads/writes, type coercions, loop iterations)

Named loggers (16):
  pyjs.lexer     — token production
  pyjs.parser    — AST node construction
  pyjs.exec      — statement execution (_exec dispatch)
  pyjs.eval      — expression evaluation (_eval dispatch)
  pyjs.call      — function invocations, entry/exit, params, returns
  pyjs.prop      — property get/set
  pyjs.event     — event loop ticks, microtasks, timers
  pyjs.promise   — promise create/resolve/reject/then/catch
  pyjs.scope     — scope (environment) create/destroy, variable declare/assign/read
  pyjs.error     — error throw/catch/propagation
  pyjs.module    — module loading, import/export
  pyjs.plugin    — plugin lifecycle (setup/teardown/hooks)
  pyjs.async     — async/await transitions, generator yield/return
  pyjs.coerce    — type coercions (_to_str, _to_num, _truthy)
  pyjs.timer     — setTimeout/setInterval scheduling and firing
  pyjs.proxy     — Proxy trap invocations
"""
from __future__ import annotations

import logging
import os
from typing import Optional

# ── Custom TRACE level (finer than DEBUG) ────────────────────────────

TRACE = 5
logging.addLevelName(TRACE, "TRACE")


def _trace(self: logging.Logger, message: str, *args: object, **kwargs: object) -> None:
    if self.isEnabledFor(TRACE):
        self._log(TRACE, message, args, **kwargs)


logging.Logger.trace = _trace  # type: ignore[attr-defined]

# ── Configuration ────────────────────────────────────────────────────

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
    "pyjs.scope",
    "pyjs.error",
    "pyjs.module",
    "pyjs.plugin",
    "pyjs.async",
    "pyjs.coerce",
    "pyjs.timer",
    "pyjs.proxy",
)

_FORMAT = "%(name)-14s %(levelname)-5s %(message)s"
_FORMAT_VERBOSE = "%(name)-14s %(levelname)-5s [%(pyjs_depth)s] %(message)s"


class _DepthFilter(logging.Filter):
    """Injects ``pyjs_depth`` into log records for call-depth indentation."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0

    def filter(self, record: logging.LogRecord) -> bool:
        indent = "│ " * self.depth
        record.pyjs_depth = indent  # type: ignore[attr-defined]
        return True


class _LoggerNameFilter(logging.Filter):
    """Only allows records from an allowlist of logger names."""

    def __init__(self, allowed: set[str]) -> None:
        super().__init__()
        self.allowed = allowed

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name in self.allowed


# Module-level depth tracker (shared across all loggers)
_depth_filter = _DepthFilter()

# Fast logging gate — True when DEBUG or finer logging is active.
# Mutable list so import sites get a live reference.
_any_enabled: list = [False]


def get_depth() -> int:
    """Return current call depth for indentation."""
    return _depth_filter.depth


def push_depth() -> None:
    """Increase call depth (on function entry)."""
    _depth_filter.depth += 1


def pop_depth() -> None:
    """Decrease call depth (on function exit)."""
    if _depth_filter.depth > 0:
        _depth_filter.depth -= 1


def configure(
    level: Optional[str] = None,
    log_filter: Optional[str] = None,
    verbose: bool = False,
) -> None:
    """Configure all pyjs loggers.

    Parameters
    ----------
    level : str or None
        Logging level name (TRACE, DEBUG, INFO, WARNING, ERROR, CRITICAL).
        Falls back to ``PYJS_LOG_LEVEL`` env var, then ``WARNING``.
    log_filter : str or None
        Comma-separated logger short names to enable (e.g. ``"exec,call,prop"``).
        Falls back to ``PYJS_LOG_FILTER`` env var.  ``None`` means all loggers.
    verbose : bool
        If True, include call-depth indentation in output format.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    # Resolve level
    if level is None:
        level = os.environ.get("PYJS_LOG_LEVEL", "WARNING")
    level_upper = level.upper()
    if level_upper == "TRACE":
        numeric = TRACE
    else:
        numeric = getattr(logging, level_upper, logging.WARNING)

    _any_enabled[0] = (numeric <= logging.DEBUG)

    # Resolve filter
    if log_filter is None:
        log_filter = os.environ.get("PYJS_LOG_FILTER", "")
    allowed: set[str] = set()
    if log_filter:
        for name in log_filter.split(","):
            name = name.strip()
            if name:
                full = f"pyjs.{name}" if not name.startswith("pyjs.") else name
                allowed.add(full)

    # Pick format
    fmt = _FORMAT_VERBOSE if verbose else _FORMAT
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(_depth_filter)
    if allowed:
        handler.addFilter(_LoggerNameFilter(allowed))

    root = logging.getLogger("pyjs")
    root.setLevel(numeric)
    if not root.handlers:
        root.addHandler(handler)
    root.propagate = False


def reconfigure(
    level: Optional[str] = None,
    log_filter: Optional[str] = None,
    verbose: bool = False,
) -> None:
    """Force reconfiguration (resets previous config)."""
    global _CONFIGURED
    root = logging.getLogger("pyjs")
    root.handlers.clear()
    _CONFIGURED = False
    _any_enabled[0] = False
    configure(level, log_filter, verbose)


def get_logger(name: str) -> logging.Logger:
    """Return a named pyjs logger (e.g. ``get_logger('exec')``)."""
    full = f"pyjs.{name}" if not name.startswith("pyjs.") else name
    return logging.getLogger(full)

