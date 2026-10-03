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
from asyncio import CancelledError
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from lion1.actor import Message, Reply

from .claude_code import _clipped, _transcript

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
    # for whoever is asking it. Every call's notes then say the release is unmeasured
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
        argv = [
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
                text, usage, failure, seen, raw = await self._attempt(prompt, scratch)
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

    async def _attempt(self, prompt: bytes, scratch: Path) -> tuple[str | None, dict, str, list[str], dict]:
        """One `codex exec` in `scratch`: (text or None, usage, why None, notes, the raw stream and
        stderr when the child answered)."""
        last = scratch / "last.txt"
        last.unlink(missing_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *self.argv(scratch),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(scratch),
            env=self._env(),
            start_new_session=True,  # the CLI's whole process group: a kill ends its children too
        )
        try:
            out, err = await _joined(proc, prompt, self.timeout)
        except TimeoutError:
            return None, {}, f"timed out after {self.timeout:.0f}s", [], {}
        raw = {"stream": out.decode(errors="replace"), "stderr": err.decode(errors="replace")}
        usage: dict = {}
        notes: list[str] = []
        failures: list[str] = []
        kinds: Counter = Counter()
        text_from_events: str | None = None  # the last non-empty message; the final one can be ""
        for line in out.decode(errors="replace").split("\n"):  # an event ends at LF; its text may hold U+2028
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            item = event.get("item") or {}
            kinds[f"{kind}/{item['type']}" if item.get("type") else kind] += 1
            if kind == "turn.completed":
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
            return text, usage, "", notes + failures, raw  # reconnects on a call that still answered
        stderr_tail = err.decode(errors="replace").strip()[-300:]
        if proc.returncode != 0 or failures:  # the last failure is the turn's verdict; reconnects precede it
            why = f"exited {proc.returncode}: " + (failures[-1] if failures else stderr_tail)
        else:
            why = f"no message: events {dict(kinds)}; " + ("; ".join(notes) or stderr_tail)
        return None, usage, why, notes + failures, raw


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
