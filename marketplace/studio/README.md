# studio

Lion Studio dashboard — orchestration observability for lionagi runs, agents, playbooks, and shows.

## Purpose

Studio is the director's primary surface for inspecting and controlling lionagi orchestration in real-time. It exposes live process trees, team file state, cost and time observability, resume state, abort sentinels, and log streaming — all aimed at surfacing what the file substrate hides.

## Feature Roadmap

| Feature | Description |
|---------|-------------|
| F1 | Exit-code reconciliation panel |
| F2 | Verdict JSON parse and schema validation |
| F3 | Gate context expander for working artifacts and repo deliverables |
| F4 | Team file state inspector |
| F5 | Live `_show.md` editor and decisions log |
| F6 | Worktree status badge per play |
| F7 | Show-level cost and time observable |
| F8 | Resume state inspector |
| F9 | Abort sentinel control |
| F10 | Per-play log streaming |
| F11 | Remote URL and repo redirect detector |
| F12 | Live process tree per play |

## Usage

### Start the backend (available now)

```bash
li studio start
# or
uv run li studio start
```

### MCP integration (pending)

`li studio mcp` is not yet implemented. When ready, it will expose lion-studio backend routes as MCP tools for CC agent consumption. See ADR-0003.

## Architecture References

- **ADR-0001**: Internal monorepo structure
- **ADR-0002**: Lift heavy runtime dependencies
- **ADR-0003**: Marketplace plugin registry

## Install

```bash
uv pip install 'lionagi[studio]'
```

The MCP server entry in `plugin.json` is a TODO stub. Replace once `li studio mcp` is implemented.
