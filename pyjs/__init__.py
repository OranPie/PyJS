from __future__ import annotations

from pathlib import Path
from typing import List

from .lexer import Lexer, Token
from .parser import N, Parser
from .values import JsValue, UNDEFINED, JS_NULL, JS_TRUE, JS_FALSE
from .environment import Environment
from .runtime import Interpreter
from .modules import ModuleLoader
from .plugin import PyJSPlugin, PluginContext


__all__ = [
    'Environment',
    'Interpreter',
    'JS_FALSE',
    'JS_NULL',
    'JS_TRUE',
    'Lexer',
    'ModuleLoader',
    'N',
    'Parser',
    'PluginContext',
    'PyJSPlugin',
    'Token',
    'UNDEFINED',
    'JsValue',
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


def repl() -> None:
    import readline  # noqa: F401

    interp = Interpreter()
    print("JavaScript Interpreter (Pure Python) - type JS code, 'quit' to exit\n")
    while True:
        try:
            line = input('js> ')
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.strip() in ('quit', 'exit', '.exit'):
            break
        if not line.strip():
            continue
        interp.run(line)
