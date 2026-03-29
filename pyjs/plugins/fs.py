"""Sandboxed filesystem access plugin for PyJS."""
from __future__ import annotations
import os
import stat
import time

from ..plugin import PyJSPlugin, PluginContext
from ..values import JsValue, UNDEFINED, JS_NULL
from ..core import py_to_js
from ..exceptions import _JSError


class FileSystemPlugin(PyJSPlugin):
    name = "fs"
    version = "1.0.0"

    def __init__(self, root: str = ".", allow_write: bool = True):
        self._root = os.path.realpath(root)
        self._allow_write = allow_write

    def setup(self, ctx: PluginContext) -> None:
        plugin = self
        interp = ctx.get_interpreter()

        def _safe_path(raw_path, interp_inner):
            """Resolve a path and ensure it doesn't escape the sandbox root."""
            resolved = os.path.realpath(os.path.join(plugin._root, raw_path))
            if not resolved.startswith(plugin._root + os.sep) and resolved != plugin._root:
                raise _JSError(interp_inner._make_js_error(
                    'Error', f'Path traversal denied: {raw_path}'
                ))
            return resolved

        def _require_write(interp_inner):
            if not plugin._allow_write:
                raise _JSError(interp_inner._make_js_error(
                    'Error', 'File system write access is disabled'
                ))

        def read_file_sync(this_val, args, interp_inner):
            if not args:
                raise _JSError(interp_inner._make_js_error('TypeError', 'path is required'))
            raw = interp_inner._to_str(args[0])
            encoding = 'utf-8'
            if len(args) > 1 and args[1].type == 'string':
                encoding = args[1].value
            path = _safe_path(raw, interp_inner)
            try:
                with open(path, 'r', encoding=encoding) as f:
                    return py_to_js(f.read())
            except FileNotFoundError:
                raise _JSError(interp_inner._make_js_error(
                    'Error', f"ENOENT: no such file or directory, open '{raw}'"
                ))
            except OSError as e:
                raise _JSError(interp_inner._make_js_error('Error', str(e)))

        def write_file_sync(this_val, args, interp_inner):
            _require_write(interp_inner)
            if len(args) < 2:
                raise _JSError(interp_inner._make_js_error('TypeError', 'path and data are required'))
            raw = interp_inner._to_str(args[0])
            data = interp_inner._to_str(args[1])
            encoding = 'utf-8'
            if len(args) > 2 and args[2].type == 'string':
                encoding = args[2].value
            path = _safe_path(raw, interp_inner)
            try:
                with open(path, 'w', encoding=encoding) as f:
                    f.write(data)
            except OSError as e:
                raise _JSError(interp_inner._make_js_error('Error', str(e)))
            return UNDEFINED

        def exists_sync(this_val, args, interp_inner):
            if not args:
                return py_to_js(False)
            raw = interp_inner._to_str(args[0])
            try:
                path = _safe_path(raw, interp_inner)
                return py_to_js(os.path.exists(path))
            except _JSError:
                return py_to_js(False)

        def mkdir_sync(this_val, args, interp_inner):
            _require_write(interp_inner)
            if not args:
                raise _JSError(interp_inner._make_js_error('TypeError', 'path is required'))
            raw = interp_inner._to_str(args[0])
            recursive = False
            if len(args) > 1 and args[1].type == 'object' and isinstance(args[1].value, dict):
                rec_val = args[1].value.get('recursive')
                if rec_val and isinstance(rec_val, JsValue):
                    recursive = interp_inner._truthy(rec_val)
            path = _safe_path(raw, interp_inner)
            try:
                if recursive:
                    os.makedirs(path, exist_ok=True)
                else:
                    os.mkdir(path)
            except FileExistsError:
                raise _JSError(interp_inner._make_js_error(
                    'Error', f"EEXIST: file already exists, mkdir '{raw}'"
                ))
            except OSError as e:
                raise _JSError(interp_inner._make_js_error('Error', str(e)))
            return UNDEFINED

        def readdir_sync(this_val, args, interp_inner):
            if not args:
                raise _JSError(interp_inner._make_js_error('TypeError', 'path is required'))
            raw = interp_inner._to_str(args[0])
            path = _safe_path(raw, interp_inner)
            try:
                entries = os.listdir(path)
                return py_to_js(sorted(entries))
            except FileNotFoundError:
                raise _JSError(interp_inner._make_js_error(
                    'Error', f"ENOENT: no such file or directory, scandir '{raw}'"
                ))
            except OSError as e:
                raise _JSError(interp_inner._make_js_error('Error', str(e)))

        def stat_sync(this_val, args, interp_inner):
            if not args:
                raise _JSError(interp_inner._make_js_error('TypeError', 'path is required'))
            raw = interp_inner._to_str(args[0])
            path = _safe_path(raw, interp_inner)
            try:
                st = os.stat(path)
            except FileNotFoundError:
                raise _JSError(interp_inner._make_js_error(
                    'Error', f"ENOENT: no such file or directory, stat '{raw}'"
                ))
            except OSError as e:
                raise _JSError(interp_inner._make_js_error('Error', str(e)))

            is_file_val = stat.S_ISREG(st.st_mode)
            is_dir_val = stat.S_ISDIR(st.st_mode)

            def is_file(this_inner, args_inner, interp_i):
                return py_to_js(is_file_val)

            def is_directory(this_inner, args_inner, interp_i):
                return py_to_js(is_dir_val)

            stat_obj = JsValue('object', {})
            stat_obj.value['size'] = py_to_js(st.st_size)
            stat_obj.value['mtime'] = py_to_js(st.st_mtime * 1000)  # JS uses ms
            stat_obj.value['isFile'] = interp_inner._make_intrinsic(is_file, 'Stats.isFile')
            stat_obj.value['isDirectory'] = interp_inner._make_intrinsic(is_directory, 'Stats.isDirectory')
            return stat_obj

        def unlink_sync(this_val, args, interp_inner):
            _require_write(interp_inner)
            if not args:
                raise _JSError(interp_inner._make_js_error('TypeError', 'path is required'))
            raw = interp_inner._to_str(args[0])
            path = _safe_path(raw, interp_inner)
            try:
                os.unlink(path)
            except FileNotFoundError:
                raise _JSError(interp_inner._make_js_error(
                    'Error', f"ENOENT: no such file or directory, unlink '{raw}'"
                ))
            except OSError as e:
                raise _JSError(interp_inner._make_js_error('Error', str(e)))
            return UNDEFINED

        ctx.add_global_object('fs', {
            'readFileSync': read_file_sync,
            'writeFileSync': write_file_sync,
            'existsSync': exists_sync,
            'mkdirSync': mkdir_sync,
            'readdirSync': readdir_sync,
            'statSync': stat_sync,
            'unlinkSync': unlink_sync,
        })
