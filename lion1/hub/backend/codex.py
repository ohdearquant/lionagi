"""Codex CLI as a chat-completion backend (ADR-0009): `codex exec`, one call per round.

The view goes in as one prompt with its roles marked, the model's final message comes out. The
CLI is an agent, so every feature that lets the model act is switched off by flag (shell, exec,
web search, sub-agents, images, apps, browser, computer use, plugins, hooks); measured on 0.153,
what is left cannot run a command, search, or spawn. Every command the model wants goes through
this runtime's handlers as LNDL. Without those flags a read-only sandbox still let the model read
this machine and search the web for the upstream fix: 34 commands and 850k input tokens in one
round. Usage comes from the `turn.completed` event, whose input count is handed back as the prompt
count; there is no cost on a subscription, so `total_cost_usd` stays None and the bench records the
spend as unknown, never as zero.

The CLI's own base instructions describe an agent with a shell in a read-only workspace; with the
tools off, a reasoning model reads that as a broken interface and, one turn in three at round one,
answers "unable to access repository tools" instead of writing LNDL. `model_instructions_file`
replaces those instructions with a chat framing and `include_environment_context=false` drops the
cwd and sandbox lines; measured on gpt-5.6-luna, the round-one prompt falls from 7.4k to 3.6k tokens
and the refusals stop (0 of 12 round-one samples; 4 of 12 before). The shared `~/.codex` home
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

from lionagi.actor import Message, Reply

from .claude_code import _clipped, _transcript

__all__ = (
    "TRANSIENT",
    "DISABLED",
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
)
INSTRUCTIONS = (  # replaces the CLI's base instructions, which describe an agent with tools
    "You are a language model completing a conversation. The conversation arrives as tagged blocks "
    "(USER, ASSISTANT, SYSTEM); the latest messages define the format your reply must take. Reply "
    "only in that format, with nothing before or after it.\n"
)
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
    calls: list[dict] = field(default_factory=list)  # per call: usage, timing, model, events of note

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
            "agents.max_depth=0",  # the only thing that removes spawn_agent; the feature flag does not
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
            "include_skills_usage_instructions=false",
            "-c",
            "include_plugin_usage_instructions=false",
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
        # this call's own directory: its retries reuse it, and it goes once the child is joined
        scratch = Path(tempfile.mkdtemp(prefix="lionagi-codex-"))
        answered = 0  # attempts that came back, billed whether or not they answered
        try:
            (scratch / "AGENTS.md").write_text(SCRATCH_AGENTS_MD)
            (scratch / "instructions.md").write_text(INSTRUCTIONS)
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

    async def _attempt(self, prompt: bytes, scratch: Path) -> tuple[str | None, dict, str, list[str], dict]:
        """One `codex exec` in `scratch`: (text or None, usage, why None, notes, the raw stream and
        stderr when the child answered)."""
        last = scratch / "last.txt"
        last.unlink(missing_ok=True)
        env = dict(os.environ)
        if self.home:
            env["CODEX_HOME"] = self.home
        proc = await asyncio.create_subprocess_exec(
            *self.argv(scratch),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(scratch),
            env=env,
            start_new_session=True,  # the CLI's whole process group: a kill ends its children too
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt), timeout=self.timeout)
        except (TimeoutError, asyncio.CancelledError) as e:
            # the child is this backend's to stop: a cancelled task must not leave it running, nor
            # a grandchild holding the pipes open past the loop's end
            # the group, whether or not its leader has exited: a child the leader left behind can
            # hold the pipes open, and the drain below would wait on it with no deadline
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            await proc.communicate()  # drains and closes the pipes the cancelled read left open
            if isinstance(e, asyncio.CancelledError):
                raise
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
            ):  # should never happen with DISABLED in force; the record shows it if it does
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
