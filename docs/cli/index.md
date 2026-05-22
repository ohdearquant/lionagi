# CLI Reference

lionagi ships a first-class command-line interface called `li`. Every operation available through the Python API—running a single agent, fanning out parallel workers, building a DAG pipeline, managing team channels—is also available from your shell.

## Installation

```bash
pip install lionagi
# The `li` binary is registered as a package entry-point.
li --version
```

## Quick Reference

| Command | Description | Key Flags |
|---------|-------------|-----------|
| [`li agent`](agent.md) | Spawn a single blocking subagent | `-a`, `-r`, `-c`, `--effort`, `--yolo` |
| [`li o fanout`](orchestrate.md#fanout) | Parallel workers + optional synthesis | `-n`, `--workers`, `--with-synthesis` |
| [`li o flow`](orchestrate.md#flow) | Auto-DAG pipeline from a prompt or spec file | `-f`, `-p`, `--dry-run`, `--bare` |
| [`li play`](orchestrate.md#play) | Shortcut for `li o flow -p NAME` | dynamic playbook flags |
| [`li team`](team.md) | Create / send / receive on named team channels | `create`, `send`, `receive` |
| [`li studio`](studio.md) | Launch Lion Studio web UI | `--port`, `--host` |
| [`li state`](state.md) | Manage the session state database | `ls`, `stats`, `prune`, `doctor` |
| [`li invoke`](invoke.md) | Track skill-level orchestration records | `start`, `end`, `list` |
| `li skill` | Load and print installed skill bodies | `list`, `show`, `NAME` |

## Primitives Under `~/.lionagi/`

| Directory | Contents |
|-----------|----------|
| `~/.lionagi/agents/` | Agent profiles (`.md` files with YAML frontmatter) |
| `~/.lionagi/skills/` | Skill bodies (`.md` files, read by `li skill`) |
| `~/.lionagi/playbooks/` | Playbook specs (`<name>.playbook.yaml`) |
| `~/.lionagi/runs/` | Persisted run artifacts and branch snapshots |
| `~/.lionagi/teams/` | Team channel JSON files |

## Global Flags

These flags apply to every subcommand.

| Flag | Default | Description |
|------|---------|-------------|
| `--version` | — | Print version and exit. |
| `-h`, `--help` | — | Print command help and exit. |

## Common Flags

The following flags are available on `li agent`, `li o fanout`, and `li o flow`.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--yolo` | flag | `false` | Auto-approve all tool calls. |
| `--bypass` | flag | `false` | Bypass codex approvals and sandbox (for cloud/codespace environments). |
| `--fast` | flag | `false` | Route codex through OpenAI priority service tier (lower latency). Does not change model or effort. |
| `-v`, `--verbose` | flag | `false` | Stream real-time output to the terminal. |
| `--theme {light,dark}` | string | — | Terminal color theme. |
| `--effort LEVEL` | string | — | Override reasoning effort. `claude`: `low\|medium\|high\|xhigh\|max`. `codex`: `none\|minimal\|low\|medium\|high\|xhigh`. |
| `--cwd DIR` | path | — | Working directory for CLI tool calls. |
| `--timeout SECONDS` | int | — | Abort after this many seconds. Exit code `124`. |
| `--invocation ID` | string | — | Parent invocation ID from `li invoke start`. Groups this session under a skill orchestration record. |

## Model Spec Syntax

Most commands accept a `model` positional argument. Accepted forms:

```
claude              # bare alias → latest Claude (claude-code backend)
codex               # bare alias → OpenAI Codex
gemini-code         # bare alias → Gemini 2.5 Pro
claude/opus         # provider/model
claude/opus:high    # provider/model:effort suffix
```

Full alias table is in `lionagi/cli/_providers.py::BACKENDS`.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `LIONAGI_HOME` | Override `~/.lionagi` base directory. |
| `LIONAGI_RUN_ID` | Pre-set run ID instead of auto-generating one. |
| `LIONAGI_STUDIO_PORT` | Default port for `li studio` (fallback: `8765`). |
| `ANTHROPIC_API_KEY` | API key for Claude models. |
| `OPENAI_API_KEY` | API key for Codex/OpenAI models. |
| `GOOGLE_API_KEY` | API key for Gemini models. |

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | `completed` |
| `1` | `failed` |
| `124` | `timed_out` |
| `130` | `aborted` (Ctrl-C / SIGINT) |
| `143` | `cancelled` (orchestrator / system) |

## Subcommand Pages

- [li agent](agent.md)
- [li o fanout / li o flow](orchestrate.md)
- [li team](team.md)
- [li studio](studio.md)
- [li state](state.md)
- [li invoke](invoke.md)
