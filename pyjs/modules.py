"""Module loader and registry for PyJS."""
from __future__ import annotations
import os
from pathlib import Path


class ModuleLoader:
    """
    Loads, caches and resolves JS modules.

    Each module is loaded once and cached by its resolved absolute path.
    Circular imports are detected (module returns empty namespace during loading).
    """

    def __init__(self, interpreter_factory):
        self._cache: dict = {}    # path -> exports dict (JsValue objects)
        self._loading: set = set()  # paths currently being loaded (cycle detection)
        self._interp_factory = interpreter_factory  # callable() -> Interpreter

    def resolve(self, specifier: str, from_file: str | None) -> str:
        """Resolve a module specifier to an absolute path."""
        if specifier.startswith('.'):
            base = os.path.dirname(from_file) if from_file else os.getcwd()
            path = os.path.normpath(os.path.join(base, specifier))
            for ext in ('', '.js', '.mjs'):
                candidate = path + ext
                if os.path.isfile(candidate):
                    return candidate
            raise FileNotFoundError(f"Cannot find module '{specifier}'")
        raise ImportError(f"Cannot resolve bare module specifier '{specifier}'")

    def load(self, path: str) -> dict:
        """Load a module and return its exports namespace dict."""
        if path in self._cache:
            return self._cache[path]
        if path in self._loading:
            # Circular import: return empty namespace stub
            return {}
        self._loading.add(path)
        source = Path(path).read_text('utf-8')
        interp = self._interp_factory()
        interp._module_loader = self
        interp._module_file = path
        interp.run_module(source)
        exports = dict(interp._module_exports)
        self._cache[path] = exports
        self._loading.discard(path)
        return exports
