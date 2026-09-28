"""What a person sees of a conversation, and the line they type."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
from pathlib import Path
from typing import Any

from rich.console import Console as RichConsole
from rich.markdown import Markdown
from rich.markup import escape
from rich.rule import Rule

from hub.areas import partial
from hub.chat import entry_row, line
from lionagi import lndl
from lionagi.record import Entry, Kind

__all__ = ("RESULT_LINES", "Console", "stdin_ask")

_SYSTEM_TAGS = re.compile(r"</?system[^>]*>")
RESULT_LINES = 12  # with --verbose a result shows this many lines; the log and the model have it whole


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
    ):
        self.model = model.split("/")[-1]
        self.calls = calls
        self.billed = 0  # calls already written to the log
        self.log = log
        self.raw = raw
        self.echo = echo
        self.verbose = verbose
        self.quiet_until = -1  # entries up to this seq are logged, not printed: a resumed history
        self.asking = False  # at the `you ▸` prompt: an interrupt there is a quit
        self.resulted: set[str] = set()  # aliases whose result printed since the last notice
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
        self.out.print(f"[dim]{escape(str(directory))} · {escape(', '.join(tools))} · /quit ends[/dim]")

    def close(self, ended: str, calls: int, cost: float, unknown: int, log: Path | None) -> None:
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
        try:
            return await stdin_ask("[bold]you ▸[/bold] ", self.out)
        finally:
            self.asking = False

    def notice(self, text: str) -> None:
        """A line from the console itself, dimmed: what it did on the person's behalf."""
        self.out.print(f"[dim]{escape(text)}[/dim]")

    def __call__(self, e: Entry) -> None:
        if self.log is not None:
            with self.log.open("a") as fh:
                fh.write(json.dumps(entry_row(e), default=str) + "\n")
                self._bill(fh, e.round)
            self.written += 1
        if e.seq <= self.quiet_until:
            return
        if self.raw:
            self.out.print(line(e), markup=False)
            return
        for piece in self.render(e):
            self.out.print(piece, markup=isinstance(piece, str))

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
            return [
                "[dim]" + escape("\n".join([f"· round {e.round}"] + ["  " + ln for ln in lines])) + "[/dim]"
            ]
        if e.kind is Kind.TEXT:
            return self._turn(str(e.content))
        r = e.content  # RESULT
        alias = e.alias or "?"
        self.resulted.add(alias)
        if not getattr(r, "ok", False):
            first = str(getattr(r, "error", r)).splitlines()[0] if str(getattr(r, "error", r)) else ""
            return [f"[red]✗ {escape(alias)} {escape(str(getattr(r, 'spec', '?')))}[/red] {escape(first)}"]
        if isinstance(r.value, dict) and isinstance(r.value.get("rc"), int):  # a shell result: its own rc
            head = _shell_head(alias, r.spec, r.value)
        else:
            head = f"[green]✓ {escape(alias)} {escape(r.spec)}[/green] [dim]{escape(_shape(r.value))}[/dim]"
        if not self.verbose:
            return [head]
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
        return [head + "\n" + escape(shown) + more]

    def _turn(self, text: str) -> list[Any]:
        calls: list[str] = []
        cut = lndl.spans(text)  # the parser's own spans: the prose shown is the prose that ran
        try:
            program = lndl.parse(text)
        except lndl.LNDLError as err:
            calls.append(f"[yellow]? unreadable turn: {escape(str(err))}[/yellow]")
        else:
            for v in program.lvars:  # a value's shape only, like a result; its text is in the log
                calls.append(f"[cyan]= {escape(v.alias)}[/cyan] [dim]· {len(v.value)} chars[/dim]")
            for la in program.lacts:
                args = ", ".join(f"{k}={json.dumps(v, default=str)}" for k, v in la.args.items())
                calls.append(f"[cyan]→ {escape(la.alias)} {escape(la.name)}({escape(args)})[/cyan]")
            if program.out is not None:
                calls.append(f"[yellow]OUT {escape(json.dumps(program.out.fields, default=str))}[/yellow]")
            for problem in program.problems:
                calls.append(f"[yellow]? {escape(problem)}[/yellow]")
        prose, at = [], 0
        for start, end in cut:
            prose.append(text[at:start])
            at = end
        prose.append(text[at:])
        prose = "".join(prose).strip()
        pieces: list[Any] = []
        if prose:
            pieces.append(f"[bold]{escape(self.model)} ▸[/bold]")
            pieces.append(Markdown(prose))
        pieces.extend(calls)
        return pieces


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


async def stdin_ask(prompt: str = "you ▸ ", out: RichConsole | None = None) -> str | None:
    """One line from the terminal, read on a daemon thread so a Ctrl-C ends the process instead of
    hanging on the thread pool's exit join; None at EOF. With `out` the prompt is rich markup."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str | None] = loop.create_future()

    def read() -> None:
        try:
            shown = prompt if sys.stdin.isatty() else ""
            line_: str | None = out.input(shown) if out is not None else input(shown)
        except EOFError:
            line_ = None
        if not fut.done():
            loop.call_soon_threadsafe(fut.set_result, line_)

    threading.Thread(target=read, name="stdin", daemon=True).start()
    return await fut
