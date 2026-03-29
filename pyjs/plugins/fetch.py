"""HTTP fetch API plugin for PyJS using urllib (stdlib only)."""
from __future__ import annotations
import json as _json
import urllib.request
import urllib.error
from typing import Optional

from ..plugin import PyJSPlugin, PluginContext
from ..values import JsValue, UNDEFINED, JS_NULL
from ..core import py_to_js


class FetchPlugin(PyJSPlugin):
    name = "fetch"
    version = "1.0.0"

    def __init__(self, timeout: Optional[int] = 30):
        self._timeout = timeout

    def setup(self, ctx: PluginContext) -> None:
        plugin = self

        def fetch_fn(this_val, args, interp):
            if not args:
                raise TypeError("fetch requires at least 1 argument")

            url = interp._to_str(args[0])
            options = args[1] if len(args) > 1 and args[1].type == 'object' else None

            method = 'GET'
            headers = {}
            body = None

            if options and isinstance(options.value, dict):
                m = options.value.get('method')
                if m and isinstance(m, JsValue) and m.type == 'string':
                    method = m.value.upper()

                h = options.value.get('headers')
                if h and isinstance(h, JsValue) and h.type == 'object':
                    for k, v in h.value.items():
                        headers[k] = interp._to_str(v)

                b = options.value.get('body')
                if b and isinstance(b, JsValue) and b.type != 'undefined' and b.type != 'null':
                    body = interp._to_str(b).encode('utf-8')

            try:
                req = urllib.request.Request(url, data=body, headers=headers, method=method)
                resp = urllib.request.urlopen(req, timeout=plugin._timeout)
                status = resp.status
                status_text = resp.reason or ''
                resp_headers = dict(resp.getheaders())
                resp_body = resp.read().decode('utf-8', errors='replace')
                resp_url = resp.url or url
            except urllib.error.HTTPError as e:
                status = e.code
                status_text = e.reason or ''
                resp_headers = dict(e.headers.items()) if e.headers else {}
                try:
                    resp_body = e.read().decode('utf-8', errors='replace')
                except Exception:
                    resp_body = ''
                resp_url = url
            except Exception as e:
                return interp._rejected_promise(
                    interp._make_js_error('TypeError', f'fetch failed: {e}')
                )

            response_obj = _build_response(interp, status, status_text, resp_headers, resp_body, resp_url)
            return interp._resolved_promise(response_obj)

        ctx.add_global('fetch', fetch_fn)


def _build_response(interp, status, status_text, resp_headers, resp_body, resp_url):
    """Build a JS Response-like object."""
    ok = 200 <= status <= 299

    headers_dict = resp_headers

    def headers_get(this_val, args, interp_inner):
        name = interp_inner._to_str(args[0]) if args else ''
        for k, v in headers_dict.items():
            if k.lower() == name.lower():
                return py_to_js(v)
        return JS_NULL

    headers_obj = JsValue('object', {})
    headers_obj.value['get'] = interp._make_intrinsic(headers_get, 'Headers.get')

    def text_fn(this_val, args, interp_inner):
        return interp_inner._resolved_promise(py_to_js(resp_body))

    def json_fn(this_val, args, interp_inner):
        try:
            parsed = _json.loads(resp_body)
            return interp_inner._resolved_promise(py_to_js(parsed))
        except _json.JSONDecodeError as e:
            return interp_inner._rejected_promise(
                interp_inner._make_js_error('SyntaxError', str(e))
            )

    response = JsValue('object', {})
    response.value['status'] = py_to_js(status)
    response.value['statusText'] = py_to_js(status_text)
    response.value['ok'] = py_to_js(ok)
    response.value['headers'] = headers_obj
    response.value['url'] = py_to_js(resp_url)
    response.value['type'] = py_to_js('basic')
    response.value['text'] = interp._make_intrinsic(text_fn, 'Response.text')
    response.value['json'] = interp._make_intrinsic(json_fn, 'Response.json')

    return response
