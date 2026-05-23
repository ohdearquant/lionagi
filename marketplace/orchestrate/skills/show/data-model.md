# Data Model

Shows and plays are first-class entities in `state.db` (SQLite).

## Tables

```
shows table:
  id, topic, goal, repo, base_branch, integration_branch
  status: active | completed | aborted | imported
  show_dir, created_at, updated_at

plays table:
  id, show_id, name, playbook, effort
  status: pending | prepared | running | running_complete |
          gated | gate_failed | redoing | merged |
          escalated | blocked | aborted_after_finish
  attempt (1 or 2), session_id, started_at, ended_at, exit_code
  worktree, branch, merge_sha, merged_at
  gate_passed, gate_feedback, depends_on (JSON array), sort_order
```

Source: `lionagi/state/schema.sql` line ~218.

## Status enums

### Show status

| Value | Meaning |
|---|---|
| `active` | Show is in progress — plays are running or pending |
| `completed` | Final gate passed; integration PR opened |
| `aborted` | Operator triggered abort; no more plays will launch |
| `imported` | Show was imported from an external show directory |

### Play status

| Value | Meaning |
|---|---|
| `pending` | Not yet started; waiting for deps to merge |
| `prepared` | Worktree and prompt files written; not yet fired |
| `running` | `li play` process is live (`.pid` present) |
| `running_complete` | Process exited; gate not yet run |
| `gated` | Gate ran and passed (synonym: about to merge) |
| `gate_failed` | Gate ran and failed; may redo |
| `redoing` | Attempt 2 is in progress |
| `merged` | Branch merged into integration; `merge_sha` recorded |
| `escalated` | Failed gate on attempt 2; human intervention needed |
| `blocked` | Dep failed or escalated; this play cannot proceed |
| `aborted_after_finish` | Show was aborted but the play had already completed |

## Studio pages

- `/shows` — list all shows with status, play count, last update
- `/shows/<topic>` — PlayDag component: dependency graph with per-play status colors
- Each play links to its session in `/runs`

The show directory is controlled by `LIONAGI_SHOWS_ROOT`. Set it to any path you prefer.
If unset, the skill uses `$HOME/.lionagi/shows` as its default.
