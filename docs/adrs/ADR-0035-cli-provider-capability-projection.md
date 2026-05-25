# ADR-0035: Capability Projection for Agentic CLI Providers

**Status**: Proposed
**Date**: 2026-05-25
**Extends**: ADR-0033 (capability declarations), ADR-0034 (hook enforcement)
**Targets**: lionagi 0.27.0

## Context

ADR-0033 declares agent capabilities; ADR-0034 enforces them via existing
hooks. For tools registered to `ActionManager` (Python tools that lionagi
controls), the security_pre hook path is sufficient: the gate inspects args,
rewrites or raises.

But four agentic CLI providers execute tools **inside their own subprocess**:

- `claude_code` (Anthropic Claude Code CLI)
- `codex` (OpenAI Codex CLI)
- `gemini_code` (Google Gemini CLI)
- `pi` (Pi CLI — multi-provider)

For these, lionagi does not participate in the tool dispatch loop.
Request construction prior to subprocess spawn is the sole effective enforcement
point — once `asyncio.create_subprocess_exec` is called, governance is advisory
(observe the stream, optionally `os.killpg`).

An audit of the four provider request models (2026-05-25) found wildly
different permission surfaces. This ADR specifies the projection from lionagi
capabilities onto each provider's request kwargs.

## Decision

A `lionagi.governance.cli_projection` module maps a `CapabilitySet` to
provider-specific request kwargs. The projection fires at
`HookRegistry.PreEventCreate` (per ADR-0034 §3).

### Projection table

#### claude_code — full enforcement

`claude_code` has the richest surface: `permission_mode`, `allowed_tools`,
`disallowed_tools`, `mcp_config`, `permission_prompt_tool_name`.

| Capability | Projects to |
|---|---|
| `tool.bash` | `allowed_tools` includes `"Bash"`; without it: `disallowed_tools += ["Bash"]` |
| `tool.editor` | `allowed_tools` includes `"Edit", "Write", "MultiEdit"` |
| `tool.editor` constraint `paths: [...]` | `--allowedTools 'Edit(paths/**)'` syntax (Claude Code path scoping) |
| `tool.reader` | `allowed_tools` includes `"Read", "Glob", "Grep"` |
| `tool.search` | `allowed_tools` includes `"WebFetch", "WebSearch"` |
| `tool.sandbox` *not* present | `permission_mode = "plan"` (read-only mode) |
| `tool.editor` *not* present | `permission_mode = "plan"` |
| `network.http` *not* present | `disallowed_tools += ["WebFetch"]` |
| `tool.bash` constraint `exec: [...]` | `--allowedTools 'Bash(pytest *)'` per command |

Effective enforcement granularity: **full**. lionagi capabilities project
cleanly onto Claude Code's CLI flags.

#### codex — partial enforcement (sandbox-tier)

`codex` has structural sandbox levels rather than per-tool allowlists.

| Capability | Projects to |
|---|---|
| `tool.editor` present | `sandbox = "workspace-write"` |
| `tool.editor` absent | `sandbox = "read-only"` |
| `tool.bash` constraints with `confirm: true` | `ask_for_approval = "on-request"` |
| `tool.bash` without confirm | `ask_for_approval = "never"` + `full_auto = true` |
| `network.http` absent | (codex sandbox already blocks network in "read-only" / "workspace-write" — no extra flag needed) |
| **danger override** | `bypass_approvals = true` requires `tool.dangerous` capability explicitly |

Effective enforcement granularity: **partial** (sandbox-tier). Per-command
allowlists (`exec: ["pytest"]`) are not representable at request time;
enforcement for those constraints is stream-observation only.

#### pi — per-tool enforcement with pre-result abort window

`pi` has an explicit `tools: list[str]` allowlist passed to the CLI.

| Capability | Projects to |
|---|---|
| `tool.bash` | `tools.append("bash")` |
| `tool.editor` | `tools.append("edit")` |
| `tool.reader` | `tools.append("read")` |
| `tool.search` | `tools.append("web_search")` |
| (no tool caps at all) | `no_tools = True` |
| `network.http` absent | `no_extensions = True` (best approximation) |

Effective enforcement granularity: **per-tool allowlist** at request time.
Additionally, pi emits `tool_execution_start` before the tool result lands,
providing a narrow pre-result abort window in which stream observers may
invoke `os.killpg` on policy violation.

#### gemini_code — audit-only

`gemini_code` has only `yolo: bool` (binary) and `sandbox: bool`.

| Capability | Projects to |
|---|---|
| `tool.sandbox` *not* present (any restrictive cap) | `sandbox = true` |
| `tool.dangerous` present | `yolo = true` |
| (no projection for `tool.bash`/`tool.editor`/etc.) | — |

Effective enforcement granularity: **audit-only** for fine-grained policy.
Any tool-level capability specification emits a `policy_audit_only` log event
recording that gemini_code cannot enforce it pre-spawn. Stream-observed
`tool_use` events are logged for post-execution review.

### Implementation shape

```python
# lionagi/governance/cli_projection.py
class CLIProjector(Protocol):
    """Provider-specific projection from capabilities to request kwargs."""
    provider_name: str
    governable: Literal["full", "partial", "audit"]

    def project(self, caps: CapabilitySet, kw: dict) -> dict:
        """Return kw with permission flags set per the capability set."""

# Registered per provider (claude_code, codex, pi, gemini_code)
PROJECTORS: dict[str, CLIProjector] = {...}

# Wired in lionagi/governance/enforcement.py via PreEventCreate hook
async def project_caps_to_cli(event_type, **kw):
    branch = kw["branch"]
    projector = PROJECTORS.get(branch.chat_model.endpoint.config.provider)
    if projector is None:
        return  # API providers don't need this — handled by tool-call gate
    projector.project(branch.capabilities, kw)
```

### Stream observation (post-spawn audit + selective abort)

ADR-0034 §3 covers the enforcement hook. Post-spawn, the existing
`streaming_chunk` hook (already at `lionagi/service/hooks/hook_registry.py`)
is the audit point. The governance module subscribes to:

- `tool_use` chunks → log to audit
- For pi: `tool_use` arriving as `tool_execution_start` → check policy; raise
  to trigger `os.killpg` if disallowed
- For all: aggregate observed tool calls into the run-level audit record

## Consequences

**Positive**

- One declarative capability set drives enforcement across Python tools AND
  the four CLI providers — no per-provider config in agent profiles.
- The integration documents per-provider gaps explicitly: gemini_code is
  designated audit-only; codex is designated sandbox-tier only. The
  enforcement level is disclosed rather than implied.
- Existing `PROVIDER_YOLO_KWARGS` / `PROVIDER_BYPASS_KWARGS` plumbing
  (already in `lionagi/cli/_providers.py`) is the appropriate extension
  path — this ADR formalizes the values it sets and adds capability-driven
  mapping.

**Negative**

- Projection mapping is provider-specific code that drifts as CLI flags
  change. Each provider release potentially invalidates the table.
- gemini_code users receive weaker pre-spawn enforcement than claude_code
  users despite identical capability declarations. Documentation must
  explicitly disclose this asymmetry.
- Stream-observed enforcement for pi requires the governance hook to run
  fast (between `tool_execution_start` and the actual exec). Latency budget
  is tight.

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Don't enforce on CLI providers at all | Defeats the whole point of capabilities for the agentic flows lionagi pushes |
| Wrap each CLI provider with a lionagi-controlled tool dispatch | Massive scope; defeats the purpose of using vendor CLIs |
| Require all 4 providers to expose `allowedTools`-style flag | Out of lionagi's control; codex/gemini won't comply |
| Enforce only on claude_code (the rich surface) | Leaves users of other providers without enforcement or disclosure of the tradeoff |
| Stream-kill on every policy violation post-spawn | Subject to a race condition; the tool has typically executed before the kill signal can be delivered |

## References

- ADR-0033: Agent capability declarations
- ADR-0034: Hook-based governance enforcement
- `lionagi/providers/anthropic/claude_code/models.py:171-194`: claude_code
  permission fields (`permission_mode`, `allowed_tools`, `disallowed_tools`,
  `mcp_config`, `allow_dangerously_skip_permissions`)
- `lionagi/providers/openai/codex/models.py:140-155`: codex sandbox fields
  (`ask_for_approval`, `full_auto`, `sandbox`, `bypass_approvals`)
- `lionagi/providers/pi/cli/models.py:130-139, 712-721`: pi `tools` allowlist
  and `tool_execution_start` event
- `lionagi/providers/google/gemini_code/models.py:63-71`: gemini_code minimal
  surface (`yolo`, `sandbox`)
- `lionagi/cli/_providers.py:89-105`: existing `PROVIDER_YOLO_KWARGS` and
  `PROVIDER_BYPASS_KWARGS` — extended by this ADR
