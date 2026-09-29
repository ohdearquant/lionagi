"""What a person sees of a conversation, and the line they type."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
from collections.abc import Callable
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

from .clipboard import to_clipboard

__all__ = ("RESULT_LINES", "Console", "Editor", "stdin_ask")

_SYSTEM_TAGS = re.compile(r"</?system[^>]*>")
_COPY = re.compile(r"/copy(?:\s+(.*))?", re.S)  # the console's own command: never sent to the model
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
        head: dict | None = None,  # the log's first row, written with its first entry: where the chat ran
        history: Path | None = None,  # the person's earlier lines, for Up at the prompt (a terminal only)
        status: Callable[[], str] | None = None,  # the status line, read afresh each time it is shown
    ):
        self.model = model.split("/")[-1]
        self.calls = calls
        self.head = head
        self.billed = 0  # calls already written to the log
        self.log = log
        self.raw = raw
        self.echo = echo
        self.verbose = verbose
        self.quiet_until = -1  # entries up to this seq are logged, not printed: a resumed history
        self.asking = False  # at the `you ▸` prompt: an interrupt there is a quit
        self.status = status
        self.editor = (
            Editor(history, status=status, copy_last=self.copy)
            if sys.stdin.isatty() and sys.stdout.isatty()
            else None
        )
        self.resulted: set[str] = set()  # aliases whose result printed since the last notice
        self.responses: list[str] = []  # each response's prose, as printed: what `/copy N` numbers from 1
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
            f"[dim]{escape(str(directory))} · {escape(', '.join(tools))} · /copy [N] copies a response · "
            "/quit ends[/dim]"
        )

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
            if self.editor is not None:
                return await self.editor.ask("you ▸ ")
            return await stdin_ask("[bold]you ▸[/bold] ", self.out)
        finally:
            self.asking = False

    def notice(self, text: str) -> None:
        """A line from the console itself, dimmed: what it did on the person's behalf."""
        self.out.print(f"[dim]{escape(text)}[/dim]")

    def command(self, text: str) -> bool:
        """Run `text` when it is the console's own command (`/copy`, `/copy N`) and say so; False
        for anything else, which is the person's message."""
        m = _COPY.fullmatch(text.strip())
        if m is None:
            return False
        which = (m.group(1) or "").strip()
        if which and not which.isdigit():
            self.notice(f"/copy takes a response number, from 1: not {which!r}")
        else:
            self.notice(self.copy(int(which) if which else None))
        return True

    def copy(self, n: int | None = None) -> str:
        """Put response `n` (from 1, a continued chat's earlier ones counted) or the last one on the
        clipboard; returns what to tell the person."""
        if not self.responses:
            return "nothing to copy: no response yet"
        n = len(self.responses) if n is None else n
        if not 1 <= n <= len(self.responses):
            return f"no response {n}: this chat has {len(self.responses)}; nothing copied"
        text = self.responses[n - 1]
        to_clipboard(text)
        return f"copied response {n} ({len(text)} chars)"

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
            pieces = [
                "[dim]" + escape("\n".join([f"· round {e.round}"] + ["  " + ln for ln in lines])) + "[/dim]"
            ]
            # a round's own notice, not a cut or the closure: once per round boundary
            if self.status is not None and str(e.content).startswith("<system round="):
                pieces.append(f"[dim]{escape(self.status())}[/dim]")
            return pieces
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
        prose = _prose(text)
        pieces: list[Any] = []
        if prose:
            pieces.append(f"[bold]{escape(self.model)} ▸[/bold]")
            pieces.append(Markdown(prose))
        pieces.extend(calls)
        return pieces


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


class Editor:
    """The line a person types on a terminal: an editor over the whole message (prompt_toolkit), so a
    paste of many lines is one message, Enter sends, Alt-Enter (or Ctrl-J) breaks a line, Up recalls
    earlier messages from `history`, Alt-C copies the last response with `copy_last`, and Ctrl-C
    clears the line or, on an empty one, ends the chat. Reading through the tty's line discipline lost
    all of that: a paste arrived one line per message and macOS drops any line over 1024 bytes without
    a word (measured)."""

    def __init__(
        self,
        history: Path | None = None,
        *,
        status: Callable[[], str] | None = None,  # the bottom toolbar, called on every redraw
        copy_last: Callable[[], str | None] | None = None,  # Alt-C: copies, returns the notice to show
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

        if history is not None:
            history.parent.mkdir(parents=True, exist_ok=True)
        self.session: PromptSession[str] = PromptSession(
            multiline=True,
            key_bindings=keys,
            history=FileHistory(str(history)) if history is not None else InMemoryHistory(),
            enable_open_in_editor=False,
            bottom_toolbar=status,
            style=Style.from_dict({"bottom-toolbar": "noreverse dim"}),  # a quiet line, not a bar
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
