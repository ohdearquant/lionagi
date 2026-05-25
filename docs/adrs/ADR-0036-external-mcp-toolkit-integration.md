# ADR-0036: External MCP Toolkit Integration — Provider-Native Config

**Status**: Proposed
**Date**: 2026-05-25
**Targets**: lionagi 0.27.0
**Relates to**: ADR-0033 (capabilities — gates discovered tools the same way as any other)

## Context

Earlier drafts of this ADR proposed shipping a lionagi-side wrapper module
that re-exposed external toolkit verbs (memory, knowledge graph, GTD, etc.)
as agent tools. That approach was wrong:

1. **External toolkits already ship their own MCP servers.** Re-wrapping their
   verbs in lionagi duplicates surface area and drifts as the upstream evolves.
2. **Every provider lionagi targets (Claude Code, codex, etc.) already has its
   own MCP / plugin configuration mechanism** — Claude Code's `.mcp.json`,
   codex's MCP config, and so on. Users wiring an external toolkit do it
   through the provider's native config path, not through a lionagi-specific
   layer.
3. **Lionagi already discovers MCP tools** via
   `ActionManager.register_mcp_server` (`lionagi/protocols/action/manager.py:256-310`)
   and `load_mcp_config` (line 386). Given a server config dict, it
   auto-discovers and registers all tools with full Pydantic validation. No
   new code needed.

The right integration is **no lionagi-side wrapper at all**: let users
configure external toolkits through their provider's existing MCP/plugin
config, and rely on lionagi's already-working MCP discovery + the ADR-0033
capability gates to govern the discovered tools.

## Decision

Lionagi **does not** ship a toolkit-specific integration module for any
external MCP-exposing service. The integration model is:

### 1. Provider-native config is the entry point

Users wire external MCP servers through whichever config mechanism their
provider already supports. For lionagi-managed branches running against
agentic CLI providers, the provider's MCP config is the canonical location.
For lionagi's direct API-provider branches, MCP servers are registered via
the existing `register_mcp_server` / `load_mcp_config` API:

```python
from lionagi import Branch

branch = Branch(name="researcher")
await branch.acts.register_mcp_server({"server": "memory-toolkit"})
# or: await branch.acts.load_mcp_config(Path(".mcp.json"))
```

The `server` name resolves against whatever MCP server config the
environment already has loaded — provider-native, project-local, or
user-global. Lionagi does not prescribe the format or location of the config
file.

### 2. No lionagi.integrations.{toolkit} modules

- No re-wrapping of external verbs as Python callables.
- No toolkit-specific `Branch.memory` / `Branch.kg` / etc. convenience
  surfaces. If a generic `Branch.memory` abstraction is later wanted, it
  lives in its own ADR.
- No `[toolkit-name]` optional extras in `pyproject.toml`. External
  toolkits are user-managed dependencies.

### 3. Capability gating still applies uniformly

Tools discovered from any MCP server pass through the same ActionManager
registration path. ADR-0034's `security_pre:{tool_name}` hook fires for them
identically to in-tree Python tools. So a capability like:

```yaml
# ~/.lionagi/agents/researcher.md
capabilities:
  - memory.recall
  - memory.write
  - kg.search
```

…gates the MCP-discovered tool of the same name without any toolkit-specific
code. Verbs that the user's capability set doesn't grant are simply not
registered as tools — the agent never sees them.

### 4. Documentation pointer

The 0.27.0 docs reference the existing `register_mcp_server` API and link to
the relevant provider's MCP config docs (Claude Code, codex, etc.). No new
lionagi documentation prescribes what an external toolkit's config should
look like — that's the provider's and the toolkit's responsibility.

## Consequences

**Positive**

- Zero new code in lionagi for this integration class. Smallest possible
  surface area.
- No drift between a lionagi wrapper and the upstream toolkit's actual API.
  When the toolkit ships a new verb, MCP discovery sees it on next attach.
- Same path works for any MCP-exposing toolkit — not just one. Generalization
  for free.
- Capability gating (ADR-0033/0034) covers MCP-discovered tools without
  toolkit-specific glue.
- Provider-native config keeps the integration boundary at the layer where
  it's already understood (Claude Code users already know about MCP config;
  codex users already know about their CLI's plugin config).

**Negative**

- Users have to know how to configure their provider's MCP loader. The
  cognitive load lives outside lionagi (already true for any MCP user).
- No typed `Branch.memory.recall(...)` convenience layer. `branch.acts.invoke("recall", ...)`
  is the canonical path — same as any other MCP tool.
- If users were hoping for a "pip install lionagi[X] and it works"
  experience, they get the more honest "wire it via your provider's MCP
  config and lionagi discovers it" experience instead.

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Ship a `lionagi.integrations.<toolkit>` Python module re-wrapping each verb | Duplicates verb surface; drifts as upstream evolves; rejected by Ocean |
| Ship a thin `Branch.memory` abstraction backed by MCP recall/remember | Premature abstraction — earns its own ADR if/when the pattern proves out across toolkits |
| Ship reference `.mcp.json` files in lionagi's `examples/` | Each provider has its own config format; reference files would drift; better to point at the provider's own docs |
| Auto-detect installed toolkits at Branch init and register their MCP servers | Magic; bad surprise when the server's not running; explicit `register_mcp_server` is fine |
| Require a `[mcp-toolkits]` optional extra that auto-discovers servers | Same magic problem; users opt in explicitly via config, not import-time side effects |

## References

- `lionagi/protocols/action/manager.py:256-310`: `register_mcp_server` —
  the existing path for MCP tool discovery + registration
- `lionagi/protocols/action/manager.py:386`: `load_mcp_config` — reads
  MCP server config from a dict or file
- `lionagi/service/connections/mcp_wrapper.py`: the MCP transport layer
  (stdio, http) — works with any MCP-compliant server
- ADR-0033: agent capability declarations — gates MCP-discovered tools
  identically to any other tool
- ADR-0034: hook-based governance enforcement — the security_pre hook fires
  on tools regardless of origin (in-tree Python, MCP-discovered, agentic-CLI)
