"""What a person sees of a conversation, and the line they type."""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from rich.console import Console as RichConsole
from rich.console import Group
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.rule import Rule
from rich.text import Text

from hub.areas import partial
from hub.chat import entry_row, line
from lionagi import lndl
from lionagi.actor import _read
from lionagi.record import Entry, Kind

from .clipboard import to_clipboard
from .voice import Voice

__all__ = ("RESULT_LINES", "Console", "linked", "mermaid_blocks", "diagram_page", "Editor", "stdin_ask")

_SYSTEM_TAGS = re.compile(r"</?system[^>]*>")
_COPY = re.compile(r"/copy(?:\s+(.*))?", re.S)  # the console's own command: never sent to the model
_MERMAID_JS = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"
RESULT_LINES = 12  # with --verbose a result shows this many lines; the log and the model have it whole

_URL = re.compile(r"https?://[^\s<>\[\]`\"']+")
# a path as prose spells it: `a/b.py`, `./a`, `../a`, `~/a`, `/abs/a`, `.hidden/a.toml`, then an
# optional `:line` or `:line:col`; the lookbehind keeps it from starting inside a longer token
_PATH = (
    r"(?<![\w./~:@+-])(?P<path>(?:~|\.{1,2})?/?[\w@+-][\w.@+-]*(?:/[\w.@+-]+)*)"
    r"(?::(?P<line>\d+)(?::(?P<col>\d+))?)?"
)
_PATH_ALONE = re.compile(_PATH)
# what a pass over markdown prose meets, left to right: a backslash escape, a link the model already
# wrote, an autolink, a code span, a bare URL, a path. The first three pass through untouched
_TOKENS = re.compile(
    r"(?P<escaped>\\.)"
    r"|(?P<link>!?\[[^\]\n]*\](?:\([^)\n]*\)|\[[^\]\n]*\]))"
    r"|(?P<autolink><[a-zA-Z][\w+.-]*:[^\s<>]*>)"
    r"|(?<!`)(?P<ticks>`+)(?!`)(?P<code>(?:[^\n]|\n(?![ \t]*\n))+?)(?<!`)(?P=ticks)(?!`)"
    r"|(?P<url>" + _URL.pattern + ")"
    r"|" + _PATH
)
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")  # a fence line: its run of backticks or tildes, then the rest


class Console:
    """What a person sees: their line, the model's prose as markdown under its name, each call as one
    line, each result as one line (its shape and size; the whole of it is in the log, and with
    `verbose` its first lines and the loop's notices too). Every entry goes to the log whole, one JSON
    line each, so the console never holds anything the log does not."""

    def __init__(
        self,
        model: str,
        log: Path | None = None,
        *,
        color: bool | None = None,
        raw: bool = False,
        echo: bool = False,  # print the person's own lines too; a terminal already shows what they typed
        verbose: bool = False,  # results' first lines and the loop's notices
        width: int | None = None,
        calls: list[dict] | None = None,  # the backend's call envelopes: usage and cost go to the log too
        head: dict | None = None,  # the log's first row, written with its first entry: where the chat ran
        history: Path | None = None,  # the person's earlier lines, for Up at the prompt (a terminal only)
        status: Callable[[], str] | None = None,  # the status line, read afresh each time it is shown
        directory: Path | None = None,  # the chat's: relative paths in prose resolve here, never the cwd
    ):
        self.model = model.split("/")[-1]
        self.directory = directory
        self.calls = calls
        self.head = head
        self.billed = 0  # calls already written to the log
        self.log = log
        self.raw = raw
        self.echo = echo
        self.verbose = verbose
        self.quiet_until = -1  # entries up to this seq are logged, not printed: a resumed history
        self.asking = False  # at the `you ▸` prompt: an interrupt there is a quit
        # the round in flight: a spinner with the elapsed time at the bottom of the screen, and under
        # it the last result's detail until the next lands, so the scrollback keeps one line per action
        self.live: Live | None = None
        self.doing: _Working | None = None
        self.detail: str | None = None
        self.commands = False  # the last turn printed asked for commands: the round runs them next
        self.status = status
        self.editor = (
            Editor(history, status=status, copy_last=self.copy, voice=Voice())
            if sys.stdin.isatty() and sys.stdout.isatty()
            else None
        )
        self.resulted: set[str] = set()  # aliases whose result printed since the last notice
        self.responses: list[str] = []  # each response's prose as printed; `/copy N` counts back from the end
        self.transcript: list[str] = []  # the chat in plain text, as `/copy all` copies it
        self.written = 0
        self.out = RichConsole(
            force_terminal=color,
            no_color=(color is False) or None,
            width=width,
            soft_wrap=False,
            highlight=False,
        )
        if log is not None:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.touch()  # appended to from here; a caller that wants a fresh file creates it (`fresh_log`)

    def open(
        self, model: str, directory: Path, chat_id: str, tools: list[str], resumed: tuple[str, int] | None
    ) -> None:
        self.out.print(Rule(f"[bold]{escape(model)}[/bold] · chat {escape(chat_id)}", style="dim"))
        if resumed is not None:
            self.out.print(f"[dim]continuing {escape(resumed[0])}, {resumed[1]} entries[/dim]")
        self.out.print(
            f"[dim]{escape(str(directory))} · {escape(', '.join(tools))} · "
            "/copy [N] copies the last (Nth-last) response, /copy all the whole chat · /quit ends[/dim]"
        )

    def working(self, what: str) -> None:
        """The round is in flight: `what` the loop is waiting on (the model, its commands), with the
        seconds since the round started; on a terminal only, where the line can be redrawn."""
        if self.doing is None:
            self.doing = _Working(what)
        else:
            self.doing.what = what
        if self.live is None and self.out.is_terminal:
            self.live = Live(
                self._below(),
                console=self.out,
                transient=True,
                refresh_per_second=8,
                redirect_stdout=False,  # the CLI session and its hook keep their own streams
                redirect_stderr=False,
            )
            self.live.start()
        elif self.live is not None:
            self.live.update(self._below())

    def idle(self) -> None:
        """Nothing in flight: the spinner and the last detail leave the screen."""
        self.doing = self.detail = None
        if self.live is not None:
            self.live.stop()
            self.live = None

    def failed(self, text: str) -> None:
        """The run ended on a failure: the indicator leaves first, then one red line with it."""
        self.idle()
        self.out.print(f"[red]✗ {escape(text)}[/red]")

    def _below(self) -> Group:
        pieces: list[Any] = []
        if self.detail is not None:
            pieces.append(Text.from_markup(self.detail))
        if self.doing is not None:
            pieces.append(self.doing)
        return Group(*pieces)

    def close(self, ended: str, calls: int, cost: float, unknown: int, log: Path | None) -> None:
        self.idle()
        if self.log is not None and self.calls:  # a call cut short still cost: its row lands too
            with self.log.open("a") as fh:
                self._bill(fh, 0)
        self.out.print(Rule(style="dim"))
        self.out.print(
            f"[dim]{escape(ended)} · {calls} call(s) · ${cost:.4f}"
            + partial(unknown)
            + f" · {self.written} entries"
            + (f" + {self.billed} usage rows" if self.billed else "")
            + (f" in {escape(str(log))} · continue it: lion chat -c" if log is not None else "")
            + "[/dim]"
        )

    async def ask(self) -> str | None:
        """One line from the person, prompted on this console."""
        self.asking = True
        self.idle()
        try:
            if self.editor is not None:
                return await self.editor.ask("you ▸ ")
            return await stdin_ask("[bold]you ▸[/bold] ", self.out)
        finally:
            self.asking = False

    def notice(self, text: str) -> None:
        """A line from the console itself, dimmed: what it did on the person's behalf. A URL in it is a
        link to itself."""
        shown, at = [], 0
        for m in _URL.finditer(text):
            url = _trim_url(m[0])
            shown += [escape(text[at : m.start()]), f"[link={url}]{escape(url)}[/link]"]
            at = m.start() + len(url)
        self.out.print(f"[dim]{''.join(shown) + escape(text[at:])}[/dim]")

    def command(self, text: str) -> bool:
        """Run `text` when it is the console's own command (`/copy`, `/copy N`, `/copy all`) and say
        so; False for anything else, which is the person's message."""
        m = _COPY.fullmatch(text.strip())
        if m is None:
            return False
        which = (m.group(1) or "").strip()
        if which == "all":
            self.notice(self.copy_all())
        elif which and not which.isdigit():
            self.notice(f"/copy takes a count back from the last response, from 1, or all: not {which!r}")
        else:
            self.notice(self.copy(int(which) if which else None))
        return True

    def copy(self, n: int | None = None) -> str:
        """Put the last response, or the `n`th counting back from it (1 = the last, 2 = the one before;
        a continued chat's replayed ones count too), on the clipboard; returns what to tell the person."""
        if not self.responses:
            return "nothing to copy: no response yet"
        n = 1 if n is None else n
        if not 1 <= n <= len(self.responses):
            return f"no response {n} back from the last: this chat has {len(self.responses)}; nothing copied"
        text = self.responses[-n]
        to_clipboard(text)
        which = "the last response" if n == 1 else f"the response {n} back from the last"
        return f"copied {which} ({len(text)} chars)"

    def copy_all(self) -> str:
        """Put the whole chat on the clipboard as plain text, unwrapped: each message, each response
        and each call and result line, a continued chat's replayed part first; the loop's notices stay
        in the log. Returns what to tell the person."""
        if not self.transcript:
            return "nothing to copy: the chat is empty"
        text = "\n".join(self.transcript).strip("\n")
        to_clipboard(text)
        return f"copied the whole chat ({len(text)} chars)"

    def _plain(self, e: Entry) -> list[str]:
        """An entry's lines in the transcript; a message or a response opens with a blank line."""
        if e.kind is Kind.INPUT:
            who = e.meta.get("origin")
            return ["", f"{'you' if who in (None, 'console') else who} ▸ {e.content}"]
        if e.kind is Kind.TEXT:
            text = str(e.content)
            calls, _ = self._calls(text)
            head = ["", f"{self.model} ▸", prose] if (prose := _prose(text)) else []
            return head + [Text.from_markup(c).plain for c in calls]
        if e.kind is Kind.RESULT:
            return [Text.from_markup(self._result(e)[0]).plain]
        return []

    def __call__(self, e: Entry) -> None:
        if self.log is not None:
            with self.log.open("a") as fh:
                if self.head is not None and not self.written:
                    fh.write(json.dumps(self.head) + "\n")
                fh.write(json.dumps(entry_row(e), default=str) + "\n")
                self._bill(fh, e.round)
            self.written += 1
        if e.kind is Kind.TEXT and (prose := _prose(str(e.content))):  # a replayed reply counts too
            self.responses.append(prose)
        self.transcript += self._plain(e)
        if e.seq <= self.quiet_until:
            return
        if self.raw:
            self.out.print(line(e), markup=False)
            return
        if self.live is not None and e.kind is Kind.RESULT and self.verbose:
            # the detail shows under the spinner while it is fresh; the scrollback keeps the head
            head, self.detail = self._result(e)
            self.resulted.add(e.alias or "?")
            self.out.print(head)
            self.live.update(self._below())
            return
        for piece in self.render(e):
            self.out.print(piece, markup=isinstance(piece, str))
        if e.kind is Kind.TEXT and self.doing is not None and self.commands:
            self.working("running")

    def _bill(self, fh, round_no: int) -> None:
        for c in (self.calls or [])[self.billed :]:
            row = {"kind": "USAGE", "round": round_no, "model": c.get("model")}
            row.update({k: c[k] for k in ("usage", "total_cost_usd", "attempt_usage") if k in c})
            fh.write(json.dumps(row, default=str) + "\n")
            self.billed += 1

    def render(self, e: Entry) -> list[Any]:
        """The pieces the console prints for one entry: rich markup strings and Markdown renderables."""
        if e.kind is Kind.INPUT:
            who = e.meta.get("origin") or "you"
            if who in ("you", "console") and not self.echo:
                return []
            return [f"[bold]{escape(who)} ▸[/bold] {escape(str(e.content))}"]
        if e.kind is Kind.SYSTEM:
            body = _SYSTEM_TAGS.sub("", str(e.content)).strip("\n")
            lines = [ln for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("context:")]
            if not self.verbose:  # only what no shown line says: a → with no result line, a broken hook
                items = [
                    f"[yellow]{escape(f'{mark} {alias} {what} · {why}')}[/yellow]"
                    for mark, alias, what, why in _flagged(lines)
                    if not (mark != "!" and alias in self.resulted)  # a failed result was its own ✗ line
                ]
                self.resulted.clear()
                return items
            pieces = [
                "[dim]" + escape("\n".join([f"· round {e.round}"] + ["  " + ln for ln in lines])) + "[/dim]"
            ]
            # a round's own notice, not a cut or the closure: once per round boundary
            if self.status is not None and str(e.content).startswith("<system round="):
                pieces.append(f"[dim]{escape(self.status())}[/dim]")
            return pieces
        if e.kind is Kind.TEXT:
            return self._turn(str(e.content), e.seq)
        head, detail = self._result(e)
        self.resulted.add(e.alias or "?")
        return [head if detail is None else head + "\n" + detail]

    def _result(self, e: Entry) -> tuple[str, str | None]:
        """A result's line, and with `verbose` its first lines (already escaped markup)."""
        r = e.content
        alias = e.alias or "?"
        if not getattr(r, "ok", False):
            first = str(getattr(r, "error", r)).splitlines()[0] if str(getattr(r, "error", r)) else ""
            return (
                f"[red]✗ {escape(alias)} {escape(str(getattr(r, 'spec', '?')))}[/red] {escape(first)}",
                None,
            )
        if isinstance(r.value, dict) and isinstance(r.value.get("rc"), int):  # a shell result: its own rc
            head = _shell_head(alias, r.spec, r.value)
        elif alias.startswith("toolu_") and isinstance(r.value, dict) and r.value:
            # a call of the model's own tools: the record holds its arguments; the first says what it did
            k, v = next(iter(r.value.items()))
            arg = v if isinstance(v, str) else json.dumps(v, default=str)
            head = f"[green]✓ {escape(r.spec)}[/green] [dim]{escape(k)}={escape(_clip(arg, 80))}[/dim]"
            return head, None  # its arguments are the whole of it; the log has them
        else:
            head = f"[green]✓ {escape(alias)} {escape(r.spec)}[/green] [dim]{escape(_shape(r.value))}[/dim]"
        if not self.verbose:
            return head, None
        text = (
            r.value
            if isinstance(r.value, str)
            else json.dumps(r.value, default=str, ensure_ascii=False, indent=1)
        )
        lines = text.splitlines() or [""]
        shown = "\n".join("    " + ln for ln in lines[:RESULT_LINES])
        more = (
            f"\n    [dim](+{len(lines) - RESULT_LINES} lines in the log)[/dim]"
            if len(lines) > RESULT_LINES
            else ""
        )
        return head, escape(shown) + more

    def _calls(self, text: str) -> tuple[list[str], bool]:
        """The lines under a turn's prose, as markup, and whether it asks for commands. The turn is
        read the way the loop reads it: one the loop refuses (a frame only the loop writes, a tag it
        cannot parse) is one line saying nothing in it ran, never calls that look pending."""
        try:
            program = _read(text)
        except lndl.LNDLError as err:
            return [f"[red]✗ nothing in this turn ran: {escape(str(err))}[/red]"], False
        calls: list[str] = []
        for v in program.lvars:  # a value's shape only, like a result; its text is in the log
            calls.append(f"[cyan]= {escape(v.alias)}[/cyan] [dim]· {len(v.value)} chars[/dim]")
        for la in program.lacts:
            calls.append(f"[cyan]→ {escape(la.alias)} {escape(la.name)}({self._args(la.args)})[/cyan]")
        if program.out is not None:
            calls.append(f"[yellow]OUT {escape(json.dumps(program.out.fields, default=str))}[/yellow]")
        for problem in program.problems:
            calls.append(f"[yellow]? {escape(problem)}[/yellow]")
        return calls, bool(program.lacts)

    def _turn(self, text: str, seq: int) -> list[Any]:
        calls, self.commands = self._calls(text)
        prose = _prose(text)
        pieces: list[Any] = []
        if prose:
            pieces.append(f"[bold]{escape(self.model)} ▸[/bold]")
            at = 0
            # a mermaid block prints as code, and under it the line that opens it drawn: a page beside
            # the log, named for the chat, the entry and the block's place in it
            for n, (end, source) in enumerate(_fences(prose), 1):
                pieces.append(Markdown(linked(prose[at:end], self.directory)))
                at = end
                if self.log is None:
                    pieces.append(f"[dim]diagram {n}: not saved, this chat has no log[/dim]")
                    continue
                page = self.log.parent / f"{self.log.stem}-{seq}-{n}.html"
                page.write_text(diagram_page(source), encoding="utf-8")
                uri = page.resolve().as_uri()
                pieces.append(f"[dim][link={uri}]diagram {n}: {escape(uri)}[/link][/dim]")
            if prose[at:].strip():
                pieces.append(Markdown(linked(prose[at:], self.directory)))
        pieces.extend(calls)
        return pieces

    def _args(self, args: dict[str, Any]) -> str:
        """A call's arguments as markup, a `path` that resolves (read, edit, list_dir, …) as a link. The
        text between links is escaped whole, so a bracket in one value never pairs with one in the next."""
        out, run = [], ""
        for i, (k, v) in enumerate(args.items()):
            shown = json.dumps(v, default=str)
            run += (", " if i else "") + f"{k}="
            target = _target(v, self.directory, dirs=True) if k == "path" and isinstance(v, str) else None
            if target is None:
                run += shown
                continue
            out += [escape(run), f"[link={target}]{escape(shown)}[/link]"]
            run = ""
        return "".join(out) + escape(run)


def _prose(text: str) -> str:
    """A turn's prose: the text between its constructs, as the console shows it and `/copy` copies it."""
    prose, at = [], 0
    for start, end in lndl.spans(text):  # the parser's own spans: the prose shown is the prose that ran
        prose.append(text[at:start])
        at = end
    prose.append(text[at:])
    return "".join(prose).strip()


def _flagged(lines: list[str]) -> list[tuple[str, str, str, str]]:
    """The items under a notice's failure headers as (mark, alias, what, why): the console showed the
    command as → and it never ran, failed or was stopped after it started, or a hook around it broke."""
    heads = {
        "not executed:": ("✗", "not run"),
        "failed after starting:": ("✗", "failed after starting"),
        "completion unknown:": ("?", "completion unknown"),
        "hooks failed": ("!", "hook failed"),
    }
    out: list[tuple[str, str, str, str]] = []
    flag = None
    for ln in lines:
        if not ln.startswith("  "):
            flag = next((f for head, f in heads.items() if ln.startswith(head)), None)
        elif flag is not None:
            alias, _, why = ln.strip().partition(": ")
            out.append((flag[0], alias, flag[1], why))
    return out


def _shell_head(alias: str, spec: str, v: dict) -> str:
    """One line for a command that ran in the sandbox: the handler succeeded either way, so the mark
    follows the command's own rc, with its time and, on failure, the first line it complained with."""
    rc, out, err = v["rc"], str(v.get("stdout") or ""), str(v.get("stderr") or "")
    took = f"{v['elapsed']:g}s" if isinstance(v.get("elapsed"), int | float) else "?"
    if rc == 0:
        lines = out.count("\n") + (1 if out and not out.endswith("\n") else 0)
        tail = f"· rc 0 · {took} · {lines} line(s) out"
        return f"[green]✓ {escape(alias)} {escape(spec)}[/green] [dim]{tail}[/dim]"
    said = next((ln for ln in err.splitlines() if ln.strip()), None) or next(
        (ln for ln in reversed(out.splitlines()) if ln.strip()), ""
    )
    return f"[red]✗ {escape(alias)} {escape(spec)} rc {rc}[/red] [dim]· {took}[/dim] {escape(said[:160])}"


def _shape(value: Any) -> str:
    """One line about a result's value: what it is and how big, never what it says."""
    if isinstance(value, str):
        n = value.count("\n") + (1 if value and not value.endswith("\n") else 0)
        return f"· {n} line(s), {len(value)} chars"
    if isinstance(value, list):
        return f"· list of {len(value)}"
    if isinstance(value, dict):
        return f"· dict of {len(value)}: {', '.join(list(value)[:6])}" + (", …" if len(value) > 6 else "")
    return f"· {type(value).__name__}"


def linked(text: str, directory: Path | None) -> str:
    """Markdown `text` with every http(s) URL a link to itself and every path naming an existing file a
    link that opens it in VS Code, at the line when a `:line` follows it. A relative path resolves
    against `directory`, the chat's, never the process's cwd; with no directory only absolute paths
    link. Fenced code is left as written: a link there would print as its own syntax."""
    out, prose, fence = [], [], ""
    for ln in text.splitlines(keepends=True):
        m = _FENCE.match(ln)
        if fence:
            out.append(ln)
            closing = m is not None and m[1][0] == fence[0] and len(m[1]) >= len(fence)
            if closing and not m[2].strip():
                fence = ""
        elif m is not None:
            out += [_TOKENS.sub(lambda t: _link(t, directory), "".join(prose)), ln]
            prose, fence = [], m[1]
        else:
            prose.append(ln)
    return "".join(out) + _TOKENS.sub(lambda t: _link(t, directory), "".join(prose))


def _link(m: re.Match[str], directory: Path | None) -> str:
    """One token of `linked`'s pass, as markdown."""
    if m["url"] is not None:
        url = _trim_url(m["url"])
        return f"<{url}>" + m["url"][len(url) :]
    if m["path"] is not None:
        path, tail = m["path"], ""
        if m["line"] is None:  # a sentence's full stop is not part of the path
            path = path.rstrip(".")
            tail = m["path"][len(path) :]
        if not ("/" in path or "." in path):  # a bare word (README, main) is prose, even when a file
            return m[0]
        target = _target(path, directory, line=m["line"], col=m["col"])
        if target is None:
            return m[0]
        shown = re.sub(r"([\\`*_\[\]<>])", r"\\\1", m[0][: len(m[0]) - len(tail)])  # as written, no emphasis
        return f"[{shown}](<{target}>){tail}"
    if m["code"] is not None:  # a code span that is exactly a URL or a path links whole
        body = m["code"].strip()
        if _URL.fullmatch(body):
            return f"[{m[0]}](<{body}>)"
        if (p := _PATH_ALONE.fullmatch(body)) is not None:
            target = _target(p["path"], directory, line=p["line"], col=p["col"])
            return f"[{m[0]}](<{target}>)" if target else m[0]
    return m[0]


def _target(
    path: str, directory: Path | None, *, line: str | None = None, col: str | None = None, dirs: bool = False
) -> str | None:
    """The `vscode://file/<abs>[:line[:col]]` URL that opens `path` in the editor, when it exists (a
    file, or with `dirs` a directory too); None otherwise. os.path, not Path: its checks answer False
    for a name the filesystem refuses (too long, a NUL) where Path's raise on some versions."""
    full = os.path.expanduser(path)
    if not os.path.isabs(full):
        if directory is None:
            return None
        full = os.path.join(directory, full)
    full = os.path.normpath(full)
    if not (os.path.exists(full) if dirs else os.path.isfile(full)):
        return None
    return "vscode://file" + quote(full) + (f":{line}" if line else "") + (f":{col}" if col else "")


def _trim_url(url: str) -> str:
    """`url` without the sentence around it: trailing punctuation, and a `)` it did not open."""
    while url and (url[-1] in ".,;:!?*_" or (url[-1] == ")" and url.count("(") < url.count(")"))):
        url = url[:-1]
    return url


class _Working:
    """`⠋ thinking · 12s`: what the round waits on and for how long, redrawn by the live region."""

    def __init__(self, what: str):
        self.what, self.since = what, time.monotonic()

    def __rich__(self) -> Text:
        gone = time.monotonic() - self.since
        return Text(f"{_FRAMES[int(gone * 8) % len(_FRAMES)]} {self.what} · {int(gone)}s", style="dim")


_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _clip(text: str, width: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def mermaid_blocks(text: str) -> list[str]:
    """The sources of the fenced ```mermaid blocks in `text`, in order."""
    return [source for _, source in _fences(text)]


def diagram_page(source: str) -> str:
    """A page that draws one mermaid diagram in a browser; mermaid itself loads from the CDN."""
    return (
        '<!doctype html>\n<html>\n<head>\n<meta charset="utf-8">\n<title>diagram</title>\n</head>\n<body>\n'
        f'<pre class="mermaid">\n{html.escape(source)}\n</pre>\n'
        f'<script type="module">\nimport mermaid from "{_MERMAID_JS}";\n'
        "mermaid.initialize({startOnLoad: true});\n</script>\n</body>\n</html>\n"
    )


def _fences(text: str) -> list[tuple[int, str]]:
    """Each closed ```mermaid block in `text` as (the offset just past its closing fence, its source).
    A fence closes on a bare run of its own character at least as long, so a ```mermaid inside a
    longer fence is that fence's text; the info string's first word is the language."""
    found: list[tuple[int, str]] = []
    opened: tuple[str, bool, list[str]] | None = None  # the open fence, whether it is mermaid, its lines
    at = 0
    for ln in text.split("\n"):  # never splitlines: it also breaks on characters a code line may hold
        at += len(ln) + 1
        m = _FENCE.fullmatch(ln)
        if opened is None:
            if m:
                opened = (m[1], m[2].split()[:1] == ["mermaid"], [])
        elif m and m[1][0] == opened[0][0] and len(m[1]) >= len(opened[0]) and not m[2].strip():
            if opened[1]:
                found.append((min(at, len(text)), "\n".join(opened[2])))
            opened = None
        else:
            opened[2].append(ln)
    return found


class Editor:
    """The line a person types on a terminal: an editor over the whole message (prompt_toolkit), so a
    paste of many lines is one message, Enter sends, Alt-Enter (or Ctrl-J) breaks a line, Up recalls
    earlier messages from `history`, Alt-C copies the last response with `copy_last`, and Ctrl-C
    clears the line or, on an empty one, ends the chat. With `voice`, Alt-V records and Alt-V again
    puts what was said at the cursor (`Voice`). Reading through the tty's line discipline lost all of
    that: a paste arrived one line per message and macOS drops any line over 1024 bytes without a
    word (measured)."""

    def __init__(
        self,
        history: Path | None = None,
        *,
        status: Callable[[], str] | None = None,  # the bottom toolbar, called on every redraw
        copy_last: Callable[[], str | None] | None = None,  # Alt-C: copies, returns the notice to show
        voice: Voice | None = None,
        **terminal: Any,  # `input`/`output`: a test's pipe
    ):
        from prompt_toolkit import PromptSession, print_formatted_text
        from prompt_toolkit.application import run_in_terminal
        from prompt_toolkit.history import FileHistory, InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import Style

        keys = KeyBindings()

        @keys.add("enter")
        def _send(event) -> None:
            event.current_buffer.validate_and_handle()

        @keys.add("escape", "enter")
        @keys.add("c-j")
        def _newline(event) -> None:
            event.current_buffer.insert_text("\n")

        @keys.add("c-c")
        def _clear_or_quit(event) -> None:
            if event.current_buffer.text:
                event.current_buffer.reset()
            else:
                event.app.exit(exception=KeyboardInterrupt(), style="class:aborting")

        # Alt-C, not Ctrl-Y (the editor's yank): the line being typed stays as it is
        if copy_last is not None:

            @keys.add("escape", "c")
            def _copy(event) -> None:
                def show() -> None:  # above the prompt, which is redrawn under it
                    if (said := copy_last()) is not None:
                        print_formatted_text([("dim", said)], output=event.app.output)

                run_in_terminal(show)

        if voice is not None:

            @keys.add("escape", "v")
            def _speak(event) -> None:
                voice.press(event.app, event.current_buffer)

        def toolbar() -> str:  # the voice's own line first while it has one
            return " · ".join(p for p in (voice.note if voice else "", status() if status else "") if p)

        self.voice = voice
        if history is not None:
            history.parent.mkdir(parents=True, exist_ok=True)
        self.session: PromptSession[str] = PromptSession(
            multiline=True,
            key_bindings=keys,
            history=FileHistory(str(history)) if history is not None else InMemoryHistory(),
            enable_open_in_editor=False,
            bottom_toolbar=toolbar if status is not None or voice is not None else None,
            style=Style.from_dict({"bottom-toolbar": "noreverse ansibrightblack"}),  # a quiet line, not a bar
            **terminal,
        )

    async def ask(self, prompt: str) -> str | None:
        """One message, or None when the person is done (Ctrl-D, or Ctrl-C on an empty line)."""
        try:
            # Ctrl-C reaches the editor as a key (raw mode); with handle_sigint the prompt would
            # replace the loop's SIGINT handler and remove it on exit, leaving a mid-round Ctrl-C ignored
            return await self.session.prompt_async([("bold", prompt)], handle_sigint=False)
        except (EOFError, KeyboardInterrupt):
            return None
        finally:
            if self.voice is not None:
                self.voice.reset()


async def stdin_ask(prompt: str = "you ▸ ", out: RichConsole | None = None) -> str | None:
    """One message from a pipe, read on a daemon thread so a Ctrl-C ends the process instead of
    hanging on the thread pool's exit join; None at EOF. With `out` a prompt on a terminal is rich
    markup; a terminal normally gets an `Editor` instead."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str | None] = loop.create_future()

    def read() -> None:
        tty = sys.stdin.isatty()
        read_line = (lambda shown: out.input(shown)) if out is not None else input
        try:
            line_: str | None = read_line(prompt if tty else "")
        except EOFError:
            line_ = None
        if not fut.done():
            loop.call_soon_threadsafe(fut.set_result, line_)

    threading.Thread(target=read, name="stdin", daemon=True).start()
    return await fut
