"""The program form (ADR-0008): the message as one fenced block of statements, read whole and checked
before anything runs, its expressions evaluated by the runtime over data.

    note["book1"] = {title: "…", author: "…"}
    similar = search(input=*note["book1"].title, candidates=*catalog)
    chosen = *similar if *similar != [] else DO search(input=*note["book1"].author, candidates=*catalog)
    OUT{books: (b for b in *chosen if b.score > 0.8)}

Python's parser reads the block once `*name`, `DO name(` and `OUT{` are marked; the tree is held to the
closed set below and lowered to nodes: a command, a value, a note write, a branch, the output. Nothing
in it is ever handed to `eval`.
"""

from __future__ import annotations

import ast
import io
import operator
import re
import tokenize
import warnings
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from itertools import combinations
from typing import Any, Protocol

from pydantic import BaseModel

from .lndl import LNDLError, _close_dangling, _escape_newlines_in_strings, _insert_commas, _line_offsets

__all__ = (
    "LIMIT",
    "Repair",
    "REPAIRS",
    "EvalError",
    "UnknownCommand",
    "Command",
    "Value",
    "Write",
    "Branch",
    "Output",
    "Node",
    "Script",
    "read",
    "check",
    "Env",
    "evaluate",
    "guidance",
)

LIMIT = 100_000  # characters and items, counted through nesting: the most one built value may hold


@dataclass(frozen=True)
class Repair:
    """One entry of the catalogue (ADR-0008/D2): a rewrite of the block, None when it does not apply."""

    name: str
    rewrite: Callable[[str], str | None]


REPAIRS: tuple[Repair, ...] = (  # the readings of ADR-0004/C3, in the order a combination applies them
    Repair("closers", _close_dangling),
    Repair("newline", _escape_newlines_in_strings),
    Repair("comma", _insert_commas),
)


class EvalError(LNDLError):
    """An expression that cannot be computed: the node it belongs to settles `invalid`."""


class UnknownCommand(LNDLError):
    """A command outside the profile's subset; `names` carries it for the run's count."""

    def __init__(self, message: str, names: tuple[str, ...]):
        super().__init__(message)
        self.names = names


@dataclass(frozen=True)
class Command:
    """`x = name(...)`, a bare `name(...)`, or what a note assignment runs; `alias` None: the runtime
    names it."""

    line: int
    name: str
    args: tuple[tuple[str, ast.expr], ...]
    alias: str | None = None


@dataclass(frozen=True)
class Value:
    """`x = expression`: computed when first read."""

    line: int
    alias: str
    expr: ast.expr


@dataclass(frozen=True)
class Write:
    """`note["key"]["path"] = ...`: written when its block reaches it, once what it writes has settled."""

    line: int
    key: str
    path: tuple[str, ...]
    value: Command | ast.expr


@dataclass(frozen=True)
class Branch:
    """`if`/`elif`/`else`: its control result gates every later node of its block."""

    line: int
    test: ast.expr
    body: tuple[Node, ...]
    orelse: tuple[Node, ...]


@dataclass(frozen=True)
class Output:
    """`OUT{...}` or `return {...}`: the candidate for the run's output; `named`, the pointers in it."""

    line: int
    fields: tuple[tuple[str, ast.expr], ...]
    named: frozenset[str]


Node = Command | Value | Write | Branch | Output


@dataclass(frozen=True)
class Script:
    body: tuple[Node, ...] = ()
    problems: tuple[str, ...] = ()  # each repair the reading applied, as the model is told it
    repairs: tuple[str, ...] = ()  # the same repairs by catalogue name, for the run's count


_FENCE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)
_PTR = "__ptr_"
_DO = "__do_"
_OUT = "__out__"
_NOTE = ("note", _PTR + "note")
_WORDS = {"true": True, "false": False, "null": None}
_NAME = re.compile(r"[A-Za-z]\w*")
_RESERVED = ("DO", "OUT", "false", "note", "null", "true")
_PREFIX_OPS = {"=", "(", "[", "{", ",", ":", "+", "-", "*", "/", "==", "!=", "<", "<=", ">", ">="}
_PREFIX_WORDS = {"and", "or", "not", "if", "elif", "else", "in", "return"}
_ARITH = {
    ast.Add: ("+", operator.add),
    ast.Sub: ("-", operator.sub),
    ast.Mult: ("*", operator.mul),
    ast.Div: ("/", operator.truediv),
}
_CMP = {
    ast.Eq: ("==", operator.eq),
    ast.NotEq: ("!=", operator.ne),
    ast.Lt: ("<", operator.lt),
    ast.LtE: ("<=", operator.le),
    ast.Gt: (">", operator.gt),
    ast.GtE: (">=", operator.ge),
}


def read(text: str) -> Script:
    """The turn's program: its one fenced block read and lowered to nodes, the catalogue tried in order
    when it does not read as written. LNDLError, naming the line, when no reading reads."""
    blocks = _FENCE.findall(text)
    if len(blocks) > 1:
        raise LNDLError(f"{len(blocks)} fenced blocks: a turn is one program in one block")
    if not blocks:
        return Script()
    source = blocks[0].rstrip("\n")  # a closer supplied at the end lands on the last line
    try:
        return Script(_parse(source))
    except LNDLError as e:
        first = e
    for applied in (c for k in range(1, len(REPAIRS) + 1) for c in combinations(REPAIRS, k)):  # fewest first
        fixed = source
        for r in applied:
            fixed = r.rewrite(fixed) or fixed
        if fixed == source:
            continue
        try:
            body = _parse(fixed)
        except LNDLError:
            continue
        old, new = source.split("\n"), fixed.split("\n")
        n = next(i for i, (a, b) in enumerate(zip(old, new, strict=False)) if a != b)
        told = f"line {n + 1}: read as {new[n]!r}; if that is not what you meant, write it again"
        return Script(body, (told,), tuple(r.name for r in applied))
    raise first


def _parse(source: str) -> tuple[Node, ...]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(_mark(source), mode="exec")
    except SyntaxError as e:
        raise LNDLError(f"line {e.lineno}: {e.msg}; the program was not run") from None
    except ValueError as e:  # a NUL byte
        raise LNDLError(f"{e}; the program was not run") from None
    return _block(tree.body)


def _mark(source: str) -> str:
    """`*name` where a value starts, `DO name(` and `OUT{` rewritten into names Python's parser reads;
    token level, so a `*` in a string or between two operands is left alone. A block that does not
    tokenize is handed on as written, for the parser to name its line."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError, ValueError):
        return source
    offsets = _line_offsets(source)

    def at(pos: tuple[int, int]) -> int:
        return offsets[pos[0] - 1] + pos[1]

    spans: list[tuple[int, int, str]] = []
    prev = None
    for tok, nxt in zip(tokens, tokens[1:], strict=False):
        if tok.type == tokenize.NAME and tok.string.startswith("__"):
            raise LNDLError(f"line {tok.start[0]}: {tok.string}: names starting with __ are the runtime's")
        if tok.type == tokenize.NAME and tok.string == "DO" and nxt.type == tokenize.NAME:
            spans.append((at(tok.start), at(nxt.start), _DO))
        elif tok.type == tokenize.NAME and tok.string == "OUT" and nxt.string == "{":
            spans.append((at(tok.start), at(tok.end), _OUT + " ="))
        elif (
            tok.string == "*" and nxt.type == tokenize.NAME and at(nxt.start) == at(tok.end) and _prefix(prev)
        ):
            spans.append((at(tok.start), at(tok.end), _PTR))
        if tok.type not in (tokenize.NL, tokenize.COMMENT):
            prev = tok
    for start, end, repl in reversed(spans):
        source = source[:start] + repl + source[end:]
    return source


def _prefix(prev: tokenize.TokenInfo | None) -> bool:
    """Whether a `*` after `prev` stands where a value starts, so it is a pointer and not a product."""
    if prev is None or prev.type in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
        return True
    return prev.string in (_PREFIX_OPS if prev.type == tokenize.OP else _PREFIX_WORDS)


def _refuse(node: ast.AST, why: str) -> LNDLError:
    return LNDLError(f"line {node.lineno}: {why}; the program was not run")


def _block(stmts: list[ast.stmt]) -> tuple[Node, ...]:
    nodes: list[Node] = []
    for st in stmts:
        match st:
            case ast.Assign(targets=[ast.Name(id=name)], value=ast.Dict() as value) if name == _OUT:
                nodes.append(_output(st, value))
            case ast.Assign(targets=[ast.Name(id=name)], value=value):
                _bindable(st, name)
                nodes.append(_rhs(value, name))
            case ast.Assign(targets=[target], value=value) if (path := _note_path(target)) is not None:
                nodes.append(Write(st.lineno, path[0], path[1], _rhs(value, None)))
            case ast.Expr(value=ast.Call() as call):
                nodes.append(_rhs(call, None))
            case ast.If(test=test, body=body, orelse=orelse):
                _expr(test, frozenset())
                nodes.append(Branch(st.lineno, test, _block(body), _block(orelse)))
            case ast.Return(value=ast.Dict() as value):
                nodes.append(_output(st, value))
            case _:
                raise _refuse(
                    st,
                    'a statement binds a name (x = ...), writes a note (note["key"] = ...), runs a '
                    "command, or is if/elif/else, return {...} or OUT{...}",
                )
    return tuple(nodes)


def _bindable(node: ast.AST, name: str) -> None:
    if not _NAME.fullmatch(name) or name in _RESERVED:
        raise _refuse(
            node, f"{name!r} cannot be bound: a name starts with a letter and is none of {_RESERVED}"
        )


def _rhs(value: ast.expr, alias: str | None) -> Any:
    """A statement's right side: a call is the command node itself, `DO` or not; else an expression,
    a value node when it binds a name."""
    if isinstance(value, ast.Call):
        name = _command(value).removeprefix(_DO)
        if value.args:
            raise _refuse(value, f"{name}(...): arguments are keyword only, write field=value")
        for kw in value.keywords:
            if kw.arg is None:
                raise _refuse(value, f"{name}(...): ** unpacking is not allowed")
            _expr(kw.value, frozenset())
        return Command(value.lineno, name, tuple((kw.arg, kw.value) for kw in value.keywords), alias)
    _expr(value, frozenset())
    return value if alias is None else Value(value.lineno, alias, value)


def _output(st: ast.stmt, value: ast.Dict) -> Output:
    _expr(value, frozenset())
    named = frozenset(
        n.id.removeprefix(_PTR)
        for n in ast.walk(value)
        if isinstance(n, ast.Name) and n.id.startswith(_PTR) and n.id not in _NOTE
    )
    return Output(
        st.lineno, tuple((_key(value, k), v) for k, v in zip(value.keys, value.values, strict=True)), named
    )


def _command(call: ast.Call) -> str:
    name = _dotted(call.func)
    if name is None:
        raise _refuse(call, "the command must be a name: name(field=value, ...)")
    return name


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _note_path(node: ast.expr) -> tuple[str, tuple[str, ...]] | None:
    """`note["k"]["a"]`, `note.k.a` or `*note["k"].a`: the note's key and the path inside it."""
    steps: list[str] = []
    while isinstance(node, ast.Attribute | ast.Subscript):
        if isinstance(node, ast.Attribute):
            steps.append(node.attr)
        elif isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            steps.append(node.slice.value)
        else:
            return None
        node = node.value
    if isinstance(node, ast.Name) and node.id in _NOTE and steps:
        steps.reverse()
        return steps[0], tuple(steps[1:])
    return None


def _expr(node: ast.expr, bound: frozenset[str]) -> None:
    """The closed set of C3; anything else refuses the message, naming its line."""
    if _note_path(node) is not None:
        return
    match node:
        case ast.Constant(value=v):
            if not (v is None or isinstance(v, str | int | float)):
                raise _refuse(node, f"{v!r} is not a value: strings, numbers, true, false and null are")
        case ast.Name(id=name):
            if name in _NOTE:
                raise _refuse(node, 'a note is read as note["key"]')
            if name.startswith(_DO):
                raise _refuse(node, "DO names a command call: DO name(field=value, ...)")
            if not (name.startswith(_PTR) or name in bound or name in _WORDS):
                raise _refuse(node, f"{name}: a name is read by pointing at it, *{name}")
        case ast.Attribute(value=value):
            _expr(value, bound)
        case ast.Subscript(value=value, slice=index):
            _expr(value, bound)
            _expr(index, bound)
        case ast.BinOp(left=left, op=op, right=right) if type(op) in _ARITH:
            _expr(left, bound)
            _expr(right, bound)
        case ast.UnaryOp(op=ast.Not() | ast.USub(), operand=operand):
            _expr(operand, bound)
        case ast.BoolOp(values=values):
            for v in values:
                _expr(v, bound)
        case ast.Compare(left=left, ops=ops, comparators=rights) if all(type(o) in _CMP for o in ops):
            for v in (left, *rights):
                _expr(v, bound)
        case ast.IfExp(test=test, body=body, orelse=orelse):
            for v in (test, body, orelse):
                _expr(v, bound)
        case ast.List(elts=elts):
            for v in elts:
                _expr(v, bound)
        case ast.Dict(keys=keys, values=values):
            for k, v in zip(keys, values, strict=True):
                _key(node, k)
                _expr(v, bound)
        case ast.GeneratorExp(
            elt=elt, generators=[ast.comprehension(target=ast.Name(id=var), iter=seq, ifs=ifs, is_async=0)]
        ):
            _bindable(node, var)
            _expr(seq, bound)
            for v in (*ifs, elt):
                _expr(v, bound | {var})
        case ast.Call() if _command(node).startswith(_DO):
            _rhs(node, None)
        case ast.Call():
            raise _refuse(node, "inside an expression a command is written DO name(field=value, ...)")
        case _:
            raise _refuse(node, f"{type(node).__name__} is not part of the program form")


def _key(node: ast.Dict, key: ast.expr | None) -> str:
    """A dict display's key: a bare word or a string. A pointer stands where a value stands."""
    if isinstance(key, ast.Name) and not key.id.startswith("__"):
        return key.id
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return key.value
    raise _refuse(node, "a key is a bare word or a string; a pointer stands where a value stands")


def check(script: Script, commands: Iterable[str], known: Callable[[str], bool]) -> None:
    """ADR-0008/C1 over the read program, before anything runs: every command in the subset, every
    pointer at a name bound on an earlier line of an enclosing block or on the record, every name bound
    once. A pointer reaches only backwards, so no cycle over reference and control edges can form."""
    names = set(commands)
    taken: set[str] = set()

    def expr(node: ast.expr, visible: set[str]) -> None:
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                command(n.lineno, _command(n).removeprefix(_DO))
            elif isinstance(n, ast.Name) and n.id.startswith(_PTR) and n.id not in _NOTE:
                name = n.id.removeprefix(_PTR)
                if name not in visible and not known(name):
                    raise _refuse(n, f"*{name} names nothing bound on an earlier line or on the record")

    def command(line: int, name: str) -> None:
        if name not in names:
            raise UnknownCommand(f"line {line}: unknown command {name!r}; the program was not run", (name,))

    def block(nodes: Iterable[Node], visible: set[str]) -> None:
        visible = set(visible)
        for node in nodes:
            match node:
                case Branch(test=test, body=body, orelse=orelse):
                    expr(test, visible)
                    block(body, visible)
                    block(orelse, visible)
                case Output(fields=fields):
                    for _, v in fields:
                        expr(v, visible)
                case Write(value=Command() as value) | (Command() as value):
                    command(value.line, value.name)
                    for _, v in value.args:
                        expr(v, visible)
                case Write(value=value):
                    expr(value, visible)
                case _:
                    expr(node.expr, visible)
            alias = getattr(node, "alias", None)
            if alias is not None:
                if alias in taken or known(alias):
                    raise LNDLError(
                        f"line {node.line}: {alias} is bound already: a name is bound once for the whole "
                        "run; the program was not run"
                    )
                taken.add(alias)
                visible.add(alias)

    block(script.body, set())


class Env(Protocol):
    """What the evaluator asks of the run: a pointer's value, a note's value, a command's result."""

    def point(self, name: str) -> Awaitable[Any]: ...
    def note(self, key: str) -> Awaitable[Any]: ...
    def do(self, name: str, args: dict[str, Any], line: int) -> Awaitable[Any]: ...


async def evaluate(node: ast.expr, env: Env, bound: dict[str, Any] | None = None) -> Any:
    """ADR-0008/C3: the value of a checked expression. Access and indexing read and never call; the one
    call is `DO`, a command node. EvalError names what could not be computed."""
    bound = bound or {}
    if (path := _note_path(node)) is not None:
        value = await env.note(path[0])
        for step in path[1]:
            value = _index(node, value, step)
        return value
    match node:
        case ast.Constant(value=v):
            return v
        case ast.Name(id=name) if name in bound:
            return bound[name]
        case ast.Name(id=name) if name in _WORDS:
            return _WORDS[name]
        case ast.Name(id=name):
            return await env.point(name.removeprefix(_PTR))
        case ast.Attribute(value=value, attr=attr):
            return _index(node, await evaluate(value, env, bound), attr)
        case ast.Subscript(value=value, slice=index):
            return _index(node, await evaluate(value, env, bound), await evaluate(index, env, bound))
        case ast.BinOp(left=left, op=op, right=right):
            a, b = await evaluate(left, env, bound), await evaluate(right, env, bound)
            return _arith(node, _ARITH[type(op)], a, b)
        case ast.UnaryOp(op=op, operand=operand):
            v = await evaluate(operand, env, bound)
            if isinstance(op, ast.Not):
                return not v
            try:
                return -v
            except TypeError:
                raise EvalError(f"line {node.lineno}: - needs a number, not {_kind(v)}") from None
        case ast.BoolOp(op=op, values=values):
            for v in values:
                last = await evaluate(v, env, bound)
                if bool(last) is isinstance(op, ast.Or):
                    return last
            return last
        case ast.Compare(left=left, ops=ops, comparators=rights):
            a = await evaluate(left, env, bound)
            for op, right in zip(ops, rights, strict=True):
                b = await evaluate(right, env, bound)
                sign, fn = _CMP[type(op)]
                try:
                    ok = fn(a, b)
                except TypeError:
                    raise EvalError(
                        f"line {node.lineno}: cannot compare {_kind(a)} {sign} {_kind(b)}"
                    ) from None
                if not ok:
                    return False
                a = b
            return True
        case ast.IfExp(test=test, body=body, orelse=orelse):
            return await evaluate(body if await evaluate(test, env, bound) else orelse, env, bound)
        case ast.List(elts=elts):
            return _built(node, [await evaluate(v, env, bound) for v in elts])
        case ast.Dict(keys=keys, values=values):
            pairs = zip(keys, values, strict=True)
            return _built(node, {_key(node, k): await evaluate(v, env, bound) for k, v in pairs})
        case ast.Call(keywords=keywords):
            args = {kw.arg: await evaluate(kw.value, env, bound) for kw in keywords}
            return await env.do(_command(node).removeprefix(_DO), args, node.lineno)
        case _:  # the comprehension, the only loop
            (gen,) = node.generators
            seq = await evaluate(gen.iter, env, bound)
            if not isinstance(seq, list | tuple | dict | str):
                raise EvalError(f"line {node.lineno}: a comprehension runs over a list, not {_kind(seq)}")
            out, size = [], 0
            for item in seq:
                inner = bound | {gen.target.id: item}
                for c in gen.ifs:
                    if not await evaluate(c, env, inner):
                        break
                else:
                    out.append(await evaluate(node.elt, env, inner))
                    size += 1 + _size(out[-1])  # the item and what it holds
                    if size > LIMIT:
                        raise _past(node)
            return out


def _index(node: ast.expr, value: Any, key: Any) -> Any:
    """`.field` or `[i]`: a dict's key, a list's or a string's position, a Spec's field; read, never
    called."""
    if isinstance(value, BaseModel) and isinstance(key, str) and key in type(value).model_fields:
        return getattr(value, key)
    if isinstance(value, dict) and isinstance(key, str):
        if key not in value:
            raise EvalError(f"line {node.lineno}: no field {key!r} in the dict (it has {sorted(value)[:10]})")
        return value[key]
    if isinstance(value, list | tuple | str) and isinstance(key, int) and not isinstance(key, bool):
        if not -len(value) <= key < len(value):
            raise EvalError(
                f"line {node.lineno}: index {key} is past the end of {_kind(value)}[{len(value)}]"
            )
        return value[key]
    raise EvalError(f"line {node.lineno}: {_kind(value)} cannot be read at {key!r}")


def _arith(node: ast.expr, op: tuple[str, Callable[[Any, Any], Any]], a: Any, b: Any) -> Any:
    sign, fn = op
    seq, times = (b, a) if isinstance(a, int) else (a, b)
    if sign == "*" and isinstance(seq, list | str) and isinstance(times, int) and _size(seq) * times > LIMIT:
        raise _past(node)  # refused before it is built, not after
    try:
        value = fn(a, b)
    except TypeError:
        raise EvalError(f"line {node.lineno}: cannot apply {sign} to {_kind(a)} and {_kind(b)}") from None
    except ZeroDivisionError:
        raise EvalError(f"line {node.lineno}: division by zero") from None
    return _built(node, value)


def _built(node: ast.expr, value: Any) -> Any:
    if _size(value) > LIMIT:
        raise _past(node)
    return value


def _past(node: ast.expr) -> EvalError:
    return EvalError(f"line {node.lineno}: the value built here is past the size limit of {LIMIT}")


def _size(value: Any) -> int:
    """Characters and items through nesting, counted until past LIMIT and no further."""
    n, stack = 0, [value]
    while stack and n <= LIMIT:
        v = stack.pop()
        if isinstance(v, str):
            n += len(v)
        elif isinstance(v, dict):
            n += len(v) + sum(len(k) for k in v)  # a key is characters too
            stack.extend(v.values())
        elif isinstance(v, list | tuple):
            n += len(v)
            stack.extend(v[: LIMIT + 1])
        else:
            n += 1
    return n


def _kind(value: Any) -> str:
    return "null" if value is None else type(value).__name__


def guidance(outputs: tuple[str, ...] = (), commands: Iterable[str] | None = None) -> str:
    """The program section of the system prompt, in place of the three constructs' (ADR-0008/C6)."""
    o = outputs[0] if outputs else "name"
    show = "*name or context.show" if commands is None or "context.show" in set(commands) else "*name"
    return (
        "You act by writing one program in your reply, in one fenced block (```). Prose outside the "
        "block is yours and runs nothing; inside it, one statement per line, blocks by indent, # starts "
        "a comment:\n"
        "  r1 = name(field=value, ...)        run one of the commands listed below; its result is *r1\n"
        '  t1 = *r1[0].title + " "            a value computed from results; it costs nothing until read\n'
        '  note["key"] = *t1                  write a note; note["key"] reads one, note["key"].field a part\n'
        '  if *t1 != "":                      a branch, with elif and else; a name bound in it stays there\n'
        f"      return {{{o}: *t1}}           finish from inside a branch\n"
        f"  OUT{{{o}: (x for x in *r1 if x.score > 0.8)}}   finish: each output an expression "
        f"(this run's outputs: {', '.join(outputs) if outputs else 'listed below'})\n"
        "An expression is data: *name, .field, [i], + - * /, comparisons, and, or, not, literals "
        "({key: value} and [a, b]), a if c else b, and one comprehension (x for x in *r if ...). A command "
        "inside an expression is DO name(field=value), run only when that part is reached. A name is bound "
        "once and read by pointing at it; *_r41 reaches any entry by its record name.\n"
        "The whole block is read before any of it runs: a block that cannot be read runs nothing, and you "
        f"are told the line. A result another line read, or a note took, is shown as a placeholder ({show} "
        "brings it back); a result nothing read, and what OUT{} names, is shown whole."
    )
