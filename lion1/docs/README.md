# lion1, the map

lion1 runs a model as an actor: the model writes LNDL in its text, the actor's handlers do what it
asks, and an append-only record keeps every step. This page says where each part lives, what the
words in the code mean, and which values each state field takes. The specification is the design
records, indexed in [adr/INDEX.md](adr/INDEX.md); the code cites one claim of a record as
`ADR-000N/Cn`.

## Where things live

| path | what it holds |
|---|---|
| [`src/lion1/`](../src/lion1) | the runtime: the modules below, and nothing an app adds |
| [`hub/`](../hub) | what every app reaches for, versioned with the runtime: backends, tools, hooks, each role's guidance, the chat, flows, long-running agents |
| [`apps/cli/`](../apps/cli) | `lion`, the command line (ADR-0016) |
| [`apps/mac/`](../apps/mac) | the Mac app (ADR-0022) |
| [`khive/`](../khive) | the `lion` pack the store's rows go through, and `lion1-khive`, the store's CLI with that pack linked in (ADR-0025) |
| [`bench/`](../bench) | a SWE-bench set through the loop, one box per instance (ADR-0012) |
| [`examples/`](../examples), [`notebooks/`](../notebooks) | runnable examples, one idea each, indexed in [examples/README.md](../examples/README.md) |
| [`docs/adr/`](adr) | the design records, indexed in [adr/INDEX.md](adr/INDEX.md) |

## The modules of `src/lion1`

| module | what it holds | records |
|---|---|---|
| [`actor.py`](../src/lion1/actor.py) | the actor, its profiles and handlers, the run, its bounds and outcomes, the turn loop | ADR-0001, ADR-0002, ADR-0003, ADR-0005, ADR-0006, ADR-0010 |
| [`lndl.py`](../src/lion1/lndl.py) | the language: the three constructs a turn's text carries, the program form, the parser and the repairs it names back | ADR-0004, ADR-0008 |
| [`spec.py`](../src/lion1/spec.py) | a Spec, the Operable, and the guidance rendered from them | ADR-0001 |
| [`record.py`](../src/lion1/record.py) | the record, its four kinds, the view directives and the fold | ADR-0007 |
| [`notes.py`](../src/lion1/notes.py) | notes: named values per profile that outlive a run | ADR-0007 |
| [`bus.py`](../src/lion1/bus.py) | the in-process bus a run's events go out on | ADR-0005 |
| [`store.py`](../src/lion1/store.py) | a run's rows in khive, the replay a resumed run reads, notes as rows | ADR-0025, ADR-0026 |
| [`khive.py`](../src/lion1/khive.py) | the khive client: one `kkernel exec` per call, always as one actor | ADR-0001, ADR-0025 |
| [`checkpoint.py`](../src/lion1/checkpoint.py) | the named values and folded view of a harness session at a turn, read from the store | ADR-0019, ADR-0025 |
| [`serve.py`](../src/lion1/serve.py) | the served actor: offers, the runs a client starts, the HTTP transport on loopback | ADR-0021 |
| [`__init__.py`](../src/lion1/__init__.py) | the names `import lion1` exports | |

## Words

### The actor and the run

- **Actor** (`Actor`): one addressable principal. It holds the handlers (its Operable), the
  privileges, the inbox, the notes, a bus and a store, and is built as `Actor(name, store=...)`.
- **Spec**: a Pydantic model class registered under a name. It is the guidance the model reads and
  the validator the system runs, so there is no second schema.
- **Operable** (`Operable`): the specs an actor offers. A profile names a subset of them.
- **Handler** (`Handler`): the function behind a spec, the privileges it requires, and its effect.
- **Profile** (`Profile`): how the model is configured when the actor runs: the specs it is told
  about, the privileges the run acts under (a subset of the actor's), the role's prose, the language,
  and defaults for the bounds. It has no inbox and no address of its own.
- **Backend** (`Backend`): messages in, text out. The real ones are in `hub/backend/`.
- **Run** (`Run`): what `await actor.run(profile, request, backend)` returns. A run is turns: one
  SYSTEM notification, one model call, extract, gate, dispatch, settle.
- **RunRequest** (`RunRequest`): the job: its inputs, what `OUT{}` fills (`emits`), the bounds, the
  accept gate on the output, and for a conversation `wait` and `history`.
- **Bounds**: `max_turns`, `max_idle`, `time_budget`, `cost_budget`, `context_budget` and
  `max_refusals`. Each resolves once when the run starts: the request's value, else the profile's
  default, else the runtime's. The model asks for more with the `run.more` command.
- **Outcome** (`run.outcome`): how the run ended: `Success(output)`, `Exhausted(turns, reason)`,
  `Refused(why, times)` or `Stopped(turn)`. `Continue` is what a settled turn leaves while the run
  goes on, never an end; a run whose backend raised has no outcome and the error propagates.
- **Result** (`Result`): what a settled command put on the record: `ok`, then a value, or an error
  and the reason it failed.
- **Inbound** and **Handle** (`Inbound`, `Handle`): work another actor queued on this one, and the id
  its answer comes back under (ADR-0010).

### The language

A turn's text carries three constructs, and everything else in it is prose:

    <lvar alias>value</lvar>                     name a value
    <lvar spec.field alias>value</lvar>          name a value that fills one field of an output Spec
    <lact alias>name(field=..., x=*other)</lact> ask for a command; `*other` points at a named value
    OUT{spec: [alias, ...], other: literal}      finish with typed output

A profile with `language="program"` takes one fenced program per turn instead (ADR-0008).
`lion1.parse` reads a turn, and a slip it repairs is named back to the model.

### The record

- **Entry** (`Entry`): one step, with its `seq`, `kind`, `content`, `turn`, and the model's `alias`
  when it gave one. Its name is the kind's prefix and the sequence: `_in3`, `_sys12`, `_m40`, `_r41`.
  A name is a pointer and lives as long as the record.
- **Kind** (`Kind`): INPUT is what came in, SYSTEM the runtime's notices, TEXT the model's turn, and
  RESULT a settled command.
- **View**: what the model sees, folded from the record by the directives and the defaults. The
  record is never edited; only the view changes.
- **Directive** (`Directive`): `hide`, `show` or `summarize` over named entries, carried by a context
  command's result or by a fold.
- **Notes** (`Note`, `NoteStore`): a name with the `note.` prefix goes to the profile's note store
  instead of the record. A note outlives the run and carries the run and sequence that wrote it.

### The bus

- **Event** (`Event`): a `kind`, a `payload` and the time. `bus.emit(kind, **payload)` keeps the
  event in the bus's log, which holds the last `keep` events (none when `keep` is 0), and calls each
  subscriber whose pattern matches it, awaiting none of them inline. `bus.subscribe(pattern, fn)`
  takes a kind, `"*"`, or a prefix ending in a dot such as `"command."`.
- The runtime emits `run.started`, `turn.started`, `turn.retry`, `command.requested`,
  `command.settled`, `run.completed`, `inbox.queued` and `inbox.answered`; the served actor adds
  `run.waiting`. Handlers and apps emit kinds of their own.

### The store

- **Store** (`Store`): writes a run into khive through the `lion` pack: a session and a run row when
  it starts, a message row for each entry, a call row around each model request, and the outcome at
  the end, each signed as the actor. `store.history(run_id)` reads a session back as entries, which a
  resumed run replays (`RunRequest.history`).
- **KhiveNoteStore** (`KhiveNoteStore`): a profile's notes as rows of the same store.
- **Khive** (`lion1.khive.Khive`): the client. It is bound to a directory or a config file and checks
  on every call that khive resolves to the actor it was built for.
- **Checkpoint** (`lion1.checkpoint`): for a harness session that is not a run of this runtime, such
  as Claude Code or Codex, its named values (`Var`) and folded view at a turn, computed from the
  store. A run of this runtime needs none, since it replays its own rows.

## Value sets

Each field below takes one value of a closed set. The set is the field's type, a `Literal` alias or
the `Kind` enum, importable from its module, and `typing.get_args(Role)` gives the values as a tuple.
A value outside the set is refused when the object is made, and the error names the set:

```python
Message("robot", "hi")
# ValueError: message role 'robot' is not one of system, user, assistant
```

| field | type | values |
|---|---|---|
| `Message.role` | `actor.Role` | `system`, `user`, `assistant` |
| `Profile.language` | `actor.Language` | `core`, `program` (refused with `ProfileError`) |
| `Handler.effect` | `actor.Effect` | `write`, `read`, `pure` |
| `Result.reason` | `actor.Reason` or None | `unknown`, `invalid`, `privilege`, `refused`, `failed`, `cancelled`; None on a result that succeeded or was given no reason |
| `Exhausted.reason` | `actor.Spent` | `turns`, `idle`, `time`, `cost`, `context` |
| `bound` of the `run.more` command | a `Literal` | `max_turns`, `max_idle`, `time_budget`, `cost_budget`, `context_budget`, `max_refusals`; another fails validation and the model is told |
| `Entry.kind` | `record.Kind`, an enum | `Kind.INPUT`, `Kind.SYSTEM`, `Kind.TEXT`, `Kind.RESULT`; its value as a bare string is refused too |
| `Directive.op` | `record.Op` | `hide`, `show`, `summarize` |
| `status` of `Store.call_close` | `store.CallStatus` | `ok`, `refused`, `cut`, `failed`; another is refused before any row is written |

These stay strings on purpose, because their values are another party's to name:

- `Event.kind`: handlers and apps emit their own kinds; the runtime's are listed above.
- `checkpoint.Var.kind` and `checkpoint.Checkpoint.why`: each taker passes its own lists to `take`,
  which refuses any other; a run's are `KINDS = ("fact", "posture")` and
  `WHYS = ("threshold", "end", "manual")`.
- `checkpoint.Var.source`: where a value was read, as the harness names it.

## Read next

- [examples/README.md](../examples/README.md): what to run first, one idea per file; all but
  `examples/backends.py`, which calls a real model, run offline.
- [adr/INDEX.md](adr/INDEX.md): every design record, its status, and how much of it runs.
- [`tests/test_value_sets.py`](../tests/test_value_sets.py) pins the value sets above, and
  [`tests/test_docs.py`](../tests/test_docs.py) keeps this page in step with the code.
