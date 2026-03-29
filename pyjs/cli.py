from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, Interpreter, parse_source, repl, tokenize_source
from .demo import DEMO_SOURCE
from .colors import (
    set_enabled, is_enabled,
    c, bold, dim, red, bred, green, bgreen, yellow, byellow,
    blue, bblue, magenta, bmagenta, cyan, bcyan, white, bwhite,
    box, rule, token_color, highlight_json, format_duration,
    _supports_color,
)


# ── Coloured argparse formatter ──────────────────────────────────────────────
class _ColorHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """argparse formatter that adds colour to the help output."""

    def start_section(self, heading: str | None) -> None:
        if heading:
            heading = c('bold', 'cyan', heading.capitalize())
        super().start_section(heading)

    def _format_usage(self, usage, actions, groups, prefix):
        if prefix is None:
            prefix = c('bold', 'cyan', 'Usage') + ': '
        return super()._format_usage(usage, actions, groups, prefix)

    def _format_action_invocation(self, action: argparse.Action) -> str:
        text = super()._format_action_invocation(action)
        for opt in action.option_strings:
            text = text.replace(opt, c('byellow', opt), 1)
        return text


def _make_formatter_class():
    if is_enabled():
        return _ColorHelpFormatter
    return argparse.RawDescriptionHelpFormatter


# ── Argument parser ──────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    if is_enabled():
        _desc = (bold('PyJS') + f" v{__version__} \u2014 " +
                 cyan('Pure-Python ECMAScript 2015-2025 interpreter'))
        _q = '"console.log(42)"'
        _jq = '| jq .body[0].type'
        _epilog = '\n'.join([
            dim('Examples:'),
            f'  {c("byellow","pyjs")} {c("bgreen","script.js")}',
            f'  {c("byellow","pyjs")} {c("dim","-e")} {c("bgreen",_q)} {c("dim","--bench")}',
            f'  {c("byellow","pyjs")} {c("dim","--ast")} {c("bgreen","script.js")} {c("dim",_jq)}',
            f'  {c("byellow","pyjs")} {c("dim","--repl")}',
        ])
    else:
        _desc = f"PyJS v{__version__} \u2014 Pure-Python ECMAScript 2015-2025 interpreter"
        _epilog = (
            'Examples:\n'
            '  pyjs script.js\n'
            '  pyjs -e "console.log(42)" --bench\n'
            '  pyjs --ast script.js | jq .body[0].type\n'
            '  pyjs --repl'
        )

    parser = argparse.ArgumentParser(
        prog='pyjs',
        description=_desc,
        formatter_class=_make_formatter_class(),
        epilog=_epilog,
    )

    # Input sources
    inp = parser.add_argument_group('Input')
    inp.add_argument('file', nargs='?',
                     help='JavaScript file to run  (use - for stdin)')
    inp.add_argument('-e', '--eval', dest='inline_source', metavar='CODE',
                     help='Evaluate an inline JavaScript snippet')

    # Modes
    modes = parser.add_argument_group('Mode')
    modes.add_argument('--ast', action='store_true',
                       help='Print the parsed AST as JSON (syntax-highlighted)')
    modes.add_argument('--tokens', action='store_true',
                       help='Print tokens with type, value, and position')
    modes.add_argument('--repl', action='store_true',
                       help='Start the interactive REPL (after running file/eval if given)')
    modes.add_argument('--no-demo', action='store_true',
                       help='Skip the bundled demo when no file/eval is given')

    # Output
    out = parser.add_argument_group('Output')
    out.add_argument('--bench', action='store_true',
                     help='Show detailed execution timing (parse + run)')
    out.add_argument('--stats', action='store_true',
                     help='Show execution statistics (steps, output lines, errors)')
    out.add_argument('--color', dest='color', action='store_true', default=None,
                     help='Force enable ANSI colour output')
    out.add_argument('--no-color', dest='color', action='store_false',
                     help='Disable ANSI colour output')

    # Logging
    log = parser.add_argument_group('Logging')
    log.add_argument('--log-level', metavar='LEVEL', default=None,
                     choices=['TRACE', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL',
                              'trace', 'debug', 'info', 'warning', 'error', 'critical'],
                     help='Logging level  (TRACE DEBUG INFO WARNING ERROR)')
    log.add_argument('--log-filter', metavar='LOGGERS', default=None,
                     help='Comma-separated loggers  e.g. exec,call,scope,prop')
    log.add_argument('--log-verbose', action='store_true',
                     help='Show call-depth indentation in log lines')

    return parser


# ── Source loading ────────────────────────────────────────────────────────────
def _load_source(args: argparse.Namespace) -> tuple[str | None, str]:
    if args.inline_source is not None:
        return args.inline_source, '<eval>'
    if args.file == '-':
        return sys.stdin.read(), '<stdin>'
    if args.file:
        path = Path(args.file)
        return path.read_text(encoding='utf-8'), str(path)
    if args.no_demo or getattr(args, 'repl', False):
        return None, ''
    return DEMO_SOURCE, '<demo>'


# ── Token printer ─────────────────────────────────────────────────────────────
def _print_tokens(source: str) -> None:
    tokens = list(tokenize_source(source))
    if not tokens:
        print(dim('(no tokens)'))
        return

    # Column widths
    max_pos  = max(len(f'{t.line}:{t.col}') for t in tokens)
    max_type = max(len(t.type) for t in tokens)

    # Header
    header = (
        c('bold', 'cyan', 'POS'.ljust(max_pos + 2)) +
        c('bold', 'cyan', 'TYPE'.ljust(max_type + 2)) +
        c('bold', 'cyan', 'VALUE')
    )
    print(header)
    print(rule(max_pos + max_type + 30))

    for tok in tokens:
        pos_str  = c('dim', f'{tok.line}:{tok.col}'.ljust(max_pos + 2))
        col      = token_color(tok.type)
        type_str = c(col, 'bold', tok.type.ljust(max_type + 2))
        if tok.value is None:
            val_str = dim('∅')
        elif tok.type == 'STRING':
            val_str = bgreen(repr(tok.value))
        elif tok.type == 'NUMBER':
            n = tok.value
            if n == int(n):
                val_str = byellow(str(int(n)))
            else:
                val_str = byellow(str(n))
        elif tok.type in ('TRUE', 'FALSE', 'NULL', 'UNDEFINED'):
            val_str = byellow(repr(tok.value))
        elif tok.type == 'EOF':
            val_str = dim('<end of file>')
        else:
            val_str = white(repr(tok.value))
        print(f'{pos_str}{type_str}{val_str}')

    print(rule(max_pos + max_type + 30))
    count = len([t for t in tokens if t.type != 'EOF'])
    print(dim(f'{count} tokens') + ' in ' + dim(f'{source.count(chr(10)) + 1} line(s)'))


# ── AST printer ───────────────────────────────────────────────────────────────
def _print_ast(source: str) -> None:
    ast = parse_source(source)
    json_str = json.dumps(ast, indent=2, ensure_ascii=False)
    if is_enabled():
        print(highlight_json(json_str))
    else:
        print(json_str)

    # Summary
    body = ast.get('body', [])
    node_types = _collect_node_types(ast)
    print()
    print(dim(f'Top-level statements: {len(body)}  '
              f'Unique node types: {len(node_types)}'))


def _collect_node_types(node, seen=None) -> set:
    if seen is None:
        seen = set()
    if isinstance(node, dict):
        if 'type' in node:
            seen.add(node['type'])
        for v in node.values():
            _collect_node_types(v, seen)
    elif isinstance(node, list):
        for item in node:
            _collect_node_types(item, seen)
    return seen


# ── Demo banner ───────────────────────────────────────────────────────────────
def _print_demo_header() -> None:
    lines = [
        c('bold', 'bwhite', f'  PyJS v{__version__}'),
        '',
        c('cyan', '  ECMAScript 2015 – 2025  ·  Pure Python  ·  No dependencies'),
    ]
    print(box(lines, width=62, style='double', title=' Demo ', color='cyan'))
    print()


def _print_demo_footer(elapsed_ms: float, steps: int) -> None:
    print()
    timing = format_duration(elapsed_ms)
    lines = [
        '',
        c('bgreen', '  ✓  Demo complete'),
        '',
        f'  Time: {timing}   Steps: {c("byellow", str(steps))}',
        '',
        dim('  Run with --repl for interactive mode.'),
        dim('  Pass a .js file, or use -e/--eval for inline code.'),
        '',
    ]
    print(box(lines, width=62, style='double', color='dim'))


# ── Stats display ─────────────────────────────────────────────────────────────
def _print_stats(interp: Interpreter, label: str,
                 parse_ms: float, run_ms: float, has_error: bool) -> None:
    total_ms = parse_ms + run_ms
    lines_out = len(interp.output)
    steps = getattr(interp, '_exec_steps', 0)
    call_depth = getattr(interp, '_call_depth', 0)

    status = bred('✗ ERROR') if has_error else bgreen('✓ OK')

    rows = [
        ('Source',       c('bwhite', label)),
        ('Status',       status),
        ('Parse time',   format_duration(parse_ms)),
        ('Run time',     format_duration(run_ms)),
        ('Total time',   format_duration(total_ms)),
        ('Exec steps',   c('byellow', str(steps))),
        ('Output lines', c('cyan', str(lines_out))),
    ]
    if has_error:
        rows.append(('Error', red(interp._last_error.get('message', '?')[:60]
                                  if interp._last_error else '?')))

    key_w = max(len(k) for k, _ in rows) + 2
    print()
    print(rule(50, '─', 'dim'))
    print(c('bold', 'cyan', '  Execution Summary'))
    print(rule(50, '─', 'dim'))
    for key, val in rows:
        print(f'  {c("dim", key.ljust(key_w))} {val}')
    print(rule(50, '─', 'dim'))


# ── Bench display ─────────────────────────────────────────────────────────────
def _print_bench(label: str, parse_ms: float, run_ms: float) -> None:
    total = parse_ms + run_ms
    bar_width = 30
    frac_parse = parse_ms / total if total else 0
    bar_parse = round(frac_parse * bar_width)
    bar_run   = bar_width - bar_parse

    parse_bar = c('blue', '█' * bar_parse)
    run_bar   = c('green', '█' * bar_run)

    print()
    print(c('bold', 'cyan', '⏱  Timing'))
    print(rule(50, '─', 'dim'))
    print(f'  {c("dim", "Parse  ")} {parse_bar}{run_bar}  {format_duration(parse_ms)}')
    print(f'  {c("dim", "Run    ")} ' +
          ' ' * bar_parse + c('green', '█' * bar_run) + '  ' + format_duration(run_ms))
    print(f'  {c("dim", "Total  ")} {format_duration(total)}')
    print(rule(50, '─', 'dim'))


# ── Error display ─────────────────────────────────────────────────────────────
def _print_run_error(interp: Interpreter) -> None:
    err = interp._last_error
    if not err:
        return
    if err.get('js_error'):
        err_type = err.get('error_type', 'Error')
        msg      = err.get('message', '')
        print(bred(f'\n  ✗  {err_type}: {msg}'), file=sys.stderr)
        stack = err.get('stack', '')
        if stack and '\n' in stack:
            for line in stack.split('\n')[1:]:
                if line.strip():
                    print(dim(f'     {line.strip()}'), file=sys.stderr)
    else:
        err_type = err.get('error_type', 'InternalError')
        msg      = err.get('message', '')
        print(bred(f'\n  ✗  InternalError ({err_type}): {msg}'), file=sys.stderr)
        if os.environ.get('PYJS_DEBUG'):
            tb = err.get('python_traceback', '')
            if tb:
                print(dim(tb), file=sys.stderr)


# ── Main entry point ──────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    # Parse --color / --no-color early (before building coloured help)
    _pre = [a for a in (argv or sys.argv[1:]) if a in ('--color', '--no-color')]
    if '--no-color' in _pre:
        set_enabled(False)
    elif '--color' in _pre:
        set_enabled(True)

    parser = _build_parser()
    args = parser.parse_args(argv)

    # Apply final colour setting (handles None = auto-detect)
    if args.color is True:
        set_enabled(True)
    elif args.color is False:
        set_enabled(False)
    # else: keep auto-detected value

    # Configure logging
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
            _print_ast(source)
        else:
            is_demo = (label == '<demo>')

            if is_demo:
                _print_demo_header()

            # ── Parse phase ──────────────────────────────────────────
            parse_start = time.perf_counter()
            try:
                from .lexer import Lexer
                from .parser import Parser
                ast = Parser(Lexer(source).tokenize()).parse()
                parse_ms = (time.perf_counter() - parse_start) * 1000.0
            except SyntaxError as e:
                print(bred(f'SyntaxError: {e}'), file=sys.stderr)
                return 1

            # ── Run phase ────────────────────────────────────────────
            interp = Interpreter(
                log_level=args.log_level,
                log_filter=args.log_filter if hasattr(args, 'log_filter') else None,
            )
            run_start = time.perf_counter()
            interp.run(source)
            run_ms = (time.perf_counter() - run_start) * 1000.0
            has_error = interp._last_error is not None

            if has_error:
                _print_run_error(interp)

            if is_demo:
                steps = getattr(interp, '_exec_steps', 0)
                _print_demo_footer(parse_ms + run_ms, steps)
            elif args.bench:
                _print_bench(label, parse_ms, run_ms)

            if args.stats:
                _print_stats(interp, label, parse_ms, run_ms, has_error)
            elif args.bench and not is_demo:
                pass  # bench already printed above

            if has_error:
                return 1

    if args.repl:
        repl(log_level=args.log_level if hasattr(args, 'log_level') else None)

    if source is None and not args.repl:
        parser.print_help()
    return 0


# ── pyjs-repl entry point ─────────────────────────────────────────────────────
def repl_main(argv: list[str] | None = None) -> int:
    """Entry point for the ``pyjs-repl`` command — starts REPL immediately."""
    p = argparse.ArgumentParser(
        prog='pyjs-repl',
        description=(
            bold('PyJS') + f" v{__version__} — " + cyan('Node.js-style interactive REPL')
            if is_enabled() else
            f'PyJS v{__version__} — Node.js-style interactive REPL'
        ),
        formatter_class=_make_formatter_class(),
    )
    p.add_argument('--log-level', metavar='LEVEL', default=None,
                   help='Set logging level (TRACE, DEBUG, INFO, WARNING)')
    p.add_argument('--log-filter', metavar='LOGGERS', default=None,
                   help='Comma-separated loggers to show  e.g. call,scope')
    p.add_argument('--no-color', dest='color', action='store_false', default=None,
                   help='Disable ANSI colour output')
    p.add_argument('--color', dest='color', action='store_true',
                   help='Force enable ANSI colour output')
    args = p.parse_args(argv)

    if args.color is True:
        set_enabled(True)
    elif args.color is False:
        set_enabled(False)

    if args.log_level or args.log_filter:
        from .trace import configure as _ct
        _ct(level=args.log_level, log_filter=args.log_filter)

    repl(log_level=args.log_level)
    return 0

