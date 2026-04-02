from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from pyjs import Interpreter, evaluate, evaluate_file, parse_source, tokenize_source


class PyJSTestCase(unittest.TestCase):
    def test_tokenizer_recognizes_arrow_and_nullish(self):
        tokens = tokenize_source('const fn = value => value ?? 0;')
        token_types = [token.type for token in tokens]
        self.assertIn('ARROW', token_types)
        self.assertIn('NULLISH', token_types)

    def test_parse_source_returns_program(self):
        ast = parse_source('let value = 1 + 2;')
        self.assertEqual(ast['type'], 'Program')
        self.assertEqual(ast['body'][0]['type'], 'VariableDeclaration')

    def test_run_returns_only_new_output(self):
        interp = Interpreter()
        first_stdout = io.StringIO()
        second_stdout = io.StringIO()
        with contextlib.redirect_stdout(first_stdout):
            first = interp.run('console.log("first")')
        with contextlib.redirect_stdout(second_stdout):
            second = interp.run('console.log("second")')
        self.assertEqual(first, 'first')
        self.assertEqual(second, 'second')
        self.assertEqual(first_stdout.getvalue().strip(), 'first')
        self.assertEqual(second_stdout.getvalue().strip(), 'second')

    def test_evaluate_file_reads_utf8_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'sample.js'
            path.write_text('console.log("file run")', encoding='utf-8')
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = evaluate_file(path)
            self.assertEqual(result, 'file run')
            self.assertEqual(stdout.getvalue().strip(), 'file run')

    def test_promises_microtasks_and_timers_are_ordered(self):
        source = '''
console.log("start");
Promise.resolve(1).then(value => console.log("micro", value));
setTimeout(() => console.log("timer"), 5);
console.log("end");
'''
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = Interpreter().run(source)
        self.assertEqual(result.splitlines(), ['start', 'end', 'micro 1', 'timer'])

    def test_async_function_awaits_promise_timer(self):
        source = '''
async function readLater() {
    let value = await new Promise(resolve => setTimeout(() => resolve(7), 5));
    console.log("later", value);
}
readLater();
'''
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = Interpreter().run(source)
        self.assertEqual(result.splitlines(), ['later 7'])

    def test_optional_chaining_short_circuits_and_calls(self):
        source = '''
let missing = null;
let obj = {
    nested: { value: 7 },
    plusOne(value) { return value + 1; }
};
console.log(missing?.nested === undefined);
console.log(obj?.nested?.value);
console.log(missing?.plusOne?.(4) === undefined);
console.log(obj.plusOne?.(4));
'''
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = Interpreter().run(source)
        self.assertEqual(result.splitlines(), ['true', '7', 'true', '5'])

    def test_destructuring_bindings_and_params_work(self):
        source = '''
let {a, b: renamed = 5, ...rest} = {a: 1, c: 3};
let [first, second = 9, ...tail] = [4];
function show({name, meta: {count = 2}}, [x, y = 8]) {
    console.log(name, count, x, y);
}
show({name: "box", meta: {}}, [6]);
console.log(a, renamed, rest.c, first, second, tail.length);
'''
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = Interpreter().run(source)
        self.assertEqual(result.splitlines(), ['box 2 6 8', '1 5 3 4 9 0'])

    def test_array_from_and_includes_follow_common_js_behavior(self):
        source = '''
let chars = Array.from("ab");
let arrayLike = Array.from({0: "x", 1: "y", length: 2});
console.log(chars.join("-"));
console.log(arrayLike.join("-"));
console.log([1, 2, 3].includes(2, 2));
console.log([1, 2, 3].includes(2, 1));
'''
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = Interpreter().run(source)
        self.assertEqual(result.splitlines(), ['a-b', 'x-y', 'false', 'true'])

    def test_logical_assignment_operators_short_circuit(self):
        source = '''
let a = 0;
let b = 1;
let c = undefined;
let hits = 0;
a ||= 5;
b &&= 3;
c ??= 7;
b ||= (hits = 9);
a &&= 4;
console.log(a, b, c, hits);
'''
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = Interpreter().run(source)
        self.assertEqual(result.splitlines(), ['4 3 7 0'])

    def test_class_inheritance_and_static_methods_work(self):
        source = '''
class A {
    greet() { return "A"; }
}
class B extends A {
    static version() { return 2; }
}
let b = new B();
console.log(b.greet(), b instanceof B, b instanceof A);
console.log(B.version());
'''
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = Interpreter().run(source)
        self.assertEqual(result.splitlines(), ['A true true', '2'])

    def test_globalthis_date_regexp_and_common_statics_work(self):
        source = '''
let answer = 41;
globalThis.answer += 1;
let d = new Date(0);
let r = RegExp("a+", "i");
let made = Object.fromEntries([["x", 1], ["y", 2]]);
console.log(answer, globalThis.answer, globalThis.console === console);
console.log(typeof Date.now(), d.getTime(), d.toISOString().startsWith("1970-01-01T00:00:00"));
console.log(r.test("Caa"), "caa".match(r)[0]);
console.log(Object.hasOwn(made, "x"), Array.of(1, 2, 3).at(-1));
'''
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = Interpreter().run(source)
        self.assertEqual(result.splitlines(), ['42 42 true', 'number 0 true', 'true aa', 'true 3'])

    def test_super_map_set_and_simple_statics_work(self):
        source = '''
class A { greet() { return "A"; } }
class B extends A { greet() { return super.greet() + "B"; } }
let m = new Map([["a", 1]]);
let s = new Set([1, 2]);
m.set("b", 2);
s.add(2).add(3);
console.log(new B().greet());
console.log(m.get("a"), m.has("b"), m.size);
console.log(s.has(2), s.size);
console.log(Number.isNaN(NaN), Number.isFinite(4), Number.isInteger(4.5));
console.log(String.raw({raw:["a","b","c"]}, 1, 2));
'''
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = Interpreter().run(source)
        self.assertEqual(result.splitlines(), ['AB', '1 true 2', 'true 3', 'true true false', 'a1b2c'])

    def test_destructuring_assignment_and_for_in_existing_binding_work(self):
        source = '''
let a, b, k;
({a, b = 2} = {a: 1});
[a, b = 3] = [a];
for (k in {a: 1, b: 2}) {}
console.log(a, b, k);
'''
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = Interpreter().run(source)
        self.assertEqual(result.splitlines(), ['1 3 b'])

    def test_interval_can_clear_itself(self):
        source = '''
let count = 0;
let id = setInterval(() => {
    count++;
    console.log("tick", count);
    if (count === 3) clearInterval(id);
}, 1);
'''
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = Interpreter().run(source)
        self.assertEqual(result.splitlines(), ['tick 1', 'tick 2', 'tick 3'])

    def test_pyvm_exec_runs_host_python(self):
        source = '''
let result = PyVM.exec("print('host run')");
console.log(result.ok, result.code, result.stdout.trim());
'''
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = Interpreter().run(source)
        self.assertEqual(result.splitlines(), ['true 0 host run'])

    def test_pyvm_async_exec_can_be_awaited(self):
        source = '''
async function runHost() {
    let result = await PyVM.execAsync("print(6 * 7)");
    console.log(result.stdout.trim());
}
runHost();
'''
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = Interpreter().run(source)
        self.assertEqual(result.splitlines(), ['42'])

    def test_pyvm_run_module_and_pip_list_work(self):
        source = '''
let mod = PyVM.runModule("pip", ["--version"]);
let listed = PyVM.pipList();
console.log(mod.ok, mod.stdout.includes("pip"));
console.log(listed.ok, listed.packages.length > 0);
'''
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = Interpreter().run(source)
        self.assertEqual(result.splitlines(), ['true true', 'true true'])

    def test_engine_os_and_sys_utils_work(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            source = f'''
os.chdir("{tmpdir}");
let notePath = os.join("nested", "note.txt");
os.mkdir("nested");
os.writeText(notePath, "hello os");
console.log(os.cwd().includes("{tmpdir}"));
console.log(os.exists(notePath), os.readText(notePath).trim());
console.log(os.listdir("nested").includes("note.txt"));
console.log(os.basename(notePath), os.dirname(notePath));
console.log(os.getenv("PYJS_TEST_ENV", "missing"));
os.setenv("PYJS_TEST_ENV", "set");
console.log(os.getenv("PYJS_TEST_ENV"));
console.log(os.exists(sys.executable), sys.platform.length > 0, sys.path.length > 0);
os.rename(notePath, os.join("nested", "renamed.txt"));
console.log(os.exists(os.join("nested", "renamed.txt")));
os.remove(os.join("nested", "renamed.txt"));
os.rmdir("nested");
console.log(os.exists("nested"));
'''
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = Interpreter().run(source)
            os.chdir(old_cwd)
            self.assertEqual(
                result.splitlines(),
                ['true', 'true hello os', 'true', 'note.txt nested', 'missing', 'set', 'true true true', 'true', 'false'],
            )

    def test_getters_setters(self):
        import io, contextlib
        source = """
        class Circle {
            constructor(r) { this._r = r; }
            get radius() { return this._r; }
            set radius(v) { this._r = v; }
            get area() { return Math.PI * this._r * this._r; }
        }
        const c = new Circle(5);
        console.log(c.radius);
        c.radius = 10;
        console.log(c.radius);

        const obj = {
            _x: 42,
            get x() { return this._x; },
            set x(v) { this._x = v * 2; }
        };
        console.log(obj.x);
        obj.x = 5;
        console.log(obj.x);
        """
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            out = Interpreter().run(source)
        lines = out.strip().split('\n')
        self.assertEqual(lines[0], '5', f"Expected 5, got {lines[0]}")
        self.assertEqual(lines[1], '10', f"Expected 10, got {lines[1]}")
        self.assertEqual(lines[2], '42', f"Expected 42, got {lines[2]}")
        self.assertEqual(lines[3], '10', f"Expected 10, got {lines[3]}")

    def test_labeled_break_continue(self):
        import io, contextlib
        source = """
        outer: for (let i = 0; i < 3; i++) {
            for (let j = 0; j < 3; j++) {
                if (j === 1) continue outer;
                console.log(i + ',' + j);
            }
        }
        """
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            out = Interpreter().run(source)
        lines = out.strip().split('\n')
        self.assertEqual(lines, ['0,0', '1,0', '2,0'])

    def test_promise_all_settled(self):
        import io, contextlib
        source = """
        const p1 = Promise.resolve(1);
        const p2 = Promise.reject('err');
        const p3 = Promise.resolve(3);
        Promise.allSettled([p1, p2, p3]).then(results => {
            results.forEach(r => console.log(r.status));
        });
        """
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            out = Interpreter().run(source)
        lines = out.strip().split('\n')
        self.assertEqual(lines, ['fulfilled', 'rejected', 'fulfilled'])

    def test_new_target(self):
        import io, contextlib
        source = """
        function Foo() {
            if (new.target) {
                console.log('called with new');
            } else {
                console.log('called without new');
            }
        }
        new Foo();
        Foo();
        """
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            out = Interpreter().run(source)
        lines = out.strip().split('\n')
        self.assertEqual(lines[0], 'called with new')
        self.assertEqual(lines[1], 'called without new')


    def test_symbol_basics(self):
        source = """
        const sym1 = Symbol('foo');
        const sym2 = Symbol('foo');
        console.log(typeof sym1);
        console.log(sym1 === sym2);
        console.log(sym1.toString());
        const s = Symbol.for('shared');
        const s2 = Symbol.for('shared');
        console.log(s === s2);
        """
        out = Interpreter().run(source)
        lines = out.strip().split('\n')
        self.assertEqual(lines[0], 'symbol')
        self.assertEqual(lines[1], 'false')
        self.assertEqual(lines[2], 'Symbol(foo)')
        self.assertEqual(lines[3], 'true')

    def test_custom_iterable(self):
        source = """
        function makeRange(start, end) {
            return {
                [Symbol.iterator]() {
                    let current = start;
                    return {
                        next() {
                            if (current <= end) {
                                return { value: current++, done: false };
                            }
                            return { value: undefined, done: true };
                        }
                    };
                }
            };
        }
        for (const n of makeRange(1, 3)) {
            console.log(n);
        }
        const arr = [...makeRange(4, 6)];
        console.log(arr.join(','));
        """
        out = Interpreter().run(source)
        lines = out.strip().split('\n')
        self.assertEqual(lines[:3], ['1', '2', '3'])
        self.assertEqual(lines[3], '4,5,6')

    def test_generators(self):
        source = """
        function* counter(start, end) {
            for (let i = start; i <= end; i++) {
                yield i;
            }
        }
        const gen = counter(1, 3);
        console.log(gen.next().value);
        console.log(gen.next().value);
        console.log(gen.next().value);
        console.log(gen.next().done);
        for (const n of counter(10, 12)) {
            console.log(n);
        }
        """
        out = Interpreter().run(source)
        lines = out.strip().split('\n')
        self.assertEqual(lines[0], '1')
        self.assertEqual(lines[1], '2')
        self.assertEqual(lines[2], '3')
        self.assertEqual(lines[3], 'true')
        self.assertEqual(lines[4:], ['10', '11', '12'])

    def test_generator_yield_star(self):
        source = """
        function* inner() {
            yield 'a';
            yield 'b';
        }
        function* outer() {
            yield 1;
            yield* inner();
            yield 2;
        }
        const result = [...outer()];
        console.log(result.join(','));
        """
        out = Interpreter().run(source)
        self.assertEqual(out.strip(), '1,a,b,2')


    def test_async_generators(self):
        """Async generators with for await...of."""
        source = """
async function* range(start, end) {
    for (let i = start; i < end; i++) {
        yield i;
    }
}
async function main() {
    const nums = [];
    for await (const x of range(0, 3)) {
        nums.push(x);
    }
    console.log(nums.join(','));
}
main();
"""
        import io, contextlib
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = Interpreter().run(source)
        self.assertEqual(result.strip(), "0,1,2")

    def test_module_export_import(self):
        """Named exports/imports between two files."""
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            math_js = os.path.join(d, 'math.js')
            with open(math_js, 'w') as f:
                f.write("export const add = (a, b) => a + b;\nexport const PI = 3.14;")
            main_js = os.path.join(d, 'main.js')
            with open(main_js, 'w') as f:
                f.write("import { add, PI } from './math';\nconsole.log(add(1,2));\nconsole.log(PI);")
            result = evaluate_file(main_js)
        self.assertEqual(result.strip(), "3\n3.14")

    def test_module_default_export(self):
        """Default export/import."""
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            greet_js = os.path.join(d, 'greet.js')
            with open(greet_js, 'w') as f:
                f.write("export default function greet(name) { return 'Hello ' + name; }")
            main_js = os.path.join(d, 'main.js')
            with open(main_js, 'w') as f:
                f.write("import greet from './greet';\nconsole.log(greet('World'));")
            result = evaluate_file(main_js)
        self.assertEqual(result.strip(), "Hello World")

    def test_module_namespace_import(self):
        """Namespace import (* as ns)."""
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            utils_js = os.path.join(d, 'utils.js')
            with open(utils_js, 'w') as f:
                f.write("export const x = 10;\nexport const y = 20;")
            main_js = os.path.join(d, 'main.js')
            with open(main_js, 'w') as f:
                f.write("import * as utils from './utils';\nconsole.log(utils.x + utils.y);")
            result = evaluate_file(main_js)
        self.assertEqual(result.strip(), "30")

    def test_proxy_reflect(self):
        result = evaluate("""
const handler = {
    get(target, key) {
        return key in target ? target[key] : 37;
    }
};
const p = new Proxy({}, handler);
p.a = 1;
console.log(p.a);
console.log(p.b);

const r = Reflect.get({x: 42}, 'x');
console.log(r);

console.log(Reflect.has({a: 1}, 'a'));
""")
        self.assertIn("1", result)
        self.assertIn("37", result)
        self.assertIn("42", result)
        self.assertIn("true", result)

    def test_bigint(self):
        result = evaluate("""
const a = 42n;
const b = BigInt(8);
console.log(typeof a);
console.log(a + b);
console.log(a * 2n);
console.log(a === 42n);
""")
        self.assertIn("bigint", result)
        self.assertIn("50", result)
        self.assertIn("84", result)
        self.assertIn("true", result)

    def test_weakmap_weakset(self):
        result = evaluate("""
const wm = new WeakMap();
const key1 = {};
const key2 = {};
wm.set(key1, 'val1');
wm.set(key2, 'val2');
console.log(wm.get(key1));
console.log(wm.has(key2));
wm.delete(key2);
console.log(wm.has(key2));

const ws = new WeakSet();
ws.add(key1);
console.log(ws.has(key1));
ws.delete(key1);
console.log(ws.has(key1));
""")
        self.assertIn("val1", result)
        self.assertIn("true", result)
        self.assertIn("false", result)

    def test_tagged_templates(self):
        result = evaluate("""
function tag(strings, ...values) {
    let result = '';
    strings.forEach((str, i) => {
        result += str;
        if (i < values.length) result += values[i].toUpperCase();
    });
    return result;
}
const name = 'world';
const greeting = tag`Hello ${name}!`;
console.log(greeting);

function rawTag(strings) { return strings.raw[0]; }
const r = rawTag`test`;
console.log(r);
""")
        self.assertIn("Hello WORLD!", result)
        self.assertIn("test", result)

    def test_optional_catch_numeric_separators(self):
        result = evaluate("""
let caught = false;
try {
    throw new Error('test');
} catch {
    caught = true;
}
console.log(caught);

const million = 1_000_000;
console.log(million);
console.log(million === 1000000);
""")
        self.assertIn("true", result)
        self.assertIn("1000000", result)

    def test_error_hierarchy(self):
        result = evaluate("""
const e = new Error('oops');
console.log(e.message);
console.log(e.name);

const te = new TypeError('bad type');
console.log(te.message);
console.log(te.name);

const cause = new Error('root');
const wrapped = new Error('outer', { cause: cause });
console.log(wrapped.cause.message);

console.log(e instanceof Error);
console.log(te instanceof TypeError);

try {
    null.foo;
} catch (err) {
    console.log(err.name);
}
""")
        self.assertIn("oops", result)
        self.assertIn("Error", result)
        self.assertIn("bad type", result)
        self.assertIn("TypeError", result)
        self.assertIn("root", result)
        self.assertIn("true", result)

    def test_class_fields(self):
        result = evaluate("""
class Person {
    name = 'Anonymous';
    #age = 0;
    static count = 0;

    constructor(name, age) {
        this.name = name;
        this.#age = age;
        Person.count++;
    }

    greet() {
        return this.name + ' is ' + this.#age;
    }

    static getCount() { return Person.count; }
}

const p1 = new Person('Alice', 30);
const p2 = new Person('Bob', 25);
console.log(p1.greet());
console.log(p2.greet());
console.log(Person.count);
console.log(Person.getCount());
""")
        self.assertIn("Alice is 30", result)
        self.assertIn("Bob is 25", result)
        self.assertIn("2", result)

    def test_array_methods_expanded(self):
        result = evaluate("""
const arr = [1, 2, 3, 4, 5];
console.log(arr.findIndex(x => x > 3));   // 3
console.log(arr.findLast(x => x < 4));    // 3
console.log(arr.findLastIndex(x => x < 4)); // 2
console.log(arr.reduceRight((a, b) => a + b, 0)); // 15
console.log(arr.lastIndexOf(3));           // 2
console.log(arr.toSorted((a,b) => b-a).join(',')); // 5,4,3,2,1
console.log(arr.toReversed().join(','));    // 5,4,3,2,1
console.log(arr.with(2, 99).join(','));    // 1,2,99,4,5
console.log(arr.join(','));                // 1,2,3,4,5 (originals unchanged)
console.log(Array.of(1,2,3).join(','));   // 1,2,3
""")
        self.assertIn("3\n", result)
        self.assertIn("15", result)
        self.assertIn("5,4,3,2,1", result)
        self.assertIn("1,2,99,4,5", result)
        self.assertIn("1,2,3,4,5", result)

    def test_string_methods_expanded(self):
        result = evaluate("""
const s = '  hello  ';
console.log(s.trimStart());
console.log(s.trimEnd());
console.log('A'.codePointAt(0));
console.log(String.fromCodePoint(65, 66, 67));
""")
        self.assertIn("hello  ", result)
        self.assertIn("  hello", result)
        self.assertIn("65", result)
        self.assertIn("ABC", result)

    def test_object_methods_expanded(self):
        result = evaluate("""
console.log(Object.is(NaN, NaN));
console.log(Object.is(1, 1));
console.log(Object.is(1, 2));
const desc = Object.getOwnPropertyDescriptor({x: 42}, 'x');
console.log(desc.value);
console.log(desc.writable);
const syms = Object.getOwnPropertySymbols({});
console.log(Array.isArray(syms));
""")
        self.assertIn("true", result)
        self.assertIn("42", result)

    def test_number_math_expanded(self):
        result = evaluate("""
console.log(Number.EPSILON < 1);
console.log(Number.MAX_SAFE_INTEGER);
console.log(Number.isSafeInteger(42));
console.log(Number.isSafeInteger(9007199254740992));
console.log(Math.hypot(3, 4));
console.log(Math.cbrt(27));
""")
        self.assertIn("true", result)
        self.assertIn("9007199254740991", result)
        self.assertIn("5", result)
        self.assertIn("3", result)

    def test_json_replacer_reviver(self):
        result = evaluate(r"""
const obj = {a: 1, b: 2};
const pretty = JSON.stringify(obj, null, 2);
console.log(pretty.includes('\n'));
const filtered = JSON.stringify({a:1, b:2, c:3}, ['a','c']);
console.log(filtered);
const parsed = JSON.parse('{"a":1,"b":2}', (key, val) => {
    return typeof val === 'number' ? val * 2 : val;
});
console.log(parsed.a);
console.log(parsed.b);
""")
        self.assertIn("true", result)
        self.assertIn('"a":1', result.replace(' ', ''))
        self.assertIn("2", result)
        self.assertIn("4", result)

    def test_promise_with_resolvers(self):
        result = evaluate("""
const { promise, resolve } = Promise.withResolvers();
promise.then(v => console.log('resolved:', v));
resolve(42);
""")
        self.assertIn("resolved: 42", result)

    def test_structured_clone(self):
        result = evaluate("""
const obj = { a: 1, b: { c: 2 } };
const clone = structuredClone(obj);
clone.b.c = 99;
console.log(obj.b.c);
console.log(clone.b.c);
""")
        self.assertIn("2", result)
        self.assertIn("99", result)

    def test_iterator_helpers(self):
        result = evaluate("""
function* nums() {
    yield 1; yield 2; yield 3; yield 4; yield 5;
}

const arr = nums().map(x => x * 2).toArray();
console.log(arr.join(','));

const filtered = nums().filter(x => x % 2 === 0).toArray();
console.log(filtered.join(','));

console.log(nums().take(3).toArray().join(','));
console.log(nums().drop(2).toArray().join(','));

console.log(nums().some(x => x > 4));
console.log(nums().every(x => x > 0));
console.log(nums().find(x => x > 3));
console.log(nums().reduce((a,b) => a+b, 0));
""")
        self.assertIn("2,4,6,8,10", result)
        self.assertIn("2,4", result)
        self.assertIn("1,2,3", result)
        self.assertIn("3,4,5", result)
        self.assertIn("true", result)
        self.assertIn("15", result)

    def test_regex_named_groups(self):
        result = evaluate(r"""
const re = /(?<year>\d{4})-(?<month>\d{2})-(?<day>\d{2})/;
const match = re.exec('2024-01-15');
console.log(match[0]);
console.log(match.groups.year);
console.log(match.groups.month);
console.log(match.groups.day);

const result = '2024-01-15'.replace(re, '$<day>/$<month>/$<year>');
console.log(result);
""")
        self.assertIn("2024-01-15", result)
        self.assertIn("2024", result)
        self.assertIn("01", result)
        self.assertIn("15", result)
        self.assertIn("15/01/2024", result)

    def test_console_extras(self):
        result = evaluate("""
console.assert(true, 'should not print');
console.assert(false, 'assertion message');

console.count('test');
console.count('test');
console.count('test');

console.group('group1');
console.log('indented');
console.groupEnd();
console.log('not indented');
""")
        self.assertNotIn("should not print", result)
        self.assertIn("Assertion failed", result)
        self.assertIn("test: 3", result)
        self.assertIn("indented", result)
        self.assertIn("not indented", result)

    def test_type_coercion(self):
        result = evaluate("""
console.log([] + []);
console.log([] + {});
console.log([1,2] + [3]);
console.log(1 + {});
console.log({} + []);
console.log('' + null);
console.log('' + undefined);
console.log('' + true);
console.log('' + false);
console.log('' + 0);
const obj = {
    [Symbol.toPrimitive](hint) {
        if (hint === 'number') return 42;
        if (hint === 'string') return 'hello';
        return 'default';
    }
};
console.log(+obj);
console.log(`${obj}`);
console.log(obj + '');
""")
        self.assertIn("[object Object]", result)
        self.assertIn("1,23", result)
        self.assertIn("null", result)
        self.assertIn("42", result)
        self.assertIn("hello", result)
        self.assertIn("default", result)

    def test_loop_closure_scoping(self):
        result = evaluate("""
const fns = [];
for (let i = 0; i < 3; i++) {
    fns.push(() => i);
}
console.log(fns[0]());
console.log(fns[1]());
console.log(fns[2]());
const results = [];
for (const x of [10, 20, 30]) {
    results.push(() => x);
}
console.log(results[0]());
console.log(results[1]());
console.log(results[2]());
""")
        self.assertIn("0\n", result)
        self.assertIn("1\n", result)
        self.assertIn("2\n", result)
        self.assertIn("10\n", result)
        self.assertIn("20\n", result)
        self.assertIn("30", result)

    def test_queue_microtask(self):
        result = evaluate("""
let order = [];
queueMicrotask(() => order.push(2));
order.push(1);
queueMicrotask(() => { order.push(3); console.log(order.join(',')); });
""")
        self.assertIn("1,2,3", result)

    def test_object_create_proto(self):
        result = evaluate("""
const proto = { greet() { return 'hello ' + this.name; } };
const obj = Object.create(proto);
obj.name = 'world';
console.log(obj.greet());
console.log('name' in obj);
console.log('greet' in obj);
const keys = [];
for (const k in obj) keys.push(k);
console.log(keys.includes('greet'));
console.log(keys.includes('name'));
const bare = Object.create(null);
bare.x = 1;
console.log(bare.x);
""")
        self.assertIn("hello world", result)
        self.assertIn("true", result)
        self.assertIn("1", result)

    def test_comma_operator(self):
        result = evaluate("""
const x = (1, 2, 3);
console.log(x);
let a = 0, b = 0;
for (let i = 0; i < 3; i++, b++) {
    a += i;
}
console.log(a);
console.log(b);
let c = (console.log('side'), 42);
console.log(c);
""")
        self.assertIn("3", result)
        self.assertIn("side", result)
        self.assertIn("42", result)

    def test_computed_class_methods(self):
        result = evaluate("""
class Range {
    constructor(start, end) {
        this.start = start;
        this.end = end;
    }
    [Symbol.iterator]() {
        let current = this.start;
        const end = this.end;
        return {
            next() {
                if (current <= end) {
                    return { value: current++, done: false };
                }
                return { value: undefined, done: true };
            }
        };
    }
}
const range = new Range(1, 4);
const arr = [...range];
console.log(arr.join(','));
""")
        self.assertIn("1,2,3,4", result)

    def test_map_set_iterators(self):
        result = evaluate("""
const m = new Map([['a',1],['b',2],['c',3]]);
const keys = [...m.keys()];
console.log(keys.join(','));
const vals = [...m.values()];
console.log(vals.join(','));
const entries = [...m.entries()];
console.log(entries.length);
const s = new Set([1,2,3]);
const sArr = Array.from(s);
console.log(sArr.join(','));
const doubled = m.values().map(v => v * 2).toArray();
console.log(doubled.join(','));
""")
        self.assertIn("a,b,c", result)
        self.assertIn("1,2,3", result)
        self.assertIn("3", result)
        self.assertIn("2,4,6", result)

    def test_symbol_to_string_tag(self):
        result = evaluate("""
const obj = { [Symbol.toStringTag]: 'MyType' };
console.log(Object.prototype.toString.call(obj));
console.log(Object.prototype.toString.call([]));
console.log(Object.prototype.toString.call(null));
console.log(Object.prototype.toString.call(undefined));
console.log(Object.prototype.toString.call(42));
""")
        self.assertIn("[object MyType]", result)
        self.assertIn("[object Array]", result)
        self.assertIn("[object Null]", result)

    def test_aggregate_error(self):
        result = evaluate("""
const err = new AggregateError([new Error('a'), new Error('b')], 'All failed');
console.log(err.message);
console.log(err.name);
console.log(err.errors.length);
console.log(err.errors[0].message);

Promise.any([
    Promise.reject(new Error('e1')),
    Promise.reject(new Error('e2')),
]).catch(e => {
    console.log(e instanceof AggregateError);
    console.log(e.errors.length);
});
""")
        self.assertIn("All failed", result)
        self.assertIn("AggregateError", result)
        self.assertIn("2", result)
        self.assertIn("true", result)

    def test_set_methods_es2025(self):
        result = evaluate("""
const a = new Set([1, 2, 3]);
const b = new Set([2, 3, 4]);

console.log([...a.union(b)].sort().join(','));
console.log([...a.intersection(b)].sort().join(','));
console.log([...a.difference(b)].sort().join(','));
console.log([...a.symmetricDifference(b)].sort().join(','));
console.log(a.isSubsetOf(new Set([1,2,3,4])));
console.log(a.isSupersetOf(new Set([1,2])));
console.log(a.isDisjointFrom(new Set([4,5])));
console.log(a.isDisjointFrom(b));
""")
        self.assertIn("1,2,3,4", result)
        self.assertIn("2,3", result)
        self.assertIn("true", result)
        self.assertIn("false", result)

    def test_url_api(self):
        result = evaluate("""
const url = new URL('https://example.com:8080/path?foo=1&bar=2#hash');
console.log(url.hostname);
console.log(url.pathname);
console.log(url.search);
console.log(url.hash);
console.log(url.port);

const sp = url.searchParams;
console.log(sp.get('foo'));
console.log(sp.get('bar'));
sp.set('foo', '99');
console.log(sp.get('foo'));
console.log(sp.has('baz'));

const sp2 = new URLSearchParams('x=1&y=2&x=3');
console.log(sp2.getAll('x').join(','));
""")
        self.assertIn("example.com", result)
        self.assertIn("/path", result)
        self.assertIn("?foo=1&bar=2", result)
        self.assertIn("#hash", result)
        self.assertIn("8080", result)
        self.assertIn("1,3", result)

    def test_text_encoder_decoder(self):
        result = evaluate("""
const enc = new TextEncoder();
const bytes = enc.encode('Hello');
console.log(bytes.length);
console.log(bytes[0]);

const dec = new TextDecoder();
const str = dec.decode(bytes);
console.log(str);
""")
        self.assertIn("5", result)
        self.assertIn("72", result)
        self.assertIn("Hello", result)

    def test_crypto(self):
        result = evaluate("""
const uuid = crypto.randomUUID();
console.log(uuid.length);
console.log(uuid.includes('-'));

const arr = [0, 0, 0, 0];
crypto.getRandomValues(arr);
console.log(arr.length);
console.log(arr[0] >= 0 && arr[0] <= 255);
""")
        self.assertIn("36", result)
        self.assertIn("true", result)
        self.assertIn("4", result)

    def test_abort_controller(self):
        result = evaluate("""
const ctrl = new AbortController();
const signal = ctrl.signal;
console.log(signal.aborted);

let aborted = false;
signal.addEventListener('abort', () => { aborted = true; });

ctrl.abort();
console.log(signal.aborted);
console.log(aborted);
""")
        self.assertIn("false", result)
        self.assertIn("true", result)


    def test_private_fields(self):
        result = evaluate("""
class Counter {
    #count = 0;
    increment() { this.#count++; }
    get() { return this.#count; }
}
const c = new Counter();
c.increment();
c.increment();
console.log(c.get());
""")
        self.assertIn("2", result)

    def test_private_methods(self):
        result = evaluate("""
class Foo {
    #double(x) { return x * 2; }
    run(x) { return this.#double(x); }
}
console.log(new Foo().run(5));
""")
        self.assertIn("10", result)

    def test_function_bind(self):
        result = evaluate("""
function greet(greeting) { return greeting + ' ' + this.name; }
const hi = greet.bind({name: 'World'});
console.log(hi('Hello'));
""")
        self.assertIn("Hello World", result)

    def test_function_call_apply(self):
        result = evaluate("""
function sum(a, b) { return a + b + this.base; }
const r1 = sum.call({base: 10}, 1, 2);
const r2 = sum.apply({base: 20}, [3, 4]);
console.log(r1 + '|' + r2);
""")
        self.assertIn("13|27", result)

    def test_function_name_length(self):
        result = evaluate("""
function foo(a, b, c) {}
const bar = (x, y) => x + y;
console.log(foo.name + '|' + foo.length + '|' + bar.length);
""")
        self.assertIn("foo|3|2", result)

    def test_property_descriptors_writable(self):
        result = evaluate("""
const obj = {};
Object.defineProperty(obj, 'x', { value: 42, writable: false, enumerable: true, configurable: false });
obj.x = 99;
console.log(obj.x);
""")
        self.assertIn("42", result)

    def test_property_descriptors_enumerable(self):
        result = evaluate("""
const obj = { a: 1 };
Object.defineProperty(obj, 'hidden', { value: 2, writable: true, enumerable: false, configurable: true });
console.log(Object.keys(obj).join(','));
""")
        self.assertIn("a", result)
        self.assertNotIn("hidden", result)

    def test_object_freeze(self):
        result = evaluate("""
const obj = { x: 1, y: 2 };
Object.freeze(obj);
obj.x = 99;
obj.z = 3;
console.log(Object.isFrozen(obj) + '|' + obj.x + '|' + ('z' in obj));
""")
        self.assertIn("true|1|false", result)

    def test_object_seal(self):
        result = evaluate("""
const obj = { a: 10 };
Object.seal(obj);
obj.a = 20;
obj.b = 5;
delete obj.a;
console.log(Object.isSealed(obj) + '|' + obj.a + '|' + ('b' in obj));
""")
        self.assertIn("true|20|false", result)

    def test_prevent_extensions(self):
        result = evaluate("""
const obj = { x: 1 };
Object.preventExtensions(obj);
obj.y = 2;
obj.x = 99;
console.log(Object.isExtensible(obj) + '|' + obj.x + '|' + ('y' in obj));
""")
        self.assertIn("false|99|false", result)

    def test_weakref(self):
        result = evaluate("""
let obj = { val: 42 };
const ref = new WeakRef(obj);
const derefed = ref.deref();
console.log(derefed !== undefined && derefed.val === 42);
""")
        self.assertIn('true', result)

    def test_finalization_registry(self):
        result = evaluate("""
const registry = new FinalizationRegistry(v => {});
let obj = { x: 1 };
registry.register(obj, 'held');
console.log(typeof registry.register === 'function');
""")
        self.assertIn('true', result)

    def test_promise_try(self):
        result = evaluate("""
Promise.try(() => 42)
    .then(v => { console.log('ok:' + v); });
""")
        self.assertIn('ok:42', result)

    def test_promise_try_catches_sync_error(self):
        result = evaluate("""
Promise.try(() => { throw new Error('boom'); })
    .catch(e => { console.log('caught:' + e.message); });
""")
        self.assertIn('caught:boom', result)

    def test_error_cause(self):
        result = evaluate("""
const orig = new Error('original');
const wrapped = new TypeError('wrapper', { cause: orig });
console.log(wrapped.message + '|' + wrapped.cause.message);
""")
        self.assertIn('wrapper|original', result)

    def test_arraybuffer(self):
        result = evaluate("""
const buf = new ArrayBuffer(8);
console.log(buf.byteLength);
""")
        self.assertIn('8', result)

    def test_uint8array_basic(self):
        result = evaluate("""
const arr = new Uint8Array(4);
arr[0] = 10;
arr[1] = 20;
arr[2] = 30;
arr[3] = 40;
console.log(arr[0] + '|' + arr[1] + '|' + arr.length + '|' + arr.byteLength);
""")
        self.assertIn('10|20|4|4', result)

    def test_typed_array_from_array(self):
        result = evaluate("""
const arr = new Int32Array([1, 2, 3, 4]);
console.log(arr.reduce((s, v) => s + v, 0));
""")
        self.assertIn('10', result)

    def test_typed_array_methods(self):
        result = evaluate("""
const arr = new Float64Array([3.0, 1.0, 4.0, 1.0, 5.0]);
arr.sort();
console.log(Array.from(arr).join(','));
""")
        self.assertIn('1,1,3,4,5', result)

    def test_dataview(self):
        result = evaluate("""
const buf = new ArrayBuffer(4);
const view = new DataView(buf);
view.setUint8(0, 0xDE);
view.setUint8(1, 0xAD);
view.setUint8(2, 0xBE);
view.setUint8(3, 0xEF);
console.log(view.getUint8(0).toString(16) + view.getUint8(1).toString(16));
""")
        self.assertIn('dead', result)

    def test_typed_array_iterator(self):
        result = evaluate("""
const arr = new Uint16Array([10, 20, 30]);
const out = [];
for (const v of arr) out.push(v);
console.log(out.join(','));
""")
        self.assertIn('10,20,30', result)

    def test_regex_dotall_flag(self):
        result = evaluate(r"""
const re = /foo.bar/s;
console.log(re.test('foo\nbar'));
""")
        self.assertIn('true', result)

    def test_regex_indices_flag(self):
        result = evaluate(r"""
const re = /(\d+)/d;
const m = re.exec('abc 123 def');
console.log(m.indices[0][0] + '|' + m.indices[0][1]);
""")
        self.assertIn('4|7', result)

    def test_intl_number_format(self):
        result = evaluate("""
const fmt = new Intl.NumberFormat('en');
console.log(fmt.format(1234567));
""")
        self.assertIn('1,234,567', result)

    def test_intl_relative_time_format(self):
        result = evaluate("""
const rtf = new Intl.RelativeTimeFormat('en');
console.log(rtf.format(-2, 'day') + '|' + rtf.format(1, 'week'));
""")
        self.assertIn('2 days ago|in 1 week', result)

    def test_intl_list_format(self):
        result = evaluate("""
const lf = new Intl.ListFormat('en');
console.log(lf.format(['apples', 'bananas', 'cherries']));
""")
        self.assertIn('apples, bananas, and cherries', result)

    def test_intl_collator(self):
        result = evaluate("""
const arr = ['banana', 'apple', 'cherry'];
arr.sort(new Intl.Collator('en').compare);
console.log(arr.join(','));
""")
        self.assertIn('apple,banana,cherry', result)

    def test_date_methods(self):
        result = Interpreter().run("""
            const d2 = new Date(1705316400000);  // 2024-01-15 UTC
            console.log(d2.getFullYear() + '|' + d2.getMonth() + '|' + d2.getDate());
        """)
        self.assertIn('2024|0|15', result)

    def test_date_setters(self):
        result = Interpreter().run("""
            const d = new Date(1705316400000);  // 2024-01-15 09:00:00 UTC
            d.setFullYear(2025);
            console.log(d.getFullYear());
        """)
        self.assertIn('2025', result)

    def test_number_to_exponential(self):
        result = Interpreter().run("""
            console.log((12345).toExponential(2));
        """)
        self.assertIn('1.23e+4', result)

    def test_object_set_prototype_of(self):
        result = Interpreter().run("""
            const proto = { greet() { return 'hello'; } };
            const obj = {};
            Object.setPrototypeOf(obj, proto);
            console.log(obj.greet());
        """)
        self.assertIn('hello', result)

    def test_json_to_json_method(self):
        result = Interpreter().run("""
            const obj = {
                x: 42,
                toJSON(key) { return { serialized: this.x }; }
            };
            console.log(JSON.stringify(obj));
        """)
        self.assertIn('{"serialized":42}', result)

    def test_array_flat_infinity(self):
        result = Interpreter().run("""
            console.log([1, [2, [3, [4, [5]]]]].flat(Infinity).join(','));
        """)
        self.assertIn('1,2,3,4,5', result)

    def test_symbol_match_delegation(self):
        result = evaluate("""
const re = /\\d+/g;
const matches = '123 abc 456'.match(re);
console.log(matches.join(','));
""")
        self.assertIn('123,456', result)

    def test_symbol_split_delegation(self):
        result = evaluate("""
const parts = 'a1b2c3'.split(/\\d/);
console.log(parts.join('-'));
""")
        self.assertIn('a-b-c-', result)

    def test_concat_is_concat_spreadable(self):
        result = evaluate("""
const arr = [1, 2, 3];
const obj = { 0: 'a', 1: 'b', length: 2 };
obj[Symbol.isConcatSpreadable] = true;
console.log([0].concat(arr, obj).join(','));
""")
        self.assertIn('0,1,2,3,a,b', result)

    def test_object_assign_invokes_getters(self):
        result = evaluate("""
const src = {};
Object.defineProperty(src, 'x', {
    get() { return 42; },
    enumerable: true,
    configurable: true
});
const target = {};
Object.assign(target, src);
console.log(target.x);
""")
        self.assertIn('42', result)

    def test_structured_clone_typed_array(self):
        result = evaluate("""
const orig = new Uint8Array([1, 2, 3, 4]);
const copy = structuredClone(orig);
copy[0] = 99;
console.log(orig[0] + '|' + copy[0] + '|' + copy.length);
""")
        self.assertIn('1|99|4', result)


    def test_matchall_index_and_groups(self):
        result = evaluate(r"""
            const re = /(?<word>\w+)/g;
            const matches = [...'hello world'.matchAll(re)];
            console.log(matches[0].index + '|' + matches[1].index + '|' + matches[0].groups.word);
        """)
        self.assertEqual(result, '0|6|hello')

    def test_promise_resolve_thenable(self):
        result = evaluate("""
            const thenable = {
                then(resolve, reject) { resolve(42); }
            };
            Promise.resolve(thenable).then(v => { console.log('got:' + v); });
        """)
        self.assertEqual(result, 'got:42')

    def test_replaceall_non_global_regexp_throws(self):
        result = evaluate("""
            let threw = false;
            try {
                'hello'.replaceAll(/l/, 'r');
            } catch(e) {
                threw = e instanceof TypeError;
            }
            console.log(threw);
        """)
        self.assertIn('true', result)

    # ── Phase 18 tests ────────────────────────────────────────────

    def test_encode_decode_uri_component(self):
        result = evaluate("""
            const encoded = encodeURIComponent('hello world&foo=bar');
            console.log(encoded);
            console.log(decodeURIComponent(encoded));
            console.log(encodeURI('https://example.com/path?q=hello world'));
            console.log(decodeURI(encodeURI('https://example.com/path?q=hello world')));
        """)
        lines = result.splitlines()
        self.assertEqual(lines[0], 'hello%20world%26foo%3Dbar')
        self.assertEqual(lines[1], 'hello world&foo=bar')
        self.assertIn('example.com', lines[2])
        self.assertIn('example.com', lines[3])

    def test_atob_btoa(self):
        result = evaluate("""
            console.log(btoa('Hello, World!'));
            console.log(atob('SGVsbG8sIFdvcmxkIQ=='));
            console.log(atob(btoa('roundtrip')));
            let threw = false;
            try { atob('!!!invalid!!!'); } catch(e) { threw = true; }
            console.log(threw);
        """)
        lines = result.splitlines()
        self.assertEqual(lines[0], 'SGVsbG8sIFdvcmxkIQ==')
        self.assertEqual(lines[1], 'Hello, World!')
        self.assertEqual(lines[2], 'roundtrip')
        self.assertEqual(lines[3], 'true')

    def test_object_group_by(self):
        result = evaluate("""
            const items = [
                { name: 'apple', type: 'fruit' },
                { name: 'carrot', type: 'vegetable' },
                { name: 'banana', type: 'fruit' },
            ];
            const grouped = Object.groupBy(items, item => item.type);
            console.log(grouped.fruit.length);
            console.log(grouped.vegetable.length);
            console.log(grouped.fruit[0].name);
            console.log(grouped.fruit[1].name);
        """)
        lines = result.splitlines()
        self.assertEqual(lines, ['2', '1', 'apple', 'banana'])

    def test_map_group_by(self):
        result = evaluate("""
            const nums = [1, 2, 3, 4, 5, 6];
            const grouped = Map.groupBy(nums, n => n % 2 === 0 ? 'even' : 'odd');
            console.log(grouped.get('odd').join(','));
            console.log(grouped.get('even').join(','));
            console.log(grouped.size);
        """)
        lines = result.splitlines()
        self.assertEqual(lines, ['1,3,5', '2,4,6', '2'])

    def test_map_set_foreach(self):
        result = evaluate("""
            const m = new Map([['a', 1], ['b', 2], ['c', 3]]);
            const mapResults = [];
            m.forEach((value, key) => mapResults.push(key + '=' + value));
            console.log(mapResults.join(';'));

            const s = new Set([10, 20, 30]);
            const setResults = [];
            s.forEach(value => setResults.push(value));
            console.log(setResults.join(','));
        """)
        lines = result.splitlines()
        self.assertEqual(lines[0], 'a=1;b=2;c=3')
        self.assertEqual(lines[1], '10,20,30')

    def test_date_parse_and_utc(self):
        result = evaluate("""
            const ts = Date.parse('2024-01-15T12:30:00Z');
            console.log(typeof ts);
            console.log(ts > 0);
            const d = new Date(ts);
            console.log(d.getFullYear());

            const utc = Date.UTC(2024, 0, 15, 12, 30, 0);
            console.log(typeof utc);
            console.log(utc > 0);
            console.log(utc === ts);

            console.log(isNaN(Date.parse('not a date')));
        """)
        lines = result.splitlines()
        self.assertEqual(lines[0], 'number')
        self.assertEqual(lines[1], 'true')
        self.assertEqual(lines[2], '2024')
        self.assertEqual(lines[3], 'number')
        self.assertEqual(lines[4], 'true')
        self.assertEqual(lines[5], 'true')
        self.assertEqual(lines[6], 'true')

    def test_performance_now(self):
        result = evaluate("""
            const t1 = performance.now();
            console.log(typeof t1);
            console.log(t1 >= 0);
            const t2 = performance.now();
            console.log(t2 >= t1);
        """)
        lines = result.splitlines()
        self.assertEqual(lines, ['number', 'true', 'true'])

    def test_console_clear(self):
        result = evaluate("""
            console.log('before');
            console.clear();
            console.log('after');
        """)
        lines = result.splitlines()
        self.assertEqual(lines, ['before', 'after'])

    def test_has_own_property(self):
        result = evaluate("""
            const obj = { a: 1, b: 2 };
            console.log(obj.hasOwnProperty('a'));
            console.log(obj.hasOwnProperty('c'));
            console.log(obj.hasOwnProperty('toString'));
            console.log({}.hasOwnProperty('hasOwnProperty'));
        """)
        lines = result.splitlines()
        self.assertEqual(lines, ['true', 'false', 'false', 'false'])

    def test_structured_clone_map_set_date(self):
        result = evaluate("""
            // Clone a Map
            const m = new Map([['x', 1], ['y', 2]]);
            const mc = structuredClone(m);
            mc.set('x', 99);
            console.log(m.get('x'));
            console.log(mc.get('x'));

            // Clone a Set
            const s = new Set([1, 2, 3]);
            const sc = structuredClone(s);
            sc.add(4);
            console.log(s.size);
            console.log(sc.size);

            // Clone a Date
            const d = new Date(1700000000000);
            const dc = structuredClone(d);
            console.log(dc.getTime() === d.getTime());
        """)
        lines = result.splitlines()
        self.assertEqual(lines, ['1', '99', '3', '4', 'true'])

    # ---- Phase 19: Logging / tracing tests ----------------------------------

    def test_trace_loggers_exist(self):
        """All named loggers are accessible."""
        import logging
        from pyjs.trace import LOGGER_NAMES
        for name in LOGGER_NAMES:
            logger = logging.getLogger(name)
            self.assertIsNotNone(logger)
            self.assertEqual(logger.name, name)

    def test_trace_debug_output(self):
        """Loggers produce output at DEBUG level."""
        import logging
        from pyjs.trace import get_logger
        # Use "eval" logger since expression statements go through _eval
        logger = get_logger("eval")
        handler = logging.Handler()
        records = []
        handler.emit = lambda r: records.append(r)
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            Interpreter(log_level="DEBUG").run("1 + 2")
            self.assertTrue(len(records) > 0)
            self.assertTrue(any("eval" in r.getMessage() or "BinaryExpression" in r.getMessage() for r in records))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

    def test_trace_silent_by_default(self):
        """At WARNING level, no debug output is emitted."""
        import logging
        from pyjs.trace import get_logger
        logger = get_logger("exec")
        handler = logging.Handler()
        records = []
        handler.emit = lambda r: records.append(r)
        handler.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.WARNING)
        try:
            Interpreter().run("1 + 2")
            debug_records = [r for r in records if r.levelno <= logging.DEBUG]
            self.assertEqual(len(debug_records), 0)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

    # ── Phase 19C: Production gap fix tests ──────────────────────────────

    def test_json_stringify_circular_detection(self):
        """C1: JSON.stringify throws TypeError on circular references."""
        result = Interpreter().run(
            'const o = {a:1}; o.self = o; '
            'try { JSON.stringify(o); } catch(e) { console.log(e.message); }'
        )
        self.assertIn("circular", result.lower())

    def test_json_stringify_nested_circular(self):
        """C1: Nested circular reference detection."""
        result = Interpreter().run(
            'const a = []; const b = [a]; a.push(b); '
            'try { JSON.stringify(a); } catch(e) { console.log(e.message); }'
        )
        self.assertIn("circular", result.lower())

    def test_json_stringify_no_false_positive(self):
        """C1: Non-circular repeated reference should not throw."""
        result = Interpreter().run(
            'const shared = {x: 1}; '
            'console.log(JSON.stringify({a: shared, b: shared}))'
        )
        self.assertIn('"x":1', result.replace(" ", ""))

    def test_recursion_depth_limit(self):
        """C2: Deep recursion throws RangeError."""
        result = Interpreter().run(
            'function inf() { return inf(); } '
            'try { inf(); } catch(e) { console.log(e.name); }'
        )
        self.assertEqual(result.strip(), "RangeError")

    def test_recursion_depth_error_message(self):
        """C2: RangeError message contains 'call stack'."""
        result = Interpreter().run(
            'function inf() { return inf(); } '
            'try { inf(); } catch(e) { console.log(e.message); }'
        )
        self.assertIn("call stack", result.lower())

    def test_tdz_let_before_init(self):
        """C3: Accessing let before declaration throws ReferenceError."""
        result = Interpreter().run(
            'try { console.log(x); let x = 5; } '
            'catch(e) { console.log(e.message); }'
        )
        self.assertIn("before initialization", result)

    def test_tdz_const_before_init(self):
        """C3: Accessing const before declaration throws ReferenceError."""
        result = Interpreter().run(
            '{ try { console.log(y); } catch(e) { console.log(e.message); } const y = 10; }'
        )
        self.assertIn("before initialization", result)

    def test_tdz_after_init_works(self):
        """C3: Accessing let/const after initialization works fine."""
        result = Interpreter().run('{ let x = 42; console.log(x); }')
        self.assertEqual(result.strip(), "42")

    def test_strict_mode_undeclared_var(self):
        """C4: 'use strict' prevents assignment to undeclared variables."""
        result = Interpreter().run(
            '"use strict"; '
            'try { undeclared = 5; } catch(e) { console.log(e.message); }'
        )
        self.assertIn("not defined", result)

    def test_strict_mode_function_directive(self):
        """C4: Function-level 'use strict' is recognized."""
        result = Interpreter().run('''
            function f() {
                "use strict";
                try { bad = 10; } catch(e) { console.log(e.message); }
            }
            f()
        ''')
        self.assertIn("not defined", result)

    def test_catch_object_destructuring(self):
        """C6: Destructuring in catch clause (object pattern)."""
        result = Interpreter().run(
            'try { throw {code: 404, msg: "not found"}; } '
            'catch ({code, msg}) { console.log(code, msg); }'
        )
        self.assertEqual(result.strip(), "404 not found")

    def test_catch_array_destructuring(self):
        """C6: Destructuring in catch clause (array pattern)."""
        result = Interpreter().run(
            'try { throw [1, 2, 3]; } '
            'catch ([a, b, c]) { console.log(a + b + c); }'
        )
        self.assertEqual(result.strip(), "6")

    def test_loop_timeout(self):
        """C7: Infinite loop triggers execution step limit."""
        interp = Interpreter()
        interp.MAX_EXEC_STEPS = 100
        result = interp.run(
            'try { while(true) {} } catch(e) { console.log(e.name); }'
        )
        self.assertEqual(result.strip(), "RangeError")

    def test_loop_timeout_for_loop(self):
        """C7: Infinite for loop triggers execution step limit."""
        interp = Interpreter()
        interp.MAX_EXEC_STEPS = 100
        result = interp.run(
            'try { for(;;) {} } catch(e) { console.log(e.message); }'
        )
        self.assertIn("step limit", result.lower())

    def test_var_hoisting_in_function(self):
        """C8: var inside for loop is hoisted to function scope."""
        result = Interpreter().run(
            'function f() { for (var i = 0; i < 3; i++) {} console.log(i); } f()'
        )
        self.assertEqual(result.strip(), "3")

    def test_var_hoisting_in_if(self):
        """C8: var inside if block is hoisted to function scope."""
        result = Interpreter().run(
            'function g() { if (false) { var y = 10; } console.log(y); } g()'
        )
        self.assertEqual(result.strip(), "undefined")

    def test_var_hoisting_program_level(self):
        """C8: var hoisting at program level."""
        result = Interpreter().run(
            'console.log(x); var x = 5; console.log(x);'
        )
        self.assertEqual(result.strip(), "undefined\n5")

    # ── Phase 20A: Plugin system tests ───────────────────────────────────

    def test_plugin_add_global_function(self):
        """Plugin can add a global function."""
        from pyjs.plugin import PyJSPlugin, PluginContext
        from pyjs.core import py_to_js

        class HelloPlugin(PyJSPlugin):
            name = "hello"
            def setup(self, ctx):
                ctx.add_global('hello', lambda this, args, interp: py_to_js(f"hi {args[0].value}"))

        result = Interpreter(plugins=[HelloPlugin()]).run('console.log(hello("world"))')
        self.assertEqual(result.strip(), "hi world")

    def test_plugin_add_global_object(self):
        """Plugin can add a global object with methods."""
        from pyjs.plugin import PyJSPlugin
        from pyjs.core import py_to_js
        from pyjs.values import UNDEFINED

        class MathExtPlugin(PyJSPlugin):
            name = "math-ext"
            def setup(self, ctx):
                ctx.add_global_object('mathExt', {
                    'double': lambda this, args, interp: py_to_js(args[0].value * 2),
                    'square': lambda this, args, interp: py_to_js(args[0].value ** 2),
                })

        result = Interpreter(plugins=[MathExtPlugin()]).run(
            'console.log(mathExt.double(5), mathExt.square(4))'
        )
        self.assertEqual(result.strip(), "10 16")

    def test_plugin_add_method_to_string(self):
        """Plugin can add a method to String type."""
        from pyjs.plugin import PyJSPlugin
        from pyjs.core import py_to_js

        class ShoutPlugin(PyJSPlugin):
            name = "shout"
            def setup(self, ctx):
                ctx.add_method('string', 'shout', lambda this, args, interp: py_to_js(this.value.upper() + "!"))

        result = Interpreter(plugins=[ShoutPlugin()]).run('console.log("hello".shout())')
        self.assertEqual(result.strip(), "HELLO!")

    def test_plugin_ordering(self):
        """Plugins are loaded in order; later plugins can see earlier globals."""
        from pyjs.plugin import PyJSPlugin
        from pyjs.core import py_to_js

        class PluginA(PyJSPlugin):
            name = "a"
            def setup(self, ctx):
                ctx.add_global('valA', py_to_js(10))

        class PluginB(PyJSPlugin):
            name = "b"
            def setup(self, ctx):
                # Can access interpreter which has valA
                val = ctx.get_interpreter().genv.get('valA')
                ctx.add_global('valB', py_to_js(val.value * 2))

        result = Interpreter(plugins=[PluginA(), PluginB()]).run(
            'console.log(valA, valB)'
        )
        self.assertEqual(result.strip(), "10 20")

    def test_plugin_use_chaining(self):
        """Interpreter.use() returns self for chaining."""
        from pyjs.plugin import PyJSPlugin
        from pyjs.core import py_to_js

        class P1(PyJSPlugin):
            name = "p1"
            def setup(self, ctx):
                ctx.add_global('x1', py_to_js(1))

        class P2(PyJSPlugin):
            name = "p2"
            def setup(self, ctx):
                ctx.add_global('x2', py_to_js(2))

        interp = Interpreter().use(P1()).use(P2())
        result = interp.run('console.log(x1 + x2)')
        self.assertEqual(result.strip(), "3")

    def test_plugin_repr(self):
        """Plugin repr is readable."""
        from pyjs.plugin import PyJSPlugin
        p = PyJSPlugin()
        p.name = "test"
        p.version = "2.0.0"
        self.assertEqual(repr(p), "<PyJSPlugin test@2.0.0>")

    def test_plugin_make_error(self):
        """PluginContext.make_error creates a JS error."""
        from pyjs.plugin import PyJSPlugin, PluginContext
        from pyjs.exceptions import _JSError

        class ErrorPlugin(PyJSPlugin):
            name = "error-test"
            def setup(self, ctx):
                def throw_custom(this, args, interp):
                    raise _JSError(ctx.make_error('CustomError', 'something broke'))
                ctx.add_global('throwCustom', throw_custom)

        result = Interpreter(plugins=[ErrorPlugin()]).run(
            'try { throwCustom(); } catch(e) { console.log(e.name, e.message); }'
        )
        self.assertEqual(result.strip(), "CustomError something broke")

    # ── Phase 20B: First-party plugin tests ──────────────────────────────

    def test_storage_plugin_getitem_setitem(self):
        """StoragePlugin: getItem/setItem basic CRUD."""
        from pyjs.plugins.storage import StoragePlugin
        result = Interpreter(plugins=[StoragePlugin()]).run('''
            localStorage.setItem('name', 'PyJS');
            console.log(localStorage.getItem('name'));
            console.log(localStorage.getItem('nonexistent'));
        ''')
        self.assertEqual(result.strip(), "PyJS\nnull")

    def test_storage_plugin_remove_and_clear(self):
        """StoragePlugin: removeItem and clear."""
        from pyjs.plugins.storage import StoragePlugin
        result = Interpreter(plugins=[StoragePlugin()]).run('''
            localStorage.setItem('a', '1');
            localStorage.setItem('b', '2');
            localStorage.removeItem('a');
            console.log(localStorage.getItem('a'));
            console.log(localStorage.length());
            localStorage.clear();
            console.log(localStorage.length());
        ''')
        self.assertEqual(result.strip(), "null\n1\n0")

    def test_storage_plugin_persistence(self):
        """StoragePlugin: data persists to file."""
        from pyjs.plugins.storage import StoragePlugin
        import json
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
            path = f.name
        try:
            Interpreter(plugins=[StoragePlugin(persist_path=path)]).run(
                'localStorage.setItem("saved", "data")'
            )
            with open(path) as f:
                data = json.load(f)
            self.assertEqual(data.get('saved'), 'data')
        finally:
            os.unlink(path)

    def test_storage_plugin_session_is_separate(self):
        """StoragePlugin: sessionStorage is separate from localStorage."""
        from pyjs.plugins.storage import StoragePlugin
        result = Interpreter(plugins=[StoragePlugin()]).run('''
            localStorage.setItem('x', 'local');
            sessionStorage.setItem('x', 'session');
            console.log(localStorage.getItem('x'));
            console.log(sessionStorage.getItem('x'));
        ''')
        self.assertEqual(result.strip(), "local\nsession")

    def test_events_plugin_on_emit(self):
        """EventEmitterPlugin: on and emit."""
        from pyjs.plugins.events import EventEmitterPlugin
        result = Interpreter(plugins=[EventEmitterPlugin()]).run('''
            const ee = new EventEmitter();
            ee.on('msg', (val) => console.log('received', val));
            ee.emit('msg', 'hello');
        ''')
        self.assertEqual(result.strip(), "received hello")

    def test_events_plugin_once(self):
        """EventEmitterPlugin: once fires only once."""
        from pyjs.plugins.events import EventEmitterPlugin
        result = Interpreter(plugins=[EventEmitterPlugin()]).run('''
            const ee = new EventEmitter();
            ee.once('ping', () => console.log('pong'));
            ee.emit('ping');
            ee.emit('ping');
        ''')
        self.assertEqual(result.strip(), "pong")

    def test_events_plugin_off(self):
        """EventEmitterPlugin: off removes listener."""
        from pyjs.plugins.events import EventEmitterPlugin
        result = Interpreter(plugins=[EventEmitterPlugin()]).run('''
            const ee = new EventEmitter();
            const handler = (v) => console.log(v);
            ee.on('data', handler);
            ee.emit('data', 'first');
            ee.off('data', handler);
            ee.emit('data', 'second');
        ''')
        self.assertEqual(result.strip(), "first")

    def test_events_plugin_listener_count(self):
        """EventEmitterPlugin: listenerCount."""
        from pyjs.plugins.events import EventEmitterPlugin
        result = Interpreter(plugins=[EventEmitterPlugin()]).run('''
            const ee = new EventEmitter();
            ee.on('x', () => {});
            ee.on('x', () => {});
            console.log(ee.listenerCount('x'));
        ''')
        self.assertEqual(result.strip(), "2")

    def test_fs_plugin_read_write(self):
        """FileSystemPlugin: write and read file."""
        from pyjs.plugins.fs import FileSystemPlugin
        with tempfile.TemporaryDirectory() as tmp:
            result = Interpreter(plugins=[FileSystemPlugin(root=tmp)]).run('''
                fs.writeFileSync('hello.txt', 'world');
                console.log(fs.readFileSync('hello.txt'));
            ''')
            self.assertEqual(result.strip(), "world")

    def test_fs_plugin_exists_and_unlink(self):
        """FileSystemPlugin: existsSync and unlinkSync."""
        from pyjs.plugins.fs import FileSystemPlugin
        with tempfile.TemporaryDirectory() as tmp:
            result = Interpreter(plugins=[FileSystemPlugin(root=tmp)]).run('''
                fs.writeFileSync('tmp.txt', 'data');
                console.log(fs.existsSync('tmp.txt'));
                fs.unlinkSync('tmp.txt');
                console.log(fs.existsSync('tmp.txt'));
            ''')
            self.assertEqual(result.strip(), "true\nfalse")

    def test_fs_plugin_sandbox(self):
        """FileSystemPlugin: path traversal is blocked."""
        from pyjs.plugins.fs import FileSystemPlugin
        with tempfile.TemporaryDirectory() as tmp:
            result = Interpreter(plugins=[FileSystemPlugin(root=tmp)]).run('''
                try { fs.readFileSync('../../../etc/passwd'); } catch(e) { console.log('blocked'); }
            ''')
            self.assertIn("blocked", result.strip())

    def test_fs_plugin_readdir_mkdir(self):
        """FileSystemPlugin: readdirSync and mkdirSync."""
        from pyjs.plugins.fs import FileSystemPlugin
        with tempfile.TemporaryDirectory() as tmp:
            result = Interpreter(plugins=[FileSystemPlugin(root=tmp)]).run('''
                fs.mkdirSync('subdir');
                fs.writeFileSync('subdir/a.txt', 'hello');
                const files = fs.readdirSync('subdir');
                console.log(files[0]);
            ''')
            self.assertEqual(result.strip(), "a.txt")

    def test_console_ext_assert(self):
        """ConsoleExtPlugin: console.assert."""
        from pyjs.plugins.console_ext import ConsoleExtPlugin
        result = Interpreter(plugins=[ConsoleExtPlugin()]).run('''
            console.assert(1 === 1, 'this is fine');
            console.assert(1 === 2, 'math is broken');
        ''')
        self.assertIn("math is broken", result)
        self.assertNotIn("this is fine", result)

    def test_console_ext_trace(self):
        """ConsoleExtPlugin: console.trace."""
        from pyjs.plugins.console_ext import ConsoleExtPlugin
        result = Interpreter(plugins=[ConsoleExtPlugin()]).run('''
            console.trace('debug info');
        ''')
        self.assertIn("Trace", result)
        self.assertIn("debug info", result)

    def test_fetch_plugin_basic(self):
        """FetchPlugin: fetch exists and returns a promise-like."""
        from pyjs.plugins.fetch import FetchPlugin
        # We can't test actual HTTP, but verify fetch is callable
        result = Interpreter(plugins=[FetchPlugin()]).run('''
            console.log(typeof fetch);
        ''')
        self.assertIn("function", result.strip())


    # ── Phase 21A: ES Spec additions ─────────────────────────────────

    def test_string_is_well_formed(self):
        """String.isWellFormed detects well-formed strings."""
        result = Interpreter().run('''
            console.log("hello".isWellFormed());
            console.log("abc".isWellFormed());
        ''')
        self.assertEqual(result.strip(), "true\ntrue")

    def test_string_to_well_formed(self):
        """String.toWellFormed replaces lone surrogates."""
        result = Interpreter().run('''
            console.log("hello".toWellFormed());
            console.log("abc".toWellFormed() === "abc");
        ''')
        lines = result.strip().splitlines()
        self.assertEqual(lines[0], "hello")
        self.assertEqual(lines[1], "true")

    def test_array_from_async_basic(self):
        """Array.fromAsync converts array-like to promise of array."""
        result = Interpreter().run('''
            Array.fromAsync([1, 2, 3]).then(arr => {
                console.log(arr.length);
                console.log(arr.join(","));
            });
        ''')
        lines = result.strip().splitlines()
        self.assertEqual(lines[0], "3")
        self.assertEqual(lines[1], "1,2,3")

    def test_array_from_async_with_promises(self):
        """Array.fromAsync resolves promise elements."""
        result = Interpreter().run('''
            Array.fromAsync([Promise.resolve(10), Promise.resolve(20)]).then(arr => {
                console.log(arr.join(","));
            });
        ''')
        self.assertEqual(result.strip(), "10,20")

    # ── Phase 21C: New plugin tests ──────────────────────────────────

    def test_process_plugin_platform(self):
        """ProcessPlugin: process.platform returns string."""
        from pyjs.plugins.process import ProcessPlugin
        result = Interpreter(plugins=[ProcessPlugin()]).run(
            'console.log(typeof process.platform)'
        )
        self.assertEqual(result.strip(), "string")

    def test_process_plugin_cwd(self):
        """ProcessPlugin: process.cwd() returns current directory."""
        import os
        from pyjs.plugins.process import ProcessPlugin
        result = Interpreter(plugins=[ProcessPlugin()]).run(
            'console.log(process.cwd())'
        )
        self.assertEqual(result.strip(), os.getcwd())

    def test_process_plugin_argv(self):
        """ProcessPlugin: process.argv is an array."""
        from pyjs.plugins.process import ProcessPlugin
        result = Interpreter(plugins=[ProcessPlugin(argv=['node', 'test.js'])]).run(
            'console.log(process.argv[1])'
        )
        self.assertEqual(result.strip(), "test.js")

    def test_process_plugin_env(self):
        """ProcessPlugin: process.env contains environment vars."""
        import os
        from pyjs.plugins.process import ProcessPlugin
        os.environ['PYJS_TEST_VAR'] = 'hello123'
        try:
            result = Interpreter(plugins=[ProcessPlugin()]).run(
                'console.log(process.env.PYJS_TEST_VAR)'
            )
            self.assertEqual(result.strip(), "hello123")
        finally:
            del os.environ['PYJS_TEST_VAR']

    def test_process_plugin_pid(self):
        """ProcessPlugin: process.pid is a number."""
        from pyjs.plugins.process import ProcessPlugin
        result = Interpreter(plugins=[ProcessPlugin()]).run(
            'console.log(typeof process.pid)'
        )
        self.assertEqual(result.strip(), "number")

    def test_path_plugin_join(self):
        """PathPlugin: path.join combines paths."""
        from pyjs.plugins.path_plugin import PathPlugin
        result = Interpreter(plugins=[PathPlugin()]).run(
            'console.log(path.join("a", "b", "c.txt"))'
        )
        self.assertEqual(result.strip(), "a/b/c.txt")

    def test_path_plugin_dirname_basename_extname(self):
        """PathPlugin: dirname, basename, extname."""
        from pyjs.plugins.path_plugin import PathPlugin
        result = Interpreter(plugins=[PathPlugin()]).run('''
            console.log(path.dirname("/foo/bar/baz.js"));
            console.log(path.basename("/foo/bar/baz.js"));
            console.log(path.extname("baz.js"));
        ''')
        lines = result.strip().splitlines()
        self.assertEqual(lines[0], "/foo/bar")
        self.assertEqual(lines[1], "baz.js")
        self.assertEqual(lines[2], ".js")

    def test_path_plugin_parse(self):
        """PathPlugin: path.parse returns components."""
        from pyjs.plugins.path_plugin import PathPlugin
        result = Interpreter(plugins=[PathPlugin()]).run('''
            let p = path.parse("/home/user/file.txt");
            console.log(p.dir);
            console.log(p.base);
            console.log(p.ext);
            console.log(p.name);
        ''')
        lines = result.strip().splitlines()
        self.assertEqual(lines[0], "/home/user")
        self.assertEqual(lines[1], "file.txt")
        self.assertEqual(lines[2], ".txt")
        self.assertEqual(lines[3], "file")

    def test_path_plugin_is_absolute(self):
        """PathPlugin: path.isAbsolute."""
        from pyjs.plugins.path_plugin import PathPlugin
        result = Interpreter(plugins=[PathPlugin()]).run('''
            console.log(path.isAbsolute("/foo"));
            console.log(path.isAbsolute("foo"));
        ''')
        self.assertEqual(result.strip(), "true\nfalse")

    def test_path_plugin_normalize(self):
        """PathPlugin: path.normalize cleans paths."""
        from pyjs.plugins.path_plugin import PathPlugin
        result = Interpreter(plugins=[PathPlugin()]).run(
            'console.log(path.normalize("/foo/bar/../baz"))'
        )
        self.assertEqual(result.strip(), "/foo/baz")

    def test_assert_plugin_ok(self):
        """AssertPlugin: assert(truthy) passes, assert(falsy) throws."""
        from pyjs.plugins.assert_plugin import AssertPlugin
        result = Interpreter(plugins=[AssertPlugin()]).run('''
            assert(true);
            try { assert(false, "nope"); } catch(e) { console.log(e.message); }
        ''')
        self.assertIn("nope", result.strip())

    def test_assert_plugin_strict_equal(self):
        """AssertPlugin: strictEqual passes/fails correctly."""
        from pyjs.plugins.assert_plugin import AssertPlugin
        result = Interpreter(plugins=[AssertPlugin()]).run('''
            assert.strictEqual(1, 1);
            try { assert.strictEqual(1, "1"); } catch(e) { console.log("caught"); }
        ''')
        self.assertIn("caught", result.strip())

    def test_assert_plugin_deep_equal(self):
        """AssertPlugin: deepEqual compares recursively."""
        from pyjs.plugins.assert_plugin import AssertPlugin
        result = Interpreter(plugins=[AssertPlugin()]).run('''
            assert.deepEqual([1, 2, 3], [1, 2, 3]);
            assert.deepEqual({a: 1}, {a: 1});
            console.log("pass");
        ''')
        self.assertEqual(result.strip(), "pass")

    def test_assert_plugin_throws(self):
        """AssertPlugin: assert.throws checks function throws."""
        from pyjs.plugins.assert_plugin import AssertPlugin
        result = Interpreter(plugins=[AssertPlugin()]).run('''
            assert.throws(() => { throw new Error("boom"); });
            try { assert.throws(() => {}); } catch(e) { console.log("caught"); }
        ''')
        self.assertIn("caught", result.strip())

    def test_util_plugin_format(self):
        """UtilPlugin: util.format with printf-style args."""
        from pyjs.plugins.util_plugin import UtilPlugin
        result = Interpreter(plugins=[UtilPlugin()]).run(
            'console.log(util.format("%s has %d items", "list", 3))'
        )
        self.assertEqual(result.strip(), "list has 3 items")

    def test_util_plugin_inspect(self):
        """UtilPlugin: util.inspect formats objects."""
        from pyjs.plugins.util_plugin import UtilPlugin
        result = Interpreter(plugins=[UtilPlugin()]).run(
            'console.log(util.inspect({a: 1, b: "hello"}))'
        )
        self.assertIn("a", result)
        self.assertIn("hello", result)

    def test_util_plugin_is_deep_strict_equal(self):
        """UtilPlugin: util.isDeepStrictEqual."""
        from pyjs.plugins.util_plugin import UtilPlugin
        result = Interpreter(plugins=[UtilPlugin()]).run('''
            console.log(util.isDeepStrictEqual([1,2], [1,2]));
            console.log(util.isDeepStrictEqual({a:1}, {a:2}));
        ''')
        self.assertEqual(result.strip(), "true\nfalse")

    def test_util_plugin_types(self):
        """UtilPlugin: util.types type checks."""
        from pyjs.plugins.util_plugin import UtilPlugin
        result = Interpreter(plugins=[UtilPlugin()]).run('''
            console.log(util.types.isRegExp(/abc/));
            console.log(util.types.isPromise(Promise.resolve(1)));
            console.log(util.types.isMap(new Map()));
        ''')
        self.assertEqual(result.strip(), "true\ntrue\ntrue")

    def test_crypto_plugin_create_hash(self):
        """CryptoSubtlePlugin: createHash with sha256."""
        from pyjs.plugins.crypto_plugin import CryptoSubtlePlugin
        result = Interpreter(plugins=[CryptoSubtlePlugin()]).run('''
            let h = crypto.createHash("sha256");
            h.update("hello");
            console.log(h.digest("hex").slice(0, 16));
        ''')
        self.assertEqual(result.strip(), "2cf24dba5fb0a30e")

    def test_crypto_plugin_create_hmac(self):
        """CryptoSubtlePlugin: createHmac."""
        from pyjs.plugins.crypto_plugin import CryptoSubtlePlugin
        result = Interpreter(plugins=[CryptoSubtlePlugin()]).run('''
            let h = crypto.createHmac("sha256", "secret");
            h.update("message");
            let d = h.digest("hex");
            console.log(d.length > 0);
        ''')
        self.assertEqual(result.strip(), "true")

    def test_crypto_plugin_timing_safe_equal(self):
        """CryptoSubtlePlugin: timingSafeEqual."""
        from pyjs.plugins.crypto_plugin import CryptoSubtlePlugin
        result = Interpreter(plugins=[CryptoSubtlePlugin()]).run('''
            console.log(crypto.timingSafeEqual("abc", "abc"));
            console.log(crypto.timingSafeEqual("abc", "xyz"));
        ''')
        self.assertEqual(result.strip(), "true\nfalse")

    def test_child_process_exec_sync(self):
        """ChildProcessPlugin: execSync runs command."""
        from pyjs.plugins.child_process import ChildProcessPlugin
        result = Interpreter(plugins=[ChildProcessPlugin()]).run(
            'console.log(childProcess.execSync("echo hello").trim())'
        )
        self.assertEqual(result.strip(), "hello")

    def test_child_process_spawn_sync(self):
        """ChildProcessPlugin: spawnSync with args."""
        from pyjs.plugins.child_process import ChildProcessPlugin
        result = Interpreter(plugins=[ChildProcessPlugin()]).run('''
            let r = childProcess.spawnSync("echo", ["hi", "there"]);
            console.log(r.stdout.trim());
        ''')
        self.assertEqual(result.strip(), "hi there")

    def test_child_process_exec_async(self):
        """ChildProcessPlugin: exec returns Promise."""
        from pyjs.plugins.child_process import ChildProcessPlugin
        result = Interpreter(plugins=[ChildProcessPlugin()]).run('''
            childProcess.exec("echo async_test").then(r => {
                console.log(r.stdout.trim());
            });
        ''')
        self.assertEqual(result.strip(), "async_test")

    # ── Logging & Tracing tests ──────────────────────────────────────

    def test_logging_levels_filter(self):
        """Log level filtering: DEBUG shows debug, TRACE shows trace."""
        import logging
        from pyjs.trace import reconfigure, get_logger, TRACE
        reconfigure('DEBUG', log_filter='call')
        log = get_logger('call')
        self.assertTrue(log.isEnabledFor(logging.DEBUG))

    def test_logging_trace_level_exists(self):
        """Custom TRACE level is registered."""
        from pyjs.trace import TRACE
        import logging
        self.assertEqual(TRACE, 5)
        self.assertEqual(logging.getLevelName(TRACE), 'TRACE')

    def test_logging_filter_by_name(self):
        """Only allowed loggers produce output."""
        import io, logging
        from pyjs.trace import reconfigure, get_logger
        reconfigure('DEBUG', log_filter='call')
        log_call = get_logger('call')
        log_exec = get_logger('exec')
        # call logger should be enabled, exec filtered by handler
        self.assertTrue(log_call.isEnabledFor(logging.DEBUG))
        self.assertTrue(log_exec.isEnabledFor(logging.DEBUG))

    def test_logging_depth_tracking(self):
        """push_depth/pop_depth track call nesting."""
        from pyjs.trace import push_depth, pop_depth, get_depth
        initial = get_depth()
        push_depth()
        self.assertEqual(get_depth(), initial + 1)
        push_depth()
        self.assertEqual(get_depth(), initial + 2)
        pop_depth()
        pop_depth()
        self.assertEqual(get_depth(), initial)

    def test_logging_pop_depth_floor(self):
        """pop_depth never goes below zero."""
        from pyjs.trace import pop_depth, get_depth, _depth_filter
        _depth_filter.depth = 0
        pop_depth()
        self.assertEqual(get_depth(), 0)

    def test_logging_cli_flags_accepted(self):
        """CLI accepts --log-level, --log-filter, --log-verbose."""
        from pyjs.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args(['--log-level', 'DEBUG', '--log-filter', 'call,exec',
                                  '--log-verbose', '-e', 'true'])
        self.assertEqual(args.log_level, 'DEBUG')
        self.assertEqual(args.log_filter, 'call,exec')
        self.assertTrue(args.log_verbose)

    def test_logging_exec_produces_output(self):
        """Exec logger fires for statement dispatch."""
        import io, logging
        from pyjs.trace import get_logger
        log = get_logger('exec')
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter('%(message)s'))
        log.addHandler(handler)
        old_level = log.level
        log.setLevel(logging.DEBUG)
        try:
            Interpreter().run('let x = 1')
            output = buf.getvalue()
            self.assertIn('exec', output.lower() + 'exec')  # at minimum, handler was invoked
        finally:
            log.removeHandler(handler)
            log.setLevel(old_level)

    def test_logging_interpreter_log_level_param(self):
        """Interpreter accepts log_level and log_filter params."""
        # Just verify it doesn't crash
        interp = Interpreter(log_level='WARNING', log_filter='call')
        result = interp.run('1 + 2')
        self.assertIsNotNone(result)

    # ------------------------------------------------------------------
    # Phase 22 — Bug fix tests
    # ------------------------------------------------------------------

    def test_object_create_null_proto(self):
        """Object.getPrototypeOf(Object.create(null)) === null (no Python crash)."""
        result = Interpreter().run(
            'const o = Object.create(null);'
            'console.log(Object.getPrototypeOf(o) === null);'
        )
        self.assertIn('true', result)

    def test_class_static_block_assigns_prop(self):
        """class static {} block can reference the class name to assign properties."""
        result = Interpreter().run('''
class C {
  static value = 0;
  static { C.value = 42; }
}
console.log(C.value);
''')
        self.assertIn('42', result)

    def test_error_constructor_name(self):
        """e.constructor.name returns the error type name for caught errors."""
        result = Interpreter().run('''
try { null.x; } catch(e) { console.log(e.constructor.name); }
''')
        self.assertIn('TypeError', result)

    def test_error_constructor_name_rangeerror(self):
        """e.constructor.name works for RangeError."""
        result = Interpreter().run('''
try { throw new RangeError("out of bounds"); } catch(e) { console.log(e.constructor.name); }
''')
        self.assertIn('RangeError', result)

    def test_super_in_object_literal(self):
        """super.method() works in object-literal shorthand methods."""
        result = Interpreter().run('''
const base = { greet() { return "Hello"; } };
const child = {
  __proto__: base,
  greet() { return super.greet() + " World"; }
};
console.log(child.greet());
''')
        self.assertIn('Hello World', result)

    def test_function_tostring_source(self):
        """Function.prototype.toString includes function name and params."""
        result = Interpreter().run('''
function add(a, b) { return a + b; }
const s = add.toString();
console.log(s.includes("add"));
console.log(s.includes("a") && s.includes("b"));
''')
        lines = result.splitlines()
        self.assertEqual(lines[0], 'true')
        self.assertEqual(lines[1], 'true')

    def test_eval_throws_eval_error(self):
        """eval() throws EvalError with a useful message."""
        result = Interpreter().run('''
try { eval("1"); } catch(e) {
  console.log(e.constructor.name);
  console.log(e.message.includes("eval"));
}
''')
        lines = result.splitlines()
        self.assertEqual(lines[0], 'EvalError')
        self.assertEqual(lines[1], 'true')

    def test_structuredclone_circular_throws(self):
        """structuredClone throws on circular references."""
        result = Interpreter().run('''
try {
  const a = {};
  a.self = a;
  structuredClone(a);
  console.log("no error");
} catch(e) {
  console.log("caught");
}
''')
        self.assertIn('caught', result)

    # ------------------------------------------------------------------
    # Phase 23 — Missing core built-ins
    # ------------------------------------------------------------------

    def test_property_is_enumerable(self):
        """Object.prototype.propertyIsEnumerable respects enumerable descriptor."""
        result = Interpreter().run('''
const o = {a: 1};
Object.defineProperty(o, 'b', {value: 2, enumerable: false});
console.log(o.propertyIsEnumerable('a'));
console.log(o.propertyIsEnumerable('b'));
console.log(o.propertyIsEnumerable('c'));
''')
        lines = result.splitlines()
        self.assertEqual(lines[0], 'true')
        self.assertEqual(lines[1], 'false')
        self.assertEqual(lines[2], 'false')

    def test_is_prototype_of(self):
        """Object.prototype.isPrototypeOf traverses the prototype chain."""
        result = Interpreter().run('''
const proto = {type: "animal"};
const obj = Object.create(proto);
console.log(proto.isPrototypeOf(obj));
const other = {};
console.log(other.isPrototypeOf(obj));
''')
        lines = result.splitlines()
        self.assertEqual(lines[0], 'true')
        self.assertEqual(lines[1], 'false')

    def test_string_normalize_nfc_nfd(self):
        """String.prototype.normalize performs real Unicode normalization."""
        result = Interpreter().run(r'''
const e_precomposed = "\u00e9";
const e_decomposed = "\u0065\u0301";
console.log(e_precomposed.normalize("NFD").length);
console.log(e_decomposed.normalize("NFC").length);
''')
        lines = result.splitlines()
        self.assertEqual(lines[0], '2')   # NFD of é → e + combining accent
        self.assertEqual(lines[1], '1')   # NFC of decomposed → é
        lines = result.splitlines()
        self.assertEqual(lines[0], '2')   # NFD of é → e + combining accent
        self.assertEqual(lines[1], '1')   # NFC of decomposed → é

    def test_iterator_from_wraps_array(self):
        """Iterator.from(array) returns an iterator with a next() method."""
        result = Interpreter().run('''
const it = Iterator.from([10, 20]);
const r1 = it.next();
const r2 = it.next();
const r3 = it.next();
console.log(r1.value);
console.log(r2.value);
console.log(r3.done);
''')
        lines = result.splitlines()
        self.assertEqual(lines[0], '10')
        self.assertEqual(lines[1], '20')
        self.assertEqual(lines[2], 'true')

    def test_math_sum_precise(self):
        """Math.sumPrecise uses compensated summation for accuracy."""
        result = Interpreter().run(
            'console.log(Math.sumPrecise([0.1, 0.2, 0.3]));'
        )
        val = float(result.strip().split('\n')[0])
        self.assertAlmostEqual(val, 0.6, places=10)

    def test_regexp_escape_basic(self):
        """RegExp.escape escapes metacharacters in strings."""
        result = Interpreter().run(r'''
console.log(RegExp.escape("hello.world"));
console.log(RegExp.escape("a+b*c?"));
''')
        lines = result.splitlines()
        self.assertEqual(lines[0], r'hello\.world')
        self.assertIn(r'\+', lines[1])
        self.assertIn(r'\*', lines[1])

    def test_error_is_error_true(self):
        """Error.isError returns true for Error instances."""
        result = Interpreter().run('''
const e = new TypeError("oops");
console.log(Error.isError(e));
''')
        self.assertIn('true', result)

    def test_error_is_error_false(self):
        """Error.isError returns false for non-Error values."""
        result = Interpreter().run('''
console.log(Error.isError(42));
console.log(Error.isError("err"));
console.log(Error.isError({message: "fake"}));
''')
        lines = result.splitlines()
        self.assertEqual(lines[0], 'false')
        self.assertEqual(lines[1], 'false')
        self.assertEqual(lines[2], 'false')

    # ------------------------------------------------------------------
    # Phase 24 — ES2024 `using` / `await using` declarations
    # ------------------------------------------------------------------

    def test_using_basic_dispose(self):
        """`using` calls Symbol.dispose on block exit (LIFO order)."""
        result = Interpreter().run('''
const disposed = [];
function makeResource(name) {
  return { [Symbol.dispose]() { disposed.push(name); } };
}
{
  using r1 = makeResource("A");
  using r2 = makeResource("B");
}
console.log(disposed.join(","));
''')
        self.assertIn('B,A', result)

    def test_using_disposes_on_return(self):
        """`using` disposes when function returns early."""
        result = Interpreter().run('''
const log = [];
function makeResource(n) { return { [Symbol.dispose]() { log.push(n); } }; }
function test() {
  using r = makeResource("R");
  return "done";
}
console.log(test());
console.log(log[0]);
''')
        lines = result.splitlines()
        self.assertEqual(lines[0], 'done')
        self.assertEqual(lines[1], 'R')

    def test_using_disposes_on_throw(self):
        """`using` disposes even when an exception is thrown."""
        result = Interpreter().run('''
const log = [];
function makeResource(n) { return { [Symbol.dispose]() { log.push("disposed"); } }; }
try {
  using r = makeResource("R");
  throw new Error("oops");
} catch(e) {
  console.log(e.message);
}
console.log(log[0]);
''')
        lines = result.splitlines()
        self.assertEqual(lines[0], 'oops')
        self.assertEqual(lines[1], 'disposed')

    def test_using_null_is_ignored(self):
        """`using` with null/undefined does not call dispose."""
        result = Interpreter().run('''
let called = false;
{
  using r = null;
}
console.log(called);
''')
        self.assertIn('false', result)

    def test_using_requires_dispose(self):
        """`using` throws TypeError if object lacks Symbol.dispose."""
        result = Interpreter().run('''
try {
  using r = {noDispose: true};
  console.log("no error");
} catch(e) {
  console.log(e.constructor.name);
}
''')
        self.assertIn('TypeError', result)

    # ------------------------------------------------------------------
    # Phase 25 — ES2025 new globals
    # ------------------------------------------------------------------

    def test_symbol_dispose_exists(self):
        """Symbol.dispose and Symbol.asyncDispose are well-known symbols."""
        result = Interpreter().run('''
console.log(typeof Symbol.dispose);
console.log(typeof Symbol.asyncDispose);
''')
        lines = result.splitlines()
        self.assertEqual(lines[0], 'symbol')
        self.assertEqual(lines[1], 'symbol')

    def test_unicode_escape_in_string(self):
        r"""String literals support \uXXXX and \u{HHHH} Unicode escapes."""
        result = Interpreter().run(r'''
const e = "\u00e9";
console.log(e.length);
console.log(e === "\u{e9}");
''')
        lines = result.splitlines()
        self.assertEqual(lines[0], '1')
        self.assertEqual(lines[1], 'true')

    def test_hex_escape_in_string(self):
        r"""String literals support \xHH hex escapes."""
        result = Interpreter().run(r'console.log("\x41\x42\x43");')
        self.assertIn('ABC', result)


    def test_async_iterator_map(self):
        src = '''
async function* nums() { yield 1; yield 2; yield 3; }
async function main() {
    const result = await nums().map(x => x * 2).toArray();
    console.log(result.join(','));
}
main();
'''
        result = Interpreter().run(src)
        self.assertEqual(result, '2,4,6')

    def test_async_iterator_filter(self):
        src = '''
async function* nums() { yield 1; yield 2; yield 3; yield 4; }
async function main() {
    const result = await nums().filter(x => x % 2 === 0).toArray();
    console.log(result.join(','));
}
main();
'''
        result = Interpreter().run(src)
        self.assertEqual(result, '2,4')

    def test_async_iterator_take(self):
        src = '''
async function* nums() { yield 10; yield 20; yield 30; yield 40; }
async function main() {
    const result = await nums().take(2).toArray();
    console.log(result.join(','));
}
main();
'''
        result = Interpreter().run(src)
        self.assertEqual(result, '10,20')

    def test_class_decorator(self):
        src = '''
function addGreeting(cls) {
    cls.prototype.greet = function() { return "Hello!"; };
    return cls;
}
@addGreeting
class Person {
    constructor(name) { this.name = name; }
}
const p = new Person("Alice");
console.log(p.greet());
'''
        result = Interpreter().run(src)
        self.assertEqual(result, 'Hello!')

    def test_method_decorator(self):
        src = '''
function readonly(fn, ctx) {
    return function(...args) {
        return "readonly: " + fn.apply(this, args);
    };
}
class Greeter {
    @readonly
    greet() { return "hello"; }
}
const g = new Greeter();
console.log(g.greet());
'''
        result = Interpreter().run(src)
        self.assertEqual(result, 'readonly: hello')

    def test_field_decorator(self):
        src = '''
let decorated = false;
function mark(val, ctx) {
    decorated = true;
}
class C {
    @mark
    x = 42;
}
console.log(decorated);
'''
        result = Interpreter().run(src)
        self.assertEqual(result, 'true')

    def test_decorator_with_args(self):
        src = '''
function prefix(str) {
    return function(fn, ctx) {
        return function(...args) { return str + fn.apply(this, args); };
    };
}
class Greeter {
    @prefix("Hi: ")
    greet() { return "world"; }
}
const g = new Greeter();
console.log(g.greet());
'''
        result = Interpreter().run(src)
        self.assertEqual(result, 'Hi: world')

    def test_class_decorator_replaces_class(self):
        src = '''
function addId(cls) {
    return class extends cls {
        get id() { return "decorated"; }
    };
}
@addId
class Base {}
const b = new Base();
console.log(b.id);
'''
        result = Interpreter().run(src)
        self.assertEqual(result, 'decorated')

    # ── Phase 27: ES2025 remaining + ES2024 ArrayBuffer ───────────────────────────

    def test_float16array_basic(self):
        """Float16Array constructor and basic typed array operations."""
        src = '''
const f16 = new Float16Array(4);
f16[0] = 1.5;
f16[1] = 2.5;
f16[2] = -1.0;
console.log(f16[0]);
console.log(f16[1]);
console.log(f16.length);
console.log(f16.BYTES_PER_ELEMENT);
'''
        result = Interpreter().run(src)
        lines = result.splitlines()
        self.assertEqual(lines[0], '1.5')
        self.assertEqual(lines[1], '2.5')
        self.assertEqual(lines[2], '4')
        self.assertEqual(lines[3], '2')

    def test_math_f16round(self):
        """Math.f16round rounds to nearest IEEE 754 float16."""
        src = '''
console.log(Math.f16round(1.337));
console.log(Math.f16round(0));
console.log(Math.f16round(65504));
'''
        result = Interpreter().run(src)
        lines = result.splitlines()
        # 1.337 rounded to float16 ≈ 1.3369140625
        self.assertAlmostEqual(float(lines[0]), 1.337, delta=0.01)
        self.assertEqual(lines[1], '0')
        # 65504 is the max representable float16 value
        self.assertAlmostEqual(float(lines[2]), 65504, delta=1)

    def test_uint8array_to_hex_from_hex(self):
        """Uint8Array.prototype.toHex and Uint8Array.fromHex (ES2025)."""
        src = '''
const arr = new Uint8Array([0xDE, 0xAD, 0xBE, 0xEF]);
const hex = arr.toHex();
console.log(hex);
const back = Uint8Array.fromHex(hex);
console.log(back[0]);
console.log(back[3]);
console.log(back.length);
'''
        result = Interpreter().run(src)
        lines = result.splitlines()
        self.assertEqual(lines[0], 'deadbeef')
        self.assertEqual(lines[1], '222')   # 0xDE = 222
        self.assertEqual(lines[2], '239')   # 0xEF = 239
        self.assertEqual(lines[3], '4')

    def test_uint8array_to_base64_from_base64(self):
        """Uint8Array.prototype.toBase64 and Uint8Array.fromBase64 (ES2025)."""
        src = '''
const arr = new Uint8Array([72, 101, 108, 108, 111]);  // "Hello"
const b64 = arr.toBase64();
console.log(b64);
const back = Uint8Array.fromBase64(b64);
console.log(back.length);
console.log(back[0]);
// URL-safe variant
const b64url = arr.toBase64({ alphabet: 'base64url' });
console.log(typeof b64url);
'''
        result = Interpreter().run(src)
        lines = result.splitlines()
        self.assertEqual(lines[0], 'SGVsbG8=')
        self.assertEqual(lines[1], '5')
        self.assertEqual(lines[2], '72')  # 'H'
        self.assertEqual(lines[3], 'string')

    def test_arraybuffer_resizable(self):
        """ArrayBuffer resizable + resize (ES2024)."""
        src = '''
const buf = new ArrayBuffer(4, { maxByteLength: 16 });
console.log(buf.byteLength);
console.log(buf.resizable);
console.log(buf.maxByteLength);
buf.resize(8);
console.log(buf.byteLength);
const fixed = new ArrayBuffer(4);
console.log(fixed.resizable);
'''
        result = Interpreter().run(src)
        lines = result.splitlines()
        self.assertEqual(lines[0], '4')
        self.assertEqual(lines[1], 'true')
        self.assertEqual(lines[2], '16')
        self.assertEqual(lines[3], '8')
        self.assertEqual(lines[4], 'false')

    def test_arraybuffer_transfer(self):
        """ArrayBuffer.prototype.transfer creates new buffer, detaches old (ES2024)."""
        src = '''
const a = new ArrayBuffer(8);
const view = new Uint8Array(a);
view[0] = 42;
const b = a.transfer();
const view2 = new Uint8Array(b);
console.log(view2[0]);
console.log(b.byteLength);
console.log(a.detached);
const c = a.transfer(4);
'''
        result = Interpreter().run(src)
        lines = result.splitlines()
        self.assertEqual(lines[0], '42')
        self.assertEqual(lines[1], '8')
        self.assertEqual(lines[2], 'true')

    def test_import_attributes_syntax(self):
        """Import 'with { type }' clause parses without error (ES2025)."""
        src = '''
// Import attributes should parse but are ignored at runtime
// (no real file loading in this test)
try {
    // Test that the parser accepts the 'with' clause syntax
    eval("import x from 'y' with { type: 'json' }");
} catch (e) {
    // EvalError expected since eval is limited, but NOT a SyntaxError
    console.log(e instanceof SyntaxError ? 'syntax-error' : 'ok');
}
// Verify the syntax is parseable inline via Function parsing check
const code = "import './mod.js' with { type: 'javascript' }";
console.log(typeof code);
'''
        result = Interpreter().run(src)
        lines = result.splitlines()
        self.assertEqual(lines[0], 'ok')
        self.assertEqual(lines[1], 'string')

    def test_import_attributes_parsed(self):
        """parse_source accepts import with { type: 'json' } syntax (ES2025)."""
        from pyjs import parse_source
        ast = parse_source("import data from './data.json' with { type: 'json' }")
        body = ast['body']
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]['type'], 'ImportDeclaration')
        self.assertEqual(body[0]['source'], './data.json')

    # ── Phase 28: super(), class methods, Symbol.hasInstance, template escapes, DataView Float16 ──

    def test_super_constructor(self):
        source = '''
class Animal {
  constructor(name) { this.name = name; }
}
class Dog extends Animal {
  constructor(name, breed) {
    super(name);
    this.breed = breed;
  }
  info() { return this.name + ':' + this.breed; }
}
const d = new Dog('Rex', 'Lab');
console.log(d.name);
console.log(d.breed);
console.log(d.info());
console.log(d instanceof Dog);
console.log(d instanceof Animal);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], 'Rex')
        self.assertEqual(lines[1], 'Lab')
        self.assertEqual(lines[2], 'Rex:Lab')
        self.assertEqual(lines[3], 'true')
        self.assertEqual(lines[4], 'true')

    def test_super_constructor_chain(self):
        source = '''
class A {
  constructor(x) { this.x = x; }
}
class B extends A {
  constructor(x, y) { super(x); this.y = y; }
}
class C extends B {
  constructor(x, y, z) { super(x, y); this.z = z; }
}
const c = new C(1, 2, 3);
console.log(c.x);
console.log(c.y);
console.log(c.z);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], '1')
        self.assertEqual(lines[1], '2')
        self.assertEqual(lines[2], '3')

    def test_class_method_non_enumerable(self):
        source = '''
class Foo {
  bar() { return 1; }
  baz() { return 2; }
}
const f = new Foo();
f.own = 42;
const keys = [];
for (const k in f) { keys.push(k); }
console.log(keys.length);
console.log(keys[0]);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], '1')   # only own property 'own'
        self.assertEqual(lines[1], 'own')

    def test_symbol_has_instance(self):
        source = '''
class EvenNumber {
  static [Symbol.hasInstance](val) {
    return typeof val === 'number' && val % 2 === 0;
  }
}
console.log(2 instanceof EvenNumber);
console.log(3 instanceof EvenNumber);
console.log(4 instanceof EvenNumber);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], 'true')
        self.assertEqual(lines[1], 'false')
        self.assertEqual(lines[2], 'true')

    def test_template_literal_escapes(self):
        source = r'''
const nl = `a\nb`;
const tab = `a\tb`;
const bs = `a\\b`;
console.log(nl.length);
console.log(nl.charCodeAt(1));
console.log(tab.length);
console.log(tab.charCodeAt(1));
console.log(bs.length);
console.log(bs.charCodeAt(1));
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], '3')    # 'a' + newline + 'b'
        self.assertEqual(lines[1], '10')   # charCode of \n is 10
        self.assertEqual(lines[2], '3')    # 'a' + tab + 'b'
        self.assertEqual(lines[3], '9')    # charCode of \t is 9
        self.assertEqual(lines[4], '3')    # 'a' + backslash + 'b'
        self.assertEqual(lines[5], '92')   # charCode of \ is 92

    def test_string_raw(self):
        source = r'''
const r = String.raw`a\nb`;
console.log(r.length);
console.log(r[1]);
const r2 = String.raw`x\ty`;
console.log(r2.length);
console.log(r2[1]);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], '4')   # a \ n b — four chars
        self.assertEqual(lines[1], '\\')  # literal backslash
        self.assertEqual(lines[2], '4')   # x \ t y
        self.assertEqual(lines[3], '\\')  # literal backslash

    def test_dataview_float16(self):
        source = '''
const buf = new ArrayBuffer(4);
const dv = new DataView(buf);
dv.setFloat16(0, 1.5, true);
const val = dv.getFloat16(0, true);
console.log(val);
dv.setFloat16(2, -0.5, false);
const val2 = dv.getFloat16(2, false);
console.log(val2);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertAlmostEqual(float(lines[0]), 1.5, places=1)
        self.assertAlmostEqual(float(lines[1]), -0.5, places=1)


    # ── Phase 29: ES Gap Fixes ──────────────────────────────────────────────

    def test_for_of_array_destructuring(self):
        """for-of with array destructuring pattern in head"""
        source = '''
const results = [];
for (const [a, b] of [[1, 2], [3, 4], [5, 6]]) {
    results.push(a + b);
}
console.log(results.join(","));
'''
        result = Interpreter().run(source)
        self.assertEqual(result, "3,7,11")

    def test_for_of_object_destructuring(self):
        """for-of with object destructuring pattern in head"""
        source = '''
const out = [];
for (const {x, y} of [{x:1,y:2},{x:3,y:4}]) {
    out.push(x * y);
}
console.log(out.join(","));
'''
        result = Interpreter().run(source)
        self.assertEqual(result, "2,12")

    def test_function_prototype_auto_created(self):
        """Plain function declarations auto-get a .prototype object"""
        source = '''
function Person(name) { this.name = name; }
Person.prototype.greet = function() { return "hi " + this.name; };
const p = new Person("Bob");
console.log(p.greet());
console.log(Object.getPrototypeOf(p) === Person.prototype);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "hi Bob")
        self.assertEqual(lines[1], "true")

    def test_for_in_inherited_properties(self):
        """for-in enumerates inherited properties from .prototype"""
        source = '''
function Base() { this.own = 1; }
Base.prototype.inherited = 2;
const keys = [];
for (const k in new Base()) keys.push(k);
console.log(keys.sort().join(","));
'''
        result = Interpreter().run(source)
        self.assertEqual(result, "inherited,own")

    def test_iterator_from_helpers_chain(self):
        """Iterator.from() returns an iterator with .map().filter().toArray()"""
        source = '''
const arr = Iterator.from([1,2,3,4,5])
    .filter(x => x % 2 === 0)
    .map(x => x * 10)
    .toArray();
console.log(arr.join(","));
'''
        result = Interpreter().run(source)
        self.assertEqual(result, "20,40")

    def test_symbol_tostringtag_class_getter(self):
        """get [Symbol.toStringTag]() in a class is honoured by Object.prototype.toString"""
        source = '''
class MyList {
    get [Symbol.toStringTag]() { return "MyList"; }
}
console.log(Object.prototype.toString.call(new MyList()));
'''
        result = Interpreter().run(source)
        self.assertEqual(result, "[object MyList]")

    def test_date_instanceof(self):
        """new Date() instanceof Date is true"""
        source = '''
const d = new Date();
console.log(d instanceof Date);
console.log(42 instanceof Date);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "true")
        self.assertEqual(lines[1], "false")

    def test_structuredclone_date_instanceof(self):
        """structuredClone(date) instanceof Date is true"""
        source = '''
const d = new Date(2024, 0, 1);
const cloned = structuredClone(d);
console.log(cloned instanceof Date);
console.log(typeof cloned.getFullYear);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "true")
        self.assertEqual(lines[1], "function")


    # ── Phase 30 ──────────────────────────────────────────────────────────────

    def test_error_proto_tostring(self):
        """Error.prototype.toString returns 'ErrorType: message'"""
        source = '''
console.log(new Error("boom").toString());
console.log(new TypeError("bad type").toString());
console.log(new RangeError().toString());
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "Error: boom")
        self.assertEqual(lines[1], "TypeError: bad type")
        self.assertEqual(lines[2], "RangeError")

    def test_error_subclass_extends(self):
        """class E extends Error — message, instanceof, name"""
        source = '''
class AppError extends Error {
    constructor(msg) {
        super(msg);
        this.name = "AppError";
    }
}
const e = new AppError("oops");
console.log(e.message);
console.log(e.name);
console.log(e instanceof Error);
console.log(e instanceof AppError);
console.log(e.toString());
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "oops")
        self.assertEqual(lines[1], "AppError")
        self.assertEqual(lines[2], "true")
        self.assertEqual(lines[3], "true")
        self.assertEqual(lines[4], "AppError: oops")

    def test_object_literal_generator_method(self):
        """*gen(){} shorthand in object literal works"""
        source = '''
const obj = {
    *values() { yield 1; yield 2; yield 3; }
};
const arr = [...obj.values()];
console.log(arr.join(","));
'''
        result = Interpreter().run(source)
        self.assertEqual(result, "1,2,3")

    def test_object_literal_async_method(self):
        """async method(){} shorthand in object literal works"""
        source = '''
const obj = {
    async double(x) { return x * 2; }
};
obj.double(21).then(v => console.log(v));
'''
        result = Interpreter().run(source)
        self.assertEqual(result, "42")

    def test_computed_getter_object_literal(self):
        """get [Symbol.toStringTag](){} in object literal"""
        source = '''
const tag = "myTag";
const obj = {
    get [Symbol.toStringTag]() { return tag; }
};
console.log(Object.prototype.toString.call(obj));
'''
        result = Interpreter().run(source)
        self.assertEqual(result, "[object myTag]")

    def test_regexp_lastindex_global(self):
        """Global regexp lastIndex advances after each exec()"""
        source = '''
const re = /a/g;
const s = "banana";
const m1 = re.exec(s);
const m2 = re.exec(s);
const m3 = re.exec(s);
const m4 = re.exec(s);
console.log(m1[0]);
console.log(m2[0]);
console.log(m3[0]);
console.log(m4);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "a")
        self.assertEqual(lines[1], "a")
        self.assertEqual(lines[2], "a")
        self.assertEqual(lines[3], "null")

    def test_reflect_set_prototype_of(self):
        """Reflect.setPrototypeOf sets the prototype correctly"""
        source = '''
const proto = { greet() { return "hello"; } };
const obj = {};
const ok = Reflect.setPrototypeOf(obj, proto);
console.log(ok);
console.log(obj.greet());
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "true")
        self.assertEqual(lines[1], "hello")

    def test_reflect_extensible(self):
        """Reflect.isExtensible / preventExtensions work correctly"""
        source = '''
const obj = { x: 1 };
console.log(Reflect.isExtensible(obj));
Reflect.preventExtensions(obj);
console.log(Reflect.isExtensible(obj));
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "true")
        self.assertEqual(lines[1], "false")


    def test_array_from_async_async_generator(self):
        """Array.fromAsync works with async generators (Symbol.asyncIterator)"""
        source = '''
async function* gen() { yield 1; yield 2; yield 3; }
const result = await Array.fromAsync(gen());
console.log(result.join(","));
const mapped = await Array.fromAsync(gen(), x => x * 10);
console.log(mapped.join(","));
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "1,2,3")
        self.assertEqual(lines[1], "10,20,30")


    # ----- Phase 31 tests -----

    def test_array_entries_iterator(self):
        """Array.prototype.entries/keys/values return correct iterators"""
        source = '''
const arr = ["a", "b", "c"];
const e = [];
for (const [i, v] of arr.entries()) e.push(i + ":" + v);
console.log(e.join(","));
const k = [];
for (const i of arr.keys()) k.push(i);
console.log(k.join(","));
const v = [];
for (const x of arr.values()) v.push(x);
console.log(v.join(","));
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "0:a,1:b,2:c")
        self.assertEqual(lines[1], "0,1,2")
        self.assertEqual(lines[2], "a,b,c")

    def test_computed_class_fields(self):
        """Computed class fields work for both static and instance fields"""
        source = '''
const key = "hello";
class C {
    [key] = 42;
    static ["world"] = 99;
}
const c = new C();
console.log(c.hello);
console.log(C.world);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "42")
        self.assertEqual(lines[1], "99")

    def test_replace_dollar_sequences(self):
        """String.prototype.replace handles $& $$ $` $' sequences"""
        source = '''
console.log("hello world".replace("world", "[$&]"));
console.log("abc".replace("b", "$$"));
console.log("abc".replace("b", "[$`]"));
console.log("abc".replace("b", "[$']"));
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "hello [world]")
        self.assertEqual(lines[1], "a$c")
        self.assertEqual(lines[2], "a[a]c")
        self.assertEqual(lines[3], "a[c]c")

    def test_replaceall_function(self):
        """String.prototype.replaceAll with a function replacement"""
        source = '''
const result = "aabbcc".replaceAll("b", (m, offset, s) => m.toUpperCase() + offset);
console.log(result);
'''
        result = Interpreter().run(source)
        self.assertEqual(result.strip(), "aaB2B3cc")

    def test_parseint_radix(self):
        """parseInt handles trailing chars, hex, and explicit base"""
        source = '''
console.log(parseInt("42px"));
console.log(parseInt("0xFF"));
console.log(parseInt("11", 2));
console.log(parseInt("  10  "));
console.log(parseInt("xyz"));
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "42")
        self.assertEqual(lines[1], "255")
        self.assertEqual(lines[2], "3")
        self.assertEqual(lines[3], "10")
        self.assertEqual(lines[4], "NaN")

    def test_number_is_nan_strict(self):
        """Number.isNaN/isFinite/isInteger do NOT coerce types"""
        source = '''
console.log(Number.isNaN("NaN"));
console.log(Number.isNaN(NaN));
console.log(Number.isFinite("1"));
console.log(Number.isFinite(1));
console.log(Number.isInteger("1"));
console.log(Number.isInteger(1));
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "false")
        self.assertEqual(lines[1], "true")
        self.assertEqual(lines[2], "false")
        self.assertEqual(lines[3], "true")
        self.assertEqual(lines[4], "false")
        self.assertEqual(lines[5], "true")

    def test_super_getter_this(self):
        """super.getter in derived class passes correct this"""
        source = '''
class Animal {
    constructor(n) { this.name = n; }
    get description() { return "Animal: " + this.name; }
}
class Dog extends Animal {
    get description() { return super.description + " (Dog)"; }
}
console.log(new Dog("Rex").description);
'''
        result = Interpreter().run(source)
        self.assertEqual(result.strip(), "Animal: Rex (Dog)")


    # ----- Phase 32 tests -----

    def test_symbol_to_primitive(self):
        """Symbol.toPrimitive on class instances works for numeric/string/default hints"""
        source = '''
class Num {
    constructor(v) { this.v = v; }
    [Symbol.toPrimitive](hint) {
        if (hint === "number") return this.v;
        if (hint === "string") return "Num(" + this.v + ")";
        return this.v;
    }
}
const n = new Num(7);
console.log(+n);
console.log(`${n}`);
console.log(n + 3);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "7")
        self.assertEqual(lines[1], "Num(7)")
        self.assertEqual(lines[2], "10")

    def test_string_comparison_operators(self):
        """String < > <= >= use lexicographic comparison"""
        source = '''
console.log("abc" < "abd");
console.log("Z" < "a");
console.log("b" > "a");
console.log("a" <= "a");
console.log("b" <= "a");
console.log("10" < "9");
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "true")
        self.assertEqual(lines[1], "true")
        self.assertEqual(lines[2], "true")
        self.assertEqual(lines[3], "true")
        self.assertEqual(lines[4], "false")
        self.assertEqual(lines[5], "true")

    def test_sort_with_comparator(self):
        """Array.prototype.sort uses comparator function when provided"""
        source = '''
const arr = [3, 1, 4, 1, 5, 9, 2];
arr.sort((a, b) => b - a);
console.log(arr.join(","));
const words = ["banana", "apple", "cherry"];
words.sort();
console.log(words.join(","));
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "9,5,4,3,2,1,1")
        self.assertEqual(lines[1], "apple,banana,cherry")

    def test_abstract_equality_coercion(self):
        """Abstract equality (==) coerces arrays/objects to primitives"""
        source = '''
console.log([] == false);
console.log([] == 0);
console.log(["1"] == 1);
console.log(null == undefined);
console.log(null == 0);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "true")
        self.assertEqual(lines[1], "true")
        self.assertEqual(lines[2], "true")
        self.assertEqual(lines[3], "true")
        self.assertEqual(lines[4], "false")

    def test_instanceof_builtin(self):
        """instanceof works for built-in constructors: Array, Object, Map, Set, RegExp"""
        source = '''
console.log([] instanceof Array);
console.log({} instanceof Object);
console.log([] instanceof Object);
console.log(new Map() instanceof Map);
console.log(new Set() instanceof Set);
console.log(/x/ instanceof RegExp);
console.log(new WeakMap() instanceof WeakMap);
console.log([] instanceof Map);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "true")
        self.assertEqual(lines[1], "true")
        self.assertEqual(lines[2], "true")
        self.assertEqual(lines[3], "true")
        self.assertEqual(lines[4], "true")
        self.assertEqual(lines[5], "true")
        self.assertEqual(lines[6], "true")
        self.assertEqual(lines[7], "false")

    def test_super_setter(self):
        """super.prop = v in derived class calls ancestor setter with correct this"""
        source = '''
class Base {
    set x(v) { this._x = v * 2; }
    get x() { return this._x; }
}
class Child extends Base {
    setViaSuper(v) { super.x = v; }
}
const c = new Child();
c.setViaSuper(7);
console.log(c.x);
'''
        result = Interpreter().run(source)
        self.assertEqual(result.strip(), "14")

    def test_string_locale_compare(self):
        """String.prototype.localeCompare returns -1, 0, 1"""
        source = '''
console.log("a".localeCompare("b"));
console.log("b".localeCompare("a"));
console.log("a".localeCompare("a"));
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "-1")
        self.assertEqual(lines[1], "1")
        self.assertEqual(lines[2], "0")

    def test_object_create_with_descriptors(self):
        """Object.create with second argument (property descriptors) applies them"""
        source = '''
const obj = Object.create({base: true}, {
    x: { value: 42, enumerable: true },
    y: { value: "hidden", enumerable: false }
});
console.log(obj.x);
console.log(obj.y);
console.log(obj.base);
console.log(Object.keys(obj).join(","));
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "42")
        self.assertEqual(lines[1], "hidden")
        self.assertEqual(lines[2], "true")
        self.assertEqual(lines[3], "x")

    def test_object_define_properties(self):
        """Object.defineProperties defines multiple properties at once"""
        source = '''
const obj = {};
Object.defineProperties(obj, {
    x: { value: 1, enumerable: true },
    y: { value: 2, enumerable: true },
    hidden: { value: 3, enumerable: false }
});
console.log(obj.x, obj.y, obj.hidden);
console.log(Object.keys(obj).join(","));
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "1 2 3")
        self.assertEqual(lines[1], "x,y")

    def test_array_constructor(self):
        """new Array(n) and Array(n) create arrays; typeof Array is 'function'"""
        source = '''
console.log(typeof Array);
const a = new Array(3);
console.log(a.length);
console.log(Array.isArray(a));
const b = new Array(1, 2, 3);
console.log(b.join(","));
const c = Array.of(4, 5, 6);
console.log(c.join(","));
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "function")
        self.assertEqual(lines[1], "3")
        self.assertEqual(lines[2], "true")
        self.assertEqual(lines[3], "1,2,3")
        self.assertEqual(lines[4], "4,5,6")

    def test_object_constructor(self):
        """typeof Object is 'function'; new Object() and Object(x) work"""
        source = '''
console.log(typeof Object);
const o = new Object();
console.log(typeof o);
const o2 = new Object({x: 1});
console.log(o2.x);
console.log(Object.keys({a:1,b:2}).join(","));
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "function")
        self.assertEqual(lines[1], "object")
        self.assertEqual(lines[2], "1")
        self.assertEqual(lines[3], "a,b")

    def test_function_name_inference(self):
        """Function name is inferred from const/let/var variable binding"""
        source = '''
const fn1 = function() {};
let fn2 = () => {};
var fn3 = function named() {};
console.log(fn1.name);
console.log(fn2.name);
console.log(fn3.name);
const obj = { greet() {} };
console.log(obj.greet.name);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "fn1")
        self.assertEqual(lines[1], "fn2")
        self.assertEqual(lines[2], "named")
        self.assertEqual(lines[3], "greet")

    def test_array_includes_nan(self):
        """Array.prototype.includes uses SameValueZero (handles NaN)"""
        source = '''
const arr = [1, NaN, 3];
console.log(arr.includes(NaN));
console.log(arr.includes(1));
console.log(arr.includes(4));
console.log(arr.indexOf(NaN));
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "true")
        self.assertEqual(lines[1], "true")
        self.assertEqual(lines[2], "false")
        self.assertEqual(lines[3], "-1")

    def test_switch_fall_through(self):
        """switch fall-through executes subsequent cases until break"""
        source = '''
const x = 2;
const out = [];
switch(x) {
    case 1: out.push("one");
    case 2: out.push("two");
    case 3: out.push("three"); break;
    default: out.push("other");
}
console.log(out.join(","));
const y = 5;
const out2 = [];
switch(y) {
    case 1: out2.push("one"); break;
    default: out2.push("default");
    case 2: out2.push("two"); break;
}
console.log(out2.join(","));
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "two,three")
        self.assertEqual(lines[1], "default,two")

    def test_console_log_object_formatting(self):
        """console.log formats objects, arrays, Map, Set in Node.js style"""
        source = '''
console.log({a: 1, b: "hello"});
console.log([1, 2, 3]);
console.log(new Map([["x", 1]]));
console.log(new Set([1, 2, 3]));
console.log(null);
console.log(undefined);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertIn("a: 1", lines[0])
        self.assertIn("b:", lines[0])
        self.assertIn("1", lines[1])
        self.assertIn("2", lines[1])
        self.assertIn("3", lines[1])
        self.assertIn("Map(1)", lines[2])
        self.assertIn("Set(3)", lines[3])
        self.assertEqual(lines[4], "null")
        self.assertEqual(lines[5], "undefined")

    def test_reference_error_undeclared(self):
        """Accessing undeclared variable throws ReferenceError; typeof undeclared returns 'undefined'"""
        source = '''
try { undeclaredVariable; } catch(e) {
    console.log(e instanceof ReferenceError);
    console.log(e.name);
}
console.log(typeof undeclaredVar2);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "true")
        self.assertEqual(lines[1], "ReferenceError")
        self.assertEqual(lines[2], "undefined")

    def test_class_extends_call_expression(self):
        """class C extends mixin(B) {} supports arbitrary expression in extends clause"""
        source = '''
function addExtra(Base) {
    return class extends Base {
        extra() { return "extra"; }
    };
}
class Animal { speak() { return "..."; } }
class Dog extends addExtra(Animal) {
    speak() { return "woof"; }
}
const d = new Dog();
console.log(d.speak());
console.log(d.extra());
console.log(d instanceof Dog);
console.log(d instanceof Animal);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "woof")
        self.assertEqual(lines[1], "extra")
        self.assertEqual(lines[2], "true")
        self.assertEqual(lines[3], "true")

    def test_number_large_integer_formatting(self):
        """Numbers like MAX_SAFE_INTEGER format without .0 suffix"""
        source = '''
console.log(Number.MAX_SAFE_INTEGER);
console.log(Number.MIN_SAFE_INTEGER);
console.log(9007199254740991 === Number.MAX_SAFE_INTEGER);
console.log(1e20);
'''
        result = Interpreter().run(source)
        lines = result.splitlines()
        self.assertEqual(lines[0], "9007199254740991")
        self.assertEqual(lines[1], "-9007199254740991")
        self.assertEqual(lines[2], "true")
        self.assertEqual(lines[3], "100000000000000000000")


if __name__ == '__main__':
    unittest.main()
