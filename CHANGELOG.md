
# Changelog

All notable changes to lionagi are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Changed

- Public-surface differential capture now crosses one shared clock boundary for
  the full CLI case batch, and terminal-callback offload tests release their
  worker explicitly instead of leaving a 30-second sleeper behind.
- ADR-0120 Phase 0 now freezes the distinct HookBus, Broadcaster,
  SessionObserver, message-callback, scheduler-signal, and terminal-callback
  dispatch profiles before any shared-kernel migration. Service-hook invocation
  and streaming are characterized alongside them, but that profile's matrix is
  frozen in Phase 1, not here.
- Sentinel identity is now three-state by default: the public `is_sentinel` and
  `not_sentinel` helpers reject legacy `None`/empty collapse flags, and
  `Params`/`DataClass` subclasses may enable those flags only through the
  closed ADR-0119 compatibility inventory. Existing built-in compatibility
  adapters keep their prior omission behavior. Third-party callers that set
  either flag must replace it with an explicit domain adapter.
- Resuming a flow whose checkpoint recorded any operation as failed now refuses
  and names those operations, instead of replaying them as terminal state.
  Replaying a failed operation as terminal marks it failed without running it,
  so the executor skipped it and everything downstream, and the resume finished
  nothing. `--retry-failed` on `li o flow` (and `retry_failed` on the Studio
  resume request and the Operator `resume_run` command) runs them again; it is
  an opt-in because re-running re-executes whatever side effects the first
  attempt already had. Reactive children recorded under a re-running operation
  are dropped so the new attempt derives its own.
- The notify hook's delivery timeout is derived from the handler budget, so a
  delivery now fits inside the deadline that would otherwise terminate it.
- A delivery stopped part way reports `notify_delivery_state` as `unknown`
  rather than `failed`. Whether the notice already went out is exactly what
  such a delivery cannot say, and `failed` tells a caller to send it again.
  The value appears on every job listing row, so callers switching on
  `failed` should handle `unknown` as well.
- Delivery descendants are terminated through the shared process-group helper
  on POSIX. Cross-platform descendant containment remains out of scope and is
  tracked in #2576.

### Fixed

- `Params` subclasses now receive their declared dataclass defaults, including a fresh value
  from each `default_factory`, instead of replacing every omitted field with `Unset`. `Params`
  and `DataClass` now discover inherited instance fields through one ordered dataclass path,
  exclude `ClassVar` declarations, expose immutable `allowed()` membership views, and preserve
  `Unset`/explicit-null in-memory state across `with_updates()` instead of round-tripping it
  through the omission-oriented wire projection.
- `Operable` selection and legacy `OperableModel` materialization now retain declaration order
  for every supported membership collection and across hash seeds. Multiple unnamed `Spec`
  values no longer collide; concrete duplicate names still fail, and name-required Pydantic
  materialization now rejects unnamed fields instead of silently dropping them. Legacy
  `FieldModel` materialization uses the owning field key rather than emitting a second field.
  An explicit empty `include` or `use_fields` collection now selects no fields, unknown
  exclusions fail instead of being ignored, and `Operable.allowed()` returns an immutable
  membership view.
- Writable StateDB migration now fails closed when a table's columns cannot be
  inspected, preserving the prior schema-version stamp instead of recording an
  upgrade whose additive column reconciliation did not complete.

### Deprecated

- `--resume-on-timeout` on `li o fanout` and `li o flow`. The flag is accepted
  and listed in the help output of both commands but neither ever read it, so
  passing it changes nothing. Both commands now say so at parse time instead of
  accepting it silently, and will keep accepting it until it is removed in a
  later release. It remains available and functional on `li agent`, which is
  the surface that implements it.

## [0.35.0] - 2026-08-12

A broad correctness release across the orchestration engine, the MCP surface,
CLI providers, and Studio, plus the first Rust workspace in the tree.

### Added

- The run detail page opens on its execution graph, the canvas shows live
  per-node activity, and nodes are drawn at the ranks the rank function
  assigns.
- Run details show total token usage (input and output) for agents and
  orchestrations.
- Team coordination verbs are exposed over MCP.
- Operator: fleet inspection and a run-detail read tool, with the execution
  root resolved once at startup from a shipped default.
- The Rust tree opens: a `lionrs` workspace importing lion-core 0.4.0, with
  workspace checks gated in CI.
- CLI runs record what an agent did, bounded and without quoting it.
- Fleet's live list separates orchestrations from single agents.

### Fixed

- Orchestration engine: a failed fanout leg reads as a failure at every
  surface; refused reactive spawns are surfaced and zero spawn capacity is
  honored; escalation retries get unique references; assignment-shaped
  synthesis is rejected; planner assignment contracts are delivered to
  workers; emission repair failures are isolated to the op that failed; a
  resumed run no longer reports a permanently-failed op as completed; a
  gate-skipped node is announced as skipped, not failed; op budgets are sized
  by sequential depth and dated from when their timeout starts.
- MCP: detached agents are grounded against undeliverable notification waits
  instead of waiting forever; declared and effective server sets are
  distinguished; unselected servers are rejected before connection; terminal
  notification attempt state is retained; degraded persistence carries its
  reason.
- Prompt admission: agent and scheduled prompts are bounded by the shared
  character cap — inline, from files (reads capped at the bound), and after
  schedule template rendering — with the cap disclosed in the MCP schema.
- Codex CLI provider: emits `--sandbox workspace-write` in place of the
  removed `--full-auto` flag, preserves approval modes via config, and grants
  linked worktree git stores.
- Agent runtime: MCP reachability is reported accurately, zero-rate peers no
  longer skew heartbeat stall thresholds, and routing-pack role models are
  honored.
- Studio: Operator run targeting, terminal DAG reconciliation, and extra MCP
  grants; terminal runs no longer show a `healthy` badge; runs without a
  project can be renamed and cancelled, and an exact run id passes the
  project fence; cron schedules report an overdue health state; branch
  duration is measured from lifecycle timestamps; the playbook editor
  preserves canonical fields it does not edit; engine-definition writes
  reject unsupported stage overrides with a 422 instead of silently dropping
  them; the dev proxy routes to the backend the CLI selected; a schedule
  request that times out is reported as a timeout with unknown completion,
  never as an unreachable daemon.
- CLI: `li kill` confirms process identity before signalling, playbook specs
  are validated before job creation (unknown fields rejected, real fields
  accepted), and the daemon check reads the readiness endpoint so a serving
  daemon stops reading as down.
- Service layer: a vendor model id ending in an effort word is kept whole,
  and the Opus line accepts `xhigh` instead of silently downgrading it.
- State: mirrored provider refusals are recorded as failed, and an imported
  session's role field no longer receives an engine label.
- Protocols: Progression's permutation mutators are synchronized.
- ReAct: the first analysis round receives the branch's tools.

### Security

- Raised the `gitpython` floor to 3.1.58.
- The Operator no longer leaks credentials spelled with separators, and both
  redaction layers read a field name the same way.

### Changed

- Inline prose across the tree was trimmed to working density, with rationale
  relocated to `docs/internals/`.
- Orchestration documentation explains playbooks, plans, and operations as
  three distinct tiers, and ships play, fanout, and flow skills.

## [0.34.1] - 2026-08-07

0.34.0 was tagged but never published to PyPI: its release build stopped on a
public-surface contract test, so this is the first 0.34.x on PyPI. The library
and Studio changes are the same as 0.34.0; only the fix below is new.

### Fixed

- Recorded the `GET /api/shows/gated-plays` endpoint in the public HTTP surface
  baseline. The endpoint shipped in 0.34.0, but the frozen route baseline and
  its expected route count were left at the previous 134 routes, so the surface
  gate failed against the live 135. The baseline now matches the shipped app:
  one route added, none removed or changed, and no schema changes.
- Stopped the HTTP surface gate from comparing the application's own version
  string, which every release bump changes by design. It described the build
  rather than the shape of the surface, so it coupled two unrelated things: a
  release that changed no route still had to carry a regenerated baseline, and
  that regenerated diff was indistinguishable from a real surface change. The
  route set, schemas, and title are still compared.

## [0.34.0] - 2026-08-07

A Studio-heavy release. Most of the work is in the local Studio daemon and its
surfaces, plus a set of correctness fixes in state persistence, the scheduler,
and run reporting.

### Security

- Bumped `js-yaml` to 4.3.1 for GHSA-5p4m-2wfm-xmqj, where `!!omap` resolves in
  quadratic time so a small crafted document can consume disproportionate CPU.
  The dependency is development-only and transitive, reached through frontend
  tooling; it is not part of the published Python package.

### Added

- Studio gains Teams, Engine runs, Definitions, and Attention spaces, plus an
  admin action log.
- Skills are now first-class editable definitions.
- Operator: run inspection, cancellation, and resume with strict ownership
  scoping; one conversation is now one branch; sessions can be renamed; the
  Operator reads where the human is now rather than where they were.
- A needs-attention discharge lifecycle with durable dispositions.
- Aggregate cost visibility across fleet and history, and a fleet project
  filter with server-backed counts.
- Schedule card health derived from run evidence.
- Sensitive fields are redacted from library and agent definition reads.
- `job.status` over MCP now exposes execution-graph, artifact-contract, and
  stall detail.
- Clean review verdicts now carry verification evidence.

### Fixed

- Execution graphs render readably instead of collapsing into a vertical
  strip, with honest edge reduction, edge-aware folding, and a fixed node card.
  An operation's lifecycle signals stay under one name.
- Run state reporting matches what actually happened: flows no longer report
  success when terminal nodes never ran, dependents of a rejected gate node are
  short-circuited, and default run names reflect what a run actually is.
- Cost that was never reported is surfaced as unreported rather than counted as
  zero spend. Cache tokens and whole-tree CLI usage are counted, and retries no
  longer duplicate creation POSTs.
- State persistence: plain reads no longer reserve the writer lock, teardown no
  longer claims an unrecorded status, `insert_artifact`'s natural-key upsert is
  atomic, and invocation linkage is backfilled on a resumed session.
- Scheduler keeps the tick unblocked, admits ad-hoc work through the global
  limit, and reconciles dispatched orphans.
- CLI providers survive stream reconnects and orphan-held pipes.
- Escalation-leg CLI transcripts link to their parent run.
- Four correctness gaps closed in Progression membership, graph conformance,
  marketplace validation, and reservation rollback.
- Assorted Studio fixes: continuing a conversation from a finished session no
  longer 405s, the definitions save route validates agent role and mode like
  the agents API, MCP server args and env validate independently of transport,
  artifact verification is labelled with when it was taken, Operator failure
  messages point at evidence that exists, and the dev-mode Vite URL resolves
  honestly across bind failures and LAN hosts.

### Changed

- Public surfaces are registered through an auto-registration seam
  (`lionagi/_auto.py`), with CLI declaration markers across 17 command modules
  and the casts CLI and HTTP surfaces consolidated into
  `lionagi/casts/surfaces.py`.
- `Progression` membership is now O(1), with lazy cold imports.
- State hot-path index, bounded mirror ingestion, and safe chunked retention.
- A frontend consistency pass across the main Studio surfaces.
- A contract-oracle test suite pins the public CLI and HTTP surfaces against
  committed baselines. Captured fixtures are redacted, keep host-specific state
  out of the repository, and normalize interpreter-version-dependent argparse
  rendering so a baseline cannot be broken by a CPython patch release.

## [0.33.0] - 2026-08-04

### Upgrading from 0.32.0

**There was no 0.32.1 release.** A version bump to 0.32.1 was committed on
2026-07-31 and never tagged or published, so 0.32.0 remained the latest release
on PyPI while the repository read 0.32.1 for four days. The fix it documented
ships here, in Fixed below, and no `0.32.1` section is retained: a changelog
heading for a version that never existed on PyPI is a false record. If you
installed from a git checkout during that window you were running code that
reported itself as 0.32.1; nothing else about it differs from what is released
here.

Separately, `v0.23.3` has been tagged retroactively. That release was published
to PyPI on 2026-05-03 with no corresponding git tag. The tag was created only
after verifying that all 314 Python files in the published sdist are
byte-identical to the commit it names, and its tag message records that it was
applied on 2026-08-04 rather than at release time.

Four changes are visible to existing callers.

**A Claude Code session that ends in error now raises on the streaming path.**
Previously the terminal error flag was recorded on the session object and read
by nothing on the stream, so a session that failed after producing partial text
returned that text as an ordinary result, indistinguishable from success. The
stream now yields an error chunk, which is treated as a real failure
downstream: already-delivered text is flushed and then a classified provider
error is raised through the run, collect and operate paths. If you were relying
on the partial return, that is the change. The non-streaming path is unchanged,
and `ReAct`'s final-answer turn still returns partial text because it catches
broadly, which predates this and is not altered.

**A denied tool permission is now its own error type.** A headless CLI cannot
prompt for a tool permission, so an ungranted tool call is denied and the turn
returns empty. That used to arrive as a bare `ProviderError`, which is also
what an unclassified provider failure arrives as, and the two demand opposite
responses: one is worth retrying, the other reproduces exactly and forever.
`ProviderPermissionError` carries `retryable = False`. Callers branching on
`ProviderError` still catch it.

**Operator controls are admitted by declared drain capability rather than by
run ownership.** A runner now declares whether it consumes operator controls
when it starts a session, and admission asks for that declaration. Absence of a
declaration is refused, which does refuse one case that used to be admitted and
would have worked: a CLI agent run whose session row predates the declaration.
That is deliberate, because no field on a pre-existing row says what its runner
will do. Controls already queued are not rewritten.

**A store URL with credentials and no scheme is now refused.** A value like
`user:secret@db.internal/lionagi` has no `://`, so it used to resolve as a
filesystem path: a SQLite file appeared at a path built out of the credential,
the server it named was never contacted, and every command downstream reported
on an empty store as though it were the store. It is refused rather than
warned about. The message names the target host, carries no part of the
credential, and names the `./` prefix for a path that really does look like
this.

Also removed: `wal_pending` from the `db_health` response and the Studio System
panel. It returned a second copy of `wal_bytes`, and a field named for pending
WAL frames cannot be answered by the WAL file's size — SQLite leaves a
checkpointed WAL at its allocated size, so a fully checkpointed store reported
a large pending count forever, in the alarming direction. It is dropped rather
than populated because the only source for the number is `PRAGMA
wal_checkpoint`, which does not read the pending count, it performs a
checkpoint, turning a health read into a writer against the store it reports
on.

### Added

#### Studio

- **Agent create and delete**, with role and mode validated against the cast
  catalog and the validated value being the value stored. The system agent
  cannot be edited or deleted and the default agent cannot be deleted, enforced
  on the existing `lion_system` flag rather than a second notion of "system",
  so the guard and the runtime cannot disagree about what a system agent is.
- **MCP servers as a first-class Library resource**: register, edit, enable or
  disable, remove, and test a connection, with the registry kept in step with
  the standard `.mcp.json` that CLIs read. The registry and the generated
  `.mcp.json` are written with private permissions since they hold secret env
  values. Env values are never returned to the client, so a save merges onto
  the stored config key by key and deleting a displayed key is an explicit null
  rather than an omission. A stored connection result always describes the
  configuration it was obtained for: the check re-reads the registry after
  probing and writes its result only if the configuration is still the one it
  probed. Switching a server between stdio and http drops every field belonging
  to the transport being left.
- **Rename, pin, archive, and fork for Operator conversations.** They could be
  created but never managed.
- **Search, project scoping, and cursor-paged history for the fleet session
  list.** Conversation scrollback pages backward by the stable cursor the
  backend already returned; the previous offset paging repeated or skipped rows
  on a live session.
- **A stable creation-ordered mission board** with a bounded table view. Cards
  reshuffled while being read, because the board inherited `ORDER BY
  updated_at DESC` and `updated_at` bumps on every message while the page
  polls.
- **The Operator model catalog is served by the backend**, so the picker offers
  exactly the providers, models, and effort levels the daemon can run instead
  of a hardcoded frontend list that could disagree with it. A selection pins
  provider and model as one pair on the conversation.
- **Optional periodic all-thread stack dump**, off unless
  `LIONAGI_STUDIO_TRACEBACK_DUMP` names a path. A daemon that stops answering
  on one route is asking a question only a stack can answer, and the usual
  instrument needs root on macOS, which makes it unavailable at exactly the
  moment the process is wedged. This answers it from inside the process with no
  attach and no privileges. It refuses out loud, naming the reason, for a path
  inside a git working tree, an unwritable path, or an unusable interval.

#### CLI and orchestration

- **`li o ctl msg <id> "text"` now steers a running `li agent` leg**, draining
  at the next turn boundary as a warm continuation turn rather than a context
  merge. Targets resolve by run id as well as session, invocation, play or
  branch id. Delivery is at most once. Whether a claimed message reached the
  model is not recoverable from anything the system keeps, so a claim whose
  consumer died is left standing rather than guessed at: `li agent status`
  shows the owner and the age, and `li o ctl resolve <id> --as
  applied|abandoned [--by <who>]` is how a person who has found out closes it.
  The command is deliberately absent from the MCP surface, where a machine
  caller has exactly the knowledge the row is missing.
- **`li state null-content --older-than DAYS [--role ROLE] [--dry-run]`**
  reclaims message bodies on the axis the bytes are actually on. `li state
  prune` selects sessions, and a message any surviving progression still names
  is kept whatever its age, so a store can be almost entirely message content,
  have every message inside the keep-window, and give the prune nothing to
  delete. Measured read-only on a 6.8 GiB store of 1,685,541 messages: at the
  prune's default `--keep-days 30` it reaches zero bytes of message content,
  while 3.06 GiB of bodies are older than seven days. The row, its id, role,
  timestamp, sender and every progression reference survive; only the body
  goes, and reclaiming is not reversible. A reclaimed body is replaced by a
  marker rather than emptied, because an empty body is exactly what a turn that
  genuinely produced nothing writes and nothing downstream has a second source
  for that distinction. The command is excluded from the MCP surface as an
  irreversible write.
- **`li state prune` and `li state stats` report what a retention window can
  actually reach**, including a per-role and per-age breakdown of message
  bodies, so the gap between what the window selects and where the bytes live
  is visible before running anything.
- **Manifest schema v1 loader** for declarative round fan-out, per ADR-0110.
  Unknown keys are refused by name at every mapping level and `manifest_version`
  is required as the exact integer, using a strict YAML loader that refuses
  merge keys, duplicate mapping keys and unhashable keys at construction, with
  a matching JSON duplicate-key hook — so the schema validates what the author
  wrote rather than what the parser produced. Every brief is read once through
  a single descriptor with `fstat` binding the device and inode identity to the
  bytes actually read.
- **A manifest round is proven quiet before anything publishes it complete.**
  Finishing used to mean the legs had returned, and a leg that returned can
  still have left a process group running, so "complete" was a statement about
  the runner's bookkeeping rather than about the machine. Quiescence is
  established against the process groups the round actually recorded, with
  durable per-leg and per-round records written as the round happens.
- **A caller can hand lionagi the identity of a CLI process it started
  itself**, instead of that identity being available only to the code that
  spawned it. Cancelling such a leg ends the whole process group, identified by
  evidence both ways it can be established rather than assumed from the leader
  pid.
- **`secrets.lookup` in `~/.lionagi/settings.yaml`** resolves declared secrets
  into a spawned CLI leg's environment from a keychain or vault, for the case
  where a CLI provider authenticates from its child's own environment and the
  value is not exported. It is wired at the single spawn seam shared by all four
  CLI providers. Global settings only: a repository must not get to name the
  program that reads this machine's secret store. Purely additive, an exported
  value always wins, and the value reaches the child's environment and nowhere
  else — never a file, never an argv, never a log line.
- **A failed job record now carries why**, not just that it failed. The
  exception class that ended the run reaches the record as `failure_cause`. The
  message is deliberately not carried: provider messages quote whatever the
  provider said, which routinely includes an API key, and copying that into a
  file the observer verbs read and forward opens a second channel onto the same
  secret. A run whose cause file is absent leaves the field off entirely, so a
  caller can tell a run that reported no typed cause from one that predates the
  field.

#### Configuration

- **`LIONAGI_SQLITE_BUSY_TIMEOUT_MS`** makes the SQLite busy timeout a
  deployment property. The 5000ms constant was chosen so tests holding a write
  lock fail fast, and it was also, with nothing deciding it, the production
  value on every connection any process opens. Unusable values fall back to the
  default with a warning naming the rejected value; `busy_timeout=0` means
  never wait, which no deployment that bothered to set the variable wants.
- **Each process says once which busy timeout it is actually using and where
  that came from** — the built-in default, the environment variable, an
  unusable value, or an in-process override. Previously the only way to find
  out was to read the config that launched the process, from outside it.

#### CI

- A **CVE audit gate** exporting the fully-pinned runtime dependency closure and
  failing on any advisory, with a second non-blocking job over the wider dev and
  extras closure that distinguishes "audited and clean" from "did not
  complete".
- **Import guards** on the merge-blocking aggregate. They previously ran in
  their own workflow, so `import lionagi` staying light and free of eager heavy
  imports was measured on every PR and blocked none of them.
- A **frontend formatting gate**, and a **durable 14-day traffic ledger** on its
  own long-lived data branch, since GitHub retains that data for only 14 days.

### Changed

- **`codex/<name>` names a codex config profile.** A profile at
  `$CODEX_HOME/<name>.config.toml` names a model and the provider that serves
  it, and is how the codex CLI reaches models from other vendors. The name used
  to be passed to codex as a model id, so the profile was never consulted and
  codex ran something else — silently, since the leg succeeds and answers from
  a model nobody chose. Only a bare name is looked up, table-valued keys are
  not adopted, and a profile that exists but declares no usable model raises
  rather than falling back to the name. Resolution runs on the request model,
  so library callers (`Branch(chat_model="codex/<profile>")`) get it as well as
  CLI callers.
- **A model configured behind codex may bring its own effort vocabulary.**
  `reasoning_effort` and `plan_mode_reasoning_effort` were a closed set of the
  eight words lionagi itself produces; codex and the provider behind it are the
  authority on what is valid, and a closed set here rejected working
  configurations before the CLI ever saw them.
- **An engine can grant its CLI legs the tools they were given.** The
  per-provider permission table was reachable only under `spec.yolo`, which the
  engine never passed, so a leg spawned this way was denied every tool call and
  returned success carrying nothing. The default stays off: what was broken is
  that asking was impossible from this path, not that the answer was no.
- **Long narrative comment blocks move into a `docs/internals/` reference set**,
  leaving a short summary and a cross-reference at each site. The executable
  code is unchanged, verified by comparing the parsed syntax tree of every
  touched file against its previous version. Docstrings read at runtime are
  excluded, since the MCP request tool's docstring is published as that tool's
  advertised description.
- **Parameter annotations admit their `None` defaults** across `lionagi/`, with
  `RUF013` enabled so the simple form cannot come back. No behaviour changes;
  every edit widens an annotation to match a default the code already had.
- **Dependency floors raised** on four advisories: `cryptography` to 50.0.0,
  `GitPython` to 3.1.57, `aiohttp` to 3.14.3, and in the VS Code extension an
  `undici` override at 7.29.0 with `fast-uri` at 3.1.5. `brace-expansion` moves
  to 5.0.9, and that one was not reported: the override written to remediate its
  advisory was pinned at `^5.0.8`, inside the affected range, so the pin was
  resolving to a vulnerable version.

### Fixed

#### Studio and mirroring

- **One agent run no longer shows up twice.** The codex mirror imported every
  rollout under `~/.codex/sessions`, including the rollouts written by headless
  `codex exec` runs that lionagi itself had spawned as agent legs, so a single
  run appeared once under its agent's name and again as an extra "Codex
  session". Rollouts whose originator marks them as headless are no longer
  imported. Interactive Codex history (desktop app, TUI, IDE) mirrors exactly as
  before. Rows imported by earlier versions are absorbed on the mirror's next
  start, so existing double entries heal on upgrade, and the removal is scoped
  to importer-written rows and can never touch a live run's session.
- **An unreadable definition history is no longer reported as an empty one.**
  Definition reads answer from two places at once, current content from disk and
  version history from the store, and history was read from SQLite directly
  while saves go through StateDB — so a server-backed deployment had its writes
  land in the server while reads fell back to an old local file, reported as
  this definition's versions over content read live from disk. An empty history
  is a claim about the definition and a caller told there are no versions
  concludes nothing was ever saved; the true statement is about the store.
- **Data routes a server-backed store cannot answer now return 501** instead of
  an empty list indistinguishable from "genuinely no rows". Not 503, because the
  backend does not change while the process runs, so retrying should not be
  implied. `/admin/readiness` is deliberately unguarded, since the store's
  condition is that route's subject.
- **The store is resolved once, where the services open it.** Every Studio
  service named the default path directly, which is right exactly when nothing
  has moved it; with `LIONAGI_STATE_DB_URL` pointing elsewhere a route read a
  database the daemon never opens and reported on rows nobody is serving.
- **Schedule reads no longer take a write lock.** Seven read routes opened the
  store the ordinary way, reconciling the schema and taking a `BEGIN IMMEDIATE`
  write lock once per request.
- **Studio and the state engine wait the same time on a store lock.** Each set
  its own `busy_timeout` from a number written out by hand.
- **A WAL checkpoint records what it actually drained.** `PRAGMA
  wal_checkpoint(TRUNCATE)` returns zeros on every success by definition, so the
  audit event could only say that a checkpoint succeeded; someone investigating
  a stall read it as "there was nothing to drain", which the row does not say
  and cannot say. The WAL size going in and the elapsed wall time are now
  recorded alongside.

#### State and persistence

- **Store credentials are masked in every operator-facing string.** A store URL
  is a credential when the store is a server, and every command reporting which
  store it consulted printed it whole into terminal scrollback, CI logs and
  machine-mode JSON. Two channels carried it: the URL field, and an exception's
  own message, which is separate code unaffected by masking the field beside it.
  The masker also grows the arm it was missing, where a value with no scheme
  parses as a path so the function whose whole job is to remove credentials
  returned one verbatim.
- **The read-only open is taken only where the backend supports it.** Read-only
  mode is a SQLite URI open of an existing file, so an unconditional request
  does not degrade on a server-backed store, it fails at open — which would have
  turned seven call sites asking for it as an optimisation into hard errors the
  moment the store moved to a server.
- **The imported-session teardown is locked against concurrent writers.** Its
  retention check and the deletes it authorises could straddle a concurrent
  writer committing a new reference on PostgreSQL.
- **A late writer can no longer overwrite a session-control outcome someone
  else recorded**, which used to turn a delivery an operator had resolved into
  one the record said never arrived. A refused finalize is now reported rather
  than discarded.
- **The steer tombstone is given a database handle that is still open.** The
  sweep that closes controls at run teardown was handed a connection the
  teardown had already closed, on every ordinary agent run, and it swallows its
  own errors so bookkeeping cannot fail a finished run — leaving the whole path
  collapsed to one logged line while the controls it exists to close stayed
  pending.
- **A threshold-metric comment claimed an invariant the code does not hold**,
  sending a reader auditing it to look for a bug that was not there.

#### Providers and runs

- **A codex session failure is reported once, and the chunk carries
  `is_error`.** Two objects described one failure and disagreed about whether it
  was one: a real `turn.failed` reached a consumer twice, and neither chunk set
  the flag, so a consumer reading the flag instead of the type saw no failure at
  all. The flag follows the classification rather than the type, because a
  resumed session that ends normally emits an error-typed chunk with an empty
  payload that has to keep reading as success.
- **A resumed provider session follows what actually ran.** A session belongs to
  the provider and model pair that created it, and only a caller changing the
  pin invalidated one — so an unpinned conversation ran on whatever the
  environment resolved to and then resumed that session under a different pair
  once the default moved.
- **A run that asked for a terminal notice it can never get now says so.**
  `--notify` fires on a session entity's terminal transition, so when
  persistence setup failed there was no entity, the registration was skipped,
  and the run did all of its work and finished in silence — which is exactly
  what a run that never asked for a notifier produces. Consumers here are
  automated: the MCP server takes the notice as the run's end and otherwise
  publishes `outcome=indeterminate`, so a run that completed and wrote its
  report read to the caller as a run that vanished. 44 job records on one
  machine carried that outcome.
- **A child that lost its persistence now delivers its own terminal notice**,
  directly against a synthesized envelope once the run's real terminal status is
  known, rather than registering for a transition that can never happen.
- **A spawn going ahead without its declared secrets is now said out loud.** A
  configured-but-refused lookup returned the environment unchanged and said
  nothing, and an absent lookup returns the same value, so someone who
  misconfigured a lookup saw only their child dying on a missing key with
  nothing pointing back at their own configuration.
- **A control that cannot answer is no longer reported as a policy verdict.** A
  denial means the policy is working and a fault means the policy is not
  running, and an operator does entirely different things about them.
  `ControlUnavailableError` subclasses `PermissionError`, so everything already
  failing closed on that keeps doing so. It also separates a handler that ran
  and declined from no handler being configured, which used to be reported as
  the latter.
- **Messages a teardown flush could not save are now reported.** The last
  attempt reported failure only by returning a boolean that every caller
  discarded, and the state log stays quiet when nothing changed, so the last
  attempt those events ever got was exactly the attempt that emitted nothing.
  The count is included because it is the part nobody can reconstruct
  afterwards.
- **A notify refusal caused by a sender-identity mismatch has its own class**,
  rather than being stored as `unknown` and indistinguishable from a genuinely
  novel failure, though it is a configuration fact with exactly one fix.
- **Recalled memories are dated and name the serving profile** in the block
  injected into an agent's prompt. That block is deliberately wrapped as
  untrusted context, but an undated bullet inside it gives the reader no way to
  tell a genuine record written a year ago from something invented, so a true
  old memory read as suspect.

## [0.32.0] - 2026-07-30

### Upgrading from 0.31.1

**The browser-credential system is gone.** Studio is a single-user tool that
binds to loopback by default, and before the Operator workspace landed its only
access control was the optional bearer token set through
`LIONAGI_STUDIO_AUTH_TOKEN`. The credential system introduced alongside that
workspace is removed again, so there is no login step, no stored browser
credential, and nothing to rotate. The bearer token is unchanged and is still
the way to protect a Studio you bind to a non-loopback host with `--host` or
`LIONAGI_STUDIO_HOST`. Anything that worked before the Operator arrived is
unaffected. If you stored a credential during that window, discard it.

**The Studio marketplace bundle now leads with its MCP server** and no longer
ships a `.mcp.json`. The manifest declares the server inline, so there is no
precedence question between two carriers. `uv` on PATH is the only prerequisite.
The bundle version moves to 1.1.0, which is what makes an already installed
plugin pick any of this up. A plugin-provided server's tools are namespaced, so
the tool a plugin user calls is `mcp__plugin_orchestrate_lion__request` rather
than the unscoped name. The `li` CLI stays documented as the path for someone
working inside a lionagi checkout.

### Added

- **An Operator workspace in Studio.** A conversational way to ask for work,
  watch it run, and approve what it proposes, without having to learn the rest
  of the Studio UI first. Everything that changes state is permission gated: the
  Operator proposes, and the change happens when you approve it.

- **The Operator keeps its session and can answer questions about the app.** A
  conversation persists its provider session id and resumes it, so the second
  message no longer meets a stranger and continuity is no longer faked by
  replaying earlier frames as prompt text. The id is re-read every turn rather
  than pinned on first write, because a resumed session is free to hand back a
  new id and pinning would silently stop resuming the moment it did. The page
  snapshot the browser already sent with every turn is now rendered into the
  prompt, bounded per value the same way history frames are. Five read-only
  tools answer questions the panel previously could not: run statistics over a
  window, the current view, the schedule list with next fire and recent health,
  the agent library, and the playbook names that launching one requires. Each is
  a bounded, explicitly projected read, and project paths go through the same
  leaf-name redaction as the existing run list.

- **A model selector in the Operator header.** Switching models starts a fresh
  provider session for that conversation, because a provider session belongs to
  the model that opened it and resuming one under a different model is
  undefined. Tool calls render compactly instead of dominating the transcript,
  and agent definitions render as markdown rather than raw preformatted text.

- **Codex sessions appear in Studio.** `~/.codex/sessions/**/rollout-*.jsonl` is
  mirrored into StateDB the way `~/.claude/projects` already was, and enough
  import provenance is recorded that a mirrored row can be told apart from a run
  lionagi executed itself.

- **khive context-injection counters in session detail.** A bare agent run
  recorded no context-injection telemetry, so the counters lived only as long as
  the process and `li monitor` had nothing to show.

- A run reports which MCP servers it gave its workers. `mcp_config_servers` names
  them on the submit handle, and `job.status` now carries the whole `mcp_config`
  group, so a run that has already finished answers the question without anyone
  opening its record on disk. An empty list and a null say different things: the
  list means the question was settled and the answer was none, which covers both
  a caller disabling MCP for the run and a config that was found and declares no
  servers. The null means no set was resolved, which happens three ways: a caller
  named their own config file, which this run does not read; no config was found
  at or above the launch directory; or the record predates the field. The field
  reports what was resolved, which is not a claim that the child's provider then
  started each server.

### Changed

- **The Studio marketplace bundle's primary interface is its MCP server.** The
  nine skills and the README lead with it, and the manifest carries the server
  declaration inline. See the upgrade note above for what an installed plugin
  needs in order to receive this.

- Markdown artifacts render through a guard that replaces an absolute or
  protocol-relative image target with a visible placeholder naming the alt text.
  Opening an artifact could otherwise issue requests to hosts named inside it.
  Markdown rendered elsewhere is unchanged.

### Removed

- **The browser-credential system.** See the upgrade note above.

### Fixed

- Resuming a codex agent that was given MCP servers with `env` or `http_headers`
  no longer fails before spawn. Those fields are handed to codex through a
  generated config profile rather than the command line, and codex takes one
  profile per invocation, so the code refuses to add a second one when the
  caller already asked for a profile of their own. A resumed agent re-spawns from
  its persisted request, which carries the profile the first run generated, and
  that was being read as a caller's profile. A profile named in the generated
  shape, `lionagi-mcp-` followed by 32 hex characters, is now replaced instead,
  which is also what the resumed agent needs: the first run deleted its profile
  file on the way out, so the inherited name pointed at nothing. That shape is
  reserved. Any other profile name, including another name under the same
  prefix, is the caller's and is still refused.

- **A spawn that never resolved is no longer reported as still running.** A job
  record whose spawn phase is still preparing names a spawn that was never
  attempted, or one whose result was never written. The classifier makes no
  claim about its fate, because no bound on the record can tell a loaded machine
  from a dead spawn, and it now says so instead of presenting the row with the
  signature of a live one. The job listing likewise says which running rows
  never started, and no longer lists a directory that was reserved but whose
  record has not been published: reserving a directory happens before the
  command is prepared, and publishing the record is the boundary that turns a
  reservation into a job. A record that exists but cannot be read is still
  listed, along with the state of that read, rather than hidden.

- **A spawn command rejects excess positionals before it submits.** These
  commands take at most a model and a prompt positionally. An extra value used
  to be accepted at the request layer and to fail later during argument parsing,
  after a job record and a child process already existed, with the error visible
  only in a console log rather than in the response.

- **The fingerprint a help surface quotes is the one the call can send.** A
  spawn verb's `schema_fingerprint` hashes the schema the call resolves, and for
  the playbook and flow submissions that schema includes the named playbook's
  own declared arguments, so the fingerprint is a function of the playbook. Two
  surfaces disagreed about that.

- **A terminal notice is signed by the seat that submitted the run.** The
  delivery command was spawned without a working directory and inherited the
  caller's, so a notifier that resolves its own identity from its working
  directory signed with whoever owns the directory the run executed in.
  Separately, a failed terminal notice is now classified without storing what it
  said.

- **Worker artifact directories are described only where they exist.** The
  initial worker setup set CLI-only endpoint settings for every worker, so an
  API-backed worker was told in its prompt that the artifact directory was its
  working directory, which is not true for it. Reactively spawned workers on
  non-CLI providers get an output-only artifact path for the same reason: tools
  falling back to their own default working directory could otherwise write
  relative output outside the named path.

- **A generated profile name is matched at both ends.** A prefix match treated a
  caller-supplied name that merely started like a generated one, such as one
  with a trailing newline, as this code's own to overwrite.

- **A job lock reports a release that did not happen.** Release failures were
  suppressed unconditionally. Suppression now applies only when the protected
  section itself failed, where a release failure must not answer in place of the
  real one. When the section succeeded, a failure to unlock or close is the only
  failure there is, and it reaches the caller.

- **A caller-supplied working directory is resolved before it is checked.** The
  directory check answers about the submitting process's own directory, and the
  value was recorded unresolved, so a relative path was checked against one
  directory and read back against whatever directory a later reader was in.

- **Provider capability tables are complete and guarded at import.** The
  per-provider yolo and bypass tables are keyed by provider spelling and were
  maintained by hand, so they had drifted: three CLI provider spellings were
  absent from both. Naming MCP servers for a gemini provider now raises rather
  than being silently dropped, since the CLI behind it cannot carry them, and a
  spawn is refused only when servers were actually named. Explicit MCP
  configurations that are not supported are rejected outright. The agy print
  mode gets a cap derived from the caller's timeout, validated before it reaches
  argv, and an unrepresentable Antigravity deadline is rejected rather than
  encoded.

- **Scheduler and Studio startup.** Studio inspected Git state on the event
  loop, so a slow repository read blocked the loop before the server began
  serving; the inspection now runs in a worker thread and is still awaited in
  place, which keeps the guarantee it exists for. A scheduled run exported an
  invocation id to its children, but the child CLI had no way to read it, so
  budget accounting saw zero spend for work that had really run. The one-time
  dispatch-timestamp backfill ran in the same transaction as the statement that
  added the column, so a database that gained the column in an earlier release
  without the backfill never got one; it is now gated on a durable marker
  claimed with an insert-if-absent, so it happens exactly once per database
  regardless of when the column arrived. A failing scheduler subprocess now
  carries its diagnostic instead of discarding stdout. Claim-time candidates
  sort duration-valid first, so a row that can never run no longer consumes a
  cap slot ahead of a valid one.

- **A declarative command target's arguments are held to their own field's
  contract.** They were validated against a different field's validator and
  persisted to a destination whose imperative path validates it differently.

- **State persistence backlog.** Standard-library `default=` conversions are
  honored while non-finite values are still refused on the durable JSON path.
  Schema inspection and migration are serialized under backend-appropriate
  locks. Messages referenced only by owners that were deleted are pruned.
  Inherited run identity is consumed after a single child allocation. Session
  retention is based on the latest update and rechecked under the prune lock.
  Float-list message embeddings are encoded as little-endian float32 blobs with
  parity across backends.

- **Concurrent streams no longer report each other's ending.** A stream's
  terminal state was held in a plain attribute on the event, so two streams
  running concurrently on one event overwrote each other and a post-stream hook
  could be told how the other stream ended. The state now lives in a per-event
  context variable for the duration of that hook, and is retained on the event
  afterwards so a parent reading the property later still sees the last terminal
  state.

- **An explicitly supplied model that happens to be falsy is preserved.**
  `chat()` resolved its model with an `or`, which discarded it. It now asks the
  parameter class whether the argument is a sentinel, which is the policy that
  decides every other omitted field on that class.

- **The public message projection is nullable again.** `Message.chat_msg` is
  documented to contain any projection failure and return `None`. A prior change
  removed that guard, so a failure while reading the role or rendering content
  escaped to callers that rely on the nullable result to skip an unrenderable
  message.

- **An artifact verdict that was never recorded is distinguishable from one that
  failed.** Verification carried a single shape for three situations: checked
  and passed, checked and failed, and never checked. Consumers indexed the
  failure fields unconditionally, so a session with no stored verdict read as a
  session that had missed its artifacts.

- **`li kill` recognizes macOS framework Python launchers.** Framework builds
  present argv[0] with a capital P, and the match was case-sensitive, so a
  shebang-launched `li` started by a framework interpreter was not recognized as
  ours and `li kill` declined to act on its own process. Team wakeup rounds are
  also now told the artifact contract their result has to meet.

- **The full-page graph editor's MiniMap uses its default dimensions.** It
  rendered at a fixed size intended for a compact embed that no surface actually
  renders.

- **The served OpenAPI document states the running version.** The Studio app was
  built without one, so the framework supplied its own default and every release
  served the same string. A client reading that document to decide what the
  server supports was reading a constant, which made two servers on different
  releases indistinguishable and left a version-keyed feature gate unable to
  move. Note that the published Python package carries the backend only: the
  browser interface ships in the container image and in the hosted deployment,
  both built from the same release.

- **Packaging.** The VS Code extension no longer ships a source map that a
  negation had re-included after `.vscodeignore` excluded it. The marketplace
  manifest lint accepts only frontmatter a YAML parser can read, so a file can
  no longer pass the lint and still fail to load in a plugin host.
  `datamodel-code-generator` is floored at the patched release.

### Internal

- Two consecutive releases published no container image. Buildx setup is now
  retried, since it pulls from a registry outside this project,
  and the release now reads the tag back from the registry rather than trusting
  the push step. Workflows also run when a draft pull request is marked ready,
  which raises no event under GitHub's default activity types, and a pull
  request's own in-flight runs are superseded instead of queueing beside them.

## [0.31.1] - 2026-07-28

### Upgrading from 0.31.0

**Durable JSON now refuses non-finite floats.** Writing `NaN`, `Infinity`, or
`-Infinity` through the paths that persist state to disk previously produced
output that is not JSON, or a value that reads back as `null` with no way to
tell it from an absent one. Those writes now raise, with the path to the
offending value named in the error.

Migration: if you persist computed floats, guard them at the point you build the
record (`math.isfinite(x)`) and decide what a non-finite value should mean in
your data, rather than letting it reach the writer.

A database stamped with a newer schema version is also refused when it is opened
for writing, rather than being migrated down to the shape this release applies.
Read-only opens apply no schema and are unaffected, so such a database can still
be inspected.

### Added

- A resolved MCP server set is handed to every CLI provider that can carry one,
  rather than only to Claude providers. A codex agent that was given servers
  previously lost them: on two of the three spawn paths silently, and on the
  third with a message reporting a refusal while the set was dropped anyway. The
  decision is now one capability check applied in one place, so the paths cannot
  disagree. Fields carrying secrets are written to a private profile file
  instead of the command line.
- A flow or fanout run can choose the MCP servers its agents receive, including
  deliberately handing over none instead of arriving there by an empty search.
- A flow that names neither an agent nor a model orchestrates by default rather
  than being refused.
- `StreamTerminalState` is public. A streamed event now records how its stream
  ended, as completed, closed early, cancelled, or failed, and exposes it as
  `stream_terminal_state`.
- `profile.list` takes `names` and `fields`, so a caller asking whether one
  profile exists and what model it runs no longer pays for the whole roster.
  Given neither, the reply is what it was.
- Studio shows artifacts as they arrive instead of waiting for the run to end.

### Changed

- Non-finite floats are rejected where durable JSON reaches the filesystem, and
  where a JSON `null` would be undetectable. See the upgrade note above.
- A database stamped with a newer schema version is refused on a writable open.
  Read-only opens are unaffected. See the upgrade note above.

### Fixed

- A fanout with no declared dependencies runs its workers concurrently again. An
  empty dependency list was being read as "unspecified", which chained the
  workers into a sequence, so a fan of N workers took roughly N times longer
  than it should. It still completed, which is why nothing caught it: it was
  only slow, and no test measured speed.
- Cleanup failures in the job store no longer answer for the failures underneath
  them. A release that fails cannot replace the error the caller raised, and an
  interrupt arriving during release is neither swallowed nor allowed to skip the
  close.
- An expected artifact named by a bare filename matches the file a worker
  produced in its own directory. Verification joined every declared path
  directly under the artifacts root, where in a multi-worker run the file never
  lands, so a run that had completed successfully was rewritten as failed. An
  entry that names a directory still matches exactly.
- The diagnostics for MCP passthrough describe the branch they observed rather
  than the whole run, and name the provider actually in hand. One branch's
  inability to carry a server set read as a verdict on every worker in a flow,
  and a codex agent was told the message was about the Claude CLI.
- The post-invocation hook on a streamed event runs however the stream ends,
  not only when it runs to completion. A source error, a consumer that stopped
  early, and a cancelled consuming task each bypassed it before. Whatever ended
  the stream still reaches the caller unchanged.
- A run whose process is conclusively gone gets an attributable end instead of
  staying open, and `job.wait` stops holding its window open for it.
- A terminal notice that could not be delivered is reported where callers wait,
  and the run's log keeps the line saying so.
- A submission naming no model is refused instead of returning a run id for
  something that will not start. A spawn that fails is one failed operation
  rather than a lost batch. `help` and `ops` in one request is refused instead
  of silently dropping the ops.
- Run records report what happened rather than what was asked for: the profile
  the run used, the dependencies it has, the prompt that ran, and the artifact
  directory each orchestration worker was given.
- `li kill` reaches a play's workers when the row records them, and says so when
  it cannot. A play awaiting a gate decision is no longer reported as running,
  and a kill that performed no termination exits non-zero.
- One prompt-length limit for orchestration specs, raised from 8,192 to 262,144
  characters. The check lived in three places, each with its own copy of the
  number, so a spec the CLI accepted could still be refused by a schedule. They
  now read one constant, and the refusal names the limit it enforced.
- The planner roster carries each role's description. A role sharing its name
  with a user profile showed the profile's line instead, which with the default
  profiles present was every role. Roles whose two spellings differ only by `-`
  or `_` are one entry rather than two, and a plan may still name either.
- Profile lookup reports only the profiles that shadow the name asked for,
  rather than every profile declared.
- A model supplied for a single call chooses the transport for that call. A
  CLI-backed branch given an API model for one call routed it into the CLI
  streaming path, which rejects it, and was left holding the rejected model.
- The terminal envelope a flow emits when finalization fails reports the
  resolved outcome when resolution got that far, instead of restating the
  flow's coarser status, and no longer labels an interrupted run a user abort.
- Agent profile names treat `-` and `_` as the same spelling.
- Long codex conversations can spawn: the prompt is passed over stdin rather
  than as a command-line argument.
- `Pile` and `Progression` are iterables rather than iterators, so iterating one
  twice works.
- Messages that fail to render are no longer dropped, and an arbitrary payload's
  `allowed()` is not called.
- A pre-dispatch failure no longer consumes its `github_poll` trigger.
- The schema stamp moves with the migrations, and SQLite is checked before WAL
  is enabled.
- `li state prune` reclaims message rows and previews a measured count.
- Studio renders markdown artifacts as markdown, and spaces execution-graph
  nodes by their real height.

### Docs

- ADR coverage for attributable run ends, and for four `job.wait` properties
  that were previously left to the reader.
- Corrected the provider path and list in the architecture guide.
- Two tool docstrings that described behaviour the code refuses.

## [0.31.0] - 2026-07-26

### Upgrading from 0.30.2

0.30.2 does not contain the MCP server. Its published wheel has no `lionagi/mcp/` files and its
`li` entry point registers no `mcp` subcommand, so installing `lionagi[mcp]` at that version
installs `fastmcp` and no server: the extra appears to succeed while `li mcp` fails with an
invalid-choice error. The MCP client wrapper under `lionagi/service/connections/` is a separate,
long-standing feature and is unaffected. 0.31.0 is the first release that ships the server.

### Added

- `li mcp` serves an MCP server (over stdio) that submits `li` runs — agent, flow, and fanout —
  as detached background jobs and answers questions about them. The server advertises a single
  tool, `request`, and every operation is a namespaced verb passed to it. A submit returns a
  `run_id` immediately while the run continues in its own process group, so it survives an
  MCP-server restart. `fastmcp` stays behind the optional
  `[mcp]` extra; importing `lionagi` never pulls it, and the job engine plus terminal notify hook
  are standard-library only. Terminal notices are delivered through a configured command
  (lionagi's own `notify.on_terminal` setting, a per-submit override, or an environment default),
  never a hardcoded one, and the delivery outcome is recorded on the job and surfaced by the
  `job.status` operation. See `docs/reference/mcp-server.md` for the verb catalog and `.mcp.json`
  registration.
- `/api/admin/health` reports `code_identity`: which commit the Studio daemon's code was loaded
  from, whether that checkout has since moved or been edited, how far behind its comparison ref it
  is, and a `drift` verdict over all of it. Under an editable install the code being served is
  whatever commit the working tree sits on, and it keeps being served after the tree moves — a
  state the version string cannot show, since it is identical between a current checkout and one
  many commits behind. A reading that cannot be taken is reported as `unknown` with the reason
  rather than omitted, because an absent key is indistinguishable from a healthy one to anything
  scanning the response. The reading runs off the event loop, so a daemon never stalls on its own
  health check.

### Fixed

- The Studio daemon no longer reports a commit it never loaded. `code_identity` reads its git
  position once and keeps it, initialising lazily, and the daemon left that initialisation to the
  first `/api/admin/health` request. A checkout moved after startup but before that request — an
  ordinary deploy or branch switch — was then read as the position the process had loaded from, so
  the response named the new commit with `checkout_moved` false and a clean `drift` verdict, which
  is the exact condition the field exists to detect. The daemon now takes the snapshot in its
  lifespan, before the scheduler starts and before it serves anything, as the MCP server already
  did.
- The built-in provider collision filter no longer misses a plugin entry whose endpoint class
  comes from a helper module. `_reject_builtin_collisions` identified an activation's entries by
  requiring the class to be defined in the activated provider module, but a provider module may
  register a class supplied by a helper or generated module. Such an entry carries the same
  provenance `register` already records while its `__module__` differs, so it stayed registered
  and silently took over a provider name a built-in serves, with no rejection diagnostic. The
  filter now identifies activation entries by that recorded provenance alone.
- The studio scheduler no longer silently runs a scheduled action in the daemon's own working
  directory when the schedule's configured execution root is unavailable. A subprocess started
  without an explicit `cwd` inherits the daemon's directory, and `_resolve_action_cwd` fell through
  to that inherit whenever a schedule's `action_cwd`/`action_project` could not be resolved to an
  existing directory (e.g. after a worktree was pruned). When the daemon's own directory resolves
  to a project, the spawn does not fail — it succeeds and runs the action in the wrong directory,
  substituting the working directory (and anything a tool derives from it) for the one the schedule
  configured. The resolver now fails closed for any schedule that carries an execution root, raising
  `SchedulerCwdInheritRefusedError` (recorded as `run.failed.cwd_inherit_refused`) instead of
  inheriting. Pre-migration rows that configured no execution root keep the legacy inherit-and-warn
  behavior and are backfilled on daemon restart.
- `Observable` is a structural protocol again, restoring duck-typed Pile admission. A prior
  change made admission nominal: `isinstance(item, Observable)` required inheriting the ABC, so
  an object exposing an `id` was rejected by `Pile`. `Observable` is now a single
  runtime-checkable `Protocol` — any object with an `id` conforms, and `Element` satisfies it
  through its `id` field rather than by inheritance. Identity resolution (`ID.get_id`,
  `validate_order`) is structural to match, so an admitted duck-typed item can also be found,
  retrieved, and removed by identity instead of being admitted but unreachable. `item_type`
  normalizes the classes it is given without judging conformance, since a class that assigns
  `self.id` in `__init__` declares nothing at class level while its instances conform; the check
  belongs at admission, where a real object can be inspected. Serializing a Pile still requires
  items to provide `to_dict()`.

## [0.30.2] - 2026-07-23

### Fixed

- `to_list(..., unique=True)` no longer drops unequal items whose structural hashes collide.
  When the input holds an unhashable value, deduplication falls back to hashing; that fallback
  compared by hash alone, so two unequal mappings with colliding hashes (e.g. `{"x": -1}` and
  `{"x": -2}`, since `hash(-1) == hash(-2)`) were treated as duplicates and the second was lost.
  Hashable items now stay on the native set, and only genuinely unhashable values are bucketed by
  structural hash and confirmed by identity-or-equality, so a collision keeps both items while a
  repeated same instance (such as `NaN`, which is unequal to itself) still collapses to one.

- `lcall` / `alcall` no longer ignore input transforms when `flatten`/`dropna` are unset.
  `input_use_values` now extracts a mapping's values, and `input_unique` without flatten raises the
  documented `unique=True requires flatten=True` instead of being silently dropped. The
  no-transform path is unchanged, so ordinary calls behave exactly as before.

- `aterminate_process_group` enforces its grace timeout on the Trio backend. The post-`terminate`
  wait was bounded with `asyncio.wait_for`, which raises `RuntimeError: no running event loop` on
  an AnyIO/Trio task before the timeout policy could apply, so a process that ignored `terminate`
  was never force-killed. The wait now uses an AnyIO cancel scope, preserving the `SIGKILL`
  escalation on both the asyncio and Trio backends.

- `FieldModel` metadata survives `to_spec()`, and spoofed union types are rejected. Unknown
  metadata keys and an explicit `None` default are no longer dropped when a field model is
  converted to a `Spec`, and a type that merely imitates `types.UnionType` (for instance by
  stringifying to the same repr) no longer passes the base-type validity check that real unions
  satisfy.

- `OperableModel` captures `FieldModel`s supplied as constructor extra fields. The extra-field
  validator discarded them before the after-validator could record them in `extra_field_models`,
  so a model built with field-model extras lost that structure.

- `Session` mail is no longer dropped when a delivery fails. Both the async and sync exchange
  collect paths cleared in-flight state even when the inbox write raised, losing the message from
  every recovery surface; a failed delivery now stays recoverable via `drain_pending()`. A cloned
  branch also re-serializes idempotently, so a branch restored from a clone can be saved again.

- `Flow` and `Pile` no longer mutate caller-owned state or disagree on ordering. `Flow.from_dict`
  copies caller metadata before popping `lion_class` rather than mutating a reusable snapshot;
  `Flow.add_item` resolves progressions through the owned pile so an unowned `Progression` raises
  before any mutation (the operation fails atomically); and `Pile.to_df` serializes in progression
  order, matching iteration and every other ordered dump path.

- Provider and reader fixes: reading `OllamaConfigs.CHAT.options` no longer eagerly registers the
  Ollama chat endpoint (the request schema resolves off the decorator-free schema module, restoring
  the lazy-import contract); `gemini-3.6-flash` resolves to its High-effort model id by default with
  a version-aware fallback that neither silently downgrades an explicit `3.6` id nor false-upgrades
  an unrelated number containing `3.6`; an Antigravity CLI turn that reports `SUCCESS` with empty
  content is now surfaced as an error instead of silence stamped success; and `get_run_file` rejects
  a file whose bytes are malformed UTF-8 at the read-cap boundary instead of serving it as truncated
  text.

## [0.30.1] - 2026-07-21

### Changed

- Restored the foundational name `Observable` for the nominal Pile-admission ABC that
  0.30.0 had briefly renamed to `PileItem`. `Observable` is core lionagi ontology — the
  observer/observable concept the `Session`, `SessionObserver`, `Communicatable`, and event
  layers are built on — and names a thing with durable identity that a `Pile` can hold, not a
  container-membership role. The nominal-only admission behavior introduced in 0.30.0 is
  unchanged: `isinstance(item, Observable)` still requires inheritance rather than a bare `id`
  attribute, the structural `ObservableProto` split stays deleted, and `Pile` admission remains
  nominal. `PileItem` is removed with no compatibility alias (own-use scope). Every in-tree
  caller (`Element`, `Pile`, `Communicatable`, `SessionObserver`, `validate_sender_recipient`)
  and the public `lionagi.protocols.types` facade use `Observable`.

### Fixed

- The `li agent` codex file-access hint now recommends `--yolo` (the sandboxed default) rather
  than `--bypass` (which disables the sandbox) when a codex leg would otherwise hang on its first
  tool call. `--bypass` is still noted as the sandbox-disabling escape hatch.

## [0.30.0] - 2026-07-20

### Removed

- `lionagi._paths.clear_lionagi_dirs_cache` — `find_lionagi_dirs()` no longer caches the git-root lookup (it now calls `git rev-parse --show-toplevel` directly on every call), so there is no cache to clear. `_paths` is a private module (never re-exported from `lionagi/__init__.py` or any public package); this removal is internal-only, no consumer alias needed.
- `ObservableProto` and `LegacyObservable` (`lionagi.protocols.contracts`, previously
  re-exported from `lionagi.protocols.types`). Neither had an in-tree caller. Removed outright
  per the own-use scope in `docs/governance/standards/deprecation-policy.md` section 0, with no
  compatibility alias.
- `Observable` (`lionagi.protocols._concepts`, `lionagi.protocols.types`), renamed to
  `PileItem`. The name previously pointed at a structural protocol and now had to point at the
  nominal admission ABC Pile actually enforces; rather than let the same name's `isinstance`
  answers invert under existing callers, the nominal contract ships under a new name and every
  in-tree caller (`Element`, `Pile`, `Communicatable`, `SessionObserver`,
  `validate_sender_recipient`) was updated in this change.

### Added

- `PileItem` (`lionagi.protocols._concepts`, `lionagi.protocols.types`): the nominal Pile-item
  admission contract, renamed from `Observable`.

### Fixed

- API hook emit sites (`API_PRE_CALL`/`API_POST_CALL`/`API_STREAM_CHUNK`, `operations/_api_hooks.py`)
  hardened: a non-finite provider usage count (`NaN`/`inf`) is dropped from the typed usage
  summary instead of raising on `int()` coercion and aborting an otherwise-successful call; the
  `log_api_metrics` built-in reports the real `input_tokens`/`output_tokens` instead of an
  always-`None` `total`; `_safe_identifier` now redacts credential-shaped model/provider values
  that satisfy the identifier allowlist; and the stream `chunk_type` (provider-sourced, unlike
  model/provider) is validated against the closed `StreamChunk` vocabulary so a prefixless
  credential cannot reach telemetry.
- Endpoint provider registry no longer swallows bundled-module import errors or mis-routes
  unknown providers: a genuinely-absent optional dependency is now distinguished from a broken
  bundled module (the latter surfaces its `ImportError` instead of masquerading as "not
  installed"), and an unknown provider name is refused rather than silently mis-resolved.
- A failure in the post-DAG finalize step (synthesis-artifact write, team-inbox post, run-metadata
  build, branch-snapshot and resume-pointer writes) is no longer reported as a DAG failure. The DAG
  result is already complete when finalize runs, so a finalize side-effect error is surfaced
  distinctly instead of masking an otherwise-successful run.
- MCP client recovery no longer lets a policy-omitted re-entrant `get_client` inherit an earlier
  caller's trust: the transport-recovery path (which must recover the policy the transport was
  already authorized with) and the genuine no-policy path are disambiguated, so an omitted policy
  cannot silently reuse a prior caller's security capability.
- Agent `--resume`/`--continue-last` no longer silently drops an explicitly requested role.
  create_agent provenance is read only from the immutable branch-origin marker, never re-derived
  from persisted system-message content, so a markerless branch given a role gets that role's
  system prompt rather than having it skipped.

## [0.29.1] - 2026-07-15

### Added

- Typed schedule quick-create commands: `li schedule create <agent|flow|playbook|command> <name> ...` compile straight into a `ScheduleMember` and run through the identical `resolve_member`/`create_quick_schedule` path a ScheduleSet member uses — no forked validation. Command-kind actions are gated by a `LIONAGI_SCHEDULER_COMMAND_ALLOWLIST` env allowlist (empty means refuse all), accept bare PATH-resolvable executable names only, and reject leading-dash argument tokens.
- Versioned ScheduleSet declaration layer: `li schedule apply/plan` reconcile a committed `ScheduleSet` YAML document against owned rows (omitted owned rows are disabled, never deleted), and `li schedule export` converts existing rows back into documents — legacy rows via typed reconstruction through the same static resolution an apply uses, declaration/cli rows via authored-spec re-export. Conversion blocks (never half-emits) rows it cannot represent faithfully: chained follow-up actions, unsupported action kinds, flow-YAML rows carrying a model, launch-time extra args, and github triggers whose effective poll cadence differs from the default. Mixed-project exports write one document per effective project with collision-checked sibling filenames. Exported flow snapshots are referenced relative to the output directory so document and sidecar commit and move together; absolute working directories are kept verbatim but flagged on the export report.
- Schedule observability: run rows join schedule metadata in a `RunView`, `li schedule runs/status` output is enriched, and `li schedule trigger --wait` follows the fired run to its terminal state.
- Terminal notify forwarding for declared schedules, and a per-invocation `--notify` on `li agent` and `li o fanout`, both riding the generic on-terminal callback layer (ADR-0095).
- `li kill` reaps detached-play workers transitively (ADR-0104, now marked Accepted/Implemented).
- khive context injection active on the agent spawn path and the bare `li agent` path (ADR-0008).
- Mirrored sessions stored and resolvable by `cc_session_id`; inherited agent-depth env marker `LIONAGI_AGENT_DEPTH`.
- lionbench v0 record schemas and campaign config.

### Fixed

- Schedule row reads decode `authored_spec`/`resolved_target`; schedule rolling-window cap, github 403 no-poison-cache, outbox durability contract, and scheduler cancel routing.
- Live-persist teardown drain, terminal-notify `previous_status`, run manifest lifecycle, and fanout partial durability; state lifecycle batch covering transition rejection precedence, initial-state history, and the terminal-write chokepoint; `cc_session_id` backfill preserves reconcile CAS.
- Hooks: permanently-invalid messages are dropped instead of blocking the retry queue; gate action re-derivation and denial-signal audit.
- MCP: explicit transport-trust decision at load time; the permission chain now applies to MCP-discovered tools.
- Provider batch: replayable no-buffer fast path, bounded MCP arrays, OpenAI Batch endpoint routing, gemini teardown retry, and case-fold provider routing.
- Operations: FAILED action error path, first-chunk cancel cleanup, the `suppress_errors=False` captured-failure path, and context-provider registry coverage.
- Plugins: restored cross-plugin tool-name collision check, targeted single-plugin rescan, and a win32 platform guard.
- CLI: restored the available-profiles hint on a plugin-scoped profile miss, unconditional bypass warning, settings ancestor-walk, and resume system-prompt backfill.
- Protocols: Node admission, Graph/Edge round-trip, processor capacity and queue bounds, and `gather_writeback` treating a non-int writeback return as success.
- LNDL batch: round-outcome policy, per-round-shape rule, numeric coercion, and multi-block extraction.
- Studio: honest fleet stats totals, minimap sizing, a show-level lifecycle reaper for phantom active shows, and a soft watchdog bounding coding-engine plan/verify stages.

### Docs

- Practical operator guide for scheduling workflows (typed quick-create per kind, ScheduleSet declarations, triage, stopping and misbehaving-schedule playbooks); stale recurring-runs cookbook rules rewritten.
- Benchmark-gate contributing docs aligned with the same-machine A/B contract; ADR cross-reference and numbering fixes.

## [0.29.0] - 2026-07-13

### Added

- Dispatch outbox retention and auditable purge (ADR-0059 delta 3): `dispatch_outbox` rows now expire automatically, riding the existing `prune_old_data` maintenance sweep — terminal-success rows (`delivered`/`acked`) after `LIONAGI_STUDIO_DISPATCH_RETENTION_SUCCESS_DAYS` (default 7 days), dead-lettered/expired rows after `LIONAGI_STUDIO_DISPATCH_RETENTION_DEAD_LETTER_DAYS` (default 30 days); `pending`/`delivering` rows are never eligible for the automatic sweep. `li dispatch purge` now writes an `admin_events` audit row on every delete (single-row and bulk) and gains bulk criteria: `li dispatch purge --status STATUS [--before EPOCH] [--dry-run]`, requiring an id or explicit criteria so a bare invocation cannot mass-delete. An explicit `--status` is honored exactly as given, including `pending`/`delivering` (naming an in-flight status is deliberate operator intent); a bare `--before` with no `--status` defaults to terminal statuses only and never implicitly sweeps `pending`/`delivering` rows. `status_transitions` history for purged dispatch rows is preserved, not cascade-deleted. `deliver_due_dispatches` now tolerates a row being purged out from under it mid-scan (an operator purge racing the scheduler tick): that row is skipped and the rest of the due batch still delivers, instead of the whole tick aborting on `LookupError`.
- A directory-bundle plugin system (ADR-0088): a `plugin.yaml` manifest schema, filesystem discovery scoped to `.lionagi/plugins/`, a content-pinned trust model (sha256 over the manifest plus every declared capability file), a lazy two-stage activation registry (discovery is a data-only parse; bundle code is imported only on first actual use), and a `li plugin` CLI surface (`list` / `info` / `trust` / `enable` / `disable`). Four runtime consumers are wired end to end: a trusted, enabled plugin's agent profiles and playbooks resolve as `<plugin>/<name>` (bare-name resolution when unambiguous, with a shadow warning against a same-named local profile, and `li play list` now merging local + plugin playbook names), its provider modules are consulted on an `EndpointRegistry` match miss before falling back to the generic OpenAI-compatible endpoint, and its tools are consulted on an `ActionManager` tool-name miss. A plugin that shadows a built-in provider or tool name is now rejected outright with a named, explicit error rather than silently winning or losing the collision. Plugin discovery never fires on a bare `import lionagi`.
- A generic on-terminal callback layer for run-terminal events (ADR-0095/ADR-0060), plus `--notify` and a `notify.on_terminal` settings contract: `li agent`, flow/play runs, schedule runs, and Studio launches now all emit the same versioned `RunTerminalEnvelope` from the one guarded lifecycle transition every entity status write already funnels through, instead of each CLI command needing its own notification plumbing. `notify.on_terminal` is authorable as a shell command string (no-shell, `shlex`-split) or a mapping (`{enabled, adapter: {kind: exec|python}, filter}`), resolved per-run override > project settings > global settings > disabled, and never raises on malformed settings. A `terminal_deliveries` reconciliation ledger gives consumers that need a durable "have I seen this terminal event yet" query an idempotent-ack alternative to the fire-and-forget in-process push.
- Team-lifecycle orchestration primitives: a code-level done/finished signal (`li team send --kind done|finished`, or the messenger tool's bound actions) that never depends on an LLM formatting JSON correctly, wakeup rounds that inject one bounded follow-up round of operations for workers with unread mail once every live worker has signaled done (capped by `--team-max-rounds`, default 2), and a pure `compute_quiescence` predicate declaring a run settled once every worker is done and no unread mail remains. Team mode also gains in-process messaging: API-model worker branches share an `Exchange` and get a bound `LionMessenger` tool (send/receive) at creation, so peers on the same run can message each other without round-tripping through the file-based team channel; CLI-driven workers keep using that file channel unchanged.
- A unified lifecycle transition service (ADR-0058, phases 1-2): `StateDB.update_status()` and `transitions.transition()` now delegate their guarded read/CAS/edge-validation/history-append algorithm to one shared service (SQLite `BEGIN IMMEDIATE` / PostgreSQL `FOR UPDATE`) backed by a self-validating policy registry covering all seven lifecycle-bearing entity types (session, invocation, show, play, team, schedule_run, dispatch), instead of two independently-drifting implementations of the same guarantee.
- Interoperable external hooks (ADR-0048): tool events now route through the `ActionManager.invoke` chokepoint — `add_tool_pre_hook`/`add_tool_post_hook` run outermost around every tool call the manager mediates (plain function tools, `Tool` objects, and MCP-discovered tools alike), implementing the allow/deny/ask/rewrite decision vocabulary with deny/ask/unrecognized all failing closed. `USER_PROMPT_SUBMIT` joins `TOOL_PRE` as the second blocking hook point, gated by a tri-state turn-origin token that guarantees it fires exactly once per user-originated turn no matter how many internal chat/parse/ReAct calls that turn triggers underneath.
- Studio scheduler platform depth: a claim-time admission seam (ADR-0071 D3) rejects a job over its per-concurrency-key waiter cap or whose declared max duration exceeds the worker's lease TTL, with a submit-time fast-fail path (`AdmissionRejectedError`) mirroring the authoritative claim-time gate; an allow-listed `command` action kind runs templated argv through a re-validated allow-list immediately before spawn (no intervening await can smuggle a revoked command through); a stable execution root now persists per schedule; a nullable `resume_packet` JSON column on `schedule_runs` carries resumable-run metadata; GitHub triggers now expose `head_repo`/`head_repo_is_fork`/`is_same_repo` with a `same_repo_only` filter (fail-closed on a missing/null head repo) and self-report poller health (healthy-age and consecutive-401 count); and per-run coordination telemetry (signals emitted/received/acted-on, plus a files-read overlap indicator) is surfaced in `li monitor` and the run-wait one-liner.
- Opt-in agent-profile role wiring with fail-closed policy binding: a profile's frontmatter `role` key (never defaulted from the profile name) routes `li agent -a <profile>` through `AgentSpec.coding()`'s policy-rendering path, and a role that resolves but has no entry in the active policy pack now raises instead of silently rendering an unconstrained policy block. `AgentSpec.mcp_servers`/`mcp_config_path` are now forwarded into the `claude_code` CLI provider's own per-turn MCP request (previously they only reached `branch.acts`, which CLI providers never consult); `codex`/`gemini_code` legs log a warning and no-op since neither request model carries an MCP field yet.
- A nudge engine with declarative JIT guidance rules and model-operated context-management activation, giving agent runs a rule-based post-hook plane for firing bounded, capped guidance messages (once/cooldown semantics) in response to tool results, independent of any single feature's own hook.
- A pre-turn `ContextProvider` injection seam (ADR-0100): an ordered provider registry on `Branch` renders into the system-guidance fold ahead of chat/run calls, with per-turn budget enforcement (lowest-priority providers dropped first) and full failure containment (a provider exception is reported, never raised, and injected text never enters the durable message record). `KhiveInjectionProvider` implements it against a live khive daemon over the existing MCP transport: it issues a `memory.recall` (+ optional `knowledge.compose`) built from the turn's own rendered instruction and the branch's declared name/role, emits `brain.auto_feedback` in the same round-trip, and an opt-in post-turn writeback hook FIFO-pairs tool errors with their resolving response into a capped, low-provenance `memory.remember`.
- A server-side Studio approval ledger (`approvals` table, CAS `pending -> granted/denied -> consumed/expired`, 5-minute lazy expiry, single-use params-hash matching) with a `/api/approvals/*` route surface; grant/deny routes structurally refuse an operator/service-principal bearer, so an automated driver can never self-approve its own proposed action through the human confirm path. An append-only `approval_evidence` table hash-chains every lifecycle event (`chain_hash = sha256(content_hash + previous_hash)`) with an optional HMAC-signed variant (`LIONAGI_STUDIO_EVIDENCE_HMAC_KEY`) and a `GET /api/approvals/evidence/verify` endpoint that replays and validates the chain.
- A durable task-queue substrate for Studio: `schedule_runs` is generalized into a shared task entity (nullable `schedule_id` for ad-hoc task applications, a widened lifecycle-status set, and queue/lease/concurrency columns), a submit surface writes durable queued rows, and a `workers` registry with a declarative capability-class map (eligibility/serialization/affinity) drives capability-aware claiming with bounded-lease recovery — replacing the prior capability-free-only worker exclusion.
- `lionbench`: a harvested-PR benchmark harness with pluggable agent adapters, a cross-repo instance index (lionagi in-repo source plus lattice bench data pinned at merge commit), M0/M1/M2 arm configs with khive-namespace pinning for injection-enabled arms (so a benchmark run can never contaminate the live posterior store), and additional harvested Python-framework instances. The shared cost meter also gained a fourth pricing dimension for cache-write tokens (previously mispriced into the cache-read bucket or dropped entirely) and launch pricing rows for the GPT-5.6 model tiers.
- Studio scheduler signal bus: typed run/schedule lifecycle signals with per-handler predicates and committed-write minting (a failing predicate can't starve sibling handlers; ordinary handler failures aggregate into an `ExceptionGroup` while cancellation still propagates). A companion casts help-signal escalation protocol adds an urgency-typed (`fyi`/`blocked`) escalation request, with `fyi` routed to a non-terminal notify lane and only `blocked` reaching the terminal escalated lane; the messenger tool gains a fire-and-continue `help` action.
- Studio: file references in rendered agent messages (markdown links and bare inline-code filenames pointing at a known run file) now resolve to clickable in-Studio artifact links, backed by a read-only file-serving endpoint hardened against traversal, symlink escape, and protected filenames.
- MCP tool registration now refuses, at registration time, any discovered/configured tool whose name, description, or input schema exposes a caller-controlled command/process/script executor — across discovery descriptors, raw config dicts, prebuilt `Tool` objects, and the metadata-free `tool_names` shortcut — with no configuration opt-out.
- Model-aware codex reasoning-effort tiers (`max`/`ultra`); `li stats runs`, a read-only aggregate reporting subcommand over the runs substrate (`--since`, `--group-by`, `--json`); and a `bypass` agent-profile frontmatter field (mirroring the existing `yolo` field) so a codex profile can opt into bypass without passing the CLI flag on every invocation.

### Changed

- `AgentSpec.coding(secure=True)`'s built-in guards (`guard_destructive`, `guard_paths`) now register in the same `security_pre` bucket as an explicit `PermissionPolicy`, so they get the same security -> user -> security recheck: a user pre-hook that rewrites a command or path argument into a destructive/out-of-workspace one after the guard already passed is now denied, where it previously slipped through unrechecked. Every security control evaluation (`PermissionPolicy`, the built-in guards, and the session gate) is now expressed as one immutable `GateResult` (`lionagi.agent.gate`); an evaluator that raises unexpectedly now produces a recorded, fail-closed deny instead of an uncaught exception.

### Deprecated

- `Step.request_operative`'s ignored-parameter warning (`parse_kwargs`, `exclude_fields`, `field_descriptions`, `config_dict`, `doc`, `new_model_name`, `parameter_fields`, `request_params`) now names v0.29.0 as the removal target, replacing a stale v0.21.0 promise.
- The free `lionagi.protocols.messages.create_message` function (also exported as `lionagi.create_message`) now emits a `DeprecationWarning` at call time; use `MessageManager.create_message` instead. Behavior and signature are unchanged.
- `lionagi.ln.to_uuid` now emits a `DeprecationWarning` at call time. It is not equivalent to `lionagi.protocols.ids.to_uuid` or `lionagi.protocols.ids.canonical_id`; use `lionagi.protocols.ids.to_uuid` for raw UUID/string values and `lionagi.protocols.ids.canonical_id` for generic Observable-like objects.
- `lionagi.cli._runs.teardown_orchestration_persist` is now an async wrapper that emits a `DeprecationWarning` and delegates unchanged to `teardown_persist`; use `teardown_persist` instead.
- `lionagi.cli.orchestrate._common.TEAM_WORKER_SYSTEM` is deprecated; use `TEAM_COORD_SECTION` appended to a worker's own system prompt instead.
- `lionagi.operations.select.select.select` now emits a `DeprecationWarning` at call time; use `Branch.operate()` with an explicit caller-owned response model instead. Behavior and signature are unchanged; `select_v1` remains undeprecated for internal use.

### Removed

- Studio's operator panel and its abandoned Designer page (`routes/designer.tsx`, `WorkflowDesigner`, and the nav/command/rail entries and dead i18n keys pointing at them) have been removed; the shared flow-canvas editor those pages used is still reachable (and unaffected) from the Library page's inline workflow viewer.
- Internal dead code: the AST filesystem-scan class registry (an import-time `os.walk` + `ast.parse` over `protocols/{generic,graph,messages}`) is gone in favor of a direct registry lookup with a dotted-path-import fallback, and the unused `can_transition` lifecycle helper has been dropped.

### Fixed

- `guard_paths()` now delegates workspace containment to the canonical `lionagi.libs.path_safety.resolve_workspace_path` helper instead of its own parent-walk check: a direct symlink under an allowed root is refused before it is followed (even when its target is inside the workspace), an intermediate directory symlink that resolves outside the workspace is denied on containment, and a fixed set of protected basenames (`.env`, `.netrc`, SSH private keys, `.htpasswd`) is denied even when the caller supplies no `denied_paths`. This closes a gap where only caller-supplied deny rules were enforced and symlink escapes were not checked at all. Multi-root and custom `denied_paths` composition, the `guard_paths()` signature, and deny-only behavior when no `allowed_paths` are configured are unchanged.
- The same canonical path-safety containment is now applied uniformly to `create_path`, sandbox inputs, and artifact file references — closing the same class of symlink-escape and traversal gap on surfaces outside `guard_paths()` itself.
- Run-terminal notification reliability: terminal notify now fires even when invocation finalize raises, instead of being skipped; a worker-liveness watchdog now fails a flow loudly instead of hanging it indefinitely behind a zombied worker process.
- Team-lifecycle correctness: round-2 wakeup operations now carry proper attribution and thread context, the team-mode coordination prompt is reconciled with the actual delivery channel, and a stopped `Exchange.run()` now raises instead of silently no-op'ing.
- Hook-chokepoint correctness: the `USER_PROMPT_SUBMIT` guard now runs before context-provider side effects fire (a rejected prompt leaves no persisted instruction and no provider side effects), a tool post-hook's reason notes are surfaced instead of discarded, a tool-pre-hook denial is now captured as a `FAILED` event with post-hooks skipped during cancellation, and preprocessor-added keys survive post-preprocessor revalidation instead of being dropped.
- State and scheduler reliability: `dispatched_at` is backfilled for pre-existing running `schedule_runs` rows, chain-child rows are excluded from `schedule_run_exists_since`, the working GitHub poll token is now cached instead of shelling out to `gh` on every tick, `list_schedules` N+1 queries are batched, `branches.status` is finalized at teardown via the `BRANCH_END` hook, the WAL is checkpointed before a rebuild backup with a hardened `transition()` column allowlist, and schedule occurrence recording/recovery-scan/reaping is now atomic.
- Sandbox worktree safety: `create_sandbox` now records the base branch's commit SHA at creation time; `sandbox_diff` no longer stages anything or otherwise mutates the worktree index while computing a diff; `sandbox_merge` refuses to run against a detached HEAD or a `repo_root` not checked out on the session's recorded base branch, and refuses to merge into a protected branch name (`main`, `master`, `release*`) without an explicit `allow_protected=True`; `sandbox_discard` only marks a session inactive once both the worktree removal and branch deletion actually succeed.
- Branch snapshots (both the pre-stream and post-turn writers) are now written atomically via a sibling temp file plus `os.replace()`, so a process kill mid-write leaves the prior complete snapshot intact instead of a torn, unparseable one that made the branch unresumable.
- Telemetry and provider-failure reliability: token/cost capture is now wired for the `claude_code` and `codex` CLI paths (previously only `gemini-cli` re-surfaced real usage, so `total_cost_usd` silently read `0.0` for every other CLI session); provider failures are now classified into retryable/non-retryable reason codes instead of one generic bucket; the OpenAI system-to-developer role rewrite is now gated by model family; and custom provider calls route through the native retry path.
- Orchestration and CLI planning fixes: `flow.py`'s `plan()` now enforces its `max_tasks` constraint with a backstop raise (an over-limit plan previously escaped as a bare, uncaught `ValueError` instead of the CLI's normal `FlowPlanError` exit path) and the planner prompt now explicitly maximizes DAG width instead of collapsing into a near-linear chain; a reactive flow resume can now replay reactively spawned nodes; a reactively spawned node's `spawn-N` id is now stamped at construction time (previously minted post-run by completion order, so an unrelated sibling could "become" spawn-1) and told its own required artifact directory before it runs; per-role mode allowlists now surface correctly in the planner's mode roster instead of silently dropping at execution; tool docstrings no longer drop operational content meant for the LLM; `li agent` fails fast on a nonexistent `--cwd` before spawning; and the plain-profile system prompt reapplies correctly on resume/continue.
- Studio run and session observability: `NodeEscalated` now routes as a non-terminal notify (not a terminal state); truncated run-file reads correctly distinguish a boundary-split UTF-8 character from binary content; the fleet run-detail execution graph falls through gracefully on an edgeless graph with a more compact MiniMap; run-DAG edges are transitively reduced with honest node status; fleet page legibility is improved (day-scale durations, labeled counts, tool-row output previews); and session message polling no longer binds every progression id as its own SQL bind variable.
- Studio frontend resilience: stale JS chunks are excluded from the SPA rewrite and the page reloads once on a chunk-load failure; a hosted API base is now injected on Vercel builds so the local-first SPA still works; a wrong-app-on-port condition is now distinguished from an unreachable daemon; and chat nodes now apply `config.model` and reject bare model strings instead of silently misapplying configuration.
- Message and action rendering correctness: the per-message render cache is now bypassed for content holding untracked-mutable objects, and multi-turn result-chunk deltas are summed instead of overwriting each other.
- `soupsieve` bumped from 2.8.3 to 2.8.4, addressing published ReDoS and memory-exhaustion advisories.

### Performance

- Flow execution hot path: adjacency edge lookups, a predecessor cache (moved onto `Graph` itself), and an `alcall` fast path speed up DAG execution; an identity-keyed cache for `Operative` model types avoids rebuilding structurally-identical response models on repeated calls.
- Messaging and state hot path: a revision-keyed per-message render cache avoids re-rendering unchanged prepared history; live message persistence is now bundled into a single transaction instead of one write per message; and a session's action-message stats query now forces a primary-key probe instead of a slower scan.
- CLI and service startup: the command registry and `lionagi.state` export are now lazy-loaded, and `EndpointConfig` kwarg validation no longer rebuilds its JSON schema on every call.

## [0.28.0] - 2026-07-08

### Added

- `lionagi.operations.lndl_middle` — an LNDL seam `Middle`, importable from the package itself (`from lionagi.operations.lndl_middle import lndl_middle, build_lndl_middle, DEFAULT_ROUND_BUDGET`): `lndl_middle` (ready to use) and `build_lndl_middle(round_budget=3)` (custom round budget). Opt in per call with `branch.operate(instruction=..., middle=lndl_middle)`; advances one LNDL round per inner chat call, renders the target model's field names into round-1 guidance (native `response_format` is stripped from the per-round chat call, so the model is told what to fill via an LNDL `Specs:` line instead), bridges `<lact>` calls through the branch's normal `act()` path (existing permission policies and hooks apply unchanged), and classifies each round into a `RoundOutcome` (`Success`, `Continue`, `Retry`, `Exhausted`) to drive repair — a parse/assemble/validation failure re-prompts the model with the typed error instead of surfacing a raw traceback. Round-budget exhaustion raises `LNDLError` rather than returning a raw error string or `None`.
- `lionagi.lndl.build_action_call` — parses a lact node's call text into an `ActionCall` placeholder; factored out of `assembler._alias_value` so callers (like the new LNDL middle) can build placeholders for lacts with no `OUT{}` to gate against yet.
- Studio scheduler — metric threshold alerts: a schedule carrying a `threshold_config` (`{"metric": "failed_sessions" | "total_cost_usd" | "p95_latency_ms", "op": "gt" | "gte", "value": N, "window_minutes": N}`) evaluates the metric on each tick of its own cron/interval cadence and only fires its action when the threshold is breached, with a cooldown of `window_minutes` so a sustained breach alerts once per window instead of every tick. Authorable via `li schedule create --threshold-config '<json>'`.
- Studio scheduler — per-schedule token/spend budgets (`--max-cost-usd`, `--max-tokens`) and a daemon-wide global concurrent-fire cap, so scheduled work cannot exhaust budget or overwhelm the host.
- Studio scheduler — GitHub triggers are now authorable directly from `li schedule create --trigger-type github --github-repo OWNER/NAME --github-filter '<json>'`, fire once per GitHub event (not once per poll), support a `pr_merged` mode that dispatches on merge, and filter on `draft` state; `head_sha` and `draft` are emitted on each event.
- Studio workflows — `WorkflowDef`s compile and run on `Session.flow`, emit per-node lifecycle signals for live run observability, carry many-to-many run tags with tag filtering, support condition authoring on workflow edges in the designer, and split `playbook` into its own library kind alongside built-in playbook templates surfaced on the Workflows page.
- Studio UI — a three-question Mission Control overview, schedules table and card views, and internationalization covering the top-16 world languages with RTL support.
- `EngineResult` and a budget-degrade contract for engines, so a run that exhausts its budget degrades to a typed result instead of a bare failure.
- Reactive flows surface dropped `SpawnRequest`s in the flow result for observability instead of discarding them silently.
- `li agent --context-from` for cross-agent context handoff, and `li studio` frontend modes (`--web` hosted default, `--docker`, `--no-frontend`).

### Changed

- `lionagi.lndl.assembler.assemble()` now raises `MissingLvarError` when `OUT{}` references an alias that's declared nowhere (previously the field was silently dropped from the assembled dict); an alias executed in an earlier round now resolves via `action_results` without redeclaration, so a later round's `OUT{}` can reference it directly.
- `lionagi.lndl.assembler.assemble()` now raises `MissingFieldError` when the target model has required fields absent from `OUT{}`, rather than deferring the failure to a downstream `pydantic.ValidationError` with no LNDL-level context.
- Studio run and verdict state derivation is unified on a single code path (the standalone verdict sniffer is gone), so run status and verdict are reported consistently across the run list, run detail, and the run DAG.

### Deprecated

- `li studio --no-docker` — the Docker frontend is now selected explicitly with `--docker`; the hosted `--web` frontend is the default.

### Removed

- `lionagi.lndl.MissingOutBlockError` and `lionagi.lndl.AmbiguousMatchError` — retired, unused error classes. A response with no `OUT{}` block is classified as `Continue` (the model is still thinking), not an error; ambiguous-match scoring was never wired to a real assembly path.
- A sweep of internal dead code with no public surface: a deprecated `protocols` structure re-export shim, the unused `AccessError` and hook `registered_handlers`, three unused CLI helpers, `providers/config.py`, the studio `status_mapping.py` and a runs read-path adapter, and a dead state `persist.py` module with its legacy reason-code resolver.

### Fixed

- Studio scheduler GitHub triggers no longer collapse a burst of merged PRs into a single fire or lose events behind pagination: polling returns structured per-event items, dispatches each exactly once, advances its cursor only past events it actually dispatched, and bounds merged-PR pagination so the cursor can never move past an unfetched merge (no permanent skips, no duplicate reviews). A deleted action working directory now fails the run with a clear reason instead of a generic spawn error. A GitHub `401` (typically a `GITHUB_TOKEN` that was valid at daemon launch but has since expired) now falls through to a fresh `gh`-CLI token and retries once, instead of pinning the poller to the dead credential and going silently blind on every subsequent poll; a 401 that survives the retry logs at ERROR.
- Studio run and session observability corrections: run-health and status misreporting fixed across the app, phantom `failed`/terminal classification gated behind a liveness-and-staleness check, stale in-flight play rows and null-status rows reaped to a terminal state on runner death (honoring the recorded pid and a staleness grace), engine sub-agent branches and chat-node turns persisted, and run-DAG edges rendered from `depends_on`/`parent_id`.
- Studio run and session detail read-paths are paginated with corrected message and action aggregates; long message lists are windowed and no longer flash on update.
- Studio frontend clears a misleading error when a static SPA rewrite masks a missing API base, and ships a frontend backlog cluster of redirect, API-base, pulse, overflow-accessibility, and calendar fixes.
- State and scheduler terminal-status-integrity floor: a cluster of terminal-transition correctness issues resolved so a terminal status is authoritative and cannot be silently overwritten.
- `li agent` resolves a bare model name to its provider from settings and surfaces total sub-agent failure instead of reporting a false success; `li agent --resume` and auto-resume no longer race the terminal-status guard.
- The NLIP provider's retries route through `retry_with_backoff` (the two retry loops are deduplicated), and reactive `Pattern.load` errors list the valid roles and modes instead of failing opaquely.
- Path-traversal checks across the codebase are consolidated onto a single `has_traversal` helper.

## [0.27.2] - 2026-07-02

### Added

- `SESSION_START`, `SESSION_END`, and `BRANCH_CREATE` hook emissions wired in `lionagi/cli/_runs.py`. The built-in handlers in `lionagi.hooks.builtins` (`persist_session_start`, `persist_session_end`, `persist_branch_provenance`) are now called at the correct lifecycle moments. Both `persist_session_start` and `persist_session_end` guard against double-fire: a second emit for an already-started or already-terminal session is a no-op and does not insert a duplicate `status_transitions` row.
- `log_tool_call` in `lionagi.agent.hooks` and `lionagi.hooks.builtins` — canonical name for the tool-call observability post-hook, replacing `log_tool_use`. Also name-addressable via `lionagi.hooks.loader` registry as `"log_tool_call"`.
- `lionagi.testing` is now documented as a supported public surface. Register `lionagi.testing.pytest_plugin` in your `pytest_plugins` to get the bundled fixtures.
- `StateDB` is now backend-pluggable on a single SQLAlchemy-Core implementation: the default embedded SQLite backend needs no configuration, and an optional PostgreSQL backend is available via `pip install lionagi[postgres]` with a `postgresql+asyncpg://…` URL.
- `li agent` gains `--prompt TEXT` and `--prompt-file PATH` (`-` reads stdin) for scripted callers, alongside the existing positional prompt.

### Changed

- The Gemini CLI provider now targets Antigravity (`agy`), Google's successor to the standalone Gemini CLI, via its headless JSON print mode. Streaming persistence reaches parity with the codex and claude CLI providers: the conversation id is captured as the session id (enabling native resume) and usage, turn count, and duration are recorded.
- Default Gemini models track the newest families served by `agy`: `gemini-3.5-flash` (default) and `gemini-3.1-pro`. Legacy preview names remain aliases.
- The bare `sonnet` model alias now resolves to `claude-sonnet-5`.
- `li agent` argument parsing now accepts flags before the prompt (for example `li agent codex/model --effort high "prompt"`); previously a flag ahead of the prompt raised an "unrecognized arguments" error.
- `sqlalchemy[asyncio]>=2.0.0` is now a core dependency — the `StateDB` state layer (used by `li agent`, `li play`, and the lifecycle hooks) runs on SQLAlchemy-Core for both the SQLite and PostgreSQL backends. The `[postgres]` extra now carries only the PostgreSQL driver stack (`pydapter[postgres]`, `asyncpg`).
- `EndpointConfig` gains a `serialize_by_alias` flag (default `False`); the Exa and Firecrawl endpoints set it to `True`, removing six identical per-file `create_payload` overrides whose only purpose was alias-serialization.
- Provider endpoint files no longer resolve `api_key` individually; `EndpointMeta.create_config` reads the appropriate settings field automatically when `api_key_env` is declared on the provider config, removing 21 identical boilerplate blocks while preserving per-provider env-var names and the Ollama no-key path.
- Studio engine-runs service (`lionagi.studio.services.engine_runs`) now delegates to `StateDB.list_engine_runs` / `StateDB.get_engine_run` instead of duplicating raw SQL; fresh-DB empty-list behaviour is preserved by an upfront path-existence guard.
- The CLI, studio, and operations boundaries now raise typed `LionError` subclasses (`ConfigurationError`, `OperationError`, `ExecutionError`) instead of bare `ValueError`/`RuntimeError`. These subclasses also inherit from the corresponding builtin, so existing `except ValueError` / `except RuntimeError` handlers keep working.
- Studio services `engine_runs`, `sessions`, and `invocations` no longer import `fastapi.HTTPException` in their logic functions; not-found conditions raise `NotFoundError` (status 404) instead, and the `app`-level `LionError` exception handler translates domain errors to HTTP responses at the route edge.
- `lionagi.hooks.builtins` persistence handlers now share a single open `StateDB` connection per DB path (via `get_shared_db()`) instead of opening a fresh connection on every hook firing, eliminating the per-firing connect + pragma + schema-check cost.

### Deprecated

- `log_tool_use` in `lionagi.agent.hooks` and `lionagi.hooks.builtins` — use `log_tool_call` instead. Will be removed in a future minor release.
- `CLIEndpoint` in `lionagi.service.connections` — use `AgenticEndpoint` instead. The alias now emits `DeprecationWarning` at import. Will be removed in a future minor release.

### Removed

- The unused `lionagi.outcomes` package (dead code, never part of the public surface) has been removed.
- `request_fields` phantom param dropped from `MessageManager.create_instruction`, `create_message`, and `add_message` (was accepted but silently ignored); `parse_lndl_fuzzy` removed from `lndl.__all__` (Phase-2 feature, not yet shipped).
- Internal `_get_oai_config` alias in `lionagi.testing._legacy` removed (was never public; callers within the module now call `oai_chat_endpoint_config` directly).
- `LogManager`, `LogManagerConfig`, and `to_list_type` removed from the public `lionagi.protocols.types` surface (back-compat aliases and an internal Pile helper that inflated the curated re-export set); import them from their canonical modules `lionagi.protocols.generic.log` and `lionagi.protocols.generic.pile` instead.

### Fixed

- A degraded CLI termination (for example a timeout that arrives after a complete final message) no longer discards the delivered response or surfaces successful output as an error. `li agent` flushes already-streamed text into the transcript before raising, and the agy wrapper leads error chunks with the terminal status instead of impersonating the response as the error message.
- Python 3.14 compatibility: `FieldModel.annotated()` and `Spec` materialized `Annotated` types via the `Annotated.__class_getitem__` attribute, which 3.14 removed from typing special forms (raising `AttributeError: __class_getitem__`). Both now use canonical subscription `Annotated[tuple(args)]`, valid on 3.10–3.14.
- CI now tests every Python version in the matrix. A committed `.python-version` was overriding the matrix interpreter for `uv venv`/`uv sync`/`uv run`, so every leg silently ran on 3.10; the `test` job now sets `UV_PYTHON` to the matrix version.
- `ReactiveExecutor.execute()` / `execute_stream()` subscribe to the bus via the public `session.observer` property instead of the private `_observer` attribute, which is `None` until first accessed — reactive `SpawnRequest` events were liable to be silently dropped.
- `Engine.cancel_active()` now bounds task cleanup with a configurable `cancel_timeout_s` (default 30s): non-cooperative tasks that ignore cancellation are abandoned with a warning instead of hanging the run indefinitely.
- `li kill` recognizes shebang-launched console-script runs (where `argv[0]` is the Python interpreter and `argv[1]` is the `li` script path).
- `li agent --resume` fails loudly (non-zero exit) when a resume produces an empty stream — typically an expired session — instead of exiting `0` silently.
- Studio `PUT /engine-defs/{id}` returns `404` only when the definition is absent, not when the request body is empty (an empty patch is now a no-op that returns the existing definition).

## [0.27.1] - 2026-06-16

The studio desktop substrate (Vite SPA + Tauri 2 macOS shell), custom engine
definitions driven from the frontend, a hardened launch API, and the
`AgentConfig` removal — fourteen commits since v0.27.0.

### Added

- **Studio desktop substrate** — Vite SPA migration with single-process serving
  (#1430) and a Tauri 2 desktop shell for macOS (#1431).
- **Studio custom engines** — create, save, and launch custom engine definitions
  from the frontend (#1441).
- **Studio launch API** — `POST /api/launches` fires runs through the
  scheduler's hardened spawn path (#1434).
- **Casts catalog** — read-only catalog API and CLI (#1433).
- **Live run streaming** — persist engine-run signals for live Studio streaming
  (#1432).

### Changed

- **Python 3.14 compat** — bump pyupgrade to v3.21.2 (#1442).

### Fixed

- **CI diagnostics** — fail fast with the crashed test's name when an xdist
  worker dies (#1437).
- **Studio frontend** — restore Tailwind content paths after the Vite migration
  (#1435).

### Removed

- **`AgentConfig`** — the deprecated `lionagi.agent.config` module is removed
  (#1449). Use `AgentSpec` (`lionagi.agent.spec`) instead:
  `AgentSpec.compose(role, ...)` for the general case and
  `AgentSpec.coding(...)` for the coding preset. `create_agent()` now takes an
  `AgentSpec`. The `HooksMixin` and secure-guard wiring previously in
  `config.py` now live in `spec.py`.

### Docs

- Trim verbose docstrings and comments across the codebase and relocate
  load-bearing reference material to `docs/` (#1450, #1451, #1452).

## [0.27.0] - 2026-06-11

Phase C control center (engine runs, CLI coding presets, studio maintenance),
a studio lifecycle-signal layer with live Kanban/playfield views and en/zh
i18n, inline-flow scheduling, the ADR-0079..0082 orchestration substrate
cluster, and an eight-issue security/bug discovery sweep.

### Added

- **Phase C control center** — `li engine run` + `engine_runs` visibility
  (#1415); `li agent --preset coding` + `--form spec.yaml` (#1418);
  `POST /api/admin/maintenance` (vacuum / checkpoint / prune) with page
  wiring (#1417).
- **Scheduler inline flow** — `flow_yaml` schedule action kind runs an inline
  YAML flow (#1265).
- **Studio lifecycle & live views** — canonical lifecycle-signal contract +
  lane projection (#1396); SSE lifecycle-signal stream + run event log (#1403);
  self-healing session/invocation lifecycle reapers (#1260); Kanban lifecycle
  view over run status (#1268); cross-project live playfield page (#1266);
  action panel, filter counts, play-graph color/click/zoom + a11y perf
  baseline (#1258).
- **Internationalization** — next-intl pipeline with en/zh catalogs + locale
  switcher (#1401); page conversions for kanban/playfield/dashboard + nav
  keys (#1402).
- **Orchestration substrate cluster** — ADR-0079..0082 + routing slice (#1267).
- **Work system re-cut** — `WorkForm` + `Rule` / `RuleSet` (#1386).
- **Coding-agent feedback surface** — guidance + ruff diagnostics (#1400).
- **Class-registry test suite** (#1375).

### Changed

- **Engine layering** — extract `ChainRun` intermediate base + research
  per-stage repair (#1385).
- **Module relocation** — `protocols/structure` → `operations/schema` (#1378);
  consolidate `Operative` construction + document HookPoint dispatch (#1377);
  split CLI spec-validation and state-db into dedicated modules (#1376); dedup
  `CodingToolkit` schemas to the tools/file canon + add the missing open
  action (#1372).
- **CI** — serialize gh-pages deploys with a concurrency group (#1395).

### Fixed

- **Registry fallback** — `LION_CLASS_FILE_REGISTRY` fallback now actually
  loads classes via package-context import, with a package-boundary guard
  against path escape (#1422).
- **Provider / engine diagnostics** — typed provider errors + emission
  diagnostics surface CLI worker failures (#1419); export a partial report on
  budget/deadline cancellation (#1399); deadline watchdog, normalize-before-gate
  and CLI-aware emission repair (#1371); gate runs on workspace ground truth
  despite implementer emission failure (#1365); fan collected chain events to
  `on_event` once across engines (#1362); honor `backoff_factor` in retry
  (#1370).
- **CLI providers** — map gemini NDJSON tool payloads + assistant answer into
  session state (#1390); gemini-cli env trust, oauth model set, clearer
  endpoint errors (#1374); codex surfaces the top-level error message instead
  of an empty-dict `str()` (#1388).
- **Session** — emit run lifecycle signals from `Branch.run`, fix ReAct
  double-wrap, fix resume empty stream (#1373).

### Security

- **Studio** — startup warnings for unauthenticated mode + a bounded CORS
  method allowlist derived from the route table (#1423).
- **Scheduler hardening** — reject flag injection in
  `action_model` / `action_extra_args` (#1414); validate `github_repo` format
  before GitHub API URL construction (#1420).
- **Bash tool** — remove the `allow_shell` bypass to close CWE-284 (#1421).
- **Dependencies** — urllib3 2.6.3→2.7.0, authlib 1.7.0→1.7.2 (#1384);
  codecov-action 6→7 (#1368); actions/cache 4→5 (#1369).

## [0.26.18] - 2026-06-09

Engine layer expansion — hypothesis and coding engines with autonomy
protections (ADR-0077), the casts module-coherence pass (ADR-0078), a
15-PR bug/security sweep, and a codebase-wide trim/consolidation.

### Added

- **HypothesisEngine** — hypothesis-driven development engine (Chain shape):
  frame → question → evidence → analyze, with per-run chain export (JSON +
  markdown evidence files). (#1358)
- **CodingEngine** — gated plan/implement/test/fix loop with a ground-truth
  subprocess test runner (pass = exit code 0, never an LLM claim), judge-gated
  fix rounds, and a `to_hypothesis_seeds` bridge into HypothesisEngine. (#1358)
- **Engine autonomy protections** (ADR-0077) — run-level resource budgets,
  judge gates, emission repair/retry for weak models, per-stage model routing,
  generation caps. (#1358)

### Changed

- **Module coherence pass** (ADR-0078) — normative casts model (pattern /
  role / mode / profile are configuration; agent is a runtime concept):
  single capability-grant source via `AgentSpec.emits`; CLI orchestrator and
  casts-role workers route through `create_agent` (one construction stack);
  provider tables moved down to `service/providers.py` and path constants to
  `lionagi._paths` (no more upward imports into `cli`); `AgentConfig`
  deprecated by delegation onto Profile/AgentSpec; plane-distinct vocabulary
  (`outcomes.Finding` → `ReviewFinding`, `ReviewVerdict` → `ReviewOutcome`);
  curated `casts` public surface; `EngineEvent` forbids extra fields. (#1358)
- **Docstring/comment trim** across session, models, service, providers,
  adapters, cli, operations, state, studio, hooks, engines, casts, tools,
  libs, config. (#1340–#1344)
- **Utility consolidation** — shared path-safety, `_io`/`_subprocess`, and
  NDJSON/CLI subprocess primitives; `ln.concurrency` adoption; render
  consolidation; CLI lifecycle/concurrency primitives + `last_response`.
  (#1345, #1346, #1351–#1355)
- **CI** — coverage on one version, uv caching, perf-test gating. (#1349)

### Fixed

- **ModelParams global cache** cross-wired distinct types — dropped. (#1356)
- **Pile set ops** — `items=` kwarg and `dict_values` crash. (#1316)
- **`iModel(model=...)`** resolves default provider from settings. (#1317)
- **PyYAML as core dependency** — `lionagi.agent` imports on base install.
  (#1315)
- **Studio run detail** reads StateDB like the list endpoint (was silently
  null for post-migration runs). (#1358)
- **ReviewEngine** uses structured concurrency instead of `asyncio.gather`.
  (#1322)
- lndl bool coercion (#1320), `CommonMeta` key-presence validation (#1325),
  `fuzzy_match_keys` Sequence handling (#1318), StateDB orjson serialization
  (#1326), `guard_destructive` default hook in coding presets (#1324), ruff
  format gate stragglers (#1314).

### Security

- **Studio API auth** — `GET /api/invocations` and `/api/sessions` gated
  behind bearer token. (#1319)
- **Docker symlink mounts** constrained to an allowlist. (#1323)
- **SSRF guard** — local-address allowlist for Ollama loopback endpoints.
  (#1327)
- **Credential redaction** across all URL schemes and nested dict details.
  (#1321)
- **Agentic-CLI path grants** validated against repo containment. (#1328)

## [0.26.17] - 2026-06-07

Security-hardening pass: fail-closed boundaries across MCP transports, file/exec
paths, and CLI providers, plus SSRF and credential-leak guards — alongside
the #1257 bug sweep.

### Security

- **Fail-closed MCP transports** — full inline MCP transport now fingerprinted
  into the policy key (closes a trust-leak where differing transports could share
  a policy entry); MCP transports fail closed under threaded access, with
  rate-limit deferral and processor-join fixes. (#1285, #1279)
- **Image-URL SSRF guard** — outbound image-URL fetches are validated and
  message construction hardened. (#1280)
- **Fail-closed path/exec boundaries** — tool path and exec boundaries reject on
  ambiguity instead of proceeding. (#1281)
- **CLI provider validation** — async correctness fixes plus fail-closed CLI path
  validation. (#1278)
- **Auth gating + credential redaction** — agent auth gating, spawn-constraint
  enforcement, and credential redaction across adapters/models. (#1282)
- **Artifact auth + input validation** — status-history integrity, artifact
  authorization, and input validation across state/cli/studio. (#1283)
- **Dependabot** — resolved 6 dependency vulnerabilities. (#1274)

### Fixed

- **Bug sweep** — pi-CLI parser events, SIGINT reason code, `li kill`
  PID-identity guard against recycled PIDs, lndl export. (#1257)
- **`os.killpg` on non-Unix** — guarded for platforms without process groups.
  (#1286)

### Changed

- **`lionagi/` ruff gate enabled** plus Studio UI alignment. (#1284)
- Ruff format + mechanical lint cleanup, no behavior change. (#1277)

### Docs

- README CLI section, ADR taxonomy, deprecation purge, migration-guide extract,
  DeepWiki integration. (#1259)
- Forbid internal audit/review references in committed code comments. (#1287)
- Align AGENT.md + CONTRIBUTING.md with actual tooling. (#1275)

## [0.26.16] - 2026-06-06

### Fixed

- **Codex silent failures** — `turn.failed`/`error` events were swallowed;
  now yield `StreamChunk(type="error")`. (#1272)
- **Codex `fast_mode`** — `service_tier=flex` → `service_tier=fast`. (#1272)

### Changed

- **Unified CLI provider types** — remove `ClaudeChunk`/`CodexChunk` and
  `ClaudeSession`/`CodexSession`; shared `CLISession` + `StreamChunk`
  passthrough. −255 lines. (#1272)

## [0.26.15] - 2026-06-02

Reactive orchestration, domain engines, observer-as-hook-transport.

### Added

- **Reactive self-expanding flow** — `SpawnRequest` injects nodes into a
  running DAG; CLI rewired onto casts + emissions.
- **Domain engines** (ADR-0075) — `PlanningEngine`, `ResearchEngine`,
  `ReviewEngine`; `li o flow` routes through `run_dag`.
- **Observer-as-hook transport** (ADR-0076) — emission/control/lifecycle
  extracted to `_observe.py`; API-model agents drive the bus.
- **Hook bus persistence** (ADR-0023b), pre-invoke governance gate,
  `li o flow --workers`, compositional filter DSL.
- **SWE-bench harness** — real instances, deterministic oracle, blind
  judge, per-dollar verdict.
- **Daytona sandbox** — isolated containers + context tool + tool guidance.

### Fixed

- **Roled agents lost tool-use** — `with_updates` dropped response schema.
- **Studio show-detail 404** in Docker + stale frontend build.
- **Daytona shell injection** — cwd now shell-quoted.
- **Codex `skip_git_repo_check`** defaulted to `True`.

## [0.26.14] - 2026-05-30

### Fixed

- **Dockerfile CMD target** — Docker image CMD still referenced the old
  `apps.studio.server.app:app` after the backend moved to `lionagi/studio/`
  in v0.26.13. Updated to `lionagi.studio.app:app`. Also added
  `MARKETPLACE_MANIFEST` fallback for pip-installed environments.
- **Dropped `[nlip]` optional dep group** — removed from pyproject.toml;
  users who need nlip install `ag2[nlip]` directly. Starlette pinned
  `>=0.46.2,<1.0` for FastAPI 0.115 compat.

## [0.26.13] - 2026-05-30

Universal AgentSpec, inline emission contracts, loop control, Studio in-wheel, and a large CLI/Studio bug-fix sweep.

### Added

- **Inline-Python roles/modes + emission contracts** — roles and modes are now a
  closed, built-in set defined in Python (`casts/roles/*.py`, `casts/roles/modes/*.py`),
  replacing the `.md` files and the string-keyed `ROLE_CAPABILITIES` dict. Each
  `Role` co-locates its emission contract (`emits`) with a 22-model emission
  ontology in `casts/emission.py`. Bad references are import errors, not silent
  `None`. (#1224)
- **Universal AgentSpec** — `AgentSpec` replaces `AgentConfig` as the primary
  agent creation surface. Composes `Profile` (frozen `Role + tuple[Mode]` with
  mode conflict detection) + model/tools/permissions/pack/emission grant.
  `from_legacy()` bridge preserves backward compatibility. Pack policy block
  rendered into system prompt. (#1227, closes #1212, #1213)
- **Loop control** — `LoopDirective` enum (CONTINUE/CANCEL/BREAK),
  `Branch.control()`/`poll_control()` one-shot API, `_check_control` seam in
  the `run()` stream loop. Observers can cleanly stop a running stream. (#1226)
- **`li schedule` CLI** — `li schedule list/get/create/enable/disable/trigger/
  delete/runs` wired to Studio REST API. Stdlib `urllib` only — no new deps.
  (#1218, closes #1165)
- **`li play` heartbeat/watchdog** — per-op heartbeat every 60s so flow.log
  never goes silent; 10-min idle stall warning; smart staleness for
  `li kill --all-stale` (child-derived sweep for plays/shows). (#1217,
  closes #1150, #1144)
- **`li play --help` common flags** — shows `--bypass`, `--team-mode`,
  `--timeout`, `--save`, `--cwd`, `--effort`, `--yolo`. (#1218, closes #1194)
- **Observe-by-role** — `RoleFilter` and `role=` keyword on
  `SessionObserver.observe`/`Session.observe`; subscribe to "anything emitted by
  role X" without enumerating types. `Signal.emitter_role` field. (#1219,
  closes #1208)

### Changed

- **Studio backend moved into `lionagi/studio/`** — ships with the Python wheel;
  `pip install lionagi[studio]` now includes the backend. `li studio` uvicorn
  target updated. Frontend stays in `apps/studio/`. (#1228, supersedes #1201)
- **Non-blocking observer dispatch** — `_emit_message_signal` scheduled as
  background `asyncio.Task`; all tasks drained in `finally`. Async handlers in
  `observer.emit()` run concurrently via `ln.gather` (structured concurrency)
  instead of serially. (#1219, closes #1214)
- **CLI orchestration wiring** — `FlowAgent` gains `modes`/`permissions`;
  planner roster built from casts ontology; plan validation for unknown
  roles/modes/permission presets; permission translation for claude_code
  provider. (#1227)
- **Dropped `[nlip]` optional dependency group** — `ag2[nlip]` removed from
  optional deps and `[sandbox]`; users who need nlip install `ag2[nlip]`
  directly. Starlette pinned `>=0.46.2,<1.0`. (#1218, closes #1133)

### Fixed

- **Codex stdin hang** — both codex and claude_code subprocess providers now
  redirect `stdin` to `DEVNULL`, preventing fd contention when multiple agents
  run concurrently. (#1216, closes #1158)
- **`--bypass` silently ignored** — `bypass=True` now injects provider-specific
  kwargs (codex: `bypass_approvals`; claude: `permission_mode`). (#1216,
  closes #1158)
- **Timeout discards partial output** — `_extract_partial_output()` preserves
  the last assistant message on hard timeout. (#1216, closes #1152)
- **Progress heartbeat** — 60s heartbeat when `--timeout` is set. (#1216,
  closes #1154)
- **`response_format` serialization** — `InstructionContent.to_dict` now
  includes `response_format` when it's a plain dict. (#1219, closes #1160)
- **Studio API_BASE 404** — `resolveApiBase()` fallback changed from `""` to
  `http://localhost:8765`. Fixes empty playbooks/skills pages. (#1220,
  closes #1215, #1157, #1156)
- **Studio MODEL column** — runs table cell count matched to header; Model
  column now renders. (#1220, closes #1167)
- **Studio dashboard staleness** — stale card hint corrected; show-status synced
  from filesystem; orphaned Health column removed; Projects breadcrumb and
  favicon fixed. (#1221, closes #1162, #1161, #1176, #1168)
- **`li monitor` type filter** — `--type play` now queries sessions with
  `invocation_kind='play'`; AGENTS column shows actual branch count;
  play↔monitor correlation via pre-generated UUID. (#1223, closes #1192,
  #1193, #1191)
- **Studio cwd** — `li studio` works from any directory (superseded by
  in-wheel move). (#1218, closes #1201)
- **Codex `service_tier`** — changed from `priority` (unsupported, caused
  hangs) to `flex`.

## [0.26.12] - 2026-05-29

Reactive capability bus + casts Role/Mode composition.

### Added

- **Reactive capability bus** (ADR-0072) — an agent's turn becomes an
  observable, typed event stream. A *capability* is a named, typed
  structured-output field an agent may emit inline as a fenced ```json block in
  its ordinary text; a `SessionObserver` dispatches each to handlers in real
  time, mid-run, without a dedicated emit tool. Built on lionagi's own
  primitives (`Element`/`Pile`/`Flow`/`Observer`). (#1204, #1206, #1207)
  - `Signal` / `StructuredOutput` envelopes, plus run-lifecycle
    `RunStart` / `RunEnd` / `RunFailed` signals.
  - **Filter DSL** (`lionagi.ln.types`): `TypeFilter`, `SpecFilter`,
    `FieldRef` (via `Spec.q`), composable with `& | ~`. Subscribe by type
    (`session.observe(Finding)`) or by named-field value
    (`session.observe(flower.q == "rose")`).
  - `branch.grant_capabilities(operable)` opts a branch into per-message
    emission and injects a schema-derived instruction block; the legality rule
    is `set(keys) ⊆ grant`, with over-grant attempts surfaced as an observable
    `CapabilityViolation`. `response_format` (strict final parse) and
    capabilities (per-message emission) remain orthogonal knobs.
- **Casts Role/Mode pattern** — a thin `Pattern` dataclass with a dense
  built-in role roster and a default operational pack; `AgentConfig` composes
  `role` + `modes` into the system prompt. (#1200, #1202, #1205)
- **`Structure` / `JsonStructure`** (`protocols/structure/`) — composable
  schema builders wrapping `Operable`, handling both `BaseModel` and `dict`
  response formats as first-class citizens. Wired through `ChatParam` /
  `ParseParam`; `communicate()` and `run_and_collect()` extract the structure
  from the instruction as the single source of truth for rendering + parsing.
  (#1159)
- **`lionagi.testing`** module — test infrastructure shipped with the library
  so the CLI and downstream consumers can be tested without real API calls:
  `ScriptedEndpoint` (`provider="scripted"`), `ScriptModel` (YAML/JSON/dict
  fixtures with positional + `when:` matching), and `TestBranch` factories.
  Consolidates 20+ ad-hoc inline mocks. (#1151)

### Changed

- **`InstructionContent` refactored** to delegate rendering/parsing to an
  auto-created `JsonStructure` when `response_format` is set. Removes the
  internal `_schema_dict`, `_model_class`, `custom_renderer`,
  `structure_format`, `render()`, `response_model_cls`, and `schema_dict`
  members. Public `branch.operate`/`communicate` behavior is unchanged. (#1159)

### Fixed

- **`Pile.include` dropped falsy `Observable` items** — items whose truthiness
  is `False` were silently skipped on include. (#1203)

### Docs

- Governance ADR series 0053–0070, charter DSL v0, and governance standards;
  OSS-purity cleanup of the ADR corpus. (#1185, #1190)

## [0.26.11] - 2026-05-25

### Fixed

- **`li play` AttributeError on artifact contract resolution** — `flow.py`
  referenced `env.agent_profile` in two places (lines 643 + 648) but the
  `OrchestrationEnv` dataclass field is named `orc_profile`. The bug was
  introduced by #1083 (ADR-0029 artifact contract). It only triggered when
  an explicit agent name was passed via `--agent`, so the orchestrator
  tried to look up artifact defaults on the orchestrator profile. Now
  references the correct `env.orc_profile`.
- **Regression test added** — `tests/cli/orchestrate/test_orchestration_env_attrs.py`
  parses `flow.py` for every `env.<public_attr>` access and asserts each
  matches a real `OrchestrationEnv` field or method. Prevents future typos
  of this exact shape.

## [0.26.10] - 2026-05-25

Studio launcher: auto-mount symlink targets in `~/.lionagi/*/` so the Library tab works for users with symlinked content.

### Fixed

- **`li studio` launcher — dangling symlinks inside container** — many power-user
  setups symlink `~/.lionagi/agents/*.md`, `~/.lionagi/skills/*`, etc. to
  content living elsewhere on the host (e.g. a `firm/` content repo). When
  `~/.lionagi` is bind-mounted into the container, those symlinks point at
  paths the container can't see — ENOENT → empty Library / Agents / Skills /
  Playbooks / Teams views. The launcher now walks `~/.lionagi/{agents,skills,
  playbooks,teams}`, resolves any symlinks via `Path.resolve(strict=True)`,
  and adds read-only bind mounts for each unique target parent directory so
  the symlinks resolve identically inside the container.

## [0.26.9] - 2026-05-25

Hotfix for empty Studio Library / Skills / Agents views in the Docker image.

### Fixed

- **Studio Docker image — empty Library tab** — `apps/studio/server/services/plugins.py`
  resolves `MARKETPLACE_DIR = _REPO_ROOT / "marketplace"` and
  `MARKETPLACE_MANIFEST = _REPO_ROOT / ".claude-plugin" / "marketplace.json"`,
  where `_REPO_ROOT` is `/app` inside the container. The Dockerfile previously
  COPYed only `lionagi/`, `apps/studio/server/`, and the built frontend — the
  bundled marketplace and plugin manifest were missing from the image. Now
  copied at `/app/marketplace/` and `/app/.claude-plugin/`.

- **`li studio` launcher — third-party plugins** — also mounts
  `~/.claude/plugins:/root/.claude/plugins:ro` (when the host directory exists)
  so Studio can enumerate Claude Code third-party plugins from the user's
  cache, not just the bundled marketplace.

### No Python package changes

The Python package surface is identical to v0.26.7. This bump exists only to
publish a corrected Docker image and an updated `li studio` launcher.

## [0.26.8] - 2026-05-25

Hotfix for the Lion Studio Docker image.

### Fixed

- **Studio Docker image — `/api/*` 404s** — the v0.26.7 image baked
  `NEXT_PUBLIC_STUDIO_API_BASE=""` into the client bundle at build time. Because
  `NEXT_PUBLIC_*` vars are resolved during `next build`, the empty string
  became the literal URL prefix in production — `fetch('/api/runs')` hit the
  Next.js server on `:3000` (which has no `/api` route) and returned 404 for
  every backend call. The Dockerfile now bakes
  `NEXT_PUBLIC_STUDIO_API_BASE="http://localhost:8765"` so the browser reaches
  the FastAPI backend through the host port mapping.

- **`lib/api.ts::resolveApiBase()`** — defense in depth: treat empty string as
  "not configured" (`if (env)` instead of `if (env !== undefined)`) so the
  runtime port-based fallback can recover if the env var is ever accidentally
  baked as empty again.

## [0.26.7] - 2026-05-25

Issue-sweep release. 16 issues closed, 14 PRs merged this cycle.

### Added

- **CLI: `li kill`** (#1094) — terminate runs/sessions/plays with SIGTERM→SIGKILL escalation,
  `--recursive` cascade, `--all-stale` sweep (sessions + invocations; plays/shows are
  orchestrators with no direct PID — use `--recursive` for explicit cleanup).
- **CLI: `li monitor`** (#1089) — table + detail + `--watch` for play/agent/run progress.
  Filters: `--since`, `--type`, `--project`.
- **CLI: `--timeout` deadline awareness** (#1087) — when `--timeout N` is set, a deadline
  preamble is injected as a leading user message so agents (codex/claude-code/etc.) can
  ration reasoning instead of running until guillotine.
- **Orchestrator: FlowOp budget propagation** (#1091) — total budget split across ops by
  `FlowOp.budget_weight`; each worker receives a BUDGET preamble with its share + deadline.
- **LNDL Phase 1** (#966) — opt-in structured-output formatter ported from beta:
  `LNDLOutput`, `LNDLError`, `get_lndl_system_prompt`, `extract_lndl_blocks`,
  `normalize_lndl_text`. `parse_lndl_fuzzy` stubbed pending Phase 2.
- **Custom render/parser protocols Phase 1** (#1092) — `CustomRenderer`, `CustomParser`,
  `StructureFormat`, `validate_image_url`, pure-function `prepare_messages_for_chat`.
  Phase 2 (manager.py refactor + `branch.operate(renderer=...)` wiring) deferred.
- **Studio: project chips on invocations rows** (#1081). Schedules + runs already had them.
- **Studio a11y baseline (4 of 6 tracks)** (#1020):
  - Track 1 — programmatic labels + `aria-describedby` on form inputs
  - Track 2 — `--content-muted` and `dag-assign-text` contrast lifted to WCAG AA (4.5:1)
  - Tracks 3-4 — skip-to-main link, `aria-live` on status/loading regions, `aria-busy` on
    skeleton tables, `aria-pressed`/`aria-expanded` on toggle widgets
  - Tracks 5-6 — global `prefers-reduced-motion` guard, `eslint-plugin-jsx-a11y` installed
    (13 findings deferred via `TODO(#1020 follow-up)`).
- **Studio: ARIA tabs keyboard model** (#1040) — `RunStepCard` gains ArrowLeft/Right/Home/End
  with roving `tabIndex` per WAI-ARIA Tabs Pattern.
- **CI: marketplace skill validation** (#1031) — 175 parameterized tests across 35 marketplace
  MD files: canonical khive verbs, valid CLI subcommands, no banned models, no `nohup`.
- **API: public exports** (#1122) — 19 lndl + adapters symbols now importable from package root.
- **Tests: public-API smoke test** (#1134) — fails loudly on broken `lionagi/__init__.py`
  (no `importorskip` on the package under test).

### Fixed

- **`li kill --all-stale` scope** (#1117, codex-iter ×3) — was iterating only sessions+invocations
  despite docstring claiming all entity types. Now correctly limited to sessions + invocations
  (plays/shows excluded — orchestrators with no direct PID). Follow-up #1144 tracks smart
  child-derived staleness for plays/shows.
- **`anyio.NoEventLoopError` on resumed-codex teardown** (#1082) — cancelled-exception class
  cached at session start so the error path survives loop exit.
- **SIGINT bypassed shielded teardown in `run_async()`** (#1055) — signal-aware handler
  installed in parent thread; child loop is canceled cleanly so structured `finally:`
  finalizers run before exit.
- **Test timing-race patterns** (#1090, codex-iter ×3) — replaced `anyio.sleep(<small>)` sync
  points with `anyio.Event` and `TaskGroup.start(...)` `task_status.started()`. Behavioral
  assertions added alongside CI-tolerant relative timing bounds.
- **SSRF guard missed `::169.254.169.254`** (#1125, CWE-918) — IPv4-compatible IPv6 form was
  bypassing block-net check; now unmapped before lookup, same treatment as IPv4-mapped.
- **Hook subprocess errors silently swallowed** (#1127) — `B904`/`S110`: traceback chain
  preserved on `PermissionError`; warning logged in place of bare `except: pass`.
- **State layer imported CLI internals** (#1119) — `LIONAGI_HOME` extracted to
  `lionagi/_paths.py` leaf module; state no longer transitively pulls in CLI code.
- **Studio: 501-stubbed Run/New controls** (#983) — disabled in UI with hold-message
  pointing to CLI until backend implementation is designed.
- **CLAUDE.md described `li schedule` as shipped** (#1121) — reference removed; ADR-0027
  itself correctly remains "Proposed".

### Changed

- **Removed `pydapter` core dependency** (#1044) — `Adaptable`/`AsyncAdaptable`,
  `AdapterRegistry`, `JsonAdapter`/`CsvAdapter`/`TomlAdapter` inlined into
  `lionagi/adapters/`. `DataFrameAdapter` is now opt-in via `lionagi[pandas]`.
  `AsyncPostgresAdapter` retains `pydapter[postgres]` (it inherits from a substantial
  SQLAlchemy stack) and is opt-in via `lionagi[postgres]`.
- **Asyncio sweep Phase 1** (#1043) — `asyncio.Lock` → `lionagi.ln.concurrency.Lock` (×6
  call sites), `asyncio.sleep` → `anyio.sleep` (×1), `asyncio.gather` → anyio task group
  (×1). 9 Phase 2/3 sites tagged with `TODO(#1043 Phase 2)` for next pass.
- **CI matrix split** (#1069) — `pull_request` runs Python 3.10 + 3.14; `push` to main
  runs the full 3.10–3.14 matrix. ~60% PR wall-time reduction.

### Security

- **#1125** — IPv4-compatible IPv6 IMDS bypass in `lionagi/ln/_ssrf.py`. Cloud envs with
  IPv6-reachable IMDS could have been reached via `http://[::169.254.169.254]/…`.
- **#1127** — broken hooks now logged instead of silently swallowed.

### Internal

- **Polish**: Studio microcopy centralized in `lib/copy.ts` (#1088 merged pre-cycle).
- **Discovery**: `/discover-issues` filed 19 follow-up issues (#1117–#1135) — 8 resolved
  this cycle; the remainder live for the next sweep + decisions on canonical SIGINT
  reason code (#1118), README scope (#1120), ADR status taxonomy (#1129), custom render
  Phase 1 integration (#1130), and v0.21.0 deprecation purge strategy (#1131).

### Patch releases 0.26.1–0.26.6

These were Docker-distribution iteration releases:

- 0.26.1 — initial Docker release
- 0.26.2 — Docker fix iteration
- 0.26.3 — `fix(docker): use --legacy-peer-deps for npm install in Dockerfile` (#)
- 0.26.4 — `fix(docker): remove non-existent public/ dir from COPY step` (#)
- 0.26.5 — Docker build verified locally
- 0.26.6 — minor fixups + ADR-0028 Phase 1 (#1073), marketplace consolidation (#1070),
  ADRs 0028-0032 proposed (#1072), multi-arch Docker amd64+arm64 (#1080/#1093)

## [0.26.0] - 2026-05-21

### Added

- **Lion Studio** — first public release of the operational UI for lionagi.
  Dashboard with system health, runs/shows/agents inventory. Four new pages:
  Runs (paginated, filterable by status/playbook, provenance badges), Teams
  (read-only viewer), Admin (phantom session doctor + prune), Stats (DB health).
- **State persistence** — `lionagi/state/` with SQLite-backed session, branch,
  show, and definition tracking. Forward-only migrations via `StateDB.open()`.
- **Studio security** — optional bearer auth (`LIONAGI_STUDIO_AUTH_TOKEN`),
  path sanitization, plugin path bounds, `PRAGMA foreign_keys = ON`,
  TOCTOU-safe admin prune.
- **Studio a11y** — WCAG AA contrast, skip-to-main, ARIA labels/roles,
  keyboard-navigable tables.
- **CI** — unified `scripts/ci.sh` + `Makefile`, frontend lint/typecheck/build
  job, marketplace content lint, Python 3.14 in test matrix.
- **Marketplace** — LICENSE, `.gitignore`, manifest validation, lint CI,
  root README install instructions.

### Fixed

- 45 issues from discover-2026-05-21 audit (#984–#1031): N+1 queries, SSE
  races, dead code (−1787 LOC), broken ESLint baseline, data-loss chain in
  migrate-memory, dead khive verb syntax in 6 skills, shell injection in
  show redo path, stub manifests, stale repo refs, private IP leaks.

### Changed

- ag2 0.12.2 → 0.13.0
- Pre-commit: black + isort → ruff

### Security

- Bump `idna` 3.15, `pymdown-extensions` 10.21.3
- Starlette CVE fix blocked upstream — [ag2#2894](https://github.com/ag2ai/ag2/issues/2894)

## [0.25.1] - 2026-05-12

### Added

- **Lion Studio apps** — FastAPI backend + Next.js 16 frontend (`apps/studio/`),
  17 ADRs, SSE streaming, SQLite-backed runs provenance.
- **Marketplace** — Claude Code plugin system (`.claude-plugin/marketplace.json`),
  7 plugins (show, play, orchestrate, research, memory, devx, kg-bridge).
- **Codex fast mode** — `fast_mode` for OpenAI priority tier routing.
- **AG2 passthrough** — `pre_built_agent` param on `AG2BetaEndpoint`.

## [0.25.0] - 2026-05-08

### Changed

- 0.24.0 yanked — LNDL reverted, will return once spec stabilises.

### Fixed

- SSE parser rewrite — multi-line events, `[DONE]` frames, Anthropic deltas.
- Handler leak in CLI providers, `messages` default, `call_kwargs` transport.
- Request model wiring for embed/response endpoints.

### Security

- Bump `python-multipart>=0.0.27`, `gitpython>=3.1.49`

## [0.23.1] - 2026-05-02

### Added

- Provider registry (`EndpointRegistry`, `@register` decorator)
- AG2 GroupChat endpoint
- `Note` model with `deep_update()`

### Changed

- Provider modules reorganized to `providers/{company}/{endpoint}/`
- `CLIEndpoint` → `AgenticEndpoint`

## [0.23.0] - 2026-04-27

### Added

- Agent infrastructure (`AgentConfig`, `create_agent()`, `PermissionPolicy`, hooks)
- `SandboxSession` — git-worktree isolation for speculative edits
- DeepSeek native provider, Pi CLI endpoint
- `li play NAME --help`, 250+ tests

### Fixed

- `li o flow --save` regression, flaky timing tests

## [0.22.9] - 2026-04-24

### Security

- Symlink containment, flow-id validation, `--max-ops` enforcement, save-path containment
- Pin `lxml>=6.1.0`, `python-dotenv>=1.2.2`

### Added

- `li skill`, `li play`, playbook args, `--team-attach`, `--bypass`, `--add-dir`

## [0.22.8] - 2026-04-21

- Fix `StreamChunk` propagation through iModel layer

## [0.22.7] - 2026-04-20

- `li --version`, `--background` flow, docs overhaul (74% reduction)
- Fix `--show-graph` macOS, codex `reasoning_effort` clamping

## [0.22.6] - 2026-04-20

- Two-level flow DAG (`FlowAgent` + `FlowOp`), run persistence, `Middle` protocol
- Team file locking, per-agent artifact dirs
- `branch.operate()` absorbs `branch.instruct()`

## [0.22.2–0.22.5] - 2026-04-19

- `li o flow` DAG orchestration, stream persist, unified operate routing
- Security: authlib, python-multipart, pillow bumps

## [0.22.0–0.22.1] - 2026-04-18

- `branch.run()` async generator, agent profiles, `li team`, `--team-mode`

## [0.21.1] - 2026-04-17

- `li orchestrate fanout` — parallel fan-out with synthesis

## [0.21.0] - 2026-04-15

- `li` CLI with `--theme`, `--yolo`, `--verbose`; model spec parsing

## [0.20.2–0.20.4] - 2026-03-16 to 2026-04-11

- Firecrawl, Tavily search, event lifecycle, CLI provider updates, 20 fixes

## [0.20.0–0.20.1] - 2026-02-13

- `NodeConfig`/`create_node`, `Flow`, `Broadcaster`, graph algorithms, 187 doc tests

## [0.19.0–0.19.2] - 2026-02-11

- Native Gemini API, `CLIEndpoint`, async context managers

## [0.18.0–0.18.6] - 2025-10-09

- `ChatParam`/`ParseParam`, `LION_SYSTEM_MESSAGE`, AnyIO structured concurrency

## [0.17.0] - 2025-09-14

- Remove deprecated `ClaudeCodeEndpoint`

## [0.16.0] - 2025-09-02

- V1 Observable Protocol, `CompletionStream`

## [0.15.0] - 2025-08-16

- Structured concurrency (`CancelScope`, `TaskGroup`), `Pile` generics

## [0.14.0] - 2025-07-22

- `DependencyAwareExecutor`, `Session.flow()` DAG execution

## [0.13.0] - 2025-07-13

- Claude Code provider with session management

## [0.12.0] - 2025-05-14

- XML/JSON parsing, async file I/O, adapter registry

## [0.11.0] - 2025-05-01

- `Research` models, `concat` utility

## [0.10.0] - 2025-03-19

- Pandas adapters, `BaseForm`/`FlowDefinition`/`Report`, `ln` namespace

## [0.9.0] - 2025-01-24

- `Analysis` class, `as_readable` YAML formatting

## [0.8.0] - 2025-01-18

- `FlowStep`/`FlowDefinition`, Exa search, action batching

## [0.7.0] - 2025-01-13

- `interpret`/`select`/`translate` ops, Groq/Perplexity/OpenRouter

## [0.6.0] - 2025-01-04

- `MailManager`, Branch serialization, `LiteiModel`

## [0.5.0] - 2024-12-16

- LION2 protocol, class registry, `ReactInstruct`

## [0.4.0] - 2024-10-30

- `lion-core` integration, LangChain/LlamaIndex adapters

## [0.3.0] - 2024-10-06

- `uv` replaces Poetry, pre-commit hooks, CI with dependabot

## [0.2.0] - 2024-05-28

- Ollama integration, token compressor (experimental)

## [0.1.0] - 2024-04-10

- `Branch` + tree-node architecture, tool manager, async queue, knowledge graph

---

See git history for the 0.0.x series (v0.0.102–v0.0.316).
