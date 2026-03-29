# Copilot Instructions — PyJS

PyJS is a pure-Python ECMAScript interpreter (~91% ES2015–ES2025 coverage). No external dependencies — stdlib only.

## Commands

```bash
# Run all 103 tests
python3 -m unittest tests.test_pyjs.PyJSTestCase

# Run a single test
python3 -m unittest tests.test_pyjs.PyJSTestCase.test_parse_source_returns_program

# Execute a JS file
python3 main.py path/to/file.js

# Evaluate inline JS
python3 main.py -e 'console.log("hello")'

# Dump AST as JSON / dump tokens
python3 main.py --ast -e 'let x = 1'
python3 main.py --tokens -e 'let x = 1'

# Interactive REPL
python3 main.py --repl
```

No linter, formatter, or build step is configured.

## Architecture

**Pipeline:** `Lexer` → `Parser` → AST (plain dicts) → `Interpreter._exec/_eval` (tree-walk)

| File | Role |
|---|---|
| `pyjs/lexer.py` | Tokenizer → list of `Token` dataclass instances |
| `pyjs/parser.py` | Recursive-descent parser; AST nodes built via `N.*` static constructors |
| `pyjs/runtime.py` | Tree-walking interpreter, all JS built-ins, event loop (~5k lines) |
| `pyjs/core.py` | `JsValue` wrapper, `py_to_js`/`js_to_py` converters, `JSTypeError` |
| `pyjs/modules.py` | `ModuleLoader`: path resolution, caching, cycle detection |
| `pyjs/__init__.py` | Public API: `evaluate()`, `evaluate_file()`, `parse_source()`, `tokenize_source()`, `repl()` |
| `tests/test_pyjs.py` | All tests in one `unittest.TestCase` class (`PyJSTestCase`) |

## Key Conventions

### AST nodes are plain dicts with a `"type"` key

Nodes are created via the `N` namespace in `parser.py`. Each static method returns `{"type": "...", ...}`. Example:

```python
N.BinExpr("+", N.Lit(1, "number"), N.Lit(2, "number"))
# → {"type": "BinaryExpression", "operator": "+", "left": {...}, "right": {...}}
```

### All JS values are wrapped in `JsValue(type, value)`

`JsValue` has three slots: `type` (str), `value` (any), `extras` (dict or None for metadata like prototype, descriptors, private fields). Global singletons: `UNDEFINED`, `JS_NULL`, `JS_TRUE`, `JS_FALSE`.

Type strings: `"undefined"`, `"null"`, `"boolean"`, `"number"`, `"string"`, `"array"`, `"object"`, `"function"`, `"intrinsic"`, `"bigint"`, `"symbol"`, `"regexp"`, `"promise"`.

### Control flow uses Python exceptions

`_JSBreak`, `_JSContinue`, `_JSReturn`, `_JSError` — defined at the bottom of `runtime.py`. These are internal, not meant for outside use.

### The interpreter dispatches on `node["type"]`

`_exec(node, env)` handles statements (returns `None`). `_eval(node, env)` handles expressions (returns a `JsValue`). Both are large if/elif chains keyed on `node["type"]`.

### Built-in method dispatch uses type-switch on `JsValue.type`

The `Interpreter` class has frozen sets (`ARRAY_METHODS`, `STRING_METHODS`, `NUMBER_METHODS`, `PROMISE_METHODS`) and dispatches method calls based on value type, not prototype chains. When adding a new built-in method, add it to the relevant frozenset and implement the handler in the corresponding dispatch block.

### Native functions are `"intrinsic"` JsValues

Created with `_make_intrinsic(fn, name)`. The wrapped function receives `(this_val, args, interp)`. All Python exceptions inside are auto-converted to `_JSError`.

### Environments are a linked chain

`Environment` objects have a `parent` pointer. `declare(name, value, keyword)` adds bindings; `get`/`set` walk the chain. Bindings are `(keyword, JsValue)` tuples, enforcing `const` immutability.

### Generators use real threads

`JsGenerator` runs the generator body on a daemon thread, communicating via `queue.Queue` pairs (`_to_gen`, `_from_gen`).

## Adding a New ES Feature

1. **Lexer** — Add new token types if needed (new keywords go in `Lexer.KEYWORDS`).
2. **Parser** — Add an `N.*` constructor for the new AST node, then add parsing logic.
3. **Runtime** — Handle the new `node["type"]` in `_exec` or `_eval`. For built-in methods, add to the relevant frozenset and dispatch block.
4. **Tests** — Add a test in `PyJSTestCase` that runs JS source via `Interpreter().run()` and asserts on output lines or return values.
5. **ECMASCRIPT_STATUS.md** — Update the completeness report and phase summary.

## Test Patterns

Tests create an `Interpreter()`, run JS source, and assert on output:

```python
def test_example(self):
    source = 'console.log("hello")'
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        result = Interpreter().run(source)
    self.assertEqual(result, 'hello')
```

`Interpreter.run()` returns a string of all new `console.log` output joined by newlines. Tests use `result.splitlines()` to check ordering.
