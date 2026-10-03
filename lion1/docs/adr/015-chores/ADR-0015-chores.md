---
adr: ADR-0015
status: draft
liveness: operating (the handler, the gate, the store boundary, the ticks, the catch-up over every page of the store's filtered lists and the aged pass's one telling per report are pinned by tests with scripted backends and a fake store; the hand-run check is driven both ways)
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
- **periodic chore**: a chore with a repeat or a UTC cron. The digest reads it `MISSING` absent a
  run in its period and grace, a cron `not judged`; a chore with neither runs only on an ask and
  reads `on ask: no run`, which counts runs, not asks.

## Assumptions

| #  | statement                                                                                                   | source                                                                                              | if false                                                                  |
| -- | ----------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| A1 | A reminder fires into its creator's inbox, and one the store could not deliver in time is marked missed or failed and never fired again. | `_catch_up` reads the store's `missed` and `failed` rows; the firing is the store's, outside this repository (assumption) | ticks need a second carrier, and the catch-up reads nothing               |
| A2 | The instruments' commands only read.                                                                        | the argv in `hub/agent/instruments.py` and `hub/agent/lookups.py`; nothing checks them at run time  | a chore can change what it measures, and C6 needs a sandbox (ADR-0011)    |
| A3 | A model maps a tick or a question to one handler call and nothing more.                                     | the chores guidance; no model text reaches a report (C1) (assumption)                               | a tick names its handler and code calls it without a model                |
| A4 | The store advances a `daily`, `weekly` or `every:N` reminder by fixed seconds, and a `monthly` one by calendar months in UTC from its first firing, clamped to a short month's last day. It fires a reminder at most 300 seconds late, its default grace. | the store's repeat parser, outside this repository (assumption) | the digest reads a chore `MISSING` before its tick is due, or late past it |

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

### C2: The chores are the ones `chores.toml` names, from a fixed set of thirteen _(enforced: mechanical)_ ^c2

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
| `lock-holder`       | the pids holding each named lock file open (`lsof`), each with its full command line (`ps`)                      | nothing                                           | under-report |
| `scheduled-job`     | each job's artifact age against its bound, then its launchd row's last exit                                      | nothing                                           | under-report |
| `gtd-inbox`         | the assignee's inbox tasks older than the triage age, through the store                                         | nothing                                           | under-report |
| `inbox-sla`         | unread mail older than the age per watched actor, by `comm.probe`, no bodies                                     | nothing                                           | under-report |
| `disk`              | free space per volume against each named floor (`df`), and build targets over a size cap (`find`, `du`)          | nothing                                           | over-report  |
| `worktrees`         | each repository's worktrees: branch, dirty count, head age, open pull request                                   | a snapshot per repository                         | over-report  |
| `bench-jobs`        | the bench actor's notes, the pid of each unreported job, and that job's ledger                                  | nothing                                           | under-report |
| `record`            | issues and pull requests by word search, the ADR corpus's headings and rejected alternatives, and decision notes in the store | nothing                                           | under-report |

### C3: A chore runs on its tick, on a trusted question, or on a missed tick, inside a wake _(enforced: mechanical)_ ^c3

- **Subject**: every run of a chore.
- **Violated when**: a chore runs on a clock inside the process, a missed tick is left or run twice,
  a question is answered off its thread, or a chair's agent creates a tick.

At start the agent creates each missing tick: one per chore with a period, the digest's, the aged
pass's when it has a steward. A tick arrives as mail from the agent itself and wakes it; no chore
runs on a clock inside the process. Once per run, before its first check, the agent runs its own
ticks the store marked missed or failed and not yet seen.

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
admitted message from that asker carries.

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
outside the trusted set and the desk's routes, but for the one reply a desk sends on the thread it
answers ([[ADR-0017-the-desk#^c12|ADR-0017/C12]]).

### C6: A chore changes no tree, so its result lands by print and save _(enforced: process)_ ^c6

The instruments' commands read: `gh` lists, views and GETs, `git` worktree lists, status, log and
remote lookups, `ps`, `lsof`, `launchctl print`, `df`, `find` and `du`. The chores profile offers
`check`, `digest`, `aged` and the handlers a front desk adds; none is a file tool or a note command,
so a blank `note.find` ([[ADR-0007-the-record#^c5|ADR-0007/C5]]) never reaches a chore. The handler
reads and writes the agent's notes by key. A chore has no diff to land.

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
The digest gives the owner one line per chore, `MISSING` where a periodic chore is overdue (S12 to
S14), and the day's tokens. The aged pass tells the steward once of each report the owner received
and the steward did not, left unanswered on its thread past `aged_after` periods of the chore.

## Decisions

### D1: `Chores` in `hub/agent/chores.py` ^d1

Serves C1, C3 and C7. `Chores` registers `check`, `digest` and `aged` on the agent's actor, each
requiring `comm.send`, and offers them as the `chores` profile. `check` runs the missed ticks first,
then the instrument, the refusals, the sends, the row, the commit of what the run consumed, and the
dead-instrument check. The defaults: aged after 2 periods, dead after 3 runs, the digest daily, the
aged pass hourly. The digest's line per chore names the last ending and counts the day's runs,
answered, quiet, report, escalate and instrument-failed.

- **Landing evidence**: `tests/test_chores.py`: the three endings, each refusal, the standing state
  per recipient, the dead instrument, the digest per period
  (`test_a_chore_with_no_run_yet_is_missing_only_past_its_first_due_and_the_grace`,
  `test_a_monthly_chore_is_due_on_its_anchor_day_when_its_first_due_is_on_record`,
  `test_a_ledger_row_that_parses_but_names_no_chore_or_time_makes_the_ledger_unreadable`,
  `test_a_ledger_row_the_digest_cannot_judge_from_makes_the_ledger_unreadable`,
  `test_a_fresh_agent_with_no_ledger_file_reads_no_run_yet_until_its_first_due_and_the_grace`), the
  aged pass (each report told once at 1,000 and 1,001 unanswered,
  `test_each_unanswered_report_is_told_to_the_steward_once_however_many_markers_the_ledger_holds`),
  the missed tick, `ensure_ticks`.

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

Serves C3. `ensure_ticks` reads the actor's pending reminders from `schedule.agenda` and creates
each missing one with `schedule.remind`, first firing a minute out (S13, S17). The CLI creates ticks
in the forms [[ADR-0016-the-lion-command#^c1|ADR-0016/C1]] lists, never for a chair.

`_catch_up` asks the store for the actor's missed and failed rows, filtered before each page of 200,
pages each list to its end, rechecks both in code and runs each row once. An id stays under
`missed_seen` while the store lists it; two misses ask the store for it by id (S9).

- **Landing evidence**: `test_ensure_ticks_creates_the_reminders_that_are_missing_and_only_those`,
  `test_ensure_ticks_keeps_a_chores_first_firing_once_and_never_overwrites_an_earlier_one`,
  `test_ensure_ticks_records_a_first_due_for_a_tick_it_finds_pending_from_its_own_row`,
  `test_a_pending_repeating_tick_gives_the_firing_its_history_admits_not_the_next_one`,
  `test_a_first_due_on_record_stands_over_the_pending_rows_and_one_that_is_no_instant_is_replaced`,
  `test_a_tick_made_now_leaves_a_first_due_on_record_where_it_is_later_or_earlier`,
  `test_a_pending_monthly_tick_with_no_anchor_gives_the_month_its_history_admits`,
  `test_a_missed_tick_of_the_actors_own_is_run_at_the_next_check_and_only_once`,
  `test_the_missed_tick_read_asks_the_store_for_the_actors_missed_and_failed_rows_before_the_page`,
  `test_missed_ticks_past_the_first_page_of_the_store_are_each_run_once` (201 rows, two passes),
  `test_a_seen_missed_tick_is_remembered_while_the_store_lists_it_however_many_there_are` (501
  rows), `test_a_row_the_pages_skip_twice_is_kept_on_the_stores_word_and_runs_once`,
  `test_a_page_that_is_not_a_page_fails_the_missed_tick_read_and_lets_no_id_go`,
  `test_the_seen_ids_are_matched_by_set_so_a_long_list_costs_each_row_one_look`;
  `test_a_chair_wakes_on_the_owners_mail_reads_its_directory_and_answers_on_the_thread` asserts
  `--ticks` creates nothing for a chair;
  `test_a_once_wake_reads_the_pending_ticks_so_a_digest_inside_the_first_grace_is_not_missing`.

### D5: `lion agent --check` prints and saves ^d5

Serves C6. `agent_main` claims the identity, runs the named instrument with no arguments, prints
`render()` of it and writes the same text with `land()`, overwriting the day's file of that name. It
bypasses the handler: no refusals, no row, no send.

- **Landing evidence**: `test_once_refuses_served_identity` in `tests/test_agent.py` pins its
  refusal while a live process holds the identity;
  `test_check_with_the_identity_free_prints_and_lands_the_measurement_and_sends_nothing` and
  `test_check_runs_one_instrument_prints_and_lands_it_and_sends_nothing` drive a positive run: the
  printed text is the landed text, and no send, row or tick follows.

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
  directory is not bounded, and no store-side refusal is relied on. A refusal in the store of any
  write by the agent's own actor is the kernel's to give, with the identity's grants
  ([[ADR-0013-the-agent|ADR-0013]] S9).
- **S3**: The missed-tick read passes the store's `list` the actor as creator and each of missed and
  failed as status, so another actor's rows and fired ones never fill a page. The creator is display
  metadata, not attribution, so the code checks both again, and reads a row's status at the item's
  top level, beside `properties`, as the store returns it.
- **S4**: A standing state is told once until it changes, a standing failure included: a chore that
  recovers quietly and fails again the same way is not resent. The digest carries the counts, and an
  owner who wants a state again asks.
- **S5**: The hand-run check skips the handler's refusals, so it shows the raw measurement;
  `test_check_runs_one_instrument_prints_and_lands_it_and_sends_nothing` drives it past the identity
  claim. The `verdict-freshness` enumerator runs as the config gives it, outside anything that
  checks it only reads.
- **S6**: The chore ledger is read whole on every check and never rotated, so a check's cost grows
  with the agent's age. Its `at` stamps are local time without a zone; a row also carries `ts`, the
  instant in epoch seconds, which the aged pass reads, and a row without one is read by its `at`.
  The aged pass keeps a marker per report told or found answered while its row is in the ledger, so
  the markers grow with it.
- **S7**: A front desk runs a chore's instrument through the same refusals to answer the owner's
  mail, and the digest's period becomes the desk's brief, every 12 hours unless set
  ([[ADR-0017-the-desk|ADR-0017]]).
- **S8**: The owner's own ask counts as the owner's receipt, whichever thread carried it: the
  answer's message and thread go on the row. A later tick names that answer as its prior and does
  not resend the same findings, and the aged pass waits for the owner's reply on the ask's thread.
  `test_the_owners_own_ask_keeps_its_receipt_so_the_report_ages_and_is_not_repeated` pins it.
- **S9**: Each list is paged, 200 to a page, by offset until the store says no more (`has_more`
  false, or a bare page short of 200), so no number of missed rows leaves one unread. A row an
  offset read skips is read by the next pass, its id held
  (`test_missed_ticks_past_the_first_page_of_the_store_are_each_run_once`, both page shapes). An id
  missed twice is asked for by `get`: still missed or failed is kept, moved on or not-found is let
  go, a failed read or an answer with no status keeps it
  (`test_a_row_the_pages_skip_twice_is_kept_on_the_stores_word_and_runs_once`).
- **S10**: A page that is no page, with no list in it or empty while the store says more is behind
  it, fails the whole read: nothing runs and no id is let go
  (`test_a_page_that_is_not_a_page_fails_the_missed_tick_read_and_lets_no_id_go`). Seen ids are
  matched by set, so a long list costs each row one look
  (`test_the_seen_ids_are_matched_by_set_so_a_long_list_costs_each_row_one_look`).
- **S11**: A missed tick of the chore a check is running is covered by that check: it is marked seen
  and noted, never run a second time
  (`test_a_missed_tick_of_the_chore_this_check_runs_is_covered_by_the_check_itself`). A check the
  ask narrowed covers nothing: the tick was the whole sweep, so it runs
  (`test_a_check_the_ask_narrowed_covers_no_missed_tick_of_its_chore`).
- **S12**: A periodic chore is overdue when the digest's moment, now or before the next midnight of
  a past day asked about, is past its due time and a grace. A fixed period, `every:<N><s|m|h|d>`,
  `daily` or `weekly`, is due one period after the last run; a `monthly` chore, on its anchor day in
  the month after the last run's (S14). The grace is the store's default, 300 seconds (A4):
  `MISSING` is a second past it, and `due by` names the due time
  (`test_a_weekly_chore_is_judged_against_a_week_since_its_last_run_not_against_the_day`,
  `test_an_every_90m_chore_reads_missing_past_90_minutes_though_it_ran_that_day`,
  `test_a_run_stamped_at_the_next_midnight_belongs_to_the_next_day_and_not_to_the_day_before`).
- **S13**: A chore's first due is the first firing of its tick, which `ensure_ticks` keeps in the
  agent's state under `first_due` (S17); one on record stands. A chore with no run reads `no run
  yet, first due by <instant>` until that instant and the grace are past, then `MISSING`. With none
  on record it reads one from the pending tick row (S26)
  (`test_a_chore_with_no_run_yet_is_missing_only_past_its_first_due_and_the_grace`,
  `test_ensure_ticks_keeps_a_chores_first_firing_once_and_never_overwrites_an_earlier_one`).
- **S14**: A monthly chore's anchor is the first due's UTC day and time of day, the day clamped to a
  month's last. A run off schedule, before or after the anchor day, moves nothing: the chore is due
  on the anchor day of the next month
  (`test_a_monthly_chore_is_due_on_its_anchor_day_when_its_first_due_is_on_record`). With none on
  record, a run on a month's last day is due by the next month's last, so a chore the store anchored
  on the 28th to the 30th reads `MISSING` up to three days late, never falsely
  (`test_a_monthly_chore_with_no_first_due_on_record_keeps_the_month_end_rule`,
  `test_a_monthly_chore_is_due_a_calendar_month_in_utc_after_its_last_run`).
- **S15**: A cron with no run that day reads `not judged: cron`; one that ran shows its run counts
  and then `; not judged: cron` (`test_a_cron_chore_is_not_judged_whether_or_not_it_ran_that_day`
  pins both forms); a daily chore's line is as it was
  (`test_a_daily_chore_reads_its_days_line_as_before_and_missing_a_day_after_its_last_run`). The
  aged pass counts a period by `period_s`: a monthly one as 30 days, a cron's as a day.
- **S16**: A ledger the digest cannot read judges no chore and says so: torn, undecodable,
  unreadable, or holding a row it cannot judge from (S18). Such a row is never skipped, as its run
  would read as a missing one. An empty ledger is judged, and an absent file is one: an agent whose
  chores have not run has written none, so it reads S13's line
  (`test_a_ledger_the_digest_cannot_read_reads_no_chore_missing_and_says_so`,
  `test_a_fresh_agent_with_no_ledger_file_reads_no_run_yet_until_its_first_due_and_the_grace`).
- **S17**: A first due on record stands, and one that is no instant (S23) is none. Where none is on
  record, a tick `ensure_ticks` makes gives its own firing, and one it finds pending gives its
  row's. The store moves `trigger_at` on after each delivery, so a fixed period's first due is the
  trigger less whole periods, the latest not after the ceiling (S25). The earliest of a tick's rows
  counts; a row naming no instant gives none
  (`test_a_pending_repeating_tick_gives_the_firing_its_history_admits_not_the_next_one`,
  `test_a_first_due_on_record_is_never_overwritten_by_a_pending_tick`,
  `test_a_first_due_on_record_stands_over_the_pending_rows_and_one_that_is_no_instant_is_replaced`,
  `test_a_tick_made_now_leaves_a_first_due_on_record_where_it_is_later_or_earlier`).
- **S18**: A row the digest can judge from has a non-blank `chore`, an `at` in the form a check
  writes, `YYYY-MM-DD HH:MM:SS` naming a time the clock has, none in a skipped hour, a `findings`
  list of strings, and boolean `control` and `escalate`. A `ts`, where it has one, is a finite
  number. Its time, `ts` or else `at`, is from 1970 to before the year 9999, and falls on the day
  `at` names, the day the digest selects the row by
  (`test_a_ledger_row_that_parses_but_names_no_chore_or_time_makes_the_ledger_unreadable`,
  `test_a_ledger_row_the_digest_cannot_judge_from_makes_the_ledger_unreadable`,
  `test_a_row_the_digest_cannot_select_by_its_label_is_not_a_chore_that_never_ran`,
  `test_a_label_and_an_instant_on_different_days_make_the_ledger_unreadable_naming_both`,
  `test_a_ts_that_is_not_a_finite_number_makes_the_ledger_unreadable_naming_the_field`,
  `test_a_row_with_no_ts_or_a_finite_number_for_one_is_judged`,
  `test_a_label_in_a_skipped_hour_names_no_time_and_one_in_a_repeated_hour_names_one`).
- **S19**: A row's `answered`, which the digest counts a run by, is a boolean where the row has one;
  a row from before the field has none, and its `control` says
  (`test_a_row_counts_as_answered_by_its_answered_field_and_by_its_control_where_it_has_none`).
- **S20**: A start that stopped between its reminder and its record heals at the next. At the edges
  of S17, a row created a period ago and next due in one gives the firing at its creation, the
  period exceeding the default lead and a second, and a firing at or before the ceiling (S25) is its
  own. A creation that is no instant, a cron and a zero period give the firing, or now if that is
  earlier (`test_a_row_created_a_period_ago_and_next_due_in_one_gives_the_firing_at_its_creation`,
  `test_a_pending_tick_whose_firing_precedes_its_creation_is_its_own_first_not_stepped_forward`,
  `test_a_pending_tick_with_no_instant_to_step_back_to_gives_its_firing_or_now_if_earlier`).
- **S21**: A monthly row gives its `repeat_anchor`. Without one it steps back whole UTC calendar
  months, days clamped to a month's last, to its latest firing not after the ceiling (S25). A short
  month's last day may be a clamped later one: the latest day that admits, at its time, not after
  the ceiling, or the ceiling where the step-back is after it. With no creation, the firing or now,
  if earlier (`test_a_pending_monthly_tick_gives_its_anchor_not_its_next_clamped_firing`, the
  `fired-before-creation-is-its-own` and `clamped-step-back-after-the-ceiling` rows of
  `test_a_pending_monthly_tick_with_no_anchor_gives_the_month_its_history_admits`,
  `test_a_clamped_monthly_row_reads_missing_a_month_and_the_grace_after_its_creation`,
  `test_a_clamped_monthly_row_is_missing_a_tick_lead_and_the_grace_after_its_creation`,
  `test_the_stand_in_and_the_tick_a_start_makes_share_one_lead`).
- **S22**: A day the digest is asked about is read as a date and named in the padded form a ledger
  label opens with, `2026-09-04` for `2026-9-4`, so either spelling selects the same rows and judges
  at the same moment. A text that is no date raises before anything is sent
  (`test_a_day_the_digest_is_asked_about_selects_the_same_rows_in_any_spelling_a_date_has`).
- **S23**: A stored `first_due` is a record only where it is a finite number, no bool, that a UTC
  date can name, in the digest and in `ensure_ticks` alike. Any other value is no record:
  `ensure_ticks` replaces it with the pending row's firing, and the digest reads that row as for
  none (S26), a chore with no tick pending reading `MISSING: <chore>, no run recorded, no tick
  pending`
  (`test_a_first_due_the_digest_cannot_read_as_an_instant_is_no_record_and_the_digest_delivers`,
  `test_a_first_due_a_clock_can_name_is_a_record_to_the_last_second_of_the_year_9999`,
  `test_a_first_due_a_platform_cannot_convert_and_refuses_with_an_oserror_is_no_record`,
  `test_a_chore_with_no_run_no_first_due_on_record_and_no_tick_pending_is_missing_and_says_so`).
- **S24**: A row's `ts` and `at` come from one read of the clock, `at` being that instant's local
  label, so a check across local midnight writes a row whose two name one day
  (`test_a_row_written_across_local_midnight_names_one_day_by_both_its_label_and_its_instant`).
- **S25**: A tick first fires the lead after its call's clock read, a minute by default
  (`ensure_ticks(first_in_s=...)`). `created_at` is stamped after it by the call's latency, which
  has no bound, so a row recovers its latest firing not after the ceiling: `created_at`, the default
  lead and a second for a whole-second stamp. A period within that latency and two seconds may
  recover whole periods late, `no run yet` past the due; a longer custom lead, on no row, early
  (`test_a_row_stamped_after_its_clock_read_and_delivered_once_recovers_its_true_first_firing`,
  `test_each_tick_ensure_ticks_makes_is_timed_from_its_own_read_of_the_clock`, the
  `stamped-a-period-late` and `a-longer-lead` rows of
  `test_a_pending_fixed_tick_gives_its_latest_firing_not_after_the_ceiling_its_creation_carries`,
  `test_a_second_start_keeps_the_first_due_of_a_tick_made_with_a_longer_lead`).
- **S26**: A chore with no run or first due on record reads one from its pending tick row (S17). It
  makes no tick and records nothing. The agenda is read once, when a chore needs it, 200 rows a page
  from the last row's `trigger_at`, merged by id, to 25 pages. A row naming no instant may be the
  earliest: `not judged: a pending tick row names no first due`, unrecorded. With no tick: `MISSING:
  <chore>, no run recorded, no tick pending`
  (`test_a_python_digest_wake_reads_a_pending_tick_row_for_a_chore_with_no_first_due`,
  `test_a_pending_tick_row_naming_no_instant_is_not_judged_and_gives_no_first_due`,
  `test_ensure_ticks_records_a_first_due_for_a_tick_it_finds_pending_from_its_own_row`,
  `test_the_digest_reads_the_agenda_once_and_only_for_a_chore_with_no_run_and_no_first_due`,
  `test_a_chore_with_no_run_no_first_due_on_record_and_no_tick_pending_is_missing_and_says_so`,
  `test_the_agenda_is_read_in_pages_of_200_each_from_the_last_row_of_the_page_before`,
  `test_an_agenda_read_that_ends_short_on_its_25th_page_is_not_capped`).
- **S27**: A read ending on a full 25th page is capped, and a row past the cap may give an earlier
  first due. A chore with no run or first due on record reads `not judged: the agenda read was
  capped at 5000 rows`, whatever it saw. `ensure_ticks` then makes no tick and records no first due,
  naming each tick it left. A read that fails reads each chore that needed it `not judged: agenda
  unreadable: <error>`, and the digest is still sent; `ensure_ticks` raises
  (`test_a_read_ending_on_a_full_25th_page_is_capped_and_judges_and_makes_no_tick`,
  `test_a_digest_whose_agenda_read_fails_says_so_for_each_chore_that_needed_it_and_is_sent`).
- **S28**: `from` is inclusive: a full page of rows at one instant comes back whole on the read
  after it. A full page bringing no new row stalls the read, which ends there, two pages not 25, the
  rows past that instant out of its reach. The chore reads `not judged: the agenda read stalled at
  <instant>: a full page of rows share that instant`; `ensure_ticks` makes and records nothing, as
  after a capped read
  (`test_a_full_page_of_rows_at_one_instant_stalls_the_read_which_ends_there_and_judges_nothing`,
  `test_a_stalled_read_that_shows_one_of_a_ticks_rows_judges_records_and_makes_nothing`).
