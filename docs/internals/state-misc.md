# Session display names, and telling reclaimed or stale data from real data

Two small, unrelated corners of the state package are documented here
together because each is short enough that a document per file would be
mostly white space: how a session gets the name shown to a person, and how
the system tells "this data is missing because it was never there" apart
from "this data is missing because something happened to it."

(The schema/migration story, StateDB locking, and the transcript mirrors are
covered in `state-db.md` and `state-transcript-mirrors.md`. The lifecycle
transition machinery — `state/lifecycle/`, `transitions.py`, `reasons.py`,
`schema_migrations.py`, `health.py`, `completion_evidence.py` — already has
a thorough writeup in `runtime.md`; nothing here duplicates it.)

## How a session's display name is chosen

`session_naming.py` is a pure-transform module: no randomness, no database
reads, so a row's resolved name is stable across re-reads and cheap enough to
compute per row on a paginated list. It's shared between the write path
(transcript-mirror ingestion) and the read path (Studio's API), so both sides
agree on what "prompt-shaped" and "a sane display width" mean.

`resolve_display_name` walks a fixed priority chain: an explicit user label,
then a show/play name, then a playbook name, then an agent-role descriptor,
then a sanitized prompt-derived name, and finally a short id as the
unconditional fallback. `user_label` has no write path anywhere in the
codebase yet — it's read defensively via `.get()` so a future rename feature
can slot into the top of the chain without another reorder.

The agent-role tier (`agent_role_label`) formats as `"claude-code · 1167 ·
14:22"`: engine name, four characters of the row's own id, and a UTC
HH:MM timestamp. The id slice earns its place empirically — name and minute
alone looked sufficient until the common real case (several long-lived
sessions of one engine) turned a page into near-identical strings like
`"claude-code · 21:37"`, `"· 21:49"`, `"· 21:50"` that a viewer has to
compare digit by digit. Four meaningless-looking characters turn a row into
something a person can actually point at.

The prompt-derived tier (`sanitize_prompt_name`) strips a leading
system-message banner, a markdown separator or heading it wraps, and a short
`"Label:"` prefix, repeating the strip since these nest (a `"Guidance:"`
wrapper around a system-message block needs two passes to fully unwrap). It
also has to handle a prompt that was routed through a YAML document and so
kept its block-scalar indicator (`|` or `|-`) ahead of everything else,
which would otherwise defeat every other strip pattern. The function returns
`None`, never `""`, when there's no usable name left — both cases (empty
input, or a banner-only input that strips to nothing) collapse to the same
falsy value on purpose, so every `if sanitized:` caller keeps working
unchanged, but `None` reads unambiguously as "nothing to show" rather than
being confused with a real empty-string name.

The prompt tier also declines a stored `name` that's a known placeholder —
`"agent"`, `"session"`, `"flow"`, `"codex session"` — values written by a
caller that had nothing more specific to record, not real content. Skipping
these by comparing the literal value (rather than by reordering the priority
chain) means rows with a real stored name are entirely unaffected; only rows
carrying one of these exact placeholders fall through to the short-id
fallback instead of rendering a page of identical cards.

## Telling absent data from lost data

Three small modules answer a related question from different angles: when a
piece of session data is missing, is that because it was never collected, or
because something removed or invalidated it after the fact? Conflating the
two is the actual bug each one guards against.

**`content_pruned.py`** handles reclaimed message bodies. Freeing space by
simply emptying a message's `content` column would collapse two different
truths into one: a body written as `""` or `{}` is exactly what a turn that
genuinely produced nothing writes, so a reader handed an empty body can't
tell "nothing happened here" from "something happened here and was later
discarded." Instead, `li state null-content` replaces a reclaimed body with
a small JSON marker (`{"lion_content_pruned": {"at": ..., "original_bytes":
...}}`) that says plainly what it is. The size recorded is deliberately a
per-row SQL expression (`pruned_content_sql`) evaluated against the row it's
overwriting, not a single number computed once in Python and stamped onto
every row in a batch — that would make `original_bytes` a batch average
wearing a per-row name, technically present but answering a different
question than its label claims. `content_was_pruned` reads the marker back;
it accepts the column either as raw JSON text or as an already-parsed dict,
since both shapes reach different callers, and treats anything unparseable
as "not reclaimed" without asserting anything about whether the body is
otherwise well-formed.

**`staleness.py`** answers a liveness question, not a presence question: is
a session that's still marked `"running"` actually stuck? The threshold is
kind-aware (`STALE_THRESHOLDS`, keyed by `invocation_kind`) because
multi-agent kinds like `flow` and `fanout` genuinely go quiet for longer
between visible activity than a single `agent` session does — a flat
threshold would either false-positive on healthy multi-agent runs or miss
genuinely stuck single-agent ones. `staleness_check` only ever returns
`"stale"` or `None`; terminal sessions are never flagged, since staleness is
a property of something still claiming to be running.

**`provenance.py`** writes the attribution columns (`model`, `provider`,
`agent_hash`) at session creation. `agent_definition_hash` fingerprints the
actual agent-profile file a session was launched from (a truncated SHA-256),
searching every configured lionagi directory in order and returning `None`
if no matching profile file is found — a session created without a
resolvable profile simply carries no hash rather than a fabricated one.
`resolve_model_spec` normalizes `(provider, model)` into a single
`"provider/model"` string for storage, passing an already-qualified model
name (one that already contains a `/`) through unchanged rather than
double-prefixing it.
