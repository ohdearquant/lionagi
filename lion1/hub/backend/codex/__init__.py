"""Codex CLI as a chat-completion backend (ADR-0009): `codex exec`, one call per turn.

The view goes in as one prompt with its roles marked, the model's final message comes out. The
CLI is an agent, so every feature that lets the model act is switched off by flag (shell, exec,
web search, sub-agents, images, apps, browser, computer use, plugins, hooks, goals). Every command
the model wants goes through this runtime's handlers as LNDL. Without those flags a read-only
sandbox still let the model read this machine and search the web for the upstream fix: 34 commands
and 850k input tokens in one turn. Usage comes from the `turn.completed` event, whose input count
is handed back as the prompt count; there is no cost on a subscription, so `total_cost_usd` stays
None and the bench records the spend as unknown, never as zero.

The flags are not the whole of it. The CLI's model catalogue gives each model a tool mode and a
sub-agent version, and for a model whose entry names them no flag removes those tools: measured on
0.156.1 with gpt-6-luna, every flag here left the model a code-execution tool and six sub-agent
tools, and it started a sub-agent when asked; the stream showed neither call. So each call hands
the CLI a catalogue of its own making: every entry of the CLI's (`codex debug models`, its answer
kept) rebuilt from the fields a chat needs (`KEPT`) with its shell type disabled and no tool
listed (`CHAT`). A field a later release adds is not carried over; the CLI's parser takes an
unknown field without complaint, so the filter is here. With that and the flags the model lists
no tool and can run no command, search or spawn, and the prompt of a one-line question fell from
4,795 input tokens to 191.

That was measured on the releases `MEASURED` names, and what it guards against fails silently: a
release that gives the model a tool by another route shows in no event. So a CLI of any other
release is refused before a prompt goes out, until the model has been asked again on it and the
release added; `unmeasured` lets that asking go through this backend, and notes every call.

The CLI's own base instructions describe an agent with a shell in a read-only workspace; with the
tools off, a reasoning model reads that as a broken interface and, one reply in three at turn one,
answers "unable to access repository tools" instead of writing LNDL. `model_instructions_file`
replaces those instructions with a chat framing and `include_environment_context=false` drops the
cwd and sandbox lines; measured on gpt-5.6-luna, the turn-one prompt falls from 7.4k to 3.6k tokens
and the refusals stop (0 of 12 turn-one samples; 4 of 12 before). The shared `~/.codex` home
injects its own AGENTS.md and every plugin's skill list into the prompt, so a call runs from a
private home when one is given (`home`): a directory holding only `auth.json` as a symlink to the
real one. The login crate writes that file in place (`OpenOptions` truncate+write, no rename), so
the link stays a link and both homes read one file. `CODEX_ACCESS_TOKEN` alone is refused (401), so
the link is the way. A scratch AGENTS.md beside the call says there are no guidance files to read,
for the shared-home case.

A failed call is retried when the stream says the failure was the transport's or the model
returned nothing: `turn.failed` on a 429/5xx or a timeout (never on 401/403), or `turn.completed`
with no message (a reasoning-heavy turn that ran out of output). Each call has its own scratch
directory, reused by its retries and removed once its child is joined; a call that fails for good
lands on `calls` as a failed envelope carrying its attempts' usage and the last attempt's raw
stream and stderr, since the scratch is gone: each text, the error's too, by its head and tail
within `claude_code._DIAGNOSTIC_CHARS` characters, counting what it left out. The notes of any call
(its events of note, each failed attempt's reason) are kept to the first `_NOTES`, each of them by
its head and tail within the same cap, then one entry counting the rest.

`CodexSession` is the same CLI kept on one thread per run (ADR-0009/C7): the first call starts a
thread, each later call resumes it (`codex exec resume <thread>`) with only what the view gained,
and the run's system prompt is the CLI's instructions. Measured on 0.156.1: a resumed thread
remembers its turns and takes the same flags and catalogue; resumed from another directory it is
sent that directory's AGENTS.md again, so every child runs in one; the instructions are read again
at every resume; the stream's `turn.completed` then carries the THREAD's running totals (456 after
177 and 279), so a call's share is read against the totals of the call before. The CLI's `exec`
has no fork, so a thread is gone on in place.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import tempfile
import time
import weakref
from asyncio import CancelledError
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from lion1.actor import Message, Reply, SessionPlan

from ..claude_code import (
    _REPLY,
    _beyond,
    _clipped,
    _named,
    _new,
    _plain,
    _Point,
    _prefix,
    _prompt,
    _Step,
    _transcript,
)

__all__ = (
    "TRANSIENT",
    "DISABLED",
    "MEASURED",
    "KEPT",
    "CHAT",
    "INSTRUCTIONS",
    "SCRATCH_AGENTS_MD",
    "CodexError",
    "Codex",
    "CodexSession",
)

TRANSIENT = ("429", "500", "502", "503", "504", "timed out", "timeout", "overloaded")
# never retried, whatever else the message says ("401 Unauthorized: request timeout" is an auth failure)
AUTH = re.compile(r"\b40[13]\b|unauthori[sz]ed|forbidden", re.IGNORECASE)
DISABLED = (  # codex features that let the model act; `codex features list` names them
    "plugins",  # the plugin catalog and their skills would ride into every prompt
    "hooks",
    "shell_tool",
    "unified_exec",
    "skill_search",
    "tool_suggest",
    "sleep_tool",
    "apps",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "multi_agent",  # spawn_agent worked with only the shell off
    "image_generation",
    "view_image",
    "goals",  # get_goal, create_goal, update_goal
)
# the CLI releases on which a model, asked through this backend, listed no tool and could run
# none (ADR-0009/A2, S19); on any other the claim is unmeasured and the backend refuses
MEASURED = ("0.156.1",)
# what a chat needs of the CLI's catalogue entry for a model: its name, its window, how it reasons,
# how its request is shaped. Nothing else of an entry is handed back, so a field that gives a model
# a tool (its tool mode, its sub-agent version, its patch and search tools, any a later release adds)
# is never among them
KEPT = (
    "slug",
    "display_name",
    "visibility",
    "supported_in_api",
    "priority",
    "supported_reasoning_levels",
    "default_reasoning_level",
    "default_reasoning_summary",
    "support_verbosity",
    "default_verbosity",
    "context_window",
    "max_context_window",
    "effective_context_window_percent",
    "truncation_policy",
    "use_responses_lite",
)
_CATALOG_S = 60.0  # how long the CLI has to print its catalogue: it reads a file, 0.13 s measured
# a restart's price, a character sent outside the provider's cache in reads of one inside it: the
# provider's published prices put cached input at a tenth of fresh input and charge nothing more
# to write. A subscription bills neither; the ratio only weighs a restart. Assumed, not measured
# (ADR-0009/A4)
_FRESH_OVER_CACHED = 10
# the provider's cache life from its last read: 5 to 10 minutes idle by its documentation, the low
# end taken (ADR-0009/A4)
_CACHE_TTL_S = 300.0
_TAIL = 65536  # the most of a thread's session file read for its last token count
_THREAD = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")  # a thread id as the CLI prints it
_clock = time.monotonic
INSTRUCTIONS = (  # replaces the CLI's base instructions, which describe an agent with tools
    "You are a language model completing a conversation. The conversation arrives as tagged blocks "
    "(USER, ASSISTANT, SYSTEM); the latest messages define the format your reply must take. Reply "
    "only in that format, with nothing before or after it.\n"
)
# what every entry says beside `KEPT`: fields the CLI's parser requires, as they stand for a chat
CHAT = {"shell_type": "disabled", "experimental_supported_tools": [], "base_instructions": INSTRUCTIONS}
SCRATCH_AGENTS_MD = (  # beside the shared home's AGENTS.md, which says to read guidance files first
    "This process is a text harness. Reply only in the format the latest user message specifies; "
    "there are no guidance files to read.\n"
)
_NOTES = 20  # the most notes a call's envelope keeps


class CodexError(RuntimeError):
    pass


@dataclass
class Codex:
    model: str | None = None  # None: the CLI's default
    effort: str | None = None  # model_reasoning_effort: low | medium | high | xhigh | max
    cli: str | None = None
    home: str | None = None  # CODEX_HOME for the call; None = the CLI's own (shared) home
    timeout: float = 900.0  # per attempt; a high-effort model on a large view takes minutes
    retries: int = 2  # further attempts after a transient failure or an empty answer
    # run on a CLI release outside `MEASURED`, where nobody has asked the model what it can call:
    # for whoever is asking it, and for nothing served (ADR-0009/S23): it is the one way back to a
    # model that may act unseen. Every call's notes then say the release is unmeasured
    unmeasured: bool = False
    calls: list[dict] = field(default_factory=list)  # per call: usage, timing, model, events of note
    _chat_catalog: str | None = field(default=None, repr=False)  # the catalogue each call hands the CLI
    _release_note: str | None = field(default=None, repr=False)  # on an unmeasured release, what notes say

    def __post_init__(self) -> None:
        self.cli = self.cli or shutil.which("codex")
        if not self.cli:
            raise CodexError("codex CLI not found on PATH")
        if self.home is not None:
            self.home = os.path.expanduser(self.home)
            if not os.path.exists(os.path.join(self.home, "auth.json")):
                raise CodexError(
                    f"{self.home} has no auth.json; make it a symlink to the real one: "
                    f"mkdir -p {self.home} && ln -s ~/.codex/auth.json {self.home}/auth.json"
                )

    def argv(self, scratch: Path) -> list[str]:
        return [
            self.cli,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",  # config.toml only; AGENTS.md and plugins need `home`
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-C",
            str(scratch),
            *self._flags(scratch),
        ]

    def _flags(self, scratch: Path) -> list[str]:
        """What every call passes, one-shot or on a thread: nothing that acts, the framing, the model."""
        argv = [
            "-c",
            f'model_catalog_json="{scratch / "models.json"}"',  # the catalogue with `CHAT` in every entry
            "-c",
            "agents.max_depth=0",  # removes the first sub-agent version's spawn_agent; the flag does not
            "-c",
            'web_search="disabled"',
            "-c",
            "tools.web_search=false",
            "-c",
            f'model_instructions_file="{scratch / "instructions.md"}"',
            "-c",
            "include_environment_context=false",  # the cwd and sandbox lines: a workspace it refuses to edit
            "-c",
            "include_permissions_instructions=false",
            "-c",
            "include_apps_instructions=false",
            "-c",
            "include_collaboration_mode_instructions=false",
            "-c",
            "skills.bundled.enabled=false",  # the CLI's own skills, listed in every prompt (2.5k characters)
            "-c",
            "tools.experimental_request_user_input.enabled=false",  # nobody is there to answer
            "-o",
            str(scratch / "last.txt"),
        ]
        for feature in DISABLED:
            argv += ["--disable", feature]
        if self.model:
            argv += ["-m", self.model]
        if self.effort:
            argv += ["-c", f'model_reasoning_effort="{self.effort}"']
        argv.append("-")  # the prompt on stdin
        return argv

    async def __call__(self, messages: list[Message]) -> str:
        prompt = _transcript(messages).encode()
        usage_total: Counter = Counter()
        notes: list[str] = []
        t0 = time.monotonic()
        error: str | None = None
        try:
            catalog = await self._catalog()
        except CodexError as e:  # no attempt went out; the call still lands its envelope (ADR-0009/C5)
            self.calls.append({**self._envelope(usage_total, notes, t0, 0), "error": _clipped(str(e))})
            raise
        if self._release_note:
            notes.append(self._release_note)
        # this call's own directory: its retries reuse it, and it goes once the child is joined
        scratch = Path(tempfile.mkdtemp(prefix="lionagi-codex-"))
        answered = 0  # attempts that came back, billed whether or not they answered
        try:
            (scratch / "AGENTS.md").write_text(SCRATCH_AGENTS_MD)
            (scratch / "instructions.md").write_text(INSTRUCTIONS)
            (scratch / "models.json").write_text(catalog)
            for attempt in range(self.retries + 1):
                text, usage, failure, seen, raw, _ = await self._attempt(prompt, scratch)
                answered += 1
                usage_total.update({k: v for k, v in usage.items() if isinstance(v, int)})
                notes += seen
                if text is not None:
                    break
                notes.append(f"attempt {attempt + 1} failed: {failure}")
                if attempt < self.retries and (
                    failure.startswith("no message")
                    or (not AUTH.search(failure) and any(t in failure for t in TRANSIENT))
                ):
                    await asyncio.sleep(2.0 * 2**attempt)
                    continue
                error = f"codex {failure}"
                break
        except CancelledError:
            # a cancel in a backoff, or in a later attempt, after an attempt came back: that attempt's
            # usage still lands on `calls` (ADR-0009/C5)
            if answered:
                self.calls.append(
                    {**self._envelope(usage_total, notes, t0, attempt + 1), "error": "cancelled"}
                )
            raise
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        envelope = self._envelope(usage_total, notes, t0, attempt + 1)
        if error is not None:
            # the billed attempts' usage and the last attempt's raw stream ride on the failed envelope
            # (ADR-0009/C5): the scratch that held the stream is gone. Each text is kept by head and tail
            self.calls.append(
                {**envelope, "error": _clipped(error), **{k: _clipped(v) for k, v in raw.items()}}
            )
            raise CodexError(error)
        self.calls.append(envelope)
        # the answering attempt's input count, cache read included, is the prompt it sent (ADR-0009/C2);
        # the envelope sums every attempt for the bill
        return Reply(text, prompt_tokens=usage.get("input_tokens"))

    def _envelope(self, usage_total: Counter, notes: list[str], t0: float, attempts: int) -> dict:
        cached = usage_total.get("cached_input_tokens", 0)
        kept = [_clipped(n) for n in notes[:_NOTES]]
        if len(notes) > _NOTES:
            kept.append(f"[{len(notes) - _NOTES} more note(s) omitted]")
        return {
            "model": self.model,
            "effort": self.effort,
            "duration_api_ms": int((time.monotonic() - t0) * 1000),
            "attempts": attempts,
            "total_cost_usd": None,  # a subscription, not a meter
            "usage": {
                **usage_total,
                "input_tokens": usage_total.get("input_tokens", 0) - cached,
                "cache_read_input_tokens": cached,
                "cache_creation_input_tokens": usage_total.get("cache_write_input_tokens", 0),
                "output_tokens": usage_total.get("output_tokens", 0),
            },
            "notes": kept,
        }

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.home:
            env["CODEX_HOME"] = self.home
        return env

    async def _catalog(self) -> str:
        """The catalogue a call hands the CLI: every entry of the CLI's own rebuilt from `KEPT` and
        `CHAT`. Asked of the CLI, after its release, and kept once both have answered (calls that
        start together before that each ask). A release outside `MEASURED` is refused unless
        `unmeasured`; a CLI that prints no catalogue, or exits non-zero whatever it printed, fails
        the call. Either way no prompt has gone out."""
        if self._chat_catalog is None:
            code, out, err = await self._ask("--version")
            found = re.search(r"\d+\.\d+\.\d+[^\s]*", out) if code == 0 else None
            release = found.group() if found else None
            note = None
            if release not in MEASURED:
                said = (
                    f"codex-cli {release}"
                    if release
                    else f"a CLI that printed no release ({_clipped(out + err)!r})"
                )
                note = (
                    f"{said} is not a release this backend's tools-off claim was measured on "
                    f"({', '.join(MEASURED)}): the model may have tools no flag removes (ADR-0009/S19)"
                )
                if not self.unmeasured:
                    raise CodexError(
                        f"{note}. Ask the model for its tools through this backend built with "
                        "unmeasured=True, then add the release to MEASURED"
                    )
            code, out, err = await self._ask("debug", "models")
            try:
                if code != 0:
                    raise ValueError("the command failed")
                models = json.loads(out)["models"]
                if not all(isinstance(entry, dict) for entry in models):
                    raise TypeError("an entry that is no object")
                entries = [{**{k: entry[k] for k in KEPT if k in entry}, **CHAT} for entry in models]
            except (ValueError, KeyError, TypeError):
                raise CodexError(
                    f"codex debug models printed no model catalogue (exit {code}): "
                    f"{_clipped(err.strip() or out.strip())}"
                ) from None
            self._release_note = note
            self._chat_catalog = json.dumps({"models": entries})
        return self._chat_catalog

    async def _ask(self, *args: str) -> tuple[int | None, str, str]:
        """`codex <args>` with nothing on its stdin, within `_CATALOG_S`: its exit code and what it
        printed."""
        proc = await asyncio.create_subprocess_exec(
            self.cli,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(),
            start_new_session=True,
        )
        try:
            out, err = await _joined(proc, None, _CATALOG_S)
        except TimeoutError:
            raise CodexError(f"codex {' '.join(args)} did not answer within {_CATALOG_S:.0f}s") from None
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    async def _attempt(
        self, prompt: bytes, scratch: Path, argv: list[str] | None = None, cwd: Path | None = None
    ) -> tuple[str | None, dict, str, list[str], dict, str | None]:
        """One `codex exec` in `scratch`, or in `cwd` with its files in `scratch`: (text or None,
        usage, why None, notes, the raw stream and stderr when the child answered, the thread the
        stream named)."""
        last = scratch / "last.txt"
        last.unlink(missing_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *(argv or self.argv(scratch)),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd or scratch),
            env=self._env(),
            start_new_session=True,  # the CLI's whole process group: a kill ends its children too
        )
        try:
            out, err = await _joined(proc, prompt, self.timeout)
        except TimeoutError:
            return None, {}, f"timed out after {self.timeout:.0f}s", [], {}, None
        raw = {"stream": out.decode(errors="replace"), "stderr": err.decode(errors="replace")}
        usage: dict = {}
        notes: list[str] = []
        failures: list[str] = []
        kinds: Counter = Counter()
        text_from_events: str | None = None  # the last non-empty message; the final one can be ""
        thread: str | None = None
        for line in out.decode(errors="replace").split("\n"):  # an event ends at LF; its text may hold U+2028
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            item = event.get("item") or {}
            kinds[f"{kind}/{item['type']}" if item.get("type") else kind] += 1
            if kind == "thread.started":
                thread = event.get("thread_id") if isinstance(event.get("thread_id"), str) else thread
            elif kind == "turn.completed":
                usage = event.get("usage") or {}
            elif kind == "turn.failed":
                failures.append(str((event.get("error") or {}).get("message"))[:300])
            elif kind == "error":
                failures.append(str(event.get("message"))[:300])
            elif item.get("type") == "agent_message":
                text_from_events = item.get("text") or text_from_events
            elif item.get("type") == "error":
                notes.append(str(item.get("message"))[:300])
            elif item.get("type") in (
                "command_execution",
                "file_change",
                "mcp_tool_call",
                "web_search",
                "collab_tool_call",
            ):  # none with DISABLED and CHAT in force. The stream is no proof of that: it showed
                # nothing of a sub-agent the model started, nor of its code-execution call (0.156.1)
                notes.append(f"{item['type']}: {str(item.get('command') or item.get('tool') or item)[:120]}")
        text = last.read_text() if last.exists() else ""
        if not text.strip():
            text = text_from_events or ""
        if text.strip():
            return text, usage, "", notes + failures, raw, thread  # reconnects on a call that still answered
        stderr_tail = err.decode(errors="replace").strip()[-300:]
        if proc.returncode != 0 or failures:  # the last failure is the turn's verdict; reconnects precede it
            why = f"exited {proc.returncode}: " + (failures[-1] if failures else stderr_tail)
        else:
            why = f"no message: events {dict(kinds)}; " + ("; ".join(notes) or stderr_tail)
        return None, usage, why, notes + failures, raw, thread


@dataclass
class CodexSession(Codex):
    """The chat-completion shape on one CLI thread per run: the first call starts a thread, each
    later call resumes it with only what the view gained since, and the CLI keeps the conversation.
    The run's system prompt is the CLI's instructions, written again for every call (the CLI reads
    them at each resume). The model has no tools, as in `Codex`.

    The CLI's `exec` cannot fork a thread, so a call goes on in place and the thread holds every
    prompt it was sent. When the view changes under what the thread holds (a context.hide,
    context.show or context.summarize, or the runtime's fold event), the session either goes on,
    holding what the view no longer shows, or starts a new thread from the whole view. It starts
    over only when that pays: what it frees, read on each of the next `payback` turns, must cost at
    least what it sends again outside the cache (`_FRESH_OVER_CACHED` reads a character; a thread
    idle past `_CACHE_TTL_S` is priced as sent again either way). The fold always starts it over.

    An attempt that fails on a resumed thread leaves that thread in a state this backend cannot
    see (the CLI may have kept the prompt): its retry starts a new thread from the whole view, and
    a call that fails or is cancelled there is never gone on from."""

    # the turns a restart the view asks for must repay itself in (`_step`)
    payback: int = 10
    # a prompt count and the characters it stood in, from a session before this backend's (a resumed
    # chat's last call): the first start's figure stands on it; each start after sets its own
    base: tuple[int, int] | None = None
    session_id: str | None = None  # the current thread, as the CLI named it
    sent: dict[str, int] = field(default_factory=dict)  # entry name -> hash of what the thread got
    restarts: int = 0  # threads started over because the view changed under them
    _last: dict | None = field(default=None, repr=False)
    _system: str | None = field(default=None, repr=False)
    _point: _Point | None = field(default=None, repr=False)  # what the current thread's last call left
    _run: str | None = field(default=None, repr=False)  # the record the current thread serves
    _where: str | None = field(default=None, repr=False)  # the directory every call's child runs in
    _runs: dict[str, dict] = field(default_factory=dict, repr=False)  # the other records' threads

    async def __call__(self, messages: list[Message]) -> str:
        system = messages[0].content if messages and messages[0].role == "system" else ""
        body = messages[1:] if system else messages
        run = messages[0].run if messages else None
        if run is not None:
            # one thread per record: the actor names the run on every message, and this backend
            # keeps a thread per run id, so two runs through one backend never share one
            self._switch(run)
        first = next((m for m in body if m.name is not None), None)
        if (
            run is None
            and self.sent
            and (
                system != self._system
                or (
                    first is not None
                    and first.name in self.sent
                    and self.sent[first.name] != hash(first.content)
                )
            )
        ):
            # a caller that names no run: entry names start over at _in0 in every run, so a first
            # entry already delivered with other content, or another system prompt, is a different
            # run on this backend. It gets its own thread; nothing is treated as sent.
            self.session_id, self.sent, self._last, self._point, self.base = None, {}, None, None, None
        self._system = system
        # decided on the view without its last entry when that entry is new (the notification the
        # actor wrote after asking `plan`), so the call does what the notification told
        held = {(n, h) for n, h, _ in self._point.content} if self._point else set()
        tail: list[Message] = []
        if body and (body[-1].name, hash(body[-1].content)) not in held:
            body, tail = body[:-1], body[-1:]
        forced = any(m.spec == "fold" and (m.name, hash(m.content)) not in held for m in body + tail)
        step = self._step(body, tail, forced)
        if step.kind == "full":
            self.restarts += 1
        usage_total: Counter = Counter()
        notes: list[str] = []
        t0 = time.monotonic()
        error: str | None = None
        try:
            catalog = await self._catalog()
        except CodexError as e:  # no attempt went out; the call still lands its envelope (ADR-0009/C5)
            self.calls.append({**self._envelope(usage_total, notes, t0, 0), "error": _clipped(str(e))})
            raise
        if self._release_note:
            notes.append(self._release_note)
        scratch = Path(tempfile.mkdtemp(prefix="lionagi-codex-"))
        answered = 0  # attempts that came back, billed whether or not they answered
        resumed = False  # an attempt went out on the thread held; the CLI may have kept its prompt
        unknown = False  # an attempt's share could not be read off the thread's totals
        try:
            cwd = self._cwd()
            (scratch / "instructions.md").write_text(system or INSTRUCTIONS)
            (scratch / "models.json").write_text(catalog)
            for attempt in range(self.retries + 1):
                if attempt and step.base is not None:
                    whole = body + tail
                    step = _Step("full", None, whole, _prompt(whole), 0, None)
                    notes.append("the retry starts a new thread from the whole view")
                resumed = resumed or step.base is not None
                argv = self._thread_argv(scratch, step.base.id if step.base is not None else None)
                text, totals, failure, seen, raw, thread = await self._attempt(
                    step.prompt.encode(), scratch, argv, cwd
                )
                answered += 1
                usage, lost = _share(totals, step.base.envelope if step.base is not None else None)
                unknown = unknown or lost
                usage_total.update(usage)
                notes += seen
                if text is not None:
                    break
                notes.append(f"attempt {attempt + 1} failed: {failure}")
                if attempt < self.retries and (
                    failure.startswith("no message")
                    or (not AUTH.search(failure) and any(t in failure for t in TRANSIENT))
                ):
                    await asyncio.sleep(2.0 * 2**attempt)
                    continue
                error = f"codex {failure}"
                break
        except CancelledError:
            if resumed:
                self._keep(run, _NO_THREAD)
            # the attempts that came back still land their usage on `calls` (ADR-0009/C5)
            if answered:
                self.calls.append(
                    {**self._envelope(usage_total, notes, t0, attempt + 1), "error": "cancelled"}
                )
            raise
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        chars = (step.base.chars if step.base is not None else 0) + len(step.prompt)
        envelope = {
            **self._envelope(usage_total, notes, t0, attempt + 1),
            "thread_id": thread,
            "step": step.kind,  # what the answering attempt did: a retry can start over
            "prompt_chars": chars,
            "cumulative": totals,  # the thread's running totals, as the stream printed them
        }
        if unknown:
            # a total missing, or below the call before: that attempt's share is left out, never guessed
            envelope["usage_unknown"] = True
        if error is not None:
            if resumed:
                self._keep(run, _NO_THREAD)
            self.calls.append(
                {**envelope, "error": _clipped(error), **{k: _clipped(v) for k, v in raw.items()}}
            )
            raise CodexError(error)
        envelope.update(self._meter(thread))
        # the answering attempt's own input count, cache read included (ADR-0009/C2): with no tools a
        # call is one request, so its share of the thread's input is the prompt it stood in. The
        # envelope carries it too: its `usage` sums the attempts, and a log's reader needs this one
        count = usage.get("input_tokens")
        envelope["prompt_tokens"] = count
        self.calls.append(envelope)
        if thread is None:
            # a stream that named no thread leaves nothing to resume: the next call starts over
            self._keep(run, _NO_THREAD)
            return Reply(text, prompt_tokens=count)
        base = step.base
        content = (base.content if base is not None else ()) + tuple(
            (m.name, hash(m.content), len(m.content)) for m in step.sends
        )
        content += ((_REPLY, hash(text), len(text)),)
        point = _Point(thread, content, chars + len(text), chars, count, totals, _clock())
        state = {"session_id": thread, "sent": _named(content), "_last": envelope, "_point": point}
        if base is None and count is not None:
            state["base"] = (count, chars)
        self._keep(run, state)
        return Reply(text, prompt_tokens=count)

    def plan(self, view: list[Message], *, forced: bool = False, run: str | None = None) -> SessionPlan:
        """What the next call on this view does, asked before the notification is written: whether
        the thread is started over, what it holds beyond the view, and the count it will stand in
        (ADR-0009/C7). The call decides the same on the same view, unless the thread's cache life
        ends between the two. `run` names the record even when its view is still empty, so another
        run's thread is never the one planned."""
        run = run if run is not None else view[0].run if view else None
        if run is not None:
            self._switch(run)
        step = self._step(list(view), [], forced)
        beyond = _beyond(view, self._point.content) if self._point is not None else 0
        return SessionPlan(step.kind == "full", max(0, beyond) // 4, step.tokens)

    def _step(self, view: list[Message], tail: list[Message], forced: bool) -> _Step:
        """The call's step: start a thread, go on in it, or start over from the whole view (`full`).
        `view` decides; `tail`, new either way, rides whatever is sent."""
        cur = self._point
        if cur is None:
            tokens = None
            if self.base is not None:
                tokens = self.base[0] + (len(_prompt(view)) - self.base[1]) // 4
            return _Step("start", None, view + tail, _prompt(view + tail), 0, tokens)
        new = _new(view, cur.content)
        sends = new + [m for m in _new(tail, cur.content) if m not in new] or (view + tail)[-1:]
        cont = _Step("continue", cur, sends, _plain(sends), 0, None)
        kept = cur.chars + len(_plain(new))  # what the thread holds going on, the tail aside
        if cur.count is not None:
            cont.tokens = cur.count + (kept - cur.prompt) // 4
        if not forced and _prefix(cur.content, view) is not None:
            return cont
        whole = len(_prompt(view))
        tokens = None if self.base is None else self.base[0] + (whole - self.base[1]) // 4
        full = _Step("full", None, view + tail, _prompt(view + tail), kept - whole, tokens)
        if forced:
            return full
        # the view changed under the thread: starting over, priced against going on. What it sends
        # outside the cache beyond going on's: the whole view and the instructions, less what going
        # on would send anyway, and less the thread itself once its cache life has passed
        system = len(self._system or "")
        stale = system + cur.chars if _clock() - cur.read >= _CACHE_TTL_S else 0
        writes = system + whole - len(_plain(new)) - stale
        if full.frees > 0 and self.payback * full.frees >= _FRESH_OVER_CACHED * writes:
            return full
        return cont

    def _cwd(self) -> Path:
        """The one directory every call's child runs in, holding the scratch AGENTS.md. A thread
        resumed in another directory is sent that directory's AGENTS.md again, and keeps it: about
        90 tokens a turn, measured. Each call's own files stay in its scratch, named by path. The
        directory goes when the backend is collected or the interpreter exits."""
        if self._where is None or not os.path.isdir(self._where):
            self._where = tempfile.mkdtemp(prefix="lionagi-codex-session-")
            (Path(self._where) / "AGENTS.md").write_text(SCRATCH_AGENTS_MD)
            weakref.finalize(self, shutil.rmtree, self._where, ignore_errors=True)
        return Path(self._where)

    def _thread_argv(self, scratch: Path, thread: str | None) -> list[str]:
        """A persisted `exec`, or its `resume` of `thread`. The resume form takes no `--sandbox` and no
        `-C`: the sandbox goes by its config key in both forms, the directory by the child's own."""
        return [
            self.cli,
            "exec",
            *(("resume", thread) if thread is not None else ()),
            "--json",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "-c",
            'sandbox_mode="read-only"',
            *self._flags(scratch),
        ]

    def _meter(self, thread: str | None) -> dict:
        """The model's context window and the plan's rate limits, as the CLI last wrote them in the
        thread's session file. Nothing when the file is not found or not read: its layout is the
        CLI's own, and a subscription has no other spend figure."""
        if thread is None or not _THREAD.fullmatch(thread):
            return {}
        home = Path(self.home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        try:
            (path,) = (home / "sessions").glob(f"*/*/*/rollout-*-{thread}.jsonl")
            with path.open("rb") as f:
                f.seek(max(0, path.stat().st_size - _TAIL))
                lines = f.read().decode(errors="replace").split("\n")
        except (OSError, ValueError):
            return {}
        for line in reversed(lines):
            try:
                payload = json.loads(line)["payload"]
                if payload["type"] != "token_count":
                    continue
            except (ValueError, KeyError, TypeError):
                continue
            out = {}
            info = payload.get("info")
            window = info.get("model_context_window") if isinstance(info, dict) else None
            if isinstance(window, int) and not isinstance(window, bool):
                out["context_window"] = window
            if isinstance(payload.get("rate_limits"), dict):
                out["rate_limits"] = payload["rate_limits"]
            return out
        return {}

    def _keep(self, run: str | None, state: dict) -> None:
        """`state` onto `run`'s thread: this backend's own fields when that run is the current one,
        else where another run's call parked it while this call was out."""
        for key, value in state.items():
            if run is None or run == self._run:
                setattr(self, key, value)
            else:
                self._runs[run][key] = value

    def _switch(self, run: str) -> None:
        """Make `run`'s thread the current one, parking the thread of the run served before."""
        if run == self._run:
            return
        keys = ("session_id", "sent", "_last", "_system", "_point", "base")
        if self._run is not None:
            self._runs[self._run] = {k: getattr(self, k) for k in keys}
        saved = self._runs.pop(run, None)
        if saved is not None:
            for k in keys:
                setattr(self, k, saved[k])
        elif self._run is not None or self.sent:
            self.session_id, self.sent, self._last, self._system = None, {}, None, None
            self._point, self.base = None, None
        self._run = run


_NO_THREAD = {"session_id": None, "sent": {}, "_last": None, "_point": None}  # nothing to go on from


def _share(totals: dict, prev: dict | None) -> tuple[dict, bool]:
    """What one attempt used, and whether any of it could not be told. A new thread's totals are its
    own. A resumed thread's are running totals, read against the totals of the call it resumed
    (`prev`): a total missing on either side, or below the one before, leaves that figure out, never
    negative. The input and its cached part are told together or not at all, since the fresh input
    is their difference."""
    own = {k: v for k, v in totals.items() if isinstance(v, int) and not isinstance(v, bool)}
    usage, known = own, own.keys()
    if prev is not None and own:  # a stream that printed no totals (a failed attempt) told nothing
        before = {k: v for k, v in prev.items() if isinstance(v, int) and not isinstance(v, bool)}
        usage = {k: now - before[k] for k, now in own.items() if k in before and now >= before[k]}
        known = own.keys() | before.keys()
    pair = {"input_tokens", "cached_input_tokens"}
    if "input_tokens" not in usage or not pair & known <= usage.keys():
        usage = {k: v for k, v in usage.items() if k not in pair}
    return usage, usage.keys() != known


async def _joined(
    proc: asyncio.subprocess.Process, data: bytes | None, timeout: float
) -> tuple[bytes, bytes]:
    """What the child printed, given `data` on its stdin. On a timeout or a cancel its whole group
    is killed and reaped before the exception goes on."""
    try:
        return await asyncio.wait_for(proc.communicate(data), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        # the child is this backend's to stop: a cancelled task must not leave it running, nor
        # a grandchild holding the pipes open past the loop's end
        # the group, whether or not its leader has exited: a child the leader left behind can
        # hold the pipes open, and the drain below would wait on it with no deadline
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        await proc.communicate()  # drains and closes the pipes the cancelled read left open
        raise
