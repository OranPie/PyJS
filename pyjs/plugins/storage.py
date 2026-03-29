"""localStorage and sessionStorage plugin for PyJS."""
from __future__ import annotations
import json
import os
from typing import Optional

from ..plugin import PyJSPlugin, PluginContext
from ..values import JsValue, UNDEFINED, JS_NULL
from ..core import py_to_js


class StoragePlugin(PyJSPlugin):
    name = "storage"
    version = "1.0.0"

    def __init__(self, persist_path: Optional[str] = None):
        """
        persist_path: If set, localStorage data is persisted to this JSON file.
                      sessionStorage is always memory-only.
        """
        self._persist_path = persist_path
        self._local_data: dict[str, str] = {}
        self._session_data: dict[str, str] = {}

    def setup(self, ctx: PluginContext) -> None:
        if self._persist_path and os.path.exists(self._persist_path):
            try:
                with open(self._persist_path, 'r') as f:
                    self._local_data = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        ctx.add_global_object('localStorage', self._make_storage_methods(self._local_data, persist=True))
        ctx.add_global_object('sessionStorage', self._make_storage_methods(self._session_data, persist=False))

    def _persist(self):
        if self._persist_path:
            try:
                with open(self._persist_path, 'w') as f:
                    json.dump(self._local_data, f)
            except OSError:
                pass

    def _make_storage_methods(self, data: dict, persist: bool) -> dict:
        plugin = self

        def get_item(this_val, args, interp):
            key = interp._to_str(args[0]) if args else 'undefined'
            val = data.get(key)
            return JS_NULL if val is None else py_to_js(val)

        def set_item(this_val, args, interp):
            key = interp._to_str(args[0]) if args else 'undefined'
            val = interp._to_str(args[1]) if len(args) > 1 else 'undefined'
            data[key] = val
            if persist:
                plugin._persist()
            return UNDEFINED

        def remove_item(this_val, args, interp):
            key = interp._to_str(args[0]) if args else 'undefined'
            data.pop(key, None)
            if persist:
                plugin._persist()
            return UNDEFINED

        def clear(this_val, args, interp):
            data.clear()
            if persist:
                plugin._persist()
            return UNDEFINED

        def key_fn(this_val, args, interp):
            idx = int(args[0].value) if args and args[0].type == 'number' else 0
            keys = list(data.keys())
            return py_to_js(keys[idx]) if 0 <= idx < len(keys) else JS_NULL

        def get_length(this_val, args, interp):
            return py_to_js(len(data))

        return {
            'getItem': get_item,
            'setItem': set_item,
            'removeItem': remove_item,
            'clear': clear,
            'key': key_fn,
            'length': get_length,
        }

    def teardown(self, ctx):
        self._persist()
