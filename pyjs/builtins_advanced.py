"""Auto-extracted builtin registrations for PyJS."""
from __future__ import annotations

import base64 as _b64
import json
import math
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.parse as _urlparse
from datetime import datetime, timezone

from .core import JSTypeError, py_to_js, js_to_py
from .exceptions import _JSReturn, _JSError, _JSBreak, _JSContinue
from .trace import get_logger
from .values import (
    JsValue, JsProxy, UNDEFINED, JS_NULL, JS_TRUE, JS_FALSE,
    SYMBOL_ITERATOR, SYMBOL_TO_PRIMITIVE, SYMBOL_HAS_INSTANCE,
    SYMBOL_TO_STRING_TAG, SYMBOL_ASYNC_ITERATOR, SYMBOL_SPECIES,
    SYMBOL_MATCH, SYMBOL_REPLACE, SYMBOL_SPLIT, SYMBOL_SEARCH,
    SYMBOL_IS_CONCAT_SPREADABLE, SYMBOL_DISPOSE, SYMBOL_ASYNC_DISPOSE,
    _symbol_id_counter, _symbol_registry,
    _js_regex_to_python,
)

_log = get_logger("scope")


def register_advanced_builtins(interp, g, intr):
    """Register advanced builtins into environment g."""
    # -- task queue / timers --
    def _queue_microtask(args, interp):
        fn = args[0] if args else UNDEFINED
        if not interp._is_callable(fn):
            raise _JSError(py_to_js('queueMicrotask callback must be a function'))
        interp._enqueue_microtask(lambda fn=fn: interp._call_js(fn, [], UNDEFINED))
        return UNDEFINED

    def _set_timeout(args, interp):
        fn = args[0] if args else UNDEFINED
        if not interp._is_callable(fn):
            raise _JSError(py_to_js('setTimeout callback must be a function'))
        delay = interp._to_num(args[1]) if len(args) > 1 else 0
        return JsValue('number', interp._schedule_timer(fn, delay, False, args[2:]))

    def _set_interval(args, interp):
        fn = args[0] if args else UNDEFINED
        if not interp._is_callable(fn):
            raise _JSError(py_to_js('setInterval callback must be a function'))
        delay = interp._to_num(args[1]) if len(args) > 1 else 0
        return JsValue('number', interp._schedule_timer(fn, delay, True, args[2:]))

    def _clear_timer(args, interp):
        if args:
            interp._clear_timer(int(interp._to_num(args[0])))
        return UNDEFINED

    g.declare('queueMicrotask', intr(_queue_microtask, 'queueMicrotask'), 'var')
    g.declare('setTimeout', intr(_set_timeout, 'setTimeout'), 'var')
    g.declare('setInterval', intr(_set_interval, 'setInterval'), 'var')
    g.declare('clearTimeout', intr(_clear_timer, 'clearTimeout'), 'var')
    g.declare('clearInterval', intr(_clear_timer, 'clearInterval'), 'var')

    # -- performance --
    _perf_origin = time.perf_counter()
    perf_obj = JsValue('object', {})
    perf_obj.value['now'] = intr(lambda a, i: JsValue('number', (time.perf_counter() - _perf_origin) * 1000.0), 'performance.now')
    g.declare('performance', perf_obj, 'var')

    # -- Full Symbol implementation --
    def _make_symbol(desc=''):
        _symbol_id_counter[0] += 1
        return JsValue('symbol', {'id': _symbol_id_counter[0], 'desc': str(desc)})

    def _symbol_ctor(args, interp):
        desc = interp._to_str(args[0]) if args and args[0].type != 'undefined' else ''
        return _make_symbol(desc)

    sym_ctor = intr(_symbol_ctor, 'Symbol')
    sym_ctor.value['iterator']     = JsValue('symbol', {'id': SYMBOL_ITERATOR,       'desc': 'Symbol.iterator'})
    sym_ctor.value['toPrimitive']  = JsValue('symbol', {'id': SYMBOL_TO_PRIMITIVE,   'desc': 'Symbol.toPrimitive'})
    sym_ctor.value['hasInstance']  = JsValue('symbol', {'id': SYMBOL_HAS_INSTANCE,   'desc': 'Symbol.hasInstance'})
    sym_ctor.value['toStringTag']  = JsValue('symbol', {'id': SYMBOL_TO_STRING_TAG,  'desc': 'Symbol.toStringTag'})
    sym_ctor.value['asyncIterator']= JsValue('symbol', {'id': SYMBOL_ASYNC_ITERATOR, 'desc': 'Symbol.asyncIterator'})
    sym_ctor.value['species']      = JsValue('symbol', {'id': SYMBOL_SPECIES,        'desc': 'Symbol.species'})
    sym_ctor.value['match']        = JsValue('symbol', {'id': SYMBOL_MATCH,          'desc': 'Symbol.match'})
    sym_ctor.value['replace']      = JsValue('symbol', {'id': SYMBOL_REPLACE,        'desc': 'Symbol.replace'})
    sym_ctor.value['split']        = JsValue('symbol', {'id': SYMBOL_SPLIT,          'desc': 'Symbol.split'})
    sym_ctor.value['search']       = JsValue('symbol', {'id': SYMBOL_SEARCH,         'desc': 'Symbol.search'})
    sym_ctor.value['isConcatSpreadable'] = JsValue('symbol', {'id': SYMBOL_IS_CONCAT_SPREADABLE, 'desc': 'Symbol.isConcatSpreadable'})
    sym_ctor.value['dispose']      = JsValue('symbol', {'id': SYMBOL_DISPOSE,       'desc': 'Symbol.dispose'})
    sym_ctor.value['asyncDispose'] = JsValue('symbol', {'id': SYMBOL_ASYNC_DISPOSE, 'desc': 'Symbol.asyncDispose'})

    def _sym_for(args, interp):
        if not args:
            return _make_symbol('')
        key = interp._to_str(args[0])
        if key in _symbol_registry:
            return _symbol_registry[key]
        sym = _make_symbol(key)
        _symbol_registry[key] = sym
        return sym

    def _sym_key_for(args, interp):
        if not args or args[0].type != 'symbol':
            return UNDEFINED
        sym_id = args[0].value['id']
        for key, val in _symbol_registry.items():
            if val.value['id'] == sym_id:
                return JsValue('string', key)
        return UNDEFINED

    sym_ctor.value['for']    = intr(_sym_for, 'Symbol.for')
    sym_ctor.value['keyFor'] = intr(_sym_key_for, 'Symbol.keyFor')
    g.declare('Symbol', sym_ctor, 'var')

    # -- Proxy (real implementation) --
    g.declare('Proxy', interp._make_intrinsic(
        lambda this_val, args, interp: JsValue('proxy', JsProxy(
            args[0] if args else py_to_js({}),
            args[1] if len(args) > 1 else py_to_js({})
        )),
        'Proxy',
    ), 'var')

    # -- WeakMap / WeakSet --
    def _make_weakmap():
        store = {}  # {id(key_pyobj): (key_pyobj, value)}
        wm = JsValue('object', {})

        def _wm_set(args, interp):
            key = args[0] if args else UNDEFINED
            val = args[1] if len(args) > 1 else UNDEFINED
            if key.type not in ('object', 'function', 'intrinsic', 'array', 'proxy'):
                raise _JSError(py_to_js("Invalid value used as weak map key"))
            store[id(key)] = (key, val)
            return wm

        def _wm_get(args, interp):
            key = args[0] if args else UNDEFINED
            entry = store.get(id(key))
            return entry[1] if entry else UNDEFINED

        def _wm_has(args, interp):
            key = args[0] if args else UNDEFINED
            return JS_TRUE if id(key) in store else JS_FALSE

        def _wm_delete(args, interp):
            key = args[0] if args else UNDEFINED
            if id(key) in store:
                del store[id(key)]
                return JS_TRUE
            return JS_FALSE

        wm.value['set']    = intr(_wm_set,    'WeakMap.set')
        wm.value['get']    = intr(_wm_get,    'WeakMap.get')
        wm.value['has']    = intr(_wm_has,    'WeakMap.has')
        wm.value['delete'] = intr(_wm_delete, 'WeakMap.delete')
        return wm

    def _make_weakset():
        store = {}  # {id(key_pyobj): key_pyobj}
        ws = JsValue('object', {})

        def _ws_add(args, interp):
            key = args[0] if args else UNDEFINED
            if key.type not in ('object', 'function', 'intrinsic', 'array', 'proxy'):
                raise _JSError(py_to_js("Invalid value used in weak set"))
            store[id(key)] = key
            return ws

        def _ws_has(args, interp):
            key = args[0] if args else UNDEFINED
            return JS_TRUE if id(key) in store else JS_FALSE

        def _ws_delete(args, interp):
            key = args[0] if args else UNDEFINED
            if id(key) in store:
                del store[id(key)]
                return JS_TRUE
            return JS_FALSE

        ws.value['add']    = intr(_ws_add,    'WeakSet.add')
        ws.value['has']    = intr(_ws_has,    'WeakSet.has')
        ws.value['delete'] = intr(_ws_delete, 'WeakSet.delete')
        return ws

    g.declare('WeakMap', interp._make_intrinsic(lambda this_val, args, interp: _make_weakmap(), 'WeakMap'), 'var')
    g.declare('WeakSet', interp._make_intrinsic(lambda this_val, args, interp: _make_weakset(), 'WeakSet'), 'var')

    # -- WeakRef --
    def _make_weakref(args, interp):
        target = args[0] if args else UNDEFINED
        return JsValue('object', {'__type__': py_to_js('WeakRef'), '__target__': target})

    g.declare('WeakRef', intr(_make_weakref, 'WeakRef'), 'var')

    # -- FinalizationRegistry --
    def _make_finalization_registry(args, interp):
        callback = args[0] if args else UNDEFINED
        obj = JsValue('object', {
            '__type__': py_to_js('FinalizationRegistry'),
            '__cb__': callback,
            '__entries__': JsValue('array', []),
        })

        def _fr_register(rargs, rinterp):
            target = rargs[0] if rargs else UNDEFINED
            held = rargs[1] if len(rargs) > 1 else UNDEFINED
            token = rargs[2] if len(rargs) > 2 else UNDEFINED
            entry = JsValue('object', {'target': target, 'held': held, 'token': token})
            obj.value['__entries__'].value.append(entry)
            return UNDEFINED

        def _fr_unregister(rargs, rinterp):
            token = rargs[0] if rargs else UNDEFINED
            entries = obj.value['__entries__'].value
            obj.value['__entries__'].value = [
                e for e in entries
                if not (e.type == 'object' and e.value.get('token') is token)
            ]
            return UNDEFINED

        obj.value['register'] = intr(_fr_register, 'FinalizationRegistry.register')
        obj.value['unregister'] = intr(_fr_unregister, 'FinalizationRegistry.unregister')
        return obj

    g.declare('FinalizationRegistry', intr(_make_finalization_registry, 'FinalizationRegistry'), 'var')

    # -- Reflect --
    reflect_obj = JsValue('object', {})

    def _reflect_get(args, interp):
        if not args: return UNDEFINED
        target = args[0]
        prop = args[1] if len(args) > 1 else UNDEFINED
        return interp._get_prop(target, prop)

    def _reflect_set(args, interp):
        if len(args) < 3: return JS_FALSE
        interp._set_prop(args[0], args[1], args[2])
        return JS_TRUE

    def _reflect_has(args, interp):
        if len(args) < 2: return JS_FALSE
        target, key = args[0], interp._to_key(args[1])
        if target.type == 'proxy':
            proxy = target.value
            trap = interp._get_trap(proxy.handler, 'has')
            if trap:
                return interp._call_js(trap, [proxy.target, py_to_js(key)], UNDEFINED)
            return _reflect_has([proxy.target, args[1]], interp)
        if target.type in ('object', 'function', 'intrinsic', 'class'):
            return JS_TRUE if key in target.value else JS_FALSE
        if target.type == 'array':
            try: return JS_TRUE if 0 <= int(key) < len(target.value) else JS_FALSE
            except (ValueError, TypeError): return JS_FALSE
        return JS_FALSE

    def _reflect_delete(args, interp):
        if len(args) < 2: return JS_FALSE
        return JS_TRUE if interp._del_prop(args[0], args[1]) else JS_FALSE

    def _reflect_apply(args, interp):
        fn = args[0] if args else UNDEFINED
        this_arg = args[1] if len(args) > 1 else UNDEFINED
        fn_args = list(args[2].value) if len(args) > 2 and args[2].type == 'array' else []
        return interp._call_js(fn, fn_args, this_arg)

    def _reflect_construct(args, interp):
        fn = args[0] if args else UNDEFINED
        fn_args = list(args[1].value) if len(args) > 1 and args[1].type == 'array' else []
        new_obj = JsValue('object', {})
        proto = fn.value.get('prototype') if isinstance(fn.value, dict) else None
        if proto and proto.type == 'object':
            new_obj.value['__proto__'] = proto
        result = interp._call_js(fn, fn_args, new_obj, is_new_call=True)
        if isinstance(result, JsValue) and result.type in ('object', 'array', 'function', 'intrinsic', 'class', 'promise', 'proxy'):
            return result
        return new_obj

    def _reflect_own_keys(args, interp):
        if not args: return py_to_js([])
        target = args[0]
        if target.type in ('object', 'function', 'intrinsic', 'class'):
            return py_to_js([k for k in target.value.keys() if not k.startswith('__')])
        if target.type == 'array':
            return py_to_js([str(i) for i in range(len(target.value))])
        return py_to_js([])

    def _reflect_define_property(args, interp):
        if len(args) < 3: return JS_FALSE
        obj, key, desc = args[0], interp._to_key(args[1]), args[2]
        if obj.type in ('object', 'function', 'intrinsic', 'class') and desc.type == 'object':
            getter = desc.value.get('get')
            setter = desc.value.get('set')
            if getter and interp._is_callable(getter):
                obj.value[f"__get__{key}"] = getter
            if setter and interp._is_callable(setter):
                obj.value[f"__set__{key}"] = setter
            value = desc.value.get('value')
            if value is not None:
                obj.value[key] = value
        return JS_TRUE

    def _reflect_get_own_prop_desc(args, interp):
        if len(args) < 2: return UNDEFINED
        target, key = args[0], interp._to_key(args[1])
        if target.type not in ('object', 'function', 'intrinsic', 'class'): return UNDEFINED
        getter_key = f"__get__{key}"
        setter_key = f"__set__{key}"
        if getter_key in target.value or setter_key in target.value:
            desc = JsValue('object', {})
            if getter_key in target.value:
                desc.value['get'] = target.value[getter_key]
            if setter_key in target.value:
                desc.value['set'] = target.value[setter_key]
            desc.value['enumerable'] = JS_TRUE
            desc.value['configurable'] = JS_TRUE
            return desc
        if key in target.value:
            desc = JsValue('object', {})
            desc.value['value'] = target.value[key]
            desc.value['writable'] = JS_TRUE
            desc.value['enumerable'] = JS_TRUE
            desc.value['configurable'] = JS_TRUE
            return desc
        return UNDEFINED

    reflect_obj.value['get']                      = intr(_reflect_get,              'Reflect.get')
    reflect_obj.value['set']                      = intr(_reflect_set,              'Reflect.set')
    reflect_obj.value['has']                      = intr(_reflect_has,              'Reflect.has')
    reflect_obj.value['deleteProperty']           = intr(_reflect_delete,           'Reflect.deleteProperty')
    reflect_obj.value['apply']                    = intr(_reflect_apply,            'Reflect.apply')
    reflect_obj.value['construct']                = intr(_reflect_construct,         'Reflect.construct')
    reflect_obj.value['ownKeys']                  = intr(_reflect_own_keys,         'Reflect.ownKeys')
    reflect_obj.value['defineProperty']           = intr(_reflect_define_property,  'Reflect.defineProperty')
    reflect_obj.value['getOwnPropertyDescriptor'] = intr(_reflect_get_own_prop_desc,'Reflect.getOwnPropertyDescriptor')
    reflect_obj.value['getPrototypeOf']           = intr(
        lambda a, i: i._get_proto(a[0]) if a else UNDEFINED, 'Reflect.getPrototypeOf',
    )
    reflect_obj.value['setPrototypeOf']           = intr(lambda a, i: JS_TRUE, 'Reflect.setPrototypeOf')
    reflect_obj.value['isExtensible']             = intr(lambda a, i: JS_TRUE, 'Reflect.isExtensible')
    reflect_obj.value['preventExtensions']        = intr(lambda a, i: JS_TRUE, 'Reflect.preventExtensions')
    g.declare('Reflect', reflect_obj, 'var')

    # -- BigInt constructor --
    def _bigint_ctor(args, interp):
        if not args:
            raise _JSError(py_to_js('BigInt requires an argument'))
        v = args[0]
        if v.type == 'bigint': return v
        if v.type == 'number':
            if not math.isfinite(v.value):
                raise _JSError(py_to_js('Cannot convert non-finite number to BigInt'))
            return JsValue('bigint', int(v.value))
        if v.type == 'boolean': return JsValue('bigint', 1 if v.value else 0)
        if v.type == 'string':
            try: return JsValue('bigint', int(v.value.strip()))
            except (ValueError, TypeError): raise _JSError(py_to_js(f'Cannot convert "{v.value}" to a BigInt'))
        raise _JSError(py_to_js('Cannot convert to BigInt'))

    bigint_ctor = intr(_bigint_ctor, 'BigInt')

    def _bigint_as_int_n(args, interp):
        if len(args) < 2: return UNDEFINED
        n = int(interp._to_num(args[0]))
        val = args[1].value if args[1].type == 'bigint' else int(interp._to_num(args[1]))
        mod = 1 << n
        result = val % mod
        if result >= mod // 2:
            result -= mod
        return JsValue('bigint', result)

    def _bigint_as_uint_n(args, interp):
        if len(args) < 2: return UNDEFINED
        n = int(interp._to_num(args[0]))
        val = args[1].value if args[1].type == 'bigint' else int(interp._to_num(args[1]))
        return JsValue('bigint', val % (1 << n))

    bigint_ctor.value['asIntN']  = intr(_bigint_as_int_n,  'BigInt.asIntN')
    bigint_ctor.value['asUintN'] = intr(_bigint_as_uint_n, 'BigInt.asUintN')
    g.declare('BigInt', bigint_ctor, 'var')
    def _make_map(entries=None):
        store = []

        def _find_index(key):
            for index, (existing_key, _value) in enumerate(store):
                if interp._strict_eq(existing_key, key):
                    return index
            return -1

        map_obj = JsValue('object', {})

        def _map_set(args, interp):
            key = args[0] if args else UNDEFINED
            value = args[1] if len(args) > 1 else UNDEFINED
            index = _find_index(key)
            if index >= 0:
                store[index] = (key, value)
            else:
                store.append((key, value))
            return map_obj

        def _map_get(args, interp):
            key = args[0] if args else UNDEFINED
            index = _find_index(key)
            return store[index][1] if index >= 0 else UNDEFINED

        def _map_has(args, interp):
            return JS_TRUE if _find_index(args[0] if args else UNDEFINED) >= 0 else JS_FALSE

        def _map_delete(args, interp):
            index = _find_index(args[0] if args else UNDEFINED)
            if index >= 0:
                del store[index]
                return JS_TRUE
            return JS_FALSE

        def _map_clear(args, interp):
            store.clear()
            return UNDEFINED

        map_obj.value['__kind__'] = JsValue('string', 'Map')
        map_obj.value['__store__'] = store  # raw list of (key, val) tuples for inspector
        map_obj.value['set'] = intr(_map_set, 'Map.set')
        map_obj.value['get'] = intr(_map_get, 'Map.get')
        map_obj.value['has'] = intr(_map_has, 'Map.has')
        map_obj.value['delete'] = intr(_map_delete, 'Map.delete')
        map_obj.value['clear'] = intr(_map_clear, 'Map.clear')
        map_obj.value['__size_fn__'] = intr(lambda a, i: JsValue('number', len(store)), 'Map.__size__')
        def _make_map_iter_fn(get_items_fn):
            def _make_it(a, i):
                items = get_items_fn()
                idx = [0]
                it_obj = JsValue('object', {})
                def _next(tv, a2, intp, _items=items, _idx=idx):
                    if _idx[0] >= len(_items):
                        return JsValue('object', {'value': UNDEFINED, 'done': JS_TRUE})
                    val = _items[_idx[0]]; _idx[0] += 1
                    return JsValue('object', {'value': val, 'done': JS_FALSE})
                it_obj.value['next'] = i._make_intrinsic(_next, 'MapIterator.next')
                sym_k = f"@@{SYMBOL_ITERATOR}@@"
                it_obj.value[sym_k] = i._make_intrinsic(lambda tv, a2, intp, it=it_obj: it, '[Symbol.iterator]')
                i._add_iterator_helpers(it_obj)
                return it_obj
            return intr(_make_it, 'MapIterator')
        map_obj.value['keys'] = _make_map_iter_fn(lambda: [key for key, _ in store])
        map_obj.value['values'] = _make_map_iter_fn(lambda: [value for _, value in store])
        map_obj.value['entries'] = _make_map_iter_fn(lambda: [JsValue('array', [key, value]) for key, value in store])
        map_obj.value[f"@@{SYMBOL_ITERATOR}@@"] = _make_map_iter_fn(lambda: [JsValue('array', [key, value]) for key, value in store])
        def _map_for_each(args, interp):
            callback = args[0] if args else UNDEFINED
            this_arg = args[1] if len(args) > 1 else UNDEFINED
            if not interp._is_callable(callback):
                raise _JSError(py_to_js('TypeError: callback is not a function'))
            for key, value in list(store):
                interp._call_js(callback, [value, key, map_obj], this_arg)
            return UNDEFINED
        map_obj.value['forEach'] = intr(_map_for_each, 'Map.forEach')
        if entries is not None:
            for entry in interp._array_like_items(entries):
                if isinstance(entry, JsValue) and entry.type == 'array' and len(entry.value) >= 2:
                    store.append((entry.value[0], entry.value[1]))
        return map_obj

    def _make_set(values=None):
        store = []

        def _find_index(value):
            for index, existing in enumerate(store):
                if interp._strict_eq(existing, value):
                    return index
            return -1

        set_obj = JsValue('object', {})

        def _set_add(args, interp):
            value = args[0] if args else UNDEFINED
            if _find_index(value) < 0:
                store.append(value)
            return set_obj

        def _set_has(args, interp):
            return JS_TRUE if _find_index(args[0] if args else UNDEFINED) >= 0 else JS_FALSE

        def _set_delete(args, interp):
            index = _find_index(args[0] if args else UNDEFINED)
            if index >= 0:
                del store[index]
                return JS_TRUE
            return JS_FALSE

        def _set_clear(args, interp):
            store.clear()
            return UNDEFINED

        set_obj.value['__kind__'] = JsValue('string', 'Set')
        set_obj.value['__store__'] = store  # raw list for inspector
        set_obj.value['add'] = intr(_set_add, 'Set.add')
        set_obj.value['has'] = intr(_set_has, 'Set.has')
        set_obj.value['delete'] = intr(_set_delete, 'Set.delete')
        set_obj.value['clear'] = intr(_set_clear, 'Set.clear')
        set_obj.value['__size_fn__'] = intr(lambda a, i: JsValue('number', len(store)), 'Set.__size__')
        def _make_set_iter_fn(get_items_fn):
            def _make_it(a, i):
                items = get_items_fn()
                idx = [0]
                it_obj = JsValue('object', {})
                def _next(tv, a2, intp, _items=items, _idx=idx):
                    if _idx[0] >= len(_items):
                        return JsValue('object', {'value': UNDEFINED, 'done': JS_TRUE})
                    val = _items[_idx[0]]; _idx[0] += 1
                    return JsValue('object', {'value': val, 'done': JS_FALSE})
                it_obj.value['next'] = i._make_intrinsic(_next, 'SetIterator.next')
                sym_k = f"@@{SYMBOL_ITERATOR}@@"
                it_obj.value[sym_k] = i._make_intrinsic(lambda tv, a2, intp, it=it_obj: it, '[Symbol.iterator]')
                i._add_iterator_helpers(it_obj)
                return it_obj
            return intr(_make_it, 'SetIterator')
        set_obj.value['values'] = _make_set_iter_fn(lambda: list(store))
        set_obj.value['keys'] = _make_set_iter_fn(lambda: list(store))
        set_obj.value['entries'] = _make_set_iter_fn(lambda: [JsValue('array', [v, v]) for v in store])
        set_obj.value[f"@@{SYMBOL_ITERATOR}@@"] = _make_set_iter_fn(lambda: list(store))

        def _set_for_each(args, interp):
            callback = args[0] if args else UNDEFINED
            this_arg = args[1] if len(args) > 1 else UNDEFINED
            if not interp._is_callable(callback):
                raise _JSError(py_to_js('TypeError: callback is not a function'))
            for value in list(store):
                interp._call_js(callback, [value, value, set_obj], this_arg)
            return UNDEFINED
        set_obj.value['forEach'] = intr(_set_for_each, 'Set.forEach')

        # ES2025 Set methods
        def _drain_set_iterable(other):
            """Drain any JS iterable to a Python list of JsValues."""
            if other.type == 'array':
                return list(other.value)
            it_fn = interp._get_js_iterator(other)
            if it_fn is None:
                return []
            items = []
            while True:
                r = it_fn()
                if not isinstance(r, JsValue) or r.type != 'object':
                    break
                done = r.value.get('done', JS_FALSE)
                if isinstance(done, JsValue) and done.value is True:
                    break
                items.append(r.value.get('value', UNDEFINED))
            return items

        def _make_new_set(items):
            new_s = _make_set()
            new_add = new_s.value['add']
            for item in items:
                interp._call_js(new_add, [item], new_s)
            return new_s

        def _other_contains(other_items, value):
            return any(interp._strict_eq(item, value) for item in other_items)

        def _set_union(args, interp):
            other = args[0] if args else UNDEFINED
            other_items = _drain_set_iterable(other)
            result_items = list(store)
            for item in other_items:
                if not _other_contains(result_items, item):
                    result_items.append(item)
            return _make_new_set(result_items)

        def _set_intersection(args, interp):
            other = args[0] if args else UNDEFINED
            other_items = _drain_set_iterable(other)
            return _make_new_set([v for v in store if _other_contains(other_items, v)])

        def _set_difference(args, interp):
            other = args[0] if args else UNDEFINED
            other_items = _drain_set_iterable(other)
            return _make_new_set([v for v in store if not _other_contains(other_items, v)])

        def _set_symmetric_difference(args, interp):
            other = args[0] if args else UNDEFINED
            other_items = _drain_set_iterable(other)
            result = [v for v in store if not _other_contains(other_items, v)]
            for item in other_items:
                if not _other_contains(store, item):
                    result.append(item)
            return _make_new_set(result)

        def _set_is_subset_of(args, interp):
            other = args[0] if args else UNDEFINED
            other_items = _drain_set_iterable(other)
            return JS_TRUE if all(_other_contains(other_items, v) for v in store) else JS_FALSE

        def _set_is_superset_of(args, interp):
            other = args[0] if args else UNDEFINED
            other_items = _drain_set_iterable(other)
            return JS_TRUE if all(_other_contains(store, v) for v in other_items) else JS_FALSE

        def _set_is_disjoint_from(args, interp):
            other = args[0] if args else UNDEFINED
            other_items = _drain_set_iterable(other)
            return JS_FALSE if any(_other_contains(other_items, v) for v in store) else JS_TRUE

        set_obj.value['union'] = intr(_set_union, 'Set.union')
        set_obj.value['intersection'] = intr(_set_intersection, 'Set.intersection')
        set_obj.value['difference'] = intr(_set_difference, 'Set.difference')
        set_obj.value['symmetricDifference'] = intr(_set_symmetric_difference, 'Set.symmetricDifference')
        set_obj.value['isSubsetOf'] = intr(_set_is_subset_of, 'Set.isSubsetOf')
        set_obj.value['isSupersetOf'] = intr(_set_is_superset_of, 'Set.isSupersetOf')
        set_obj.value['isDisjointFrom'] = intr(_set_is_disjoint_from, 'Set.isDisjointFrom')
        if values is not None:
            for value in interp._array_like_items(values):
                if _find_index(value) < 0:
                    store.append(value)
        return set_obj

    map_ctor = interp._make_intrinsic(lambda this_val, args, interp: _make_map(args[0] if args else None), 'Map')
    def _map_group_by(args, interp):
        iterable = args[0] if args else UNDEFINED
        callback = args[1] if len(args) > 1 else UNDEFINED
        if not interp._is_callable(callback):
            raise _JSError(py_to_js('TypeError: callback is not a function'))
        items = interp._array_like_items(iterable)
        result = _make_map()
        map_set_fn = result.value['set']
        for idx, item in enumerate(items):
            key = interp._call_js(callback, [item, JsValue('number', float(idx))], UNDEFINED)
            map_get_fn = result.value['get']
            map_has_fn = result.value['has']
            has = interp._call_js(map_has_fn, [key], result)
            if interp._truthy(has):
                existing = interp._call_js(map_get_fn, [key], result)
                existing.value.append(item)
            else:
                interp._call_js(map_set_fn, [key, JsValue('array', [item])], result)
        return result
    map_ctor.value['groupBy'] = intr(_map_group_by, 'Map.groupBy')
    g.declare('Map', map_ctor, 'var')
    g.declare('Set', interp._make_intrinsic(lambda this_val, args, interp: _make_set(args[0] if args else None), 'Set'), 'var')

    def _promise_ctor(this_val, args, interp):
        executor = args[0] if args else UNDEFINED
        if not interp._is_callable(executor):
            raise _JSError(py_to_js('Promise executor must be a function'))
        promise = interp._new_promise()
        resolve_fn = interp._make_intrinsic(
            lambda _this, call_args, inner: inner._resolve_promise(promise, call_args[0] if call_args else UNDEFINED),
            'Promise.resolve',
        )
        reject_fn = interp._make_intrinsic(
            lambda _this, call_args, inner: inner._reject_promise(promise, call_args[0] if call_args else UNDEFINED),
            'Promise.reject',
        )
        try:
            interp._call_js(executor, [resolve_fn, reject_fn], UNDEFINED)
        except _JSError as exc:
            interp._reject_promise(promise, exc.value)
        return promise

    promise_ctor = interp._make_intrinsic(_promise_ctor, 'Promise')
    promise_ctor.value['resolve'] = interp._make_intrinsic(
        lambda this_val, args, interp: interp._to_promise(args[0] if args else UNDEFINED),
        'Promise.resolve',
    )
    promise_ctor.value['reject'] = interp._make_intrinsic(
        lambda this_val, args, interp: interp._rejected_promise(args[0] if args else UNDEFINED),
        'Promise.reject',
    )
    promise_ctor.value['all'] = interp._make_intrinsic(
        lambda this_val, args, interp: interp._promise_all(args[0].value if args and args[0].type == 'array' else []),
        'Promise.all',
    )
    promise_ctor.value['race'] = interp._make_intrinsic(
        lambda this_val, args, interp: interp._promise_race(args[0].value if args and args[0].type == 'array' else []),
        'Promise.race',
    )
    promise_ctor.value['allSettled'] = interp._make_intrinsic(
        lambda this_val, args, interp: interp._promise_all_settled(
            args[0].value if args and args[0].type == 'array' else []
        ),
        'Promise.allSettled',
    )
    promise_ctor.value['any'] = interp._make_intrinsic(
        lambda this_val, args, interp: interp._promise_any(
            args[0].value if args and args[0].type == 'array' else []
        ),
        'Promise.any',
    )

    def _promise_with_resolvers(args, interp):
        promise = interp._new_promise()
        resolve_fn = interp._make_intrinsic(
            lambda _this, call_args, inner: inner._resolve_promise(
                promise, call_args[0] if call_args else UNDEFINED
            ),
            'resolve',
        )
        reject_fn = interp._make_intrinsic(
            lambda _this, call_args, inner: inner._reject_promise(
                promise, call_args[0] if call_args else UNDEFINED
            ),
            'reject',
        )
        return JsValue('object', {
            'promise': promise,
            'resolve': resolve_fn,
            'reject': reject_fn,
        })

    promise_ctor.value['withResolvers'] = intr(_promise_with_resolvers, 'Promise.withResolvers')

    def _promise_try(args, interp):
        fn = args[0] if args else UNDEFINED
        try:
            result = interp._call_js(fn, args[1:], UNDEFINED)
            return interp._to_promise(result)
        except _JSError as e:
            return interp._rejected_promise(e.value)
        except (_JSReturn, _JSBreak, _JSContinue):
            raise
        except Exception as e:  # Catches non-control-flow errors
            js_err = interp._make_js_error('Error', str(e))
            return interp._rejected_promise(js_err)

    promise_ctor.value['try'] = intr(_promise_try, 'Promise.try')
    g.declare('Promise', promise_ctor, 'var')

    pyvm_obj = JsValue('object', {})

    def _pyvm_args(value, interp):
        if value is None or value is UNDEFINED:
            return []
        if value.type != 'array':
            raise _JSError(py_to_js('PyVM argv must be an array'))
        return [interp._to_str(item) for item in value.value]

    def _pyvm_arg_list(value, interp, label):
        if value is None or value is UNDEFINED:
            return []
        if value.type == 'array':
            return [interp._to_str(item) for item in value.value]
        if value.type == 'string':
            return [value.value]
        raise _JSError(py_to_js(f'{label} must be a string or array'))

    def _pyvm_timeout(value, interp):
        if value is None or value is UNDEFINED:
            return None
        timeout_ms = interp._to_num(value)
        return max(0.0, timeout_ms) / 1000.0

    def _pyvm_options(value, interp):
        if value is None or value is UNDEFINED:
            return {}
        if value.type != 'object':
            raise _JSError(py_to_js('PyVM options must be an object'))
        raw = interp._to_py(value)
        options = {}
        if raw.get('cwd') is not None:
            options['cwd'] = str(raw['cwd'])
        timeout_ms = raw.get('timeoutMs')
        if timeout_ms is not None:
            options['timeout'] = max(0.0, float(timeout_ms)) / 1000.0
        env = raw.get('env')
        if env is not None:
            merged = dict(os.environ)
            for key, item in env.items():
                merged[str(key)] = '' if item is None else str(item)
            options['env'] = merged
        return options

    def _pyvm_result(proc, command):
        return py_to_js({
            'ok': proc.returncode == 0,
            'code': proc.returncode,
            'stdout': proc.stdout,
            'stderr': proc.stderr,
            'command': command,
        })

    def _pyvm_run_command(command, options):
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            cwd=options.get('cwd'),
            env=options.get('env'),
            timeout=options.get('timeout'),
        )

    def _pyvm_exec(args, interp):
        if not args:
            raise _JSError(py_to_js('PyVM.exec requires Python source'))
        code = interp._to_str(args[0])
        argv = _pyvm_args(args[1] if len(args) > 1 else UNDEFINED, interp)
        options = _pyvm_options(args[2] if len(args) > 2 else UNDEFINED, interp)
        command = [sys.executable, '-c', code, *argv]
        proc = _pyvm_run_command(command, options)
        return _pyvm_result(proc, command)

    def _pyvm_run_file(args, interp):
        if not args:
            raise _JSError(py_to_js('PyVM.runFile requires a path'))
        path = interp._to_str(args[0])
        argv = _pyvm_args(args[1] if len(args) > 1 else UNDEFINED, interp)
        options = _pyvm_options(args[2] if len(args) > 2 else UNDEFINED, interp)
        command = [sys.executable, path, *argv]
        proc = _pyvm_run_command(command, options)
        return _pyvm_result(proc, command)

    def _pyvm_run_module(args, interp):
        if not args:
            raise _JSError(py_to_js('PyVM.runModule requires a module name'))
        module = interp._to_str(args[0])
        argv = _pyvm_args(args[1] if len(args) > 1 else UNDEFINED, interp)
        options = _pyvm_options(args[2] if len(args) > 2 else UNDEFINED, interp)
        command = [sys.executable, '-m', module, *argv]
        proc = _pyvm_run_command(command, options)
        return _pyvm_result(proc, command)

    def _pyvm_pip_install(args, interp):
        packages = _pyvm_arg_list(args[0] if args else UNDEFINED, interp, 'PyVM.pipInstall packages')
        if not packages:
            raise _JSError(py_to_js('PyVM.pipInstall requires at least one package'))
        extra_args = _pyvm_args(args[1] if len(args) > 1 else UNDEFINED, interp)
        options = _pyvm_options(args[2] if len(args) > 2 else UNDEFINED, interp)
        command = [sys.executable, '-m', 'pip', 'install', *packages, *extra_args]
        proc = _pyvm_run_command(command, options)
        return _pyvm_result(proc, command)

    def _pyvm_pip_show(args, interp):
        packages = _pyvm_arg_list(args[0] if args else UNDEFINED, interp, 'PyVM.pipShow packages')
        if not packages:
            raise _JSError(py_to_js('PyVM.pipShow requires at least one package'))
        options = _pyvm_options(args[1] if len(args) > 1 else UNDEFINED, interp)
        command = [sys.executable, '-m', 'pip', 'show', *packages]
        proc = _pyvm_run_command(command, options)
        return _pyvm_result(proc, command)

    def _pyvm_pip_list(args, interp):
        options = _pyvm_options(args[0] if args else UNDEFINED, interp)
        command = [sys.executable, '-m', 'pip', 'list', '--format=json']
        proc = _pyvm_run_command(command, options)
        result = _pyvm_result(proc, command)
        if proc.returncode == 0:
            result.value['packages'] = py_to_js(json.loads(proc.stdout or '[]'))
        else:
            result.value['packages'] = py_to_js([])
        return result

    def _host_os_cwd(args, interp):
        return py_to_js(os.getcwd())

    def _host_os_chdir(args, interp):
        if not args:
            raise _JSError(py_to_js('os.chdir requires a path'))
        os.chdir(interp._to_str(args[0]))
        return py_to_js(os.getcwd())

    def _host_os_listdir(args, interp):
        path = interp._to_str(args[0]) if args else '.'
        return py_to_js(sorted(os.listdir(path)))

    def _host_os_exists(args, interp):
        if not args:
            raise _JSError(py_to_js('os.exists requires a path'))
        return py_to_js(os.path.exists(interp._to_str(args[0])))

    def _host_os_mkdir(args, interp):
        if not args:
            raise _JSError(py_to_js('os.mkdir requires a path'))
        path = interp._to_str(args[0])
        parents = bool(args[1].value) if len(args) > 1 and args[1].type == 'boolean' else False
        if parents:
            os.makedirs(path, exist_ok=True)
        else:
            os.mkdir(path)
        return py_to_js(path)

    def _host_os_read_text(args, interp):
        if not args:
            raise _JSError(py_to_js('os.readText requires a path'))
        path = interp._to_str(args[0])
        encoding = interp._to_str(args[1]) if len(args) > 1 else 'utf-8'
        with open(path, encoding=encoding) as handle:
            return py_to_js(handle.read())

    def _host_os_write_text(args, interp):
        if len(args) < 2:
            raise _JSError(py_to_js('os.writeText requires path and content'))
        path = interp._to_str(args[0])
        content = interp._to_str(args[1])
        encoding = interp._to_str(args[2]) if len(args) > 2 else 'utf-8'
        with open(path, 'w', encoding=encoding) as handle:
            handle.write(content)
        return py_to_js(path)

    def _host_os_stat(args, interp):
        if not args:
            raise _JSError(py_to_js('os.stat requires a path'))
        path = interp._to_str(args[0])
        info = os.stat(path)
        return py_to_js({
            'size': info.st_size,
            'mode': info.st_mode,
            'mtimeMs': info.st_mtime * 1000.0,
            'isFile': os.path.isfile(path),
            'isDir': os.path.isdir(path),
        })

    def _host_os_getenv(args, interp):
        if not args:
            raise _JSError(py_to_js('os.getenv requires a key'))
        key = interp._to_str(args[0])
        default = interp._to_str(args[1]) if len(args) > 1 else None
        value = os.environ.get(key, default)
        return UNDEFINED if value is None else py_to_js(value)

    def _host_os_setenv(args, interp):
        if len(args) < 2:
            raise _JSError(py_to_js('os.setenv requires key and value'))
        key = interp._to_str(args[0])
        value = interp._to_str(args[1])
        os.environ[key] = value
        return py_to_js(value)

    def _host_os_unsetenv(args, interp):
        if not args:
            raise _JSError(py_to_js('os.unsetenv requires a key'))
        key = interp._to_str(args[0])
        existed = key in os.environ
        os.environ.pop(key, None)
        return py_to_js(existed)

    def _host_os_which(args, interp):
        if not args:
            raise _JSError(py_to_js('os.which requires a command name'))
        result = shutil.which(interp._to_str(args[0]))
        return UNDEFINED if result is None else py_to_js(result)

    def _host_os_join(args, interp):
        if not args:
            return py_to_js('')
        return py_to_js(os.path.join(*(interp._to_str(arg) for arg in args)))

    def _host_os_dirname(args, interp):
        if not args:
            raise _JSError(py_to_js('os.dirname requires a path'))
        return py_to_js(os.path.dirname(interp._to_str(args[0])))

    def _host_os_basename(args, interp):
        if not args:
            raise _JSError(py_to_js('os.basename requires a path'))
        return py_to_js(os.path.basename(interp._to_str(args[0])))

    def _host_os_abspath(args, interp):
        if not args:
            raise _JSError(py_to_js('os.abspath requires a path'))
        return py_to_js(os.path.abspath(interp._to_str(args[0])))

    def _host_os_remove(args, interp):
        if not args:
            raise _JSError(py_to_js('os.remove requires a path'))
        path = interp._to_str(args[0])
        os.remove(path)
        return py_to_js(path)

    def _host_os_rmdir(args, interp):
        if not args:
            raise _JSError(py_to_js('os.rmdir requires a path'))
        path = interp._to_str(args[0])
        recursive = bool(args[1].value) if len(args) > 1 and args[1].type == 'boolean' else False
        if recursive:
            shutil.rmtree(path)
        else:
            os.rmdir(path)
        return py_to_js(path)

    def _host_os_rename(args, interp):
        if len(args) < 2:
            raise _JSError(py_to_js('os.rename requires source and destination'))
        src = interp._to_str(args[0])
        dst = interp._to_str(args[1])
        os.replace(src, dst)
        return py_to_js(dst)

    def _pyvm_async_runner(sync_fn, label):
        def runner(args, interp):
            promise = interp._new_promise()

            def task():
                try:
                    interp._resolve_promise(promise, sync_fn(args, interp))
                except _JSError as exc:
                    interp._reject_promise(promise, exc.value)
                except subprocess.TimeoutExpired as exc:
                    interp._reject_promise(promise, py_to_js(f'{label} timed out: {exc}'))
                except Exception as exc:  # Catches non-control-flow errors
                    interp._reject_promise(promise, interp._make_js_error('Error', str(exc)))

            interp._enqueue_microtask(task)
            return promise

        return runner

    pyvm_obj.value['executable'] = py_to_js(sys.executable)
    pyvm_obj.value['version'] = py_to_js(sys.version)
    pyvm_obj.value['exec'] = intr(_pyvm_exec, 'PyVM.exec')
    pyvm_obj.value['runFile'] = intr(_pyvm_run_file, 'PyVM.runFile')
    pyvm_obj.value['runModule'] = intr(_pyvm_run_module, 'PyVM.runModule')
    pyvm_obj.value['pipInstall'] = intr(_pyvm_pip_install, 'PyVM.pipInstall')
    pyvm_obj.value['pipShow'] = intr(_pyvm_pip_show, 'PyVM.pipShow')
    pyvm_obj.value['pipList'] = intr(_pyvm_pip_list, 'PyVM.pipList')
    pyvm_obj.value['execAsync'] = intr(_pyvm_async_runner(_pyvm_exec, 'PyVM.execAsync'), 'PyVM.execAsync')
    pyvm_obj.value['runFileAsync'] = intr(_pyvm_async_runner(_pyvm_run_file, 'PyVM.runFileAsync'), 'PyVM.runFileAsync')
    pyvm_obj.value['runModuleAsync'] = intr(_pyvm_async_runner(_pyvm_run_module, 'PyVM.runModuleAsync'), 'PyVM.runModuleAsync')
    pyvm_obj.value['pipInstallAsync'] = intr(_pyvm_async_runner(_pyvm_pip_install, 'PyVM.pipInstallAsync'), 'PyVM.pipInstallAsync')
    pyvm_obj.value['pipShowAsync'] = intr(_pyvm_async_runner(_pyvm_pip_show, 'PyVM.pipShowAsync'), 'PyVM.pipShowAsync')
    pyvm_obj.value['pipListAsync'] = intr(_pyvm_async_runner(_pyvm_pip_list, 'PyVM.pipListAsync'), 'PyVM.pipListAsync')

    g.declare('PyVM', pyvm_obj, 'var')

    host_os = JsValue('object', {})
    host_os.value['name'] = py_to_js(os.name)
    host_os.value['sep'] = py_to_js(os.sep)
    host_os.value['linesep'] = py_to_js(os.linesep)
    host_os.value['cwd'] = intr(_host_os_cwd, 'os.cwd')
    host_os.value['chdir'] = intr(_host_os_chdir, 'os.chdir')
    host_os.value['listdir'] = intr(_host_os_listdir, 'os.listdir')
    host_os.value['exists'] = intr(_host_os_exists, 'os.exists')
    host_os.value['mkdir'] = intr(_host_os_mkdir, 'os.mkdir')
    host_os.value['readText'] = intr(_host_os_read_text, 'os.readText')
    host_os.value['writeText'] = intr(_host_os_write_text, 'os.writeText')
    host_os.value['stat'] = intr(_host_os_stat, 'os.stat')
    host_os.value['getenv'] = intr(_host_os_getenv, 'os.getenv')
    host_os.value['setenv'] = intr(_host_os_setenv, 'os.setenv')
    host_os.value['unsetenv'] = intr(_host_os_unsetenv, 'os.unsetenv')
    host_os.value['which'] = intr(_host_os_which, 'os.which')
    host_os.value['join'] = intr(_host_os_join, 'os.join')
    host_os.value['dirname'] = intr(_host_os_dirname, 'os.dirname')
    host_os.value['basename'] = intr(_host_os_basename, 'os.basename')
    host_os.value['abspath'] = intr(_host_os_abspath, 'os.abspath')
    host_os.value['remove'] = intr(_host_os_remove, 'os.remove')
    host_os.value['rmdir'] = intr(_host_os_rmdir, 'os.rmdir')
    host_os.value['rename'] = intr(_host_os_rename, 'os.rename')
    g.declare('os', host_os, 'var')

    host_sys = JsValue('object', {})
    host_sys.value['executable'] = py_to_js(sys.executable)
    host_sys.value['platform'] = py_to_js(sys.platform)
    host_sys.value['version'] = py_to_js(sys.version)
    host_sys.value['versionInfo'] = py_to_js(list(sys.version_info[:5]))
    host_sys.value['path'] = py_to_js(list(sys.path))
    g.declare('sys', host_sys, 'var')

    # -- Iterator global (ES2025) --
    iterator_ctor = JsValue('object', {})
    def _iterator_from(args, interp):
        src = args[0] if args else UNDEFINED
        if src.type == 'undefined':
            raise _JSError(interp._make_js_error('TypeError', 'Iterator.from: argument must be iterable'))
        # If already an iterator with .next, wrap it and add helpers
        next_fn = interp._get_prop(src, 'next')
        if next_fn.type != 'undefined':
            interp._add_iterator_helpers(src)
            return src
        # Try Symbol.iterator protocol
        iter_sym_key = f'@@{SYMBOL_ITERATOR}@@'
        sym_fn = src.value.get(iter_sym_key) if isinstance(src.value, dict) else None
        if sym_fn is None and src.type == 'array':
            items = list(src.value)
            idx = [0]
            def _next(tv, a, i, _items=items, _idx=idx):
                if _idx[0] >= len(_items):
                    return JsValue('object', {'done': JS_TRUE, 'value': UNDEFINED})
                val = _items[_idx[0]]
                _idx[0] += 1
                return JsValue('object', {'done': JS_FALSE, 'value': val})
            it = JsValue('object', {'next': interp._make_intrinsic(_next, 'IteratorFrom.next')})
            interp._add_iterator_helpers(it)
            return it
        if sym_fn and isinstance(sym_fn, JsValue):
            it = interp._call_js(sym_fn, [], src)
            interp._add_iterator_helpers(it)
            return it
        raise _JSError(interp._make_js_error('TypeError', 'Iterator.from: argument is not iterable'))
    iterator_ctor.value['from'] = intr(_iterator_from, 'Iterator.from')
    g.declare('Iterator', iterator_ctor, 'var')

    _log.info("registered advanced builtins (Proxy, Reflect, Map, Set, ...)")


