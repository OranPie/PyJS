# PyJS

**PyJS** is a pure-Python ECMAScript interpreter with ~96% ES2015–ES2025 coverage.
Zero external dependencies — everything runs on the Python standard library.

## Features

- **Full ES2015–ES2025 language** — classes, generators, async/await, destructuring, spread, optional chaining, nullish coalescing, BigInt, `for...of`, `for await...of`, dynamic `import()`, private class fields, `using`/`await using` (ES2024), `@decorator` syntax (TC39 Stage 3)
- **Complete standard library** — `Promise`, `Map`, `Set`, `WeakMap`, `WeakRef`, `FinalizationRegistry`, `Proxy`, `Reflect`, all TypedArrays, `ArrayBuffer`, `DataView`, `Intl.*`, `URL`, `TextEncoder`/`TextDecoder`, `crypto`, `AbortController`, `performance.now()`
- **ES2025 built-ins** — `RegExp.escape()`, `Error.isError()`, `Math.sumPrecise()`, `Iterator.from()`, sync and async iterator helpers, `Symbol.dispose`/`Symbol.asyncDispose`
- **Modules** — `import`/`export`, default exports, namespace imports, circular dependency detection
- **Plugin system** — extend the runtime with first-party or custom plugins (see below)
- **Interactive REPL** — with tab completion and syntax highlighting
- **CLI** — run files, evaluate inline code, dump AST/tokens

## Installation

```bash
# editable install (recommended for development)
pip install -e .

# or run directly without installing
python3 main.py --help
```

## Quick Start

```bash
# Run a JS file
python3 main.py path/to/file.js

# Evaluate inline JS
python3 main.py -e 'console.log("Hello, world!")'

# Interactive REPL
python3 main.py --repl

# Dump the AST as JSON
python3 main.py --ast -e 'let x = 1 + 2'

# Dump tokens
python3 main.py --tokens -e 'let x = 1'

# Run all 223 tests
python3 -m unittest tests.test_pyjs.PyJSTestCase

# Run a single test
python3 -m unittest tests.test_pyjs.PyJSTestCase.test_generators
```

## Python API

```python
from pyjs import evaluate, evaluate_file, parse_source

# Evaluate a JS string — returns the final value as a Python object
result = evaluate('1 + 2')  # → 3.0

# Evaluate a JS file
result = evaluate_file('script.js')

# Parse source to an AST dict (no execution)
ast = parse_source('let x = 1')
```

For more control, use the `Interpreter` class directly:

```python
from pyjs.runtime import Interpreter

interp = Interpreter()
output = interp.run('console.log("hello")')  # → "hello"
```

## Plugins

PyJS ships eleven first-party plugins. Register them with `interp.register_plugin()`.

| Plugin | Import | Global(s) | Summary |
|--------|--------|-----------|---------|
| `StoragePlugin` | `from pyjs.plugins import StoragePlugin` | `localStorage`, `sessionStorage` | Persistent/session key-value storage |
| `FetchPlugin` | `from pyjs.plugins import FetchPlugin` | `fetch()` | HTTP client via `urllib` |
| `EventEmitterPlugin` | `from pyjs.plugins import EventEmitterPlugin` | `EventEmitter` | Node.js-style event emitter |
| `FileSystemPlugin` | `from pyjs.plugins import FileSystemPlugin` | `fs` | Sandboxed file I/O |
| `ConsoleExtPlugin` | `from pyjs.plugins import ConsoleExtPlugin` | *(extends `console`)* | `table`, `assert`, `trace`, `dir` |
| `ProcessPlugin` | `from pyjs.plugins import ProcessPlugin` | `process` | `env`, `argv`, `cwd()`, `exit()` |
| `PathPlugin` | `from pyjs.plugins import PathPlugin` | `path` | `join`, `resolve`, `dirname`, `basename`, etc. |
| `AssertPlugin` | `from pyjs.plugins import AssertPlugin` | `assert` | `assert.ok`, `strictEqual`, `deepEqual`, `throws` |
| `UtilPlugin` | `from pyjs.plugins import UtilPlugin` | `util` | `format`, `inspect`, `isDeepStrictEqual`, `types.*` |
| `CryptoSubtlePlugin` | `from pyjs.plugins import CryptoSubtlePlugin` | `crypto` | `randomBytes`, `createHash`, `createHmac`, `timingSafeEqual` |
| `ChildProcessPlugin` | `from pyjs.plugins import ChildProcessPlugin` | `childProcess` | `execSync`, `spawnSync`, `exec` |

```python
from pyjs import Interpreter
from pyjs.plugins import StoragePlugin, FetchPlugin, ProcessPlugin

interp = Interpreter()
interp.register_plugin(StoragePlugin())
interp.register_plugin(FetchPlugin(timeout=10))
interp.register_plugin(ProcessPlugin())
interp.run('console.log(process.platform)')
```

## Layout

```
pyjs/           — interpreter package
  runtime.py    — tree-walking interpreter (~4 450 lines)
  parser.py     — recursive-descent parser
  lexer.py      — tokenizer
  builtins_*.py — standard library built-ins
  plugins/      — first-party plugins (11 total)
  cli.py        — CLI entry point
  completer.py  — REPL tab completion
tests/          — 223 tests (unittest)
docs/           — reference documentation
main.py         — thin CLI wrapper
```

## Documentation

| File | Contents |
|------|----------|
| [`docs/ecmascript-status.md`](docs/ecmascript-status.md) | ECMAScript coverage report, feature list, phase history |
| [`docs/architecture.md`](docs/architecture.md) | Pipeline, AST nodes, JsValue, event loop, adding new features |
| [`docs/plugins.md`](docs/plugins.md) | Plugin authoring guide, all first-party plugin references |
| [`docs/test-list.txt`](docs/test-list.txt) | All 223 test names |
