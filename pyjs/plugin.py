"""Plugin system for the PyJS interpreter."""
from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import Interpreter

from .values import JsValue, UNDEFINED
from .core import py_to_js


class PluginContext:
    """Safe interface passed to plugins during setup — exposes registration helpers.

    Plugins use this to add globals, methods, and constructors to the JS environment
    without directly touching interpreter internals.
    """

    def __init__(self, interpreter: 'Interpreter'):
        self._interp = interpreter
        self._env = interpreter.genv
        self._registered_globals: List[str] = []
        self._registered_methods: List[tuple] = []

    def add_global(self, name: str, value, *, writable: bool = True) -> None:
        """Add a global variable or function to the JS environment.

        If value is a callable Python function, it's wrapped as a JS intrinsic.
        The callable should have signature: (this_val: JsValue, args: List[JsValue], interp: Interpreter) -> JsValue

        If value is anything else, it's converted to a JsValue via py_to_js.
        """
        if callable(value) and not isinstance(value, JsValue):
            js_val = self._interp._make_intrinsic(value, name)
        elif isinstance(value, JsValue):
            js_val = value
        else:
            js_val = py_to_js(value)

        keyword = 'var' if writable else 'const'
        self._env.declare(name, js_val, keyword)
        # Sync with globalThis
        self._interp._sync_global_binding(name, js_val, self._env)
        self._registered_globals.append(name)

    def add_global_object(self, name: str, methods: Dict[str, Callable]) -> JsValue:
        """Add a global object with methods (e.g., localStorage with getItem, setItem, etc.).

        Each method callable should have signature:
            (this_val: JsValue, args: List[JsValue], interp: Interpreter) -> JsValue

        Returns the created JS object.
        """
        obj = JsValue('object', {})
        for method_name, fn in methods.items():
            obj.value[method_name] = self._interp._make_intrinsic(fn, f'{name}.{method_name}')
        self._env.declare(name, obj, 'var')
        self._interp._sync_global_binding(name, obj, self._env)
        self._registered_globals.append(name)
        return obj

    def add_method(self, type_name: str, method_name: str, fn: Callable) -> None:
        """Add a method to an existing JS type (String, Array, Object, Number).

        type_name: one of 'string', 'array', 'number', 'object', 'promise'
        fn: callable with signature (this_val, args, interp) -> JsValue
        """
        method_sets = {
            'array': 'ARRAY_METHODS',
            'string': 'STRING_METHODS',
            'number': 'NUMBER_METHODS',
            'promise': 'PROMISE_METHODS',
        }
        attr = method_sets.get(type_name)
        if attr:
            current = getattr(self._interp, attr)
            # Convert frozenset to a mutable set, add the method, convert back
            new_set = set(current)
            new_set.add(method_name)
            setattr(self._interp, attr, frozenset(new_set))

        # Register the handler in a plugin method registry
        if not hasattr(self._interp, '_plugin_methods'):
            self._interp._plugin_methods = {}
        self._interp._plugin_methods[(type_name, method_name)] = fn
        self._registered_methods.append((type_name, method_name))

    def add_constructor(self, name: str, fn: Callable) -> None:
        """Add a constructor function (callable with `new`).

        fn signature: (this_val, args, interp) -> JsValue
        The function will receive a fresh object as this_val when called with `new`.
        """
        js_fn = self._interp._make_intrinsic(fn, name)
        # Mark as constructor
        if js_fn.extras is None:
            js_fn.extras = {}
        js_fn.extras['construct'] = True
        self._env.declare(name, js_fn, 'var')
        self._interp._sync_global_binding(name, js_fn, self._env)
        self._registered_globals.append(name)

    def get_interpreter(self) -> 'Interpreter':
        """Get the interpreter instance (for advanced use)."""
        return self._interp

    def make_js_value(self, py_val) -> JsValue:
        """Convert a Python value to a JsValue."""
        return py_to_js(py_val)

    def make_error(self, name: str, message: str) -> JsValue:
        """Create a JS error object."""
        return self._interp._make_js_error(name, message)

    def make_function(self, fn: Callable, name: str = '?') -> JsValue:
        """Create a JS function from a Python callable.

        fn signature: (this_val, args, interp) -> JsValue
        """
        return self._interp._make_intrinsic(fn, name)


class PyJSPlugin:
    """Base class for PyJS plugins.

    Subclass this and override setup() to register your plugin's functionality.

    Example:
        class MyPlugin(PyJSPlugin):
            name = "my-plugin"
            version = "1.0.0"

            def setup(self, ctx: PluginContext) -> None:
                ctx.add_global('myFunction', lambda this, args, interp: py_to_js("hello"))
    """
    name: str = "unnamed"
    version: str = "0.0.0"

    def setup(self, ctx: PluginContext) -> None:
        """Called when the plugin is registered with an Interpreter.

        Override this to add globals, methods, constructors, etc.
        """
        pass

    def teardown(self, ctx: PluginContext) -> None:
        """Called when the interpreter is being cleaned up.

        Override this for resource cleanup.
        """
        pass

    def on_error(self, error: Exception, ctx: PluginContext) -> None:
        """Called when an error occurs in plugin-registered code.

        Override this for custom error handling/logging.
        """
        pass

    def __repr__(self) -> str:
        return f"<PyJSPlugin {self.name}@{self.version}>"
