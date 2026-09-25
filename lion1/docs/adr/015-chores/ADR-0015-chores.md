---
adr: ADR-0015
status: draft
liveness: operating (the handler, the gate, the store boundary, the ticks and the catch-up are pinned by tests with scripted backends and a fake store; no test drives a positive hand-run check)
date: "2026-09-25"
area: chores
kind: new
depends_on:
  - "[[ADR-0001-the-actor|ADR-0001]]"
  - "[[ADR-0005-command-handling|ADR-0005]]"
  - "[[ADR-0007-the-record|ADR-0007]]"
  - "[[ADR-0010-delegation|ADR-0010]]"
tags:
  - adr
  - agent
  - chores
---

# ADR-0015: Chores

## Context

A long-running agent pays a model call for every wake, and much of what its owner wants watched is a
reading code can take and judge by itself: open pull requests, free disk, served processes, the age
of unread mail. A chore hands that reading to code. The model routes a tick or a question to one
handler call; the handler runs the instrument, writes the chore ledger row and sends what the
measurement says. A quiet chore sends the owner nothing.

This record fixes what a chore is, which chores the code holds, when one runs and as whom, what it
may not do, and how its result lands. What an instrument hands back is the instrument contract
([[ADR-0014-the-instrument|ADR-0014]]); the wake a chore runs inside, the process and its identity
lock belong to the long-running agent ([[ADR-0013-the-agent|ADR-0013]]).

The handlers run only under a profile holding `comm.send` ([[ADR-0001-the-actor#^c4|ADR-0001/C4]]),
and mail between processes is the layer [[ADR-0010-delegation#^c4|ADR-0010/C4]] names. Source:
`Chores`, `Outcome`, `Instrument` and `Bounded` in `hub/agent/chores.py`; `instruments()` in
`hub/agent/instruments.py`; `Record` in `hub/agent/lookups.py`; `Agent.deliver` in
`hub/agent/agent.py`; `build`, `render` and `land` in `apps/cli/lion_cli/agent.py`.

## Definitions

- **chore**: an instrument named in the agent's `chores.toml`, run by the `check` handler on a tick
  or when a trusted sender asks, with its period, its recipients and its ledger row.
- **tick**: a repeating reminder the agent sets for itself in the store; it arrives as mail reading
  `tick: ` and the name of what it runs, a chore, `digest` or `aged`.
- **ending**: how one run of a chore closes: quiet, report or escalation, chosen by code from the
  measurement.
- **chore ledger**: `ledger.jsonl` in the agent directory, append-only, one row per check.
- **trusted set**: the owner, the steward, the agent's own actor, and the senders `chores.toml`
  adds.
- **answered run**: a run whose control passed over a counted population, empty only where the chore
  declared emptiness expected.
- **standing state**: findings equal to the last ones a recipient received from that chore; they are
  not sent to that recipient again.

## Assumptions

| #  | statement                                                                                                   | source                                                                                              | if false                                                                  |
| -- | ----------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| A1 | A reminder fires into its creator's inbox, and one the store could not deliver in time is marked missed or failed and never fired again. | `_catch_up` reads the store's `missed` and `failed` rows; the firing is the store's, outside this repository (assumption) | ticks need a second carrier, and the catch-up reads nothing               |
| A2 | The instruments' commands only read.                                                                        | the argv in `hub/agent/instruments.py` and `hub/agent/lookups.py`; nothing checks them at run time  | a chore can change what it measures, and C6 needs a sandbox (ADR-0011)    |
| A3 | A model maps a tick or a question to one handler call and nothing more.                                     | the chores guidance; no model text reaches a report (C1) (assumption)                               | a tick names its handler and code calls it without a model                |

## Claims

### C1: A chore ends quiet, reported or escalated, and code picks the ending _(enforced: mechanical)_ ^c1

- **Subject**: every `check` a chore runs.
- **Violated when**: the model's text picks an ending or reaches a report, a fourth ending exists,
  or a finding leaves without the instrument's own output beside it.

The model's part is the call a tick or a question names. The handler runs the instrument and vets
the measurement (`Outcome` in the code): findings without evidence, a missing population, predicate
or count, and a passed control with no known-positive read are refused, and a failed control voids
the answer and escalates. An empty population the chore did not declare expected is a finding, and
leaves the run unanswered.

No findings is quiet: a row, and no mail beyond the answer a question is owed. Findings are a report
to the owner. An escalation, marked so, goes to the owner and the steward; the agent never performs
the remedy. A chore runs as one command inside a wake ([[ADR-0013-the-agent|ADR-0013]]).

### C2: The chores are the ones `chores.toml` names, from a fixed set of twelve _(enforced: mechanical)_ ^c2

- **Subject**: every agent built from its directory.
- **Violated when**: a chore runs that the file does not name, or one is built without its error
  direction or the question it answers.

`instruments()` builds only the chores the file names, each with its arguments and an optional
period. `Chores` refuses at build a chore that declares no direction, over-report or under-report,
or no `answers`; every report prints both, so the reader knows which way to doubt. What each reads,
and what it keeps between runs in the agent's notes:

| chore               | reads                                                                                                           | keeps                                             | errs toward  |
| ------------------- | --------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- | ------------ |
| `pr-state`          | open pull requests per repository through `gh`, and CI at each head on the channel the repository declares     | a snapshot per repository; later runs report transitions | over-report  |
| `required-contexts` | each open pull request's required checks by name, from rulesets and classic protection, and their state at its head | a snapshot per repository                         | under-report |
| `verdict-freshness` | the newest review verdict file per open pull request against its head; the pull requests from `gh` or a configured enumerator | one snapshot                                      | over-report  |
| `ci-red`            | the failing lines at each red head, from `gh run view --log-failed` or a local CI transcript                    | a snapshot per repository                         | under-report |
| `served`            | each served agent's last-wake note, pid file and launchd row                                                     | nothing                                           | under-report |
| `gtd-inbox`         | the assignee's inbox tasks older than the triage age, through the store                                         | nothing                                           | under-report |
| `inbox-sla`         | unread mail older than the age per watched actor, by `comm.probe`, no bodies                                     | nothing                                           | under-report |
| `disk`              | free space per volume against each named floor (`df`), and build targets over a size cap (`find`, `du`)          | nothing                                           | over-report  |
| `worktrees`         | each repository's worktrees: branch, dirty count, head age, open pull request                                   | a snapshot per repository                         | over-report  |
| `bench-jobs`        | the bench actor's notes, the pid of each unreported job, and that job's ledger                                  | nothing                                           | under-report |
| `daemon-passes`     | a producer's pass mail in the owner's box, through the desk's reader                                            | a cursor, moved once the owner has the report     | under-report |
| `record`            | issues and pull requests by word search, the ADR corpus's headings and rejected alternatives, and decision notes in the store | nothing                                           | under-report |

### C3: A chore runs on its tick, on a trusted question, or on a missed tick, inside a wake _(enforced: mechanical)_ ^c3

- **Subject**: every run of a chore.
- **Violated when**: a chore runs on a clock inside the process, a missed tick is left or run twice,
  a question is answered off its thread, or a chair's agent creates a tick.

At start the agent creates each missing tick: one per chore with a period, the digest's, the aged
pass's when it has a steward, and those a front desk or a mail watch adds. A tick arrives as mail
from the agent itself and wakes it; no chore runs on a clock inside the process. Once per run,
before its first check, the agent runs its own ticks the store marked missed or failed and not yet
seen.

A trusted sender's question is answered on its own thread, whatever the ending; `record` has no
period and runs only when asked. A chair's agent runs no chore: its profile offers no `check` and it
creates no tick.

### C4: A chore runs as the agent's actor, behind the gate, and the model cannot widen who asked _(enforced: mechanical)_ ^c4

- **Subject**: every chore run and every question that reaches one.
- **Violated when**: mail from outside the trusted set reaches a run, the model names an asker
  outside it or a thread the wake did not admit, or a second process acts as the same identity.

The store client signs as the actor `chores.toml` names, from a directory whose config must resolve
to it, and asserts that actor on every call. One process holds the identity at a time
([[ADR-0013-the-agent|ADR-0013]]). The agent's gate drops mail from outside the trusted set after
marking it read: the cursor names it, and it never reaches a run or a model call.

A question names its asker; the handler refuses an asker outside the trusted set and a thread no
admitted message from that asker carries. `daemon-passes` reads the owner's box through a second
client bound to the owner's identity, with two read verbs and no send.

### C5: A chore reaches the store only through the bounded client, and mails only the owner, the steward or who asked _(enforced: mechanical)_ ^c5

- **Subject**: every store call and every send a chore makes.
- **Violated when**: a verb outside the read list reaches the store, a memory above salience 0.4
  passes, a call the client cannot name passes, or mail leaves for a recipient outside the trusted
  set.

`Bounded` checks every op before the call. Its verbs must be reads, the agent's own mail and
reminders, or `memory.remember` at salience 0.4 or lower. An op with more calls than named verbs,
one shaped as JSON or as an option, and `comm.send` as an op are refused whole.

Every send goes through `Agent.deliver`, which charges the thread's hop count before the transport;
a check's send over the cap lands on its row as undelivered. `Bounded.send` refuses a recipient
outside the trusted set and the desk's routes.

### C6: A chore changes no tree, so its result lands by print and save _(enforced: process)_ ^c6

The instruments' commands read: `gh` lists, views and GETs, `git` worktree lists, status, log and
remote lookups, `ps`, `launchctl print`, `df`, `find` and `du`. The chores profile offers `check`,
`digest`, `aged` and the handlers a front desk or a mail watch adds; none is a file tool or a note
command, so a blank `note.find` ([[ADR-0007-the-record#^c5|ADR-0007/C5]]) never reaches a chore. The
handler reads and writes the agent's notes by key. A chore has no diff to land.

Its hand-run form is print and save: `lion agent --check <chore>` runs the instrument alone, prints
it and saves the same text in the agent directory's `landing` folder, one file per chore and day; it
sends nothing and writes no row. Where the product does make a diff, the box's copy, the diff is
printed and saved, and `--apply` writes it ([[ADR-0011-the-box|ADR-0011]]). The gate is code review
of an instrument's argv and of any tool the chores profile gains.

### C7: A report names its prior, is told once per recipient, and the steward hears only what the owner did not answer _(enforced: mechanical)_ ^c7

- **Subject**: every report, digest and aged pass.
- **Violated when**: a report lacks `supersedes`, a standing state reaches one recipient twice, one
  recipient's copy holds back another's, or findings reach the steward other than by escalation, a
  dead instrument, the aged pass or its own question.

A report carries `supersedes`, the owner's last report of that chore or `no-prior`, and goes on one
thread per chore, recipient and day, which the hop cap bounds. The standing state is kept per
recipient; evidence is capped at 4,000 characters, and says so.

`dead_after` unanswered runs in a row escalate once as a dead instrument; an answered run clears it.
The digest gives the owner one line per chore, `MISSING` where none ran that day, and the day's
tokens. The aged pass tells the steward once of each report the owner received and the steward did
not, left unanswered on its thread past `aged_after` periods of the chore.

## Decisions

### D1: `Chores` in `hub/agent/chores.py` ^d1

Serves C1, C3 and C7. `Chores` registers `check`, `digest` and `aged` on the agent's actor, each
requiring `comm.send`, and offers them as the `chores` profile. `check` runs the missed ticks first,
then the instrument, the refusals, the sends, the row, the commit of what the run consumed, and the
dead-instrument check. The defaults: aged after 2 periods, dead after 3 runs, the digest daily, the
aged pass hourly.

- **Landing evidence**: `tests/test_chores.py`, 30 tests: the three endings, each refusal, the
  standing state per recipient, the dead instrument, the digest, the aged pass, the missed tick,
  `ensure_ticks`.

### D2: `instruments()` and `Record` build the chores from `chores.toml` ^d2

Serves C2. `instruments(cfg, khive, state, reader)` builds each chore a `[chores.<name>]` table
names, with its period and `expect_empty`; `record` joins up to three lookups from
`hub/agent/lookups.py`, and one part's failure fails its control. Each program path is resolved
once, to an absolute path where one is found, because a launchd PATH holds neither brew nor cargo.

- **Landing evidence**: `tests/test_instruments.py` (102 tests, 4 live reads skipped unless
  `LION_GH_SMOKE=1`) and `tests/test_lookups.py` (8), with
  `test_the_factory_builds_the_record_chore_from_its_config` and
  `test_a_chore_without_an_error_direction_or_a_question_is_refused_at_build`.

### D3: `Bounded` wraps the store client ^d3

Serves C5. `Bounded.exec` blanks string literals, counts calls against named verbs, checks each verb
against `ALLOWED_VERBS`, and caps `memory.remember` at 0.4, all before the call. `Bounded.send`
checks the recipient. Under it, one store client process runs per call, from the agent directory,
with the expected actor asserted (C4).

- **Landing evidence**: five tests in `tests/test_chores.py`: the read list and recipients, failing
  closed on an unnamed call, JSON and option dispatch refused, quoted batches admitted, salience
  capped.

### D4: Ticks are the store's reminders, and missed ones are caught up once ^d4

Serves C3. `ensure_ticks` reads the actor's own pending reminders from `schedule.agenda` and creates
each missing one with `schedule.remind`, first firing a minute out. `_catch_up` lists up to 200
scheduled rows, keeps the actor's own marked missed or failed, and runs each once, remembered under
`missed_seen`. The CLI creates ticks at serve start and on `--ticks`, never for a chair.

- **Landing evidence**: `test_ensure_ticks_creates_the_reminders_that_are_missing_and_only_those`,
  `test_a_missed_tick_of_the_actors_own_is_run_at_the_next_check_and_only_once`;
  `test_a_chair_wakes_on_the_owners_mail_reads_its_directory_and_answers_on_the_thread` asserts
  `--ticks` creates nothing for a chair.

### D5: `lion agent --check` prints and saves ^d5

Serves C6. `agent_main` claims the identity, runs the named instrument with no arguments, prints
`render()` of it and writes the same text with `land()`, overwriting the day's file of that name. It
bypasses the handler: no refusals, no row, no send.

- **Landing evidence**: `test_once_refuses_served_identity` in `tests/test_agent.py` pins its
  refusal while a live process holds the identity; no test drives a positive run.

## Alternatives

| approach                                   | rejected because                                                                                                   |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| The model picks quiet, report or escalate  | the reporting path would carry model text; code holds the control, the count and the population to decide from     |
| A timer loop inside the agent              | a second scheduler beside the wake; a reminder arrives as mail, and a scheduled process is the kernel's (S1)       |
| Findings to the steward in a digest        | the steward would read each finding twice; it hears escalations, dead instruments and what the owner left unanswered |
| One suppression flag per chore             | the owner's copy would hold back the steward's undelivered escalation; the standing state is kept per recipient     |
| The agent performing the remedy it found   | a boundary with one exemption is a precedent; the escalation names the remedy and the owner performs it            |
| A chore that writes its fix into the tree  | a chore reads; a diff the box makes is printed and saved, and written only on `--apply` (ADR-0011)                 |
| An empty read taken as clean               | a broken instrument answers empty too; the control and the declared empty state decide                             |

## Consequences

- **S1**: The boundary: a scheduled run of a process, its timer, lifetime, identity and supervisor,
  is the kernel's and is not decided here. Here the period is a reminder in the store that arrives
  as mail, and a chore is the in-process form: one handler call inside a wake of the long-running
  agent ([[ADR-0013-the-agent|ADR-0013]]).
- **S2**: The store boundary is enforced in the process: a client run by hand from the agent
  directory is not bounded, and no store-side refusal is relied on.
- **S3**: The missed-tick read takes one page of 200 scheduled rows and filters to the actor in
  code; a busy namespace can push a missed row past the page.
- **S4**: A standing state is told once until it changes, a standing failure included: a chore that
  recovers quietly and fails again the same way is not resent. The digest carries the counts, and an
  owner who wants a state again asks.
- **S5**: `daemon-passes` never ends quiet: its clean run is one liveness line, because its producer
  mails only on news and the mail cannot tell a quiet producer from a dead one. An empty window is
  not an answered run, and `dead_after` in a row escalate as a dead instrument.
- **S6**: The hand-run check skips the handler's refusals, so it shows the raw measurement, and no
  test drives it past the identity claim. The `verdict-freshness` enumerator runs as the config
  gives it, outside anything that checks it only reads.
- **S7**: The chore ledger is read whole on every check and never rotated, so a check's cost grows
  with the agent's age; its stamps are local time without a zone.
- **S8**: A front desk runs a chore's instrument through the same refusals to answer the owner's
  mail, and the digest's period becomes the desk's brief, every 12 hours unless set
  ([[ADR-0017-the-desk|ADR-0017]]).
- **S9**: A mail watch configured under `[mail]` rides the same profile and ticks, but it is not a
  chore: `check` never runs it, and its code settles rows through its tracker's own commands, which
  write. This record does not cover it.
