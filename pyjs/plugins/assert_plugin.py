"""Assert plugin — provides Node.js-like `assert` module."""
from __future__ import annotations

from ..plugin import PyJSPlugin


class AssertPlugin(PyJSPlugin):
    """Exposes assert(), assert.equal, assert.strictEqual, assert.deepEqual, etc."""
    name = "assert"
    version = "1.0.0"

    def setup(self, ctx):
        from ..values import JsValue, UNDEFINED, JS_NULL, JS_TRUE, JS_FALSE
        from ..core import py_to_js, js_to_py

        def _deep_equal(a, b, strict=False):
            """Recursive deep equality check."""
            if a is b:
                return True
            if a.type != b.type:
                if not strict:
                    return js_to_py(a) == js_to_py(b)
                return False
            if a.type in ('undefined', 'null'):
                return True
            if a.type in ('boolean', 'number', 'string', 'bigint'):
                if a.type == 'number':
                    import math
                    if math.isnan(a.value) and math.isnan(b.value):
                        return True
                return a.value == b.value
            if a.type == 'array':
                if len(a.value) != len(b.value):
                    return False
                return all(_deep_equal(x, y, strict) for x, y in zip(a.value, b.value))
            if a.type == 'object':
                keys_a = set(k for k in a.value if not k.startswith('__'))
                keys_b = set(k for k in b.value if not k.startswith('__'))
                if keys_a != keys_b:
                    return False
                return all(_deep_equal(a.value[k], b.value[k], strict) for k in keys_a)
            return a is b

        def _assert_fail(message):
            raise Exception(f"AssertionError: {message}")

        def _assert_fn(this_val, args, interp):
            val = args[0] if args else UNDEFINED
            msg = js_to_py(args[1]) if len(args) > 1 else 'Assertion failed'
            if not interp._truthy(val):
                _assert_fail(msg)
            return UNDEFINED

        def _ok(this_val, args, interp):
            return _assert_fn(this_val, args, interp)

        def _equal(this_val, args, interp):
            actual = args[0] if args else UNDEFINED
            expected = args[1] if len(args) > 1 else UNDEFINED
            msg = js_to_py(args[2]) if len(args) > 2 else None
            if js_to_py(actual) != js_to_py(expected):
                m = msg or f"Expected {interp._to_str(expected)}, got {interp._to_str(actual)}"
                _assert_fail(m)
            return UNDEFINED

        def _not_equal(this_val, args, interp):
            actual = args[0] if args else UNDEFINED
            expected = args[1] if len(args) > 1 else UNDEFINED
            msg = js_to_py(args[2]) if len(args) > 2 else None
            if js_to_py(actual) == js_to_py(expected):
                m = msg or "Expected values to be unequal"
                _assert_fail(m)
            return UNDEFINED

        def _strict_equal(this_val, args, interp):
            actual = args[0] if args else UNDEFINED
            expected = args[1] if len(args) > 1 else UNDEFINED
            msg = js_to_py(args[2]) if len(args) > 2 else None
            if actual.type != expected.type or actual.value != expected.value:
                m = msg or f"Expected {interp._to_str(expected)}, got {interp._to_str(actual)}"
                _assert_fail(m)
            return UNDEFINED

        def _not_strict_equal(this_val, args, interp):
            actual = args[0] if args else UNDEFINED
            expected = args[1] if len(args) > 1 else UNDEFINED
            msg = js_to_py(args[2]) if len(args) > 2 else None
            if actual.type == expected.type and actual.value == expected.value:
                m = msg or "Expected values to be strictly unequal"
                _assert_fail(m)
            return UNDEFINED

        def _deep_equal_fn(this_val, args, interp):
            actual = args[0] if args else UNDEFINED
            expected = args[1] if len(args) > 1 else UNDEFINED
            msg = js_to_py(args[2]) if len(args) > 2 else None
            if not _deep_equal(actual, expected, strict=False):
                m = msg or "Expected deep equal"
                _assert_fail(m)
            return UNDEFINED

        def _deep_strict_equal(this_val, args, interp):
            actual = args[0] if args else UNDEFINED
            expected = args[1] if len(args) > 1 else UNDEFINED
            msg = js_to_py(args[2]) if len(args) > 2 else None
            if not _deep_equal(actual, expected, strict=True):
                m = msg or "Expected deep strict equal"
                _assert_fail(m)
            return UNDEFINED

        def _not_deep_equal(this_val, args, interp):
            actual = args[0] if args else UNDEFINED
            expected = args[1] if len(args) > 1 else UNDEFINED
            msg = js_to_py(args[2]) if len(args) > 2 else None
            if _deep_equal(actual, expected, strict=False):
                m = msg or "Expected values to not be deeply equal"
                _assert_fail(m)
            return UNDEFINED

        def _throws(this_val, args, interp):
            fn = args[0] if args else UNDEFINED
            msg = js_to_py(args[1]) if len(args) > 1 and args[1].type == 'string' else None
            try:
                interp._call_js(fn, [])
            except Exception:
                return UNDEFINED
            _assert_fail(msg or "Expected function to throw")

        def _does_not_throw(this_val, args, interp):
            fn = args[0] if args else UNDEFINED
            msg = js_to_py(args[1]) if len(args) > 1 and args[1].type == 'string' else None
            try:
                interp._call_js(fn, [])
            except Exception as e:
                _assert_fail(msg or f"Expected function not to throw, but it threw: {e}")
            return UNDEFINED

        def _match_fn(this_val, args, interp):
            string = args[0] if args else UNDEFINED
            regexp = args[1] if len(args) > 1 else UNDEFINED
            msg = js_to_py(args[2]) if len(args) > 2 else None
            import re
            s = interp._to_str(string)
            if regexp.type == 'regexp':
                pat = regexp.value['pattern']
                flags_str = regexp.value.get('flags', '')
                flags = 0
                if 'i' in flags_str:
                    flags |= re.IGNORECASE
                if 'm' in flags_str:
                    flags |= re.MULTILINE
                if 's' in flags_str:
                    flags |= re.DOTALL
                if not re.search(pat, s, flags):
                    _assert_fail(msg or "String did not match pattern")
            return UNDEFINED

        # Create assert as a callable intrinsic function
        assert_fn = ctx.make_function(_assert_fn, 'assert')

        # Attach methods directly to the intrinsic's value dict
        assert_fn.value['ok'] = ctx.make_function(_ok, 'assert.ok')
        assert_fn.value['equal'] = ctx.make_function(_equal, 'assert.equal')
        assert_fn.value['notEqual'] = ctx.make_function(_not_equal, 'assert.notEqual')
        assert_fn.value['strictEqual'] = ctx.make_function(_strict_equal, 'assert.strictEqual')
        assert_fn.value['notStrictEqual'] = ctx.make_function(_not_strict_equal, 'assert.notStrictEqual')
        assert_fn.value['deepEqual'] = ctx.make_function(_deep_equal_fn, 'assert.deepEqual')
        assert_fn.value['deepStrictEqual'] = ctx.make_function(_deep_strict_equal, 'assert.deepStrictEqual')
        assert_fn.value['notDeepEqual'] = ctx.make_function(_not_deep_equal, 'assert.notDeepEqual')
        assert_fn.value['throws'] = ctx.make_function(_throws, 'assert.throws')
        assert_fn.value['doesNotThrow'] = ctx.make_function(_does_not_throw, 'assert.doesNotThrow')
        assert_fn.value['match'] = ctx.make_function(_match_fn, 'assert.match')

        ctx.add_global('assert', assert_fn)
