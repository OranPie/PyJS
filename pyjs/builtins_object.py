"""Auto-extracted builtin registrations for PyJS."""
from __future__ import annotations

import base64 as _b64
import json
import math
import os
import random
import re
import struct
import time
import urllib.parse as _urlparse
from datetime import datetime, timezone

from .core import JSTypeError, py_to_js, js_to_py
from .exceptions import _JSReturn, _JSError
from .builtins_core import _parseInt, _parseFloat
from .trace import get_logger
from .values import (
    JsValue, JsProxy, UNDEFINED, JS_NULL, JS_TRUE, JS_FALSE,
    SYMBOL_ITERATOR, SYMBOL_TO_PRIMITIVE, SYMBOL_HAS_INSTANCE,
    SYMBOL_TO_STRING_TAG, SYMBOL_ASYNC_ITERATOR, SYMBOL_SPECIES,
    SYMBOL_MATCH, SYMBOL_REPLACE, SYMBOL_SPLIT, SYMBOL_SEARCH,
    SYMBOL_IS_CONCAT_SPREADABLE,
    _symbol_id_counter, _symbol_registry,
    _js_regex_to_python,
)

_log = get_logger("scope")


def register_object_builtins(interp, g, intr):
    """Register object builtins into environment g."""
    # -- Object statics --
    obj_ctor = JsValue("intrinsic", {})
    def _obj_ctor_fn(this_val, args, interp):
        if args and args[0].type not in ('null', 'undefined'):
            return args[0]
        return JsValue('object', {})
    obj_ctor.value['fn'] = _obj_ctor_fn
    obj_ctor.value['name'] = 'Object'

    def _public_keys(d, obj=None):
        keys = [k for k in d.keys() if not k.startswith('__') and not (k.startswith('@@') and k.endswith('@@'))]
        if obj is not None:
            keys = [k for k in keys if interp._is_enumerable(obj, k)]
        return keys

    def _obj_keys_impl(_, args, interp):
        if not args: return py_to_js([])
        obj = args[0]
        if obj.type == 'proxy':
            proxy = obj.value
            trap = interp._get_trap(proxy.handler, 'ownKeys')
            if trap:
                result = interp._call_js(trap, [proxy.target], UNDEFINED)
                keys = list(result.value) if result.type == 'array' else []
                return py_to_js([k.value for k in keys if isinstance(k, JsValue) and k.type == 'string' and interp._is_enumerable(proxy.target, k.value)])
            obj = proxy.target
        if obj.type != 'object' or not isinstance(obj.value, dict):
            return py_to_js([])
        return py_to_js(_public_keys(obj.value, obj))

    def _obj_values_impl(_, args, interp):
        if not args: return py_to_js([])
        obj = args[0]
        if obj.type == 'proxy':
            proxy = obj.value
            trap = interp._get_trap(proxy.handler, 'ownKeys')
            if trap:
                result = interp._call_js(trap, [proxy.target], UNDEFINED)
                keys = list(result.value) if result.type == 'array' else []
                return py_to_js([interp._get_prop(proxy.target, k.value) for k in keys if isinstance(k, JsValue) and k.type == 'string' and interp._is_enumerable(proxy.target, k.value)])
            obj = proxy.target
        if obj.type != 'object' or not isinstance(obj.value, dict):
            return py_to_js([])
        return py_to_js([obj.value[k] for k in _public_keys(obj.value, obj)])

    def _obj_entries_impl(_, args, interp):
        if not args: return py_to_js([])
        obj = args[0]
        if obj.type == 'proxy':
            proxy = obj.value
            trap = interp._get_trap(proxy.handler, 'ownKeys')
            if trap:
                result = interp._call_js(trap, [proxy.target], UNDEFINED)
                keys = list(result.value) if result.type == 'array' else []
                return py_to_js([[k.value, interp._get_prop(proxy.target, k.value)] for k in keys if isinstance(k, JsValue) and k.type == 'string' and interp._is_enumerable(proxy.target, k.value)])
            obj = proxy.target
        if obj.type != 'object' or not isinstance(obj.value, dict):
            return py_to_js([])
        return py_to_js([[k, obj.value[k]] for k in _public_keys(obj.value, obj)])

    obj_ctor.value['keys']   = interp._make_intrinsic(_obj_keys_impl, 'Object.keys')
    obj_ctor.value['values'] = interp._make_intrinsic(_obj_values_impl, 'Object.values')
    obj_ctor.value['entries']= interp._make_intrinsic(_obj_entries_impl, 'Object.entries')
    obj_ctor.value['assign'] = intr(lambda a,i: _obj_assign(a,i), 'assign')
    def _obj_assign(args, interp):
        target = args[0] if args else py_to_js({})
        for src in args[1:]:
            if src.type not in ('object', 'array') or not isinstance(src.value, dict):
                continue
            # Collect own keys: regular keys + getter-only keys from __get__xxx
            all_keys = set()
            for key in src.value.keys():
                if key.startswith('__') and key.endswith('__'):
                    continue
                if key.startswith('@@') and key.endswith('@@'):
                    continue
                if key.startswith('__get__'):
                    all_keys.add(key[len('__get__'):])
                    continue
                if key.startswith('__set__'):
                    continue
                all_keys.add(key)
            for key in all_keys:
                if not interp._is_enumerable(src, key):
                    continue
                val = interp._get_prop(src, key)
                interp._set_prop(target, key, val)
        return target
    obj_ctor.value['hasOwn'] = intr(
        lambda a, i: JS_TRUE if len(a) > 1 and a[0].type == 'object' and i._to_key(a[1]) in a[0].value else JS_FALSE,
        'hasOwn',
    )
    def _obj_from_entries(args, interp):
        if not args:
            return py_to_js({})
        src = args[0]
        out = {}
        for entry in interp._array_like_items(src):
            if isinstance(entry, JsValue) and entry.type == 'array' and len(entry.value) >= 2:
                out[interp._to_key(entry.value[0])] = entry.value[1]
        return JsValue('object', out)
    obj_ctor.value['fromEntries'] = intr(_obj_from_entries, 'fromEntries')
    def _obj_group_by(args, interp):
        iterable = args[0] if args else UNDEFINED
        callback = args[1] if len(args) > 1 else UNDEFINED
        if not interp._is_callable(callback):
            raise _JSError(py_to_js('TypeError: callback is not a function'))
        items = interp._array_like_items(iterable)
        result = JsValue('object', {})
        for idx, item in enumerate(items):
            key = interp._to_str(interp._call_js(callback, [item, JsValue('number', float(idx))], UNDEFINED))
            existing = result.value.get(key)
            if existing is None:
                result.value[key] = JsValue('array', [item])
            else:
                existing.value.append(item)
        return result
    obj_ctor.value['groupBy'] = intr(_obj_group_by, 'Object.groupBy')
    def _obj_create(args, interp):
        proto = args[0] if args else UNDEFINED
        new_obj = JsValue('object', {})
        if proto.type not in ('undefined', 'null'):
            new_obj.value['__proto__'] = proto
        # Handle second argument: property descriptors object
        if len(args) > 1 and args[1].type == 'object' and isinstance(args[1].value, dict):
            for prop_key, prop_desc in args[1].value.items():
                if prop_key.startswith('__'): continue
                if isinstance(prop_desc, JsValue) and prop_desc.type == 'object':
                    _obj_define_property([new_obj, JsValue('string', prop_key), prop_desc], interp)
        return new_obj
    obj_ctor.value['create'] = intr(_obj_create, 'Object.create')
    def _obj_define_property(args, interp):
        if len(args) < 3: return args[0] if args else UNDEFINED
        obj, key, desc = args[0], interp._to_key(args[1]), args[2]
        if obj.type not in ('object', 'function', 'intrinsic', 'class'): return obj
        if desc.type == 'object':
            getter = desc.value.get('get')
            setter = desc.value.get('set')
            if getter and interp._is_callable(getter):
                obj.value[f"__get__{key}"] = getter
            if setter and interp._is_callable(setter):
                obj.value[f"__set__{key}"] = setter
            value = desc.value.get('value')
            if value is not None:
                obj.value[key] = value
            # Build and store descriptor dict
            existing = interp._get_desc(obj, key) or {}
            w_raw = desc.value.get('writable')
            e_raw = desc.value.get('enumerable')
            c_raw = desc.value.get('configurable')
            if w_raw is not None:
                existing['writable'] = interp._truthy(w_raw)
            if e_raw is not None:
                existing['enumerable'] = interp._truthy(e_raw)
            if c_raw is not None:
                existing['configurable'] = interp._truthy(c_raw)
            if getter:
                existing['get'] = getter
            if setter:
                existing['set'] = setter
            # Only store descriptor if any flag was explicitly set
            if existing:
                interp._set_desc(obj, key, existing)
        return obj
    obj_ctor.value['defineProperty'] = intr(_obj_define_property, 'defineProperty')
    def _obj_define_properties(args, interp):
        obj = args[0] if args else UNDEFINED
        props = args[1] if len(args) > 1 else UNDEFINED
        if obj.type not in ('object', 'function', 'intrinsic', 'class'): return obj
        if props.type == 'object' and isinstance(props.value, dict):
            for prop_key, prop_desc in props.value.items():
                if prop_key.startswith('__'): continue
                if isinstance(prop_desc, JsValue) and prop_desc.type == 'object':
                    _obj_define_property([obj, JsValue('string', prop_key), prop_desc], interp)
        return obj
    obj_ctor.value['defineProperties'] = intr(_obj_define_properties, 'defineProperties')
    obj_ctor.value['getOwnPropertyNames'] = intr(
        lambda a, i: py_to_js([k for k in (a[0].value.keys() if a and a[0].type == 'object' else []) if not k.startswith('__')]),
        'getOwnPropertyNames',
    )

    def _obj_is(args, interp):
        a = args[0] if args else UNDEFINED
        b = args[1] if len(args) > 1 else UNDEFINED
        if a.type != b.type:
            return JS_FALSE
        if a.type == 'number':
            av, bv = a.value, b.value
            if math.isnan(av) and math.isnan(bv):
                return JS_TRUE
            if av == 0.0 and bv == 0.0:
                return JS_TRUE if math.copysign(1, av) == math.copysign(1, bv) else JS_FALSE
            return JS_TRUE if av == bv else JS_FALSE
        return JS_TRUE if interp._strict_eq(a, b) else JS_FALSE

    obj_ctor.value['is'] = intr(_obj_is, 'Object.is')

    def _obj_get_prototype_of(args, interp):
        if not args: return UNDEFINED
        proto = interp._get_proto(args[0])
        return proto if proto is not None else JS_NULL

    obj_ctor.value['getPrototypeOf'] = intr(_obj_get_prototype_of, 'Object.getPrototypeOf')

    def _obj_set_prototype_of(args, interp):
        obj = args[0] if args else UNDEFINED
        proto = args[1] if len(args) > 1 else UNDEFINED
        if isinstance(obj.value, dict):
            if proto.type == 'null':
                obj.value['__proto__'] = JS_NULL
            elif proto.type in ('object', 'function', 'class', 'intrinsic'):
                obj.value['__proto__'] = proto
        return obj

    obj_ctor.value['setPrototypeOf'] = intr(_obj_set_prototype_of, 'Object.setPrototypeOf')

    def _obj_proto_to_string(this_val, args, interp):
        target = this_val
        if target.type == 'null': return py_to_js('[object Null]')
        if target.type == 'undefined': return py_to_js('[object Undefined]')
        if target.type == 'number': return py_to_js('[object Number]')
        if target.type == 'string': return py_to_js('[object String]')
        if target.type == 'boolean': return py_to_js('[object Boolean]')
        if target.type == 'symbol': return py_to_js('[object Symbol]')
        if target.type == 'bigint': return py_to_js('[object BigInt]')
        if target.type == 'array': return py_to_js('[object Array]')
        if target.type == 'function': return py_to_js('[object Function]')
        if target.type in ('object', 'intrinsic', 'class'):
            # Check Symbol.toStringTag via full property access (walks proto chain + getters)
            tag_key = f"@@{SYMBOL_TO_STRING_TAG}@@"
            tag = interp._get_prop(target, tag_key)
            if tag and isinstance(tag, JsValue) and tag.type == 'string':
                return py_to_js(f'[object {tag.value}]')
            if isinstance(target.value, dict):
                kind = target.value.get('__kind__')
                if isinstance(kind, JsValue) and kind.type == 'string':
                    return py_to_js(f'[object {kind.value}]')
            return py_to_js('[object Object]')
        return py_to_js('[object Object]')

    proto_obj = JsValue('object', {})
    proto_obj.value['toString'] = interp._make_intrinsic(_obj_proto_to_string, 'Object.prototype.toString')
    obj_ctor.value['prototype'] = proto_obj

    def _obj_get_own_prop_desc(args, interp):
        if len(args) < 2:
            return UNDEFINED
        obj, key = args[0], interp._to_key(args[1])
        if obj.type not in ('object', 'function', 'intrinsic', 'class'):
            return UNDEFINED
        getter_key = f"__get__{key}"
        setter_key = f"__set__{key}"
        stored = interp._get_desc(obj, key) or {}
        if getter_key in obj.value or setter_key in obj.value:
            desc = JsValue('object', {})
            if getter_key in obj.value:
                desc.value['get'] = obj.value[getter_key]
            if setter_key in obj.value:
                desc.value['set'] = obj.value[setter_key]
            desc.value['enumerable'] = JS_TRUE if stored.get('enumerable', True) else JS_FALSE
            desc.value['configurable'] = JS_TRUE if stored.get('configurable', True) else JS_FALSE
            return desc
        if key in obj.value:
            desc = JsValue('object', {})
            desc.value['value'] = obj.value[key]
            desc.value['writable'] = JS_TRUE if stored.get('writable', True) else JS_FALSE
            desc.value['enumerable'] = JS_TRUE if stored.get('enumerable', True) else JS_FALSE
            desc.value['configurable'] = JS_TRUE if stored.get('configurable', True) else JS_FALSE
            return desc
        return UNDEFINED

    obj_ctor.value['getOwnPropertyDescriptor'] = intr(_obj_get_own_prop_desc, 'Object.getOwnPropertyDescriptor')

    def _obj_get_own_prop_descs(args, interp):
        if not args or args[0].type not in ('object', 'function', 'intrinsic', 'class'):
            return py_to_js({})
        obj = args[0]
        result = JsValue('object', {})
        for k in list(obj.value.keys()):
            desc = _obj_get_own_prop_desc([obj, py_to_js(k)], interp)
            if desc.type != 'undefined':
                result.value[k] = desc
        return result

    obj_ctor.value['getOwnPropertyDescriptors'] = intr(_obj_get_own_prop_descs, 'Object.getOwnPropertyDescriptors')

    def _obj_get_own_prop_symbols(args, interp):
        if not args or args[0].type not in ('object', 'function', 'intrinsic', 'class'):
            return py_to_js([])
        obj = args[0]
        syms = [JsValue('string', k) for k in obj.value.keys()
                if k.startswith('@@') and k.endswith('@@') and len(k) > 4]
        return JsValue('array', syms)

    obj_ctor.value['getOwnPropertySymbols'] = intr(_obj_get_own_prop_symbols, 'Object.getOwnPropertySymbols')
    def _obj_freeze(args, interp):
        if not args: return UNDEFINED
        obj = args[0]
        if isinstance(obj.value, dict):
            obj.value['__extensible__'] = False
            for k in list(obj.value.keys()):
                if k.startswith('__') and k.endswith('__'):
                    continue
                existing = interp._get_desc(obj, k) or {}
                existing['writable'] = False
                existing['configurable'] = False
                if 'enumerable' not in existing:
                    existing['enumerable'] = True
                interp._set_desc(obj, k, existing)
        return obj

    def _obj_seal(args, interp):
        if not args: return UNDEFINED
        obj = args[0]
        if isinstance(obj.value, dict):
            obj.value['__extensible__'] = False
            for k in list(obj.value.keys()):
                if k.startswith('__') and k.endswith('__'):
                    continue
                existing = interp._get_desc(obj, k) or {}
                existing['configurable'] = False
                if 'enumerable' not in existing:
                    existing['enumerable'] = True
                interp._set_desc(obj, k, existing)
        return obj

    def _obj_prevent_extensions(args, interp):
        if not args: return UNDEFINED
        obj = args[0]
        if isinstance(obj.value, dict):
            obj.value['__extensible__'] = False
        return obj

    def _obj_is_extensible(args, interp):
        if not args: return JS_TRUE
        obj = args[0]
        if not isinstance(getattr(obj, 'value', None), dict):
            return JS_TRUE
        return JS_FALSE if obj.value.get('__extensible__') is False else JS_TRUE

    def _obj_is_frozen(args, interp):
        if not args: return JS_FALSE
        obj = args[0]
        if not isinstance(getattr(obj, 'value', None), dict):
            return JS_FALSE
        if obj.value.get('__extensible__', True) is not False:
            return JS_FALSE
        for k in obj.value:
            if k.startswith('__') and k.endswith('__'):
                continue
            desc = interp._get_desc(obj, k) or {}
            if desc.get('writable', True) or desc.get('configurable', True):
                return JS_FALSE
        return JS_TRUE

    def _obj_is_sealed(args, interp):
        if not args: return JS_FALSE
        obj = args[0]
        if not isinstance(getattr(obj, 'value', None), dict):
            return JS_FALSE
        if obj.value.get('__extensible__', True) is not False:
            return JS_FALSE
        for k in obj.value:
            if k.startswith('__') and k.endswith('__'):
                continue
            desc = interp._get_desc(obj, k) or {}
            if desc.get('configurable', True):
                return JS_FALSE
        return JS_TRUE

    obj_ctor.value['freeze'] = intr(_obj_freeze, 'Object.freeze')
    obj_ctor.value['seal'] = intr(_obj_seal, 'Object.seal')
    obj_ctor.value['preventExtensions'] = intr(_obj_prevent_extensions, 'Object.preventExtensions')
    obj_ctor.value['isExtensible'] = intr(_obj_is_extensible, 'Object.isExtensible')
    obj_ctor.value['isFrozen'] = intr(_obj_is_frozen, 'Object.isFrozen')
    obj_ctor.value['isSealed'] = intr(_obj_is_sealed, 'Object.isSealed')
    g.declare('Object', obj_ctor, 'var')

    # -- Array constructor (used for Array.isArray etc.) --
    arr_ctor = JsValue("intrinsic", {})
    def _array_ctor_fn(this_val, args, interp):
        if len(args) == 1 and args[0].type == 'number':
            n = args[0].value
            ni = int(n)
            if n != ni or ni < 0:
                raise ValueError('Invalid array length')
            return JsValue('array', [UNDEFINED] * ni)
        return JsValue('array', list(args))
    arr_ctor.value['fn'] = _array_ctor_fn
    arr_ctor.value['name'] = 'Array'
    arr_ctor.value['isArray'] = intr(lambda a,i: JS_TRUE if a and a[0].type=='array' else JS_FALSE, 'isArray')
    def _array_from(args, interp):
        if not args:
            return py_to_js([])
        src = args[0]
        map_fn = args[1] if len(args) > 1 and interp._is_callable(args[1]) else None
        it = interp._get_js_iterator(src)
        if it is not None:
            items = []
            seen = 0
            while seen < 100000:
                r = it()
                seen += 1
                done = interp._get_prop(r, 'done')
                if interp._truthy(done):
                    break
                items.append(interp._get_prop(r, 'value'))
        elif src.type == 'array':
            items = list(src.value)
        elif src.type == 'string':
            items = [JsValue('string', ch) for ch in src.value]
        elif src.type == 'object':
            raw_length = src.value.get('length', UNDEFINED)
            if isinstance(raw_length, JsValue) and raw_length.type != 'undefined':
                length = max(0, int(interp._to_num(raw_length)))
                items = [src.value.get(str(index), UNDEFINED) for index in range(length)]
            else:
                items = []
        else:
            items = []
        if map_fn:
            items = [interp._call_js(map_fn, [item, JsValue('number', i)], UNDEFINED) for i, item in enumerate(items)]
        return JsValue('array', items)
    arr_ctor.value['from']    = intr(_array_from, 'from')
    arr_ctor.value['of']      = intr(lambda a, i: JsValue('array', list(a)), 'of')

    def _array_from_async(args, interp):
        if not args:
            return interp._resolved_promise(JsValue('array', []))
        src = args[0]
        map_fn = args[1] if len(args) > 1 and interp._is_callable(args[1]) else None

        def _apply_map_to_list(items):
            if map_fn:
                return [interp._call_js(map_fn, [item, JsValue('number', float(i))], UNDEFINED)
                        for i, item in enumerate(items)]
            return items

        # Handle async iterables (Symbol.asyncIterator)
        if src.type in ('object', 'function') and isinstance(src.value, dict):
            sym_async_key = f"@@{SYMBOL_ASYNC_ITERATOR}@@"
            async_iter_fn = src.value.get(sym_async_key)
            if async_iter_fn and interp._is_callable(async_iter_fn):
                iterator = interp._call_js(async_iter_fn, [], src)
                items = interp._drain_async_iter(iterator)
                return interp._resolved_promise(JsValue('array', _apply_map_to_list(items)))

        # Handle sync iterables (Symbol.iterator)
        sync_it = interp._get_js_iterator(src)
        if sync_it is not None:
            items = []
            while True:
                result = sync_it()
                if result is None:
                    break
                if result.type == 'object' and result.value.get('done', JS_FALSE).value is True:
                    break
                val = result.value.get('value', UNDEFINED) if result.type == 'object' else result
                items.append(val)
            # Resolve any promise elements via Promise.all
            promises = [interp._to_promise(item) for item in items]
            all_promise = interp._promise_all(promises)
            if map_fn:
                def _apply_map(this_val, map_args, interp_inner):
                    arr = map_args[0] if map_args else JsValue('array', [])
                    mapped = [interp_inner._call_js(map_fn, [item, JsValue('number', float(i))], UNDEFINED)
                              for i, item in enumerate(arr.value)]
                    return JsValue('array', mapped)
                return interp._promise_then(
                    all_promise,
                    interp._make_intrinsic(_apply_map, 'Array.fromAsync.map'),
                    None,
                )
            return all_promise

        # Array-like (has length property)
        if src.type == 'array':
            items = list(src.value)
        elif src.type == 'object':
            raw_length = src.value.get('length', UNDEFINED)
            if isinstance(raw_length, JsValue) and raw_length.type != 'undefined':
                length = max(0, int(interp._to_num(raw_length)))
                items = [src.value.get(str(index), UNDEFINED) for index in range(length)]
            else:
                items = []
        else:
            items = []

        promises = [interp._to_promise(item) for item in items]
        all_promise = interp._promise_all(promises)
        if map_fn:
            def _apply_map(this_val, map_args, interp_inner):
                arr = map_args[0] if map_args else JsValue('array', [])
                mapped = [interp_inner._call_js(map_fn, [item, JsValue('number', float(i))], UNDEFINED)
                          for i, item in enumerate(arr.value)]
                return JsValue('array', mapped)
            return interp._promise_then(
                all_promise,
                interp._make_intrinsic(_apply_map, 'Array.fromAsync.map'),
                None,
            )
        return all_promise
    arr_ctor.value['fromAsync'] = intr(_array_from_async, 'fromAsync')

    g.declare('Array', arr_ctor, 'var')

    number_ctor = intr(lambda a,i: py_to_js(i._to_num(a[0]) if a else 0), 'Number')
    number_ctor.value['isNaN'] = interp._make_intrinsic(
        lambda this_val, args, interp: JS_TRUE if args and args[0].type == 'number' and math.isnan(args[0].value) else JS_FALSE,
        'Number.isNaN',
    )
    number_ctor.value['isFinite'] = interp._make_intrinsic(
        lambda this_val, args, interp: JS_TRUE if args and args[0].type == 'number' and math.isfinite(args[0].value) else JS_FALSE,
        'Number.isFinite',
    )
    number_ctor.value['isInteger'] = interp._make_intrinsic(
        lambda this_val, args, interp: JS_TRUE if args and args[0].type == 'number' and math.isfinite(args[0].value) and args[0].value == int(args[0].value) else JS_FALSE,
        'Number.isInteger',
    )
    number_ctor.value['EPSILON'] = JsValue('number', 2.220446049250313e-16)
    number_ctor.value['MAX_SAFE_INTEGER'] = JsValue('number', float(2**53 - 1))
    number_ctor.value['MIN_SAFE_INTEGER'] = JsValue('number', float(-(2**53 - 1)))
    number_ctor.value['MAX_VALUE'] = JsValue('number', 1.7976931348623157e+308)
    number_ctor.value['MIN_VALUE'] = JsValue('number', 5e-324)
    number_ctor.value['POSITIVE_INFINITY'] = JsValue('number', float('inf'))
    number_ctor.value['NEGATIVE_INFINITY'] = JsValue('number', float('-inf'))
    number_ctor.value['NaN'] = JsValue('number', float('nan'))
    number_ctor.value['isSafeInteger'] = interp._make_intrinsic(
        lambda this_val, args, interp: (
            JS_TRUE if args and math.isfinite(interp._to_num(args[0]))
            and interp._to_num(args[0]) == int(interp._to_num(args[0]))
            and abs(interp._to_num(args[0])) <= 2**53 - 1
            else JS_FALSE
        ),
        'Number.isSafeInteger',
    )
    number_ctor.value['parseFloat'] = intr(_parseFloat, 'Number.parseFloat')
    number_ctor.value['parseInt'] = intr(_parseInt, 'Number.parseInt')

    string_ctor = intr(lambda a,i: py_to_js(i._to_str(a[0]) if a else ''), 'String')
    def _string_raw(args, interp):
        if not args:
            return JsValue('string', '')
        template = args[0]
        raw = interp._get_prop(template, 'raw')
        parts = interp._array_like_items(raw)
        out = []
        for index, part in enumerate(parts):
            out.append(interp._to_str(part))
            if index + 1 < len(args):
                out.append(interp._to_str(args[index + 1]))
        return JsValue('string', ''.join(out))
    string_ctor.value['raw'] = interp._make_intrinsic(lambda this_val, args, interp: _string_raw(args, interp), 'String.raw')
    string_ctor.value['fromCodePoint'] = interp._make_intrinsic(
        lambda this_val, args, interp: JsValue('string', ''.join(chr(int(interp._to_num(a))) for a in args)),
        'String.fromCodePoint',
    )

    def _make_regexp(source, flags=''):
        flag_text = ''.join(sorted(set(flags)))
        py_flags = 0
        if 'i' in flag_text:
            py_flags |= re.IGNORECASE
        if 'm' in flag_text:
            py_flags |= re.MULTILINE
        if 's' in flag_text:
            py_flags |= re.DOTALL
        if 'u' in flag_text or 'v' in flag_text:
            py_flags |= re.UNICODE
        py_source = _js_regex_to_python(source)

        def _regexp_test(args, interp):
            text = interp._to_str(args[0]) if args else ''
            return JS_TRUE if re.search(py_source, text, py_flags) else JS_FALSE

        def _regexp_exec(args, interp):
            text = interp._to_str(args[0]) if args else ''
            match = re.search(py_source, text, py_flags)
            if not match:
                return JS_NULL
            values = [JsValue('string', match.group(0))]
            values.extend(JsValue('string', group) if group is not None else UNDEFINED for group in match.groups())
            result = JsValue('array', values)
            groups_dict = match.groupdict()
            if groups_dict:
                groups_obj = JsValue('object', {
                    k: JsValue('string', v) if v is not None else UNDEFINED
                    for k, v in groups_dict.items()
                })
            else:
                groups_obj = UNDEFINED
            if result.extras is None:
                result.extras = {}
            result.extras['groups'] = groups_obj
            if 'd' in flag_text:
                indices_arr = []
                for i in range(len(match.regs)):
                    start, end = match.regs[i]
                    if start == -1:
                        indices_arr.append(UNDEFINED)
                    else:
                        indices_arr.append(JsValue('array', [py_to_js(float(start)), py_to_js(float(end))]))
                result.extras['indices'] = JsValue('array', indices_arr)
            return result

        regexp = JsValue('object', {})
        regexp.value['__kind__'] = JsValue('string', 'RegExp')
        regexp.value['source'] = JsValue('string', source)
        regexp.value['flags'] = JsValue('string', flag_text)
        regexp.value['global'] = JS_TRUE if 'g' in flag_text else JS_FALSE
        regexp.value['ignoreCase'] = JS_TRUE if 'i' in flag_text else JS_FALSE
        regexp.value['multiline'] = JS_TRUE if 'm' in flag_text else JS_FALSE
        regexp.value['test'] = intr(_regexp_test, 'RegExp.test')
        regexp.value['exec'] = intr(_regexp_exec, 'RegExp.exec')
        return regexp

    def _regexp_ctor(args, interp):
        source = interp._to_str(args[0]) if args else ''
        flags = interp._to_str(args[1]) if len(args) > 1 else ''
        return _make_regexp(source, flags)

    _regexp_ctor_fn = interp._make_intrinsic(lambda this_val, args, interp: _regexp_ctor(args, interp), 'RegExp')
    # RegExp.escape (ES2025 Stage 4)
    def _regexp_escape(args, interp):
        if not args or args[0].type != 'string':
            raise _JSError(interp._make_js_error('TypeError', 'RegExp.escape: argument must be a string'))
        s = args[0].value
        # Escape all regex metacharacters per the TC39 spec
        escaped = re.sub(r'([\\^$.*+?()[\]{}|/\-])', r'\\\1', s)
        return JsValue('string', escaped)
    _regexp_ctor_fn.value['escape'] = intr(_regexp_escape, 'RegExp.escape')
    g.declare('RegExp', _regexp_ctor_fn, 'var')

    def _make_date(value_ms=None):
        _ms = [float(value_ms if value_ms is not None else time.time() * 1000.0)]

        def _get_dt():
            return datetime.fromtimestamp(_ms[0] / 1000.0, tz=timezone.utc)

        def _date_get_time(args, interp):
            return JsValue('number', _ms[0])

        def _date_to_iso(args, interp):
            text = _get_dt().isoformat().replace('+00:00', 'Z')
            return JsValue('string', text)

        def _date_to_string(args, interp):
            return JsValue('string', _get_dt().ctime())

        def _date_get_full_year(args, interp):
            return JsValue('number', float(_get_dt().year))

        def _date_get_month(args, interp):
            return JsValue('number', float(_get_dt().month - 1))

        def _date_get_date(args, interp):
            return JsValue('number', float(_get_dt().day))

        def _date_get_day(args, interp):
            return JsValue('number', float((_get_dt().weekday() + 1) % 7))

        def _date_get_hours(args, interp):
            return JsValue('number', float(_get_dt().hour))

        def _date_get_minutes(args, interp):
            return JsValue('number', float(_get_dt().minute))

        def _date_get_seconds(args, interp):
            return JsValue('number', float(_get_dt().second))

        def _date_get_milliseconds(args, interp):
            return JsValue('number', float(_ms[0] % 1000))

        def _date_get_timezone_offset(args, interp):
            return JsValue('number', 0.0)

        def _date_set_full_year(args, interp):
            if not args: return JsValue('number', _ms[0])
            new_year = int(interp._to_num(args[0]))
            _ms[0] = _get_dt().replace(year=new_year).timestamp() * 1000
            return JsValue('number', _ms[0])

        def _date_set_month(args, interp):
            if not args: return JsValue('number', _ms[0])
            new_month = int(interp._to_num(args[0])) + 1
            _ms[0] = _get_dt().replace(month=new_month).timestamp() * 1000
            return JsValue('number', _ms[0])

        def _date_set_date(args, interp):
            if not args: return JsValue('number', _ms[0])
            _ms[0] = _get_dt().replace(day=int(interp._to_num(args[0]))).timestamp() * 1000
            return JsValue('number', _ms[0])

        def _date_set_hours(args, interp):
            if not args: return JsValue('number', _ms[0])
            _ms[0] = _get_dt().replace(hour=int(interp._to_num(args[0]))).timestamp() * 1000
            return JsValue('number', _ms[0])

        def _date_set_minutes(args, interp):
            if not args: return JsValue('number', _ms[0])
            _ms[0] = _get_dt().replace(minute=int(interp._to_num(args[0]))).timestamp() * 1000
            return JsValue('number', _ms[0])

        def _date_set_seconds(args, interp):
            if not args: return JsValue('number', _ms[0])
            _ms[0] = _get_dt().replace(second=int(interp._to_num(args[0]))).timestamp() * 1000
            return JsValue('number', _ms[0])

        def _date_set_milliseconds(args, interp):
            if not args: return JsValue('number', _ms[0])
            new_ms_part = int(interp._to_num(args[0]))
            _ms[0] = math.floor(_ms[0] / 1000) * 1000 + new_ms_part
            return JsValue('number', _ms[0])

        def _date_to_locale_date_string(args, interp):
            dt = _get_dt()
            return JsValue('string', f'{dt.month}/{dt.day}/{dt.year}')

        def _date_to_locale_time_string(args, interp):
            s = _get_dt().strftime('%I:%M:%S %p').lstrip('0')
            return JsValue('string', s)

        def _date_to_locale_string(args, interp):
            dt = _get_dt()
            date_part = f'{dt.month}/{dt.day}/{dt.year}'
            time_part = dt.strftime('%I:%M:%S %p').lstrip('0')
            return JsValue('string', f'{date_part}, {time_part}')

        obj = JsValue('object', {})
        obj.value['__kind__'] = JsValue('string', 'Date')
        obj.value['__date_ts__'] = _ms
        obj.value['getTime'] = intr(_date_get_time, 'Date.getTime')
        obj.value['toISOString'] = intr(_date_to_iso, 'Date.toISOString')
        obj.value['toJSON'] = intr(_date_to_iso, 'Date.toJSON')
        obj.value['toString'] = intr(_date_to_string, 'Date.toString')
        obj.value['valueOf'] = intr(_date_get_time, 'Date.valueOf')
        obj.value['getFullYear'] = intr(_date_get_full_year, 'Date.getFullYear')
        obj.value['getMonth'] = intr(_date_get_month, 'Date.getMonth')
        obj.value['getDate'] = intr(_date_get_date, 'Date.getDate')
        obj.value['getDay'] = intr(_date_get_day, 'Date.getDay')
        obj.value['getHours'] = intr(_date_get_hours, 'Date.getHours')
        obj.value['getMinutes'] = intr(_date_get_minutes, 'Date.getMinutes')
        obj.value['getSeconds'] = intr(_date_get_seconds, 'Date.getSeconds')
        obj.value['getMilliseconds'] = intr(_date_get_milliseconds, 'Date.getMilliseconds')
        obj.value['getTimezoneOffset'] = intr(_date_get_timezone_offset, 'Date.getTimezoneOffset')
        obj.value['setFullYear'] = intr(_date_set_full_year, 'Date.setFullYear')
        obj.value['setMonth'] = intr(_date_set_month, 'Date.setMonth')
        obj.value['setDate'] = intr(_date_set_date, 'Date.setDate')
        obj.value['setHours'] = intr(_date_set_hours, 'Date.setHours')
        obj.value['setMinutes'] = intr(_date_set_minutes, 'Date.setMinutes')
        obj.value['setSeconds'] = intr(_date_set_seconds, 'Date.setSeconds')
        obj.value['setMilliseconds'] = intr(_date_set_milliseconds, 'Date.setMilliseconds')
        obj.value['toLocaleDateString'] = intr(_date_to_locale_date_string, 'Date.toLocaleDateString')
        obj.value['toLocaleTimeString'] = intr(_date_to_locale_time_string, 'Date.toLocaleTimeString')
        obj.value['toLocaleString'] = intr(_date_to_locale_string, 'Date.toLocaleString')
        return obj

    def _date_ctor_fn(this_val, args, interp):
        if len(args) >= 2:
            year = int(interp._to_num(args[0]))
            month = int(interp._to_num(args[1])) + 1
            day = int(interp._to_num(args[2])) if len(args) > 2 else 1
            hours = int(interp._to_num(args[3])) if len(args) > 3 else 0
            minutes = int(interp._to_num(args[4])) if len(args) > 4 else 0
            seconds = int(interp._to_num(args[5])) if len(args) > 5 else 0
            ms_part = int(interp._to_num(args[6])) if len(args) > 6 else 0
            from datetime import datetime as _dt2
            dt = _dt2(year, month, day, hours, minutes, seconds, ms_part * 1000, tzinfo=timezone.utc)
            return _make_date(dt.timestamp() * 1000)
        return _make_date(interp._to_num(args[0]) if args else None)

    date_ctor = interp._make_intrinsic(_date_ctor_fn, 'Date')
    date_ctor.value['now'] = interp._make_intrinsic(
        lambda this_val, args, interp: JsValue('number', time.time() * 1000.0),
        'Date.now',
    )
    def _date_parse(this_val, args, interp):
        s = interp._to_str(args[0]) if args else ''
        from datetime import datetime as _dt_parse
        for fmt in ('%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S.%f',
                    '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d', '%b %d, %Y', '%d %b %Y',
                    '%Y/%m/%d', '%m/%d/%Y'):
            try:
                dt = _dt_parse.strptime(s.strip(), fmt)
                return JsValue('number', dt.replace(tzinfo=timezone.utc).timestamp() * 1000.0)
            except ValueError:
                continue
        return JsValue('number', float('nan'))
    date_ctor.value['parse'] = interp._make_intrinsic(_date_parse, 'Date.parse')
    def _date_utc(this_val, args, interp):
        nums = [int(interp._to_num(a)) for a in args]
        year = nums[0] if len(nums) > 0 else 1970
        if 0 <= year <= 99:
            year += 1900
        month = (nums[1] if len(nums) > 1 else 0) + 1
        day = nums[2] if len(nums) > 2 else 1
        hours = nums[3] if len(nums) > 3 else 0
        minutes = nums[4] if len(nums) > 4 else 0
        seconds = nums[5] if len(nums) > 5 else 0
        ms = nums[6] if len(nums) > 6 else 0
        from datetime import datetime as _dt_utc
        try:
            dt = _dt_utc(year, month, day, hours, minutes, seconds, ms * 1000, tzinfo=timezone.utc)
            return JsValue('number', dt.timestamp() * 1000.0)
        except (ValueError, OverflowError):
            return JsValue('number', float('nan'))
    date_ctor.value['UTC'] = interp._make_intrinsic(_date_utc, 'Date.UTC')
    # Symbol.hasInstance: instanceof Date checks __kind__ == 'Date'
    _date_hi_key = f"@@{SYMBOL_HAS_INSTANCE}@@"
    def _date_has_instance(this_val, args, interp):
        val = args[0] if args else UNDEFINED
        if val.type == 'object' and isinstance(val.value, dict):
            kind = val.value.get('__kind__')
            if isinstance(kind, JsValue) and kind.value == 'Date':
                return JS_TRUE
        return JS_FALSE
    date_ctor.value[_date_hi_key] = interp._make_intrinsic(_date_has_instance, 'Date[Symbol.hasInstance]')
    g.declare('Date', date_ctor, 'var')

    # Declare String / Number / Boolean constructors
    g.declare('String',  string_ctor, 'var')
    g.declare('Number',  number_ctor, 'var')
    g.declare('Boolean', intr(lambda a,i: JS_TRUE if a and i._truthy(a[0]) else JS_FALSE, 'Boolean'), 'var')
    _log.info("registered object builtins")


