---
adr: ADR-0014
status: draft
liveness: operating (twelve instruments build from one config and run under tests against scripted commands and stores; the command-line check prints a measurement unvetted and has no test)
date: "2026-09-25"
area: instrument
kind: new
depends_on:
  - "[[ADR-0005-command-handling|ADR-0005]]"
  - "[[ADR-0007-the-record|ADR-0007]]"
tags:
  - adr
  - agent
  - instrument
---

# ADR-0014: The instrument contract

## Context

A long-running agent is asked the same questions about the world around it, on a cadence or on an
ask: which pull requests are red, how much disk is free, whose inbox is past its bound. The model
routes the question and never judges the answer, so the answer carries its own proof.

Three wrong answers are designed out: an empty read from a broken instrument that renders as
clearance, a count whose population nobody can check, and a value hovering under a threshold told as
news every time. This record fixes what an instrument is, what its measurement carries, what it may
do, and how a failure is told and to whom.

The chore that schedules an instrument and delivers what it says is ADR-0015 (chores, not yet
written); the desk answers mail with instruments (ADR-0017, the desk, not yet written); the bench
has its own record (ADR-0012, the bench, not yet written). Source: `hub/agent/instruments.py`;
`Instrument`, `Outcome`, `Chores._vet` and `Chores._dead` in `hub/agent/chores.py`; `Record` in
`hub/agent/lookups.py`; `build`, `render` and `land` in `apps/cli/lion_cli/agent.py`.

## Definitions

- **instrument**: a read-only probe of something outside the run, on a cadence (`every`) or on an
  ask; it declares its name, `about`, the question it `answers`, its error direction and
  `empty_expected`, and `run(args)` returns its measurement.
- **measurement**: what one run of an instrument hands back: findings, evidence, control, escalate,
  population, predicate, count, known-positive, structured rows and an optional commit. The code
  calls it `Outcome`; in this corpus an outcome is a run's terminal value only.
- **control**: the check an instrument runs in the invocation that produces its answer; when it
  fails, the answer is void.
- **known-positive**: the text of what the control read, the thing that had to be there for the read
  to count: the repository's own name coming back, the agent's own pid alive, any task at all in the
  namespace.
- **error direction**: which way an instrument's failures run, `over-report` or `under-report`, with
  a `mechanism` sentence naming why.
- **snapshot**: the state an instrument keeps between runs in the agent's note store, one key per
  instrument and subject, from which it tells first sight and transitions.
- **steward**: the actor that hears, beside the owner, every escalation, every dead instrument and
  the reports the owner leaves unanswered past a bound; none means the owner alone.

## Assumptions

| #  | statement | source | if false |
| -- | --------- | ------ | -------- |
| A1 | Several channels an instrument reads answer a missing subject the way they answer an empty one. | commit statuses answer `[]` for a missing subject; an unshared task namespace answers empty; `comm.probe` answers 0 and an empty page alike; `ps -p` prints nothing for a missing pid (the instrument notes in `hub/agent/instruments.py`) | a return-code check would do, and the known-positive is ceremony |
| A2 | `df` prints one row per volume, so roots on one device share one free-space figure. | `Disk.run` groups roots by the first column of `df -k`; the disk test reads two roots on one device | a floor is read per root and one device counts twice |
| A3 | A capped list says nothing about the rows beyond its cap. | `gh pr list --limit` with `PR_PAGE` 100; the workflow-runs read with `_RUNS_PAGE` 50 beside its `total_count` | a full page is a complete list and the verify read is wasted |

## Claims

### C1: A measurement names its population, predicate and count, and what its known-positive read _(enforced: mechanical)_ ^c1

- **Subject**: every measurement the chore handler or the desk receives.
- **Violated when**: findings without evidence, or a passing control over a blank population, a
  blank predicate, a count of `None` or a blank known-positive, pass unrefused.

The population is a noun phrase, the predicate text with its match count, the count an integer.
`Chores._vet` refuses each missing field before anything is sent: it appends the reason to the
findings and fails the control, so the refusal escalates (C2).

Each instrument builds its control into the read that answers: the repository resolving as itself
before its pull requests are listed, the agent's own pid through the same `ps` read, any task at all
in the batch that lists the task inbox. An instrument that raises is a measurement with a failed
control and the exception as its evidence.

### C2: A failed control voids the answer and escalates to the owner and the steward _(enforced: mechanical)_ ^c2

- **Subject**: every measurement whose control failed, the refusals of C1 included.
- **Violated when**: it is sent as quiet or as a plain report, the steward is never told, or the
  desk replies with it.

`_vet` appends "its answer is void" and sets `escalate`. On the chore path the report goes to the
owner and to the steward, marked so, and an empty answer from a broken instrument escalates instead
of reading clean. On the desk path the message is left for the owner and nothing is sent.

An instrument sets `escalate` itself when a finding's remedy is destructive (C6). The remedy stays
the owner's: an escalation names it and performs nothing.

### C3: An empty population is a finding unless the instrument declared it expected _(enforced: mechanical)_ ^c3

- **Subject**: every measurement whose control passed over a count of zero.
- **Violated when**: it is quiet on an instrument that did not declare `empty_expected`, or its
  ledger row says it answered.

A subject that went away reads like one that never arrived, so an undeclared empty population is
reported to the owner as "population empty and not declared expected", naming the population,
without escalation. `answered` is the run's own verdict beside `control`: a control that passed over
a counted population, empty only where declared. The task inbox, the mail bound and the bench jobs
expect empty by default, the rest do not, and `expect_empty` in the config overrides.

### C4: An instrument that never answers is a finding about the instrument _(enforced: mechanical)_ ^c4

- **Subject**: every chore's unanswered ledger rows.
- **Violated when**: `dead_after` consecutive unanswered runs pass untold, the crossing is told
  again on every run after it, or an answered run leaves the mark.

`Chores._dead` counts the chore's unanswered rows back from the newest. At `dead_after`, three by
default, it tells the owner and the steward once that the instrument is dead, and marks the chore
told. An answered run clears the mark, so the next crossing is told again. The desk writes no chore
ledger row, so a measurement it received does not count here.

### C5: First sight is the whole row, then transitions, and a crossing is told once _(enforced: mechanical)_ ^c5

- **Subject**: the snapshot instruments: `pr-state`, `required-contexts`, `verdict-freshness`,
  `ci-red`, `worktrees`.
- **Violated when**: a subject born in a bad state is never reported, a standing state is reported
  every run, or `pr-state` calls a pull request absent from a full page closed without reading it.

A subject first seen is reported whole, whatever state it was born in; after that only what changed:
a head, its CI verdict, a review decision, mergeability, a branch, an open pull request. A crossing,
stale or parked, is told once and again only after it cleared.

A list is one page. A pull request absent from a full page is read on its own with `gh pr view`
before `pr-state` calls it closed, and a failed view fails the control; `required-contexts` and
`ci-red` keep its last row unread. A short page needs no verify.

### C6: A floor is read by name, once per device, with its own budget, in GiB base-1024 _(enforced: mechanical)_ ^c6

- **Subject**: the `disk` instrument.
- **Violated when**: a floor is read per root or without its budget, the value sits in the finding
  text, a crossing of a floor not marked `enforcing` escalates, or a `find` or `du` failure passes
  the control.

Roots are grouped by the device `df -k` names, and each floor is read once per device with its roots
listed. Free space minus the floor's `budget_gib` is compared with `floor_gib` in GiB base-1024, the
base-1000 figure printed beside it. An `enforcing` floor's crossing escalates, since its remedy is a
deletion; any other crossing reports.

The finding names the floor and the device and the value stays in the evidence, so a value hovering
under a floor is the same finding each run, told once by the chore's repeat rule. No `floors` means
one default floor of 300 GiB; an empty list declines every floor.

### C7: An instrument reads and acts on nothing _(enforced: process)_ ^c7

An instrument reads: `gh` reads, `git` reads, `ps`, `launchctl print`, `df`, `find` and `du`, and
the store through the agent's bounded client, which admits only the verbs on its list (ADR-0015,
chores, not yet written). It writes only its snapshot or cursor to the agent's note store
([[ADR-0007-the-record#^d3|ADR-0007/D3]]) and sends nothing; the report is the handler's. A remedy,
a restart or a removal, is the owner's.

The command runner has no read list of its own, and `verdict-freshness` may run an enumerator
command from the config as given. The gate is review of each instrument's commands.

### C8: The model sees one line; the owner gets the measurement _(enforced: mechanical)_ ^c8

- **Subject**: every run of the `check` handler.
- **Violated when**: a ledger row lacks one of the fields listed below, or evidence is cut without
  saying so.

The handler's result ([[ADR-0005-command-handling|ADR-0005]]) is one line: the chore, how it ended,
the number of findings and who was told. The report and the ledger row carry the rest: findings,
evidence, control, escalate, answered, error direction, population, predicate, count and
known-positive, the evidence capped at `evidence_cap` characters with the cap stated. The structured
rows are neither sent nor kept. The model's system text lists each instrument's `about`, error
direction, cadence and question.

## Decisions

### D1: One builder names the instruments from the config ^d1

Serves C3 and C8. `instruments(cfg, khive, state, reader)` builds only what the `[chores.<name>]`
tables name, twelve names in all, with their arguments and a cadence from `every` (none: on ask
only). Constructors refuse a malformed declaration at build; `Chores` refuses an error direction
outside the two and a blank `answers`. `every` is a store repeat such as `every:30m` or `daily`;
only `daemon-passes` has a default, twelve hours. `gh` and `git` resolve once to an absolute path
where one is found.

- **Landing evidence**:
  `test_instruments_wires_the_desk_chores_and_signs_as_the_owners_desk_by_default` and
  `test_build_refuses_daemon_passes_without_a_desk_and_names_the_missing_section` in
  `tests/test_instruments.py`;
  `test_a_chore_without_an_error_direction_or_a_question_is_refused_at_build` in
  `tests/test_chores.py`.

### D2: `Chores._vet` on both paths that run an instrument inside the agent ^d2

Serves C1, C2 and C3. The `check` handler and the desk's `answer` handler turn a raised exception
into a failed control, then call `_vet`, which appends each refusal as a finding, computes
`answered` and escalates a failed control. The desk leaves a message whose measurement did not
answer.

- **Landing evidence**: in `tests/test_chores.py`,
  `test_findings_without_the_instruments_own_output_are_refused_before_send`,
  `test_an_outcome_without_population_predicate_count_or_known_positive_is_refused`,
  `test_an_empty_population_is_a_finding_unless_the_chore_declared_it_expected`,
  `test_an_instrument_that_raises_or_whose_control_fails_escalates_to_owner_and_steward`;
  `test_a_void_answer_is_left_with_the_instruments_own_reason_and_nothing_is_sent` in
  `tests/test_desk.py`.

### D3: `Chores._dead` reads the ledger after each row ^d3

Serves C4. The mark lives under `dead_told` in the agent's state; the crossing is told to each
recipient on the chore's thread of the day.

- **Landing evidence**:
  `test_a_chore_that_never_answers_is_escalated_once_as_a_dead_instrument_and_an_answer_clears_it`
  in `tests/test_chores.py`.

### D4: A snapshot per instrument and subject; a cursor retired by `commit` ^d4

Serves C5. `pr-state:<repo>`, `required-contexts:<repo>`, `ci-red:<repo>`, `worktrees:<repo>` and
`verdict-freshness` are put once per run. An ask narrowed to named pull requests (`prs`) answers
every named row and leaves the `required-contexts` and `ci-red` snapshots untouched. `daemon-passes`
keeps a cursor instead, moved only by the measurement's `commit`, which the handler calls after the
owner's report landed or was a standing repeat.

- **Landing evidence**: in `tests/test_instruments.py`,
  `test_pr_state_first_sight_is_the_full_row_then_transitions_and_the_stale_crossing_once`,
  `test_pr_state_verifies_a_pr_absent_from_a_full_page_before_calling_it_closed`,
  `test_worktrees_first_sight_then_a_parked_dirty_tree_without_a_pr`,
  `test_ci_red_first_sight_then_only_new_red_then_no_red_then_closed`,
  `test_daemon_passes_the_window_retires_only_after_the_owners_report_lands`.

### D5: The `disk` instrument ^d5

Serves C6. `Disk.run` reads `/bin/df -k` per root, groups by device and reads every floor per
device, then finds `target` and `node_modules` directories to depth four with `/usr/bin/find` and
sizes each once with `/usr/bin/du`, reporting those over `target_cap_gib`, 20 by default.

- **Landing evidence**: the two disk tests in `tests/test_instruments.py`: one device for two roots,
  the enforcing crossing alone escalating, both units printed, `find` and `du` failures failing the
  control, the default and the declined floors.

### D6: `lion agent --check <name>` runs one instrument outside the handlers ^d6

Serves C8, and stands outside C1 to C3. The command builds the agent from its directory, takes the
identity's lock (refused while the agent serves there), calls `run({})`, prints `render()` and
writes the text to a dated file in the agent directory, the day's latest run of that name kept.
Nothing is sent, no ledger row is written, and `_vet` is not called.

- **Landing evidence**: none; no test drives `--check`, `render` or `land` (S3).

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| The model reads the raw output and judges it | the model routes; the refusals sit in code on the reporting path, where a misread count cannot reach the owner as clean |
| A control run as its own call | it can pass while the read that answered failed; the known-positive comes from that read |
| An empty population read as quiet | a subject that went away reads like one that never arrived (C3) |
| The value in the finding text | a value hovering under a floor would be a new finding every run; the floor's name keys it (C6) |
| Transitions only, no first sight | a subject born in a bad state is never reported (C5) |
| A verify read for every pull request absent from any page | a short page is a complete list; the read is spent only where the page could have cut (A3) |
| One floor list for every volume | each floor belongs to what enforces it and carries its budget |
| The count inside the population text | a number inside a noun phrase is not a field the handler can refuse on |

## Consequences

- **S1**: A chore is an instrument with a cadence and a delivery path. The tick, the ledger row, the
  three ends (quiet, report, escalate), the thread of the day, the repeat rule, the digest and the
  aged pass are ADR-0015's (chores, not yet written); this record owns what the instrument hands
  over and what is refused.
- **S2**: A snapshot instrument writes its snapshot inside its run, before any report lands. A
  transition is not reported to the owner again when its report was not delivered, or when a run off
  the chore path measured it: `lion agent --check`, or a desk answer (one narrowed to named pull
  requests spares only the `required-contexts` and `ci-red` snapshots). Only `daemon-passes` defers
  its cursor to `commit`.
- **S3**: `lion agent --check` prints a measurement unvetted and has no test; a missing
  known-positive prints as "(none)" and nothing refuses it.
- **S4**: A full page of 100 open pull requests costs one `gh pr view` per pull request the last
  snapshot held and the page lacks, on every `pr-state` run. `verdict-freshness` has no full-page
  check and calls a pull request beyond the page no longer open.
- **S5**: `mechanism` is declared by every instrument and read by no code; the structured rows (a
  check inventory per pull request, red lines per run, verdict rows) are read by no code either.
- **S6**: The model can narrow or replace the population through `args`: `pr-state` takes `repos`
  and `inbox-sla` takes `actors` in place of the configured ones. The `check` handler sets
  `source_event` over whatever the args name.
- **S7**: The bench's scripts hand back no measurement of this shape. The instrument that touches
  the bench is `bench-jobs`, which reads the jobs the bench actor noted; whether the bench meets
  this contract is ADR-0012's (the bench, not yet written).
- **S8**: The boundary: this record is product-side and names no kernel concern. An instrument reads
  with the credentials and namespace its process was started with; a read they deny is a failed
  control, or an empty answer the control must catch (A1).
