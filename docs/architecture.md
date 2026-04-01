# PyJS — Architecture Overview

PyJS is a **pure-Python ECMAScript interpreter** covering ~96% of the
ES2015–ES2025 specification.  It has zero external dependencies — everything
runs on the Python standard library.

---

## Pipeline

Source code flows through three stages:

```
Source string
  │
  ▼
┌─────────┐     List[Token]     ┌──────────┐     AST dict     ┌─────────────┐
│  Lexer   │ ──────────────────▸ │  Parser  │ ───────────────▸ │ Interpreter │
│ lexer.py │                     │ parser.py│                  │ runtime.py  │
└─────────┘                     └──────────┘                  └─────────────┘
                                                                  │
                                                  _exec (statements)
                                                  _eval (expressions)
                                                                  │
                                                                  ▼
                                                            JsValue result
```

1. **Lexer** — tokenizes source into a flat list of `Token` dataclass
   instances (keywords, identifiers, literals, operators, punctuation).
2. **Parser** — recursive-descent parser that consumes tokens and builds an
   AST made of plain Python dicts, each with a `"type"` key.
3. **Interpreter** — tree-walking evaluator that dispatches on
   `node["type"]`, executing statements via `_exec()` and evaluating
   expressions via `_eval()`.

---

## Module Dependency Graph

```
pyjs/__init__.py          (public API — re-exports everything)
  ├── lexer.py
  ├── parser.py           → lexer.py
  ├── values.py           (leaf — no internal deps)
  ├── environment.py      → values.py, core.py
  ├── runtime.py          → core.py, lexer.py, parser.py, values.py,
  │                          environment.py, exceptions.py, generators.py,
  │                          modules.py, plugin.py, trace.py,
  │                          builtins_core.py, builtins_object.py,
  │                          builtins_advanced.py, builtins_promise.py,
  │                          builtins_typed.py
  ├── core.py             → values.py (TYPE_CHECKING)
  ├── exceptions.py       → values.py
  ├── generators.py       (generator/async-generator threading)
  ├── modules.py          (module loader — creates Interpreter instances)
  ├── plugin.py           → values.py, core.py, runtime.py (TYPE_CHECKING)
  ├── trace.py            (logging configuration)
  ├── cli.py              (CLI entry point, argparse)
  ├── completer.py        (REPL tab completion)
  ├── colors.py           (ANSI color utilities)
  └── inspect_val.py      (JS value pretty-printing)

pyjs/plugins/
  ├── storage.py          → plugin.py, values.py, core.py
  ├── fetch.py            → plugin.py, values.py, core.py
  ├── events.py           → plugin.py, values.py, core.py
  ├── fs.py               → plugin.py, values.py, core.py, exceptions.py
  ├── console_ext.py      → plugin.py, values.py, core.py
  ├── process.py          → plugin.py, values.py, core.py
  ├── path_plugin.py      → plugin.py, values.py, core.py
  ├── assert_plugin.py    → plugin.py, values.py, core.py
  ├── util_plugin.py      → plugin.py, values.py, core.py
  ├── crypto_plugin.py    → plugin.py, values.py, core.py
  └── child_process.py    → plugin.py, values.py, core.py
```

---

## Key Files

| File | Lines | Role |
|------|------:|------|
| `pyjs/runtime.py` | 4 449 | Tree-walking interpreter, method dispatch, event loop |
| `pyjs/parser.py` | 1 293 | Recursive-descent parser, AST node constructors (`N.*`) |
| `pyjs/builtins_advanced.py` | 1 098 | Array, String, Math, JSON, Date, RegExp, Symbol, Map, Set built-ins |
| `pyjs/completer.py` | 421 | REPL tab-completion engine |
| `pyjs/__init__.py` | 450 | Public API: `evaluate()`, `evaluate_file()`, `parse_source()`, `repl()` |
| `pyjs/builtins_object.py` | 724 | Object.*, console.*, global utility functions |
| `pyjs/builtins_typed.py` | 567 | TypedArray constructors, ArrayBuffer, DataView |
| `pyjs/cli.py` | 439 | CLI entry point (`pyjs` command) |
| `pyjs/lexer.py` | 383 | Tokenizer — keywords, numbers, strings, regex, templates, `\uXXXX` |
| `pyjs/builtins_core.py` | 357 | parseInt, parseFloat, isNaN, URI encoding, Math.sumPrecise |
| `pyjs/colors.py` | 256 | ANSI color utilities for REPL output |
| `pyjs/inspect_val.py` | 249 | JS value pretty-printing for REPL |
| `pyjs/builtins_promise.py` | 188 | Promise, Error constructors, eval, structuredClone |
| `pyjs/trace.py` | 198 | Logging/tracing configuration |
| `pyjs/plugin.py` | 170 | PluginContext + PyJSPlugin base class |
| `pyjs/generators.py` | 153 | JsGenerator / JsAsyncGenerator (thread-based) |
| `pyjs/core.py` | 126 | `JsValue`, `py_to_js`/`js_to_py`, `JSTypeError` |
| `pyjs/environment.py` | 94 | Lexical scope chain with TDZ and `_using_stack` |
| `pyjs/values.py` | 59 | JsValue class, JsProxy, singletons, well-known symbols |
| `pyjs/modules.py` | 56 | ModuleLoader: path resolution, caching, cycle detection |
| `pyjs/exceptions.py` | 27 | `_JSBreak`, `_JSContinue`, `_JSReturn`, `_JSError` |
| `tests/test_pyjs.py` | 2 859 | 223 tests covering all phases |

**Total:** ~13 400 source lines (including tests and plugins).

---

## AST Nodes

AST nodes are **plain Python dicts** with a `"type"` key.  They are created
via the `N` namespace in `parser.py`, which contains static constructor
methods.

```python
# parser.py — examples of N.* constructors
N.Lit(42, "number")
# → {"type": "Literal", "value": 42, "kind": "number"}

N.BinExpr("+", N.Lit(1, "number"), N.Lit(2, "number"))
# → {"type": "BinaryExpression", "operator": "+", "left": {...}, "right": {...}}

N.Id("x")
# → {"type": "Identifier", "name": "x"}

N.VarDecl("let", [N.VarDeclarator(N.Id("x"), N.Lit(1, "number"))])
# → {"type": "VariableDeclaration", "kind": "let", "declarations": [...]}
```

Key node types:

| Category | Node Types |
|----------|-----------|
| **Program** | `Program` |
| **Statements** | `BlockStatement`, `VariableDeclaration`, `FunctionDeclaration`, `ClassDeclaration`, `IfStatement`, `WhileStatement`, `DoWhileStatement`, `ForStatement`, `ForInStatement`, `ForOfStatement`, `SwitchStatement`, `TryStatement`, `BreakStatement`, `ContinueStatement`, `ThrowStatement`, `ReturnStatement`, `ExpressionStatement` |
| **Expressions** | `Literal`, `Identifier`, `BinaryExpression`, `LogicalExpression`, `UnaryExpression`, `UpdateExpression`, `AssignmentExpression`, `ConditionalExpression`, `MemberExpression`, `CallExpression`, `NewExpression`, `ArrayExpression`, `ObjectExpression`, `FunctionExpression`, `ArrowFunctionExpression`, `TemplateLiteral`, `SpreadElement`, `AwaitExpression`, `YieldExpression`, `ThisExpression`, `SequenceExpression` |
| **Class** | `ClassBody`, `ClassField`, `StaticBlock` |
| **Modules** | `ImportDeclaration`, `ExportNamedDeclaration`, `ExportDefaultDeclaration` |
| **ES2024+** | `UsingDeclaration` (`using`/`await using`), `Decorator` (`@expr` on classes/methods/fields) |

---

## JsValue — The Value Wrapper

Every JavaScript value is represented as a `JsValue` instance with three
slots:

```python
class JsValue:
    __slots__ = ('type', 'value', 'extras')
```

| Slot | Purpose |
|------|---------|
| `type` | String tag: `"undefined"`, `"null"`, `"boolean"`, `"number"`, `"string"`, `"bigint"`, `"symbol"`, `"array"`, `"object"`, `"function"`, `"intrinsic"`, `"class"`, `"promise"`, `"regexp"`, `"proxy"` |
| `value` | The payload: Python `float` for numbers, `str` for strings, `list` for arrays, `dict` for objects/functions, etc. |
| `extras` | Optional `dict` for metadata: prototype link, property descriptors, private fields, `'construct': True` flag, generator state, etc. |

### Global Singletons

```python
UNDEFINED = JsValue("undefined", None)
JS_NULL   = JsValue("null", None)
JS_TRUE   = JsValue("boolean", True)
JS_FALSE  = JsValue("boolean", False)
```

These are reused everywhere — never create new instances for `undefined`,
`null`, `true`, or `false`.

### Type Conversion

```python
from pyjs.core import py_to_js, js_to_py

py_to_js(42)        # → JsValue("number", 42.0)
py_to_js("hello")   # → JsValue("string", "hello")
py_to_js([1, 2])    # → JsValue("array", [JsValue("number", 1.0), ...])
py_to_js({"a": 1})  # → JsValue("object", {"a": JsValue("number", 1.0)})
py_to_js(None)       # → JsValue("null", None)
py_to_js(True)       # → JsValue("boolean", True)

js_to_py(JsValue("number", 42.0))  # → 42.0
js_to_py(UNDEFINED)                  # → None
```

### Well-Known Symbols

Symbols are represented as integer IDs stored as dict keys on objects:

```python
SYMBOL_ITERATOR           = 1
SYMBOL_TO_PRIMITIVE       = 2
SYMBOL_HAS_INSTANCE       = 3
SYMBOL_TO_STRING_TAG      = 4
SYMBOL_ASYNC_ITERATOR     = 5
SYMBOL_SPECIES            = 6
SYMBOL_MATCH              = 7
SYMBOL_REPLACE            = 8
SYMBOL_SPLIT              = 9
SYMBOL_SEARCH             = 10
SYMBOL_IS_CONCAT_SPREADABLE = 11
SYMBOL_DISPOSE            = 12   # ES2024 — used by `using` declarations
SYMBOL_ASYNC_DISPOSE      = 13   # ES2024 — used by `await using` declarations
```

---

## Environment Chain

Lexical scoping is implemented via a linked list of `Environment` objects.

```python
class Environment:
    __slots__ = ('parent', 'bindings', '_this', '_fn_args',
                 '_is_arrow', '_is_fn_env', '_generator', '_fn_val', '_strict')
```

Each environment stores:

- **`bindings`** — `dict[str, tuple[str, JsValue]]` mapping variable names to
  `(keyword, value)` tuples.  The keyword (`'var'`, `'let'`, `'const'`)
  determines mutability and scoping behavior.
- **`parent`** — pointer to the enclosing scope (or `None` for the global
  scope).
- **`_this`** — the `this` binding for this scope.

### Variable Resolution

```
┌──────────────┐
│ Block scope  │   let x = 1
│ (for-body)   │
└──────┬───────┘
       │ parent
┌──────▼───────┐
│ Function env │   var y = 2;  this = obj
│ (_is_fn_env) │
└──────┬───────┘
       │ parent
┌──────▼───────┐
│ Global env   │   undefined, NaN, Infinity, console, ...
│ (genv)       │
└──────────────┘
```

- **`get(name)`** walks the chain upward until the binding is found.
- **`set(name, value)`** finds the binding and updates it (raises on `const`
  reassignment).
- **`declare(name, value, keyword)`** adds a binding to the current scope.
  `var` declarations hoist to the nearest function/program scope.
- **`declare_tdz(name, keyword)`** creates an uninitialized binding for
  `let`/`const` — accessing it before initialization raises a
  `ReferenceError` (Temporal Dead Zone).

### Arrow Functions

Arrow functions set `_is_arrow = True` on their environment.  When the
interpreter resolves `this`, it skips arrow environments and walks up to the
nearest non-arrow function scope — giving arrows their characteristic lexical
`this` binding.

---

## Control Flow via Exceptions

JavaScript's `break`, `continue`, `return`, and `throw` are implemented as
Python exceptions that unwind the call stack:

```python
# exceptions.py
class _JSBreak(Exception):
    def __init__(self, label=None): self.label = label

class _JSContinue(Exception):
    def __init__(self, label=None): self.label = label

class _JSReturn(Exception):
    def __init__(self, value): self.value = value

class _JSError(Exception):
    def __init__(self, value: JsValue): self.value = value
```

| JS Statement | Python Exception | Caught By |
|---|---|---|
| `break` / `break label` | `_JSBreak(label)` | Loop / switch `_exec` handlers |
| `continue` / `continue label` | `_JSContinue(label)` | Loop `_exec` handlers |
| `return value` | `_JSReturn(value)` | Function call in `_call_fn` |
| `throw value` | `_JSError(JsValue)` | `TryStatement` handler in `_exec` |

This approach is elegant: a `return` inside deeply nested loops and
conditionals automatically unwinds to the function boundary without any
explicit loop over statements.

---

## Built-in Registration

Built-in functions and objects are registered in the global environment during
`Interpreter.__init__()`.  The work is split across five modules:

```python
# runtime.py — inside Interpreter.__init__()
register_core_builtins(self)       # parseInt, parseFloat, isNaN, URI functions
register_object_builtins(self)     # Object.*, console.*, globalThis setup
register_advanced_builtins(self)   # Array.*, String.*, Math, JSON, Date, RegExp, etc.
register_promise_builtins(self)    # Promise constructor, all/race/allSettled/any
register_typed_builtins(self)      # ArrayBuffer, TypedArray constructors, DataView
```

| Module | Lines | What It Registers |
|--------|------:|-------------------|
| `builtins_core.py` | 357 | `parseInt`, `parseFloat`, `isNaN`, `isFinite`, `encodeURI`, `decodeURI`, `encodeURIComponent`, `decodeURIComponent`, `atob`, `btoa`, `structuredClone`, `queueMicrotask`, `Math.sumPrecise` |
| `builtins_object.py` | 724 | `Object.*` (keys, values, entries, assign, create, freeze, seal, defineProperty, …), `console.*` (log, warn, error, time, group, …), `globalThis` sync, `RegExp.escape` |
| `builtins_advanced.py` | 1 098 | `Array.from/of/isArray`, `String.fromCharCode/fromCodePoint/raw`, `Math.*`, `JSON.*`, `Date`, `RegExp`, `Symbol` (incl. `dispose`/`asyncDispose`), `Map`, `Set`, `WeakMap`, `WeakSet`, `WeakRef`, `FinalizationRegistry`, `Intl.*`, `Iterator.from`, async iterator helpers |
| `builtins_promise.py` | 188 | `Promise` constructor, `.resolve()`, `.reject()`, `.all()`, `.race()`, `.allSettled()`, `.any()`, `.withResolvers()`, `.try()`; all `Error` subclasses; `Error.isError()`; `eval()` (throws EvalError) |
| `builtins_typed.py` | 567 | `ArrayBuffer`, `DataView`, all 11 TypedArray constructors with full method sets |

### Method Dispatch

Built-in method calls on values (e.g. `"hello".toUpperCase()`) are dispatched
via type-switch, not prototype chains.  The interpreter maintains frozen sets
of known method names per type:

```python
ARRAY_METHODS = frozenset({
    "push", "pop", "shift", "unshift", "indexOf", "includes", "join",
    "slice", "splice", "concat", "reverse", "sort", "forEach", "map",
    "filter", "reduce", "find", "flat", "flatMap", "every", "some", ...
})

STRING_METHODS = frozenset({
    "charAt", "charCodeAt", "indexOf", "includes", "slice", "substring",
    "toLowerCase", "toUpperCase", "trim", "split", "replace", ...
})
```

When a method call is encountered, the interpreter checks whether the method
name is in the relevant frozenset for the value's type, then dispatches to
the corresponding handler code.

Plugins can extend these sets via `PluginContext.add_method()`.

---

## Event Loop

PyJS implements a single-threaded event loop that processes **microtasks**
(promises, `queueMicrotask`) and **macrotasks** (timers) in the correct
priority order.

### Architecture

```
┌────────────────────────────────────┐
│          _run_event_loop()         │
│                                    │
│  while microtasks or timers:       │
│    1. Drain ALL microtasks first   │ ◀── promise .then() callbacks
│    2. Execute ONE timer task       │ ◀── setTimeout / setInterval
│    3. Repeat                       │
│                                    │
│  Safety: EVENT_LOOP_LIMIT = 10000  │
└────────────────────────────────────┘
```

### Microtask Queue

- Backed by a `collections.deque` (`_microtasks`).
- Promise `.then()` / `.catch()` / `.finally()` callbacks and
  `queueMicrotask()` calls push to this queue.
- **All microtasks are drained before any timer fires** — matching browser
  behavior.

### Timer Heap

- Timers are stored in a min-heap (`_timers`) keyed by due time.
- `setTimeout(fn, ms)` creates a one-shot timer.
- `setInterval(fn, ms)` creates a repeating timer that re-enqueues itself.
- `clearTimeout(id)` / `clearInterval(id)` remove timers from the active set.

### Promise Resolution

Promises follow the A+ spec pattern:

1. `_new_promise()` creates a pending promise (state `'pending'`).
2. Resolving/rejecting enqueues `.then()` callbacks as microtasks.
3. `_chain_promise()` wires up handler chains between promises.
4. Thenable assimilation: if a `.then()` handler returns a thenable, its
   resolution is automatically chained.

### Safety Limits

```python
EVENT_LOOP_LIMIT = 10_000   # max event loop iterations
MAX_CALL_DEPTH   = 200      # recursion depth
MAX_EXEC_STEPS   = 10_000_000  # total execution steps
```

---

## Plugin System

PyJS supports an extensible plugin system.  Plugins subclass `PyJSPlugin` and
use `PluginContext` to register globals, methods, constructors, and objects.

```python
interp = Interpreter()
interp.register_plugin(MyPlugin())
```

Five first-party plugins ship with PyJS:

| Plugin | Global(s) Added |
|--------|----------------|
| `StoragePlugin` | `localStorage`, `sessionStorage` |
| `FetchPlugin` | `fetch()` |
| `EventEmitterPlugin` | `EventEmitter` constructor |
| `FileSystemPlugin` | `fs.*` methods |
| `ConsoleExtPlugin` | `console.table/assert/trace/dir` |
| `ProcessPlugin` | `process` (`env`, `argv`, `cwd()`, `exit()`, `platform`) |
| `PathPlugin` | `path` (`join`, `resolve`, `dirname`, `basename`, `extname`, `parse`, `isAbsolute`, `normalize`) |
| `AssertPlugin` | `assert` (`ok`, `equal`, `strictEqual`, `deepEqual`, `throws`, `doesNotThrow`) |
| `UtilPlugin` | `util` (`format`, `inspect`, `isDeepStrictEqual`, `types.*`) |
| `CryptoSubtlePlugin` | `crypto` (`randomBytes`, `createHash`, `createHmac`, `timingSafeEqual`) |
| `ChildProcessPlugin` | `childProcess` (`execSync`, `spawnSync`, `exec`) |

For full details, see **[plugins.md](plugins.md)**.

---

## Adding a New ES Feature

### 1. Lexer (`lexer.py`)

If the feature introduces new syntax tokens (keywords, operators), add them
to the lexer:

- New keywords → add to `Lexer.KEYWORDS` frozenset
- New operators → add a case in the main `tokenize()` switch

### 2. Parser (`parser.py`)

- Add an `N.*` static constructor for the new AST node type:
  ```python
  @staticmethod
  def MyNewNode(param1, param2):
      return {"type": "MyNewNode", "param1": param1, "param2": param2}
  ```
- Add parsing logic in the appropriate precedence level or statement parser

### 3. Runtime (`runtime.py`)

- **Statements** → add a branch in `_exec()` for `node["type"] == "MyNewNode"`
- **Expressions** → add a branch in `_eval()` for the new node type
- **Built-in methods** → add the method name to the relevant frozenset
  (`ARRAY_METHODS`, `STRING_METHODS`, etc.) and implement the handler in the
  dispatch block

### 4. Tests (`tests/test_pyjs.py`)

Add a test to `PyJSTestCase` that runs JS source and asserts on output:

```python
def test_my_new_feature(self):
    source = '''
        // JS code exercising the feature
        console.log(result);
    '''
    interp = Interpreter()
    result = interp.run(source)
    self.assertEqual(result, "expected output")
```

### 5. Documentation (`docs/ecmascript-status.md`)

Update the completeness report:
- Add the feature to the "What Works" section
- Update version-specific coverage percentages if needed
- Add a row to the Phases Summary table
