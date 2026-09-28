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
from collections.abc import Iterable
from dataclasses import dataclass, field
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
_PTR_MARK = "\x00ptr:"
_DOC_MARK = "\x00doc:"
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


def _restore_pointers(value: Any, docs: list[str] = ()) -> Any:
    """The placeholders `_mark_pointers` and `_lift_heredocs` left in the literal, turned back."""
    if isinstance(value, str) and value.startswith(_PTR_MARK):
        return Pointer(value[len(_PTR_MARK) :])
    if isinstance(value, str) and value.startswith(_DOC_MARK):
        try:
            return docs[int(value[len(_DOC_MARK) :])]
        except (IndexError, ValueError):  # not a marker of ours: a string that happens to look like one
            return value
    if isinstance(value, list):
        return [_restore_pointers(v, docs) for v in value]
    if isinstance(value, tuple):
        return tuple(_restore_pointers(v, docs) for v in value)
    if isinstance(value, dict):
        return {k: _restore_pointers(v, docs) for k, v in value.items()}
    return value


_HEREDOC_AT = re.compile(r"=\s*<<\s*['\"]?([A-Za-z_]\w*)['\"]?[ \t]*\r?\n")
# the closing line: the tag alone, followed by `,` or `)`, by the next argument, or by nothing (`end`:
# the end of a call's text, or its closing tag in a whole turn); `PYTHONPATH=` is not the tag `PY`
_HEREDOC_END = r"^[ \t]*{tag}(?!\w)[ \t]*(?=\s*(?:[,)]|[A-Za-z_]\w*\s*=|{end}))"


def _lift_heredocs(source: str) -> tuple[str, list[str]]:
    """Each heredoc argument replaced by a placeholder string literal; the texts, verbatim, beside it.
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
            docs.append(source[m.end() :].replace("\r\n", "\n"))  # the lines, here too (C2)
            i = len(source)
        else:
            body = source[m.end() : end.start()]
            # "the lines verbatim" means the lines, not their transport line endings
            # (ADR-0004/C2); a CRLF body otherwise keeps a stray `\r` on every line.
            body = body.replace("\r\n", "\n")
            docs.append(body[:-1] if body.endswith("\n") else body)
            i = end.end()
        out.append("=" + repr(f"{_DOC_MARK}{len(docs) - 1}"))
    return "".join(out), docs


def _line_offsets(source: str) -> list[int]:
    """Where each line starts, lines as `readline` splits them: at `\\n` only. `str.splitlines` also
    breaks at U+2028, U+2029, a lone `\\r` and the other separators, so a string argument holding one
    would shift every position the tokenizer reports on the lines after it."""
    offsets = [0]
    for line in source.split("\n")[:-1]:
        offsets.append(offsets[-1] + len(line) + 1)
    return offsets


def _mark_pointers(source: str) -> str:
    """Rewrite each `*name` that stands as a value into a string literal the walker turns back into a
    Pointer. Token level, so a `*` inside a string literal is left alone. A `*name` where a key or a
    set member stands is refused: a pointer stands where a value stands, and the resolver walks values."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError, ValueError):  # ValueError: 3.12+'s tokenizer, a lone surrogate
        return source
    offsets = _line_offsets(source)

    def at(pos: tuple[int, int]) -> int:
        return offsets[pos[0] - 1] + pos[1]

    spans: list[tuple[int, int, str]] = []
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
            prev = tokens[i - 1] if i else None
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
            spans.append((at(tok.start), at(tokens[end].end), repr(_PTR_MARK + name)))
    for start, end, repl in reversed(spans):
        source = source[:start] + repl + source[end:]
    return source


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
        value_end = prev.type in (tokenize.STRING, tokenize.NUMBER) or (
            prev.type == tokenize.OP and prev.string in ")]}"
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
        warnings.simplefilter("ignore", SyntaxWarning)  # a `\s` in a regex argument is the model's
        return ast.parse(text, mode="eval").body


def parse_call(body: str) -> tuple[str, dict[str, Any]]:
    """`name(k=literal, x=*alias, y=<<TAG ... TAG)` -> (name, args). Arguments are Python literals,
    pointers, or heredocs (the text between the `<<TAG` line and the `TAG` line, verbatim)."""
    name, args, _ = parse_call_noting(body)
    return name, args


def parse_call_noting(body: str) -> tuple[str, dict[str, Any], tuple[str, ...]]:
    """`parse_call`, plus the repairs that read it, by name (`closers`, `newline`, `comma`); empty when
    it read as written."""
    source, docs = _lift_heredocs(body.strip())
    node = first = None
    repairs: tuple[str, ...] = ()
    for text, names in _readings(source):
        try:
            node = _parse_expr(_mark_pointers(text))
        except (SyntaxError, ValueError) as e:  # ValueError: a lone surrogate or a NUL byte
            first = first or e
            continue
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
    if not isinstance(node, ast.Call):
        raise LNDLError(f"{body.strip()!r} is not a call: write name(field=value, ...)")
    name = _dotted(node.func)
    if name is None:
        raise LNDLError(f"{body.strip()!r}: the callee must be a name")
    if node.args:
        raise LNDLError(f"{name}(...): arguments are keyword only, write field=value")
    args: dict[str, Any] = {}
    for kw in node.keywords:
        if kw.arg is None:
            raise LNDLError(f"{name}(...): ** unpacking is not allowed")
        try:
            args[kw.arg] = _restore_pointers(ast.literal_eval(_JsonWords().visit(kw.value)), docs)
        except (ValueError, TypeError, SyntaxError):  # `{[]: 1}` raises TypeError; the model retries
            raise LNDLError(
                f"{name}(...): {kw.arg}= must be a literal (string, number, list, dict) or a *pointer"
            ) from None
    return name, args, repairs


class _JsonWords(ast.NodeTransformer):
    """`true`, `false`, `null` written where a Python literal was expected: models mix the two."""

    WORDS = {"true": True, "false": False, "null": None}

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in self.WORDS:
            return ast.copy_location(ast.Constant(self.WORDS[node.id]), node)
        return node


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


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
            fields[key] = _restore_pointers(ast.literal_eval(_quote_bare_keys(_mark_pointers(value))))
        except LNDLError:
            raise  # a pointer where a key or a set member stands: the message names the position
        except (ValueError, TypeError, SyntaxError):
            raise LNDLError(
                f"OUT{{{key}: ...}}: {value!r} is neither aliases like [a, b] nor a literal like 3, "
                f'["x", "y"] or {{field: *alias}}'
            ) from None
    return Out(fields=fields)


_OPEN = re.compile(r"<(lvar|lact)\s+[^>]+?\s*>|\bOUT\s*\{")


_CODE_SPAN = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)


def _code_spans(text: str, opaque: Iterable[tuple[int, int]] = ()) -> list[tuple[int, int]]:
    """Markdown code, fenced or inline: where a model quotes a tag it is talking about. The
    `opaque` spans (heredoc arguments) are blanked first, offsets and line breaks kept, so a fence
    inside one never pairs with a fence outside it."""
    for a, b in opaque:
        text = text[:a] + re.sub(r"[^\n]", " ", text[a:b]) + text[b:]
    return [m.span() for m in _CODE_SPAN.finditer(text)]


def _in(spans: list[tuple[int, int]], pos: int) -> bool:
    return any(a <= pos < b for a, b in spans)


def _lact_closer(
    text: str, pos: int, docs: list[tuple[int, int]]
) -> tuple[re.Match | None, list[tuple[int, int]]]:
    """The closer of the `<lact>` opener just before `pos`, and the call's heredoc arguments, opening
    line to closing line, found in order. An opening counts only in the call's own arguments,
    outside its string literals, and its body is verbatim, so a tag or a fence in it (a span copied
    out of a file that shows one) is neither an opener nor a closer: the first legal closing line
    ends it, whatever comes before. Only one with no closing line at all runs to the call's first
    closing tag, which ends the call. A closing tag inside one of the call's string literals is text
    too; when every closing tag after the call is, a string was left open at the first one, which
    stands in for the call's closers (ADR-0004/C1, ADR-0004/C3)."""
    found: list[tuple[int, int]] = []
    quoted: list[tuple[int, int]] = []  # the string literals of the arguments read so far
    at = pos
    while True:
        spans = docs + found
        opaque = _code_spans(text, spans) + spans + quoted
        strings, prev = [], -1  # the string literals from `at` on, a quote left open running to the end
        for j, _ch in _outside(text[at:]):
            if j != prev + 1:
                strings.append((at + prev + 1, at + j))
            prev = j
        strings.append((at + prev + 1, len(text)))
        closer = _closer(text, pos, "lact", opaque + strings) or _closer(text, pos, "lact", opaque)
        stop = closer.start() if closer else len(text)
        m = next(
            (h for j, ch in _outside(text[at:stop]) if ch == "=" and (h := _HEREDOC_AT.match(text, at + j))),
            None,
        )
        if m is None:
            return closer, found
        quoted += [s for s in strings if s[1] <= m.start()]
        end = re.compile(_HEREDOC_END.format(tag=m.group(1), end=r"</lact\s*>"), re.MULTILINE).search(
            text, m.end()
        )
        if end is None:
            first = next(
                (t for t in _ANY_TAG.finditer(text, m.end()) if t.group(1) and t.group(2) == "lact"), None
            )
            if first is not None:
                found.append((m.start(), first.start()))
            return first, found
        found.append((m.start(), end.end()))
        at = end.end()


def _closer(text: str, pos: int, kind: str, spans: list[tuple[int, int]]) -> re.Match | None:
    """The closer of the `kind` opener just before `pos`. Same-kind tags nest, so a value holding a
    complete tag keeps it whole; when the tags do not balance (a bare opener quoted inside the value)
    the first closer ends it, as it always did; the other kind, and anything in a code span, is
    text. None when there is no closer at all."""
    depth, first = 1, None
    for t in _ANY_TAG.finditer(text, pos):
        if t.group(2) != kind or _in(spans, t.start()):
            continue
        if t.group(1):
            first = first or t
            depth -= 1
            if depth == 0:
                return t
        else:
            depth += 1
    return first


def _constructs(text: str):
    """Every tag and every `OUT{...}` in the text, in order, from one pass: whichever opens first owns
    its span, so a tag's body is never read for OUT{} and an OUT literal's strings are never read
    for tags (`OUT{answer: "<lact a>x()</lact>"}` is a string, and so is `"OUT{x: 1}"` inside one).
    An OUT{} is taken with its braces balanced, so a dict literal inside is not the end of it. An
    opener that is not a tag (a bad alias, no closer) is yielded as a "problem" with its text, and
    the scan goes on right after it, so tag-shaped prose costs a notice, never the turn; an opener
    inside markdown code is quoted text and costs nothing. A heredoc is read only in a call's
    arguments, as the call is read, and its body is opaque to everything after it."""
    docs: list[tuple[int, int]] = []  # the heredoc arguments of the calls read so far
    code = _code_spans(text)
    i = 0
    while (m := _OPEN.search(text, i)) is not None:
        if _in(code + docs, m.start()):  # quoted: `<lvar x>` or `OUT{}` in a sentence about them
            i = m.end()
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
            if kind == "lact":
                closer, found = _lact_closer(text, opener.end(), docs)
                if closer is not None:
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
                        # at a closing tag, paired with a later one), is bounded again as before (ADR-0004/C3)
                        opaque = docs + found
                        blind = _closer(text, opener.end(), "lact", _code_spans(text, opaque) + opaque)
                        closer = blind or closer
                        found = [s for s in found if s[1] <= closer.start()]
                if found:
                    docs += found
                    code = _code_spans(text, docs)
            else:
                closer = _closer(text, opener.end(), kind, code + docs)
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


_TAGS = re.compile(r"<(lact|lvar)\b[^>]*>.*?</\1>|OUT\{.*?\}", re.S)


def plain(text: str) -> str:
    """The reply without its LNDL: what the model said beside its commands, its lines kept."""
    lines = (" ".join(ln.split()) for ln in _TAGS.sub(" ", text).splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse(text: str) -> Program:
    """Every construct in the text, in order. Raises `LNDLError` on the first one that cannot be read."""
    lvars: list[Lvar] = []
    lacts: list[Lact] = []
    problems: list[str] = []
    repairs: list[str] = []
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

    out = None
    if outs:
        # an unreadable finish does not lose the turn's commands: they run, and the model is told
        # why it has not finished, with its results in view for the next attempt
        try:
            if len(outs) > 1:
                raise LNDLError("one OUT{...} per turn")
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
        f"  OUT{{{o}: [a1, ...]}}" + " " * max(1, 27 - len(o)) + "finish: the aliases fill the named output "
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
        "notification under their aliases. A turn with OUT{} runs every <lact> too, waits for them and for "
        "anything still running that OUT{} points at, then ends.\n"
        "Aliases are unique within a turn. Tags are siblings; a complete tag quoted inside a value stays "
        "text."
    )
