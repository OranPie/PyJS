"""Process plugin — provides Node.js-like `process` global."""
from __future__ import annotations
import os
import sys
import platform

from ..plugin import PyJSPlugin


class ProcessPlugin(PyJSPlugin):
    """Exposes process.env, process.argv, process.cwd(), process.exit(), etc."""
    name = "process"
    version = "1.0.0"

    def __init__(self, argv=None):
        self._argv = argv or sys.argv[:]

    def setup(self, ctx):
        from ..values import JsValue, UNDEFINED
        from ..core import py_to_js, js_to_py

        def _cwd(this_val, args, interp):
            return py_to_js(os.getcwd())

        def _chdir(this_val, args, interp):
            if args:
                os.chdir(js_to_py(args[0]))
            return UNDEFINED

        def _exit_fn(this_val, args, interp):
            code = int(interp._to_num(args[0])) if args else 0
            raise SystemExit(code)

        def _hrtime(this_val, args, interp):
            import time
            ns = time.time_ns()
            return py_to_js([ns // 1_000_000_000, ns % 1_000_000_000])

        def _uptime(this_val, args, interp):
            import time
            return py_to_js(time.monotonic())

        # Build process object manually so we can mix methods and static props
        obj = JsValue("object", {})
        obj.value['cwd'] = ctx.make_function(_cwd, 'process.cwd')
        obj.value['chdir'] = ctx.make_function(_chdir, 'process.chdir')
        obj.value['exit'] = ctx.make_function(_exit_fn, 'process.exit')
        obj.value['hrtime'] = ctx.make_function(_hrtime, 'process.hrtime')
        obj.value['uptime'] = ctx.make_function(_uptime, 'process.uptime')

        # Static properties
        obj.value['pid'] = py_to_js(os.getpid())
        obj.value['platform'] = py_to_js(sys.platform)
        mapping = {'x86_64': 'x64', 'AMD64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}
        obj.value['arch'] = py_to_js(mapping.get(platform.machine(), platform.machine()))
        obj.value['version'] = py_to_js(
            f"v{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )
        obj.value['argv'] = py_to_js(self._argv)

        # env as a live object snapshot
        env_obj = JsValue("object", {})
        for k, v in os.environ.items():
            env_obj.value[k] = py_to_js(v)
        obj.value['env'] = env_obj

        ctx.add_global('process', obj)
