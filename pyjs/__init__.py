from __future__ import annotations

__version__ = "0.3.0"
__author__ = "OranPie"
__license__ = "MIT"

from pathlib import Path
from typing import List

from .lexer import Lexer, Token
from .parser import N, Parser
from .values import JsValue, UNDEFINED, JS_NULL, JS_TRUE, JS_FALSE
from .environment import Environment
from .runtime import Interpreter
from .modules import ModuleLoader
from .plugin import PyJSPlugin, PluginContext
from .plugins import (
    AssertPlugin,
    ChildProcessPlugin,
    ConsoleExtPlugin,
    CryptoSubtlePlugin,
    EventEmitterPlugin,
    FetchPlugin,
    FileSystemPlugin,
    PathPlugin,
    ProcessPlugin,
    StoragePlugin,
    UtilPlugin,
)


__all__ = [
    '__version__',
    '__author__',
    '__license__',
    'AssertPlugin',
    'ChildProcessPlugin',
    'ConsoleExtPlugin',
    'CryptoSubtlePlugin',
    'Environment',
    'EventEmitterPlugin',
    'FetchPlugin',
    'FileSystemPlugin',
    'Interpreter',
    'JS_FALSE',
    'JS_NULL',
    'JS_TRUE',
    'JsValue',
    'Lexer',
    'ModuleLoader',
    'N',
    'Parser',
    'PathPlugin',
    'PluginContext',
    'ProcessPlugin',
    'PyJSPlugin',
    'StoragePlugin',
    'Token',
    'UNDEFINED',
    'UtilPlugin',
    'evaluate',
    'evaluate_file',
    'parse_source',
    'repl',
    'tokenize_source',
]


def tokenize_source(source: str) -> List[Token]:
    return Lexer(source).tokenize()


def parse_source(source: str) -> dict:
    return Parser(tokenize_source(source)).parse()


def evaluate(source: str) -> str:
    return Interpreter().run(source)


def evaluate_file(path: str | Path, encoding: str = 'utf-8') -> str:
    from .modules import ModuleLoader
    path = Path(path)
    interp = Interpreter()
    loader = ModuleLoader(Interpreter)
    interp._module_loader = loader
    interp._module_file = str(path.resolve())
    return interp.run(path.read_text(encoding))


def repl(plugins: list | None = None, log_level: str | None = None) -> None:
    """Start a Node.js-style interactive REPL.

    Features:
    - Multi-line input (continues when braces/brackets/parens are unclosed)
    - Prints expression values automatically (like Node.js)
    - Tab completion for globals
    - .help  .exit  .clear  .break  .load <file>  .save <file>  .version commands
    - Readline history
    """
    import sys
    import os
    import traceback

    try:
        import readline
        import rlcompleter  # noqa: F401
        _has_readline = True
    except ImportError:
        _has_readline = False

    interp = Interpreter(log_level=log_level, plugins=plugins or [])

    # ── Tab completion ────────────────────────────────────────────────
    if _has_readline:
        def _completer(text: str, state: int) -> str | None:
            try:
                env = interp._global_env
            except AttributeError:
                env = interp.genv
            names = list(env.bindings.keys())
            # Also add JS keywords
            names += ['function', 'class', 'const', 'let', 'var', 'return',
                      'if', 'else', 'for', 'while', 'do', 'switch', 'case',
                      'break', 'continue', 'throw', 'try', 'catch', 'finally',
                      'new', 'delete', 'typeof', 'instanceof', 'void',
                      'true', 'false', 'null', 'undefined', 'this',
                      'async', 'await', 'import', 'export', 'from', 'of',
                      '.help', '.exit', '.clear', '.break', '.load', '.save', '.version']
            matches = [n for n in names if n.startswith(text)]
            return matches[state] if state < len(matches) else None

        readline.set_completer(_completer)
        readline.parse_and_bind("tab: complete")

        # History file
        history_file = os.path.expanduser("~/.pyjs_history")
        try:
            readline.read_history_file(history_file)
        except FileNotFoundError:
            pass

    # ── REPL helpers ─────────────────────────────────────────────────
    def _count_balance(code: str) -> int:
        """Return open-bracket depth (>0 means incomplete input)."""
        depth = 0
        in_str: str | None = None
        escape = False
        for ch in code:
            if escape:
                escape = False
                continue
            if ch == '\\' and in_str:
                escape = True
                continue
            if in_str:
                if ch == in_str:
                    in_str = None
            elif ch in ('"', "'", '`'):
                in_str = ch
            elif ch in ('{', '[', '('):
                depth += 1
            elif ch in ('}', ']', ')'):
                depth -= 1
        return depth

    def _format_value(val_str: str) -> str:
        """Colorise output like Node.js (basic ANSI if terminal)."""
        if not sys.stdout.isatty():
            return val_str
        # Number
        try:
            float(val_str)
            return f"\033[33m{val_str}\033[0m"  # yellow
        except (ValueError, TypeError):
            pass
        if val_str in ('true', 'false'):
            return f"\033[33m{val_str}\033[0m"  # yellow
        if val_str in ('null', 'undefined'):
            return f"\033[2m{val_str}\033[0m"   # dim
        if val_str.startswith(("'", '"', '`')):
            return f"\033[32m{val_str}\033[0m"  # green
        return val_str

    def _save_history() -> None:
        if _has_readline:
            try:
                readline.write_history_file(history_file)
            except Exception:
                pass

    WELCOME = (
        f"Welcome to PyJS v{__version__} (ECMAScript 2015-2025)\n"
        f"Type JavaScript code to evaluate. Special commands:\n"
        f"  .help    — show this help\n"
        f"  .exit    — exit the REPL\n"
        f"  .clear   — reset interpreter state\n"
        f"  .break   — abort multi-line input\n"
        f"  .load <file> — load and run a JS file\n"
        f"  .save <file> — save session history to a JS file\n"
        f"  .version — show version info\n"
    )
    print(WELCOME)

    buffer: list[str] = []
    session_lines: list[str] = []

    while True:
        prompt = "... " if buffer else "> "
        try:
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            if buffer:
                buffer.clear()
                print()
                continue
            print()
            _save_history()
            break

        # ── Dot commands ─────────────────────────────────────────────
        stripped = line.strip()

        if stripped == '.exit' or stripped in ('quit', 'exit'):
            _save_history()
            break

        if stripped == '.help':
            print(WELCOME)
            continue

        if stripped == '.clear':
            interp.__class__(log_level=log_level, plugins=plugins or [])
            # Reinitialise interpreter in-place is tricky; recreate instead
            interp = Interpreter(log_level=log_level, plugins=plugins or [])
            print("Interpreter state cleared.")
            buffer.clear()
            session_lines.clear()
            continue

        if stripped == '.break':
            buffer.clear()
            continue

        if stripped == '.version':
            print(f"PyJS v{__version__} — Python {sys.version.split()[0]}")
            continue

        if stripped.startswith('.load '):
            load_path = stripped[6:].strip()
            try:
                src = Path(load_path).read_text(encoding='utf-8')
                interp.run(src)
                session_lines.append(f"// .load {load_path}")
                session_lines.append(src)
            except FileNotFoundError:
                print(f"Error: file not found: {load_path}")
            except Exception as exc:
                print(f"Error: {exc}")
            continue

        if stripped.startswith('.save '):
            save_path = stripped[6:].strip()
            try:
                Path(save_path).write_text('\n'.join(session_lines), encoding='utf-8')
                print(f"Session saved to {save_path}")
            except Exception as exc:
                print(f"Error saving: {exc}")
            continue

        # ── Multi-line accumulation ───────────────────────────────────
        buffer.append(line)
        code = '\n'.join(buffer)

        if _count_balance(code) > 0:
            # Still open — keep reading
            continue

        # Ends with backslash continuation?
        if line.endswith('\\'):
            buffer[-1] = line[:-1]
            continue

        # ── Execute ───────────────────────────────────────────────────
        source = code.strip()
        buffer.clear()

        if not source:
            continue

        session_lines.append(source)

        try:
            # Wrap in parens to allow expression statements like {a:1} or functions
            # Try as expression first to get a printable result
            is_expr = True
            try:
                from .parser import Parser as _Parser
                from .lexer import Lexer as _Lexer
                ast = _Parser(_Lexer(source).tokenize()).parse()
                body = ast.get('body', [])
                # If single ExpressionStatement, print its value
                if (len(body) == 1 and
                        body[0].get('type') == 'ExpressionStatement' and
                        body[0].get('expression', {}).get('type') not in
                        ('AssignmentExpression',)):
                    pass  # will print result
                else:
                    is_expr = False
            except Exception:
                is_expr = False

            # Always run through interpreter
            # Capture printed output separately from expression value
            import io, contextlib
            stdout_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf):
                interp.run(source)
            printed = stdout_buf.getvalue()
            if printed:
                print(printed, end='' if printed.endswith('\n') else '\n')

            # For expression statements: also display the last evaluated value
            if is_expr:
                try:
                    from .values import UNDEFINED as _UNDEF
                    last = interp._last_value if hasattr(interp, '_last_value') else None
                    if last is not None and last is not _UNDEF:
                        val_str = interp._to_str(last)
                        # For objects/arrays, use JSON-like inspect
                        if last.type in ('object', 'array', 'function', 'intrinsic', 'class'):
                            try:
                                import json
                                from .core import js_to_py
                                py_val = js_to_py(last)
                                val_str = json.dumps(py_val, default=str, ensure_ascii=False)
                            except Exception:
                                pass
                        print(_format_value(val_str))
                except Exception:
                    pass

        except SystemExit:
            raise
        except KeyboardInterrupt:
            print("\n(interrupted)")
        except Exception as exc:
            print(f"Uncaught exception: {exc}")
            if os.environ.get('PYJS_REPL_TRACEBACK'):
                traceback.print_exc()

    print("(To exit, press Ctrl+C again or type .exit)")

