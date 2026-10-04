"""A provider's own markup around LNDL, rewritten to the tags the parser reads (ADR-0004/D2: the
parser knows no provider; the backend adapts a provider's output). Two shapes come from models
trained on function calling: their native tool-call markup wrapped around a tag in the text, and a
turn the provider parsed out of the content into `tool_calls`."""

from __future__ import annotations

import ast
import hashlib
import json
import re

from lion1.lndl import _constructs

__all__ = ("normalize", "tool_calls_text")


# DeepSeek wraps tag-shaped text in its native tool-call markup: `<lact a1>` arrives as
# `<｜DSML｜ invoke name="lact a1>` under a `<｜DSML｜ calls>` line. The intent is legible, so it is read.
_DSML_TAG = re.compile(r"""<｜DSML｜\s*invoke\s+name\s*=\s*["']?(lvar|lact)\s+([^"'>]+?)["']?\s*>""")
_DSML_NOISE = re.compile(r"</?｜DSML｜[^>]*>")


_DSML_INVOKE = re.compile(
    r"""<｜DSML｜\s*invoke\s+name\s*=\s*["']([A-Za-z_][\w.]*)["']\s*>(.*?)</｜DSML｜\s*invoke\s*>""",
    re.DOTALL,
)
_DSML_PARAM = re.compile(
    r"""<｜DSML｜\s*parameter\s+name\s*=\s*["']([^"']+)["'](?:\s+string\s*=\s*["'](true|false)["'])?\s*>"""
    r"""(.*?)</｜DSML｜\s*parameter\s*>""",
    re.DOTALL,
)


def _alias(key: str, seen: dict[str, int]) -> str:
    """`auto_` and a digest of `key` and its occurrence: two identical calls in one turn are two calls,
    and the parser keeps only the first of two equal aliases."""
    n = seen.get(key, 0)
    seen[key] = n + 1
    return "auto_" + hashlib.blake2b((key if n == 0 else f"{key}\0{n}").encode(), digest_size=3).hexdigest()


def _dsml_call(m: re.Match, seen: dict[str, int]) -> str:
    """A whole native tool call, `invoke name="run"` with `parameter` children, as one <lact>."""
    name, body = m.group(1), m.group(2)
    args = []
    for pm in _DSML_PARAM.finditer(body):
        key, kind, raw = pm.group(1), pm.group(2), pm.group(3)
        if kind != "false":
            args.append(f"{key}={raw!r}")
            continue
        try:
            ast.literal_eval(raw.strip())
        except (ValueError, SyntaxError):
            args.append(f"{key}={raw!r}")
        else:
            args.append(f"{key}={raw.strip()}")
    return f"<lact {_alias(m.group(0), seen)}>{name}({', '.join(args)})</lact>"


def normalize(text: str) -> str:
    """Model-specific markup that carries an LNDL tag inside it, rewritten to the tag itself.

    DeepSeek's native tool-call syntax comes in two shapes: our tag wrapped in an invoke header
    (`<｜DSML｜ invoke name="lact a1>...`), which is unwrapped, and a whole native call
    (`invoke name="run"` with `parameter` children), which becomes a <lact> under a generated alias.
    """
    if "｜DSML｜" not in text:
        return text
    # a construct already well formed in the raw text is a literal's home (an OUT string, a call's
    # argument, a value tag's body) and is handed on untouched; the markup is rewritten only in the
    # text between such constructs, which is where a wrapped tag stands. A construct that stands
    # inside a whole native call is that call's argument text, not a construct of its own: the call
    # owns it and converts it as data.
    calls = [m.span() for m in _DSML_INVOKE.finditer(text)]
    seen: dict[str, int] = {}
    out, i = [], 0
    for what, _, (start, end) in _constructs(text):
        if what == "problem" or start < i or any(s <= start and end <= e for s, e in calls):
            continue
        out.append(_normalize_gap(text[i:start], seen))
        out.append(text[start:end])
        i = end
    out.append(_normalize_gap(text[i:], seen))
    return "".join(out)


def _normalize_gap(text: str, seen: dict[str, int]) -> str:
    if "｜DSML｜" not in text:
        return text
    text = _DSML_TAG.sub(r"<\1 \2>", text)
    # a whole call converts to a finished <lact> whose argument may hold tag-shaped text: it is set
    # aside while the unclosed-tag repair runs, which would otherwise read that text as a new tag
    whole: list[str] = []

    def aside(m: re.Match) -> str:
        whole.append(_dsml_call(m, seen))
        return f"\0{len(whole) - 1}\0"

    text = _DSML_INVOKE.sub(aside, text)
    text = _DSML_NOISE.sub("", _UNCLOSED.sub(_close_tag, text))
    return re.sub(r"\0(\d+)\0", lambda m: whole[int(m.group(1))], text)


# a tag opened in native markup whose `</lact>` the provider swallowed as its own parameter close:
# the body runs to the next native tag, the next LNDL tag or the end
_UNCLOSED = re.compile(r"(<(lact|lvar)\s+[^>]+>)(.*?)(?=</?｜DSML｜|<(?:lact|lvar)\s|\0|\Z)", re.DOTALL)


def _close_tag(m: re.Match) -> str:
    open_, tag, body = m.group(1), m.group(2), m.group(3)
    if re.search(rf"</{tag}\s*>\s*$", body):
        return m.group(0)
    return f"{open_}{body.rstrip()}</{tag}>\n"


def tool_calls_text(calls: list[dict]) -> str:
    """A turn the provider returned as native tool calls, as the text the model wrote.

    With no tools offered, a model trained on function calling still emits its native call markup
    around LNDL, and the provider parses it out of the content: the tag text lands in the call's
    `name`, cut after the model's `<`, sometimes with the closing tag swallowed. A call with a real
    name and JSON arguments is the other shape, a whole native call, and becomes one <lact>.
    """
    parts = []
    seen: dict[str, int] = {}
    for call in calls:
        fn = call.get("function") or {}
        name, raw = str(fn.get("name") or ""), fn.get("arguments") or ""
        if ">" in name or name.startswith(("lact", "lvar")):
            parts.append("<" + name + ("" if str(raw).strip() in ("", "{}") else str(raw)))
            continue
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (ValueError, TypeError):
            args = {"value": raw}
        if not isinstance(args, dict):
            args = {"value": args}
        alias = _alias(json.dumps(call, sort_keys=True), seen)
        parts.append(f"<lact {alias}>{name}({', '.join(f'{k}={v!r}' for k, v in args.items())})</lact>")
    return "\n".join(parts)
