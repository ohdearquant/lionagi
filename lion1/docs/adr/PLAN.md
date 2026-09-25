# docs/adr: the plan

## What the records are

`docs/adr/` is the design of the loop written once, in the present tense, from `lionagi-v1` as it
stands; the amended history it was distilled from is not in this repository. A record says what is, why,
and what it costs, in the register the tooling in `docs/adr/_scripts/` enforces: Context, Definitions,
Assumptions, Claims, Decisions, Alternatives, Consequences, with anchors on the C and D items. A fact the
code does not show is not asserted; a claim marked mechanical has a landed decision behind it, else it is
marked process or dropped.

The frame since 2026-09-23: the loop is the inside of one process, and every record here is the
in-process form of a concern the larger system's kernel owns: identity and grants, admission and
budgets, the process lifecycle, the tool protocol, context assembly, the durable record and its receipts.
Each record states what is inside the process and names the boundary; nothing below the boundary is
decided here. A decision in these records is an experiment: the candidate the kernel's port reads, never
a commitment the kernel inherits. The public substrate's own records for the process table and the
sandboxed executor are the published side of that boundary; the rest is named by role.

## Records

| record | title | state (2026-09-23) | kernel concern it is the in-process form of |
|---|---|---|---|
| ADR-0001 | The actor and its privileges: Spec, Operable, profile | operating; `defaults` owed | identity, grants, a gate that narrows and never widens |
| ADR-0002 | The run: the turn table, repeats, stop, four outcomes, the closure | operating; reference-only wait and reap owed | process lifecycle: admission, turn as the unit, terminal states |
| ADR-0003 | The bounds: the job, run.more, extend, `OUT{}` as a command | partial; extend, run.more, cost, context check owed | admission and budgets: minted at spawn, inherited, exhaustion refuses the next call |
| ADR-0004 | The language: LNDL, commands, values, pointers, lenient reading | operating; mixed-response refusal owed | the request program |
| ADR-0005 | Command handling: order, the bus, hooks, effect classes | operating; captures, rebuilt replacement, effect wait owed | the tool protocol: declared effects, denial as data, receipts |
| ADR-0006 | The notification, and the closure | operating; closure owed | context assembly: the runtime's typed section |
| ADR-0007 | The record, the view, the fold, context commands, notes | operating; captures, dated directives, consumed mark owed | the durable record, visibility directives, the store families |
| ADR-0008 | The program: compile then run, control results, curated view | unimplemented | the request program |
| ADR-0009 | Backends and the context figure: three shapes, the `Reply` count, the floor, the session restart | draft (2026-09-25); the subscription CLI's count and the mixed-response refusal owed | the inference driver: a binding row, the served model read from the response; here the envelope keeps the response's model and compares nothing |
| ADR-0010 | Delegation between actors | unwritten | principals and mail; in-process peers are one layer, the long-running agent another |
| ADR-0011 | The box: sandbox, boxes, coding tools, the watch | unwritten | the sandboxed executor: content-addressed trees, declared write paths, receipts |
| ADR-0013 | The long-running agent | unwritten | the process: one identity per process, lease by turn activity, one supervisor |
| ADR-0012 | The bench as the instrument | unwritten | the instrument contract |
| ADR-0014 | The instrument contract | unwritten | none |
| ADR-0015 | Chores | unwritten | none |
| ADR-0016 | The lion command and the spend row | unwritten | none |
| ADR-0017 | The desk: front desk, record answers, areas and chairs | unwritten | mail settlement by keyed replay |
| ADR-0018 | Chat in a box | unwritten | none |

Order of writing: 0010, 0011, 0013 next, in that order, because each is the in-process side of a
kernel concern the port has to read; 0012 and 0014 to 0018 after, product-side. Each record is written
from the code first; the vocabulary below is fixed before any of them. 0009 landed 2026-09-25 and
amended 0004 D2, 0006 C4 and S3, and 0007 C2 to the code of 2026-09-24: the fold measures the
estimate plus the reported context above the run's floor, `fold_inputs` folds earlier inputs, and a
session-keeping backend starts over from the folded view.

## Decided in the records (2026-09-23)

- A validator is shape only and stable on canonical fields; a replacement, dict or instance, is rebuilt
  from its fields, a class being no receipt (0001 S4, 0005 C4).
- A command is admitted at alias reservation, entered when its handler is called, settled when its RESULT
  lands; the loop records one cancelled before it entered; cancellation at the run's end is awaited before
  `run` returns (0002 C1, C4, D1, S4).
- `OUT{}` waits for its references and no effect; an accepted output ends the run and the rest is
  cancelled and reaped; a refused output settles its turn before the next backend call (0002 C1).
- A job over `context_budget` before the first call is a job error; extension grants are answered in
  order against one total; `accept` sees the run, reads a prefix of the record, and is the last before
  hook of `out`; an after hook on `out` observes (0003 C2, C3, C4, D3).
- A mixed native-and-text response is refused before any effect (0004 C4).
- Three captures around the hooks, each hook on a copy: the request as the handler entered, the return
  before the first after hook, the result as the hooks left it; the first two ride the entry as
  provenance (0005 C5, 0007 C1).
- A handler's effect class, `write` (default), `read` or `pure`, orders the turn: a write waits for every
  earlier non-pure command, a read for the nearest earlier write, a pure command for its pointers;
  measured on the bench, three classes save 17% of the serial steps over two (0005 C6, S7). One run's
  commands only; coordination across runs is the kernel's.
- `Context.get` reaches settled entries and the command's own pointers; a wait that never returns reaches
  no bound check, a deadline beside the wait is owed (0005 S6, 0003 S3).
- A run that ends by an outcome appends one closure SYSTEM entry carrying the outcome, the cancelled
  aliases and the directives no notification follows; directives are dated at their event and the fold
  applies them by date, so a later `context.show` stands over a consumption's hide (0006 C1, 0007 C3,
  D2, 0008 D3).
- A note is read once per command with its version; every write is one put; in a program a note
  assignment is a node, written when its block reaches it and never hoisted (0007 C5, D3, 0008 C4).
- A block finishes with Continue, Return(value) or Failed; only Continue runs its suffix; a return
  propagates through every enclosing block; a refused candidate is a retry, never fallthrough; the cycle
  check covers reference, control and effect edges (0008 C1, C7, D1).
- A run that reads a profile's notes and writes none is a read-only view of the same store, refusing at
  the one put; not built (0007 S4).
- A backend is a function from the view to the model's text and acts on nothing; it hands back the
  prompt count of the attempt that answered, never a sum over attempts, and the record keeps plain
  text; every call lands an envelope, failed ones too, only a transient failure is retried, and a
  subscription's spend is unknown, never zero (0009 C1, C2, C5).
- The fold measures the estimate plus what the last count stands above it beyond the run's floor; the
  floor, the backend's fixed context, stays in the figure and out of the measure; a session-keeping
  backend starts over from the folded view on a directive or the fold event, one session per run
  (0009 C3, C4).
- Which model a name binds to, through which route and on whose key, and whether the served model is
  the one named, are below the boundary; the envelope keeps the response's model and compares nothing
  (0009 S5).

## Open

- A hard ceiling above `extend`: the kernel's admission; the in-process `extend` stays as written (0003).
- The before-hook `guard` flag: a guard may not replace; the loader refuses a replacing hook after a guard
  on the same command, matches expanded over the run's admitted names, Python registrations and the
  job's `accept` included, rechecked when the subset or the hooks change; a guard works on a copy. In a
  record once the hub loader carries it (0005 C4).
- A RESULT larger than the view budget: a sized preview with the value kept whole (0007 S5).
- The runtime's name: the corpus says "the LION runtime" while the kernel already carries that name;
  open.
- The landing order for the owed tests: reference-only `OUT{}` beside an optional and a required sibling
  held on an event (readiness, required evidence, no next call while the turn is unsettled); cancellation
  at each boundary (before entry, after return, during the store write); the alias under refusal and
  cancellation; the undeclared wait and the deadline; the hook chain and the captures; branch control and
  the closure. A cross-run exclusion test is characterisation, not a gate.

## Vocabulary, fixed in the Context of 0002 and used everywhere

run (one `Actor.run`) · round (one loop iteration; the code still says round where the corpus says turn,
the rename owed with the first change that touches it) · turn (one backend response) · backend (messages
to text; an agentic harness is a different shape and is named so) · outcome (the run's terminal value
only; an instrument returns a measurement, a cursor holds a state) · message (a provider message; mail
becomes one INPUT entry) · record (the run's append-only history; the desk's knowledge lookup is the
`record` chore) · owner (the human or actor the agent works for) · chair (the actor accountable for an
area) · steward (who hears instrument failures).

## Facts to carry into the unwritten records

- Notes (0007, done): the commands are `note.list`, `note.find`, `note.get`, `note.delete` and the
  `note.` prefix write; a blank find matches nothing.
- Backends and the turn (0004, 0009, done): a native tool call is read back as the LNDL tag the model
  wrote, or as an `auto_` lact, and is then dispatched like any command; "never depends on native tool
  calling" means no schema is forwarded, not that a rewritten call is inert.
- The sandbox (0011): network on by default, `--offline` for none; the VM has egress.
- The watch (0011): an edited test still counts as a command run.
- Delegation vs the agent (0010, 0013): in-process peers (one process, several actors, the bench) and the
  long-running agent (one identity per process) are two layers; say which applies where.
- The bench actor's report (0013): every send goes through `Agent.deliver`; if code bypasses it, the
  record says so as a limit, not a claim.
- Citations (0001 A1, 0009): bench numbers belong with the run they came from (1976 calls / 952 turns /
  3 output errors), never with the 50-instance table; thinking-budget rows are in the bench record or not
  cited.
- The desk (0017): the reader is a second client inside the agent's process, bound to the agent's
  identity, read-only; the agent's wake marks mail read, the desk's sweep reads by cursor and never
  marks; `desk_pending` and the ledger row: a failed wake re-lists the message. Unknown-send settlement
  is keyed replay or leave, never chronology. Every model-facing command is listed in one claim with its
  gate; code-only escalations separately. `Agent.admit` drops untrusted senders before any model call.
- Posture (0013): `OUT{posture}` is written to the notes by the agent's handler, not by the runtime.
- Serving prohibitions since lifted are not carried (0014, 0016).
- The diff-landing fork is closed by "print and save, `--apply`" (0011, 0015).
