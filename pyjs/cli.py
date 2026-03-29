from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import Interpreter, parse_source, repl, tokenize_source
from .demo import DEMO_SOURCE


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Pure Python JavaScript interpreter')
    parser.add_argument('file', nargs='?', help='JavaScript file to execute, or - to read stdin')
    parser.add_argument('-e', '--eval', dest='inline_source', help='Evaluate an inline JavaScript snippet')
    parser.add_argument('--ast', action='store_true', help='Parse input and print the AST as JSON')
    parser.add_argument('--tokens', action='store_true', help='Tokenize input and print one token per line')
    parser.add_argument('--bench', action='store_true', help='Print execution time in milliseconds')
    parser.add_argument('--repl', action='store_true', help='Start the interactive REPL after any requested run')
    parser.add_argument('--no-demo', action='store_true', help='Skip the bundled demo when no input is provided')
    parser.add_argument('--log-level', metavar='LEVEL', default=None,
                        choices=['TRACE', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL',
                                 'trace', 'debug', 'info', 'warning', 'error', 'critical'],
                        help='Set logging level (TRACE, DEBUG, INFO, WARNING)')
    parser.add_argument('--log-filter', metavar='LOGGERS', default=None,
                        help='Comma-separated logger names to show (e.g. exec,call,prop)')
    parser.add_argument('--log-verbose', action='store_true',
                        help='Include call-depth indentation in log output')
    return parser


def _load_source(args: argparse.Namespace) -> tuple[str | None, str]:
    if args.inline_source is not None:
        return args.inline_source, '<eval>'
    if args.file == '-':
        return sys.stdin.read(), '<stdin>'
    if args.file:
        path = Path(args.file)
        return path.read_text(encoding='utf-8'), str(path)
    if args.no_demo:
        return None, ''
    return DEMO_SOURCE, '<demo>'


def _print_tokens(source: str) -> None:
    for token in tokenize_source(source):
        print(f'{token.line}:{token.col} {token.type} {token.value!r}')


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Configure logging early (before any Interpreter creation)
    if args.log_level or args.log_filter or args.log_verbose:
        from .trace import configure as _configure_trace
        _configure_trace(
            level=args.log_level,
            log_filter=args.log_filter,
            verbose=args.log_verbose,
        )

    source, label = _load_source(args)

    if source is not None:
        if args.tokens:
            _print_tokens(source)
        elif args.ast:
            print(json.dumps(parse_source(source), indent=2, ensure_ascii=False))
        else:
            if label == '<demo>':
                print('=' * 60)
                print('  JavaScript Interpreter - Demo')
                print('=' * 60)
                print()
            started = time.perf_counter()
            interp = Interpreter()
            interp.run(source)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if label == '<demo>':
                print()
                print('=' * 60)
                print('  Demo complete. Run with --repl for interactive mode.')
                print('  Or pass a .js file as argument, or use -e/--eval.')
                print('=' * 60)
            if args.bench:
                print(f'Execution time: {elapsed_ms:.3f} ms')

    if args.repl:
        repl()

    if source is None and not args.repl:
        parser.print_help()
    return 0
