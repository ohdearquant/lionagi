# li team

Create and manage named team channels for agent coordination.

## Synopsis

```
li team create <name> -m <members>
li team list
li team ls
li team show <team>
li team send <content> -t <team> --to <recipients>
li team receive -t <team> [--as <member>]
```

## Description

Teams are lightweight coordination channels backed by JSON files in `~/.lionagi/teams/`. Any agent running inside `li o fanout --team-mode` or `li o flow --team-mode` can use `li team send` and `li team receive` to pass signals mid-run.

Teams are identified by name or UUID. All write operations use `fcntl.flock` for safe concurrent access.

## Subcommands

### create

Create a new team with named members.

```
li team create <name> -m <members>
```

| Argument / Flag | Required | Description |
|-----------------|----------|-------------|
| `name` | yes | Team name (positional). |
| `-m`, `--members MEMBERS` | yes | Comma-separated member names, e.g. `orchestrator,worker1,worker2`. |

---

### list / ls

List all teams stored in `~/.lionagi/teams/`.

```
li team list
li team ls
```

No flags.

---

### show

Print full team JSON including members and message log.

```
li team show <team>
```

| Argument | Required | Description |
|----------|----------|-------------|
| `team` | yes | Team ID or name. |

---

### send

Post a message to one or more members of a team.

```
li team send <content> -t <team> --to <recipients> [options]
```

| Argument / Flag | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `content` | yes | — | Message text (positional). |
| `-t`, `--team TEAM` | yes | — | Team ID or name. |
| `--to RECIPIENTS` | yes | — | `all` or comma-separated member names. |
| `--from SENDER` | no | `_cli` | Sender name. Defaults to `_cli` when omitted. |
| `--from-op OP_ID` | no | — | Op ID this message belongs to. Ties the coordination signal to a specific flow operation for traceability. |

---

### receive / recv

Read the inbox for a team member.

```
li team receive -t <team> [--as <member>]
li team recv   -t <team> [--as <member>]
```

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `-t`, `--team TEAM` | yes | — | Team ID or name. |
| `--as MEMBER` | no | — | Read messages addressed to this member. |

## Examples

=== "Create and inspect"

    ```bash
    # Create a team with three members
    li team create sprint-42 -m orchestrator,worker1,worker2

    # List all teams
    li team list

    # Inspect a team
    li team show sprint-42
    ```

=== "Send and receive"

    ```bash
    # Send a broadcast to all members
    li team send "Starting phase 2 — proceed with implementation" \
      -t sprint-42 --to all

    # Send a targeted message
    li team send "Your subtask is complete, pending review" \
      -t sprint-42 --to worker1 --from orchestrator

    # Read your inbox
    li team receive -t sprint-42 --as worker1
    ```

=== "Mid-flow coordination"

    ```bash
    # Inside a flow worker — signal the orchestrator that a subtask is done
    li team send "research complete, artifacts at ./out/research.md" \
      -t sprint-42 --to orchestrator --from worker1 --from-op op-research-1

    # Orchestrator polls its inbox before proceeding
    li team receive -t sprint-42 --as orchestrator
    ```

=== "With li o flow --team-mode"

    ```bash
    # Flow creates the team automatically
    li o flow claude --team-mode sprint-42 "Run the sprint"

    # Workers inside the flow can communicate via the team
    # The team persists after the flow ends for post-run inspection
    li team show sprint-42
    ```

## Storage Layout

Teams are stored as JSON files:

```
~/.lionagi/teams/
  <team-id>.json
```

Each JSON file contains the team name, member list, creation timestamp, and full message log. Reads are non-destructive — `receive` marks messages as read per member without deleting them.
