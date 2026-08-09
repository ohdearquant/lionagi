# Data Model

Shows and plays are first-class entities in `state.db` (SQLite).

## Tables

```
shows table:
  id, topic, goal, repo, base_branch, integration_branch
  status: active | completed | aborted | imported
  show_dir, status_source, created_at, updated_at

plays table:
  id, show_id, name, playbook, effort
  status: pending | prepared | running | running_complete |
          gated | gate_failed | redoing | merged |
          escalated | blocked | aborted_after_finish
  attempt (1 or 2), session_id, started_at, ended_at, exit_code
  worktree, branch, merge_sha, merged_at
  gate_passed, gate_feedback, depends_on (JSON array), sort_order
  created_at, updated_at
```

Source: the `CREATE TABLE shows` and `CREATE TABLE plays` declarations in
`lionagi/state/schema.sql`.

## Status enums

### Show status

| Value | Meaning |
|---|---|
| `active` | Show is in progress — plays are running or pending |
| `completed` | Final verdict passed, or every imported play is already merged |
| `aborted` | Operator triggered abort; no more plays will launch |
| `imported` | Legacy state value retained for previously imported records |

### Play status

| Value | Meaning |
|---|---|
| `pending` | Not yet started; waiting for deps to merge |
| `prepared` | Worktree and prompt files written; not yet fired |
| `running` | The play's run is live (`job.status` for its `run_id` reports it non-terminal) |
| `running_complete` | Process exited; gate not yet run |
| `gated` | Gate ran and passed (synonym: about to merge) |
| `gate_failed` | Gate ran and failed; may redo |
| `redoing` | Attempt 2 is in progress |
| `merged` | Branch merged into integration; `merge_sha` recorded |
| `escalated` | Failed gate on attempt 2; human intervention needed |
| `blocked` | Dep failed or escalated; this play cannot proceed |
| `aborted_after_finish` | Show was aborted but the play had already completed |

## Reading a show from Studio

Studio's backend serves shows over `/api/shows` — list, detail, import and an SSE watcher —
and that is the interface to use.

**There is no dedicated Studio page for shows.** The retired `/shows` route redirects to
Fleet, and while a `PlayDag` component exists in the source tree nothing imports it, so there
is no show dependency graph to watch in the UI. The `_show.md` file and the per-play
`_meta.json` and `_verdict.json` are the readable state, and `job.status`, `job.output` and
`job.wait` are how you follow a play that is running.

The show directory is controlled by `LIONAGI_SHOWS_ROOT`. Set it explicitly, and use the same
value in both your shell and Studio's environment.
