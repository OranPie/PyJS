# PyJS

PyJS is a pure Python JavaScript interpreter with a lexer, parser, tree-walking runtime, CLI, and test suite.

## Layout

- `pyjs/`: interpreter package and CLI
- `tests/`: automated test coverage
- `docs/`: project reports and reference notes
- `main.py`: thin top-level CLI wrapper

## Quick Start

```bash
python main.py
python main.py --repl
python main.py -e "console.log(1 + 2)"
pytest
```

## Package Entry Point

After installation, the CLI is available as:

```bash
pyjs --help
```

## Documentation

- [`docs/ecmascript-status.md`](docs/ecmascript-status.md)
- [`docs/test-list.txt`](docs/test-list.txt)
