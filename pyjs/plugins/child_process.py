"""Child process plugin — provides shell command execution.

WARNING: This plugin gives JavaScript code full shell access. Only enable it
in trusted environments.
"""
from __future__ import annotations

import subprocess
import os

from ..plugin import PyJSPlugin, PluginContext
from ..values import JsValue, UNDEFINED
from ..core import py_to_js, js_to_py


class ChildProcessPlugin(PyJSPlugin):
    name = "child_process"
    version = "1.0.0"

    def __init__(self, allow_shell: bool = True, timeout: int = 30):
        self._allow_shell = allow_shell
        self._timeout = timeout

    def setup(self, ctx: PluginContext) -> None:
        default_timeout = self._timeout
        allow_shell = self._allow_shell

        def _parse_opts(args, index):
            """Extract cwd and timeout from an options argument."""
            opts = {}
            if len(args) > index and isinstance(args[index], JsValue) and args[index].type == 'object':
                opts = args[index].value if isinstance(args[index].value, dict) else {}
            cwd_val = opts.get('cwd')
            cwd = js_to_py(cwd_val) if isinstance(cwd_val, JsValue) else '.'
            timeout_val = opts.get('timeout')
            timeout = js_to_py(timeout_val) if isinstance(timeout_val, JsValue) else default_timeout
            return cwd, timeout

        # -- childProcess.execSync(command, options?) -----------------------

        def _exec_sync(this_val, args, interp):
            cmd = js_to_py(args[0]) if args else ''
            cwd, timeout = _parse_opts(args, 1)
            try:
                result = subprocess.run(
                    cmd, shell=allow_shell, capture_output=True, text=True,
                    cwd=cwd, timeout=timeout,
                )
                if result.returncode != 0:
                    raise Exception(
                        f"Command failed with exit code {result.returncode}: {result.stderr}")
                return py_to_js(result.stdout)
            except subprocess.TimeoutExpired:
                raise Exception(f"Command timed out after {timeout}s")

        # -- childProcess.exec(command, options?) ---------------------------

        def _exec_async(this_val, args, interp):
            cmd = js_to_py(args[0]) if args else ''
            cwd, timeout = _parse_opts(args, 1)
            try:
                result = subprocess.run(
                    cmd, shell=allow_shell, capture_output=True, text=True,
                    cwd=cwd, timeout=timeout,
                )
                obj = JsValue('object', {
                    'stdout': py_to_js(result.stdout),
                    'stderr': py_to_js(result.stderr),
                    'exitCode': py_to_js(result.returncode),
                })
                return interp._resolved_promise(obj)
            except subprocess.TimeoutExpired:
                return interp._rejected_promise(
                    py_to_js(f"Command timed out after {timeout}s"))
            except Exception as e:
                return interp._rejected_promise(py_to_js(str(e)))

        # -- childProcess.spawnSync(command, args?, options?) ----------------

        def _spawn_sync(this_val, args, interp):
            cmd = js_to_py(args[0]) if args else ''
            cmd_args = []
            opts_index = 1
            if len(args) > 1 and isinstance(args[1], JsValue) and args[1].type == 'array':
                cmd_args = [js_to_py(a) for a in args[1].value]
                opts_index = 2
            cwd, timeout = _parse_opts(args, opts_index)
            try:
                result = subprocess.run(
                    [cmd] + cmd_args, capture_output=True, text=True,
                    cwd=cwd, timeout=timeout,
                )
                return JsValue('object', {
                    'stdout': py_to_js(result.stdout),
                    'stderr': py_to_js(result.stderr),
                    'status': py_to_js(result.returncode),
                })
            except subprocess.TimeoutExpired:
                raise Exception(f"Command timed out after {timeout}s")

        ctx.add_global_object('childProcess', {
            'execSync': _exec_sync,
            'exec': _exec_async,
            'spawnSync': _spawn_sync,
        })
