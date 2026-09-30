"""LNDL: the three constructs the runtime extracts from a turn (ADR-0004/C1).

    <lvar alias>value</lvar>                    name a value
    <lvar spec.field alias>value</lvar>          name a value that fills one field of an output Spec
    <lact alias>name(field=..., x=*other)</lact> request that a thing happen; `*other` is a pointer
    OUT{spec: [alias, ...], other: literal}     finish with typed output

A call's string argument may be a heredoc, `field=<<TAG` then the lines verbatim then `TAG` alone
on a line, so a script or a code block needs no escaping. Everything else in the text is prose and
is ignored. Pointers (`*name`) are left unresolved here; the runtime dereferences them at execution
time.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
import warnings
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from keyword import iskeyword
from typing import Any

__all__ = (
    "LNDLError",
    "Pointer",
    "Lvar",
    "Lact",
    "pointers_in",
    "Aliases",
    "Out",
    "Program",
    "parse_call",
    "parse_call_noting",
    "stray_tags",
    "parse_out",
    "spans",
    "plain",
    "parse",
    "guidance",
)


class LNDLError(ValueError):
    """The turn's LNDL could not be read. The message is what the model is told next round."""


@dataclass(frozen=True)
class Pointer:
    """`*name`: a reference to a value on the record or in the note store."""

    name: str


@dataclass(frozen=True)
class Lvar:
    alias: str
    value: str
    spec: str | None = None  # `<lvar spec.field alias>`: which output Spec this fills
    field: str | None = None


@dataclass(frozen=True)
class Lact:
    alias: str
    name: str  # the Spec being asked for
    args: dict[str, Any]  # literals, with Pointer where the model wrote `*name`
    spec: str | None = None  # `<lact spec.field alias>`: the result fills this output field
    field: str | None = None

    def pointers(self) -> list[Pointer]:
        return pointers_in(self.args)


def pointers_in(value: Any) -> list[Pointer]:
    """Every Pointer inside a literal, however nested."""
    found: list[Pointer] = []

    def walk(v: Any) -> None:
        if isinstance(v, Pointer):
            found.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list | tuple):
            for x in v:
                walk(x)

    walk(value)
    return found


class Aliases(list[str]):
    """`[a, b]` in OUT{}: bare names, each an alias. A bracket holding anything else is a literal."""


@dataclass(frozen=True)
class Out:
    fields: dict[str, Aliases | Any]  # spec name -> aliases, or a literal


@dataclass(frozen=True)
class Program:
    lvars: list[Lvar] = field(default_factory=list)
    lacts: list[Lact] = field(default_factory=list)
    out: Out | None = None
    problems: list[str] = field(default_factory=list)  # constructs dropped from this turn, and why
    repairs: list[str] = field(default_factory=list)  # each repair a call's reading applied, by name

    @property
    def aliases(self) -> set[str]:
        return {v.alias for v in self.lvars} | {a.alias for a in self.lacts}


_OPEN_TAG = re.compile(r"<(lvar|lact)\s+([^>]+?)\s*>")
_ANY_TAG = re.compile(r"<(/?)(lvar|lact)(?:\s+[^>]*?)?\s*>")  # an opener or a closer of either kind
_IDENT = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")
# the reader's own names for a pointer and a heredoc while Python's parser reads the call; a name the
# model wrote with the same `__` start is refused, so no string, and no name it writes, is taken for one
_PTR = "__ptr_"
_DOC = "__doc_"
_WORDS = {"true": True, "false": False, "null": None}  # JSON's spellings, where Python's were expected
_DEEP = "it is nested too deeply to read"
_CALLISH = re.compile(r"^[A-Za-z_][\w.]*\s*\(")
# `key=<<TAG` then the lines verbatim then TAG alone on a line: a string argument with no escaping.
# Shell's heredoc, which is how models already write multi-line text; `<<'TAG'` is accepted too.
# The closing line may be followed by `,` or `)`, by the next argument with the comma left out
# (repaired downstream), or by nothing (the call's closers left off, repaired likewise).


_ATTR = re.compile(r"""^(?:alias|name|id)\s*=\s*["']?([^"'\s]+)["']?$""")


def _header(raw: str, tag: str) -> tuple[str, str | None, str | None]:
    """`alias` or `spec.field alias` -> (alias, spec, field). `alias="x"` is read as `x`."""
    tokens = [(_ATTR.match(t) or [t, t])[1] for t in raw.split()]
    if len(tokens) == 1 and ("." not in tokens[0] or tokens[0].startswith("note.")):
        alias = tokens[0]
        spec = fld = None
    elif len(tokens) == 1:  # `<lvar report.summary>`: the field name doubles as the alias
        alias = tokens[0]
        spec, fld = tokens[0].split(".", 1)
    elif len(tokens) == 2 and "." in tokens[0]:
        spec, fld = tokens[0].split(".", 1)
        alias = tokens[1]
    else:
        raise LNDLError(f"<{tag} {raw}>: the header is `alias` or `spec.field alias`, space separated")
    if not _IDENT.match(alias):
        raise LNDLError(f"<{tag} {raw}>: {alias!r} is not a valid alias")
    if alias in ("True", "False", "None"):
        # OUT{} reads these three spellings as Python literals, never as Aliases (`parse_out` below),
        # so a declared alias here would resolve to the wrong value with no notice.
        raise LNDLError(f"<{tag} {raw}>: alias {alias!r} reads as a Python literal in OUT{{}}; pick another")
    return alias, spec, fld


class _Literal(ast.NodeTransformer):
    """The names that stand for a value in a literal, `true` or a pointer's or a heredoc's, replaced
    by the value, so `literal_eval` reads it as a constant."""

    def __init__(self, values: dict[str, Any]):
        self.values = values

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in self.values:
            return ast.copy_location(ast.Constant(self.values[node.id]), node)
        return node


def _marks(nodes: Iterable[ast.expr], pointers: list[str], docs: list[str] = ()) -> dict[str, Any]:
    """What the reader's own names in `nodes` stand for: each `__ptr_N` a Pointer, each `__doc_N` a
    heredoc's text. Each stands once; any other `__` name, or one standing twice, the model wrote,
    and it is refused rather than read as one of them."""
    values: dict[str, Any] = {f"{_PTR}{i}": Pointer(n) for i, n in enumerate(pointers)}
    values |= {f"{_DOC}{i}": d for i, d in enumerate(docs)}
    written = Counter(
        n.id for node in nodes for n in ast.walk(node) if isinstance(n, ast.Name) and n.id.startswith("__")
    )
    extra = next(iter(written - Counter(values.keys())), None)
    if extra is not None:
        raise LNDLError(f"{extra}: a name starting with __ is the reader's own, never a value")
    return values


_HEREDOC_AT = re.compile(r"=\s*<<\s*['\"]?([A-Za-z_]\w*)['\"]?[ \t]*\r?\n")
# the closing line: the tag alone, followed by `,` or `)`, by the next argument, or by nothing (`end`:
# the end of a call's text, or its closing tag in a whole turn); `PYTHONPATH=` is not the tag `PY`. The
# blanks after the tag stay in the call, so a next argument on the closing line stays its own name
_HEREDOC_END = r"^[ \t]*{tag}(?!\w)(?=\s*(?:[,)]|[A-Za-z_]\w*\s*=|{end}))"


def _lift_heredocs(source: str) -> tuple[str, list[str]]:
    """Each heredoc argument replaced by the reader's name for it, `__doc_N`; the texts, verbatim, beside.
    One walk over the call: a string literal is stepped over whole (a triple-quoted `x=<<EOF` is
    text, not an opening), a heredoc's body is taken to its closing line without being read, and
    one opened and never closed runs to the end of the call, its closers supplied downstream. A
    closing line right after the opening is an empty body."""
    docs: list[str] = []
    out: list[str] = []
    i, quote = 0, None
    while i < len(source):
        ch = source[i]
        if quote:
            if ch == "\\":
                out.append(source[i : i + 2])
                i += 2
                continue
            if source.startswith(quote, i):
                out.append(quote)
                i += len(quote)
                quote = None
                continue
            out.append(ch)
            i += 1
            continue
        if ch in "\"'":
            quote = ch * 3 if source.startswith(ch * 3, i) else ch
            out.append(quote)
            i += len(quote)
            continue
        m = _HEREDOC_AT.match(source, i) if ch == "=" else None
        if m is None:
            out.append(ch)
            i += 1
            continue
        end = re.compile(_HEREDOC_END.format(tag=m.group(1), end=r"\Z"), re.MULTILINE).search(source, m.end())
        if end is None:
            docs.append(source[m.end() :].replace("\r\n", "\n").rstrip())  # the lines, here too (C2)
            i = len(source)
        else:
            body = source[m.end() : end.start()]
            # "the lines verbatim" means the lines, not their transport line endings
            # (ADR-0004/C2); a CRLF body otherwise keeps a stray `\r` on every line.
            body = body.replace("\r\n", "\n")
            docs.append(body[:-1] if body.endswith("\n") else body)
            i = end.end()
        out.append(f"={_DOC}{len(docs) - 1}")
    return "".join(out), docs


def _line_offsets(source: str) -> list[int]:
    """Where each line starts, lines as `readline` splits them: at `\\n` only. `str.splitlines` also
    breaks at U+2028, U+2029, a lone `\\r` and the other separators, so a string argument holding one
    would shift every position the tokenizer reports on the lines after it."""
    offsets = [0]
    for line in source.split("\n")[:-1]:
        offsets.append(offsets[-1] + len(line) + 1)
    return offsets


def _mark_pointers(source: str) -> tuple[str, list[str]]:
    """Rewrite each `*name` that stands as a value into the reader's name for it, `__ptr_N`, the Nth
    of the names beside the text. Token level, so a `*` inside a string literal is left alone. A
    `*name` where a key or a set member stands is refused: a pointer stands where a value stands, and
    the resolver walks values."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError, ValueError):  # ValueError: 3.12+'s tokenizer, a lone surrogate
        return source, []
    offsets = _line_offsets(source)

    def at(pos: tuple[int, int]) -> int:
        return offsets[pos[0] - 1] + pos[1]

    spans: list[tuple[int, int, str]] = []
    names: list[str] = []
    opened: list[str] = []  # the brackets around the token: `,` inside `{` starts a key or a member
    for i, tok in enumerate(tokens):
        if tok.type == tokenize.OP and tok.string in ("(", "[", "{"):
            opened.append(tok.string)
        elif tok.type == tokenize.OP and tok.string in (")", "]", "}") and opened:
            opened.pop()
        if tok.type == tokenize.OP and tok.string == "*" and i + 1 < len(tokens):
            nxt = tokens[i + 1]
            if nxt.type != tokenize.NAME or at(nxt.start) != at(tok.end):
                continue
            k = i - 1
            while k >= 0 and tokens[k].type in (tokenize.NL, tokenize.COMMENT):  # a pointer starting a line
                k -= 1
            prev = tokens[k] if k >= 0 else None
            if prev is not None and not (prev.type == tokenize.OP and prev.string in "=,([{:"):
                continue
            end = i + 1
            while (
                end + 2 < len(tokens)
                and tokens[end + 1].string == "."
                and tokens[end + 2].type == tokenize.NAME
            ):
                end += 2
            name = "".join(t.string for t in tokens[i + 1 : end + 1])
            if prev is not None and (prev.string == "{" or (prev.string == "," and opened[-1:] == ["{"])):
                raise LNDLError(
                    f"*{name}: a pointer stands where a value stands, not as a key or a set member"
                )
            spans.append((at(tok.start), at(tokens[end].end), f"{_PTR}{len(names)}"))
            names.append(name)
    for start, end, repl in reversed(spans):
        source = source[:start] + repl + source[end:]
    return source, names


def _escape_newlines_in_strings(source: str) -> str:
    """A real line break inside a '...' or "..." literal becomes \\n. Models write code blocks that way;
    Python's grammar does not allow it, and the value they meant is the one with the newline. A
    triple-quoted literal allows them and is copied through untouched."""
    out, i, quote = [], 0, None
    while i < len(source):
        ch = source[i]
        if quote is None:
            if source.startswith(('"""', "'''"), i):
                end = _triple_end(source, i + 3, source[i : i + 3])
                out.append(source[i:end])
                i = end
                continue
            if ch in "'\"":
                quote = ch
            out.append(ch)
        elif ch == "\\" and i + 1 < len(source):
            out.append(source[i : i + 2])
            i += 1
        elif ch == quote:
            quote = None
            out.append(ch)
        elif ch == "\n":
            out.append("\\n")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _triple_end(source: str, i: int, quote: str) -> int:
    """The index just past the closing triple quote, or the end of the text when there is none."""
    while i < len(source):
        if source[i] == "\\":
            i += 2
            continue
        if source.startswith(quote, i):
            return i + 3
        i += 1
    return len(source)


def _close_dangling(source: str) -> str | None:
    """A string or bracket left open at the end: `edit(path="a", old="x", new="y` written with the
    closing tag standing in for `")`. The closers, in order; None when nothing is open."""
    quote: str | None = None
    stack: list[str] = []
    i = 0
    while i < len(source):
        ch = source[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if source.startswith(quote, i):
                i += len(quote)
                quote = None
                continue
        elif source.startswith(('"""', "'''"), i):
            quote = source[i : i + 3]
            i += 3
            continue
        elif ch in "'\"":
            quote = ch
        elif ch in "([{":
            stack.append(ch)
        elif ch in ")]}" and stack:
            stack.pop()
        i += 1
    if quote is None and not stack:
        return None
    closers = {"(": ")", "[": "]", "{": "}"}
    return source + (quote or "") + "".join(closers[c] for c in reversed(stack))


def _insert_commas(source: str) -> str | None:
    """`run(cmd="ls" timeout=1)`: the comma between two arguments left out. None when none is missing
    or the text does not tokenize. Only inside brackets: `a = f(x=1)` then `b = ...` is two statements."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError, ValueError):  # ValueError: 3.12+'s tokenizer, a lone surrogate
        return None
    offsets = _line_offsets(source)
    spots = []
    skip = (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT)
    depth = 0
    for i in range(len(tokens) - 1):
        tok, nxt = tokens[i], tokens[i + 1]
        k = i - 1
        while k > 0 and tokens[k].type in skip:  # the previous argument may end on an earlier line
            k -= 1
        prev = tokens[k]
        value_end = (
            prev.type in (tokenize.STRING, tokenize.NUMBER)
            or (prev.type == tokenize.OP and prev.string in ")]}")
            # a name ends a value too, `*a`, `true` or `None`, but `lambda`, `else` or `in` does not
            or (
                prev.type == tokenize.NAME
                and (prev.string in ("True", "False", "None") or not iskeyword(prev.string))
            )
        )
        keyword = tok.type == tokenize.NAME and nxt.type == tokenize.OP and nxt.string == "="
        if depth and keyword and value_end:
            spots.append(offsets[tok.start[0] - 1] + tok.start[1])
        if tok.type == tokenize.OP and tok.string in ("(", "[", "{"):
            depth += 1
        elif tok.type == tokenize.OP and tok.string in (")", "]", "}"):
            depth -= 1
    if not spots:
        return None
    for at in reversed(spots):
        source = source[:at] + ", " + source[at:]
    return source


_CLOSERS = "the call's closers were missing at its end and were supplied"
_COMMA = "a comma between arguments was missing and was supplied"
# a repair's name -> what the model is told of it; a line break escaped inside a string changes nothing
# the model would rewrite, so it is counted and not told (ADR-0004/C3)
_TOLD = {"closers": _CLOSERS, "comma": _COMMA}


def _readings(source: str) -> list[tuple[str, tuple[str, ...]]]:
    """The text as written, then each repair a model's slip calls for, with the repairs it applied."""
    out: list[tuple[str, tuple[str, ...]]] = [(source, ())]
    if "\n" in source:
        out.append((_escape_newlines_in_strings(source), ("newline",)))
    closed = _close_dangling(source)
    if closed is not None:
        out.append((closed, ("closers",)))
        if "\n" in closed:
            out.append((_escape_newlines_in_strings(closed), ("closers", "newline")))
    for text, names in list(out):
        with_commas = _insert_commas(text)
        if with_commas is not None:
            out.append((with_commas, (*names, "comma")))
    return out


def _parse_expr(text: str) -> ast.expr:
    with warnings.catch_warnings():
        # a `\s` in a regex argument is the model's: SyntaxWarning from 3.12, DeprecationWarning before
        warnings.simplefilter("ignore", SyntaxWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        return ast.parse(text, mode="eval").body


def parse_call(body: str) -> tuple[str, dict[str, Any]]:
    """`name(k=literal, x=*alias, y=<<TAG ... TAG)` -> (name, args). Arguments are Python literals,
    pointers, or heredocs (the text between the `<<TAG` line and the `TAG` line, verbatim)."""
    name, args, _ = parse_call_noting(body)
    return name, args


def parse_call_noting(body: str) -> tuple[str, dict[str, Any], tuple[str, ...]]:
    """`parse_call`, plus the repairs that read it, by name (`closers`, `newline`, `comma`); empty when
    it read as written."""
    # stripped at the start only: a string left open at the closing tag keeps the space it ends
    # with (`sep=", `); a heredoc left open is the one body that loses its trailing whitespace
    source, docs = _lift_heredocs(body.lstrip())
    node = first = None
    repairs: tuple[str, ...] = ()
    for text, names in _readings(source):
        try:
            marked, pointers = _mark_pointers(text)
            node = _parse_expr(marked)
        except (SyntaxError, ValueError) as e:  # ValueError: a lone surrogate or a NUL byte
            first = first or e
            continue
        except (RecursionError, MemoryError):  # the parser's own limits, the same for every reading
            raise LNDLError(f"cannot read the call {body.strip()[:160]!r}: {_DEEP}") from None
        repairs = names
        break
    if node is None:
        assert first is not None
        hint = ""
        msg = getattr(first, "msg", str(first))
        if "\n" in source or "unterminated" in msg or "EOL" in msg or "never closed" in msg:
            hint = (
                ". A multi-line value, or one with quotes in it, is written as a heredoc argument: "
                "cmd=<<EOF, then the lines verbatim, then EOF alone on a line, nothing escaped"
            )
        raise LNDLError(f"cannot read the call {body.strip()[:160]!r}: {msg}{hint}") from first
    try:
        return (*_call_of(node, body, docs, pointers), repairs)
    except LNDLError as e:  # read by a repair and refused after: the repair was applied, and is counted
        e.repairs = repairs
        raise
    except (RecursionError, MemoryError):  # a literal nested past what the reading walks
        raise LNDLError(f"cannot read the call {body.strip()[:160]!r}: {_DEEP}") from None


def _call_of(node: ast.expr, body: str, docs: list[str], pointers: list[str]) -> tuple[str, dict[str, Any]]:
    if not isinstance(node, ast.Call):
        raise LNDLError(f"{body.strip()!r} is not a call: write name(field=value, ...)")
    name = _dotted(node.func)
    if name is None or name.startswith((_PTR, _DOC)):  # `*f(x=1)`: a pointer is a value, not a callee
        raise LNDLError(f"{body.strip()!r}: the callee must be a name")
    if node.args:
        raise LNDLError(f"{name}(...): arguments are keyword only, write field=value")
    values = _WORDS | _marks([kw.value for kw in node.keywords], pointers, docs)
    args: dict[str, Any] = {}
    for kw in node.keywords:
        if kw.arg is None:
            raise LNDLError(f"{name}(...): ** unpacking is not allowed")
        try:
            args[kw.arg] = ast.literal_eval(_Literal(values).visit(kw.value))
        except (ValueError, TypeError, SyntaxError):  # `{[]: 1}` raises TypeError; the model retries
            raise LNDLError(
                f"{name}(...): {kw.arg}= must be a literal (string, number, list, dict) or a *pointer"
            ) from None
    return name, args


def _dotted(node: ast.AST) -> str | None:
    """`a.b.c` as the name it spells; None for anything else. A loop, so a long chain costs no depth."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id, *reversed(parts)])


def _outside(text: str):
    """Every (index, character) of `text` that is not inside a string literal, quotes included: what a
    scanner may read as syntax. A string's commas, braces and colons are its own; a triple-quoted
    string is one literal, the quotes inside it included."""
    quote, i = None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif text.startswith(quote, i):
                i += len(quote) - 1
                quote = None
        elif ch in "\"'":
            quote = ch * 3 if text.startswith(ch * 3, i) else ch
            i += len(quote) - 1
        else:
            yield i, ch
        i += 1


def _split_top(text: str, sep: str = ",") -> list[str]:
    parts, depth, start = [], 0, 0
    for i, ch in _outside(text):
        if ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    if text[start:].strip():
        parts.append(text[start:])
    return parts


_BARE_KEY = re.compile(r"([{,]\s*)([A-Za-z_]\w*)(\s*:)")


def _quote_bare_keys(value: str) -> str:
    """`{summary: "x"}` is how models write a dict; Python wants the key quoted. Only outside string
    literals: `"first, foo: bar"` is a value and stays as written."""
    out, start, prev = [], 0, -1
    for i, _ch in _outside(value):
        if i != prev + 1:  # a string literal sits between prev and i: keep it verbatim
            out.append(
                _BARE_KEY.sub(lambda m: f'{m.group(1)}"{m.group(2)}"{m.group(3)}', value[start : prev + 1])
            )
            out.append(value[prev + 1 : i])
            start = i
        prev = i
    out.append(_BARE_KEY.sub(lambda m: f'{m.group(1)}"{m.group(2)}"{m.group(3)}', value[start : prev + 1]))
    out.append(value[prev + 1 :])
    return "".join(out)


def stray_tags(text: str, names: set[str]) -> list[str]:
    """`<edit>...</edit>` written where `<lact a1>edit(...)</lact>` was meant: tag names that are commands."""
    found = []
    for m in re.finditer(r"<([A-Za-z_][\w.]*)\s*>", text):
        if m.group(1) in names and m.group(1) not in ("lvar", "lact"):
            found.append(m.group(1))
    return sorted(set(found))


def parse_out(body: str) -> Out:
    fields: dict[str, Any] = {}
    for part in _split_top(body):
        if ":" not in part:
            raise LNDLError(f"OUT{{...}}: {part.strip()!r} needs the form key: value")
        key, value = part.split(":", 1)
        key, value = key.strip(), value.strip()
        if not _IDENT.match(key):
            raise LNDLError(f"OUT{{...}}: {key!r} is not a spec name")
        if value.startswith("[") and value.endswith("]"):
            items = [a.strip() for a in _split_top(value[1:-1]) if a.strip()]
            if items and all(_IDENT.match(a) and a not in ("True", "False", "None") for a in items):
                fields[key] = Aliases(items)
                continue
        elif _IDENT.match(value) and value not in ("True", "False", "None"):
            fields[key] = Aliases([value])
            continue
        try:  # a literal, in which `*alias` stands for the value it names, as in a call's arguments
            marked, pointers = _mark_pointers(value)
            node = _parse_expr(_quote_bare_keys(marked))
            fields[key] = ast.literal_eval(_Literal(_marks([node], pointers)).visit(node))
        except LNDLError:
            raise  # a pointer where a key or a set member stands: the message names the position
        except (RecursionError, MemoryError):
            raise LNDLError(f"OUT{{{key}: ...}}: {_DEEP}") from None
        except (ValueError, TypeError, SyntaxError):
            raise LNDLError(
                f"OUT{{{key}: ...}}: {value!r} is neither aliases like [a, b] nor a literal like 3, "
                f'["x", "y"] or {{field: *alias}}'
            ) from None
    return Out(fields=fields)


_OPEN = re.compile(r"<(lvar|lact)\s+[^>]+?\s*>|\bOUT\s*\{")


_CODE_SPAN = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
_STOPS = re.compile(r"[\"'=`<]")  # what `_closer` reads; a heredoc argument starts at its `=`


def _closer(
    text: str, pos: int, kind: str, call: bool = False, skip: Iterable[tuple[int, int]] = ()
) -> tuple[re.Match | None, list[tuple[int, int]]]:
    """The closer of the `kind` opener just before `pos`, and the heredoc arguments met on the way,
    opening line to closing line, from one walk left to right. Same-kind tags nest, so a value
    holding a complete tag keeps it whole; when the tags do not balance (a bare opener quoted inside
    the value) the first closer ends it, as it always did; the other kind is text. Markdown code
    keeps the tags in it quoted (`<lvar q>` or `</lvar>` in backticks inside a note), except code
    that runs from one construct into the next, a closer then an opener of the kind: its backticks
    are two constructs' text, and its closer ends this one.

    A `call` is walked as one: a string literal is stepped over whole, so a tag in it is text, and a
    heredoc argument's body is verbatim, so a tag, a quote or a fence in it (a span copied out of a
    file that shows one) is text: the first legal closing line ends it, whatever comes before, and
    one with no closing line at all runs to the call's first closing tag, which ends the call. When a
    string runs to the end of the text, one was left open at a closing tag, which stands in for the
    call's closers: the call ends at the first closing tag outside its heredocs, strings or not
    (ADR-0004/C1, C2, C3). The `skip` spans are stepped over unread. None when there is no closer."""
    skip = dict(skip)
    depth, first, docs, i = 1, None, [], pos
    strings: list[tuple[int, int]] = []  # the string literals read so far
    held: list[tuple[int, int]] = []  # those before a heredoc: finding it outside them vouches for them
    while (stop := _STOPS.search(text, i)) is not None:
        i = stop.start()
        ch = text[i]
        if i in skip:
            i = skip[i]
        elif call and ch in "\"'":
            quote = ch * 3 if text.startswith(ch * 3, i) else ch
            j = i + len(quote)
            while j < len(text) and not text.startswith(quote, j):
                j += 2 if text[j] == "\\" else 1
            if j >= len(text):  # never closed: it runs to the end, so nothing after it is read
                break
            strings.append((i, j + len(quote)))
            i = j + len(quote)
        elif call and ch == "=" and (m := _HEREDOC_AT.match(text, i)):
            held += strings
            strings = []
            end = re.compile(_HEREDOC_END.format(tag=m.group(1), end=rf"</{kind}\s*>"), re.MULTILINE)
            closing = end.search(text, m.end())
            if closing is None:
                t = next(
                    (t for t in _ANY_TAG.finditer(text, m.end()) if t.group(1) and t.group(2) == kind), None
                )
                if t is not None:
                    docs.append((m.start(), t.start()))
                return t or first, docs
            docs.append((m.start(), closing.end()))
            i = closing.end()
        elif (ch == "`" and (code := _CODE_SPAN.match(text, i))) and "<" not in "".join(
            "/" if t.group(1) else "<" for t in _ANY_TAG.finditer(code.group()) if t.group(2) == kind
        ).partition("/")[2]:
            i = code.end()
        elif ch == "<" and (t := _ANY_TAG.match(text, i)):
            if t.group(2) != kind:  # the other kind is text, the whole tag
                pass
            elif t.group(1):
                first = first or t
                depth -= 1
                if depth == 0:
                    return t, docs
            else:
                depth += 1
            i = t.end()
        else:
            i += 1
    if call and first is None:  # every closing tag is in a string: one was left open at the first
        first = _closer(text, pos, kind, skip=docs + held)[0]
    return first, docs


def _constructs(text: str):
    """Every tag and every `OUT{...}` in the text, in order, from one walk: whichever opens first owns
    its span, so a tag's body is never read for OUT{} and an OUT literal's strings are never read
    for tags (`OUT{answer: "<lact a>x()</lact>"}` is a string, and so is `"OUT{x: 1}"` inside one).
    An OUT{} is taken with its braces balanced, so a dict literal inside is not the end of it. An
    opener that is not a tag (a bad alias, no closer) is yielded as a "problem" with its text, and
    the scan goes on right after it, so tag-shaped prose costs a notice, never the turn. Markdown
    code opens only in the prose between constructs, and an opener inside it is quoted text that
    costs nothing; backticks inside a construct are that construct's (`_closer`), so they never pair
    with backticks in another. A heredoc is read only in a call's arguments, as the call is read."""
    i, m = 0, None
    while True:
        if m is None or m.start() < i:
            m = _OPEN.search(text, i)
        if m is None:
            return
        tick = text.find("`", i, m.start())
        if tick != -1:  # quoted: `<lvar x>` or `OUT{}` in a sentence about them
            code = _CODE_SPAN.match(text, tick)
            i = code.end() if code else tick + 1
            continue
        if m.group(1):
            opener = _OPEN_TAG.match(text, m.start())
            kind, header = opener.group(1), opener.group(2)
            try:
                _header(header, kind)
            except LNDLError as e:  # tag-shaped prose (`<lact …>` in a sentence): not a tag
                yield "problem", f"{e}; it was read as prose", (m.start(), m.end())
                i = m.end()
                continue
            closer, docs = _closer(text, opener.end(), kind, call=kind == "lact")
            if kind == "lact" and closer is not None:
                body = text[opener.end() : closer.start()]
                readable = bool(_CALLISH.match(body.strip()))
                if readable:
                    try:
                        parse_call_noting(body)
                    except LNDLError as e:
                        readable = not isinstance(e.__cause__, SyntaxError)
                if not readable:
                    # strings are opaque to the closing tag only to keep one inside a call's string:
                    # a body that is not a call, or a call that then cannot be read (a quote left open
                    # at a closing tag, paired with a later one), is bounded again as before, its
                    # heredocs still verbatim (ADR-0004/C3)
                    closer = _closer(text, opener.end(), kind, skip=docs)[0] or closer
            if closer is None:  # an opener without any closer: prose
                yield (
                    "problem",
                    f"<{kind} {header}> has no closing tag, so it was read as prose",
                    (m.start(), m.end()),
                )
                i = m.end()
                continue
            yield "tag", (kind, header, text[opener.end() : closer.start()]), (m.start(), closer.end())
            i = closer.end()
            continue
        depth, end = 1, None
        for j, ch in _outside(text[m.end() :]):
            depth += {"{": 1, "}": -1}.get(ch, 0)
            if depth == 0:
                end = m.end() + j
                break
        if end is None:  # never closed: prose
            i = m.end()
            continue
        yield "out", text[m.end() : end], (m.start(), end + 1)
        i = end + 1


def spans(text: str) -> list[tuple[int, int]]:
    """Where the constructs stand in `text`, in order: what a reader cuts out to show the
    prose between them, by the same scan the parser uses, so what it shows as prose is what ran as
    prose."""
    return [span for what, _, span in _constructs(text) if what != "problem"]


def plain(text: str) -> str:
    """The reply without its LNDL: what the model said beside its commands, its lines kept. The
    constructs are cut where `spans` finds them, so the prose is the prose the parser left."""
    prose, at = [], 0
    for start, end in spans(text):
        prose.append(text[at:start])
        at = end
    prose.append(text[at:])
    lines = (" ".join(ln.split()) for ln in " ".join(prose).splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse(text: str) -> Program:
    """Every construct in the text, in order. Raises `LNDLError` on the first one that cannot be read;
    the error carries the repairs the readings before it applied, so they are counted (ADR-0008/D2)."""
    repairs: list[str] = []
    try:
        return _parse_constructs(text, repairs)
    except LNDLError as e:  # the readings before it, and the one that read the refused call
        e.repairs = (*repairs, *getattr(e, "repairs", ()))
        raise


def _parse_constructs(text: str, repairs: list[str]) -> Program:
    lvars: list[Lvar] = []
    lacts: list[Lact] = []
    problems: list[str] = []
    seen: set[str] = set()

    outs: list[str] = []
    for what, m, _ in _constructs(text):
        if what == "problem":
            problems.append(m)
            continue
        if what == "out":
            if m.strip():
                outs.append(m)
            continue
        tag, header, body = m
        alias, spec, fld = _header(header, tag)
        if alias in seen:  # the first one stands; the rest of the turn is not lost over it
            problems.append(f"alias {alias!r} is used twice in one turn; the second <{tag}> was dropped")
            continue
        seen.add(alias)
        if tag == "lvar":
            lvars.append(Lvar(alias=alias, value=body.strip(), spec=spec, field=fld))
        elif not _CALLISH.match(body.strip()):
            # a shell line, a script, a note's prose, written in <lact>: it is a value, not a request,
            # and it is kept under the alias rather than lost to a wasted round (on Mini-50, 7 unreadable
            # turns were bare shell in a <lact>, ADR-0004/A1, and the model's next turn pointed at the
            # alias it had used)
            lvars.append(Lvar(alias=alias, value=body.strip(), spec=spec, field=fld))
            problems.append(
                f"<lact {alias}> held a value, not a call, so it was kept as <lvar {alias}>; to act on "
                f"it, point at it from a command: name(field=*{alias})"
            )
        else:
            name, args, repaired = parse_call_noting(body)
            repairs += repaired
            told = "; ".join(_TOLD[r] for r in repaired if r in _TOLD)
            if told:
                problems.append(f"<lact {alias}>: {told}; if that is not the call you meant, write it again")
            lacts.append(Lact(alias=alias, name=name, args=args, spec=spec, field=fld))

    if len(outs) > 1:  # a second finish refuses the message whole, as the loop's own frame does (ADR-0002/C1)
        err = LNDLError(
            f"one OUT{{...}} per turn and this turn carries {len(outs)}, so nothing in it was executed: "
            "write it again with one OUT{...}"
        )
        # a log written before this rule ran the turn's values and calls: a replay of that log admits
        # them from here (ADR-0018/C6); a live turn is refused whole
        err.program = Program(lvars, lacts, None, problems)
        raise err
    out = None
    if outs:
        # an unreadable finish does not lose the turn's commands: they run, and the model is told
        # why it has not finished, with its results in view for the next attempt
        try:
            out = parse_out(outs[0])
        except LNDLError as e:
            problems.append(f"OUT{{...}} could not be read, so this turn ran without it: {e}")
    return Program(lvars=lvars, lacts=lacts, out=out, problems=problems, repairs=repairs)


def guidance(outputs: tuple[str, ...] = (), commands: Iterable[str] | None = None) -> str:
    """The LNDL section of the system prompt; `outputs` names what OUT{} may fill, for the example.
    `commands` is the profile's subset: the note and context commands are described only when it has
    them (a profile is told nothing about a command it cannot run, ADR-0001/C5); None means all."""
    o = outputs[0] if outputs else "name"
    has = (lambda name: True) if commands is None else set(commands).__contains__
    finding = (
        'note.list() and note.find(query="...") find them, '
        + ('note.get(key="...") reads one whole, ' if has("note.get") else "")
        + ('note.delete(key="...") removes one, ' if has("note.delete") else "")
        + "and "
        if has("note.list") and has("note.find")
        else ""
    )
    curating = (
        ' Hide in one batch when a phase ends (context.hide(refs=["_r41", "_r42"])), replace a finished '
        "span with a few lines (context.summarize), and bring an entry back when you need it again "
        "(context.show)."
        if has("context.hide") and has("context.summarize") and has("context.show")
        else ""
    )
    return (
        # The opening claims nothing false: the parser reads a tag wherever it stands.
        "You act by writing LNDL in your reply. Three constructs, where `a1` stands for "
        "an alias you pick (a bare word, written right after the tag name):\n"
        "  <lvar a1>value</lvar>                       name a value so you can point at it later\n"
        "  <lact a1>name(field=value, ...)</lact>      ask for something to happen; `name` is one of the "
        "commands listed below; arguments are literals or pointers like *a1\n"
        f"  OUT{{{o}: [a1, ...]}}" + " " * max(1, 28 - len(o)) + "finish: the aliases fill the named output "
        f"(this run's outputs: {', '.join(outputs) if outputs else 'listed below'}); a single-field output "
        f"also takes a literal, OUT{{{o}: 3}}, and any output takes a dict literal of its fields, "
        f"OUT{{{o}: {{field: value, ...}}}}\n"
        # the concrete example names commands only when the profile has them (ADR-0001/C5: a command
        # outside the subset is never presented); otherwise the shape is shown with placeholder names
        + (
            'Example: <lact f1>read_file(path="src/x.py")</lact> then, next round, '
            '<lact g1>grep(pattern="def ", text=*f1)</lact>. '
            if has("read_file") and has("grep")
            else 'Example: <lact f1>first(field="value")</lact> then, next round, '
            "<lact g1>second(text=*f1)</lact>, `first` and `second` standing for commands listed below. "
        )
        + "A multi-line value, or one with quotes in it "
        "(a script, a code block, a shell line), is written as a heredoc argument, nothing escaped:\n"
        f"  <lact r1>{'run(cmd' if has('run') else 'name(field'}=<<EOF\n"
        "  ...the lines, verbatim...\n  EOF)</lact>\n"
        "or goes in its own <lvar s1>...</lvar> and is passed as a pointer, "
        f"{'run(cmd' if has('run') else 'name(field'}=*s1).\n"
        "A pointer *alias refers to anything you named, to any entry by its record name (_r41, _m12; "
        "names starting with _ are the record's), "
        "or to a note (note.key). Values are never repeated for you; point at them.\n"
        # Mini-50 wrote 6 notes in 952 turns with only the pointer sentence above to go on; the model
        # was never told what a note is for, how to write one, or that it outlives the run
        "Notes outlive this run: <lvar note.key>text</lvar> writes one under `key`, *note.key reads it, "
        f"{finding}the next run of this profile starts with "
        "them in reach. Write a note when you have established something a later round or a later run "
        "should not have to rediscover: a fact about the material, the plan, what is still unverified. "
        "A few lines each; write the same key again as it changes.\n"
        "Your view is the record: everything stays in it where it landed, addressable by name, until "
        "you hide it or the view outgrows its budget, when the runtime folds the oldest results to "
        f"placeholders once.{curating}\n"
        "A turn without OUT{} runs every <lact> and continues; their results arrive in the next system "
        "notification under their aliases. A turn with OUT{} runs every <lact> too, but OUT{} waits only "
        "for what it names or points at: once it is accepted the run ends and any <lact> not finished is "
        "cancelled.\n"
        "Aliases are unique within a turn. Tags are siblings; a complete tag quoted inside a value stays "
        "text."
    )
