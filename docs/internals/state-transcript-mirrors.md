# How transcript mirroring turns external CLI logs into StateDB rows

Two external coding CLIs — Claude Code and Codex — each write their own
transcript format to disk as a conversation happens. Neither writes directly
into StateDB. Instead, a *mirror* (`claude_mirror.py`, `codex_mirror.py`)
tails the external format and turns it into ordinary lionagi messages,
sessions, and branches, so the rest of the system (Studio, status APIs, the
lifecycle machinery) can treat a mirrored CLI conversation exactly like a
conversation lionagi itself is running. `_mirror_common.py` holds everything
both mirrors share, since once a session id is known, status reconciliation
and lineage linking are identical regardless of which CLI produced it.

## Deterministic ids and idempotent writes

A mirror never decides whether a row already exists by asking the database
first. It derives every id — session id, branch id, message ids, progression
ids — deterministically from the external tool's own conversation/session
identifier (`session_db_id(uid)`, `_det(uid, "branch")`, etc.). Writing a
batch of records is then naturally idempotent: replaying the same rollout or
transcript segment through the mirror produces the same ids and the same
rows, rather than duplicates. This matters because both mirrors are designed
to be re-run over a file: a crash mid-import, a later sweep picking up new
lines appended to the same file, or a reconciliation pass are all just
another call with the same deterministic ids.

Codex rollouts specifically use an enveloped JSONL format
(`{type, timestamp, payload}` per line). Rollouts written before
2025-09-20 use an older flat format with no envelope and mirror nothing —
measured at 6 files out of 29,652 in the local corpus, not sampled. Such a
file still gets a session row carrying zero counts, because completeness
checking works by subtraction (see below) and a row is what there is to
subtract against.

## Completeness is a subtraction, not a self-report

Both mirrors track, per record/event type, how many records they *saw* in
the source file versus how many they actually *mirrored* into messages
(`RecordTally` in `codex_mirror.py` plays this role). The design choice is
deliberate: completeness is computed as `seen - mirrored`, a subtraction any
consumer can do later, rather than the importer narrating its own
completeness as it goes. A self-report goes stale silently the moment the
importer's behavior changes; two counters that disagree cannot. A record
that failed to parse at all is counted separately from one deliberately
skipped by type — collapsing the two into one number would hide exactly
where a corpus is damaged.

## Bounding content: preview plus a recoverable source pointer

Mirrored messages don't always store an external tool's raw content
verbatim. When a caller passes `event_sources` (the exact
`(byte_offset, byte_count, sha256)` of each source JSONL line) together with
`max_preview_chars`, `_mirror_common.bound_mirror_content` truncates the
message's content to a bounded preview and attaches a versioned pointer back
into the source file, stored in `messages.node_metadata.mirror_source`. The
full content is never lost — it's recoverable from the pointer — it's just
not duplicated into SQLite. `_bound_content` applies a different truncation
rule per message shape (instruction, assistant response, or an action
response/request split between a function-name preview and an
output/arguments preview), discriminated by which fields are present rather
than an exact key match, since `RoledMessage.to_dict(mode="db")`'s live shape
varies per instance even though the mirror only ever populates a fixed
subset. An unrecognized shape fails bounded rather than persisting unbounded:
it's replaced with a JSON preview and marked truncated.

Reading a mirrored row back (`resolve_mirrored_content`) reverses this: it
recovers the full content from the pointer, verifying every step, and
degrades to the stored preview with a stable `reason` on any mismatch. A
stale, moved, or rotated source file must never silently return content from
a different file than the one that produced the row. This reconstruction
path currently exists for tests and future integration — adapters aren't yet
wired to call it live.

## Attribution corrections after the fact

Both mirrors guess at a session's `project`/`name` from cheap signals (the
transcript's working directory, its first prompt) at create time, and never
revisit those fields once the row exists — a later sweep will not clobber
them. This creates a problem for one specific case: a flow escalation
retries a node on a higher-tier CLI engine as an in-session child op, and
that engine's own transcript gets mirrored independently, under a session id
the mirror has no way to connect back to the originating run. Once the child
op's branch reports that session id, the escalation call site calls
`link_escalation_session` to stamp the link and *overwrite* the mirror's
guessed `project`/`name`, since both are wrong for an escalation leg by
construction (its `cwd` is a scratch workspace, its first prompt is injected
guidance, not a task description). If the mirror hasn't created the session
row yet, `link_escalation_session` returns `False` and the caller is
expected to retry for a bounded window — which side writes first is an
unresolved race, not a bug.

`set_session_provenance` (in `db.py`) handles a related but distinct case for
`artifacts_path`: a mirrored session's artifact root (the transcript's own
`cwd`) is a weaker signal than a launcher-set root, so it's written via
`COALESCE` rather than a plain assignment, so a later, more precise write is
never clobbered by an earlier guess.

## The engine underneath: SQLite tuning and credential safety

`engine.py` builds the `AsyncEngine` both mirrors and the rest of `db.py`
write through. Two things worth knowing if you're calling into it directly:

- **`SQLITE_BUSY_TIMEOUT_MS`** controls how long a SQLite write waits on a
  contended lock before reporting "database is locked". The default (5000ms)
  is sized for the test suite, where a write that deliberately holds a lock
  should fail fast rather than wait out a production-grade timeout — and for
  years this was silently also the production value. It's read from
  `LIONAGI_SQLITE_BUSY_TIMEOUT_MS` so a daemon's launch config can raise it
  for a large, contended store while the test suite leaves it at the fast
  default. `announce_busy_timeout()` logs the effective value once per
  process, recomputed at call time (not cached at import) since the module
  attribute is writable and tests retune it.
- **Credential masking** (`mask_credentials`, `mask_db_url`) is a backstop
  for logging and error messages that might otherwise leak a database
  password. It's text-scanning, not URL-parsing, for the general case
  (`mask_credentials`), which means a password containing a literal `@` is
  masked only up to that character, and a bare secret argument outside a
  `user:secret@` URL shape isn't matched at all — both are under-masking,
  which is why this exists alongside other controls rather than instead of
  them. `mask_db_url` tries a structured `urlparse` pass first and falls
  back to the text scan for URLs it can't decompose, notably a schemeless
  string, which parses as a path rather than a URL and would otherwise
  return a password verbatim.

## Verifying what artifacts actually landed

`artifact_verifier.py` answers a narrower question than the mirrors: given a
recorded verification verdict (produced artifacts, a `checked_at`
timestamp), is that verdict still trustworthy, or might the files have
changed since? `stale_artifact_markers` never re-runs the original
pass/fail check — it only compares mtime and size (both from a single
`stat()` call) against what the verdict recorded, so it stays a cheap
read-time check rather than a full re-verify. Comparing size alongside mtime
narrows, but does not close, the false-negative window a bare mtime check
would have against a rewrite that happens to preserve both. It returns
`None` only when the check can't be performed at all (no artifacts root
configured, or a verdict recorded before this field existed) — callers use
that to surface an explicit "unknown" state rather than treating an
unchecked verdict as clean.
