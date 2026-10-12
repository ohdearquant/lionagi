---
adr: ADR-0027
status: proposed
liveness: operating (the refusal by name, link, hard link and shell text, its failure of the test or the run, the stand-in rule, the single reading of each value of a call and the source census are pinned by tests)
date: "2026-10-08"
area: suite
kind: new
depends_on:
  - "[[ADR-0025-the-graph|ADR-0025]]"
  - "[[ADR-0026-the-submission|ADR-0026]]"
tags:
  - adr
  - tests
  - suite
---

# ADR-0027: The suite's launch guard

## Context

`kkernel`, `khive` and `lion1-khive` each leave a daemon running against whatever store their home
and working directory lead them to. `claude` and `codex` each spend a real model call. A test run
must start none of them by accident.

Tests in both suites, `tests` and `apps/cli/tests`, therefore never start a real one of these five
programs. A test that means to declares it with `@pytest.mark.launches_cli(<name>)`. Besides the
guard's own tests, three declare a launch, each of `lion1-khive`: the built-binary store tests of
[[ADR-0025-the-graph|ADR-0025]] and [[ADR-0026-the-submission|ADR-0026]].

The guard sits on `subprocess.Popen._execute_child`, the function every `subprocess` and asyncio
launch passes. It reads what a call will start and raises `FileNotFoundError` before anything
starts. S1 to S9 state its limits. Source: `tests/conftest.py`, `apps/cli/tests/conftest.py`,
`tests/test_launch_guard.py`, `apps/cli/tests/test_cli_suite_guards.py`, `tests/test_store.py`.

## Definitions

- **guarded program**: `kkernel`, `khive`, `lion1-khive`, `claude` or `codex`, as a call names it,
  as its file resolves, or as the same file as one found on a PATH.
- **declaration**: `@pytest.mark.launches_cli(<name>, ...)` on a test; it covers the names it lists,
  for that test and the threads it starts through `threading.Thread.start`.
- **stand-in**: a program whose file, links resolved, lies under the session's temporary directory;
  the suite's stubs are stand-ins.
- **seam**: `subprocess.Popen._execute_child`, the one function every `subprocess` and asyncio
  launch passes.
- **census**: the source scan in `tests/test_launch_guard.py` of every Python file outside the
  directories pytest collects tests from.

## Assumptions

| # | statement | source | if false |
| - | --------- | ------ | -------- |
| A1 | Every program the suites start through `subprocess` or asyncio starts inside the seam, on each supported interpreter. | CPython's `Lib/subprocess.py` and `Lib/asyncio/unix_events.py`, read at 3.11.16, 3.12.14, 3.13.15 and 3.14.7 | a launch skips the seam, and the asyncio and plain-launch tests turn red on that interpreter |
| A2 | A shell text with none of the constructs D3 refuses starts only programs that its words name. | the measurements in the comments beside the scan in `tests/conftest.py`, made with bash and `sh` | a construct joins D3's whole-text refusals |
| A3 | The suite writes its stand-ins under the base directory of `tmp_path_factory`. | `kkernel_stub` in `tests/conftest.py`, and the `kkernel` scripts `tests/test_khive_client.py` writes under `tmp_path` | a stand-in written elsewhere reads as the real program and needs a declaration, which also lets the real program start in that test |

## Claims

### C1: An undeclared launch of a guarded program is refused before it starts _(enforced: mechanical)_ ^c1

- **Subject**: every `subprocess.Popen` call made while a test runs, asyncio's included.
- **Violated when**: a guarded program starts although a name it is called by is not declared, a
  refusal raises anything but `FileNotFoundError`, or the program had started when it raised.

The guard judges the program a call names, as written and as its file resolves, so a link to a
guarded program under another name is that program, and a hard link found on a PATH is judged by
file (S7). With `shell=True` it judges the shell, when the call names one, and every word of the
text (D3).

Every guarded name a program is called by must be declared; a program called by none starts when its
file is one a declaration leads to. A declaration is the running test's own: a thread alive before
the test was bound, or started by one, carries none. The error is the one a machine without the
program raises.

### C2: A refusal fails its test, or the run, even when the code swallowed the error _(enforced: mechanical)_ ^c2

- **Subject**: the refusals recorded for the running test, and the suite's own list for launches
  outside any test.
- **Violated when**: a refused launch leaves its test, or the run, passing, whether the code
  swallowed the error or the launch came from collection, a fixture of a wider scope, a thread a
  test left running, or a hook after the last test.

A refusal is recorded, and the test fails at teardown naming each program. A test of the guard
declares the refusals it expects with `refused_launches.expect([...])`, which checks and empties the
list; nothing else empties it.

Outside any test nothing is declared. A refusal there fails the next test's setup, or the session's
teardown after the last test, which first waits up to 60 seconds for the threads the interpreter
waits for at exit. A refusal in a hook after that is read at `pytest_unconfigure` and again at exit,
and fails the run.

### C3: A program the test wrote under the session's temporary directory starts undeclared _(enforced: mechanical)_ ^c3

- **Subject**: every program whose file, links resolved, lies under that directory and is not the
  file of a guarded program.
- **Violated when**: such a program is refused for its name, or a program outside the directory, a
  link from it to a program outside, or a hard link to a guarded program starts as a stand-in.

The `kkernel` stub and the scripts of the client tests are written there. A declaration on those
tests would also let the real program start in them, so a file under the directory needs none. A
directory whose name only begins with the temporary directory's name lies outside it. When a
stand-in is the `executable` of a `shell=True` call, it is the one program the call starts, and the
text is its argument.

### C4: Every value that selects what starts is read once and handed to Popen as read _(enforced: mechanical)_ ^c4

- **Subject**: `args`, `executable`, `cwd`, `env`, `shell` and `preexec_fn` of every call.
- **Violated when**: Popen starts a program other than the one the guard judged because a value
  answered a second reading differently, a call with no `env` gets anything but the guard's copy of
  `os.environ`, or a call with a `preexec_fn` starts.

A path-like, a mapping, or a `str` or `bytes` subclass can answer a second reading differently, so
each value is read once, as the exact text it holds, and that reading is what Popen starts. `shell`
is read once as a bool. A call with no `env` gets the copy of `os.environ` the guard read (S2). A
`preexec_fn` runs in the child between the reading and the start and can change what a name leads
to, so a call with one is refused whole. The code under test passes none.

### C5: Code under test that launches past `subprocess`, or evaluates text through a named call, fails the census when written _(enforced: mechanical)_ ^c5

- **Subject**: every Python file outside the directories pytest collects tests from, `__pycache__`,
  `node_modules` and dot-named directories, read by
  `test_the_code_under_test_launches_through_subprocess_only`.
- **Violated when**: such a file launches past `subprocess`, evaluates text through a call the
  census names, or imports by name a module the census does not list, and the test passes.

The census reads the source as code, so an alias (`import os as o`) reaches the same call.
`os.system`, `os.exec*`, `os.spawn*`, `os.posix_spawn*`, `pty.spawn`, `ctypes`, `multiprocessing`
and the private fork functions are launches. So are `eval`, `exec`, `compile`, the calls that
evaluate text such as `typing.get_type_hints`, and a string constant that reads as Python and
launches.

A linked directory, which neither count of the subject enters, fails the census (C6).

A module the code imports by name must be listed. A new one fails the census until it is read and
added, and one no longer imported fails it until it is removed.

### C6: The census refuses an import origin it cannot read _(enforced: mechanical)_ ^c6

- **Subject**: the tree's import paths and unread files, read by
  `test_the_code_under_test_launches_through_subprocess_only`.
- **Violated when**: a scanned file hands an import path on or names a loader that imports a file by
  its path, or the tree holds a file the counts never read, and the test passes.

Handing an import path on is a launch: `sys.path`, `getattr(sys, "path")` or a `__path__` assigned,
deleted, called on, passed or stored under a name, and a `__path__` imported under any name. So are
`sys.meta_path`, `sys.path_hooks`, `sys.path_importer_cache`, `importlib.machinery` and the loaders
that import a file by its path. Iterating, indexing or comparing one is not.

The counts never read a linked directory, a sourceless file (`.pyc`, `.so`, `.zip` and their kin),
or a module file under `__pycache__` or `node_modules` whose directory names an import can spell;
each stops the census. A lookup by a computed name is beyond a source scan.

- `test_the_source_check_reads_every_launch_past_subprocess`,
  `test_the_source_check_passes_what_stays_on_subprocess` (the import-path arms)
- `test_a_package_reached_through_a_linked_directory_stops_the_census`
- `test_a_module_file_under_a_left_out_directory_stops_the_census`

## Decisions

### D1: The seam is taken when `tests/conftest.py` is imported ^d1

Serves C1, C2 and C4. `_guarded_execute_child` replaces `Popen._execute_child` at import, so
collection and a fixture of any scope are judged, which a function-scoped fixture alone would not
do. The autouse fixture binds the running test's declarations. A reload of `subprocess` makes a
`Popen` without the seam, so each test's setup and teardown and the session's teardown check for it,
put it back and fail; a reload past the last of them is not checked (S9). The CLI suite's conftest
imports the same fixtures. `KHIVE_NO_DAEMON=1` is set at import too.

- **Landing evidence**: `tests/test_launch_guard.py`:
  `test_a_launch_from_a_fixture_of_a_wider_scope_is_refused_and_fails_the_next_test`,
  `test_a_test_that_replaces_subprocess_fails_and_the_seam_is_put_back`; in
  `apps/cli/tests/test_cli_suite_guards.py`,
  `test_the_launch_guard_and_the_no_daemon_setting_reach_the_cli_suite`.

### D2: `Refusals` and one lock: a test's list is read and emptied where its launches are recorded ^d2

Serves C2. `Refusals` is the list of one test, and the suite keeps another for launches outside
tests. A launch is judged and recorded under the lock that a test's binding and unbinding take, and
every reader that empties a list (`expect`, the read before each test, the reads after the last)
checks and empties it under that lock. A launch from a thread still in flight at its test's teardown
then lands in the list read next, never in one already read and let go.

- **Landing evidence**: `tests/test_launch_guard.py`:
  `test_every_reader_that_empties_a_ledger_takes_the_guards_lock`,
  `test_every_refusal_is_recorded_under_the_guards_lock`,
  `test_a_launch_in_flight_at_the_tests_teardown_fails_the_next_tests_setup`.

### D3: A shell text is read word by word and refused whole when the shell reads something before naming a program ^d3

Serves C1. Any guarded name in the text is refused, an argument included. An expansion refuses a
text whole, since what it starts is not in it: `$`, a backtick, an unquoted glob or brace, a leading
`~`. So does text the scan cannot read past: a comment, a here-document, an open quote, a line
continuation. So does a word that changes where a later word resolves (`cd`, `PATH=`, `eval`,
another shell), a startup variable (`BASH_ENV`), or an executable not named `sh`, `bash` or `dash`,
as written and as resolved.

- **Landing evidence**: `tests/test_launch_guard.py`:
  `test_a_word_the_shell_would_expand_resolves_nothing_and_is_refused`,
  `test_a_text_the_scan_cannot_read_past_is_refused_whole`,
  `test_a_shell_command_under_startup_state_in_the_environment_is_refused_whole`,
  `test_a_shell_command_whose_executable_is_not_sh_bash_or_dash_is_refused_whole`.

### D4: The census reads the source as code and pins the list of modules the code under test imports ^d4

Serves C5 and C6. `_launches_past_subprocess` parses each file, so an alias, an attribute and a
quoted name reach the same call. `_IMPORTED` lists every module the code under test imports by name,
each read once for the calls that launch or evaluate text, because what a library does inside a call
is beyond a source scan. A package of this tree that shares a library's name is told apart from it.
The scanned directories come from `testpaths`.

- **Landing evidence**: `tests/test_launch_guard.py`:
  `test_the_code_under_test_launches_through_subprocess_only`,
  `test_the_source_check_reads_every_launch_past_subprocess`,
  `test_the_census_reads_every_module_imported_by_name`,
  `test_the_census_boundary_is_named_calls_as_written`,
  `test_a_package_reached_through_a_linked_directory_stops_the_census`.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| An OS-level sandbox for the test process | it is one rule for the whole process, so a test could not declare the one launch it means to make (C1), and the suite runs on a developer's machine and on Linux in CI, which would each need a profile of their own |
| Patching every `os` launch function | `subprocess` and asyncio pass one function, and the code under test calls none of the `os` ones, which the census keeps true (C5); the patches would guard calls that code does not make and still miss native calls (S3) |
| An evaluator list the census keeps extending until it is complete | no such list can be finished, since any call that resolves an annotation evaluates text, so the list would always lack one more call (S1) |

## Consequences

- **S1**: The census holds code that launches, or evaluates text through a named call, as written.
  Text built while the code runs, from code points or from data, and handed to an evaluator it does
  not name is outside it. Any call that resolves an annotation evaluates text,
  `functools.singledispatch` registering a function by its annotation among them. Falsified by:
  `test_the_census_boundary_is_named_calls_as_written`, which pins one such case as not caught and
  its named-evaluator twin as caught; a census change that reverses either turns it red.
- **S2**: A test that sets a variable with `os.putenv` and leaves `os.environ` alone does not reach
  a child the suite starts, whatever program it is: with no `env` the child gets the copy of
  `os.environ` the guard read (C4). Change `os.environ`, or use `monkeypatch.setenv`. Falsified by:
  a child started with no `env` that sees a value set only through `os.putenv`.
  `test_a_call_without_an_environment_starts_with_the_one_read` pins the reading, and
  `test_a_program_on_a_path_only_os_putenv_set_does_not_start` pins it for a program not guarded.
- **S3**: `os.system`, `os.posix_spawn`, `os.spawn*`, `os.exec*`, `pty.spawn`,
  `subprocess._fork_exec`, `_posixsubprocess`, native calls through `ctypes` or `cffi`, and
  `multiprocessing`, called directly, do not pass `Popen`, so the seam does not see them. Tests are
  not checked for them, and the census holds the code under test off them (C5). Falsified by: a
  launch through one of them from a test that the guard refuses, which would make the seam wider
  than this says, or one from the code under test that the census passes.
- **S4**: A relink of a path, or a change of working directory, between the guard's reading and
  Popen's exec can start a file the reading did not judge. The guard stats the path and Popen execs
  the name; pinning the file would need an exec by descriptor, which Popen does not do. No test
  relinks a path a launch is using, except the one that pins this limit. Falsified by:
  `test_a_path_relinked_between_the_reading_and_the_exec_starts_what_it_now_names` turning red; it
  relinks a read path to a guarded program and finds that program started.
- **S5**: `env`, `nohup` and similar programs named as the program have their arguments unread:
  `["env", "codex"]` starts `env`, and only `env` is judged. What a script or another program the
  test started goes on to start is not seen, one named `sh`, `bash` or `dash` as a shell's
  executable included. In a shell text every word is judged, so the same wrapper there is refused by
  the guarded name it holds. Falsified by:
  `test_a_wrapper_named_as_the_program_has_its_arguments_unread` turning red; it starts a guarded
  program through `/usr/bin/env` and finds it started.
- **S6**: The session's teardown waits for the threads the interpreter waits for at exit. A daemon
  thread that outlives the last test is not waited for, and one still running is not named as a
  leak. Falsified by: the guard fixture's own teardown waiting for a daemon thread still running
  when it checks, or naming one as lingering.
- **S7**: A hard link to a guarded program keeps none of its name, so the guard finds it by file,
  against the guarded programs on the child's PATH and on the session's PATH. A guarded program that
  neither PATH reaches is known by its name alone, and a hard link to it under another name, started
  by path, is not recognized. Falsified by: such a hard link refused although neither PATH reaches
  its target.
- **S8**: A test that puts another function in the seam's place, and the guard back before its
  teardown, launches unjudged meanwhile: D1 checks the seam at each test's setup and teardown, not
  at each launch. No test replaces it, except the one that pins this limit. Falsified by:
  `test_a_test_that_swaps_the_seam_out_and_back_launches_unjudged_meanwhile` turning red; it puts
  back the function the guard wraps, starts a guarded program, and finds it started.
- **S9**: Nothing checks the seam past the session's teardown. A hook past it, or an `atexit`
  callback registered before the guard's, that reloads `subprocess` first launches through a class
  without the seam, unjudged, and the run passes. Falsified by:
  `test_a_reload_of_subprocess_past_the_sessions_teardown_launches_unjudged` turning red; it does so
  from a `pytest_unconfigure` hook and from such a callback, and finds the program started.
