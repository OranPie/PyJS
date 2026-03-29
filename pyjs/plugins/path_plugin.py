"""Path plugin — provides Node.js-like `path` global."""
from __future__ import annotations
import os

from ..plugin import PyJSPlugin


class PathPlugin(PyJSPlugin):
    """Exposes path.join, path.resolve, path.dirname, etc."""
    name = "path"
    version = "1.0.0"

    def setup(self, ctx):
        from ..values import JsValue, JS_TRUE, JS_FALSE
        from ..core import py_to_js, js_to_py

        def _join(this_val, args, interp):
            parts = [js_to_py(a) for a in args]
            return py_to_js(os.path.join(*parts) if parts else '.')

        def _resolve(this_val, args, interp):
            parts = [js_to_py(a) for a in args]
            if not parts:
                return py_to_js(os.getcwd())
            result = os.getcwd()
            for p in parts:
                if os.path.isabs(p):
                    result = p
                else:
                    result = os.path.join(result, p)
            return py_to_js(os.path.normpath(result))

        def _dirname(this_val, args, interp):
            p = js_to_py(args[0]) if args else ''
            return py_to_js(os.path.dirname(p))

        def _basename(this_val, args, interp):
            p = js_to_py(args[0]) if args else ''
            ext = js_to_py(args[1]) if len(args) > 1 else None
            base = os.path.basename(p)
            if ext and base.endswith(ext):
                base = base[:-len(ext)]
            return py_to_js(base)

        def _extname(this_val, args, interp):
            p = js_to_py(args[0]) if args else ''
            _, ext = os.path.splitext(p)
            return py_to_js(ext)

        def _normalize(this_val, args, interp):
            p = js_to_py(args[0]) if args else ''
            return py_to_js(os.path.normpath(p))

        def _relative(this_val, args, interp):
            from_path = js_to_py(args[0]) if args else os.getcwd()
            to_path = js_to_py(args[1]) if len(args) > 1 else os.getcwd()
            return py_to_js(os.path.relpath(to_path, from_path))

        def _is_absolute(this_val, args, interp):
            p = js_to_py(args[0]) if args else ''
            return JS_TRUE if os.path.isabs(p) else JS_FALSE

        def _parse(this_val, args, interp):
            p = js_to_py(args[0]) if args else ''
            dirname = os.path.dirname(p)
            basename = os.path.basename(p)
            name_part, ext = os.path.splitext(basename)
            root = os.sep if os.path.isabs(p) else ''
            return JsValue("object", {
                'root': py_to_js(root),
                'dir': py_to_js(dirname),
                'base': py_to_js(basename),
                'name': py_to_js(name_part),
                'ext': py_to_js(ext),
            })

        def _format(this_val, args, interp):
            if not args or args[0].type != 'object':
                return py_to_js('')
            obj = args[0].value
            dir_val = js_to_py(obj.get('dir', py_to_js('')))
            base_val = js_to_py(obj.get('base', py_to_js('')))
            name_val = js_to_py(obj.get('name', py_to_js('')))
            ext_val = js_to_py(obj.get('ext', py_to_js('')))
            result = base_val if base_val else name_val + ext_val
            if dir_val:
                result = os.path.join(dir_val, result)
            return py_to_js(result)

        # Build path object manually to support both methods and static props
        obj = JsValue("object", {})
        obj.value['join'] = ctx.make_function(_join, 'path.join')
        obj.value['resolve'] = ctx.make_function(_resolve, 'path.resolve')
        obj.value['dirname'] = ctx.make_function(_dirname, 'path.dirname')
        obj.value['basename'] = ctx.make_function(_basename, 'path.basename')
        obj.value['extname'] = ctx.make_function(_extname, 'path.extname')
        obj.value['normalize'] = ctx.make_function(_normalize, 'path.normalize')
        obj.value['relative'] = ctx.make_function(_relative, 'path.relative')
        obj.value['isAbsolute'] = ctx.make_function(_is_absolute, 'path.isAbsolute')
        obj.value['parse'] = ctx.make_function(_parse, 'path.parse')
        obj.value['format'] = ctx.make_function(_format, 'path.format')
        obj.value['sep'] = py_to_js(os.sep)
        obj.value['delimiter'] = py_to_js(os.pathsep)

        ctx.add_global('path', obj)
