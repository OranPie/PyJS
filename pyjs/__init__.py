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
    - Full JS error tracebacks with stack frames
    - Python internal error tracebacks (set PYJS_DEBUG=1 or --log-level DEBUG)
    - Tab completion for globals and JS keywords
    - Persistent readline history (~/.pyjs_history)
    - Dot commands: .help .exit .clear .break .load .save .version .stack
    """
    import sys
    import os
    import io
    import contextlib
    import traceback as _traceback_mod

    try:
        import readline as _readline
        _has_readline = True
    except ImportError:
        _has_readline = False

    _use_color = sys.stdout.isatty()
    _use_color_err = sys.stderr.isatty()

    # ANSI helpers
    def _ansi(code: str, text: str, enabled: bool = True) -> str:
        if not enabled:
            return text
        codes = {'red': '31', 'green': '32', 'yellow': '33', 'cyan': '36',
                 'dim': '2', 'bold': '1', 'reset': '0', 'magenta': '35', 'blue': '34'}
        return f"\033[{codes.get(code, code)}m{text}\033[0m"

    interp = Interpreter(log_level=log_level, plugins=plugins or [])

    # ── Tab completion ────────────────────────────────────────────────
    if _has_readline:
        def _completer(text: str, state: int) -> str | None:
            env = interp.genv
            names = list(env.bindings.keys())
            names += [
                'function', 'class', 'const', 'let', 'var', 'return',
                'if', 'else', 'for', 'while', 'do', 'switch', 'case',
                'break', 'continue', 'throw', 'try', 'catch', 'finally',
                'new', 'delete', 'typeof', 'instanceof', 'void',
                'true', 'false', 'null', 'undefined', 'this',
                'async', 'await', 'import', 'export', 'from', 'of', 'in',
                '.help', '.exit', '.clear', '.break', '.load ', '.save ',
                '.version', '.stack',
            ]
            matches = [n for n in names if n.startswith(text)]
            return matches[state] if state < len(matches) else None

        _readline.set_completer(_completer)
        _readline.parse_and_bind("tab: complete")

        history_file = os.path.expanduser("~/.pyjs_history")
        try:
            _readline.read_history_file(history_file)
            _readline.set_history_length(2000)
        except FileNotFoundError:
            pass

    # ── REPL helpers ─────────────────────────────────────────────────
    def _count_balance(code: str) -> int:
        """Return net open-bracket count (>0 means input is incomplete)."""
        depth = 0
        in_str: str | None = None
        escape = False
        in_tpl = 0  # template literal nesting
        for ch in code:
            if escape:
                escape = False
                continue
            if ch == '\\' and in_str:
                escape = True
                continue
            if in_str == '`':
                if ch == '`':
                    in_str = None
                continue
            if in_str:
                if ch == in_str:
                    in_str = None
                continue
            if ch in ('"', "'", '`'):
                in_str = ch
            elif ch in ('{', '[', '('):
                depth += 1
            elif ch in ('}', ']', ')'):
                depth -= 1
        return depth

    def _save_history() -> None:
        if _has_readline:
            try:
                _readline.write_history_file(history_file)
            except Exception:
                pass

    def _print_error(text: str) -> None:
        """Print error text to stderr (red if TTY)."""
        print(_ansi('red', text, _use_color_err), file=sys.stderr)

    def _print_dim(text: str) -> None:
        print(_ansi('dim', text, _use_color_err), file=sys.stderr)

    def _format_js_error(last_error: dict, *, verbose: bool = False) -> str:
        """Format a JS or internal error for REPL display."""
        lines = []
        if last_error.get('js_error'):
            err_type = last_error.get('error_type', 'Error')
            msg = last_error.get('message', '')
            # Main error line
            lines.append(_ansi('red', f"{err_type}: {msg}", _use_color_err))
            # Stack frames (everything after the first line in .stack)
            stack = last_error.get('stack', '')
            if stack:
                frame_lines = stack.split('\n')[1:]  # skip "ErrorType: msg"
                for fl in frame_lines:
                    if fl.strip():
                        lines.append(_ansi('dim', f"  {fl.strip()}", _use_color_err))
        else:
            # Internal Python error
            err_type = last_error.get('error_type', 'InternalError')
            msg = last_error.get('message', '')
            lines.append(_ansi('red', f"InternalError ({err_type}): {msg}", _use_color_err))
            if verbose or os.environ.get('PYJS_DEBUG'):
                tb = last_error.get('python_traceback', '')
                if tb:
                    lines.append(_ansi('dim', 'Python traceback:', _use_color_err))
                    for tbl in tb.strip().splitlines():
                        lines.append(_ansi('dim', f"  {tbl}", _use_color_err))
            else:
                lines.append(_ansi('dim',
                    '  (set PYJS_DEBUG=1 or use --log-level DEBUG for full Python traceback)',
                    _use_color_err))
        return '\n'.join(lines)

    from .inspect_val import js_inspect
    from .values import UNDEFINED as _UNDEF

    _verbose_errors = bool(os.environ.get('PYJS_DEBUG')) or (log_level or '').upper() in ('DEBUG', 'TRACE')

    WELCOME = (
        f"Welcome to PyJS v{__version__} (ECMAScript 2015-2025)\n"
        f"Type JavaScript code to evaluate. Special commands:\n"
        f"  .help         — show this help\n"
        f"  .exit         — exit the REPL\n"
        f"  .clear        — reset interpreter state\n"
        f"  .break        — abort multi-line input\n"
        f"  .stack        — show current JS call stack\n"
        f"  .load <file>  — load and run a JS file\n"
        f"  .save <file>  — save session history to a JS file\n"
        f"  .version      — show version info\n"
        f"Set PYJS_DEBUG=1 for full Python tracebacks on internal errors."
    )
    print(WELCOME)
    print()

    buffer: list[str] = []
    session_lines: list[str] = []

    while True:
        prompt = _ansi('dim', '... ', _use_color) if buffer else _ansi('bold', '> ', _use_color)
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

        if stripped in ('.exit', 'quit', 'exit'):
            _save_history()
            break

        if stripped == '.help':
            print(WELCOME)
            continue

        if stripped == '.clear':
            interp = Interpreter(log_level=log_level, plugins=plugins or [])
            print("Interpreter state cleared.")
            buffer.clear()
            session_lines.clear()
            continue

        if stripped == '.break':
            buffer.clear()
            continue

        if stripped == '.version':
            import platform
            print(f"PyJS v{__version__} — Python {platform.python_version()} "
                  f"[{platform.python_implementation()}]")
            continue

        if stripped == '.stack':
            stack = interp._js_call_stack if hasattr(interp, '_js_call_stack') else []
            if stack:
                print("Current JS call stack:")
                for i, f in enumerate(reversed(stack)):
                    print(f"  #{i}  {f['name']} ({f['file']}:{f['line']})")
            else:
                print("(empty call stack)")
            continue

        if stripped.startswith('.load '):
            load_path = stripped[6:].strip()
            try:
                src = Path(load_path).read_text(encoding='utf-8')
                stdout_buf = io.StringIO()
                with contextlib.redirect_stdout(stdout_buf):
                    interp.run(src)
                printed = stdout_buf.getvalue()
                if printed:
                    print(printed, end='' if printed.endswith('\n') else '\n')
                if interp._last_error:
                    print(_format_js_error(interp._last_error, verbose=_verbose_errors),
                          file=sys.stderr)
                else:
                    session_lines.append(f"// .load {load_path}\n{src}")
            except FileNotFoundError:
                _print_error(f"Error: file not found: {load_path}")
            except Exception as exc:
                _print_error(f"Error loading file: {exc}")
                if _verbose_errors:
                    _traceback_mod.print_exc(file=sys.stderr)
            continue

        if stripped.startswith('.save '):
            save_path = stripped[6:].strip()
            try:
                Path(save_path).write_text('\n\n'.join(session_lines), encoding='utf-8')
                print(f"Session saved to {save_path}")
            except Exception as exc:
                _print_error(f"Error saving: {exc}")
            continue

        # ── Multi-line accumulation ───────────────────────────────────
        buffer.append(line)
        code = '\n'.join(buffer)

        if _count_balance(code) > 0:
            continue

        if line.endswith('\\'):
            buffer[-1] = line[:-1]
            continue

        # ── Execute ───────────────────────────────────────────────────
        source = code.strip()
        buffer.clear()

        if not source:
            continue

        session_lines.append(source)

        # Determine if this is a pure expression (to print its value).
        # If source starts with `{`, try wrapping in `(...)` first
        # (handles object literals and blocks ambiguity like Node.js).
        is_expr = False
        run_source = source
        try:
            from .parser import Parser as _Parser
            from .lexer import Lexer as _Lexer

            def _try_parse(src: str):
                return _Parser(_Lexer(src).tokenize()).parse()

            # Try original source first
            try:
                _ast = _try_parse(source)
                _body = _ast.get('body', [])
                if (len(_body) == 1 and
                        _body[0].get('type') == 'ExpressionStatement' and
                        _body[0].get('expression', {}).get('type') != 'AssignmentExpression'):
                    is_expr = True
            except SyntaxError:
                if source.startswith('{') and source.endswith('}'):
                    # Try as parenthesised expression
                    try:
                        wrapped = f"({source})"
                        _ast2 = _try_parse(wrapped)
                        _body2 = _ast2.get('body', [])
                        if (len(_body2) == 1 and
                                _body2[0].get('type') == 'ExpressionStatement'):
                            run_source = wrapped
                            is_expr = True
                    except SyntaxError:
                        pass
        except Exception:
            pass

        try:
            # Redirect stdout to capture console.log output
            stdout_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf):
                interp.run(run_source)

            printed = stdout_buf.getvalue()
            if printed:
                print(printed, end='' if printed.endswith('\n') else '\n')

            # ── Error display ─────────────────────────────────────────
            if interp._last_error:
                print(_format_js_error(interp._last_error, verbose=_verbose_errors),
                      file=sys.stderr)

            # ── Value display ─────────────────────────────────────────
            elif is_expr:
                last = getattr(interp, '_last_value', None)
                if last is not None:
                    result_str = js_inspect(last, interp, depth=3, colors=_use_color)
                    print(result_str)

        except SystemExit:
            raise
        except KeyboardInterrupt:
            print(_ansi('yellow', '\n(interrupted)', _use_color))
            buffer.clear()
        except Exception as exc:
            # Unexpected Python exception from the REPL machinery itself
            _print_error(f"InternalError: {exc}")
            if _verbose_errors:
                _traceback_mod.print_exc(file=sys.stderr)
            else:
                _print_dim("  (set PYJS_DEBUG=1 for full traceback)")

    print(_ansi('dim', '(bye)', _use_color))



