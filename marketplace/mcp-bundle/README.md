# mcp-bundle

Canonical lionagi MCP server access for agents — bundles the contract for how external CC agents discover and invoke lionagi's tool ecosystem.

## Purpose

`mcp-bundle` defines the integration point between Claude Code agents and lionagi's tool layer. Once `li mcp serve` is implemented, this plugin will allow any CC agent to call lionagi tools (branches, sessions, imodels, flows) directly over the MCP protocol.

## Planned Shape

```json
{
  "type": "stdio",
  "command": "uv",
  "args": ["run", "li", "mcp", "serve"],
  "installHint": "uv pip install lionagi"
}
```

## Status

`li mcp serve` is not yet registered in `lionagi/cli/main.py`. The bridge module at `lionagi/service/connections/mcp_wrapper.py` provides the underlying mechanism:

- `MCPSecurityConfig` — security configuration
- `MCPConnectionPool` — connection lifecycle management
- `create_mcp_tool` — tool registration bridge

A future play will wire these into a `li mcp serve` entrypoint and update `plugin.json`.

## Install

```bash
uv pip install lionagi
```

No MCP server entry is shipped until `li mcp serve` is implemented.
