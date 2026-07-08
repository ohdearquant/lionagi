"""Agent-backend abstraction (DESIGN_CONTRACT §3) — plug ANY agent into the bench.

Three adapters, one uniform contract: given a sandbox + instance + the repo's
working directory, produce a unified diff. Scoring never looks at how the diff
was produced — only ``sandbox.git_diff(workdir)`` after the adapter runs, so
lionagi's own agent, Claude Code, and codex are compared apples-to-apples.

``LionagiAdapter`` reuses the swebench suite's ``_sandbox_entry.py`` unmodified
(uploaded as-is, driven via the same spec.json contract) — no new in-sandbox
agent machinery. ``ClaudeCodeAdapter``/``CodexAdapter`` run their CLI whole
inside the sandbox; the CLIs must be baked into the snapshot image (image.py).

Ablation axis (forward-compat, not built in v0): every adapter accepts
``mcp_servers`` and ``system_prompt_extra`` so a future substrate ablation
(same brain/hands, khive-enabled vs bare) can flip them per arm without a
schema change. ``system_prompt_extra`` is fully wired now (folded into the
prompt every adapter sends — see ``prompt_envelope``). ``mcp_servers`` is
wired for ``ClaudeCodeAdapter`` (its CLI takes a JSON config file trivially);
for ``LionagiAdapter``/``CodexAdapter`` it is recorded but not yet connected
to a running MCP server, and passing it raises loudly rather than silently
dropping it — see each adapter's ``run`` for the specific gap.

After ``run()``, every adapter exposes ``last_usage`` (token counts, ``{}`` if
unavailable) and ``last_tool_calls`` (per-tool call counts, ``{}`` if
unavailable) — raw per-run efficiency signal for future repeat-class analysis.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Protocol

_HERE = Path(__file__).resolve().parent
_SWEBENCH_ENTRY = _HERE.parent / "swebench" / "_sandbox_entry.py"

for _p in (str(_HERE),):
    if _p not in sys.path:
        sys.path.insert(0, _p)


PROMPT_TAIL = (
    "\n\nRepository is at {workdir}. Fix the described issue. Modify code, not tests. "
    "Leave your changes in the working tree when you are done — do not revert them, "
    "and do not `git commit`; the harness reads the working-tree diff directly."
)


def prompt_envelope(task_text: str, workdir: str, system_prompt_extra: str | None = None) -> str:
    """The uniform prompt every adapter hands to its agent (DESIGN_CONTRACT §3).

    ``system_prompt_extra`` is the ablation-axis scaffolding text (e.g. flywheel
    instructions for a khive-enabled arm). It is folded into the same prompt
    string every adapter sends — none of the three backends here expose a
    separate "append to system prompt" seam without either an unsupported CLI
    flag or a swebench-file edit, so this is the honest, working substitute."""
    body = task_text.strip()
    if system_prompt_extra:
        body = system_prompt_extra.strip() + "\n\n" + body
    return body + PROMPT_TAIL.format(workdir=workdir)


class AgentAdapter(Protocol):
    """One agent backend. ``run`` returns the unified diff of its edits.

    After a call, implementations set ``last_usage: dict`` and
    ``last_tool_calls: dict`` (``{}`` when the backend can't report them)."""

    last_usage: dict
    last_tool_calls: dict

    async def run(self, sandbox, instance, workdir: str) -> str: ...


# Tool names the swebench in-sandbox entry's @@SIG@@ stream tags ActionRequests
# with — same set sandbox_runner.py counts (coding.py's DEFAULT_CODING_TOOLS).
_LIONAGI_TOOL_NAMES = ("reader", "editor", "bash", "search")


class LionagiAdapter:
    """Our in-sandbox lionagi coding agent, driven via swebench's _sandbox_entry.py."""

    def __init__(
        self,
        model: str,
        *,
        effort: str | None = None,
        max_extensions: int = 30,
        refine_rounds: int = 1,
        lion_system: bool = True,
        env: dict[str, str] | None = None,
        mcp_servers: dict | None = None,
        system_prompt_extra: str | None = None,
    ):
        if mcp_servers is not None:
            # AgentSpec/create_agent already support mcp_servers + mcp_config_path
            # (lionagi/agent/spec.py, factory.py:_load_mcp), but the unmodified
            # swebench _sandbox_entry.py builds its AgentSpec.coding(...) call
            # without ever reading spec["mcp_servers"] — the field would be a
            # silent no-op if we let it through. Fail loudly instead: wiring
            # this needs a lionbench-owned entry variant, out of v0 scope.
            raise NotImplementedError(
                "LionagiAdapter mcp_servers is not wired through the unmodified "
                "swebench _sandbox_entry.py yet; pass mcp_servers=None for v0."
            )
        self.model = model
        self.effort = effort
        self.max_extensions = max_extensions
        self.refine_rounds = refine_rounds
        self.lion_system = lion_system
        self.env = env or {}
        self.mcp_servers = mcp_servers
        self.system_prompt_extra = system_prompt_extra
        self.last_result: dict | None = None
        self.last_usage: dict = {}
        self.last_tool_calls: dict = {}

    async def run(self, sandbox, instance, workdir: str) -> str:
        home = await sandbox.home_dir()
        await sandbox.upload_file(_SWEBENCH_ENTRY, f"{home}/_sandbox_entry.py")
        instruction = prompt_envelope(instance.task_text, workdir, self.system_prompt_extra)
        spec = {
            "repo_path": workdir,
            "model": self.model,
            "effort": self.effort,
            "instruction": instruction,
            "max_extensions": self.max_extensions,
            "refine_rounds": self.refine_rounds,
            "lion_system": self.lion_system,
            "result_path": f"{home}/result.json",
            "control_path": f"{home}/control",
            "repro_path": f"{workdir}/_lionbench_repro.py",
            "branch_path": f"{home}/branch.json",
            "env": self.env,
        }
        await sandbox.write_text(json.dumps(spec), f"{home}/spec.json")

        tool_calls = dict.fromkeys(_LIONAGI_TOOL_NAMES, 0)
        buf = [""]

        def on_stdout(chunk: str) -> None:
            # Same @@SIG@@ line-buffer parse as swebench/sandbox_runner.py's
            # on_out — copied narrowly (tool-call counting only) so lionbench
            # doesn't depend on that runner's CLI-specific plumbing.
            buf[0] += chunk
            while "\n" in buf[0]:
                line, buf[0] = buf[0].split("\n", 1)
                if not line.startswith("@@SIG@@ "):
                    continue
                try:
                    obj = json.loads(line[8:])
                except Exception:  # noqa: S112 — skip malformed signal lines
                    continue
                fn = obj.get("fn")
                if fn in tool_calls:
                    tool_calls[fn] += 1

        await sandbox.exec_stream(
            f"python {home}/_sandbox_entry.py {home}/spec.json", on_stdout=on_stdout
        )
        try:
            self.last_result = json.loads(await sandbox.read_text(f"{home}/result.json"))
        except Exception:  # noqa: BLE001 — diff is still recomputed below regardless
            self.last_result = None
        self.last_usage = (self.last_result or {}).get("usage", {})
        self.last_tool_calls = tool_calls
        # Uniform harvesting: recompute from the working tree rather than trust
        # result.json's self-reported diff, so all three adapters score identically.
        return await sandbox.git_diff(workdir)


_UNSAFE_PROMPT_SUBSTITUTION_RE = re.compile(
    r"\$\([^)]*\{prompt_path\}[^)]*\)|`[^`]*\{prompt_path\}[^`]*`"
)


def _assert_file_mediated(template: str) -> None:
    """Refuse an ``invocation_template`` that expands ``{prompt_path}``'s file
    CONTENTS into the command line via shell command substitution (``$(...)``
    or backticks) — that reintroduces untrusted ``task_text`` into argv even
    though the Python-side command string only names the path. Applies to the
    class defaults too (self-check), not just caller overrides."""
    if _UNSAFE_PROMPT_SUBSTITUTION_RE.search(template):
        raise ValueError(
            "invocation_template expands {prompt_path} via shell command "
            "substitution ($(...) or backticks) — this puts untrusted prompt "
            "bytes into argv again. Reference {prompt_path} as a literal path "
            "only (e.g. stdin redirection: '< {prompt_path}'), never inside "
            "$(...) or `...`."
        )


class _CliHarnessAdapter:
    """Shared shape for external-CLI adapters: write the prompt to a file inside
    the sandbox, exec the CLI's non-interactive invocation, harvest via git_diff.

    Token/tool-call counts are not derivable from these CLIs' stdout without a
    per-CLI structured-output parser (out of v0 scope); ``last_usage`` and
    ``last_tool_calls`` are always ``{}`` here — an honest gap, not a silent
    zero standing in for a real count."""

    invocation_template: str
    env_keys: tuple[str, ...]
    mcp_flag_template: str | None = None  # e.g. "--mcp-config {mcp_config_path}"

    def __init__(
        self,
        *,
        invocation_template: str | None = None,
        env_keys: tuple[str, ...] | None = None,
        timeout: int = 1800,
        mcp_servers: dict | None = None,
        system_prompt_extra: str | None = None,
    ):
        if invocation_template is not None:
            _assert_file_mediated(invocation_template)
            self.invocation_template = invocation_template
        else:
            _assert_file_mediated(self.invocation_template)
        if env_keys is not None:
            self.env_keys = env_keys
        if mcp_servers is not None and self.mcp_flag_template is None:
            raise NotImplementedError(
                f"{type(self).__name__} has no trivial CLI flag for mcp_servers; "
                "pass mcp_servers=None for v0."
            )
        self.timeout = timeout
        self.mcp_servers = mcp_servers
        self.system_prompt_extra = system_prompt_extra
        self.last_stdout: str = ""
        self.last_exit_code: int | None = None
        self.last_usage: dict = {}
        self.last_tool_calls: dict = {}

    def _env(self) -> dict[str, str]:
        return {k: os.environ[k] for k in self.env_keys if k in os.environ}

    async def run(self, sandbox, instance, workdir: str) -> str:
        prompt = prompt_envelope(instance.task_text, workdir, self.system_prompt_extra)
        prompt_path = f"{workdir}/.lionbench_prompt.txt"
        await sandbox.write_text(prompt, prompt_path)
        # File-mediated only: invocation_template must reference {prompt_path} as a
        # literal path (e.g. via stdin redirection), never expand the prompt bytes
        # themselves into the command string. task_text is untrusted (it comes from
        # a PR/issue body); letting it reach argv means process-list exposure and an
        # argv-length failure mode. See each adapter's invocation_template.
        cmd = self.invocation_template.format(prompt_path=prompt_path, workdir=workdir)
        if self.mcp_servers is not None:
            mcp_config_path = f"{workdir}/.lionbench_mcp.json"
            await sandbox.write_text(json.dumps(self.mcp_servers), mcp_config_path)
            cmd += " " + self.mcp_flag_template.format(mcp_config_path=mcp_config_path)
        result = await sandbox.exec(cmd, cwd=workdir, env=self._env(), timeout=self.timeout)
        self.last_stdout = result.stdout
        self.last_exit_code = result.exit_code
        return await sandbox.git_diff(workdir)


class ClaudeCodeAdapter(_CliHarnessAdapter):
    """Claude Code CLI, non-interactive, whole-harness run inside the sandbox.

    The prompt is piped in via stdin redirection rather than expanded into argv —
    the executed command names only the trusted prompt path, never the prompt
    bytes themselves. The invocation is a config field with a documented default —
    verify the actual non-interactive flag names against `claude --help` in the
    target image before trusting this in a real run; don't assume training-data
    flags are still current."""

    invocation_template = "claude -p --dangerously-skip-permissions < {prompt_path}"
    env_keys = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
    mcp_flag_template = "--mcp-config {mcp_config_path}"


class CodexAdapter(_CliHarnessAdapter):
    """OpenAI codex CLI, non-interactive, whole-harness run inside the sandbox.

    Same stdin-redirection shape as ClaudeCodeAdapter, same caveat: verify
    `codex exec --help` in the image before a real run; `--full-auto` is the
    documented default for unattended approval, overridable via
    ``invocation_template``. codex reads MCP servers from its own config.toml
    rather than a per-invocation flag, so ``mcp_servers`` has no trivial wiring
    here — see ``mcp_flag_template=None``."""

    invocation_template = "codex exec --full-auto < {prompt_path}"
    env_keys = ("OPENAI_API_KEY",)
    mcp_flag_template = None


ADAPTER_REGISTRY = {
    "lionagi": LionagiAdapter,
    "claude": ClaudeCodeAdapter,
    "codex": CodexAdapter,
}
