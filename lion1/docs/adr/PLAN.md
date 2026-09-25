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
| ADR-0010 | Delegation between actors: `queue`, the handle, the answer as a settled command, the reply, no graph object | draft (2026-09-25); no bench exercises the path | principals and mail; in-process peers are one layer, the long-running agent another |
| ADR-0011 | The box: one shape for three boxes, the coding tools over a tree, the watch and its gate, the patch | draft (2026-09-25); the real-box tests opt-in | the sandboxed executor: content-addressed trees, declared write paths, receipts |
| ADR-0013 | The long-running agent: the wake, the send gate and the hop count, the caps, the cursor and posture, the lock, the continuous run and its checkpoint | draft (2026-09-25) | the process: one identity per process, lease by turn activity, one supervisor |
| ADR-0012 | The bench as the instrument | unwritten | the instrument contract |
| ADR-0014 | The instrument contract | unwritten | none |
| ADR-0015 | Chores | unwritten | none |
| ADR-0016 | The lion command and the spend row | unwritten | none |
| ADR-0017 | The desk: front desk, record answers, areas and chairs | unwritten | mail settlement by keyed replay |
| ADR-0018 | Chat in a box | unwritten | none |

Order of writing: 0012 and 0014 to 0018, product-side. Each record is written
from the code first; the vocabulary below is fixed before any of them. 0009 landed 2026-09-25 and
amended 0004 D2, 0006 C4 and S3, and 0007 C2 to the code of 2026-09-24: the fold measures the
estimate plus the reported context above the run's floor, `fold_inputs` folds earlier inputs, and a
session-keeping backend starts over from the folded view. 0010 landed 2026-09-25 from the same code and
amended nothing: it names the long-running agent's mail as the other layer and leaves the process
boundary to 0013. 0011 landed 2026-09-25 and amended nothing: the box, the tools and the watch as the code
holds them, with the executor over content-addressed trees named as the boundary. 0013 landed 2026-09-25 and amended
0010 C4 (mail is the wake's input, or one inbound while a run continues) and 0007 S2 (the continuous run's
checkpoint is built and replays as history).

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
- `queue` returns at once and lands on the peer's record as INPUT with handle and origin; a handler that
  returns the handle is settled by the peer's plain output under the alias, or failed naming the peer's
  outcome or error; an answer to a run that has ended is a reply on the origin's inbox (0010 C1, C2, C3).
- No graph object: a group of actors is handlers that queue to each other; the handler starts the peer's
  run, the runtime never does; the long-running agent's mail is the other layer, one inbound from "the
  inbox" and every send through one gate (0010 C4, D2).
- Who may ask whom, the lineage of an ask, ending a peer with its asker, authority that narrows downward
  and delivery across processes are below the boundary; no budget is shared across peers (0010 S4, S5).
- One box shape whatever runs the command: `exec` with the streams apart and rc 137 at the timeout, `run`
  under pipefail with 124; a command touches the box and nothing else of the host; the network is on by
  default and `--offline` is none (0011 C1, C2, D4).
- Three coding tools over a tree that may be anywhere; an edit lands once, verbatim, keeping each line's
  ending; the watch reports the change as the tree shows it and a finish is accepted only after a command
  ran against it; the patch is printed and saved and lands only on `--apply` (0011 C3, C4, C5, C6).
- An executor over content-addressed trees with declared write paths and receipts is below the boundary;
  a box here is a process apart from this machine whose patch is the only thing that comes back (0011 S8).
- One identity per process held by a kernel lock on the agent's directory; a wake takes its mail as one
  batch, marked read before anything acts, and runs it once; the cursor is written running before the run
  and a lost batch is told to the owner (0013 C1, C2, C3).
- Every send a handler makes goes through one gate that charges the hop count first, under a lock, a key
  charged once; the caps hold a wake and the mail waits unread; the agent writes the posture, never the
  runtime; a continuous run carries many wakes and its checkpoint carries the record across a restart
  (0013 C4, C5, C6, C7).
- The identity and its grants, the lease by turn activity, the supervisor and the durable record are below
  the boundary; the agent holds a lock and nothing more (0013 S10).

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
- The sandbox (0011, done): network on by default, `--offline` for none; the VM has egress.
- The watch (0011, done): an edited test still counts as a command run (0011 S5).
- Delegation vs the agent (0010, 0013, done): in-process peers (one process, several actors, the bench) and
  the long-running agent (one identity per process) are two layers; 0010 says which applies where, 0013
  carries the agent's side: one inbound per wake, `send` under `comm.send`, the hop count at one gate.
- The bench actor's report (0013, done): every send goes through `Agent.deliver`; the two bypasses are
  named as a limit in 0013 S4.
- Citations (0001 A1, 0009): bench numbers belong with the run they came from (1976 calls / 952 turns /
  3 output errors), never with the 50-instance table; thinking-budget rows are in the bench record or not
  cited.
- The desk (0017): the reader is a second client inside the agent's process, bound to the agent's
  identity, read-only; the agent's wake marks mail read, the desk's sweep reads by cursor and never
  marks; `desk_pending` and the ledger row: a failed wake re-lists the message. Unknown-send settlement
  is keyed replay or leave, never chronology. Every model-facing command is listed in one claim with its
  gate; code-only escalations separately. `Agent.admit` drops untrusted senders before any model call.
- Posture (0013, done): `OUT{posture}` is written to the notes by the agent's own code, not by the runtime
  (0013 C6).
- Serving prohibitions since lifted are not carried (0014, 0016).
- The diff-landing fork is closed by "print and save, `--apply`" (0011 C6 done, 0015).
