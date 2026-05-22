# li agent

Spawn a single subagent and block until it returns a final response.

## Synopsis

```
li agent [model] <prompt> [options]
li agent -a <NAME> <prompt> [options]
li agent -r <BRANCH_ID> <prompt> [options]
li agent -c <prompt> [options]
```

## Description

`li agent` runs one subagent against a prompt and prints the result to stdout. The command blocks until the agent finishes (or times out) then exits with a status-coded exit code.

The agent can be:

- **One-shot** — a fresh conversation with any supported model.
- **Resumed** — continued from a previous branch by ID (`-r`) or from the most recent branch (`-c`).
- **Profile-driven** — loaded from an agent profile in `~/.lionagi/agents/` (`-a`).

## Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `model` | conditional | Model spec (`claude`, `codex`, `gemini-code`, `claude/opus`, etc.). Required unless `-a`/`--agent` supplies one or `--resume`/`--continue-last` is set. |
| `prompt` | yes | Prompt text sent to the subagent. |

## Flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `-a`, `--agent NAME` | string | — | Load agent profile by name. Resolves `~/.lionagi/agents/<NAME>/<NAME>.md` first, then `~/.lionagi/agents/<NAME>.md`. Profile provides system prompt, default model, effort, yolo. CLI flags override profile settings. |
| `-r`, `--resume BRANCH_ID` | string | — | Resume a previous branch by ID. Mutually exclusive with `-c`. |
| `-c`, `--continue-last` | flag | `false` | Continue the most recently used branch. Mutually exclusive with `-r`. |
| `--yolo` | flag | `false` | Auto-approve all tool calls. |
| `--bypass` | flag | `false` | Bypass codex approvals and sandbox. |
| `--fast` | flag | `false` | Route codex through OpenAI priority tier. |
| `-v`, `--verbose` | flag | `false` | Stream output in real time. |
| `--theme {light,dark}` | string | — | Terminal color theme. |
| `--effort LEVEL` | string | — | Reasoning effort override. `claude`: `low\|medium\|high\|xhigh\|max`. `codex`: `none\|minimal\|low\|medium\|high\|xhigh`. |
| `--cwd DIR` | path | — | Working directory for tool calls. |
| `--timeout SECONDS` | int | — | Abort after N seconds. Exit code `124`. |
| `--invocation ID` | string | — | Parent invocation ID (from `li invoke start`). |

## Exit Codes

| Code | Status |
|------|--------|
| `0` | `completed` |
| `1` | `failed` |
| `124` | `timed_out` |
| `130` | `aborted` (Ctrl-C) |
| `143` | `cancelled` |

## Examples

=== "One-shot"

    ```bash
    # Simplest form — Claude with a prompt
    li agent claude "Summarize the CHANGELOG"

    # Explicit model spec with effort suffix
    li agent claude/opus:high "Review this architecture and flag risks"
    ```

=== "Resume"

    ```bash
    # Continue by branch ID (printed at end of each run)
    li agent -r abc123def "Follow up: what's the fix?"

    # Continue the most recent branch — no ID needed
    li agent -c "Actually, focus on the auth layer"
    ```

=== "Agent profile"

    ```bash
    # Load a named profile from ~/.lionagi/agents/reviewer/reviewer.md
    li agent -a reviewer "Review PR #42 for security issues"

    # Profile sets model + system prompt; override effort at the CLI
    li agent -a codex-agent --effort high "Refactor the rate-limiter module"
    ```

=== "Save artifacts"

    ```bash
    # --save writes output to a directory
    li agent claude "Write a migration guide" --save ./output/

    # Combine with --verbose to see streaming output
    li agent claude "Explain the session lifecycle" -v --save ./docs/
    ```

=== "Timeout / yolo"

    ```bash
    # Auto-approve all tool calls, abort after 2 minutes
    li agent claude --yolo --timeout 120 "Scaffold a new FastAPI service"

    # Bypass codex sandbox (codespace / CI environment)
    li agent codex --bypass "Run the test suite and summarize failures"
    ```

## Common Patterns

### Using agent profiles

An agent profile is a Markdown file with YAML frontmatter. Place it at:

```
~/.lionagi/agents/<NAME>/<NAME>.md   (preferred)
~/.lionagi/agents/<NAME>.md          (flat fallback)
```

Minimal profile:

```yaml
---
model: claude/opus
effort: high
yolo: false
---
You are a senior code reviewer. Focus on correctness and security.
Ignore style issues unless they introduce bugs.
```

CLI flags override any profile setting:

```bash
li agent -a reviewer --effort low "Quick scan for obvious issues"
```

### Resuming conversations

Every `li agent` run prints a branch ID on completion. Store it to continue later:

```bash
BRANCH=$(li agent claude "Start planning the auth rewrite" | tail -1)
li agent -r "$BRANCH" "Now draft the migration steps"
```

Or use `-c` to automatically continue the last branch without tracking IDs.

### Integrating with `li invoke`

Group multiple `li agent` calls under a single skill invocation for Studio visibility:

```bash
INV=$(li invoke start --skill my-review-skill --prompt "Review PR #99")
li agent -a reviewer --invocation "$INV" "Review the diff at path/to/diff.patch"
li invoke end "$INV" --status completed
```
