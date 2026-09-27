"""The hub's repository watch: what it tells the model, and when it stops the run."""

import asyncio

from hub.tools.watch import Watch, by_file, digest_of, is_test


def _diff(*files: tuple[str, str]) -> str:
    return "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-old\n+{body}\n" for p, body in files
    )


def _new(*files: tuple[str, str]) -> str:
    return "".join(
        f"diff --git a/{p} b/{p}\nnew file mode 100644\n--- /dev/null\n+++ b/{p}\n@@ -0,0 +1 @@\n+{body}\n"
        for p, body in files
    )


class FakeBox:
    def __init__(self, diffs: list[str], rc: dict[str, int] | None = None, out: dict[str, str] | None = None):
        self.diffs = iter(diffs)
        self.rc = rc or {}  # exit code per command
        self.out = out or {}  # stdout per command
        self.ran: list[str] = []

    async def diff(self) -> str:
        return next(self.diffs)

    async def run(self, cmd: str, *, timeout: float = 120.0, env: dict | None = None) -> tuple[int, str]:
        self.ran.append(cmd)
        return self.rc.get(cmd, 0), self.out.get(cmd, "")


class _Run:
    def __init__(self, round: int, *ran: tuple[str, int]):
        self.round = round
        self.record = [_Result(spec, r) for spec, r in ran]  # (spec, round) of commands that ran


class _Result:
    """A RESULT entry as the gate reads it: kind name, the result's spec, the round."""

    def __init__(self, spec: str, round: int, ok: bool = True):
        self.kind = type("K", (), {"name": "RESULT"})()
        self.content = type("C", (), {"spec": spec, "ok": ok})()
        self.round = round


async def _drive(watch: Watch, n: int) -> list[tuple[str | None, bool]]:
    out = []
    for r in range(1, n + 1):
        stopped = watch.stop()  # asked at the top of the round, before the observation
        out.append((await watch.section(_Run(r)), stopped))
    return out


def test_by_file_splits_on_headers_and_names_the_b_side():
    d = _diff(("django/x.py", "a"), ("tests/test_x.py", "b"))
    parts = by_file(d)
    assert list(parts) == ["django/x.py", "tests/test_x.py"]
    assert parts["django/x.py"].startswith("diff --git a/django/x.py")
    assert "+b" in parts["tests/test_x.py"] and "+b" not in parts["django/x.py"]


def test_by_file_unquotes_a_git_quoted_non_ascii_header():
    # P3-1 (hub review): `core.quotePath` (default on) wraps a non-ASCII header in quotes and
    # octal-escapes the bytes; splitting on " b/" then read the whole quoted header as one path
    quoted = (
        'diff --git "a/tests/test_\\303\\251.py" "b/tests/test_\\303\\251.py"\n'
        '--- "a/tests/test_\\303\\251.py"\n+++ "b/tests/test_\\303\\251.py"\n@@ -1 +1 @@\n-old\n+new\n'
    )
    parts = by_file(quoted)
    assert list(parts) == ["tests/test_é.py"]
    assert parts["tests/test_é.py"].startswith('diff --git "a/tests/test_\\303\\251.py"')
    assert is_test("tests/test_é.py")


def test_is_test_keys_on_the_tests_directory_and_test_file_names():
    assert is_test("tests/admin_views/tests.py")
    assert is_test("sphinx/tests/test_build.py")
    assert is_test("pkg/conftest.py")
    assert not is_test("django/test/runner.py")  # the test framework is source
    assert not is_test("sphinx/testing/util.py")
    assert not is_test("django/contrib/admin/helpers.py")


def test_nothing_is_said_before_a_source_change_and_scaffolding_is_named_apart():
    fix = _diff(("django/a.py", "fix"))
    seq = ["", _diff(("tests/t.py", "x")), fix, fix + _diff(("tests/t.py", "y"))]
    watch = Watch(FakeBox(seq), settle_after=6)
    rows = asyncio.run(_drive(watch, 4))
    assert rows[0][0] is None
    assert (
        rows[1][0] == "repository: no source change yet; tests/t.py changed under tests, not part of the fix"
    )
    assert rows[2][0] == "repository: the fix touches django/a.py, unchanged since round 2"
    assert rows[3][0] == (
        "repository: the fix touches django/a.py, unchanged since round 2; "
        "tests/t.py changed under tests, not part of the fix"
    )
    assert not any(stopped for _, stopped in rows)


def test_new_files_outside_the_tracked_tree_are_scaffolding_and_start_no_clock():
    # a repro script at the root and a throwaway package beside the source: the model's own, not the fix
    scratch = _new(("repro.py", "x"), ("tt/__init__.py", ""), ("tt/models.py", "class M: pass"))
    fix = _diff(("django/a.py", "fix"))
    added = _new(("django/db/new.py", "def f(): pass"))  # a new file inside the tracked tree is source
    box = FakeBox(
        [scratch] * 8 + [scratch + fix] + [scratch + fix + added],
        out={"git ls-files -- django | head -1": "django/__init__.py\n"},
    )
    watch = Watch(box, settle_after=6)
    rows = asyncio.run(_drive(watch, 10))
    assert rows[0][0] == (
        "repository: no source change yet; repro.py, tt/__init__.py, tt/models.py are new files outside "
        "the tracked tree, not part of the fix"
    )
    assert not any(stopped for _, stopped in rows)  # eight rounds of scratch never settle
    assert watch.state()["scratch"] == ["repro.py", "tt/__init__.py", "tt/models.py"] and watch.source == [
        "django/a.py",
        "django/db/new.py",
    ]
    assert rows[8][0].startswith("repository: the fix touches django/a.py, unchanged since round 8; repro.py")
    assert rows[9][0].startswith("repository: the fix touches django/a.py, django/db/new.py, unchanged since")
    assert box.ran.count("git ls-files -- tt | head -1") == 1  # asked once per directory, not per round


def test_the_notice_then_the_warning_then_the_stop_and_a_source_edit_resets_all_three():
    fix, fix2 = _diff(("django/a.py", "fix")), _diff(("django/a.py", "fix2"))
    # observed at rounds 1..: fix stands from round 2 through 8, changes at 9, then stands
    seq = [""] + [fix] * 7 + [fix2] * 8
    watch = Watch(FakeBox(seq), notice_after=3, settle_after=6)
    rows = asyncio.run(_drive(watch, len(seq)))
    texts = [t or "" for t, _ in rows]
    stops = [s for _, s in rows]
    assert "it has stood for" not in texts[3] and "it has stood for 3 rounds" in texts[4]
    assert "the run ends after this round" not in texts[5] and "the run ends after this round" in texts[6]
    # the fix is first seen at round 2 (stable 0) and stands through round 8 (stable 6): round 9 is refused
    assert stops[:8] == [False] * 8 and stops[8] is True
    # the edit observed at round 9 resets: since moves, the counters start over, round 10 runs
    assert texts[8] == "repository: the fix touches django/a.py, unchanged since round 8"
    assert stops[9] is False and "it has stood for" not in texts[8]
    assert "it has stood for 3 rounds" in texts[11]


def test_settle_zero_never_stops_and_never_warns():
    fix = _diff(("django/a.py", "fix"))
    watch = Watch(FakeBox([fix] * 12), settle_after=0)
    rows = asyncio.run(_drive(watch, 12))
    assert not any(s for _, s in rows)
    assert not any("the run ends" in (t or "") for t, _ in rows)
    assert "it has stood for 11 rounds" in rows[-1][0]


def test_checks_that_already_pass_are_refused_and_nothing_is_stored():
    box = FakeBox([], rc={"pytest -q t.py": 0})
    watch = Watch(box, settle_after=6)
    text = asyncio.run(watch.declare(["pytest -q t.py"], 300))
    assert text.startswith("refused: all 1 checks already exit 0")
    assert watch.criteria is None and not watch.passed and not watch.stop()


def test_checks_rerun_only_when_the_source_changes_and_end_the_run_when_they_pass():
    fix1, fix2 = _diff(("django/a.py", "one")), _diff(("django/a.py", "two"))
    # the last diff is read twice: once by the section, once after the checks pass, to see they left it
    box = FakeBox(["", fix1, fix1, fix2, fix2], rc={"python repro.py": 1, "pytest -q t.py": 0})
    watch = Watch(box, settle_after=6)
    text = asyncio.run(watch.declare(["python repro.py", "pytest -q t.py"], 300))
    assert text.startswith("declared 2 acceptance checks") and "`python repro.py` exit 1" in text
    assert len(box.ran) == 2 and not watch.passed

    async def step(r: int) -> tuple[str | None, bool, int]:
        stopped = watch.stop()  # asked at the top of the round, before the observation
        return await watch.section(_Run(r)), stopped, len(box.ran)

    async def drive():
        rows = [await step(1), await step(2)]  # no source yet: nothing runs; then checks run, still fail
        box.rc["python repro.py"] = 0  # the tree now passes, but the diff did not change
        rows += [await step(3), await step(4)]  # no rerun on a stale digest; the diff changed: all pass
        return rows

    rows = asyncio.run(drive())
    assert rows[0] == (None, False, 2)
    assert "acceptance 1/2 pass" in rows[1][0] and rows[1][1:] == (False, 4)
    assert "acceptance 1/2 pass" in rows[2][0] and rows[2][1:] == (False, 4)
    assert rows[3][0].endswith("all acceptance checks pass, the run ends here") and rows[3][1:] == (False, 6)
    assert watch.passed and watch.stop()  # the next round's stop() ends the run
    assert watch.state()["checks"] == [("python repro.py", 0), ("pytest -q t.py", 0)]


def test_out_is_refused_while_a_declared_check_fails():
    from hub.tools.watch import accept

    fix = _diff(("django/a.py", "one"))
    box = FakeBox([fix, fix, fix, fix], rc={"python repro.py": 1})
    watch = Watch(box, settle_after=6)
    asyncio.run(watch.declare(["python repro.py"], 300))
    asyncio.run(watch.section(_Run(2)))  # the fix landed in round 1
    why = asyncio.run(accept(watch, _Run(2, ("bash", 2)), {}))
    assert why == "your own acceptance checks fail: `python repro.py` exit 1"
    box.rc["python repro.py"] = 0
    assert asyncio.run(accept(watch, _Run(3, ("bash", 2)), {})) is None and watch.passed


def test_out_is_refused_until_a_command_ran_after_the_source_changed():
    # 13212 (unaided hard45): six edits in round 2, one quick check, OUT{} in round 4, wrong fix;
    # 13837: OUT{} with only the tests it had itself edited as evidence
    from hub.tools.watch import accept

    fix = _diff(("django/a.py", "one"))
    box = FakeBox([fix] * 6)
    watch = Watch(box)
    # round 1 edited; the OUT{} arrives in the same round: the section has not seen the change
    why = asyncio.run(accept(watch, _Run(1, ("bash", 1)), {}))
    assert why.startswith("the source changed in this round; nothing has run against that change")
    asyncio.run(watch.section(_Run(2)))  # round 2 opens: the change is known, since round 1
    why = asyncio.run(accept(watch, _Run(2, ("bash", 1), ("read_lines", 2)), {}))
    assert why.startswith("no command has run since the source changed in round 1")
    assert asyncio.run(accept(watch, _Run(2, ("bash", 1), ("bash", 2)), {})) is None


def test_the_first_refusal_never_names_round_0():
    # P3-5 (hub review): `since = run.round - 1` read round 0 when the change already stood at
    # round 1's own section call, and round 0 does not exist
    from hub.tools.watch import accept

    fix = _diff(("django/a.py", "one"))
    box = FakeBox([fix] * 6)
    watch = Watch(box)
    asyncio.run(watch.section(_Run(1)))
    assert watch.since == 1
    why = asyncio.run(accept(watch, _Run(2), {}))
    assert why == (
        "no command has run since the source changed in round 1, so the change is not "
        "verified: run the nearest tests first, then finish"
    )
    assert asyncio.run(accept(watch, _Run(3, ("python", 2)), {})) is None


def test_a_command_the_actor_refused_is_not_a_command_that_ran():
    # a `bash` with the wrong argument name, an unknown `run`, a hook's refusal: the actor writes a
    # RESULT for each and box.exec was never called, so none of them verifies the change
    from hub.tools.watch import accept

    box = FakeBox([_diff(("django/a.py", "one"))] * 6)
    watch = Watch(box)
    asyncio.run(watch.section(_Run(1)))
    run = _Run(2)
    run.record = [_Result("bash", 2, ok=False), _Result("run", 2), _Result("python", 2, ok=False)]
    why = asyncio.run(accept(watch, run, {}))
    assert why.startswith("no command has run since the source changed")
    run.record.append(_Result("bash", 2))  # rc 3 is still a command that ran; ok is the handler's
    assert asyncio.run(accept(watch, run, {})) is None


def test_a_new_file_beside_an_edited_one_keeps_the_digest_the_section_computed():
    # F01: git_diff lists the tracked edit before the new file; sorted, the new file comes first.
    # The section sorted and the gate did not, so a verified fix was refused as "changed this round".
    from hub.tools.watch import accept

    fix = _diff(("pkg/z.py", "one")) + _new(("pkg/a.py", "two"))
    box = FakeBox([fix] * 4, out={"git ls-files -- pkg | head -1": "pkg/z.py\n"})
    watch = Watch(box)
    asyncio.run(watch.section(_Run(1)))
    assert watch.source == ["pkg/a.py", "pkg/z.py"]
    assert asyncio.run(accept(watch, _Run(2, ("bash", 2)), {})) is None


def test_a_change_under_tests_or_outside_the_tree_alone_is_not_a_fix():
    # P1-4 (hub review): a tests-only edit and a scratch script passed the gate as "the source
    # changed" because the gate read scratch off the section's stale list; a fix edits source
    from hub.tools.watch import accept

    tests_only = _diff(("tests/test_a.py", "assert True"))
    scratch = _new(("repro.py", "print(1)"))
    box = FakeBox([tests_only] * 2 + [scratch] * 2 + [scratch + _diff(("pkg/a.py", "fix"))] * 3)
    watch = Watch(box)
    asyncio.run(watch.section(_Run(1)))
    why = asyncio.run(accept(watch, _Run(2, ("bash", 2)), {}))
    assert why.startswith("the change is under tests: tests/test_a.py, and nothing else")
    asyncio.run(watch.section(_Run(3)))
    why = asyncio.run(accept(watch, _Run(3, ("bash", 3)), {}))  # scratch of this round: classified afresh
    assert why.startswith("the change is new outside the tracked tree: repro.py, and nothing else")
    asyncio.run(watch.section(_Run(4)))
    assert asyncio.run(accept(watch, _Run(5, ("bash", 5)), {})) is None
    assert watch.source == ["pkg/a.py"] and watch.scratch == ["repro.py"]


def test_a_refused_declaration_racing_an_accepted_one_leaves_the_accepted_one_standing():
    # the actor runs a round's independent actions concurrently: measured on django-11951, a refused
    # set reset the criteria the sibling call had just declared, and OUT{} was then accepted unchecked
    box = FakeBox([], rc={"python passes.py": 0, "python fails.py": 1})
    watch = Watch(box, settle_after=6)

    async def both():
        return await asyncio.gather(
            watch.declare(["python passes.py"], 300), watch.declare(["python fails.py"], 300)
        )

    refused, declared = asyncio.run(both())
    assert refused.startswith("refused") and declared.startswith("declared 1")
    assert watch.criteria == ["python fails.py"] and watch.checks == [("python fails.py", 1)]
    assert asyncio.run(watch.declare([], 300)) == "refused: no checks given"
    assert watch.criteria == ["python fails.py"]


def test_a_new_binary_file_outside_the_tree_is_scaffolding(tmp_path):
    # `is_new` looked for the `--- /dev/null` hunk line; a binary file's chunk under `--binary` has
    # `new file mode` and a `GIT binary patch` block and no hunk, so a root-level .bin read as source
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "repro.bin").write_bytes(b"\0\1binary")
    (tmp_path / "note.txt").write_text("hi\n")
    chunks = ""
    for name in ("repro.bin", "note.txt"):
        done = subprocess.run(
            ["git", "diff", "--no-index", "--binary", "--", "/dev/null", name],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert done.returncode == 1 and done.stdout.startswith("diff --git")
        chunks += done.stdout
    assert "GIT binary patch" in chunks and "\n--- /dev/null\n" in chunks  # one of each shape
    watch = Watch(FakeBox([]))
    assert asyncio.run(watch.classify(by_file(chunks))) == ([], ["repro.bin", "note.txt"], [])


def test_a_pass_does_not_outlive_the_source_it_verified():
    # the no-source branch cleared the digest but kept `passed`, so a reverted fix still stopped the
    # run as done; source that comes back is checked again
    fix = _diff(("django/a.py", "one"))
    box = FakeBox([fix, fix, "", fix, fix], rc={"python repro.py": 1})
    watch = Watch(box, settle_after=6)
    asyncio.run(watch.declare(["python repro.py"], 300))
    box.rc["python repro.py"] = 0  # the fix makes it pass
    assert asyncio.run(watch.section(_Run(2))).endswith("all acceptance checks pass, the run ends here")
    assert watch.passed and watch.stop()
    assert asyncio.run(watch.section(_Run(3))) is None  # the model reverted its edit
    assert not watch.passed and not watch.stop() and watch.state()["checks"]  # the results stay readable
    ran = len(box.ran)
    assert asyncio.run(watch.section(_Run(4))).endswith("all acceptance checks pass, the run ends here")
    assert len(box.ran) == ran + 1 and watch.stop()  # checked again, not remembered


def test_a_check_that_rewrites_the_source_does_not_pass_the_tree_it_rewrote():
    # a formatter declared as a check exits 0 having changed the source: the pass was on the tree
    # before it, so the section holds the run open and the gate refuses the OUT{}
    from hub.tools.watch import accept

    fix1, fix2 = _diff(("django/a.py", "one")), _diff(("django/a.py", "two"))

    class Formatting(FakeBox):
        async def run(self, cmd, *, timeout=120.0, env=None):
            self.diffs = iter([fix2, fix2, fix2])  # the check rewrote the tree
            return await super().run(cmd, timeout=timeout, env=env)

    box = Formatting([fix1, fix1], rc={"black .": 0})
    watch = Watch(box, settle_after=6)
    watch.criteria, watch.criteria_timeout = ["black ."], 300
    line = asyncio.run(watch.section(_Run(2)))
    assert line.endswith("; a check changed the source, so the checks run again on the tree it left")
    assert not watch.passed and not watch.stop()
    line = asyncio.run(watch.section(_Run(3)))  # fix2 now stands; the check leaves it: the run ends
    assert line.endswith("all acceptance checks pass, the run ends here") and watch.passed
    box = Formatting([fix1, fix1, fix1], rc={"black .": 0})
    watch = Watch(box, settle_after=6)
    watch.criteria, watch.criteria_timeout = ["black ."], 300
    watch.digest, watch.since = digest_of(by_file(fix1), ["django/a.py"]), 1
    why = asyncio.run(accept(watch, _Run(2, ("bash", 2)), {}))
    assert why.startswith("an acceptance check changed the source") and not watch.passed


def test_a_check_that_raises_leaves_no_earlier_pass_standing():
    fix1, fix2 = _diff(("django/a.py", "one")), _diff(("django/a.py", "two"))
    box = FakeBox([fix1, fix1], rc={"python repro.py": 0})
    watch = Watch(box, settle_after=6)
    watch.criteria, watch.criteria_timeout = ["python repro.py"], 300
    assert asyncio.run(watch.section(_Run(2))).endswith("all acceptance checks pass, the run ends here")

    async def dead(cmd, *, timeout=120.0, env=None):
        raise RuntimeError("the box stopped answering")

    box.run = dead
    box.diffs = iter([fix2])
    try:
        asyncio.run(watch.section(_Run(3)))  # a new source change; its check raises
    except RuntimeError:
        pass
    else:
        raise AssertionError("the check's error was swallowed")
    assert not watch.passed and not watch.stop()
