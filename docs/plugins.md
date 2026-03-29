# PyJS — Plugin Authoring Guide

PyJS includes a plugin system that lets you extend the JavaScript environment
with custom globals, methods, constructors, and objects — all from Python.
Plugins keep extensions modular and reusable: install only what you need, and
swap or remove them without touching the interpreter core.

---

## Quick Start

```python
from pyjs import Interpreter, PyJSPlugin, PluginContext
from pyjs.core import py_to_js

class HelloPlugin(PyJSPlugin):
    name = "hello"
    version = "1.0.0"

    def setup(self, ctx: PluginContext) -> None:
        ctx.add_global("greet", lambda this, args, interp: py_to_js("Hello, PyJS!"))

interp = Interpreter()
interp.register_plugin(HelloPlugin())
print(interp.run('console.log(greet())'))  # Hello, PyJS!
```

That's it — five lines of plugin code to inject a new global function.

---

## Core Concepts

Every plugin subclasses `PyJSPlugin` and overrides `setup()`.  The interpreter
calls `setup()` with a **`PluginContext`** — a safe facade that exposes
registration helpers without giving raw access to interpreter internals.

```
Interpreter.register_plugin(plugin)
  └─ creates PluginContext(interpreter)
       └─ calls plugin.setup(ctx)
            └─ ctx.add_global(...)
            └─ ctx.add_method(...)
            └─ ctx.add_constructor(...)
            └─ ctx.add_global_object(...)
```

---

## PluginContext API Reference

All callback functions in the plugin API follow the **intrinsic signature**:

```python
def my_fn(this_val: JsValue, args: list[JsValue], interp: Interpreter) -> JsValue:
    ...
```

- `this_val` — the `this` binding (or `UNDEFINED` for plain calls)
- `args` — list of positional arguments, each a `JsValue`
- `interp` — the running `Interpreter` instance

### `add_global(name, value, *, writable=True)`

Add a global variable or function visible to all JS code.

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | `str` | Name of the global (e.g. `"myUtil"`) |
| `value` | callable, `JsValue`, or Python primitive | The value to bind |
| `writable` | `bool` | If `False`, declares as `const` (default `True` → `var`) |

If `value` is a Python callable (but not already a `JsValue`), it is
automatically wrapped as a JS intrinsic function.  Otherwise it is converted
via `py_to_js`.

```python
# Global function
ctx.add_global("double", lambda this, args, interp: py_to_js(args[0].value * 2))

# Global constant
ctx.add_global("VERSION", "2.1.0", writable=False)

# Global number
ctx.add_global("MAX_RETRIES", 5)
```

### `add_global_object(name, methods)`

Add a global object whose properties are intrinsic methods.

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | `str` | Name of the global object |
| `methods` | `dict[str, Callable]` | Mapping of method names → callables |

Returns the created `JsValue` object.

```python
def get_item(this_val, args, interp):
    key = args[0].value if args else None
    return py_to_js(store.get(key))

def set_item(this_val, args, interp):
    store[args[0].value] = args[1].value
    return UNDEFINED

ctx.add_global_object("myStore", {
    "getItem": get_item,
    "setItem": set_item,
})
```

JS usage:

```js
myStore.setItem("name", "Alice");
console.log(myStore.getItem("name")); // Alice
```

### `add_method(type_name, method_name, fn)`

Attach a new method to an existing JS type's dispatch table.

| Parameter | Type | Description |
|-----------|------|-------------|
| `type_name` | `str` | One of `'string'`, `'array'`, `'number'`, `'object'`, `'promise'` |
| `method_name` | `str` | Name of the method (e.g. `"reverse"`) |
| `fn` | `Callable` | Intrinsic-signature handler |

The method name is added to the interpreter's frozenset for that type (e.g.
`STRING_METHODS`) and the handler is stored in `_plugin_methods`.

```python
def string_reverse(this_val, args, interp):
    return py_to_js(this_val.value[::-1])

ctx.add_method("string", "reverse", string_reverse)
```

JS usage:

```js
console.log("hello".reverse()); // "olleh"
```

### `add_constructor(name, fn)`

Register a constructor callable with `new`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | `str` | Constructor name (e.g. `"EventEmitter"`) |
| `fn` | `Callable` | Receives a fresh object as `this_val` |

The function's `extras` dict is tagged with `'construct': True` so the
interpreter knows to allocate a new object for `new Name(...)` calls.

```python
def point_ctor(this_val, args, interp):
    this_val.value["x"] = py_to_js(args[0].value if args else 0)
    this_val.value["y"] = py_to_js(args[1].value if len(args) > 1 else 0)
    return this_val

ctx.add_constructor("Point", point_ctor)
```

JS usage:

```js
const p = new Point(3, 4);
console.log(p.x, p.y); // 3 4
```

### `make_js_value(py_val)`

Convert a Python value to `JsValue`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `py_val` | `Any` | Python value (`int`, `float`, `str`, `bool`, `list`, `dict`, `None`) |

Equivalent to calling `py_to_js()` directly. Useful inside plugin code that
doesn't import `core` directly.

```python
js_arr = ctx.make_js_value([1, 2, 3])       # JsValue("array", [...])
js_obj = ctx.make_js_value({"a": 1})         # JsValue("object", {...})
js_str = ctx.make_js_value("hello")          # JsValue("string", "hello")
```

### `make_error(name, message)`

Create a JS error object (same structure as `new Error()`).

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | `str` | Error constructor name (`"TypeError"`, `"RangeError"`, etc.) |
| `message` | `str` | Human-readable error message |

```python
err = ctx.make_error("TypeError", "expected a number")
# JsValue("object", {"name": ..., "message": ..., "stack": ...})
```

### `make_function(fn, name='?')`

Wrap a Python callable as a JS function value.

| Parameter | Type | Description |
|-----------|------|-------------|
| `fn` | `Callable` | Intrinsic-signature callable |
| `name` | `str` | Function `.name` property (default `'?'`) |

```python
js_fn = ctx.make_function(lambda this, args, interp: py_to_js(42), "getAnswer")
ctx.add_global("getAnswer", js_fn)
```

### `get_interpreter()`

Returns the `Interpreter` instance.  Use sparingly — prefer the
`PluginContext` helpers for most tasks.

```python
interp = ctx.get_interpreter()
# Useful for calling interp._call_js(), accessing interp.genv, etc.
```

---

## Lifecycle Hooks

### `setup(ctx: PluginContext)`

Called once when `Interpreter.register_plugin()` is invoked.  This is where
you register everything: globals, methods, constructors.

### `teardown(ctx: PluginContext)`

Called when the interpreter is being cleaned up.  Override this to close file
handles, flush buffers, persist data, or release other resources.

```python
def teardown(self, ctx):
    self._save_to_disk()
```

### `on_error(error: Exception, ctx: PluginContext)`

Called when an error occurs in code registered by your plugin.  Useful for
logging, telemetry, or error recovery.

```python
def on_error(self, error, ctx):
    print(f"[{self.name}] Error: {error}")
```

---

## Example: Custom String Method Plugin

A plugin that adds `.reverse()` and `.capitalize()` to JavaScript strings:

```python
from pyjs import Interpreter, PyJSPlugin, PluginContext
from pyjs.core import py_to_js
from pyjs.values import UNDEFINED

class StringExtPlugin(PyJSPlugin):
    name = "string-ext"
    version = "1.0.0"

    def setup(self, ctx: PluginContext) -> None:
        def reverse(this_val, args, interp):
            return py_to_js(this_val.value[::-1])

        def capitalize(this_val, args, interp):
            s = this_val.value
            return py_to_js(s[0].upper() + s[1:] if s else "")

        ctx.add_method("string", "reverse", reverse)
        ctx.add_method("string", "capitalize", capitalize)

# Usage
interp = Interpreter()
interp.register_plugin(StringExtPlugin())
print(interp.run('''
    console.log("hello".reverse());      // "olleh"
    console.log("world".capitalize());   // "World"
'''))
```

---

## Example: Timer/Counter Plugin

A plugin that adds a global `counter` object with `increment()`, `reset()`,
and `value` getter:

```python
from pyjs import Interpreter, PyJSPlugin, PluginContext
from pyjs.core import py_to_js
from pyjs.values import UNDEFINED

class CounterPlugin(PyJSPlugin):
    name = "counter"
    version = "1.0.0"

    def setup(self, ctx: PluginContext) -> None:
        state = {"count": 0}

        def increment(this_val, args, interp):
            step = int(args[0].value) if args and args[0].type == "number" else 1
            state["count"] += step
            return py_to_js(state["count"])

        def reset(this_val, args, interp):
            state["count"] = 0
            return UNDEFINED

        def get_value(this_val, args, interp):
            return py_to_js(state["count"])

        ctx.add_global_object("counter", {
            "increment": increment,
            "reset":     reset,
            "value":     get_value,
        })

# Usage
interp = Interpreter()
interp.register_plugin(CounterPlugin())
print(interp.run('''
    counter.increment();
    counter.increment();
    counter.increment(5);
    console.log(counter.value());  // 7
    counter.reset();
    console.log(counter.value());  // 0
'''))
```

---

## Built-in Plugins

PyJS ships with five first-party plugins in `pyjs/plugins/`.  Import them
from `pyjs.plugins`.

### StoragePlugin — `localStorage` / `sessionStorage`

Browser-compatible key-value storage.  `localStorage` can optionally persist
to a JSON file; `sessionStorage` is always in-memory.

```python
from pyjs import Interpreter
from pyjs.plugins import StoragePlugin

interp = Interpreter()
interp.register_plugin(StoragePlugin(persist_path="./storage.json"))
interp.run('''
    localStorage.setItem("user", "Alice");
    console.log(localStorage.getItem("user"));   // Alice
    console.log(localStorage.length());           // 1

    sessionStorage.setItem("token", "abc123");
    console.log(sessionStorage.getItem("token")); // abc123

    localStorage.removeItem("user");
    localStorage.clear();
''')
```

| Constructor Parameter | Type | Default | Description |
|----|------|---------|-------------|
| `persist_path` | `str \| None` | `None` | Path to JSON file for localStorage persistence |

**Methods on each storage object:** `getItem(key)`, `setItem(key, value)`,
`removeItem(key)`, `clear()`, `key(index)`, `length()`.

### FetchPlugin — `fetch()`

HTTP client using Python's `urllib` (stdlib only, no external dependencies).
Returns a `Promise` that resolves to a Response-like object.

```python
from pyjs import Interpreter
from pyjs.plugins import FetchPlugin

interp = Interpreter()
interp.register_plugin(FetchPlugin(timeout=10))
interp.run('''
    const resp = await fetch("https://api.example.com/data");
    console.log(resp.status);       // 200
    console.log(resp.ok);           // true
    const body = await resp.text();
    const json = await resp.json();
''')
```

| Constructor Parameter | Type | Default | Description |
|----|------|---------|-------------|
| `timeout` | `int \| None` | `30` | Request timeout in seconds |

**Response object properties:** `status`, `statusText`, `ok`, `url`, `type`,
`headers` (with `get(name)` method).

**Response methods:** `text()` → Promise\<string\>, `json()` → Promise\<object\>.

### EventEmitterPlugin — `new EventEmitter()`

Node.js-compatible event emitter constructor.

```python
from pyjs import Interpreter
from pyjs.plugins import EventEmitterPlugin

interp = Interpreter()
interp.register_plugin(EventEmitterPlugin())
interp.run('''
    const emitter = new EventEmitter();

    emitter.on("data", (msg) => console.log("got:", msg));
    emitter.once("end", () => console.log("done"));

    emitter.emit("data", "hello");  // got: hello
    emitter.emit("data", "world");  // got: world
    emitter.emit("end");            // done
    emitter.emit("end");            // (nothing — once listener removed)

    console.log(emitter.listenerCount("data")); // 1
    emitter.removeAllListeners("data");
    console.log(emitter.listenerCount("data")); // 0
''')
```

**Instance methods:** `on(event, fn)`, `addListener(event, fn)` (alias),
`once(event, fn)`, `off(event, fn)`, `removeListener(event, fn)` (alias),
`emit(event, ...args)`, `removeAllListeners([event])`,
`listenerCount(event)`.

All methods except `emit` and `listenerCount` return `this` for chaining.

### FileSystemPlugin — `fs.*`

Sandboxed, synchronous file system access modeled after Node.js `fs`.

```python
from pyjs import Interpreter
from pyjs.plugins import FileSystemPlugin

interp = Interpreter()
interp.register_plugin(FileSystemPlugin(root="./sandbox", allow_write=True))
interp.run('''
    fs.writeFileSync("hello.txt", "Hello, world!");
    console.log(fs.readFileSync("hello.txt"));          // Hello, world!
    console.log(fs.existsSync("hello.txt"));             // true

    fs.mkdirSync("subdir", { recursive: true });
    console.log(fs.readdirSync("."));                    // ["hello.txt", "subdir"]

    const stat = fs.statSync("hello.txt");
    console.log(stat.size);                              // 13
    console.log(stat.isFile());                          // true
    console.log(stat.isDirectory());                     // false

    fs.unlinkSync("hello.txt");
''')
```

| Constructor Parameter | Type | Default | Description |
|----|------|---------|-------------|
| `root` | `str` | `"."` | Root directory — all paths are resolved relative to this |
| `allow_write` | `bool` | `True` | Set `False` to make the filesystem read-only |

**Path traversal protection:** Any path that resolves outside `root` raises a
JS error.  This prevents `../../etc/passwd` escapes.

**Methods:** `readFileSync(path[, encoding])`, `writeFileSync(path, data[, encoding])`,
`existsSync(path)`, `mkdirSync(path[, options])`,
`readdirSync(path)`, `statSync(path)`, `unlinkSync(path)`.

**Stat object:** `size`, `mtime`, `isFile()`, `isDirectory()`.

### ConsoleExtPlugin — Extended `console` Methods

Adds `console.table()`, `console.assert()`, `console.trace()`, and
`console.dir()` to the built-in console.

```python
from pyjs import Interpreter
from pyjs.plugins import ConsoleExtPlugin

interp = Interpreter()
interp.register_plugin(ConsoleExtPlugin())
interp.run('''
    console.table([
        { name: "Alice", age: 30 },
        { name: "Bob",   age: 25 },
    ]);
    // Prints an ASCII table

    console.assert(1 === 1);            // (no output)
    console.assert(1 === 2, "oh no!");  // Assertion failed: oh no!

    console.trace("checkpoint");        // Trace: checkpoint

    console.dir({ a: 1, b: [2, 3] });  // Pretty-printed object
''')
```

No constructor parameters.

---

## Best Practices

1. **Use `PluginContext` helpers, not raw interpreter access.**
   `ctx.add_global()` handles intrinsic wrapping, `globalThis` sync, and
   keyword declaration for you.

2. **Always return `JsValue` from callbacks.**
   Return `UNDEFINED` when there's no meaningful return value — never return
   Python `None` from an intrinsic.

3. **Name your plugin.**  Set `name` and `version` class attributes.  They
   appear in `repr()` and make debugging easier.

4. **Keep plugins focused.**  One plugin, one concern.  If you're adding both
   a global object and a constructor, that's fine — but don't stuff unrelated
   features into the same plugin.

5. **Clean up in `teardown()`.**  If your plugin opens files, network
   connections, or background threads, release them in `teardown()`.

6. **Validate arguments defensively.**  JS callers can pass any number of
   arguments of any type.  Check `len(args)` and `args[i].type` before
   accessing `.value`.

7. **Use `make_error()` for JS-visible errors.**  Don't raise raw Python
   exceptions — create a proper JS error and raise `_JSError(err)` to give
   callers a catchable `try/catch` experience.

8. **Prefer `py_to_js` for return values.**  It handles `None → null`,
   `bool → boolean`, `int/float → number`, `str → string`, `list → array`,
   `dict → object` automatically.

9. **Test your plugin in isolation.**  Create an `Interpreter`, register only
   your plugin, run JS source, and assert on `interp.run()` output:

   ```python
   def test_my_plugin(self):
       interp = Interpreter()
       interp.register_plugin(MyPlugin())
       result = interp.run('console.log(myGlobal())')
       self.assertEqual(result, "expected output")
   ```

10. **Document the JS API your plugin exposes.**  Users of your plugin will be
    writing JavaScript, not Python — make sure they know what globals, methods,
    and constructors are available.
