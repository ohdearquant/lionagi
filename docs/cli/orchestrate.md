# li orchestrate (li o)

Multi-agent orchestration. Two patterns are available:

- **`li o fanout`** — decompose a task into N parallel workers, then optionally synthesize their outputs.
- **`li o flow`** — give the orchestrator a prompt; it plans a DAG and executes it phase by phase.

`orchestrate` has the alias `o`. All examples use the short form.

---

## li o fanout {#fanout}

### Synopsis

```
li o fanout [model] <prompt> [options]
li o fanout -a <PROFILE> <prompt> [options]
```

### Description

Three-phase execution:

1. **Decompose** — the orchestrator breaks the prompt into N sub-tasks.
2. **Execute** — workers run in parallel (up to `--max-concurrent` at once).
3. **Synthesize** _(optional)_ — a synthesis agent merges worker outputs into a final answer.

### Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `model` | conditional | Orchestrator model spec. Also used as default worker model unless `--workers` is set. Optional when `-a` provides a model. |
| `prompt` | yes | Task for the orchestrator to decompose. |

### Flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `-a`, `--agent NAME` | string | — | Orchestrator profile. Profile provides system prompt, default model, effort, yolo. CLI flags override. |
| `-n`, `--num-workers N` | int | `3` | Number of workers. Ignored when `--workers` is set. |
| `--workers M1,M2,...` | string | — | Comma-separated worker model specs (e.g. `claude,codex,claude/opus`). Overrides `-n`. |
| `--max-concurrent N` | int | `0` | Max parallel workers. `0` = all. |
| `--with-synthesis [MODEL]` | optional string | `false` | Enable synthesis step. Bare flag uses the orchestrator model; pass a model spec to use a different one. |
| `--synthesis-prompt TEXT` | string | — | Custom instruction for the synthesis agent. |
| `--output {text,json}` | choice | `text` | Output format. |
| `--save DIR` | path | — | Save all worker + synthesis outputs to a directory. |
| `--team-mode [NAME]` | optional string | — | Create a persistent team channel for this fanout. Bare flag uses `fanout` as name. |
| `--yolo` | flag | `false` | Auto-approve all tool calls. |
| `--bypass` | flag | `false` | Bypass codex approvals/sandbox. |
| `--fast` | flag | `false` | Codex priority service tier. |
| `-v`, `--verbose` | flag | `false` | Stream real-time output. |
| `--theme {light,dark}` | string | — | Terminal color theme. |
| `--effort LEVEL` | string | — | Reasoning effort override. |
| `--cwd DIR` | path | — | Working directory for tool calls. |
| `--timeout SECONDS` | int | — | Abort after N seconds. |
| `--invocation ID` | string | — | Parent invocation ID. |

### Examples

=== "Simple fanout"

    ```bash
    # 3 Claude workers (default), no synthesis
    li o fanout claude "Audit the codebase for security vulnerabilities"

    # 5 workers
    li o fanout claude -n 5 "Write unit tests for every public function in src/"
    ```

=== "Mixed workers"

    ```bash
    # Different model per worker
    li o fanout claude --workers claude/opus,codex,gemini-code \
      "Evaluate these three database schemas and recommend the best one"

    # Cap concurrency to 2 at a time
    li o fanout claude --workers claude,claude,claude,claude \
      --max-concurrent 2 "Translate these four chapters to French"
    ```

=== "With synthesis"

    ```bash
    # Synthesis using the orchestrator model
    li o fanout claude -n 4 --with-synthesis \
      "Research competing auth frameworks and pick the best one"

    # Synthesis using a stronger model
    li o fanout claude -n 3 --with-synthesis claude/opus \
      --synthesis-prompt "Produce a concise executive summary of the findings" \
      "Analyse Q3 revenue by region"
    ```

=== "Save outputs"

    ```bash
    li o fanout claude -n 5 --save ./research/ \
      "Deep-dive into five potential product features"

    # JSON output for programmatic consumption
    li o fanout claude -n 3 --output json --save ./out/ \
      "Extract action items from these meeting notes"
    ```

=== "Team coordination"

    ```bash
    # Create a named team so workers can coordinate via li team send/receive
    li o fanout claude -n 3 --team-mode sprint-review \
      "Review all open PRs in the repo"
    ```

---

## li o flow {#flow}

### Synopsis

```
li o flow [model] [prompt] [options]
li o flow -f <spec.yaml> [options]
li o flow -p <PLAYBOOK> [playbook-args...] [options]
```

### Description

The orchestrator receives the prompt, plans a directed acyclic graph (DAG) of operations, then executes it phase by phase. Each operation is assigned to an agent; dependencies between operations are respected. The engine handles topological sorting and phase-parallel execution.

`-f` (file) and `-p` (playbook) are mutually exclusive. `--team-mode` and `--team-attach` are mutually exclusive. `--background` requires `--save`.

### Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `model` | conditional | Orchestrator model spec. Optional when `-a` or a flow spec/playbook provides one. |
| `prompt` | conditional | Task for the orchestrator. Optional when `-f`/`-p` embeds the prompt. |

### Flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `-f`, `--file PATH` | path | — | Load flow spec from YAML or JSON. File values are defaults; CLI flags override. Mutually exclusive with `-p`. |
| `-p`, `--playbook NAME` | string | — | Load from `~/.lionagi/playbooks/<NAME>.playbook.yaml`. Dynamic args are injected as flags. Mutually exclusive with `-f`. |
| `-a`, `--agent NAME` | string | — | Orchestrator profile from `~/.lionagi/agents/`. |
| `--with-synthesis [MODEL]` | optional string | `false` | Synthesis step after all ops complete. |
| `--max-concurrent N` | int | `0` | Max agents running in a single phase. `0` = all. |
| `--output {text,json}` | choice | `text` | Output format. |
| `--save DIR` | path | — | Save all outputs to a directory. Required for `--background`. |
| `--team-mode [NAME]` | optional string | — | Create a fresh team for this flow. Default name: `flow`. Mutually exclusive with `--team-attach`. |
| `--team-attach NAME` | string | — | Attach to an existing team (upsert). Mutually exclusive with `--team-mode`. |
| `--dry-run` | flag | `false` | Plan the DAG but do not execute. Prints agents, operations, and model assignments. |
| `--show-graph` | flag | `false` | Render DAG as a matplotlib visualization. With `--save`, writes a PNG. |
| `--background` | flag | `false` | Run in background. Requires `--save`. |
| `--bare` | flag | `false` | Ignore agent profiles; all workers use the CLI model spec. |
| `--max-ops N` | int | `0` | Cap total operations. `0` = unlimited. |
| `--max-agents N` | int | `0` | Deprecated alias for `--max-ops`. Prefer `--max-ops`. |
| `--yolo` | flag | `false` | Auto-approve all tool calls. |
| `--bypass` | flag | `false` | Bypass codex approvals/sandbox. |
| `--fast` | flag | `false` | Codex priority tier. |
| `-v`, `--verbose` | flag | `false` | Stream real-time output. |
| `--theme {light,dark}` | string | — | Terminal color theme. |
| `--effort LEVEL` | string | — | Reasoning effort override. |
| `--cwd DIR` | path | — | Working directory for tool calls. |
| `--timeout SECONDS` | int | — | Abort after N seconds. |
| `--invocation ID` | string | — | Parent invocation ID. |

### Examples

=== "Simple flow"

    ```bash
    # Orchestrator plans a DAG; workers execute
    li o flow claude "Implement a JWT auth layer for the FastAPI app"

    # See the plan before running anything
    li o flow claude --dry-run "Migrate the database schema to v2"
    ```

=== "From file"

    ```bash
    # Load a pre-written flow spec
    li o flow -f .khive/workspaces/20260522/auth-flow.yaml

    # Override the model at CLI; file is defaults only
    li o flow claude/opus -f specs/migration.yaml
    ```

=== "From playbook"

    ```bash
    # Run a saved playbook (resolves ~/.lionagi/playbooks/codex-review.playbook.yaml)
    li o flow -p codex-review

    # Playbooks may declare dynamic args
    li o flow -p release-prep --version 2.1.0 --target main
    ```

=== "DAG visualization"

    ```bash
    # Show the graph in a matplotlib window
    li o flow claude --show-graph "Refactor the billing service"

    # Save graph PNG alongside outputs
    li o flow claude --dry-run --show-graph --save ./plan/ \
      "Design the new notification pipeline"
    ```

=== "Team coordination"

    ```bash
    # Create a fresh team for this run
    li o flow claude --team-mode "sprint-42" \
      "Plan and execute all tasks from this sprint board"

    # Attach workers to a pre-created team
    li team create sprint-42 -m orchestrator,worker1,worker2
    li o flow claude --team-attach sprint-42 "Continue yesterday's sprint"
    ```

=== "Background + synthesis"

    ```bash
    # Run in background; results written to ./out/
    li o flow claude --save ./out/ --background \
      "Generate comprehensive docs for all public APIs"

    # Same, with synthesis summary
    li o flow claude --save ./out/ --background --with-synthesis claude/opus \
      "Audit all microservices for security issues"
    ```

---

## li play {#play}

`li play` is sugar for `li o flow -p`. It resolves playbook names automatically.

### Synopsis

```
li play <NAME> [playbook-args...]
li play list
li play <NAME> --help
```

### Description

`li play NAME` rewrites to `li o flow -p NAME ...` before argparse runs. The playbook's declared `args:` are injected as typed CLI flags.

Playbooks live at `~/.lionagi/playbooks/<NAME>.playbook.yaml`.

### Examples

```bash
# List installed playbooks
li play list

# Run a playbook
li play codex-review

# Run with declared playbook args
li play release-prep --version 2.1.0

# Print playbook description and declared args without running
li play codex-review --help
```

### Playbook YAML Shape

```yaml
name: release-prep
description: "Prepare a release: changelog, version bump, tag."
args:
  version:
    type: str
    help: "Semver string, e.g. 2.1.0"
  target:
    type: str
    default: main
    help: "Branch to release from"
prompt: |
  Prepare release {version} from branch {target}.
  Steps: update CHANGELOG, bump version files, create release tag.
```

Prompt placeholders `{version}` are interpolated from declared args or the positional `prompt` argument.

---

## Playbook Integration

Both `li o flow -p` and `li play` inject the playbook's `args:` as typed CLI flags before argparse runs, so `--version 2.1.0` is parsed as a string and `--dry-run` as a bool (if declared `type: bool`).

If a playbook does not declare explicit `args:`, an `argument-hint:` fallback is used: `--flag VALUE` parses as string, bare `--flag` parses as bool.

Flags that collide with built-in `li o flow` flags are silently skipped — playbooks cannot override core CLI behavior.
