# li state

Manage the session state database (`state.db`).

## Synopsis

```
li state import
li state import-teams
li state ls [options]
li state stats
li state checkpoint [--mode MODE]
li state vacuum
li state prune [options]
li state doctor [options]
```

## Description

`li state` provides maintenance operations for the SQLite database that tracks every session, branch, and tool call. The database is populated automatically as agents run, but `import` and `import-teams` let you backfill data from existing on-disk run directories and team JSON files.

## Subcommands

### import

Backfill all run directories from `~/.lionagi/runs/` into the state database. Idempotent — safe to run multiple times.

```
li state import
```

No flags.

---

### import-teams

Backfill team JSON files from `~/.lionagi/teams/` into `teams` and `team_messages` tables. Idempotent.

```
li state import-teams
```

No flags.

---

### ls

List recent sessions.

```
li state ls [--limit N] [--status STATUS]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--limit N` | int | `50` | Maximum sessions to list. |
| `--status STATUS` | string | — | Filter by status: `running`, `completed`, `failed`, `aborted`. |

---

### stats

Print database health metrics: file size, WAL size, row counts per table, lifecycle status breakdown, and PRAGMA values.

```
li state stats
```

No flags.

---

### checkpoint

Force a WAL checkpoint to flush the write-ahead log into the main database file.

```
li state checkpoint [--mode MODE]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--mode {PASSIVE,FULL,RESTART,TRUNCATE}` | choice | `TRUNCATE` | Checkpoint mode. `TRUNCATE` shrinks the WAL file. |

---

### vacuum

Rebuild the database file to reclaim free pages and reduce size.

```
li state vacuum
```

No flags. May take several seconds on large databases.

---

### prune

Delete old sessions from the database.

```
li state prune [--keep-days N] [--keep-n N] [--dry-run]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--keep-days N` | int | `30` | Keep sessions updated within the last N days. |
| `--keep-n N` | int | `100` | Always keep the N most recent sessions regardless of age. |
| `--dry-run` | flag | `false` | Print what would be deleted without deleting. |

---

### doctor

Sweep stale `running` sessions — sessions still marked `running` that started more than `--stale-hours` ago — and update their status.

```
li state doctor [--stale-hours N] [--new-status STATUS] [--dry-run]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--stale-hours N` | int | `24` | Hours since `started_at` to consider a session stale. |
| `--new-status {aborted,failed}` | choice | `aborted` | Status to assign swept sessions. |
| `--dry-run` | flag | `false` | Print what would be swept without updating. |

## Examples

=== "Initial setup"

    ```bash
    # After first install, backfill existing runs
    li state import
    li state import-teams

    # Verify the import
    li state stats
    li state ls --limit 20
    ```

=== "Routine maintenance"

    ```bash
    # Check database health
    li state stats

    # Remove sessions older than 14 days, always keep the 50 most recent
    li state prune --keep-days 14 --keep-n 50

    # Compact the database
    li state vacuum
    li state checkpoint --mode TRUNCATE
    ```

=== "Recover stale sessions"

    ```bash
    # Preview what doctor would sweep
    li state doctor --stale-hours 12 --dry-run

    # Mark sessions stale for >6 hours as failed
    li state doctor --stale-hours 6 --new-status failed
    ```

=== "Filter sessions"

    ```bash
    # Show only failed sessions
    li state ls --status failed --limit 10

    # Show all running sessions
    li state ls --status running
    ```
