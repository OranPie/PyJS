"""Crypto plugin — extends the built-in crypto global with hashing and HMAC."""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import secrets

from ..plugin import PyJSPlugin, PluginContext
from ..values import JsValue, UNDEFINED, JS_TRUE, JS_FALSE
from ..core import py_to_js, js_to_py


# Map Web Crypto algorithm names to hashlib names
_SUBTLE_ALGORITHMS = {
    'SHA-1': 'sha1',
    'SHA-256': 'sha256',
    'SHA-384': 'sha384',
    'SHA-512': 'sha512',
}

_HASH_ALGORITHMS = frozenset({'md5', 'sha1', 'sha256', 'sha384', 'sha512'})


class CryptoSubtlePlugin(PyJSPlugin):
    name = "crypto-subtle"
    version = "1.0.0"

    def setup(self, ctx: PluginContext) -> None:
        interp = ctx.get_interpreter()
        crypto = interp.genv.get('crypto')

        # -- crypto.createHash(algorithm) -----------------------------------

        def _create_hash(this_val, args, interp_inner):
            algo = js_to_py(args[0]).lower() if args else ''
            if algo not in _HASH_ALGORITHMS:
                raise Exception(f"Unsupported hash algorithm: {algo}")
            h = hashlib.new(algo)

            hash_obj = JsValue('object', {})

            def _update(this_val2, args2, interp2):
                data = js_to_py(args2[0]) if args2 else ''
                h.update(data.encode('utf-8') if isinstance(data, str) else data)
                return hash_obj

            def _digest(this_val2, args2, interp2):
                encoding = js_to_py(args2[0]) if args2 else 'hex'
                if encoding == 'hex':
                    return py_to_js(h.hexdigest())
                elif encoding == 'base64':
                    import base64
                    return py_to_js(base64.b64encode(h.digest()).decode('ascii'))
                else:
                    return py_to_js(h.hexdigest())

            hash_obj.value['update'] = interp_inner._make_intrinsic(_update, 'Hash.update')
            hash_obj.value['digest'] = interp_inner._make_intrinsic(_digest, 'Hash.digest')
            return hash_obj

        # -- crypto.createHmac(algorithm, key) ------------------------------

        def _create_hmac(this_val, args, interp_inner):
            algo = js_to_py(args[0]).lower() if args else ''
            key = js_to_py(args[1]) if len(args) > 1 else ''
            if algo not in _HASH_ALGORITHMS:
                raise Exception(f"Unsupported HMAC algorithm: {algo}")
            key_bytes = key.encode('utf-8') if isinstance(key, str) else key
            h = hmac_mod.new(key_bytes, digestmod=algo)

            hmac_obj = JsValue('object', {})

            def _update(this_val2, args2, interp2):
                data = js_to_py(args2[0]) if args2 else ''
                h.update(data.encode('utf-8') if isinstance(data, str) else data)
                return hmac_obj

            def _digest(this_val2, args2, interp2):
                encoding = js_to_py(args2[0]) if args2 else 'hex'
                if encoding == 'hex':
                    return py_to_js(h.hexdigest())
                elif encoding == 'base64':
                    import base64
                    return py_to_js(base64.b64encode(h.digest()).decode('ascii'))
                else:
                    return py_to_js(h.hexdigest())

            hmac_obj.value['update'] = interp_inner._make_intrinsic(_update, 'Hmac.update')
            hmac_obj.value['digest'] = interp_inner._make_intrinsic(_digest, 'Hmac.digest')
            return hmac_obj

        # -- crypto.pbkdf2Sync(password, salt, iterations, keylen, digest) --

        def _pbkdf2_sync(this_val, args, interp_inner):
            if len(args) < 5:
                raise Exception('pbkdf2Sync requires 5 arguments')
            password = js_to_py(args[0])
            salt = js_to_py(args[1])
            iterations = int(js_to_py(args[2]))
            keylen = int(js_to_py(args[3]))
            digest = js_to_py(args[4]).lower()
            if digest not in _HASH_ALGORITHMS:
                raise Exception(f"Unsupported digest: {digest}")
            pwd = password.encode('utf-8') if isinstance(password, str) else password
            s = salt.encode('utf-8') if isinstance(salt, str) else salt
            derived = hashlib.pbkdf2_hmac(digest, pwd, s, iterations, dklen=keylen)
            return py_to_js(derived.hex())

        # -- crypto.timingSafeEqual(a, b) -----------------------------------

        def _timing_safe_equal(this_val, args, interp_inner):
            a = js_to_py(args[0]) if args else ''
            b = js_to_py(args[1]) if len(args) > 1 else ''
            a_bytes = a.encode('utf-8') if isinstance(a, str) else a
            b_bytes = b.encode('utf-8') if isinstance(b, str) else b
            if len(a_bytes) != len(b_bytes):
                raise Exception('Input buffers must have the same byte length')
            return JS_TRUE if hmac_mod.compare_digest(a_bytes, b_bytes) else JS_FALSE

        # -- crypto.subtle.digest(algorithm, data) --------------------------

        def _subtle_digest(this_val, args, interp_inner):
            algo = js_to_py(args[0]) if args else ''
            data = js_to_py(args[1]) if len(args) > 1 else ''
            hashlib_name = _SUBTLE_ALGORITHMS.get(algo)
            if not hashlib_name:
                return interp_inner._rejected_promise(
                    py_to_js(f"Unsupported algorithm: {algo}"))
            data_bytes = data.encode('utf-8') if isinstance(data, str) else data
            digest = hashlib.new(hashlib_name, data_bytes).hexdigest()
            return interp_inner._resolved_promise(py_to_js(digest))

        # -- Register on existing crypto object -----------------------------

        crypto.value['createHash'] = interp._make_intrinsic(
            _create_hash, 'crypto.createHash')
        crypto.value['createHmac'] = interp._make_intrinsic(
            _create_hmac, 'crypto.createHmac')
        crypto.value['pbkdf2Sync'] = interp._make_intrinsic(
            _pbkdf2_sync, 'crypto.pbkdf2Sync')
        crypto.value['timingSafeEqual'] = interp._make_intrinsic(
            _timing_safe_equal, 'crypto.timingSafeEqual')

        subtle = JsValue('object', {})
        subtle.value['digest'] = interp._make_intrinsic(
            _subtle_digest, 'crypto.subtle.digest')
        crypto.value['subtle'] = subtle
