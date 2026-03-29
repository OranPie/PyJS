"""Node.js-compatible EventEmitter plugin for PyJS."""
from __future__ import annotations

from ..plugin import PyJSPlugin, PluginContext
from ..values import JsValue, UNDEFINED
from ..core import py_to_js


_LISTENERS_KEY = '__event_listeners__'


class EventEmitterPlugin(PyJSPlugin):
    name = "events"
    version = "1.0.0"

    def setup(self, ctx: PluginContext) -> None:
        interp = ctx.get_interpreter()

        def _get_listeners(obj):
            """Get or create the listeners dict on an emitter object."""
            if not isinstance(obj.value, dict):
                return {}
            listeners = obj.value.get(_LISTENERS_KEY)
            if listeners is None:
                listeners = {}
                obj.value[_LISTENERS_KEY] = listeners
            return listeners

        def on_fn(this_val, args, interp_inner):
            if len(args) < 2:
                return this_val
            event = interp_inner._to_str(args[0])
            callback = args[1]
            listeners = _get_listeners(this_val)
            listeners.setdefault(event, []).append({'fn': callback, 'once': False})
            return this_val

        def once_fn(this_val, args, interp_inner):
            if len(args) < 2:
                return this_val
            event = interp_inner._to_str(args[0])
            callback = args[1]
            listeners = _get_listeners(this_val)
            listeners.setdefault(event, []).append({'fn': callback, 'once': True})
            return this_val

        def off_fn(this_val, args, interp_inner):
            if len(args) < 2:
                return this_val
            event = interp_inner._to_str(args[0])
            callback = args[1]
            listeners = _get_listeners(this_val)
            lst = listeners.get(event, [])
            listeners[event] = [e for e in lst if e['fn'] is not callback]
            return this_val

        def emit_fn(this_val, args, interp_inner):
            if not args:
                return py_to_js(False)
            event = interp_inner._to_str(args[0])
            emit_args = list(args[1:])
            listeners = _get_listeners(this_val)
            lst = listeners.get(event, [])
            if not lst:
                return py_to_js(False)
            # Copy to avoid mutation during iteration
            to_call = list(lst)
            # Remove once listeners before calling
            listeners[event] = [e for e in lst if not e['once']]
            for entry in to_call:
                interp_inner._call_js(entry['fn'], emit_args, this_val)
            return py_to_js(True)

        def remove_all_listeners(this_val, args, interp_inner):
            listeners = _get_listeners(this_val)
            if args:
                event = interp_inner._to_str(args[0])
                listeners.pop(event, None)
            else:
                listeners.clear()
            return this_val

        def listener_count(this_val, args, interp_inner):
            if not args:
                return py_to_js(0)
            event = interp_inner._to_str(args[0])
            listeners = _get_listeners(this_val)
            return py_to_js(len(listeners.get(event, [])))

        def constructor(this_val, args, interp_inner):
            this_val.value[_LISTENERS_KEY] = {}
            this_val.value['on'] = interp_inner._make_intrinsic(on_fn, 'EventEmitter.on')
            this_val.value['addListener'] = interp_inner._make_intrinsic(on_fn, 'EventEmitter.addListener')
            this_val.value['off'] = interp_inner._make_intrinsic(off_fn, 'EventEmitter.off')
            this_val.value['removeListener'] = interp_inner._make_intrinsic(off_fn, 'EventEmitter.removeListener')
            this_val.value['once'] = interp_inner._make_intrinsic(once_fn, 'EventEmitter.once')
            this_val.value['emit'] = interp_inner._make_intrinsic(emit_fn, 'EventEmitter.emit')
            this_val.value['removeAllListeners'] = interp_inner._make_intrinsic(remove_all_listeners, 'EventEmitter.removeAllListeners')
            this_val.value['listenerCount'] = interp_inner._make_intrinsic(listener_count, 'EventEmitter.listenerCount')
            return this_val

        ctx.add_constructor('EventEmitter', constructor)
