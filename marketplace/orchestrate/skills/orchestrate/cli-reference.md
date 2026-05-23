# CLI Reference

Complete flag tables for lionagi orchestration commands.

---

## `li agent [MODEL] PROMPT` — single agent

Spawn one blocking agent turn. Prints the final response to stdout.

```
li agent claude "Write unit tests for auth.py"
li agent claude/opus-4-6-high "Produce a security audit"
li agent -r <branch-id> "Follow-up question"
li agent -c "Continue the previous conversation"
```

| Flag | Default | Description |
|---|---|---|
| `MODEL` | (positional, optional) | Provider/model spec, e.g. `claude`, `codex`, `claude/opus-4-6-high` |
| `PROMPT` | (positional, required) | Task text |
| `-a / --agent NAME` | — | Load agent profile from `.lionagi/agents/<NAME>.md` |
| `-r / --resume BRANCH_ID` | — | Resume a previous branch by ID |
| `-c / --continue-last` | false | Continue the most recently used branch |
| `--yolo` | false | Auto-approve all tool calls |
| `--bypass` | false | Bypass all codex approvals and sandbox |
| `--effort LEVEL` | — | `low\|medium\|high\|xhigh\|max` (claude); `none\|minimal\|low\|medium\|high\|xhigh` (codex) |
| `--cwd DIR` | — | Working directory for CLI provider |
| `--timeout SECONDS` | — | Kill after N seconds |
| `--invocation ID` | — | Parent invocation id (from `li invoke start`) |
| `--project NAME` | — | Explicit project name; overrides auto-detection |
| `-v / --verbose` | false | Stream real-time output |
| `--theme light\|dark` | — | Terminal display theme |
| `--fast` | false | Codex priority service tier |

Exit codes: `0` completed, `1` failed, `124` timed out, `130` aborted (Ctrl-C), `143` cancelled.

---

## `li o fanout [MODEL] PROMPT` — parallel workers

Orchestrator decomposes the task into N subtasks, fans out to workers in parallel,
optionally synthesizes results.

```
li o fanout claude "Review this codebase for security issues" -n 4
li o fanout claude/sonnet "Suggest API design approaches" -n 3 \
    --with-synthesis claude/opus-4-6-high
```

| Flag | Default | Description |
|---|---|---|
| `MODEL` | (positional, optional) | Orchestrator model; also default worker model |
| `PROMPT` | (positional, required) | Task for the orchestrator to decompose |
| `-a / --agent NAME` | — | Load orchestrator profile |
| `-n / --num-workers N` | 3 | Number of workers (ignored if `--workers` set) |
| `--workers M1,M2,...` | — | Explicit comma-separated worker model specs |
| `--max-concurrent N` | 0 (all) | Max workers running at once |
| `--with-synthesis [MODEL]` | false | Enable synthesis. Bare flag uses orchestrator model |
| `--synthesis-prompt TEXT` | — | Custom synthesis instruction |
| `--save DIR` | — | Save all outputs to directory |
| `--team-mode [NAME]` | — | Create a team for inter-worker messaging |
| `--output text\|json` | text | Output format |
| `--yolo` | false | Auto-approve tool calls for all workers |
| `--bypass` | false | Bypass approvals for all workers |
| `--effort LEVEL` | — | Effort level for all workers |
| `--cwd DIR` | — | Working directory |
| `--timeout SECONDS` | — | Kill after N seconds |
| `--invocation ID` | — | Parent invocation id |
| `--project NAME` | — | Explicit project name |

---

## `li o flow [MODEL] [PROMPT]` — DAG orchestration

Orchestrator plans a DAG of agents with dependency edges, executes with automatic
parallelism where dependencies allow.

```
li o flow claude "Audit and harden the authentication module" \
    --with-synthesis --save ./audit-out --yolo --bypass
li o flow -f ./my-spec.yaml --yolo --bypass
li o flow -p security-audit "JWT middleware" --save ./out --yolo --bypass
```

| Flag | Default | Description |
|---|---|---|
| `MODEL` | (positional, optional) | Orchestrator model spec |
| `PROMPT` | (positional, optional) | Task; can come from spec file's `prompt:` |
| `-f / --file PATH` | — | Load flow spec from YAML/JSON. CLI flags override |
| `-p / --playbook NAME` | — | Load from `~/.lionagi/playbooks/<NAME>.playbook.yaml` |
| `-a / --agent NAME` | — | Load orchestrator profile |
| `--with-synthesis [MODEL]` | false | Final synthesis after all ops complete |
| `--max-concurrent N` | 0 (all) | Max agents running in parallel within a phase |
| `--save DIR` | — | Save outputs (required with `--background`) |
| `--team-mode [NAME]` | — | Fresh team per invocation |
| `--team-attach NAME` | — | Attach to existing team (mutually exclusive with `--team-mode`) |
| `--dry-run` | false | Plan DAG without executing |
| `--show-graph` | false | Render DAG visualization |
| `--background` | false | Fork into background subprocess (requires `--save`) |
| `--bare` | false | Ignore agent profiles; all workers use CLI model |
| `--max-ops N` | 0 (unlimited) | Cap total DAG nodes. `--max-agents` is deprecated alias |
| `--output text\|json` | text | Output format |

Plus all common flags (`--yolo`, `--bypass`, `--effort`, `--cwd`, `--timeout`, `--invocation`, `--project`).

---

## `li play NAME [PROMPT] [ARGS...]` — playbook sugar

Sugar for `li o flow -p NAME`. Playbooks at `~/.lionagi/playbooks/<NAME>.playbook.yaml`.

```
li play security-audit "Audit the JWT middleware"
li play list                     # list available playbooks
li play security-audit --help    # show playbook description and args
```

All `li o flow` flags work with `li play` (except `-p`).

---

## `li team` — persistent team messaging

```bash
li team create "my-team" -m "researcher,writer,reviewer"
li team list
li team show my-team
li team send "Found a critical bug" --team my-team --to all --from analyst
li team receive --team my-team --as reviewer
```

---

## `li invoke` — invocation tracking

```bash
INV=$(li invoke start --skill orchestrate --prompt "Full audit")
li o flow claude "..." --invocation "$INV" --yolo --bypass
li invoke end "$INV" --status completed
li invoke list --skill orchestrate --limit 10
```

Statuses: `completed`, `failed`, `timed_out`, `aborted`, `cancelled`.
