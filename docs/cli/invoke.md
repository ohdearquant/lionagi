# li invoke

Track skill-level orchestration records.

## Synopsis

```
li invoke start --skill <NAME> [options]
li invoke end <invocation-id> [options]
li invoke list [options]
```

## Description

`li invoke` implements ADR-0020 skill-orchestration tracking. A skill invocation groups multiple `li agent` or `li o flow` sessions under a single record, making them visible together on the Studio `/invocations` page.

Workflow:

1. `li invoke start` — opens an invocation, prints the invocation ID.
2. Pass `--invocation <ID>` to any `li agent`, `li o fanout`, or `li o flow` call.
3. `li invoke end <ID>` — closes the invocation with a terminal status.

## Subcommands

### start

Open a new invocation and print its ID to stdout.

```
li invoke start --skill <NAME> [--plugin PLUGIN] [--prompt PROMPT] [--metadata JSON]
```

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--skill SKILL` | yes | — | Skill name, e.g. `show`, `codex-pr-review`, `reprompt`. |
| `--plugin PLUGIN` | no | — | Marketplace plugin that packages the skill. |
| `--prompt PROMPT` | no | — | User input that triggered the skill. Stored for display. |
| `--metadata JSON` | no | — | JSON string to attach verbatim to the invocation record. |

The invocation ID is a 12-character hex string (`uuid4().hex[:12]`). The initial status is `running`.

---

### end

Close an existing invocation.

```
li invoke end <invocation-id> [--status STATUS] [--metadata JSON]
```

| Argument / Flag | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `invocation-id` | yes | — | ID printed by `li invoke start`. |
| `--status STATUS` | no | `completed` | Terminal status: `completed`, `failed`, `timed_out`, `aborted`, `cancelled`. |
| `--metadata JSON` | no | — | JSON string to merge into the invocation record. |

---

### list

List invocation records.

```
li invoke list [--skill SKILL] [--status STATUS] [--limit N]
```

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--skill SKILL` | no | — | Filter by skill name. |
| `--status STATUS` | no | — | Filter by status. |
| `--limit N` | no | `20` | Maximum rows to print. |

## Examples

=== "Basic tracking"

    ```bash
    # Open an invocation
    INV=$(li invoke start --skill my-review --prompt "Review PR #42")
    echo "Invocation: $INV"

    # Run agents under it
    li agent -a reviewer --invocation "$INV" "Review the security changes in PR #42"
    li agent claude --invocation "$INV" "Summarize the test coverage delta"

    # Close the invocation
    li invoke end "$INV" --status completed
    ```

=== "With a flow"

    ```bash
    INV=$(li invoke start --skill release-prep --prompt "Release 2.1.0")

    li o flow claude --invocation "$INV" \
      "Prepare release 2.1.0: changelog, version bump, tag"

    li invoke end "$INV"
    ```

=== "Error handling"

    ```bash
    INV=$(li invoke start --skill build-validator)

    li o fanout claude -n 3 --invocation "$INV" "Validate all CI checks" || {
      li invoke end "$INV" --status failed --metadata '{"reason":"fanout error"}'
      exit 1
    }

    li invoke end "$INV" --status completed
    ```

=== "Query invocations"

    ```bash
    # All recent invocations
    li invoke list

    # Failed invocations for a specific skill
    li invoke list --skill codex-pr-review --status failed --limit 5
    ```

## Notes

- Invocation IDs are short (12 hex chars) and safe to embed in shell variables.
- The `--invocation` flag is available on `li agent`, `li o fanout`, and `li o flow`.
- Invocations are stored in the state database alongside sessions; `li studio` surfaces them at `/invocations`.
