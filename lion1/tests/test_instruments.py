"""The instruments (hub/agent/instruments.py) against a scripted runner: every one runs its control
in the same call and fails it on a dead read, a count-shaped finding names its population and
predicate, a repo's CI channel is read as declared, the first sight of a subject is its full row and
later runs report transitions, and a floor is read with the budget of the instrument that enforces
it."""

import asyncio
import asyncio.base_subprocess as base_subprocess
import contextlib
import errno
import hashlib
import json
import os
import re
import signal
import sys
import time
import tomllib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from lion_cli import agent as cli_agent
from lion_cli.agent import arm, build

from hub.agent import AgentError
from hub.agent import instruments as instr
from hub.agent.chores import Bounded
from hub.agent.instruments import (
    BenchJobs,
    CiRed,
    Disk,
    GtdInbox,
    InboxSla,
    LockHolder,
    PrState,
    RequiredContexts,
    ScheduledJob,
    Served,
    VerdictFreshness,
    Worktrees,
)
from tests.lionfake import resident_store, routed_config
from tests.lionfake import store as fresh_store
from tests.test_chores import FakeKhive


@pytest.fixture(autouse=True)
def _stub(kkernel_stub):
    """Every resident directory here reads its notes through the stub binary (ADR-0025 D6)."""


class FakeSh:
    """A runner answering by the first key found in the joined argv; an argv nothing matches fails,
    the way a missing binary does, so a control that does not run its command cannot pass."""

    def __init__(self, **answers: tuple[int, str, str] | str):
        self.answers = {
            k.replace("__", " "): (v if isinstance(v, tuple) else (0, v, "")) for k, v in answers.items()
        }
        self.calls: list[list[str]] = []

    def set(self, key: str, value: tuple[int, str, str] | str) -> None:
        self.answers[key] = value if isinstance(value, tuple) else (0, value, "")

    async def __call__(self, argv: list[str], *, timeout: float = 60, cwd: str | None = None):
        self.calls.append(list(argv))
        line = " ".join(argv)
        for key in sorted(self.answers, key=len, reverse=True):  # the most specific key answers
            if key in line:
                return self.answers[key]
        return 1, "", f"no fake for {line}"


def bench_dir(tmp_path):
    """A resident directory whose notes the stub binary serves, no note written yet."""
    d = tmp_path / "bench"
    d.mkdir()
    (d / "chores.toml").write_text('actor = "lambda:x:bench"\n')
    resident_store(d)
    return d


def iso(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def runs_page(rows: list[dict], total: int | None = None) -> str:
    """The workflow runs at a head as the runs query prints them: one page and the head's total_count."""
    return json.dumps({"total_count": len(rows) if total is None else total, "runs": rows})


PR = {
    "number": 7,
    "title": "agent folder",
    "headRefOid": "a" * 40,
    "updatedAt": iso(1),
    "reviewDecision": "",
    "isDraft": False,
    "mergeable": "MERGEABLE",
    "url": "https://github.com/o/r/pull/7",
}
RUNS = runs_page(
    [
        {
            "workflow_id": 501,
            "name": "ci",
            "event": "pull_request",
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2026-09-20T01:00:00Z",
        },
        {
            "workflow_id": 501,
            "name": "ci",
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-09-20T02:00:00Z",
        },
    ]
)


def pr_state(sh: FakeSh, repos, **kw) -> PrState:
    return PrState(repos, fresh_store().notes("agent"), every="every:30m", run=sh, **kw)


def test_pr_state_first_sight_is_the_full_row_then_transitions_and_the_stale_crossing_once():
    sh = FakeSh(
        **{"api repos/o/r --jq": "o/r\n", "pr list -R o/r": json.dumps([PR]), "actions/runs?head_sha=": RUNS}
    )
    inst = pr_state(sh, ["o/r"])
    out = asyncio.run(inst.run({}))
    assert out.control and out.findings == ("o/r: first sight, 1 open PR(s); the table is the row",)
    assert out.evidence.startswith("control: gh resolves o/r as itself; ci channel actions\n")
    assert (
        "o/r#7 agent folder | aaaaaaaaaa | ci=success(pull_request) | review=- | mergeable=MERGEABLE"
        in out.evidence
    )
    assert (
        out.population == "open PRs in ['o/r']"
        and out.predicate == "non-draft and updatedAt older than 3 days (0 match)"
        and out.count == 1
    )
    assert asyncio.run(inst.run({})).findings == ()  # nothing moved: quiet
    sh.set(
        "pr list -R o/r",
        json.dumps(
            [{**PR, "headRefOid": "b" * 40, "updatedAt": iso(100)}, {**PR, "number": 8, "title": "born red"}]
        ),
    )
    sh.set("actions/runs?head_sha=" + "b" * 40, runs_page([]))
    out = asyncio.run(inst.run({}))
    assert out.findings == (
        "o/r#7: sha aaaaaaaaaa -> bbbbbbbbbb",
        "o/r#7: verdict ci=success(pull_request) -> REQUIRED CHECKS DO NOT EXIST AT THIS HEAD (no runs)",
        "o/r#7: untouched for more than 3 days",
        "o/r#8: first sight: born red | aaaaaaaaaa | ci=success(pull_request) | review=- | "
        "mergeable=MERGEABLE",
    )
    assert out.predicate == "non-draft and updatedAt older than 3 days (1 match)" and out.count == 2
    assert asyncio.run(inst.run({})).findings == ()  # the stale crossing is told once


def test_pr_state_verifies_a_pr_absent_from_a_full_page_before_calling_it_closed(monkeypatch):
    monkeypatch.setattr(instr, "PR_PAGE", 2)
    two = [PR, {**PR, "number": 8, "title": "second"}]
    sh = FakeSh(
        **{"api repos/o/r --jq": "o/r\n", "pr list -R o/r": json.dumps(two), "actions/runs?head_sha=": RUNS}
    )
    inst = pr_state(sh, ["o/r"])
    out = asyncio.run(inst.run({}))
    assert out.control and out.positive.endswith(
        "answered 2 row(s) as JSON (a full page of 2: absences read on their own)"
    )
    sh.set(
        "pr list -R o/r", json.dumps([two[1], {**PR, "number": 9, "title": "third"}])
    )  # #7 fell off the page
    sh.set("pr view 7 -R o/r", "OPEN\n")
    out = asyncio.run(inst.run({}))
    assert out.control and [f for f in out.findings if "#7" in f] == []  # open beyond the page: not closed
    assert "o/r#7: open beyond the page; its last row is kept" in out.evidence
    assert ["pr", "view", "7", "-R", "o/r", "--json", "state", "--jq", ".state"] in [c[1:] for c in sh.calls]
    sh.set("pr view 7 -R o/r", "MERGED\n")
    out = asyncio.run(inst.run({}))
    assert "o/r#7: closed or merged" in out.findings
    sh.set("pr list -R o/r", json.dumps([{**PR, "number": 9, "title": "third"}]))  # a short page: no verify
    out = asyncio.run(inst.run({}))
    assert out.findings == ("o/r#8: closed or merged",) and not any("view" in c for c in sh.calls[-3:])
    sh.set("pr list -R o/r", json.dumps(two))
    asyncio.run(inst.run({}))
    sh.set("pr list -R o/r", json.dumps([two[1], {**PR, "number": 9, "title": "third"}]))
    sh.set("pr view 7 -R o/r", (1, "", "gh: HTTP 502"))
    out = asyncio.run(inst.run({}))
    assert (
        not out.control
        and "o/r#7: absent from a full page and gh pr view failed: gh: HTTP 502" in out.findings
    )


@pytest.mark.parametrize("answer", ["{}", "", '""', "null", '[{"number": ' + "9" * 4301 + "}]"])
def test_pr_state_a_pr_list_that_is_not_a_json_list_is_unread_and_keeps_the_last_rows(answer):
    sh = FakeSh(
        **{"api repos/o/r --jq": "o/r\n", "pr list -R o/r": json.dumps([PR]), "actions/runs?head_sha=": RUNS}
    )
    inst = pr_state(sh, ["o/r"])
    assert asyncio.run(inst.run({})).control
    kept = inst.state.get("pr-state:o/r").value
    sh.set("pr list -R o/r", answer)
    out = asyncio.run(inst.run({}))
    assert not out.control and out.findings == (f"o/r: gh pr list gave no JSON list: {answer[:120]!r}",)
    assert inst.state.get("pr-state:o/r").value == kept  # an unread list closes nothing
    sh.set("pr list -R o/r", "[]")  # a decoded empty list is a value: #7 is gone
    out = asyncio.run(inst.run({}))
    assert out.control and out.findings == ("o/r#7: closed or merged",) and out.count == 0


@pytest.mark.parametrize("state", ["", "\n", "UNKNOWN"])
def test_pr_state_a_pr_beyond_a_full_page_is_retired_only_on_an_explicit_closure(monkeypatch, state):
    monkeypatch.setattr(instr, "PR_PAGE", 2)
    two = [PR, {**PR, "number": 8, "title": "second"}]
    sh = FakeSh(
        **{"api repos/o/r --jq": "o/r\n", "pr list -R o/r": json.dumps(two), "actions/runs?head_sha=": RUNS}
    )
    inst = pr_state(sh, ["o/r"])
    asyncio.run(inst.run({}))
    sh.set("pr list -R o/r", json.dumps([two[1], {**PR, "number": 9, "title": "third"}]))
    sh.set("pr view 7 -R o/r", state)
    out = asyncio.run(inst.run({}))
    assert not out.control
    assert f"o/r#7: absent from a full page and gh pr view gave no state: {state.strip()!r}" in out.findings
    assert "o/r#7: closed or merged" not in out.findings and "7" in inst.state.get("pr-state:o/r").value
    sh.set("pr view 7 -R o/r", "CLOSED\n")
    out = asyncio.run(inst.run({}))
    assert out.control and "o/r#7: closed or merged" in out.findings
    assert "7" not in inst.state.get("pr-state:o/r").value


def test_pr_state_reads_each_repos_declared_ci_channel_and_a_statuses_read_must_be_non_empty():
    sh = FakeSh(
        **{
            "api repos/o/s --jq": "o/s\n",
            "api repos/o/n --jq": "o/n\n",
            "pr list -R o/s": json.dumps([PR]),
            "pr list -R o/n": json.dumps([PR]),
            "commits/": "[]",
        }
    )
    inst = pr_state(sh, [{"name": "o/s", "ci": "statuses"}, {"name": "o/n", "ci": "none"}])
    out = asyncio.run(inst.run({}))
    assert not out.control  # the statuses channel answers [] for a missing subject: asserted non-empty
    assert out.findings[0].startswith("o/s#7: CI channel statuses did not answer: statuses read gave no rows")
    assert "o/n#7 agent folder | aaaaaaaaaa | no CI by declaration" in out.evidence
    sh.set(
        "commits/",
        json.dumps(
            [{"context": "local/suites", "state": "success"}, {"context": "local/suites", "state": "pending"}]
        ),
    )
    out = asyncio.run(pr_state(sh, [{"name": "o/s", "ci": "statuses"}]).run({}))
    assert out.control and "| local/suites=success |" in out.evidence  # newest status per context wins
    assert not any("actions/runs" in " ".join(c) for c in sh.calls)
    with pytest.raises(ValueError, match="a repo's ci is actions, statuses or none"):
        pr_state(sh, [{"name": "o/x", "ci": "rollup"}])


def test_pr_state_now_reads_one_pr_from_gh_at_the_moment_of_asking_and_leaves_the_snapshot_alone():
    head = "c" * 40
    view = {
        "headRefOid": head,
        "state": "OPEN",
        "mergeStateStatus": "BLOCKED",
        "reviewDecision": "REVIEW_REQUIRED",
        "isDraft": False,
        "baseRefName": "main",
    }
    sh = FakeSh(
        **{
            "api repos/o/r --jq .full_name": "o/r\n",
            "api repos/o/r --jq {full_name": json.dumps({"full_name": "o/r", "id": 42, "admin": True}),
            "pr view 7 -R o/r --json": json.dumps(view),
            f"commits/{head}/status": json.dumps([{"context": "local/suites", "state": "failure"}]),
            "rules/branches/main": "[]",
            "branches/main/protection/required_status_checks": json.dumps(
                {"contexts": ["local/suites", "local/guards"], "checks": []}
            ),
            "pr list -R o/r": json.dumps([PR]),
            "commits/": json.dumps([{"context": "local/suites", "state": "success"}]),
        }
    )
    store = fresh_store().notes("agent")
    inst = PrState([{"name": "o/r", "ci": "statuses"}], store, every="every:30m", run=sh)
    row = asyncio.run(inst.now("o/r", 7))
    assert set(row) == {
        "repo",
        "number",
        "read_at",
        "errors",
        "headRefOid",
        "state",
        "mergeStateStatus",
        "reviewDecision",
        "isDraft",
        "baseRefName",
        "ci",
        "ci_answered",
        "required",
        "required_surfaces",
    }
    assert row["errors"] == [] and row["headRefOid"] == head and row["mergeStateStatus"] == "BLOCKED"
    assert row["ci"] == "local/suites=failure" and row["ci_answered"] is True  # the repo's declared channel
    assert row["required"] == ["local/guards", "local/suites"]
    assert row["required_surfaces"] == {"ruleset": "read (0 rule(s))", "classic": "read"}
    assert datetime.fromisoformat(row["read_at"]).tzinfo is not None
    assert store.get("pr-state:o/r") is None  # the snapshot is not touched
    out = asyncio.run(inst.run({}))
    assert out.findings == ("o/r: first sight, 1 open PR(s); the table is the row",)  # nor the transitions
    sh.set("pr view 7 -R o/r --json", (1, "", "GraphQL: Could not resolve to a PullRequest"))
    row = asyncio.run(inst.now("o/r", 7))
    assert row["errors"] == ["gh pr view failed: GraphQL: Could not resolve to a PullRequest"]
    assert row["headRefOid"] is None and row["required"] is None
    sh.set("api repos/o/r --jq {full_name", json.dumps({"full_name": "o/moved", "id": 42, "admin": True}))
    assert asyncio.run(inst.now("o/r", 7))["errors"][0].startswith("gh does not resolve o/r as itself")


def test_pr_state_control_fails_when_gh_does_not_return_the_repo_as_itself():
    sh = FakeSh(**{"api repos/o/r --jq": "other/r\n"})  # a rename redirects with rc 0
    out = asyncio.run(pr_state(sh, ["o/r"]).run({}))
    assert not out.control and out.findings == ("o/r: gh does not resolve it as itself: other/r",)
    out = asyncio.run(pr_state(FakeSh(), ["o/r"]).run({}))
    assert not out.control and "no fake for" in out.findings[0]


def test_served_reads_the_cursor_first_then_the_pid_then_launchd_with_its_own_pid_as_control(tmp_path):
    d = bench_dir(tmp_path)
    (d / "agent.pid").write_text("999999")
    stale = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3 * 3600))
    resident_store(d).put("cursor", {"at": stale, "outcome": "Success"}, run="-", seq=0)
    me = os.getpid()
    sh = FakeSh(
        **{f"-p {me} -o": "S+ 01:02\n", "-p 999999 -o": "", "print gui": (0, "  state = waiting\n", "")}
    )
    inst = Served([{"dir": str(d), "stale_min": 60, "label": "com.example.x"}], every="every:15m", run=sh)
    out = asyncio.run(inst.run({}))
    assert out.control and out.evidence.startswith(f"control: own pid {me} reads 'S+ 01:02'")
    assert out.findings == (
        "bench: pid file names 999999 and it is not running (no such process)",
        "bench: serving but last wake 180 min ago, bound 60",
        "bench: launchd com.example.x is loaded and waiting",
    )
    sh.set(f"-p {me} -o", "")
    out = asyncio.run(inst.run({}))
    assert not out.control and "NOT alive: read broken" in out.evidence


@pytest.mark.parametrize("device", ["/dev/disk4s1", "map auto_home", "a  b 7"])
@pytest.mark.parametrize("mount", ["/Volumes/My Disk", "/Volumes/two  spaces", "/"])
def test_disk_names_the_device_and_the_mount_point_whole_when_either_holds_a_space(tmp_path, device, mount):
    df = (
        "Filesystem    1024-blocks Used Available Capacity  Mounted on\n"
        f"{device}     1 1    {400 * 2**20}   1%    {mount}\n"
    )
    sh = FakeSh(**{"/bin/df": df, "find ": ""})
    out = asyncio.run(Disk([str(tmp_path)], every=None, floors=[], run=sh).run({}))
    assert out.control and out.evidence.splitlines()[1] == (
        f"{device} ({tmp_path}): free 400.0 GiB base-1024 / 429.5 GB base-1000, mounted on {mount}"
    )


@pytest.mark.parametrize("text", ["\u00b2", "\u0662", "12a", ""])
def test_served_reads_a_pid_file_as_ascii_digits_or_as_unreadable_and_probes_no_process_for_it(
    tmp_path, text
):
    d = bench_dir(tmp_path)
    (d / "agent.pid").write_text(text)
    me = os.getpid()
    sh = FakeSh(**{f"-p {me} -o": "S+ 01:02\n"})
    inst = Served([{"dir": str(d), "stale_min": 60, "label": "com.example.x"}], every="every:15m", run=sh)
    out = asyncio.run(inst.run({}))
    assert (
        out.control
        and out.findings[0] == f"bench: pid file names {text} and it is not running (unreadable pid file)"
    )
    assert [
        c for c in sh.calls if "-p" in c and str(me) not in c
    ] == []  # no ps for the pid it could not read


def test_disk_groups_roots_by_device_reads_each_floor_once_in_gib_and_escalates_only_an_enforcing_crossing(
    tmp_path,
):
    free_kib = 290 * 2**20  # 290 GiB base-1024 = 311.4 GB base-1000
    df = (
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        f"/dev/disk0s1 1948 1500 {free_kib} 84% /\n"
    )
    sh = FakeSh(
        **{"df -P -k": df, "find ": f"{tmp_path}/x/target\0", "du -sk": f"26000000\t{tmp_path}/x/target\n"}
    )
    floors = [
        {"name": "build admission", "budget_gib": 16, "floor_gib": 280, "enforcing": True},
        {"name": "steady", "budget_gib": 0, "floor_gib": 300},
    ]
    roots = [str(tmp_path), str(tmp_path / "x")]  # two roots, one device: each floor is read once
    inst = Disk(roots, every="every:2h", floors=floors, run=sh)
    out = asyncio.run(inst.run({}))
    assert out.control and out.escalate and out.count == 1
    assert out.findings == (
        f"build admission: under the floor on /dev/disk0s1 ({tmp_path}, {tmp_path}/x)",
        f"steady: under the floor on /dev/disk0s1 ({tmp_path}, {tmp_path}/x)",
        f"{tmp_path}/x/target: over the 20 GiB target cap",
    )
    assert (
        f"/dev/disk0s1 ({tmp_path}, {tmp_path}/x): free 290.0 GiB base-1024 / 311.4 GB base-1000"
        in out.evidence
    )
    assert (
        "  floor build admission: 274.0 GiB projected (free minus 16 GiB budget) against 280 GiB: UNDER "
        "[enforcing]" in out.evidence
    )
    assert (
        "  floor steady: 290.0 GiB projected (free minus 0 GiB budget) against 300 GiB: UNDER" in out.evidence
    )
    assert out.evidence.count("floor build admission") == 1  # once per device, not once per root
    assert out.population == f"volumes under {roots}" and out.predicate.endswith("in GiB (2 match)")
    assert (
        out.positive
        == "df parsed 1 device(s) for 2 root(s); floors read in GiB base-1024, the unit floors are "
        "written in"
    )
    assert [c[:3] for c in sh.calls[:2]] == [["/bin/df", "-P", "-k"], ["/bin/df", "-P", "-k"]]
    sh.set("df -P -k", df.replace(str(free_kib), str(297 * 2**20)))  # 281 projected: only the steady floor
    out = asyncio.run(inst.run({}))
    assert out.control and not out.escalate  # a steady-floor crossing reports; the owner decides
    assert out.findings[0] == f"steady: under the floor on /dev/disk0s1 ({tmp_path}, {tmp_path}/x)"
    sh.set("df -P -k", "garbage")
    out = asyncio.run(inst.run({}))
    assert not out.control and out.findings[0].endswith("df gave no free-space number: garbage")
    assert out.positive == "" and out.count == 0


def test_disk_fails_its_control_when_find_or_du_fail_and_absent_floors_inherit_the_default_floor(tmp_path):
    df = f"Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/disk0s1 1 1 {400 * 2**20} 1% /\n"
    sh = FakeSh(
        **{
            "df -P -k": df,
            "find ": (1, f"{tmp_path}/x/target\0", "find: permission denied"),
            "du -sk": "26000000\t/x\n",
        }
    )
    out = asyncio.run(Disk([str(tmp_path)], every=None, run=sh).run({}))
    assert not out.control and f"{tmp_path}: find failed (rc 1): find: permission denied" in out.findings
    assert f"{tmp_path}/x/target: over the 20 GiB target cap" in out.findings  # what it did find is sized
    sh.set("find ", f"{tmp_path}/x/target\0")
    sh.set("du -sk", (1, "", "du: cannot read"))
    out = asyncio.run(Disk([str(tmp_path)], every=None, run=sh).run({}))
    assert not out.control and out.findings == (f"{tmp_path}/x/target: du failed (rc 1): du: cannot read",)
    assert Disk(["/"], every=None).floors == [{"name": "steady (default)", "budget_gib": 0, "floor_gib": 300}]
    assert Disk(["/"], every=None, floors=[]).floors == []  # an empty list declines every floor
    with pytest.raises(ValueError, match="a floor has a name and a floor_gib"):
        Disk(["/"], every=None, floors=[{"name": "x", "floor_gb": 1}])


def test_disk_sizes_a_target_whose_path_holds_a_space_as_one_path(tmp_path):
    df = f"Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/disk0s1 1 1 {400 * 2**20} 1% /\n"
    spaced, plain = f"{tmp_path}/my app/target", f"{tmp_path}/x/node_modules"
    sh = FakeSh(**{"df -P -k": df, "find ": f"{spaced}\0{plain}\0", "du -sk": "1024\t-\n"})
    out = asyncio.run(Disk([str(tmp_path)], every=None, run=sh).run({}))
    assert out.control and out.findings == ()
    assert "-print0" in next(c for c in sh.calls if c[0] == "/usr/bin/find")
    assert sorted(c[2] for c in sh.calls if c[0] == "/usr/bin/du") == [spaced, plain]


@pytest.mark.parametrize(
    ("table", "refusal"),
    [
        ("target_cap_gib = nan", "target_cap_gib is a finite number of GiB, zero or more: nan"),
        ("target_cap_gib = -1", "target_cap_gib is a finite number of GiB, zero or more: -1"),
        ("target_cap_gib = true", "target_cap_gib is a finite number of GiB, zero or more: True"),
        ("target_cap_gib = 1" + "0" * 400, "target_cap_gib is a finite number of GiB, zero or more: 1000"),
        (
            "target_cap_gib = 0x" + "F" * 5000,
            "target_cap_gib is a finite number of GiB, zero or more: an integer of 20000 bits",
        ),
        (
            '[[chores.disk.floors]]\nname = "steady"\nfloor_gib = nan',
            "floor_gib of floor 'steady' is a finite",
        ),
        (
            '[[chores.disk.floors]]\nname = "steady"\nfloor_gib = inf',
            "floor_gib of floor 'steady' is a finite",
        ),
        (
            '[[chores.disk.floors]]\nname = "steady"\nfloor_gib = "300"',
            "floor_gib of floor 'steady' is a finite",
        ),
        (
            '[[chores.disk.floors]]\nname = "steady"\nfloor_gib = true',
            "floor_gib of floor 'steady' is a finite number of GiB, zero or more: True",
        ),
        (
            '[[chores.disk.floors]]\nname = "admit"\nfloor_gib = 280\nbudget_gib = nan',
            "budget_gib of floor 'admit' is a finite number of GiB, zero or more: nan",
        ),
        (
            '[[chores.disk.floors]]\nname = "admit"\nfloor_gib = 280\nbudget_gib = -16',
            "budget_gib of floor 'admit' is a finite number of GiB, zero or more: -16",
        ),
    ],
    ids=[
        "cap-nan",
        "cap-negative",
        "cap-bool",
        "cap-too-large-for-a-float",
        "cap-too-large-for-its-decimal-form",
        "floor-nan",
        "floor-inf",
        "floor-string",
        "floor-bool",
        "budget-nan",
        "budget-negative",
    ],
)
def test_disk_refuses_a_floor_budget_or_cap_that_is_not_a_finite_number_of_gib_at_build(table, refusal):
    cfg = tomllib.loads(f'[chores.disk]\nroots = ["/"]\n{table}\n')
    with pytest.raises(ValueError, match=re.escape(refusal)):
        instr.instruments(cfg, FakeKhive(), fresh_store().notes("agent"))
    fine = '[[chores.disk.floors]]\nname = "zero"\nfloor_gib = 0\nbudget_gib = 0.5\n'
    disk = instr.instruments(
        tomllib.loads(f'[chores.disk]\nroots = ["/"]\ntarget_cap_gib = 0\n{fine}'),
        FakeKhive(),
        fresh_store().notes("agent"),
    )
    assert (
        disk["disk"].floors == [{"name": "zero", "floor_gib": 0, "budget_gib": 0.5}] and disk["disk"].cap == 0
    )


def test_disk_reads_df_in_the_posix_layout_because_gnu_wraps_a_long_device_name_without_it(tmp_path):
    device, free_kib = "/dev/mapper/vg0-lv_root_on_a_long_name", 400 * 2**20
    wrapped = (  # GNU df -k: the source is past its column, so the numbers move to the next line
        "Filesystem           1K-blocks      Used Available Use% Mounted on\n"
        f"{device}\n                             1         1 {free_kib}   1% /\n"
    )
    posix = f"Filesystem 1024-blocks Used Available Capacity Mounted on\n{device} 1 1 {free_kib} 1% /\n"
    sh = FakeSh(**{"df -k": wrapped, "df -P -k": posix, "find ": ""})
    inst = Disk([str(tmp_path)], every=None, run=sh)
    out = asyncio.run(inst.run({}))
    assert out.control and out.count == 1 and out.findings == ()
    assert f"{device} ({tmp_path}): free 400.0 GiB base-1024" in out.evidence
    sh.set("df -P -k", wrapped)  # the wrapped row gives the parse no free-space column
    out = asyncio.run(inst.run({}))
    assert not out.control and out.findings == (
        f"{tmp_path}: df gave no free-space number: {wrapped.strip()[:120]}",
    )
    sh.set("df -P -k", (1, "", "df: No such file or directory"))
    out = asyncio.run(inst.run({}))
    assert not out.control and out.findings == (
        f"{tmp_path}: df gave no free-space number: df: No such file or directory",
    )


class Transport:
    """What `proc._transport.close()` reaches: records that the pipes were closed."""

    closed = False

    def close(self):
        self.closed = True


class Child:
    """A command's leader as the OS treats its process group. Running, it ends at the kill and its pipes
    drain. `gone` has it leave just as the kill arrives: "reaped" is already collected, so the group no
    longer exists (ESRCH); "unreaped" is exited and not yet collected, the only member left, so macOS
    answers EPERM until the drain has reaped it. `blocked` is a live group this process may not signal:
    EPERM every time, and nothing ends it."""

    pid = 424242

    def __init__(self, gone=None, blocked=False, drain_error=None):
        self.gone, self.blocked, self.drain_error = gone, blocked, drain_error
        self.exited = self.killed = self.drained = False
        self.reaped = gone == "reaped"
        self.kills = 0
        self.returncode = None
        self._transport = Transport()

    def killpg(self, pid, sig):
        assert (pid, sig) == (self.pid, signal.SIGKILL)
        self.kills += 1
        self.exited = self.exited or self.gone is not None
        if self.exited:
            if self.reaped:
                raise ProcessLookupError(errno.ESRCH, "No such process")
            raise PermissionError(errno.EPERM, "Operation not permitted")
        if self.blocked:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        self.killed = True

    async def communicate(self):
        while not (self.killed or self.exited):  # the pipes stay open while the group lives
            await asyncio.sleep(0.005)
        if self.drain_error is not None:
            raise self.drain_error
        self.drained, self.reaped, self.returncode = True, True, -9
        return b"", b""


def stub_child(monkeypatch, child):
    async def spawn(*argv, **kw):
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(os, "killpg", child.killpg)


@pytest.mark.parametrize(
    ("gone", "kills"),
    [(None, 1), ("reaped", 1), ("unreaped", 2)],
    ids=["running", "exited-on-its-own", "exited-not-yet-reaped"],
)
def test_sh_stops_and_joins_its_child_on_a_timeout_and_on_a_cancellation(monkeypatch, gone, kills):
    child = Child(gone)
    stub_child(monkeypatch, child)
    assert asyncio.run(instr.sh(["/bin/x"], timeout=0.01)) == (124, "", "/bin/x did not finish within 0.01s")
    # an EPERM is read once the drain has reaped the leader: the group is gone, and a second kill says so
    assert child.drained and child.kills == kills and child._transport.closed

    async def cancelled():
        task = asyncio.create_task(instr.sh(["/bin/x"]))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    child = Child(gone)
    stub_child(monkeypatch, child)
    asyncio.run(cancelled())
    assert child.drained and child.kills == kills and child._transport.closed


@pytest.mark.parametrize("by", ["a-timeout", "a-cancel"])
def test_sh_raises_when_a_live_group_cannot_be_signalled_and_still_closes_its_pipes(monkeypatch, by):
    # EPERM before the drain and after it: a member still runs that this process may not signal, which
    # is not the exited-unreaped leader. The caller is told, and a cancel in flight gives way to it
    monkeypatch.setattr(instr, "_GRACE", 0.05)
    child = Child(blocked=True)
    stub_child(monkeypatch, child)

    async def run():
        if by == "a-timeout":
            return await instr.sh(["/bin/x"], timeout=0.01)
        task = asyncio.create_task(instr.sh(["/bin/x"]))
        await asyncio.sleep(0.01)
        task.cancel()
        await task

    with pytest.raises(PermissionError):
        asyncio.run(run())
    assert child.kills == 2 and not child.drained and child._transport.closed


def test_sh_retries_a_refused_signal_when_a_cancel_ends_the_drain_and_then_raises_the_cancel(monkeypatch):
    # EPERM is an exited leader not yet reaped or a member this process may not signal, and the drain tells
    # which: a cancel that ends the drain first still gets the second signal, with the pipes still open
    child = Child(blocked=True)
    stub_child(monkeypatch, child)
    sent = []

    def killpg(pid, sig):
        sent.append("closed" if child._transport.closed else "open")
        if len(sent) == 1:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        raise ProcessLookupError(errno.ESRCH, "No such process")

    monkeypatch.setattr(os, "killpg", killpg)

    async def run():
        task = asyncio.create_task(instr.sh(["/bin/x"]))
        await asyncio.sleep(0.05)
        task.cancel()  # the first signal is refused, and the drain is waiting
        await asyncio.sleep(0.05)
        assert sent == ["open"] and not child._transport.closed
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert sent == ["open", "open"] and child._transport.closed


@pytest.mark.parametrize("fault", ["kill", "drain"])
def test_sh_raises_any_other_cleanup_failure_and_still_closes_its_pipes(monkeypatch, fault):
    child = Child(drain_error=ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"))
    stub_child(monkeypatch, child)
    if fault == "kill":  # not the documented answers: nothing is drained that could hang, the pipes close

        def killpg(pid, sig):
            raise OSError(errno.EINVAL, "Invalid argument")

        monkeypatch.setattr(os, "killpg", killpg)
    with pytest.raises(OSError) as raised:
        asyncio.run(instr.sh(["/bin/x"], timeout=0.01))
    assert raised.value.errno == (errno.EINVAL if fault == "kill" else errno.ECONNRESET)
    assert child._transport.closed


class Start:
    """The start of a command, replaced: it waits `delay` seconds, or on a future nobody sets when there is
    no `delay`, then fails with `fault` when it has one. A cancel is recorded in `seen`; a start with
    `child` has begun that process before the cancel reaches it, and hands it back instead of raising; one
    that `ignores` n cancels goes on waiting through them, and takes the next. `ended` says it is over. A
    start with `popen` has forked that process, which it keeps in the `extra` dict it is given, as asyncio
    does, before it waits."""

    def __init__(self, monkeypatch, delay=None, fault=None, child=None, ignores=0, popen=None):
        self.delay, self.fault, self.child, self.seen, self.gate = delay, fault, child, [], None
        self.ignores, self.ended, self.popen = ignores, False, popen
        monkeypatch.setattr(asyncio, "create_subprocess_exec", self.spawn)
        if child is not None:
            monkeypatch.setattr(os, "killpg", child.killpg)

    async def spawn(self, *argv, **kw):
        if self.popen is not None:
            kw.get("extra", {})["subprocess"] = self.popen
        try:
            if self.delay is None:
                self.gate = asyncio.get_running_loop().create_future()
                await self.gate
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.seen.append("cancelled")
            while len(self.seen) <= self.ignores:
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    self.seen.append("cancelled")
            if self.child is not None:
                return self.child
            raise
        else:
            if self.fault is not None:
                raise self.fault
        finally:
            self.ended = True

    def release(self):
        """Ends a start that never lands, so that a runner stuck waiting for it can return."""
        if self.gate is not None and not self.gate.done():
            self.gate.set_exception(RuntimeError("released"))


STUB_BOUND = 3.0  # seconds: a call over a stubbed start still running then is a regression that fails


def sh_over(start, *, timeout, cancel_after=None):
    """sh over a stubbed start: (what it gave or the exception it raised, "cancelled", or "hung" past
    STUB_BOUND; the seconds it took, counted from the cancel when there is one). A call that does not end
    is released and cancelled, so that it fails the test and does not hang it."""

    async def go():
        task = asyncio.create_task(instr.sh(["/bin/x"], timeout=timeout))
        t0 = time.monotonic()
        if cancel_after is not None:
            await asyncio.sleep(cancel_after)
            t0 = time.monotonic()
            task.cancel()
        done, _ = await asyncio.wait([task], timeout=STUB_BOUND)
        took = time.monotonic() - t0
        if task not in done:
            start.release()
            task.cancel()
            _, pending = await asyncio.wait([task], timeout=STUB_BOUND)
            assert not pending, "sh outlived its release and a cancel"
            return "hung", took
        if task.cancelled():
            return "cancelled", took
        return (task.exception() or task.result()), took

    return asyncio.run(go())


def test_sh_cancels_a_start_that_never_lands_at_its_grace_and_gives_the_timeouts_answer(monkeypatch):
    # the start's future is never set: it is waited for the grace past the timeout, so that a process it
    # began can land and be ended, and then cancelled
    monkeypatch.setattr(instr, "_GRACE", 0.2)
    start = Start(monkeypatch)
    sent = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: sent.append(pid))
    got, took = sh_over(start, timeout=0.05)
    assert got == (124, "", "/bin/x did not finish within 0.05s")
    assert start.seen == ["cancelled"] and 0.2 <= took < 0.05 + 0.2 + 0.2 + 1
    assert sent == []  # a start that began no process leaves no group to signal


@pytest.mark.parametrize("ignores", [0, 2], ids=["cancelled", "abandoned"])
@pytest.mark.parametrize("answer", ["kills", "finds-it-gone", "fails"])
def test_sh_kills_the_group_of_a_start_that_never_lands_by_the_pid_its_popen_holds(
    monkeypatch, answer, ignores
):
    # the start never returns, cancelled or given up on, but it has forked its process and keeps it in the
    # extra dict asyncio hands it: that process leads a group of its own, which is ended by its pid. Any
    # answer but ESRCH and EPERM is the caller's to be told, and is not tried again: this Popen has no `poll`
    monkeypatch.setattr(instr, "_GRACE", 0.1)
    start = Start(monkeypatch, ignores=ignores, popen=SimpleNamespace(pid=424242))
    sent = []

    def killpg(pid, sig):
        sent.append((pid, sig))
        if answer == "finds-it-gone":
            raise ProcessLookupError(errno.ESRCH, "No such process")
        if answer == "fails":
            raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(os, "killpg", killpg)
    got, _ = sh_over(start, timeout=0.05)
    assert sent == [(424242, signal.SIGKILL)]
    if answer == "fails":
        assert type(got) is OSError and got.errno == errno.EINVAL
    else:
        assert got == (124, "", "/bin/x did not finish within 0.05s")


class Leader:
    """The Popen of a start that never lands, as the OS treats the group it leads. `exited` leaves the leader
    the group's only member, exited and not yet reaped: macOS answers EPERM to the signal until `poll` reaps
    it, and ESRCH after. One that still runs is a live member this process may not signal: EPERM every time,
    whatever `poll` says. `events` is the order of the calls."""

    pid = 424242

    def __init__(self, exited):
        self.exited, self.reaped, self.events = exited, False, []

    def killpg(self, pid, sig):
        assert (pid, sig) == (self.pid, signal.SIGKILL)
        self.events.append("killpg")
        if self.reaped:
            raise ProcessLookupError(errno.ESRCH, "No such process")
        raise PermissionError(errno.EPERM, "Operation not permitted")

    def poll(self):
        self.events.append("poll")
        self.reaped = self.exited
        return -9 if self.exited else None


@pytest.mark.parametrize("by", ["a-timeout", "a-cancel"])
@pytest.mark.parametrize("leader", ["exited", "running"])
def test_a_refused_signal_to_a_start_that_never_lands_is_retried_once_its_leader_is_reaped(
    monkeypatch, leader, by
):
    # macOS answers EPERM to a group whose only member is its exited, unreaped leader: `poll` reaps it
    # between the two signals, and the second finds the group gone. A leader that still runs answers EPERM
    # again, which is the caller's to be told, and a cancel in flight gives way to it
    monkeypatch.setattr(instr, "_GRACE", 0.1)
    popen = Leader(exited=leader == "exited")
    start = Start(monkeypatch, popen=popen)
    monkeypatch.setattr(os, "killpg", popen.killpg)
    if by == "a-timeout":
        got, _ = sh_over(start, timeout=0.05)
        expected = (124, "", "/bin/x did not finish within 0.05s")
    else:
        got, _ = sh_over(start, timeout=0.05, cancel_after=0.1)  # inside the wait for the landing
        expected = "cancelled"
    if leader == "exited":
        assert got == expected
    else:
        assert isinstance(got, PermissionError)
    assert popen.events == ["killpg", "poll", "killpg"]


def test_a_cancel_in_the_wait_for_a_start_that_never_lands_still_ends_its_group(monkeypatch):
    monkeypatch.setattr(instr, "_GRACE", 0.1)
    start = Start(monkeypatch, popen=SimpleNamespace(pid=424242))
    sent = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    got, _ = sh_over(start, timeout=0.05, cancel_after=0.1)  # inside the wait for the landing
    assert got == "cancelled" and sent == [(424242, signal.SIGKILL)]


@pytest.mark.parametrize("ignores", [1, 2], ids=["takes-the-second-cancel", "takes-neither"])
def test_sh_returns_once_a_start_has_taken_its_second_cancel_and_gives_up_on_one_that_takes_neither(
    monkeypatch, ignores
):
    # on 3.11 and 3.12 a cancelled start that began its process waits for its pipes, and takes the second
    # cancel: the call returns with that start over, not one loop turn short of it. A start that takes
    # neither is given up on after a grace of its own, and the call is not held by it
    monkeypatch.setattr(instr, "_GRACE", 0.3)
    start = Start(monkeypatch, ignores=ignores)

    async def call():
        t0 = time.monotonic()
        got = await instr.sh(["/bin/x"], timeout=0.05)
        return got, time.monotonic() - t0, start.ended, list(start.seen)  # read in the step it returned

    async def go():
        task = asyncio.create_task(call())
        done, _ = await asyncio.wait([task], timeout=STUB_BOUND)
        if task not in done:  # a regression that fails this test and does not hang it
            start.ignores = 0
            start.release()
            task.cancel()
            await asyncio.wait([task], timeout=STUB_BOUND)
            pytest.fail("sh outlived its bound")
        return task.result()

    got, took, ended, seen = asyncio.run(go())
    assert got == (124, "", "/bin/x did not finish within 0.05s")
    assert seen == ["cancelled", "cancelled"]  # two cancels, and none from the runner after the call
    if ignores == 1:
        assert ended and 0.05 + 0.3 + 0.3 <= took < 0.05 + 0.3 + 0.3 + 0.2
    else:
        assert not ended and 0.05 + 0.3 + 0.3 + 0.3 <= took < 0.05 + 0.3 + 0.3 + 0.3 + 1


def test_a_cancel_in_the_wait_for_a_start_that_never_lands_is_raised_after_its_cancel(monkeypatch):
    monkeypatch.setattr(instr, "_GRACE", 0.2)
    start = Start(monkeypatch)
    got, took = sh_over(start, timeout=0.05, cancel_after=0.1)  # inside the wait for the landing
    assert got == "cancelled" and start.seen == ["cancelled"] and took < 0.2 + 0.2 + 1


def test_a_process_a_cancelled_start_had_begun_is_stopped_like_one_that_landed(monkeypatch):
    # the cancel reaches a start that has begun its process: the start hands it back, and it is ended
    monkeypatch.setattr(instr, "_GRACE", 0.2)
    child = Child()
    start = Start(monkeypatch, child=child)
    got, _ = sh_over(start, timeout=0.05)
    assert got == (124, "", "/bin/x did not finish within 0.05s")
    assert start.seen == ["cancelled"] and child.drained and child.kills == 1 and child._transport.closed


@pytest.mark.parametrize("delay", [0.0, 0.15], ids=["before-the-timeout", "after-the-timeout"])
def test_a_start_that_failed_is_raised_on_either_side_of_the_timeout(monkeypatch, delay):
    # a start that failed is a failure and not a timeout, whichever side of the deadline it failed on
    fault = FileNotFoundError(errno.ENOENT, "No such file or directory", "/bin/x")
    monkeypatch.setattr(instr, "_GRACE", 1)
    got, _ = sh_over(Start(monkeypatch, delay=delay, fault=fault), timeout=0.05)
    assert got is fault


# a command writing without end: past its timeout its pipe stays full until something reads it
FLOOD = "import os, sys\nb = b'y' * 65536\nwhile True:\n    os.write(int(sys.argv[1]), b)"
# a command that starts a sleeping descendant on its own stdout and stderr and writes the
# descendant's pid, then sleeps on or exits; a descendant `apart` leads a session of its own
FAMILY = (
    "import subprocess, sys, time\n"
    "p = subprocess.Popen(['sleep', '30'], start_new_session=sys.argv[3] == 'apart')\n"
    "open(sys.argv[1], 'w').write(str(p.pid))\n"
    "if sys.argv[2] == 'running':\n"
    "    time.sleep(30)\n"
)
BOUND = 10.0  # seconds: a call still running then is a regression that fails, never one that hangs


def alive(pid: int, settle: float = 0.0) -> bool:
    """Whether the pid still runs once `settle` seconds have let a killed one be reaped."""
    end = time.monotonic() + settle
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        if time.monotonic() >= end:
            return True
        time.sleep(0.02)


async def groups_left(pgids: list[int], settle: float) -> list[int]:
    """The process groups among `pgids` with a member left once `settle` seconds have let the killed ones be
    reaped: an exited member not yet reaped answers EPERM on macOS, and is still a member."""
    end = time.monotonic() + settle
    while True:
        left = []
        for pgid in pgids:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            left.append(pgid)
        if not left or time.monotonic() >= end:
            return left
        await asyncio.sleep(0.02)


def open_fds() -> set[int]:
    """The descriptors this process has open (the listing's own is closed by the time it is read)."""
    fds = set()
    for name in os.listdir("/dev/fd"):
        with contextlib.suppress(OSError):
            os.fstat(int(name))
            fds.add(int(name))
    return fds


def other_tasks(*skip: asyncio.Task) -> list[str]:
    """The coroutines of every task still pending beside the running one and `skip`."""
    running = asyncio.current_task()
    return sorted(
        t.get_coro().__qualname__ for t in asyncio.all_tasks() if t is not running and t not in skip
    )


def delay_connect(monkeypatch, seconds):
    """Hold open the start of every command: asyncio connects a new process's pipes in loop turns after
    it has forked, and a loaded machine stretches them."""
    connect = base_subprocess.BaseSubprocessTransport._connect_pipes

    async def late(self, waiter):
        await asyncio.sleep(seconds)
        await connect(self, waiter)

    monkeypatch.setattr(base_subprocess.BaseSubprocessTransport, "_connect_pipes", late)


def real_sh(
    monkeypatch,
    argv,
    *,
    timeout,
    cancels=0,
    pause=0.0,
    pidfile=None,
    settle=5.0,
    hold=False,
    land=False,
    census=None,
):
    """sh on a real command under the test's own clock: (what it gave, "cancelled", or "hung" past
    BOUND; the seconds it took, counted from the first cancel, which comes `pause` seconds in, when
    there is one; whether the descendant in `pidfile` still runs; whether the pipes of every command
    it started are closing; whether each was reaped). The last two are read the moment the call
    ended, before this helper cleans up after it: the descendant is then killed and each command is
    closed and killed. A command is recorded where its process comes to exist, before anything is
    awaited, so a start cut short is counted too, and a call that started none fails. With `hold` the start of
    every command never connects its pipes, until the helper releases it: right after the census below, or
    when the call is hung, or with `land` once the cancels are in. A `census` dict is filled in the step in
    which the call returns, before its caller's next turn: `tasks`, the coroutines still pending beside the
    call; `fds`, the descriptors open that were not before it; `ends`, the parent's pipe ends of the commands
    started. It then has `group`, the commands' process groups with a member left after `settle` seconds. With
    `hold` it is finally filled with `released`, the fds and tasks once the held connection has run, which
    nothing in the helper helps to close."""
    started = []
    before: set[int] = set()
    runner: list[asyncio.Task] = []  # this helper's own task, which waits beside the call
    create = base_subprocess.BaseSubprocessTransport.__init__
    gate = asyncio.Event()

    def record(self, *args, **kwargs):
        create(self, *args, **kwargs)
        started.append(self)

    monkeypatch.setattr(base_subprocess.BaseSubprocessTransport, "__init__", record)
    if hold:
        connect = base_subprocess.BaseSubprocessTransport._connect_pipes

        async def held(self, waiter):
            await gate.wait()
            await connect(self, waiter)

        monkeypatch.setattr(base_subprocess.BaseSubprocessTransport, "_connect_pipes", held)

    async def written() -> int:
        while not (pidfile.exists() and pidfile.read_text()):
            await asyncio.sleep(0.01)
        return int(pidfile.read_text())

    async def call():
        try:
            return await instr.sh(argv, timeout=timeout)
        finally:
            if census is not None:
                census["tasks"] = other_tasks(*runner)
                census["fds"] = open_fds() - before
                procs = [t._proc for t in started if t._proc is not None]
                ends = [end for proc in procs for end in (proc.stdout, proc.stderr)]
                census["ends"] = {end.fileno() for end in ends if not end.closed}

    async def go():
        nonlocal before
        t0 = time.monotonic()
        before = open_fds()
        runner.append(asyncio.current_task())
        task = asyncio.create_task(call())
        # the descendant runs before the call is timed out or cancelled
        pid = await asyncio.wait_for(written(), BOUND) if pidfile is not None else None
        if cancels:
            await asyncio.sleep(pause)
            t0 = time.monotonic()
        for _ in range(cancels):  # a second cancel lands while the first one's cleanup runs
            task.cancel()
            await asyncio.sleep(0.05)
        if land:
            gate.set()
        done, _ = await asyncio.wait([task], timeout=BOUND)
        took = time.monotonic() - t0
        pipes = [t._pipes.get(fd) for t in started for fd in (1, 2)]  # None: never connected
        closed = all(pipe is not None and pipe.pipe.is_closing() for pipe in pipes)
        reaped = all(t.get_returncode() is not None for t in started)
        if task in done:
            got = "cancelled" if task.cancelled() else task.result()
            if census is not None:
                census["group"] = await groups_left([t.get_pid() for t in started], settle)
            if census is not None and hold:
                gate.set()
                stop = time.monotonic() + BOUND
                while (open_fds() - before or other_tasks(task)) and time.monotonic() < stop:
                    await asyncio.sleep(0.01)
                census["released"] = open_fds() - before, other_tasks(task)
        else:
            got = "hung"
            gate.set()
            task.cancel()
            _, pending = await asyncio.wait([task], timeout=BOUND)
            assert not pending, "sh outlived its release and a cancel"
        assert started, "no command was started"
        left = None
        if pid is not None:
            left = alive(pid, settle)
            if left:
                os.kill(pid, signal.SIGKILL)
        for t in started:
            with contextlib.suppress(ProcessLookupError):
                t.kill()
            t.close()
        stop = time.monotonic() + 5
        while any(t.get_returncode() is None for t in started):
            assert time.monotonic() < stop, "a killed command was not reaped"
            await asyncio.sleep(0.01)
        return got, took, left, closed, reaped

    return asyncio.run(go())


@pytest.mark.parametrize("fd", ["1", "2"], ids=["stdout", "stderr"])
def test_sh_returns_at_its_timeout_when_the_command_filled_its_pipe(monkeypatch, fd):
    # the kill alone frees nothing: the full pipe is paused, and on 3.11 and 3.12 a wait returns only
    # once every pipe has closed
    got, took, _, closed, reaped = real_sh(monkeypatch, [sys.executable, "-c", FLOOD, fd], timeout=0.05)
    assert got == (124, "", f"{sys.executable} did not finish within 0.05s")
    assert took < 0.05 + 3 and closed and reaped


@pytest.mark.parametrize("leader", ["running", "exited"])
def test_sh_ends_the_descendant_holding_its_pipe_at_the_timeout(monkeypatch, tmp_path, leader):
    pidfile = tmp_path / "descendant.pid"
    argv = [sys.executable, "-c", FAMILY, str(pidfile), leader, "in-group"]
    got, took, left, closed, reaped = real_sh(monkeypatch, argv, timeout=1.0, pidfile=pidfile)
    assert got == (124, "", f"{sys.executable} did not finish within 1.0s")
    assert took < 1.0 + 3  # the group is killed, so the pipe closes without waiting out the grace
    assert left is False and closed and reaped


def test_a_cancelled_sh_ends_the_descendant_holding_its_pipe_and_stays_cancelled(monkeypatch, tmp_path):
    pidfile = tmp_path / "descendant.pid"
    argv = [sys.executable, "-c", FAMILY, str(pidfile), "running", "in-group"]
    got, took, left, closed, reaped = real_sh(monkeypatch, argv, timeout=30, cancels=1, pidfile=pidfile)
    assert got == "cancelled" and took < 3
    assert left is False and closed and reaped


def test_sh_counts_the_start_of_its_command_inside_its_timeout(monkeypatch):
    # the start is held 1.2 s and the timeout runs from the call: the command is ended when it has
    # started, not a whole timeout after
    delay_connect(monkeypatch, 1.2)
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    got, took, _, closed, reaped = real_sh(monkeypatch, argv, timeout=1.0)
    assert got == (124, "", f"{sys.executable} did not finish within 1.0s")
    assert took < 1.2 + 0.5 and closed and reaped


def test_a_cancel_after_the_timeout_while_the_start_connects_is_not_the_timeouts_answer(
    monkeypatch, tmp_path
):
    # the deadline passed while the start was held: the command and its group are ended once it has
    # started, and the cancel that came in meanwhile is raised then. The start is held until the cancel is
    # in, and the grace outlasts the test's bound, so no clock decides the order, however slow the machine
    monkeypatch.setattr(instr, "_GRACE", BOUND)
    pidfile = tmp_path / "descendant.pid"
    argv = [sys.executable, "-c", FAMILY, str(pidfile), "running", "in-group"]
    got, took, left, closed, reaped = real_sh(
        monkeypatch, argv, timeout=0.2, cancels=1, pause=0.3, pidfile=pidfile, hold=True, land=True
    )
    assert got == "cancelled" and took < 3
    assert left is False and closed and reaped


def test_sh_gives_up_at_its_grace_on_a_command_whose_pipes_never_connect_and_ends_it(monkeypatch):
    # the start is stuck connecting for good with its process begun: past the timeout and the grace the
    # start is cancelled, which ends the process, and the call returns the timeout's answer
    monkeypatch.setattr(instr, "_GRACE", 0.5)
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    census = {}
    got, took, _, _, reaped = real_sh(monkeypatch, argv, timeout=0.2, hold=True, census=census)
    assert got == (124, "", f"{sys.executable} did not finish within 0.2s")
    assert reaped and 0.2 + 0.5 <= took < 0.2 + 0.5 + 0.5 + 2
    # the start has taken its cancel on every interpreter: what is left pending is asyncio's own connection
    # of the pipes, held by this test, and its two parent ends are the only descriptors open
    assert census["tasks"] == ["real_sh.<locals>.held"]
    assert len(census["ends"]) == 2 and census["fds"] == census["ends"]
    # and when the connection runs it closes them, the leader already killed: nothing helps it
    assert census["released"] == (set(), [])


def test_sh_ends_the_group_of_a_command_whose_pipes_never_connect(monkeypatch, tmp_path):
    # the start is stuck connecting for good with its process begun, and a descendant of it runs in its
    # group: the start is cancelled and the group is ended by the pid asyncio forked it under, not left
    monkeypatch.setattr(instr, "_GRACE", 0.3)
    pidfile = tmp_path / "descendant.pid"
    argv = [sys.executable, "-c", FAMILY, str(pidfile), "running", "in-group"]
    census = {}
    got, _, left, _, _ = real_sh(monkeypatch, argv, timeout=1.0, pidfile=pidfile, hold=True, census=census)
    assert got == (124, "", f"{sys.executable} did not finish within 1.0s")
    assert left is False and census["group"] == []
    # the descendant held the pipe: with it gone the held connection closes the ends when it runs
    assert census["released"] == (set(), [])


def test_a_cancel_that_lands_over_a_start_that_failed_is_still_the_cancel():
    # no such program: the start raises in the turn the cancel is delivered; the caller gets the cancel
    async def run():
        task = asyncio.create_task(instr.sh(["/no/such/program"]))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


@pytest.mark.parametrize("cancels", [1, 2], ids=["a-cancel", "a-second-cancel"])
def test_a_cancel_while_the_start_connects_its_pipes_leaves_no_child_running(monkeypatch, tmp_path, cancels):
    # asyncio ends the leader alone when a cancel lands in that window; the group it leads kept running,
    # and on 3.11 and 3.12 the cancel waited out the pipes it held
    delay_connect(monkeypatch, 0.4)
    pidfile = tmp_path / "descendant.pid"
    argv = [sys.executable, "-c", FAMILY, str(pidfile), "running", "in-group"]
    got, took, left, closed, reaped = real_sh(monkeypatch, argv, timeout=30, cancels=cancels, pidfile=pidfile)
    assert got == "cancelled" and took < 3
    assert left is False and closed and reaped


@pytest.mark.parametrize("cancels", [0, 1, 2], ids=["a-timeout", "a-cancel", "a-second-cancel"])
def test_sh_returns_within_its_grace_when_a_descendant_that_left_the_group_holds_the_pipe(
    monkeypatch, tmp_path, cancels
):
    # the bound on the caller holds; ending every descendant does not: one in a session of its own is
    # out of the group's reach, and is left running
    monkeypatch.setattr(instr, "_GRACE", 1, raising=False)
    pidfile = tmp_path / "descendant.pid"
    argv = [sys.executable, "-c", FAMILY, str(pidfile), "running", "apart"]
    timeout = 30 if cancels else 1.0
    got, took, left, closed, reaped = real_sh(
        monkeypatch, argv, timeout=timeout, cancels=cancels, pidfile=pidfile, settle=0
    )
    if cancels:
        assert got == "cancelled" and took < 1 + 3
    else:
        assert got == (124, "", f"{sys.executable} did not finish within 1.0s")
        assert took < 1.0 + 1 + 3
    # the parent's ends of the pipes the descendant holds are closed when the call ends, not whenever it
    # lets go of them: read before the helper's own cleanup, so a runner that leaves them open fails here
    assert left is True and closed
    assert reaped or cancels == 2  # a second cancel ends the drain, and the leader's exit is read later


class ExecKhive:
    """A khive whose `exec` answers per verb, for the instruments that read the store."""

    actor, cwd = "lambda:x:agent", None

    def __init__(self, **by_verb):
        self.by_verb, self.ops = by_verb, []

    async def exec(self, ops: str):
        self.ops.append(ops)
        if ops.startswith("["):  # a batch: one answer per op, by verb
            parts = [p.strip() for p in ops[1:-1].split("), ")]
            return [self.by_verb[p.split("(")[0]].pop(0) for p in parts]
        return [self.by_verb[ops.split("(")[0]].pop(0)]


def test_gtd_inbox_names_its_population_and_fails_its_control_on_an_unshared_namespace():
    old = {"id": "t1", "title": "triage me", "priority": "p1", "created_at": iso(30)}
    new = {"id": "t2", "title": "fresh", "priority": "p2", "created_at": iso(2)}
    k = ExecKhive(**{"gtd.tasks": [[old, new], [old]]})
    inst = GtdInbox(k, "lambda:x", every="every:6h", max_age_h=24)
    out = asyncio.run(inst.run({}))
    assert out.control and out.findings == ("1 gtd inbox row(s) older than 24h of 2 in inbox",)
    assert out.population == "gtd tasks assigned to lambda:x with status inbox" and out.count == 2
    assert out.predicate == "created_at older than 24h (1 match)" and "t1 p1 triage me (30h)" in out.evidence
    assert k.ops[0] == (  # the control rides in the same batch: does the namespace hold any task at all
        '[gtd.tasks(status="inbox", assignee="lambda:x", limit=100), gtd.tasks(assignee="lambda:x", limit=1)]'
    )
    k = ExecKhive(**{"gtd.tasks": [[], []]})  # an unshared namespace answers empty, not an error
    out = asyncio.run(GtdInbox(k, "lambda:x", every=None).run({}))
    assert not out.control and out.findings == () and "has NO task at all" in out.evidence


@pytest.mark.parametrize("answer", [None, {"tasks": []}, "error"])
def test_gtd_inbox_an_inbox_read_that_is_not_a_list_is_unread_not_empty(answer):
    known = {"id": "t1", "title": "any", "priority": "p1", "created_at": iso(1)}
    k = ExecKhive(**{"gtd.tasks": [answer, [known]]})
    out = asyncio.run(GtdInbox(k, "lambda:x", every=None).run({}))
    assert not out.control and out.findings == () and out.positive == ""
    assert f"the inbox read answered {type(answer).__name__}, not a list of tasks" in out.evidence
    assert "lambda:x has a task at all" in out.evidence  # the known-positive held; the answer did not


def test_gtd_inbox_a_full_page_is_a_floor_and_fails_the_control():
    known = {"id": "t1", "title": "any", "priority": "p1", "created_at": iso(1)}
    fresh = [{"id": f"t{i}", "title": "new", "priority": "p2", "created_at": iso(1)} for i in range(99)]
    k = ExecKhive(**{"gtd.tasks": [fresh, [known]]})
    out = asyncio.run(GtdInbox(k, "lambda:x", every=None).run({}))
    assert out.control and out.count == 99 and "limit=100)" in k.ops[0]
    k = ExecKhive(**{"gtd.tasks": [[*fresh, known], [known]]})
    out = asyncio.run(GtdInbox(k, "lambda:x", every=None).run({}))
    assert not out.control and out.count == 100 and out.positive == ""
    assert "the inbox read returned a full page of 100; rows past it are unread" in out.evidence


def test_inbox_sla_probes_each_actor_and_a_dead_read_fails_the_control():
    page = [{"id": "m1", "from_actor": "lambda:owner", "created_at_us": 1789959858375317}]
    k = ExecKhive(
        **{
            "comm.probe": [
                {"new_messages": page, "stale_unread_count": 3},
                {"new_messages": [], "stale_unread_count": 0},
            ]
        }
    )
    inst = InboxSla(k, ["lambda:x", "lambda:x:echo"], every="every:15m", stale_minutes=20)
    out = asyncio.run(inst.run({}))
    assert out.findings == ("lambda:x: 3 unread message(s) older than 20 min",)
    assert not out.control and "lambda:x:echo: empty page and zero count" in out.evidence
    assert out.population == "messages on the probe pages of ['lambda:x', 'lambda:x:echo']"
    assert out.predicate == "unread and older than 20 minutes (3 match)"
    assert k.ops == [
        '[comm.probe(actor="lambda:x", stale_minutes=20), '
        'comm.probe(actor="lambda:x:echo", stale_minutes=20)]'
    ]
    k = ExecKhive(**{"comm.probe": [{"new_messages": page, "stale_unread_count": 0}]})
    out = asyncio.run(InboxSla(k, ["lambda:x"], every=None).run({}))
    assert out.control and out.findings == ()


def test_worktrees_first_sight_then_a_parked_dirty_tree_without_a_pr(tmp_path):
    porcelain = (
        f"worktree {tmp_path}/main\0HEAD abc\0branch refs/heads/main\0\0"
        f"worktree {tmp_path}/wt\0HEAD def\0branch refs/heads/feat\0\0"
    )
    sh = FakeSh(
        **{
            "worktree list --porcelain": porcelain,
            "remote get-url origin": "https://github.com/o/r.git\n",
            f"-C {tmp_path}/wt status --porcelain": " M a.py\n?? b\n",
            f"-C {tmp_path}/wt log -1": f"{int(time.time()) - 5 * 86400}\n",
            "pr list -R o/r --head feat": "[]",
        }
    )
    inst = Worktrees(
        [str(tmp_path / "main")], fresh_store().notes("agent"), every="every:6h", parked_days=3, run=sh
    )
    out = asyncio.run(inst.run({}))
    assert out.control and out.evidence.startswith(
        f"control: {tmp_path}/main lists 2 tree(s) incl. the main checkout; origin o/r\n"
    )
    assert out.findings == (
        f"{tmp_path}/main: first snapshot, 1 worktree(s)",
        f"{tmp_path}/wt (feat): parked: dirty, HEAD older than 3 days, no open PR",
    )
    assert f"{tmp_path}/wt | feat | dirty 2 | HEAD 5.0d | PR none | PARKED" in out.evidence and out.count == 1
    assert asyncio.run(inst.run({})).findings == ()  # still parked: the crossing was told once
    sh.set("pr list -R o/r --head feat", json.dumps([{"number": 9}]))
    out = asyncio.run(inst.run({}))
    assert out.findings == (f"{tmp_path}/wt: feat/none -> feat/#9",)
    sh.set("pr list -R o/r --head feat", "[]")
    out = asyncio.run(inst.run({}))  # the PR closed: parked again, and that crossing is told again
    assert out.findings == (
        f"{tmp_path}/wt: feat/#9 -> feat/none",
        f"{tmp_path}/wt (feat): parked: dirty, HEAD older than 3 days, no open PR",
    )
    out = asyncio.run(
        Worktrees([str(tmp_path / "main")], fresh_store().notes("agent"), every=None, run=FakeSh()).run({})
    )
    assert not out.control and "git worktree list failed" in out.findings[0]


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/o/project.v2.git\n",
        "https://github.com/o/project.v2\n",
        "git@github.com:o/project.v2.git\n",
        "ssh://git@github.com/o/project.v2/\n",
    ],
)
def test_worktrees_an_origin_keeps_every_dot_of_its_repository(tmp_path, origin):
    porcelain = (
        f"worktree {tmp_path}/main\0HEAD abc\0branch refs/heads/main\0\0"
        f"worktree {tmp_path}/wt\0HEAD d\0branch refs/heads/feat\0\0"
    )
    sh = FakeSh(
        **{
            "worktree list --porcelain": porcelain,
            "remote get-url origin": origin,
            "status --porcelain": "",
            "log -1": f"{int(time.time())}\n",
            "pr list -R o/project.v2 --head feat": "[]",
        }
    )
    out = asyncio.run(
        Worktrees([str(tmp_path / "main")], fresh_store().notes("agent"), every=None, run=sh).run({})
    )
    assert out.control and "origin o/project.v2\n" in out.evidence
    assert ["pr", "list", "-R", "o/project.v2"] == next(c for c in sh.calls if "pr" in c)[1:5]


@pytest.mark.parametrize("failing", ["status", "log", "pr"])
def test_worktrees_a_failed_read_of_a_tree_fails_the_control_and_keeps_its_last_reading(tmp_path, failing):
    porcelain = (
        f"worktree {tmp_path}/main\0HEAD abc\0branch refs/heads/main\0\0"
        f"worktree {tmp_path}/wt\0HEAD d\0branch refs/heads/feat\0\0"
    )
    good = {
        "worktree list --porcelain": porcelain,
        "remote get-url origin": "https://github.com/o/r.git\n",
        f"-C {tmp_path}/wt status --porcelain": " M a.py\n",
        f"-C {tmp_path}/wt log -1": f"{int(time.time()) - 5 * 86400}\n",
        "pr list -R o/r --head feat": "[]",
    }
    key = {"status": "status --porcelain", "log": "log -1", "pr": "pr list -R o/r --head feat"}[failing]
    store = fresh_store().notes("agent")
    inst = Worktrees([str(tmp_path / "main")], store, every=None, parked_days=3, run=FakeSh(**good))
    assert asyncio.run(inst.run({})).control
    kept = store.get(f"worktrees:{tmp_path}/main").value
    broken = FakeSh(**good)
    broken.set(next(k for k in good if key in k), (1, "", "read failed"))
    inst.sh = broken
    out = asyncio.run(inst.run({}))
    word = {"status": "status", "log": "log", "pr": "PR"}[failing]
    assert not out.control and out.findings == (f"{tmp_path}/wt: the {word} read failed; the tree is unread",)
    assert store.get(f"worktrees:{tmp_path}/main").value == kept  # no transition from an unread tree
    fresh = Worktrees([str(tmp_path / "main")], fresh_store().notes("agent"), every=None, run=broken)
    out = asyncio.run(fresh.run({}))  # first sight of an unread tree: nothing stored for it
    assert not out.control and out.count == 0
    assert f"{tmp_path}/wt: the {word} read failed; the tree is unread" in out.findings


@pytest.mark.parametrize(
    ("origin", "why"),
    [
        ((1, "", "No such remote 'origin'"), "failed: No such remote 'origin'"),
        (
            (128, "https://github.com/o/r.git\n", "fatal: bad config line 3"),
            "failed: fatal: bad config line 3",
        ),
        ("https://git.example.com/o/r.git\n", "names no GitHub repo: https://git.example.com/o/r.git"),
        ("https://evilgithub.com/o/r.git\n", "names no GitHub repo: https://evilgithub.com/o/r.git"),
        (
            "https://evil.example/path/github.com/o/r.git\n",
            "names no GitHub repo: https://evil.example/path/github.com/o/r.git",
        ),
    ],
)
def test_worktrees_an_unread_origin_leaves_every_branch_pr_unread(tmp_path, origin, why):
    porcelain = (
        f"worktree {tmp_path}/main\0HEAD abc\0branch refs/heads/main\0\0"
        f"worktree {tmp_path}/wt\0HEAD d\0branch refs/heads/feat\0\0"
    )
    good = {
        "worktree list --porcelain": porcelain,
        "remote get-url origin": "https://github.com/o/r.git\n",
        "status --porcelain": " M a.py\n",
        "log -1": f"{int(time.time()) - 5 * 86400}\n",
        "pr list -R o/r --head feat": "[]",
    }
    store = fresh_store().notes("agent")
    inst = Worktrees([str(tmp_path / "main")], store, every=None, parked_days=3, run=FakeSh(**good))
    assert asyncio.run(inst.run({})).control
    kept = store.get(f"worktrees:{tmp_path}/main").value
    inst.sh = FakeSh(**good)
    inst.sh.set("remote get-url origin", origin)
    out = asyncio.run(inst.run({}))
    assert not out.control and out.findings == (
        f"{tmp_path}/main: origin {why}",
        f"{tmp_path}/wt: the PR read failed; the tree is unread",
    )
    assert not [c for c in inst.sh.calls if "pr" in c]  # no repository, no PR query
    assert store.get(f"worktrees:{tmp_path}/main").value == kept


@pytest.mark.parametrize(
    "answer",
    [
        "null",
        "{}",
        "[1]",
        '[{"id": 1}]',
        '[{"number": null}]',
        '[{"number": true}]',
        '[{"number": 0}]',
        '[{"number": -3}]',
        '[{"number": 7}, {"number": 0}]',
        "not json",
    ],
)
def test_worktrees_a_pr_answer_that_is_not_a_pr_list_is_unread(tmp_path, answer):
    porcelain = (
        f"worktree {tmp_path}/main\0HEAD abc\0branch refs/heads/main\0\0"
        f"worktree {tmp_path}/wt\0HEAD d\0branch refs/heads/feat\0\0"
    )
    sh = FakeSh(
        **{
            "worktree list --porcelain": porcelain,
            "remote get-url origin": "https://github.com/o/r.git\n",
            "status --porcelain": "",
            "log -1": f"{int(time.time())}\n",
            "pr list -R o/r --head feat": answer,
        }
    )
    out = asyncio.run(
        Worktrees([str(tmp_path / "main")], fresh_store().notes("agent"), every=None, run=sh).run({})
    )
    assert not out.control and f"{tmp_path}/wt: the PR read failed; the tree is unread" in out.findings
    assert f"{tmp_path}/wt | feat | dirty 0 | HEAD 0.0d | PR not a PR list" in out.evidence


def test_worktrees_a_head_committed_ahead_of_the_clock_is_a_read(tmp_path):
    porcelain = (
        f"worktree {tmp_path}/main\0HEAD abc\0branch refs/heads/main\0\0"
        f"worktree {tmp_path}/wt\0HEAD d\0branch refs/heads/feat\0\0"
    )
    sh = FakeSh(
        **{
            "worktree list --porcelain": porcelain,
            "remote get-url origin": "https://github.com/o/r.git\n",
            "status --porcelain": "",
            "log -1": f"{int(time.time()) + 3600}\n",
            "pr list -R o/r --head feat": "[]",
        }
    )
    out = asyncio.run(
        Worktrees([str(tmp_path / "main")], fresh_store().notes("agent"), every=None, run=sh).run({})
    )
    assert out.control and out.findings == (f"{tmp_path}/main: first snapshot, 1 worktree(s)",)


def test_worktrees_strips_only_the_leading_refs_heads_from_a_branch(tmp_path):
    porcelain = (
        f"worktree {tmp_path}/main\0HEAD abc\0branch refs/heads/main\0\0"
        f"worktree {tmp_path}/nested\0HEAD d\0branch refs/heads/topic/refs/heads/fix\0\0"
        f"worktree {tmp_path}/plain\0HEAD e\0branch refs/heads/feat\0\0"
    )
    sh = FakeSh(
        **{
            "worktree list --porcelain -z": porcelain,
            "remote get-url origin": "https://github.com/o/r.git\n",
            "status --porcelain": "",
            "log -1": f"{int(time.time())}\n",
            "pr list -R o/r --head topic/refs/heads/fix": json.dumps([{"number": 4}]),
            "pr list -R o/r --head feat": "[]",
        }
    )
    store = fresh_store().notes("agent")
    out = asyncio.run(Worktrees([str(tmp_path / "main")], store, every=None, run=sh).run({}))
    assert out.control
    snap = store.get(f"worktrees:{tmp_path}/main").value
    assert snap[f"{tmp_path}/nested"]["branch"] == "topic/refs/heads/fix"
    assert snap[f"{tmp_path}/nested"]["pr"] == "#4" and snap[f"{tmp_path}/plain"]["branch"] == "feat"
    heads = [c[c.index("--head") + 1] for c in sh.calls if "--head" in c]
    assert heads == ["topic/refs/heads/fix", "feat"]


def test_worktrees_a_path_holding_a_line_break_is_read_whole(tmp_path):
    paths = [f"{tmp_path}/new\nline", f"{tmp_path}/sep ar", f"{tmp_path}/plain", f"{tmp_path}/with space"]
    porcelain = f"worktree {tmp_path}/main\0HEAD abc\0branch refs/heads/main\0\0" + "".join(
        f"worktree {p}\0HEAD d\0branch refs/heads/feat\0\0" for p in paths
    )
    sh = FakeSh(
        **{
            "worktree list --porcelain -z": porcelain,
            "remote get-url origin": "https://github.com/o/r.git\n",
            "status --porcelain": "",
            "log -1": f"{int(time.time())}\n",
            "pr list -R o/r --head feat": "[]",
        }
    )
    store = fresh_store().notes("agent")
    out = asyncio.run(Worktrees([str(tmp_path / "main")], store, every=None, run=sh).run({}))
    assert out.control and out.count == 4
    assert sorted(store.get(f"worktrees:{tmp_path}/main").value) == sorted(paths)
    assert sorted(c[2] for c in sh.calls if "status" in c) == sorted(paths)
    assert sh.calls[0][1:] == ["-C", f"{tmp_path}/main", "worktree", "list", "--porcelain", "-z"]


def test_worktrees_a_pr_number_past_the_integer_conversion_limit_is_unread(tmp_path):
    porcelain = (
        f"worktree {tmp_path}/main\0HEAD abc\0branch refs/heads/main\0\0"
        f"worktree {tmp_path}/wt\0HEAD d\0branch refs/heads/feat\0\0"
    )
    sh = FakeSh(
        **{
            "worktree list --porcelain": porcelain,
            "remote get-url origin": "https://github.com/o/r.git\n",
            "status --porcelain": "",
            "log -1": f"{int(time.time())}\n",
            "pr list -R o/r --head feat": "[]",
        }
    )
    store = fresh_store().notes("agent")
    inst = Worktrees([str(tmp_path / "main")], store, every=None, run=sh)
    assert asyncio.run(inst.run({})).control
    kept = store.get(f"worktrees:{tmp_path}/main").value
    sh.set("pr list -R o/r --head feat", '[{"number": ' + "1" * 4301 + "}]")
    out = asyncio.run(inst.run({}))
    assert not out.control and out.findings == (f"{tmp_path}/wt: the PR read failed; the tree is unread",)
    assert f"{tmp_path}/wt | feat | dirty 0 | HEAD 0.0d | PR not a PR list" in out.evidence
    assert store.get(f"worktrees:{tmp_path}/main").value == kept


def _named_and_loose(tmp_path, named: str = "named", loose: str = "loose") -> str:
    """The main checkout, a tree on a branch named `detached`, and a tree whose HEAD is detached."""
    return (
        f"worktree {tmp_path}/main\0HEAD abc\0branch refs/heads/main\0\0"
        f"worktree {tmp_path}/{named}\0HEAD d\0branch refs/heads/detached\0\0"
        f"worktree {tmp_path}/{loose}\0HEAD e\0detached\0\0"
    )


def _old_dirty_answers(tmp_path) -> dict:
    return {
        "worktree list --porcelain -z": _named_and_loose(tmp_path),
        "remote get-url origin": "https://github.com/o/r.git\n",
        "status --porcelain": " M a.py\n",
        "log -1": f"{int(time.time()) - 10 * 86400}\n",
        "pr list -R o/r --head detached": "[]",
    }


def test_worktrees_a_branch_named_detached_is_queried_and_parks_beside_a_detached_head(tmp_path):
    sh = FakeSh(**_old_dirty_answers(tmp_path))
    store = fresh_store().notes("agent")
    out = asyncio.run(Worktrees([str(tmp_path / "main")], store, every=None, parked_days=3, run=sh).run({}))
    assert out.control and out.count == 2
    assert [c[1:] for c in sh.calls if "pr" in c] == [
        ["pr", "list", "-R", "o/r", "--head", "detached", "--state", "open", "--json", "number"]
    ]  # one query, for the branch; the detached HEAD has no branch to ask about
    assert out.findings == (
        f"{tmp_path}/main: first snapshot, 2 worktree(s)",
        f"{tmp_path}/named (detached): parked: dirty, HEAD older than 3 days, no open PR",
    )
    assert out.predicate == "dirty, HEAD older than 3 days, no open PR (1 match)"
    lines = out.evidence.splitlines()
    assert f"{tmp_path}/named | detached | dirty 1 | HEAD 10.0d | PR none | PARKED" in lines
    assert f"{tmp_path}/loose | (detached HEAD) | dirty 1 | HEAD 10.0d | PR ?" in lines
    assert store.get(f"worktrees:{tmp_path}/main").value == {
        f"{tmp_path}/named": {"branch": "detached", "dirty": 1, "pr": "none", "parked": True},
        f"{tmp_path}/loose": {"branch": None, "dirty": 1, "pr": "?", "parked": False},
    }


def test_worktrees_a_row_stored_under_the_old_detached_label_is_compared_on_its_pr_alone(tmp_path):
    # rows stored before a detached HEAD was kept as no branch hold `detached` with PR `?` for both a
    # detached HEAD and a branch of that name: such a row is guessed into neither
    sh = FakeSh(**_old_dirty_answers(tmp_path))
    store = fresh_store().notes("agent")
    old = {"branch": "detached", "dirty": 1, "pr": "?", "parked": False}
    rows = {f"{tmp_path}/named": old, f"{tmp_path}/loose": old}
    store.put(f"worktrees:{tmp_path}/main", rows, run="-", seq=0)
    inst = Worktrees([str(tmp_path / "main")], store, every=None, parked_days=3, run=sh)
    out = asyncio.run(inst.run({}))
    assert out.control and out.findings == (
        f"{tmp_path}/named: detached or (detached HEAD)/? -> detached/none",
        f"{tmp_path}/named (detached): parked: dirty, HEAD older than 3 days, no open PR",
    )
    sh.set("worktree list --porcelain -z", _named_and_loose(tmp_path, named="loose", loose="named"))
    out = asyncio.run(inst.run({}))  # the two trees swap states, and a row stored now tells which
    assert out.control and out.findings == (
        f"{tmp_path}/loose: (detached HEAD)/? -> detached/none",
        f"{tmp_path}/loose (detached): parked: dirty, HEAD older than 3 days, no open PR",
        f"{tmp_path}/named: detached/none -> (detached HEAD)/?",
    )


@pytest.mark.parametrize(
    ("key", "answer"),
    [
        ("remote get-url origin", (1, "", "No such remote 'origin'")),
        ("pr list -R o/r --head detached", (1, "", "HTTP 502")),
        ("pr list -R o/r --head detached", "not json"),
        ("pr list -R o/r --head detached", "null"),
    ],
)
def test_worktrees_a_branch_named_detached_whose_origin_or_pr_read_fails_is_unread(tmp_path, key, answer):
    good = _old_dirty_answers(tmp_path)
    store = fresh_store().notes("agent")
    inst = Worktrees([str(tmp_path / "main")], store, every=None, parked_days=3, run=FakeSh(**good))
    assert asyncio.run(inst.run({})).control
    kept = store.get(f"worktrees:{tmp_path}/main").value
    inst.sh = FakeSh(**good)
    inst.sh.set(key, answer)
    out = asyncio.run(inst.run({}))
    origin = (f"{tmp_path}/main: origin failed: No such remote 'origin'",) if key.startswith("remote") else ()
    assert not out.control and out.findings == (
        *origin,
        f"{tmp_path}/named: the PR read failed; the tree is unread",
    )  # never confirmed PR-less, so never parked; the detached HEAD beside it is read
    heads = [c[c.index("--head") + 1] for c in inst.sh.calls if "--head" in c]
    assert heads == ([] if origin else ["detached"])
    assert store.get(f"worktrees:{tmp_path}/main").value == kept


def test_bench_jobs_reports_an_unreported_job_whose_process_is_gone(tmp_path):
    d = bench_dir(tmp_path)
    jobs = {
        "r1": {"pid": 4242, "started": "x", "asker": "lambda:x", "reported": False},
        "r0": {"pid": 1, "reported": True},
    }
    resident_store(d).put("jobs", jobs, run="-", seq=0)
    (tmp_path / "results" / "r1").mkdir(parents=True)
    (tmp_path / "results" / "r1" / "ledger.jsonl").write_text('{"a":1}\n{"b":2}\n')
    inst = BenchJobs(str(d), str(tmp_path / "results"), every="every:1h", run=FakeSh(**{"-p 4242": ""}))
    out = asyncio.run(inst.run({}))
    assert out.control and out.findings == (
        "job r1: unreported, pid 4242 gone, 2 ledger row(s); asked by lambda:x",
    )
    assert out.evidence.startswith(f"control: {d} notes read, 2 job(s) noted\n")
    out = asyncio.run(BenchJobs(str(tmp_path / "nope"), str(tmp_path), every=None).run({}))
    assert not out.control and out.findings == (
        f"no bench notes at {tmp_path / 'nope'}: chores.toml names no actor",
    )


def test_lock_holder_resolves_each_holders_pid_to_its_full_command_line_beside_its_own_held_lock(tmp_path):
    own, lock, free = tmp_path / "agent.pid", tmp_path / "build.lock", tmp_path / "free.lock"
    me = os.getpid()
    listed = f"p{me}\nf3\nn{os.path.realpath(own)}\np4242\nf5\nn{os.path.realpath(lock)}\n"
    sh = FakeSh(
        **{
            "-F pn": (1, listed, ""),  # rc 1: a named file nobody holds open
            "-o pid=,command= -p": (
                f"{me} /usr/bin/python3 -m lion agent --dir /a\n 4242 cargo build --release\n"
            ),
        }
    )
    inst = LockHolder([str(lock), str(free)], str(own), every="every:15m", run=sh)
    out = asyncio.run(inst.run({}))
    assert out.control and out.findings == (f"{lock}: held by pid 4242: cargo build --release",)
    assert out.positive == f"own lock {own} held by own pid {me}: /usr/bin/python3 -m lion agent --dir /a"
    assert out.evidence.startswith(f"control: {out.positive}\n") and f"{free}: no holder" in out.evidence
    assert (out.count, out.predicate) == (2, "open by a process (1 match)")
    # read by the lock's file and the pid, never by matching command text and never by taking the lock
    assert sh.calls == [
        [instr.LSOF, "-w", "-F", "pn", "--", str(own), str(lock), str(free)],
        ["/bin/ps", "-ww", "-o", "pid=,command=", "-p", ",".join(map(str, sorted({me, 4242})))],
    ]
    sh.set("-o pid=,command= -p", f"{me} /usr/bin/python3 -m lion agent --dir /a\n")
    out = asyncio.run(inst.run({}))  # the holder exited between the two reads
    assert out.control and out.findings == (
        f"{lock}: held by pid 4242: (no command line: the pid exited between the reads)",
    )


@pytest.mark.parametrize("broken", ["lsof", "ps"])
def test_lock_holder_fails_its_control_when_its_own_held_lock_does_not_read_as_its_own(tmp_path, broken):
    own, lock = tmp_path / "agent.pid", tmp_path / "build.lock"
    me = os.getpid()
    sh = FakeSh(
        **{
            "-F pn": f"p{me}\nf3\nn{os.path.realpath(own)}\n",
            "-o pid=,command= -p": f"{me} /usr/bin/python3 -m lion agent\n",
        }
    )
    # a dead lsof answers the way a lock nobody holds does: empty, rc 1
    sh.set("-F pn" if broken == "lsof" else "-o pid=,command= -p", (1, "", "no such program"))
    out = asyncio.run(LockHolder([str(lock)], str(own), every=None, run=sh).run({}))
    assert not out.control and out.findings == () and out.positive == ""
    assert "read broken" in out.evidence.splitlines()[0]


def test_lock_holder_reports_a_lock_lsof_could_not_read_as_unreadable_not_unheld(tmp_path):
    own, lock, gone = tmp_path / "agent.pid", tmp_path / "shut" / "build.lock", tmp_path / "free.lock"
    me = os.getpid()
    # lsof names each file it could not stat on stderr, by its resolved path, and still lists the rest
    err = (
        f"lsof: status error on {os.path.realpath(lock)}: Permission denied\n"
        f"lsof: status error on {os.path.realpath(gone)}: No such file or directory\n"
    )
    sh = FakeSh(
        **{
            "-F pn": (1, f"p{me}\nf3\nn{os.path.realpath(own)}\n", err),
            "-o pid=,command= -p": f"{me} /usr/bin/python3 -m lion agent\n",
        }
    )
    out = asyncio.run(LockHolder([str(lock), str(gone)], str(own), every=None, run=sh).run({}))
    assert out.control and out.findings == (f"{lock}: unreadable by lsof: Permission denied",)
    assert f"{lock}: unreadable: Permission denied" in out.evidence
    assert f"{gone}: no holder (no such file)" in out.evidence  # a lock with no file is held by nobody


@pytest.mark.parametrize("stream", ["held", "unreadable"])
@pytest.mark.parametrize("name", ["build\nline.lock", "build\ttab.lock", "build\\back.lock"])
def test_lock_holder_a_name_lsof_escaped_fails_the_control_and_is_named_as_unmapped(tmp_path, stream, name):
    own, lock = tmp_path / "agent.pid", tmp_path / name
    me = os.getpid()
    # lsof prints a name it escapes as the escape (`\n`, `\t`, `\\`), on stdout and on stderr alike
    shown = os.path.realpath(lock).replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")
    listed = f"p{me}\nf3\nn{os.path.realpath(own)}\n"
    answer = (
        (0, listed + f"p4242\nf5\nn{shown}\n", "")
        if stream == "held"
        else (1, listed, f"lsof: status error on {shown}: Permission denied\n")
    )
    sh = FakeSh(**{"-F pn": answer, "-o pid=,command= -p": f"{me} python -m lion agent\n 4242 cargo build\n"})
    out = asyncio.run(LockHolder([str(lock)], str(own), every=None, run=sh).run({}))
    assert not out.control and out.positive == ""
    assert "read broken" in out.evidence.splitlines()[0] and f"unmapped: {shown}" in out.evidence


@pytest.mark.skipif(instr.LSOF == "lsof", reason="no lsof on this machine")
def test_lock_holder_reads_a_lock_a_live_process_holds_through_the_real_lsof_and_ps(tmp_path):
    import fcntl
    import subprocess
    import sys

    own, lock, free = tmp_path / "agent.pid", tmp_path / "held.lock", tmp_path / "free.lock"
    tabbed = tmp_path / "held\ttab.lock"
    fd = os.open(own, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    hold = (
        "import fcntl, os, sys; fds = [os.open(p, os.O_RDWR | os.O_CREAT) for p in sys.argv[1:]]; "
        "fcntl.flock(fds[0], fcntl.LOCK_EX); print('locked', flush=True); sys.stdin.read()"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", hold, str(lock), str(tabbed)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        out = asyncio.run(LockHolder([str(lock), str(free)], str(own), every=None).run({}))
        # lsof prints the tab as `\t`: a held lock under a name it escaped voids the read, never unheld
        escaped = asyncio.run(LockHolder([str(tabbed)], str(own), every=None).run({}))
    finally:
        holder.stdin.close()
        holder.wait(timeout=5)
        holder.stdout.close()
        os.close(fd)
    assert out.control, out.evidence
    assert len(out.findings) == 1 and out.findings[0].startswith(f"{lock}: held by pid {holder.pid}: ")
    # the evidence carries the full command line, its argument included; the finding caps it
    assert any(
        ln.startswith(f"{lock}: pid {holder.pid}: ") and f" {lock} " in ln for ln in out.evidence.splitlines()
    )
    assert f"{free}: no holder" in out.evidence
    assert not escaped.control and escaped.positive == "", escaped.evidence
    assert f"unmapped: {os.path.realpath(tabbed)}".replace("\t", "\\t") in escaped.evidence


def test_scheduled_job_reads_the_artifact_first_then_the_supervisors_last_exit(tmp_path):
    d = tmp_path / "out"
    d.mkdir()
    (d / "fresh.json").write_text("{}")
    (d / "stale.json").write_text("{}")
    old = time.time() - 30 * 3600
    os.utime(d / "stale.json", (old, old))
    sh = FakeSh(
        **{
            "/com.example.fresh": "\tstate = not running\n\tlast exit code = 0\n",
            "/com.example.stale": "\tstate = not running\n\tlast exit code = 0\n",
            "/com.example.failed": "\tstate = not running\n\tlast exit code = 1: Operation not permitted\n",
            "/com.example.new": "\tstate = waiting\n\tlast exit code = (never exited)\n",
            "/com.example.gone": (113, "", 'Could not find service "com.example.gone" in domain'),
        }
    )
    jobs = [
        {"artifact": str(d / "fresh.json"), "max_age_h": 24, "label": "com.example.fresh"},
        {"artifact": str(d / "stale.json"), "max_age_h": 24, "label": "com.example.stale"},
        {"artifact": str(d / "none.json"), "max_age_h": 24, "label": "com.example.failed"},
        {"artifact": str(d / "fresh.json"), "max_age_h": 1, "label": "com.example.new"},
        {"artifact": str(d / "fresh.json"), "max_age_h": 24, "label": "com.example.gone"},
        {"artifact": str(d / "fresh.json"), "max_age_h": 24},
    ]
    inst = ScheduledJob(jobs, every="every:1h", run=sh)
    out = asyncio.run(inst.run({}))
    assert out.control and out.findings == (
        f"{d}/stale.json: older than its 24h bound",  # a clean exit beside a stale artifact is a finding
        f"{d}/none.json: absent, bound 24h",
        f"{d}/none.json: its supervisor com.example.failed last exited 1",
        f"{d}/fresh.json: its supervisor com.example.gone is not loaded",
    )
    lines = out.evidence.splitlines()
    assert lines[1].startswith(f"{d}/stale.json: written 30.0h ago, bound 24h | launchd com.example.stale")
    assert lines[3].endswith("| launchd com.example.new: last exit (never exited)")
    assert (out.count, out.predicate.endswith("(3 match)")) == (6, True)
    assert out.positive.startswith(f"{d} present; launchd com.example.fresh row read; {d} present")
    assert [c[:2] for c in sh.calls] == [[instr.LAUNCHCTL, "print"]] * 5
    assert asyncio.run(inst.run({})).findings == out.findings  # the age stays in the evidence: told once


@pytest.mark.parametrize("broken", ["directory", "row"])
def test_scheduled_job_fails_its_control_on_an_absent_directory_or_a_row_without_its_exit(tmp_path, broken):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.json").write_text("{}")
    artifact = tmp_path / ("gone" if broken == "directory" else "out") / "a.json"
    row = "\tstate = running\n" if broken == "row" else "\tlast exit code = 0\n"
    inst = ScheduledJob(
        [{"artifact": str(artifact), "max_age_h": 24, "label": "com.example.job"}],
        every=None,
        run=FakeSh(**{"/com.example.job": row}),
    )
    out = asyncio.run(inst.run({}))
    assert not out.control and out.findings == () and out.positive == ""
    reason = "its directory is absent" if broken == "directory" else "no last exit code in its row (unread)"
    assert reason in out.evidence


@pytest.mark.parametrize("kind", ["dir", "symlink-to-dir", "file", "symlink-to-file"])
def test_scheduled_job_an_artifact_that_is_not_a_regular_file_is_a_finding_never_fresh(tmp_path, kind):
    (tmp_path / "out").mkdir()
    artifact, real = tmp_path / "out" / "result.json", tmp_path / "real"
    made = artifact if kind in ("dir", "file") else real
    if kind.endswith("dir"):
        made.mkdir()
    else:
        made.write_text("{}")
    if made is real:
        artifact.symlink_to(real)
    inst = ScheduledJob([{"artifact": str(artifact), "max_age_h": 24}], every=None, run=FakeSh())
    out = asyncio.run(inst.run({}))
    assert out.control
    if kind.endswith("dir"):
        assert out.findings == (f"{artifact}: not a regular file, bound 24h",)
        assert out.evidence == f"{artifact}: not a regular file"
    else:
        assert out.findings == () and out.evidence == f"{artifact}: written 0.0h ago, bound 24h"


@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
def test_scheduled_job_a_supervisor_read_that_did_not_finish_is_unread_not_unloaded(tmp_path, stale):
    (tmp_path / "out").mkdir()
    artifact = tmp_path / "out" / "a.json"
    artifact.write_text("{}")
    if stale:
        old = time.time() - 30 * 3600
        os.utime(artifact, (old, old))
    sh = FakeSh(**{"/com.example.job": (124, "", "/bin/launchctl did not finish within 60s")})
    job = {"artifact": str(artifact), "max_age_h": 24, "label": "com.example.job"}
    out = asyncio.run(ScheduledJob([job], every=None, run=sh).run({}))
    assert not out.control and out.positive == ""
    assert out.findings == ((f"{artifact}: older than its 24h bound",) if stale else ())
    assert "launchd com.example.job: unread, rc 124: /bin/launchctl did not finish within 60s" in out.evidence
    assert "not loaded" not in out.evidence
    sh.set("/com.example.job", (113, "", 'Could not find service "com.example.job" in domain'))
    out = asyncio.run(ScheduledJob([job], every=None, run=sh).run({}))
    assert out.control and out.findings[-1] == f"{artifact}: its supervisor com.example.job is not loaded"


@pytest.mark.parametrize("bound", ["nan", '"soon"', "true", "inf", "-1.0", "0"])
def test_scheduled_job_a_max_age_h_that_is_not_a_positive_number_is_refused_at_build(tmp_path, bound):
    import tomllib
    from types import SimpleNamespace

    job = f'{{artifact = "/x/out.json", max_age_h = {bound}}}'
    cfg = tomllib.loads(f'owner = "lambda:x"\n[chores.scheduled-job]\njobs = [{job}]\n')
    with pytest.raises(ValueError, match="max_age_h is a positive number of hours"):
        instr.instruments(cfg, SimpleNamespace(cwd=tmp_path), fresh_store().notes("agent"))


def test_instruments_builds_the_lock_holder_on_the_agents_own_pid_file_and_the_scheduled_jobs(tmp_path):
    from types import SimpleNamespace

    cfg = {
        "owner": "lambda:x",
        "chores": {
            "lock-holder": {"every": "every:15m", "locks": ["/tmp/a.lock"]},
            "scheduled-job": {"every": "every:1h", "jobs": [{"artifact": "/x/out.json", "max_age_h": 26}]},
        },
    }
    built = instr.instruments(cfg, SimpleNamespace(cwd=tmp_path), fresh_store().notes("agent"))
    assert sorted(built) == ["lock-holder", "scheduled-job"]
    lock_holder = built["lock-holder"]
    assert (lock_holder.locks, lock_holder.held) == (["/tmp/a.lock"], str(tmp_path / "agent.pid"))
    assert built["scheduled-job"].jobs == [{"artifact": "/x/out.json", "max_age_h": 26}]
    assert {i.every for i in built.values()} == {"every:15m", "every:1h"}
    assert not any(i.empty_expected for i in built.values())
    cfg["chores"]["scheduled-job"]["jobs"] = [{"artifact": "/x/out.json"}]
    with pytest.raises(ValueError, match="a scheduled job names its artifact and its max_age_h"):
        instr.instruments(cfg, SimpleNamespace(cwd=tmp_path), fresh_store().notes("agent"))


def test_build_reads_the_actors_config_bounds_the_client_and_gates_the_mail(tmp_path, kkernel_stub):
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config("lambda:x:agent", tmp_path))
    (tmp_path / "chores.toml").write_text(
        'actor = "lambda:x:agent"\nowner = "lambda:x"\nsteward = "lambda:owner"\n'
        'trusted = ["daemon:review"]\n'
        'aged_after = 3\n[chores.disk]\nevery = "every:2h"\nroots = ["/"]\n'
        '[chores.inbox-sla]\nevery = "every:15m"\nactors = ["lambda:x"]\n'
    )
    chores, agent = build(tmp_path, backend=None)
    assert chores.trusted == frozenset({"lambda:x", "lambda:owner", "lambda:x:agent", "daemon:review"})
    assert chores.aged_after == 3 and sorted(chores.instruments) == ["disk", "inbox-sla"]
    assert isinstance(agent.khive, Bounded) and agent.khive.recipients == chores.trusted
    assert agent.admit({"properties": {"from_actor": "daemon:review"}}) is True
    assert agent.admit({"properties": {"from_actor": "lambda:stranger"}}) is False
    with pytest.raises(AgentError, match="agent boundary"):
        asyncio.run(agent.khive.exec('gtd.complete(id="x")'))
    assert sorted((n, i.direction) for n, i in chores.instruments.items()) == [
        ("disk", "over-report"),
        ("inbox-sla", "under-report"),
    ]
    assert agent.profile_for([]).name == "chores"


def desk_dir(tmp_path, front: str = "") -> None:
    """An agent directory whose `[desk]` reads the chair `lambda:x`; `front` is appended to the table."""
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config("lambda:x:agent", tmp_path))
    chair = tmp_path / "chair"
    (chair / ".khive").mkdir(parents=True)
    (chair / ".khive" / "config.toml").write_text(routed_config("lambda:x", chair))
    (tmp_path / "chores.toml").write_text(
        'actor = "lambda:x:agent"\nowner = "lambda:x"\nsteward = "lambda:owner"\n'
        + f"[desk]\nactor_dir = {json.dumps(str(chair))}\n{front}"
    )


def test_build_with_front_false_builds_no_front_desk(tmp_path, kkernel_stub):
    """`[desk] front = false` wires no front desk: nothing sweeps, answers, routes, tasks or escalates
    the chair's mail, and the digest keeps its own tick."""
    desk_dir(tmp_path, front="front = false\n")
    chores, agent = build(tmp_path, backend=None)
    assert getattr(agent, "desk", None) is None and agent.watch is None and agent.accept is None
    assert not {"sweep", "answer", "leave", "clarify", "route", "task"} & set(agent.actor.handlers)
    assert chores.extra_specs == frozenset() and chores.digest_every == "daily"
    assert agent.profile_for([]).specs == frozenset({"check", "digest", "aged"})
    assert "front desk" not in agent.profile_for([]).system


def test_build_with_front_false_refuses_desk_routes_and_no_route_only_recipient_is_reached(
    tmp_path, monkeypatch
):
    """With `front = false` a `[desk.routes]` value would widen the bounded recipients with no Desk to
    validate it, and the landing arm's outsider probe would reach that route-only recipient. The config
    refuses at build."""
    clients: list[FakeKhive] = []

    def client(cwd, actor):
        k = FakeKhive()
        k.cwd, k.actor = cwd, actor
        clients.append(k)
        return k

    monkeypatch.setattr(cli_agent, "Khive", client)
    desk_dir(tmp_path, front='front = false\n[desk.routes]\nbuild = "email:outsider@example.com"\n')
    refused = ""
    try:
        chores, agent = build(tmp_path, backend=None)
    except ValueError as e:
        refused = str(e)
    else:
        asyncio.run(arm(chores, agent))
    assert [s["to"] for k in clients for s in k.sent] == []
    assert refused == "[desk.routes] needs the front desk: front = false names no routes"


SHA = "a" * 40
SEVEN = [
    "local/admission",
    "local/baseline",
    "local/build",
    "local/guards",
    "local/lint",
    "local/shellcheck",
    "local/suites",
]
CLASSIC_SEVEN = json.dumps(
    {"strict": False, "contexts": SEVEN, "checks": [{"context": c, "app_id": None} for c in SEVEN]}
)
RULESET_SEVEN = json.dumps(
    [
        {"type": "deletion", "ruleset_id": 1},
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": c} for c in SEVEN]},
            "ruleset_id": 1,
        },
    ]
)
CLASSIC_404 = (
    1,
    '{"message":"Branch not protected","status":"404"}',
    "gh: Branch not protected (HTTP 404)\n",
)
INVENTORY_KEYS = {"repository", "repository_name", "head", "required", "observed_at", "envelope"}
ROW_KEYS = {
    "context",
    "source",
    "base",
    "app",
    "app_slug",
    "required_app",
    "workflow_id",
    "event",
    "run_id",
    "conclusion",
    "completed_at",
}
ENVELOPE_KEYS = {"source_identity", "source_event", "interval", "completeness", "reason", "payload_digest"}


def statuses(*rows: tuple[str, str]) -> str:
    return json.dumps(
        {
            "total": len(rows),
            "rows": [{"context": c, "state": s, "created_at": "2026-09-22T10:00:00Z"} for c, s in rows],
        }
    )


def required_sh(admin: bool | None = True, **over) -> FakeSh:
    answers = {
        "api repos/o/r --jq": json.dumps({"full_name": "o/r", "id": 42, "admin": admin}),
        "pr list -R o/r": json.dumps([{"number": 7, "headRefOid": SHA, "baseRefName": "main"}]),
        "rules/branches/main": "[]",
        "branches/main/protection/required_status_checks": CLASSIC_SEVEN,
        f"commits/{SHA}/status": statuses(
            *[(c, "success") for c in SEVEN[:5]], ("local/shellcheck", "failure")
        ),
        f"commits/{SHA}/check-runs": json.dumps({"total": 0, "rows": []}),
    }
    return FakeSh(**{**answers, **over})


def required(sh: FakeSh, classic: bool = True, state=None) -> RequiredContexts:
    return RequiredContexts(
        [{"name": "o/r", "classic": classic}],
        state or fresh_store().notes("agent"),
        every=None,
        source_identity="lambda:x:desk",
        run=sh,
    )


def test_required_contexts_reads_seven_classic_names_by_name_and_emits_the_check_inventory_observation():
    out = asyncio.run(required(required_sh()).run({"source_event": "chore run r1"}))
    assert out.control and out.count == 1
    line = "o/r#7 aaaaaaaa: 5/7 by name; missing: [local/suites]; failing: [local/shellcheck=failure]"
    assert out.findings == (line,)  # first sight: the full row
    assert "o/r main: required local/admission [classic], local/baseline [classic]" in out.evidence
    assert "ruleset read (0 rule(s)); classic read" in out.evidence
    assert out.predicate.endswith("(1 match)") and out.positive.startswith("o/r resolved as itself (id 42)")
    (obs,) = out.data
    assert set(obs) == INVENTORY_KEYS and set(obs["envelope"]) == ENVELOPE_KEYS
    assert all(set(r) == ROW_KEYS for r in obs["required"]) and len(obs["required"]) == 7
    assert obs["repository"] == 42 and obs["repository_name"] == "o/r" and obs["head"] == SHA
    by = {r["context"]: r for r in obs["required"]}
    assert by["local/suites"] == {
        "context": "local/suites",
        "source": "classic",
        "base": "main",
        "app": None,
        "app_slug": None,
        "required_app": None,
        "workflow_id": None,
        "event": None,
        "run_id": None,
        "conclusion": "ABSENT",
        "completed_at": None,
    }
    assert by["local/shellcheck"]["conclusion"] == "failure" and by["local/guards"]["conclusion"] == "success"
    assert by["local/guards"]["completed_at"] == "2026-09-22T10:00:00Z"
    env = obs["envelope"]
    assert env["completeness"] == "complete" and env["reason"] is None  # complete | partial | unavailable
    assert env["source_identity"] == "lambda:x:desk" and env["source_event"] == "chore run r1"
    assert set(env["interval"]) == {"start", "end"} and env["interval"]["start"] <= env["interval"]["end"]
    assert obs["observed_at"].endswith("Z") and datetime.fromisoformat(obs["observed_at"]).tzinfo is not None
    payload = {k: obs[k] for k in ("repository", "repository_name", "head", "required")}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert env["payload_digest"] == hashlib.sha256(canonical).hexdigest()


def test_required_contexts_first_sight_is_the_row_then_only_transitions():
    sh, store = required_sh(), fresh_store().notes("agent")
    inst = required(sh, state=store)
    assert len(asyncio.run(inst.run({})).findings) == 1
    assert asyncio.run(inst.run({})).findings == ()  # nothing moved: quiet
    sh.set(f"commits/{SHA}/status", statuses(*[(c, "success") for c in SEVEN]))
    sh.set(
        "pr list -R o/r",
        json.dumps(
            [
                {"number": 7, "headRefOid": SHA, "baseRefName": "main"},
                {"number": 8, "headRefOid": SHA, "baseRefName": "main"},
            ]
        ),
    )
    out = asyncio.run(inst.run({}))
    assert out.findings == (
        "o/r#7 aaaaaaaa: 7/7 by name; missing: []; failing: []",
        "o/r#8 aaaaaaaa: 7/7 by name; missing: []; failing: []",  # born green: its first row all the same
    )
    sh.set("pr list -R o/r", json.dumps([{"number": 8, "headRefOid": SHA, "baseRefName": "main"}]))
    assert asyncio.run(inst.run({})).findings == ("o/r#7: no longer open",)
    out = asyncio.run(inst.run({"prs": ["o/r#8", "o/r#9"]}))  # an ask: the named rows, snapshot untouched
    assert out.findings == (
        "o/r#8 aaaaaaaa: 7/7 by name; missing: []; failing: []",
        "o/r#9: not among the open PRs read",
    )
    assert asyncio.run(inst.run({})).findings == ()


def test_required_contexts_classic_404_without_admin_is_unread_and_the_required_set_is_not_empty():
    sh = required_sh(
        admin=False,
        **{
            "rules/branches/main": (1, "", "gh: Forbidden (HTTP 403)\n"),
            "branches/main/protection/required_status_checks": CLASSIC_404,
        },
    )
    out = asyncio.run(required(sh).run({}))
    assert not out.control
    assert "o/r main: neither surface read; the required set is UNREAD" in out.findings
    assert "o/r#7 aaaaaaaa: required set UNREAD on main; nothing judged" in out.findings
    assert "classic UNREAD (404 without admin reads the same as unprotected)" in out.evidence
    assert not any(" 0/0 " in f for f in out.findings)  # never an empty required set
    (obs,) = out.data
    assert obs["required"] is None and obs["envelope"]["completeness"] == "unavailable"  # nothing read
    assert not any(
        f"commits/{SHA}" in " ".join(c) for c in sh.calls
    )  # nothing read at a head it cannot judge


def test_required_contexts_classic_unread_beside_a_read_ruleset_is_partial_with_its_reason():
    sh = required_sh(
        admin=False,
        **{
            "rules/branches/main": RULESET_SEVEN,
            "branches/main/protection/required_status_checks": CLASSIC_404,
        },
    )
    out = asyncio.run(required(sh, classic=False).run({}))
    assert out.control  # the rulesets read: the set is stated, and said to be partial
    assert out.findings[0] == (
        "o/r main: classic UNREAD (404 without admin reads the same as unprotected); "
        "the required set is partial"
    )
    (obs,) = out.data
    assert {r["source"] for r in obs["required"]} == {"ruleset"} and len(obs["required"]) == 7
    assert obs["envelope"]["completeness"] == "partial"
    assert obs["envelope"]["reason"] == "classic UNREAD (404 without admin reads the same as unprotected)"


def test_required_contexts_classic_404_with_admin_is_none_and_an_empty_set_is_a_value():
    sh = required_sh(admin=True, **{"branches/main/protection/required_status_checks": CLASSIC_404})
    out = asyncio.run(required(sh, classic=False).run({}))
    assert out.control and out.findings == (
        "o/r#7 aaaaaaaa: 0/0 by name; missing: []; failing: [] (no context required on main)",
    )
    assert out.data[0]["required"] == [] and out.data[0]["envelope"]["completeness"] == "complete"
    out = asyncio.run(required(sh, classic=True).run({}))  # declared classic, and none is there
    assert "o/r main: classic protection declared and none is there" in out.findings


def test_required_contexts_both_surfaces_empty_at_the_head_is_required_checks_do_not_exist():
    sh = required_sh(**{f"commits/{SHA}/status": statuses()})
    out = asyncio.run(required(sh).run({}))
    assert out.control and out.findings == (
        f"o/r#7 aaaaaaaa: 0/7 by name; missing: [{', '.join(SEVEN)}]; failing: []; "
        "REQUIRED CHECKS DO NOT EXIST AT THIS HEAD",
    )
    assert {r["conclusion"] for r in out.data[0]["required"]} == {"ABSENT"}


def test_required_contexts_names_a_check_run_by_its_actions_run_and_the_newest_row_per_name_wins():
    rules = json.dumps(
        [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {"context": "ci", "integration_id": 15368},
                        {"context": "lint"},
                        {"context": "e2e"},
                    ]
                },
            }
        ]
    )
    runs = runs_page(
        [
            {
                "id": 555,
                "workflow_id": 9,
                "name": "CI",
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-09-22T09:00:00Z",
            }
        ]
    )
    check_runs = {
        "total": 3,
        "rows": [
            {
                "name": "ci",
                "app": 15368,
                "app_slug": "github-actions",
                "status": "completed",
                "conclusion": "success",
                "details_url": "https://github.com/o/r/actions/runs/555/job/999",
                "started_at": "2026-09-22T11:00:00Z",
                "completed_at": "2026-09-22T11:05:00Z",
            },
            {
                "name": "lint",
                "app": 15368,
                "app_slug": "github-actions",
                "status": "completed",
                "conclusion": "skipped",
                "details_url": "https://github.com/o/r/actions/runs/555/job/998",
                "started_at": "2026-09-22T11:00:00Z",
                "completed_at": "2026-09-22T11:00:01Z",
            },
            {
                "name": "e2e",
                "app": 15368,
                "app_slug": "github-actions",
                "status": "in_progress",
                "conclusion": None,
                "details_url": "https://github.com/o/r/actions/runs/555/job/997",
                "started_at": "2026-09-22T11:00:00Z",
                "completed_at": None,
            },
        ],
    }
    sh = required_sh(
        **{
            "rules/branches/main": rules,
            "branches/main/protection/required_status_checks": CLASSIC_404,
            f"commits/{SHA}/status": statuses(("ci", "failure")),  # older than the check-run of the same name
            f"commits/{SHA}/check-runs": json.dumps(check_runs),
            "actions/runs?head_sha=": runs,
        }
    )
    out = asyncio.run(required(sh, classic=False).run({}))
    assert out.control and out.findings == (
        "o/r#7 aaaaaaaa: 1/3 by name; missing: []; failing: [lint=skipped]; pending: [e2e=in_progress]",
    )
    by = {r["context"]: r for r in out.data[0]["required"]}
    assert by["ci"] == {
        "context": "ci",
        "source": "ruleset",
        "base": "main",
        "app": 15368,
        "app_slug": "github-actions",
        "required_app": 15368,
        "workflow_id": 9,
        "event": "pull_request",
        "run_id": 555,  # the run in details_url, not the trailing job id
        "conclusion": "success",
        "completed_at": "2026-09-22T11:05:00Z",
    }
    assert by["e2e"]["conclusion"] == "pending" and by["e2e"]["completed_at"] is None


RULE_CI = json.dumps(
    [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "ci"}]}}]
)


def check_run(run: int, *, status: str = "completed", started: str | None = "2026-09-22T10:00:00Z") -> dict:
    """A check-run named ci as the check-runs query prints it; `run` is the Actions run in its details_url."""
    done = status == "completed"
    return {
        "name": "ci",
        "app": 15368,
        "app_slug": "github-actions",
        "status": status,
        "conclusion": "success" if done else None,
        "details_url": f"https://github.com/o/r/actions/runs/{run}/job/{run + 1000}",
        "started_at": started,
        "completed_at": "2026-09-22T11:00:00Z" if done else None,
    }


def queued_sh(checks: list[dict], states: str) -> FakeSh:
    """ci required by a ruleset; a queued ci check on run 2, created two hours after the 10:00 statuses."""
    runs = [
        {
            "id": 2,
            "workflow_id": 9,
            "name": "CI",
            "event": "pull_request",
            "status": "queued",
            "conclusion": None,
            "created_at": "2026-09-22T12:00:00Z",
        },
        {
            "id": 1,
            "workflow_id": 9,
            "name": "CI",
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-09-22T09:00:00Z",
        },
    ]
    return required_sh(
        **{
            "rules/branches/main": RULE_CI,
            "branches/main/protection/required_status_checks": CLASSIC_404,
            f"commits/{SHA}/status": states,
            f"commits/{SHA}/check-runs": json.dumps({"total": len(checks), "rows": checks}),
            "actions/runs?head_sha=": runs_page(runs),
        }
    )


@pytest.mark.parametrize("arm", ["older status", "older check first", "older check last"])
def test_required_contexts_a_queued_check_is_ordered_by_its_runs_creation_never_lost_to_an_older_green(arm):
    """A queued check has no started_at: its run's creation orders it, so an older timestamped green (a
    status, or a check read before or after it) never takes its name."""
    queued, older = check_run(2, status="queued", started=None), check_run(1, started="2026-09-22T09:00:00Z")
    checks = {
        "older status": [queued],
        "older check first": [older, queued],
        "older check last": [queued, older],
    }
    states = statuses(("ci", "success")) if arm == "older status" else statuses()
    out = asyncio.run(required(queued_sh(checks[arm], states), classic=False).run({}))
    (row,) = out.data[0]["required"]
    assert (row["conclusion"], row["run_id"], row["event"], row["workflow_id"]) == (
        "pending",
        2,
        "pull_request",
        9,
    )
    assert out.control and out.data[0]["envelope"]["reason"] is None


def test_required_contexts_a_name_whose_candidates_cannot_be_ordered_is_pending_and_partial_never_green():
    """A queued check from a producer with no Actions run (no started_at, no run creation time) beside an
    older green status: nothing orders the two, so the name is pending and the read partial."""
    queued = {**check_run(0, status="queued", started=None), "details_url": "https://ci.example/build/7"}
    out = asyncio.run(required(queued_sh([queued], statuses(("ci", "success"))), classic=False).run({}))
    (row,) = out.data[0]["required"]
    assert row["conclusion"] == "pending" and row["run_id"] is None
    assert not out.control and out.findings == (
        "o/r#7 aaaaaaaa: 0/1 by name; missing: []; failing: []; pending: [ci=ordering unresolved]; "
        "head read incomplete: ci: ordering unresolved",
    )
    env = out.data[0]["envelope"]
    assert env["completeness"] == "partial" and env["reason"] == "ci: ordering unresolved"


def test_required_contexts_a_queued_check_alone_is_pending_on_its_run():
    """With no older status the queued check is the name's only row."""
    queued = check_run(2, status="queued", started=None)
    out = asyncio.run(required(queued_sh([queued], statuses()), classic=False).run({}))
    (row,) = out.data[0]["required"]
    assert out.control and (row["conclusion"], row["run_id"]) == ("pending", 2)


def tie_sh(checks: list[dict], runs: list[dict]) -> FakeSh:
    """ci required by a ruleset; a green ci status stamped 10:00 beside the given checks and runs."""
    return required_sh(
        **{
            "rules/branches/main": RULE_CI,
            "branches/main/protection/required_status_checks": CLASSIC_404,
            f"commits/{SHA}/status": statuses(("ci", "success")),
            f"commits/{SHA}/check-runs": json.dumps({"total": len(checks), "rows": checks}),
            "actions/runs?head_sha=": runs_page(runs),
        }
    )


def ci_run(n: int, check: dict, created: str) -> dict:
    """The Actions run a check-run names in its details_url, in that check's state."""
    run = {"id": n, "workflow_id": 9, "name": "CI", "event": "pull_request", "created_at": created}
    return run | {"status": check["status"], "conclusion": check["conclusion"]}


@pytest.mark.parametrize("mode", ["queued", "completed failure"])
def test_required_contexts_equal_stamps_that_disagree_are_pending_and_partial_never_the_green_one(mode):
    """A green status and a queued or failed check stamped 10:00 alike: one stamp orders neither, so the
    status, read first, does not win the tie; the name is pending and the read partial."""
    queued = mode == "queued"
    check = check_run(
        2, status="queued" if queued else "completed", started=None if queued else "2026-09-22T10:00:00Z"
    )
    check["conclusion"] = None if queued else "failure"
    out = asyncio.run(
        required(tie_sh([check], [ci_run(2, check, "2026-09-22T10:00:00Z")]), classic=False).run({})
    )
    (row,) = out.data[0]["required"]
    env = out.data[0]["envelope"]
    assert (row["conclusion"], env["completeness"], env["reason"]) == (
        "pending",
        "partial",
        "ci: ordering unresolved",
    )
    assert not out.control and row["run_id"] is None


@pytest.mark.parametrize("arm", ["agreeing", "tie then newer", "newer then tie"])
def test_required_contexts_equal_stamps_that_agree_or_a_strictly_newer_row_still_order_the_name(arm):
    """The tie rule is narrow: a check agreeing with the green status at one stamp keeps the name
    complete, and a strictly newer row still wins over a disagreeing pair, read in either order."""
    agreeing, failed = check_run(2), {**check_run(2), "conclusion": "failure"}
    newer = check_run(3, started="2026-09-22T11:00:00Z")
    checks = {"agreeing": [agreeing], "tie then newer": [failed, newer], "newer then tie": [newer, failed]}
    tied = agreeing if arm == "agreeing" else failed
    runs = [ci_run(2, tied, "2026-09-22T10:00:00Z"), ci_run(3, newer, "2026-09-22T11:00:00Z")]
    out = asyncio.run(required(tie_sh(checks[arm], runs), classic=False).run({}))
    (row,) = out.data[0]["required"]
    env = out.data[0]["envelope"]
    assert out.control and (row["conclusion"], env["completeness"], env["reason"]) == (
        "success",
        "complete",
        None,
    )
    assert arm == "agreeing" or row["run_id"] == 3


def producer_sh() -> FakeSh:
    """A ruleset requiring integration 15368 for ci and local/suites: ci is a check-run by
    github-actions (app id 15368) on run 555, local/suites a status only."""
    rules = [{"context": "ci", "integration_id": 15368}, {"context": "local/suites", "integration_id": 15368}]
    run = {"id": 555, "workflow_id": 9, "name": "CI", "event": "pull_request", "status": "completed"}
    run |= {"conclusion": "success", "created_at": "2026-09-22T09:00:00Z"}
    return required_sh(
        **{
            "rules/branches/main": json.dumps(
                [{"type": "required_status_checks", "parameters": {"required_status_checks": rules}}]
            ),
            "branches/main/protection/required_status_checks": CLASSIC_404,
            f"commits/{SHA}/status": statuses(("local/suites", "success")),
            f"commits/{SHA}/check-runs": json.dumps({"total": 1, "rows": [check_run(555)]}),
            "actions/runs?head_sha=": runs_page([run]),
        }
    )


def test_required_contexts_app_is_the_observed_producer_and_the_rules_integration_is_required_app():
    """A status carries no producing app, so a status-only row never claims the rule's integration
    15368 as its producer; a check-run's app is its id, its slug beside it, as the check query asks."""
    sh = producer_sh()
    out = asyncio.run(required(sh, classic=False).run({}))
    by = {r["context"]: r for r in out.data[0]["required"]}
    assert by["local/suites"]["app"] is None  # a status carries no producing app
    assert (by["local/suites"]["app_slug"], by["local/suites"]["required_app"]) == (None, 15368)
    assert (by["ci"]["app"], by["ci"]["app_slug"], by["ci"]["required_app"]) == (
        15368,
        "github-actions",
        15368,
    )
    (query,) = [" ".join(c) for c in sh.calls if "check-runs" in " ".join(c)]
    assert "app: .app.id, app_slug: .app.slug," in query
    assert "  local/suites [ruleset] success app=None required_app=15368 event=None run=None" in out.evidence


def test_required_contexts_the_envelope_digest_covers_every_field_of_every_row():
    """The payload digest is taken over the rows as emitted, so a change to any field of a row changes
    it."""
    (obs,) = asyncio.run(required(producer_sh(), classic=False).run({})).data

    def digest(o: dict) -> str:
        payload = {k: o[k] for k in ("repository", "repository_name", "head", "required")}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()

    assert obs["envelope"]["payload_digest"] == digest(obs)
    for i, row in enumerate(obs["required"]):
        for field in row:
            rows = [*obs["required"][:i], {**row, field: "changed"}, *obs["required"][i + 1 :]]
            assert digest({**obs, "required": rows}) != obs["envelope"]["payload_digest"], field


def test_required_contexts_control_fails_when_the_repo_does_not_resolve_as_itself():
    sh = required_sh(**{"api repos/o/r --jq": json.dumps({"full_name": "other/r", "id": 1, "admin": True})})
    out = asyncio.run(required(sh).run({}))
    assert not out.control and out.findings[0].startswith("o/r: gh does not resolve it as itself")
    assert out.data == ()


def test_required_contexts_control_fails_when_admin_is_not_read():
    out = asyncio.run(required(required_sh(admin=None)).run({}))
    assert not out.control
    assert out.findings[0] == "o/r: admin not read (None); a classic 404 cannot be told from none"


def test_required_contexts_control_fails_when_the_prs_head_is_not_read():
    sh = required_sh(
        **{"pr list -R o/r": json.dumps([{"number": 7, "headRefOid": "", "baseRefName": "main"}])}
    )
    out = asyncio.run(required(sh).run({}))
    assert not out.control and out.findings == ("o/r#7: head not read ('')",) and out.data == ()


def test_required_contexts_control_fails_when_a_head_surface_does_not_read():
    sh = required_sh(**{f"commits/{SHA}/check-runs": (1, "", "gh: HTTP 502\n")})
    out = asyncio.run(required(sh).run({}))
    assert not out.control
    assert out.findings[0].endswith("; head read incomplete: check-runs unread: gh: HTTP 502")
    env = out.data[0]["envelope"]
    assert env["completeness"] == "partial" and env["reason"] == "check-runs unread: gh: HTTP 502"
    with pytest.raises(ValueError, match="classic = true|false"):
        RequiredContexts([{"name": "o/r"}], fresh_store().notes("agent"), every=None, source_identity="x")


HEAD7, HEAD8, HEAD9, OLD = "7" * 40, "8" * 40, "9" * 40, "0" * 40


def verdict(d, n: int, evaluated: str, *, head8: str, mtime: float, round_no=1, json_file=True, **kw):
    """One review verdict pair as the review daemon writes it: <owner>-<repo>-<n>-<head8>.json and .md."""
    blocking = kw.get("blocking", ["a finding"])
    body = {
        "artifact_schema_version": 11,
        "repo": kw.get("repo", "o/r"),
        "number": n,
        "evaluated_head": evaluated,
        "decision": kw.get("decision", "REQUEST-CHANGES"),
        "blocking": blocking,
        "asserted_blocking": kw.get("asserted", len(blocking)),
        "degraded": kw.get("degraded", False),
        "round_no": round_no,
    }
    md = (
        f"# Review verdict \u2014 {body['repo']}#{n} @ {head8}\n"
        f"- decision: **{body['decision']}** (hygiene: ok)\n"
        f"- degraded: {body['degraded']} ()\n- evaluated head: {evaluated}\n- evaluated base: main\n\n"
        "## Blocking\n" + "".join(f"- {b}\n" for b in blocking or ["none"]) + "\n## Issues (0)\n"
    )
    slug = kw.get("named", body["repo"]).replace("/", "-")  # the daemon names the file as the repo is spelled
    num = kw.get("filed_as", n)  # and as the number is spelled
    paths = [d / f"{slug}-{num}-{head8}.md"]
    paths[0].write_text(md)
    if json_file:
        paths.append(d / f"{slug}-{num}-{head8}.json")
        paths[1].write_text(json.dumps(body))
    for p in paths:
        os.utime(p, (mtime, mtime))


def listed_as(monkeypatch, d, spell, *, reverse=False):
    """List d's files under spell(name), in name order or reversed: a case-sensitive directory holds two
    files that differ only in case, and the filesystems a run may sit on do not all."""
    real = os.scandir

    class Entry:
        def __init__(self, entry):
            self._entry, self.name, self.path = entry, spell(entry.name), entry.path

        def stat(self):
            return self._entry.stat()

    def scandir(path):
        if os.fspath(path) != os.fspath(d):
            return real(path)
        return iter([Entry(e) for e in sorted(real(path), key=lambda e: e.name, reverse=reverse)])

    monkeypatch.setattr(os, "scandir", scandir)


def freshness_sh(prs=((7, HEAD7), (8, HEAD8), (9, HEAD9))) -> FakeSh:
    return FakeSh(
        **{
            "api repos/o/r --jq": json.dumps({"full_name": "o/r", "id": 42, "admin": True}),
            "pr list -R o/r": json.dumps([{"number": n, "headRefOid": h} for n, h in prs]),
        }
    )


def test_verdict_freshness_at_head_stale_never_reviewed_newest_by_mtime_then_round(tmp_path):
    verdict(tmp_path, 7, OLD, head8="00000000", mtime=1000, round_no=3)
    verdict(
        tmp_path,
        7,
        HEAD7,
        head8="77777777",
        mtime=1000,
        round_no=4,
        blocking=["x", "y"],
        asserted=3,
        degraded=True,
    )  # same mtime as round 3: the later round is the newest
    verdict(tmp_path, 8, HEAD8, head8="88888888", mtime=900)
    verdict(tmp_path, 8, OLD, head8="0000aaaa", mtime=950, decision="APPROVE", blocking=[])
    # named as o/r's, its own repo is not o/r
    verdict(tmp_path, 7, HEAD7, head8="7777beef", mtime=2000, repo="o/other", named="o/r")
    inst = VerdictFreshness(
        fresh_store().notes("agent"), every=None, verdict_dir=str(tmp_path), repos=["o/r"], run=freshness_sh()
    )
    out = asyncio.run(inst.run({}))
    assert out.control and out.count == 3
    assert out.findings == (
        f"o/r#7 77777777: AT-HEAD, REQUEST-CHANGES, blocking 2 (asserted_blocking 3 != 2 listed), DEGRADED; "
        f"read json: {tmp_path}/o-r-7-77777777.json",
        f"o/r#8 88888888: STALE (verdict at 00000000), APPROVE, blocking 0; "
        f"read json: {tmp_path}/o-r-8-0000aaaa.json",
        "o/r#9 99999999: NEVER-REVIEWED",
    )
    assert out.positive.startswith(f"{tmp_path}: newest verdict o-r-7-7777beef.json parses")
    assert out.predicate == "newest verdict not at the PR's current head, or none (2 match)"
    assert [(r["number"], r["state"]) for r in out.data] == [
        (7, "AT-HEAD"),
        (8, "STALE"),
        (9, "NEVER-REVIEWED"),
    ]


def test_verdict_freshness_first_sight_then_transitions(tmp_path):
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    sh = freshness_sh(prs=((7, HEAD7),))
    inst = VerdictFreshness(
        fresh_store().notes("agent"), every=None, verdict_dir=str(tmp_path), repos=["o/r"], run=sh
    )
    assert len(asyncio.run(inst.run({})).findings) == 1
    assert asyncio.run(inst.run({})).findings == ()  # nothing moved: quiet
    sh.set(
        "pr list -R o/r",
        json.dumps([{"number": 7, "headRefOid": "b" * 40}, {"number": 9, "headRefOid": HEAD9}]),
    )
    out = asyncio.run(inst.run({}))
    assert out.findings == (
        f"o/r#7 bbbbbbbb: STALE (verdict at 77777777), REQUEST-CHANGES, blocking 1; read json: "
        f"{tmp_path}/o-r-7-77777777.json",
        "o/r#9 99999999: NEVER-REVIEWED",
    )
    sh.set("pr list -R o/r", json.dumps([{"number": 9, "headRefOid": HEAD9}]))
    assert asyncio.run(inst.run({})).findings == ("o/r#7: no longer open",)


def test_verdict_freshness_a_pr_absent_from_a_full_page_keeps_its_last_row_until_a_short_page(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(instr, "PR_PAGE", 2)
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    sh = freshness_sh(prs=((7, HEAD7), (8, HEAD8)))
    inst = VerdictFreshness(
        fresh_store().notes("agent"), every=None, verdict_dir=str(tmp_path), repos=["o/r"], run=sh
    )
    asyncio.run(inst.run({}))
    kept = inst.state.get("verdict-freshness").value["o/r#7"]
    sh.set(
        "pr list -R o/r",
        json.dumps([{"number": 8, "headRefOid": HEAD8}, {"number": 9, "headRefOid": HEAD9}]),
    )  # #7 fell off a full page
    told = []
    for _ in range(2):  # twice: a kept row is neither closed nor told again
        out = asyncio.run(inst.run({}))
        assert out.control and inst.state.get("verdict-freshness").value["o/r#7"] == kept
        assert kept not in out.evidence and [d["number"] for d in out.data] == [8, 9]  # not a fresh read
        told.append(out.findings)
    assert told == [("o/r#9 99999999: NEVER-REVIEWED",), ()]
    sh.set("pr list -R o/r", json.dumps([{"number": 9, "headRefOid": HEAD9}]))  # a short page is the list
    out = asyncio.run(inst.run({}))
    assert out.control and out.findings == ("o/r#7: no longer open", "o/r#8: no longer open")
    assert set(inst.state.get("verdict-freshness").value) == {"o/r#9"}


def test_verdict_freshness_a_full_page_keeps_a_row_the_last_snapshot_spelled_another_way(
    monkeypatch, tmp_path
):
    """The repo is configured as `o/r`; an older snapshot holds `O/R#7`. A full page without #7 keeps
    that row, as it keeps one spelled as configured."""
    monkeypatch.setattr(instr, "PR_PAGE", 2)
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    store = fresh_store().notes("agent")
    kept = "O/R#7 77777777: AT-HEAD, REQUEST-CHANGES, blocking 1; read json: x"
    store.put("verdict-freshness", {"O/R#7": kept}, run="-", seq=0)
    sh = freshness_sh(prs=((8, HEAD8), (9, HEAD9)))
    inst = VerdictFreshness(store, every=None, verdict_dir=str(tmp_path), repos=["o/r"], run=sh)
    out = asyncio.run(inst.run({}))
    assert out.control and store.get("verdict-freshness").value["O/R#7"] == kept
    assert not any("no longer open" in f for f in out.findings)


def test_verdict_freshness_an_empty_verdict_dir_fails_the_instrument_and_judges_no_pr(tmp_path):
    inst = VerdictFreshness(
        fresh_store().notes("agent"), every=None, verdict_dir=str(tmp_path), repos=["o/r"], run=freshness_sh()
    )
    out = asyncio.run(inst.run({}))
    assert not out.control and out.findings == (f"{tmp_path}: no verdict JSON in 0 entries; no PR is judged",)
    assert out.data == () and not any("NEVER-REVIEWED" in f for f in out.findings)
    out = asyncio.run(
        VerdictFreshness(
            fresh_store().notes("agent"),
            every=None,
            verdict_dir=str(tmp_path / "gone"),
            repos=["o/r"],
            run=freshness_sh(),
        ).run({})
    )
    assert not out.control and out.findings[0].startswith(f"{tmp_path}/gone: unreadable")


def test_verdict_freshness_a_newest_json_that_does_not_parse_fails_the_control(tmp_path):
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    (tmp_path / "o-r-7-12345678.json").write_text("{not json")
    inst = VerdictFreshness(
        fresh_store().notes("agent"), every=None, verdict_dir=str(tmp_path), repos=["o/r"], run=freshness_sh()
    )
    out = asyncio.run(inst.run({}))
    assert not out.control and out.findings == (
        f"{tmp_path}: its newest JSON o-r-7-12345678.json does not parse; no PR is judged",
    )


@pytest.mark.parametrize(
    "head",
    ["12345678", "12345678" + "a" * 31, "12345678" + "a" * 33, "12345678" + "g" * 32, int("1" * 40)],
)
def test_verdict_freshness_a_json_whose_evaluated_head_is_not_a_full_sha_does_not_parse(tmp_path, head):
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    body = {"repo": "o/r", "number": 7, "evaluated_head": head, "decision": "APPROVE", "blocking": []}
    (tmp_path / "o-r-7-12345678.json").write_text(json.dumps(body))
    rec = VerdictFreshness._read({"json": (str(tmp_path / "o-r-7-12345678.json"), 1.0)})
    assert rec["error"] == f"ValueError: evaluated_head {str(head)[:48]!r} is not a full sha"


def test_verdict_freshness_reads_the_md_header_when_the_json_is_missing_and_says_so(tmp_path):
    verdict(tmp_path, 8, HEAD8, head8="12121212", mtime=900)  # the control's known-positive
    verdict(
        tmp_path, 7, HEAD7, head8="77777777", mtime=1000, json_file=False, blocking=["x", "y"], degraded=True
    )
    inst = VerdictFreshness(
        fresh_store().notes("agent"),
        every=None,
        verdict_dir=str(tmp_path),
        repos=["o/r"],
        run=freshness_sh(prs=((7, HEAD7),)),
    )
    out = asyncio.run(inst.run({}))
    assert out.control and out.findings == (
        f"o/r#7 77777777: AT-HEAD, REQUEST-CHANGES, blocking 2, DEGRADED; read md header (no JSON): "
        f"{tmp_path}/o-r-7-77777777.md",
    )


def test_verdict_freshness_a_directory_of_md_only_verdicts_is_read_through_the_md_header(tmp_path):
    """The directory control reads an .md-only verdict through its header too, so the .md fallback the
    per-PR read promises is reachable without another PR's JSON."""
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000, json_file=False)
    inst = VerdictFreshness(
        fresh_store().notes("agent"),
        every=None,
        verdict_dir=str(tmp_path),
        repos=["o/r"],
        run=freshness_sh(prs=((7, HEAD7),)),
    )
    out = asyncio.run(inst.run({}))
    assert out.control and out.findings == (
        f"o/r#7 77777777: AT-HEAD, REQUEST-CHANGES, blocking 1; read md header (no JSON): "
        f"{tmp_path}/o-r-7-77777777.md",
    )
    assert out.positive.startswith(f"{tmp_path}: newest verdict o-r-7-77777777.md parses (read md header)")


def test_verdict_freshness_a_prs_malformed_json_is_never_hidden_behind_its_md_copy(tmp_path):
    """The .md header stands in only for a missing JSON: the world's newest verdict parses, this PR's
    JSON does not, and its healthy .md does not stand in for it."""
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    (tmp_path / "o-r-7-77777777.json").write_text("{incomplete json")
    os.utime(tmp_path / "o-r-7-77777777.json", (1000, 1000))
    verdict(tmp_path, 8, OLD, head8="88888888", mtime=2000)
    inst = VerdictFreshness(
        fresh_store().notes("agent"),
        every=None,
        verdict_dir=str(tmp_path),
        repos=["o/r"],
        run=freshness_sh(prs=((7, HEAD7),)),
    )
    out = asyncio.run(inst.run({}))
    assert not out.control and [d["state"] for d in out.data] == ["UNREADABLE"]
    assert out.findings[0].startswith(
        f"o/r#7 77777777: newest verdict unreadable: {tmp_path}/o-r-7-77777777.json"
    )


@pytest.mark.parametrize("newer", [True, False])
def test_verdict_freshness_an_unreadable_verdict_is_a_candidate_that_fails_the_control_when_newest(
    tmp_path, newer
):
    """A file that cannot be read cannot name its PR, so it stays a candidate by its file name: when it is
    the PR's newest, the run reports it unreadable rather than reading the older verdict beside it."""
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    bad = tmp_path / "o-r-7-12345678.json"
    bad.write_text("{not json")
    os.utime(bad, (1500, 1500) if newer else (500, 500))
    verdict(tmp_path, 8, OLD, head8="88888888", mtime=2000)  # the directory's newest parses
    inst = VerdictFreshness(
        fresh_store().notes("agent"),
        every=None,
        verdict_dir=str(tmp_path),
        repos=["o/r"],
        run=freshness_sh(prs=((7, HEAD7),)),
    )
    out = asyncio.run(inst.run({}))
    if newer:
        assert not out.control and [d["state"] for d in out.data] == ["UNREADABLE"]
        assert out.findings[0].startswith(
            f"o/r#7 77777777: newest verdict unreadable: {bad}: JSONDecodeError"
        )
    else:
        assert out.control and out.findings == (
            f"o/r#7 77777777: AT-HEAD, REQUEST-CHANGES, blocking 1; read json: "
            f"{tmp_path}/o-r-7-77777777.json",
        )


@pytest.mark.parametrize("kind", ["directory", "link to a directory", "link to a file"])
def test_verdict_freshness_a_candidate_is_a_regular_file_and_a_link_is_read_as_its_target(tmp_path, kind):
    """A directory named like #7's newest verdict is no verdict, and neither is a link to one: the older
    verdict beside it is #7's. A link to a regular file is that file, as the scheduled-job instrument reads
    an artifact through a link."""
    d, elsewhere = tmp_path / "verdicts", tmp_path / "elsewhere"
    d.mkdir()
    elsewhere.mkdir()
    verdict(d, 7, OLD, head8="00000000", mtime=1000)
    verdict(d, 8, OLD, head8="88888888", mtime=3000)  # the directory's newest parses
    named = d / "o-r-7-77777777.json"
    if kind == "directory":
        named.mkdir()
    elif kind == "link to a directory":
        named.symlink_to(elsewhere)
    else:
        verdict(elsewhere, 7, HEAD7, head8="77777777", mtime=2000)
        named.symlink_to(elsewhere / "o-r-7-77777777.json")
    os.utime(named, (2000, 2000))  # through a link, its target's: #7's newest by mtime
    inst = VerdictFreshness(
        fresh_store().notes("agent"),
        every=None,
        verdict_dir=str(d),
        repos=["o/r"],
        run=freshness_sh(prs=((7, HEAD7),)),
    )
    out = asyncio.run(inst.run({}))
    assert out.control and [r["state"] for r in out.data] == [
        "AT-HEAD" if kind == "link to a file" else "STALE"
    ]
    if kind == "link to a file":
        assert out.findings == (f"o/r#7 77777777: AT-HEAD, REQUEST-CHANGES, blocking 1; read json: {named}",)
    else:
        assert out.findings == (
            f"o/r#7 77777777: STALE (verdict at 00000000), REQUEST-CHANGES, blocking 1; read json: "
            f"{d}/o-r-7-00000000.json",
        )


def test_verdict_freshness_an_enumerator_without_a_total_line_failed(tmp_path):
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    sh = FakeSh(**{"/x/my_open_prs.sh": (2, "", "ENUMERATION FAILED rc=1: HTTP 403 rate limit\n")})
    inst = VerdictFreshness(
        fresh_store().notes("agent"),
        every=None,
        verdict_dir=str(tmp_path),
        enumerator=["/x/my_open_prs.sh"],
        run=sh,
    )
    out = asyncio.run(inst.run({}))
    assert not out.control and out.count is None and out.data == ()
    assert out.findings == (
        "ENUMERATION FAILED (rc 2, no TOTAL line): ENUMERATION FAILED rc=1: HTTP 403 rate limit",
    )
    sh.set("/x/my_open_prs.sh", "o/r#7 draft=false  a title\n")  # rc 0, the list, and no TOTAL: not 1 PR
    out = asyncio.run(inst.run({}))
    assert not out.control and out.findings[0].startswith("ENUMERATION FAILED (rc 0, no TOTAL line)")


def test_verdict_freshness_an_enumerator_whose_closed_pr_control_is_zero_is_untrusted(tmp_path):
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    total = (
        "TOTAL 0 open PRs authored by bot (control: closed-PR search returned 0, so an empty list is real)\n"
    )
    sh = FakeSh(**{"/x/my_open_prs.sh": total})
    inst = VerdictFreshness(
        fresh_store().notes("agent"),
        every=None,
        verdict_dir=str(tmp_path),
        enumerator=["/x/my_open_prs.sh"],
        run=sh,
    )
    out = asyncio.run(inst.run({}))
    assert not out.control and out.findings == (
        "ENUMERATION UNTRUSTED: the closed-PR control returned 0; the open list is UNREAD, not empty",
    )


@pytest.mark.parametrize("ending", ["final TOTAL with k=0", "trailing failure line"])
def test_verdict_freshness_only_the_enumerators_last_line_is_its_total(tmp_path, ending):
    """Only the last line is the enumerator's verdict: an earlier good TOTAL line does not validate a
    failed enumeration, so the open PRs in the snapshot are not reported no longer open."""
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    store = fresh_store().notes("agent")
    store.put("verdict-freshness", {"o/r#7": "previous open row"}, run="-", seq=0)
    total = (
        "TOTAL 0 open PRs authored by bot (control: closed-PR search returned {k}, an empty list is real)\n"
    )
    last = {
        "final TOTAL with k=0": total.format(k=0),
        "trailing failure line": "ENUMERATION FAILED rc=1: HTTP 403 rate limit\n",
    }
    sh = FakeSh(**{"/x/my_open_prs.sh": total.format(k=5) + last[ending]})
    inst = VerdictFreshness(
        store, every=None, verdict_dir=str(tmp_path), enumerator=["/x/my_open_prs.sh"], run=sh
    )
    out = asyncio.run(inst.run({}))
    said = {
        "final TOTAL with k=0": "ENUMERATION UNTRUSTED: the closed-PR control returned 0; the open list is "
        "UNREAD, not empty",
        "trailing failure line": "ENUMERATION FAILED (rc 0, its last line is not the TOTAL line): "
        "ENUMERATION FAILED rc=1: HTTP 403 rate limit",
    }
    assert not out.control and out.findings == (said[ending],)
    assert store.get("verdict-freshness").value == {"o/r#7": "previous open row"}  # the snapshot untouched


def test_verdict_freshness_reads_each_enumerated_prs_head_on_its_own(tmp_path):
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    out_ = (
        "o/r#7 draft=false  a title\n"
        "TOTAL 1 open PRs authored by bot (control: closed-PR search returned 5, so an empty list is real)\n"
    )
    sh = FakeSh(
        **{"/x/my_open_prs.sh": out_, "pr view 7 -R o/r": json.dumps({"headRefOid": HEAD7, "state": "OPEN"})}
    )
    inst = VerdictFreshness(
        fresh_store().notes("agent"),
        every=None,
        verdict_dir=str(tmp_path),
        enumerator=["/x/my_open_prs.sh"],
        run=sh,
    )
    out = asyncio.run(inst.run({}))
    assert out.control and out.findings[0].startswith("o/r#7 77777777: AT-HEAD")
    assert "enumerator TOTAL 1 open PR(s) by bot; closed-PR control 5" in out.positive
    assert sh.calls[0] == ["/x/my_open_prs.sh"]  # run as the chores.toml names it, nothing added


def test_verdict_freshness_an_enumerator_total_beside_fewer_prs_retires_only_a_pr_read_closed(tmp_path):
    """A TOTAL the listed PRs do not reach is an incomplete list: a prior row it lacks is kept, a PR it
    lists is read and merged, and a PR read MERGED retires its own row."""
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    store = fresh_store().notes("agent")
    prior = {"o/r#8": "o/r#8 88888888: NEVER-REVIEWED", "o/r#9": "o/r#9 99999999: NEVER-REVIEWED"}
    store.put("verdict-freshness", prior, run="-", seq=0)
    total = (
        "TOTAL {n} open PRs authored by bot (control: closed-PR search returned 5, an empty list is real)\n"
    )
    listed = "o/r#7 draft=false  a title\no/r#9 draft=false  merged\n"
    sh = FakeSh(
        **{
            "/x/my_open_prs.sh": listed + total.format(n=3),
            "pr view 7 -R o/r": json.dumps({"headRefOid": HEAD7, "state": "OPEN"}),
            "pr view 9 -R o/r": json.dumps({"headRefOid": HEAD9, "state": "MERGED"}),
        }
    )
    inst = VerdictFreshness(
        store, every=None, verdict_dir=str(tmp_path), enumerator=["/x/my_open_prs.sh"], run=sh
    )
    told = []
    for _ in range(2):  # twice: the kept row is neither closed nor told again
        out = asyncio.run(inst.run({}))
        snap = store.get("verdict-freshness").value
        assert not out.control and snap["o/r#8"] == prior["o/r#8"] and "o/r#9" not in snap
        assert snap["o/r#7"].startswith("o/r#7 77777777: AT-HEAD")
        told.append(out.findings)
    assert told == [
        (
            "enumerator: TOTAL 3 beside 2 PR line(s) parsed",
            f"o/r#7 77777777: AT-HEAD, REQUEST-CHANGES, blocking 1; read json: "
            f"{tmp_path}/o-r-7-77777777.json",
            "o/r#9: no longer open",
        ),
        ("enumerator: TOTAL 3 beside 2 PR line(s) parsed",),
    ]
    sh.set("/x/my_open_prs.sh", "o/r#7 draft=false  a title\n" + total.format(n=1))  # the count agrees
    out = asyncio.run(inst.run({}))
    assert out.control and out.findings == ("o/r#8: no longer open",)
    assert set(store.get("verdict-freshness").value) == {"o/r#7"}


@pytest.mark.parametrize("again", ["o/r#7", "o/r#07", "O/R#7"])
def test_verdict_freshness_a_pr_listed_twice_beside_an_equal_total_closes_no_unseen_pr(tmp_path, again):
    """Lines that reach the TOTAL by listing one PR twice, however its repo or number is spelled, are an
    incomplete list: the PR they omit keeps its row, and the PR they repeat is read once."""
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    store = fresh_store().notes("agent")
    prior = {"o/r#8": "o/r#8 88888888: NEVER-REVIEWED"}
    store.put("verdict-freshness", prior, run="-", seq=0)
    total = (
        "TOTAL {n} open PRs authored by bot (control: closed-PR search returned 5, an empty list is real)\n"
    )
    sh = FakeSh(
        **{
            "/x/my_open_prs.sh": f"o/r#7 draft=false  a title\n{again} draft=false  a title\n"
            + total.format(n=2),
            "pr view 7 -R o/r": json.dumps({"headRefOid": HEAD7, "state": "OPEN"}),
        }
    )
    inst = VerdictFreshness(
        store, every=None, verdict_dir=str(tmp_path), enumerator=["/x/my_open_prs.sh"], run=sh
    )
    told = []
    for _ in range(2):  # twice: the kept row is neither closed nor told again
        out = asyncio.run(inst.run({}))
        assert not out.control and store.get("verdict-freshness").value["o/r#8"] == prior["o/r#8"]
        assert out.count == 1 and [d["number"] for d in out.data] == [7]
        told.append(out.findings)
    assert told == [
        (
            "enumerator: TOTAL 2 beside 2 PR line(s) parsed, 1 distinct",
            f"o/r#7 77777777: AT-HEAD, REQUEST-CHANGES, blocking 1; read json: "
            f"{tmp_path}/o-r-7-77777777.json",
        ),
        ("enumerator: TOTAL 2 beside 2 PR line(s) parsed, 1 distinct",),
    ]
    assert [c[2:6] for c in sh.calls if "view" in c] == [["view", "7", "-R", "o/r"]] * 2  # once a run
    sh.set("/x/my_open_prs.sh", "o/r#7 draft=false  a title\n" + total.format(n=1))  # a complete list
    out = asyncio.run(inst.run({}))
    assert out.control and out.findings == ("o/r#8: no longer open",)
    assert set(store.get("verdict-freshness").value) == {"o/r#7"}


@pytest.mark.parametrize("listed", ["O/R#07", "o/r#007"])
def test_verdict_freshness_a_prs_own_read_meets_its_row_however_the_enumerator_spells_it(tmp_path, listed):
    """A PR's row is matched by its repo case-blind and its number unpadded: a closed read retires it
    from an incomplete list, an unread head keeps it, and an open read keeps its key."""
    total = (
        "TOTAL {n} open PRs authored by bot (control: closed-PR search returned 5, an empty list is real)\n"
    )
    shown = listed.split("#")[1]
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000, repo="O/R")  # the file spelled another way

    def inst_with(prior, lines, n, view):
        store = fresh_store().notes("agent")
        store.put("verdict-freshness", prior, run="-", seq=0)
        sh = FakeSh(**{"/x/my_open_prs.sh": lines + total.format(n=n), f"pr view {shown} -R": view})
        return store, VerdictFreshness(
            store, every=None, verdict_dir=str(tmp_path), enumerator=["/x/my_open_prs.sh"], run=sh
        )

    seven, eight = "o/r#7 77777777: NEVER-REVIEWED", "o/r#8 88888888: NEVER-REVIEWED"
    # closed, in a list that repeats it: its own read retires its row; the unseen #8 keeps its row
    store, inst = inst_with(
        {"o/r#7": seven, "o/r#8": eight},
        f"{listed} draft=false  a title\no/r#7 draft=false  a title\n",
        2,
        json.dumps({"headRefOid": HEAD7, "state": "CLOSED"}),
    )
    out = asyncio.run(inst.run({}))
    assert store.get("verdict-freshness").value == {"o/r#8": eight}
    assert "o/r#7: no longer open" in out.findings and not out.control
    # its head not read, in a complete list: the row is kept and nothing says it closed
    store, inst = inst_with({"o/r#7": seven}, f"{listed} draft=false  a title\n", 1, (1, "", "boom"))
    out = asyncio.run(inst.run({}))
    assert store.get("verdict-freshness").value == {"o/r#7": seven}
    assert not any("no longer open" in f for f in out.findings) and not out.control
    # open and read: the row keeps its key, so the PR is neither closed nor new
    store, inst = inst_with(
        {"o/r#7": seven},
        f"{listed} draft=false  a title\n",
        1,
        json.dumps({"headRefOid": HEAD7, "state": "OPEN"}),
    )
    out = asyncio.run(inst.run({}))
    assert set(store.get("verdict-freshness").value) == {"o/r#7"}
    assert not any("no longer open" in f for f in out.findings) and out.control
    # and its verdict is found however the PR is spelled, in the file name and in the verdict's own say
    assert store.get("verdict-freshness").value["o/r#7"].startswith("o/r#7 77777777: AT-HEAD")


def test_verdict_freshness_two_rows_for_one_pr_become_one_without_a_closure(tmp_path):
    """An older snapshot that held a PR under two spellings keeps one row; the other is retired
    silently, since the PR is still open."""
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    store = fresh_store().notes("agent")
    seven = "o/r#7 77777777: NEVER-REVIEWED"
    store.put(
        "verdict-freshness", {"o/r#7": seven, "O/R#07": seven.replace("o/r#7", "O/R#07")}, run="-", seq=0
    )
    total = "TOTAL 1 open PRs authored by bot (control: closed-PR search returned 5, an empty list is real)\n"
    sh = FakeSh(
        **{
            "/x/my_open_prs.sh": "o/r#7 draft=false  a title\n" + total,
            "pr view 7 -R": json.dumps({"headRefOid": HEAD7, "state": "OPEN"}),
        }
    )
    inst = VerdictFreshness(
        store, every=None, verdict_dir=str(tmp_path), enumerator=["/x/my_open_prs.sh"], run=sh
    )
    out = asyncio.run(inst.run({}))
    rows = store.get("verdict-freshness").value
    assert len(rows) == 1 and " 77777777: AT-HEAD, " in next(iter(rows.values()))
    assert not any("no longer open" in f for f in out.findings) and out.control


@pytest.mark.parametrize("reverse", [False, True])
def test_verdict_freshness_two_verdicts_that_differ_only_in_the_repos_case_are_both_candidates(
    monkeypatch, tmp_path, reverse
):
    """One PR's verdicts can be filed as `o-r-7-<head8>` and `O-R-7-<head8>`: the newer is the verdict
    however the directory lists them, so an older approval filed as the lookup spells the repo never hides
    a newer request for changes filed in another case."""
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000, decision="APPROVE", blocking=[], named="old/r")
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=2000, named="new/r")
    listed_as(
        monkeypatch,
        tmp_path,
        lambda name: name.replace("old-r", "o-r").replace("new-r", "O-R"),
        reverse=reverse,
    )
    sh = freshness_sh(prs=((7, HEAD7),))
    inst = VerdictFreshness(
        fresh_store().notes("agent"), every=None, verdict_dir=str(tmp_path), repos=["o/r"], run=sh
    )
    out = asyncio.run(inst.run({}))
    assert out.control and out.findings == (
        f"o/r#7 77777777: AT-HEAD, REQUEST-CHANGES, blocking 1; read json: {tmp_path}/new-r-7-77777777.json",
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_verdict_freshness_verdicts_of_one_mtime_and_round_are_chosen_by_path_not_by_listing_order(
    monkeypatch, tmp_path, reverse
):
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000, decision="APPROVE", blocking=[], named="old/r")
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000, named="new/r")
    listed_as(
        monkeypatch,
        tmp_path,
        lambda name: name.replace("old-r", "O-R").replace("new-r", "o-r"),
        reverse=reverse,
    )
    sh = freshness_sh(prs=((7, HEAD7),))
    inst = VerdictFreshness(
        fresh_store().notes("agent"), every=None, verdict_dir=str(tmp_path), repos=["o/r"], run=sh
    )
    out = asyncio.run(inst.run({}))
    assert out.control and out.findings == (
        f"o/r#7 77777777: AT-HEAD, APPROVE, blocking 0; read json: {tmp_path}/old-r-7-77777777.json",
    )


@pytest.mark.parametrize("filed_as", ["007", "07"])
def test_verdict_freshness_a_verdict_is_found_whether_its_file_pads_the_number(tmp_path, filed_as):
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000, filed_as=filed_as)
    sh = freshness_sh(prs=((7, HEAD7),))
    inst = VerdictFreshness(
        fresh_store().notes("agent"), every=None, verdict_dir=str(tmp_path), repos=["o/r"], run=sh
    )
    out = asyncio.run(inst.run({}))
    assert out.control and out.findings == (
        f"o/r#7 77777777: AT-HEAD, REQUEST-CHANGES, blocking 1; read json: "
        f"{tmp_path}/o-r-{filed_as}-77777777.json",
    )


@pytest.mark.parametrize("first", ["o/r#7", "O/R#07"])
def test_verdict_freshness_of_two_conflicting_rows_for_one_pr_the_one_at_its_head_now_is_the_row(
    tmp_path, first
):
    """An older snapshot's rows for one PR disagree: the one that read the head the PR has now is the
    later observation, whichever the snapshot lists first."""
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    path = f"{tmp_path}/o-r-7-77777777.json"
    rows = {
        "o/r#7": "o/r#7 00000000: STALE (verdict at 00000000), APPROVE, blocking 0; read json: x",
        "O/R#07": f"O/R#07 77777777: AT-HEAD, REQUEST-CHANGES, blocking 1; read json: {path}",
    }
    store = fresh_store().notes("agent")
    snapshot = {k: rows[k] for k in sorted(rows, key=lambda k: k != first)}  # `first` is listed first
    store.put("verdict-freshness", snapshot, run="-", seq=0)
    sh = freshness_sh(prs=((7, HEAD7),))
    inst = VerdictFreshness(store, every=None, verdict_dir=str(tmp_path), repos=["o/r"], run=sh)
    out = asyncio.run(inst.run({}))
    assert store.get("verdict-freshness").value == {"O/R#07": rows["O/R#07"]}
    assert out.control and out.findings == ()  # the row it was told is the row it reads again


@pytest.mark.parametrize("flip", [False, True])
@pytest.mark.parametrize(
    ("aliases", "winner"),
    [(("O/R#07", "o/r#7"), "o/r#7"), (("o/R#07", "O/R#7"), "O/R#7")],
    ids=["the-spelling-read-now", "the-smaller-key"],
)
def test_verdict_freshness_two_rows_at_one_head_keep_the_spelling_read_now_then_the_smaller_key(
    tmp_path, aliases, winner, flip
):
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    store = fresh_store().notes("agent")
    rows = {k: f"{k} 00000000: NEVER-REVIEWED" for k in (reversed(aliases) if flip else aliases)}
    store.put("verdict-freshness", rows, run="-", seq=0)
    sh = freshness_sh(prs=((7, HEAD7),))
    inst = VerdictFreshness(store, every=None, verdict_dir=str(tmp_path), repos=["o/r"], run=sh)
    out = asyncio.run(inst.run({}))
    assert set(store.get("verdict-freshness").value) == {winner}
    assert out.control and not any("no longer open" in f for f in out.findings)


def test_verdict_freshness_a_short_page_meets_a_row_its_snapshot_spelled_in_another_case(tmp_path):
    """The page is short, so nothing retains the row: the PR's own read finds it, its repo case-blind."""
    assert instr.PR_PAGE > 1
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    store = fresh_store().notes("agent")
    store.put("verdict-freshness", {"O/R#7": "O/R#7 77777777: NEVER-REVIEWED"}, run="-", seq=0)
    sh = freshness_sh(prs=((7, HEAD7),))
    inst = VerdictFreshness(store, every=None, verdict_dir=str(tmp_path), repos=["o/r"], run=sh)
    out = asyncio.run(inst.run({}))
    rows = store.get("verdict-freshness").value
    assert out.control and set(rows) == {"O/R#7"} and rows["O/R#7"].startswith("O/R#7 77777777: AT-HEAD")
    assert not any("no longer open" in f for f in out.findings)


def test_verdict_freshness_control_fails_when_a_repo_does_not_resolve_as_itself(tmp_path):
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    sh = FakeSh(**{"api repos/o/r --jq": json.dumps({"full_name": "o/elsewhere", "id": 1, "admin": True})})
    inst = VerdictFreshness(
        fresh_store().notes("agent"), every=None, verdict_dir=str(tmp_path), repos=["o/r"], run=sh
    )
    out = asyncio.run(inst.run({}))
    assert not out.control and out.findings[0].startswith("o/r: gh does not resolve it as itself")
    with pytest.raises(ValueError, match="exactly one of them"):
        VerdictFreshness(fresh_store().notes("agent"), every=None, repos=["o/r"], enumerator=["x"])


class LateList(FakeSh):
    """A runner whose first call to `gated` takes its answer when it is made and delivers it only once
    released: a list that is complete when read and old by the time its run goes on."""

    def __init__(self, gated: str, **answers: tuple[int, str, str] | str):
        super().__init__(**answers)
        self.gated, self.began, self.release = gated, asyncio.Event(), asyncio.Event()

    async def __call__(self, argv: list[str], *, timeout: float = 60, cwd: str | None = None):
        answer = await super().__call__(argv, timeout=timeout, cwd=cwd)
        if self.gated in " ".join(argv) and not self.began.is_set():
            self.began.set()
            await self.release.wait()
        return answer


@pytest.mark.parametrize("channel", ["repos", "enumerator"])
def test_a_complete_but_older_list_does_not_retire_a_pr_a_later_overlapping_run_listed(tmp_path, channel):
    """Run A takes its list before #9 opens and waits; run B starts and lists #9. Left to overlap, B writes
    first and A, finding #9 absent from its complete list, reports it closed and drops it. The runs of one
    instrument go one at a time, so B runs on the snapshot A put: #9 is B's first sight, never A's closure."""
    verdict(tmp_path, 7, HEAD7, head8="77777777", mtime=1000)
    heads = {7: HEAD7, 8: HEAD8, 9: HEAD9}
    total = (
        "TOTAL {n} open PRs authored by bot (control: closed-PR search returned 5, an empty list is real)\n"
    )

    def listing(prs):
        if channel == "repos":
            return json.dumps([{"number": n, "headRefOid": heads[n]} for n in prs])
        return "".join(f"o/r#{n} draft=false  a title\n" for n in prs) + total.format(n=len(prs))

    key = "pr list -R o/r" if channel == "repos" else "/x/my_open_prs.sh"
    answers = {key: listing((7, 8))}
    if channel == "repos":
        answers["api repos/o/r --jq"] = json.dumps({"full_name": "o/r", "id": 42, "admin": True})
        where = {"repos": ["o/r"]}
    else:
        answers |= {
            f"pr view {n} -R o/r": json.dumps({"headRefOid": h, "state": "OPEN"}) for n, h in heads.items()
        }
        where = {"enumerator": ["/x/my_open_prs.sh"]}
    store = fresh_store().notes("agent")

    async def both():
        sh = LateList(key, **answers)
        inst = VerdictFreshness(store, every=None, verdict_dir=str(tmp_path), run=sh, **where)
        first = asyncio.create_task(inst.run({}))
        await sh.began.wait()  # A holds the list it took
        sh.set(key, listing((7, 8, 9)))  # #9 opens
        second = asyncio.create_task(inst.run({}))
        await asyncio.wait({second}, timeout=0)  # B goes as far as nothing holds it: to its end, unserialized
        sh.release.set()
        return await first, await second

    out_a, out_b = asyncio.run(both())
    assert [f for o in (out_a, out_b) for f in o.findings if "no longer open" in f] == []
    assert set(store.get("verdict-freshness").value) == {"o/r#7", "o/r#8", "o/r#9"}
    assert out_a.control and out_b.control
    assert len(out_b.findings) == 1 and out_b.findings[0].startswith("o/r#9 99999999: NEVER-REVIEWED")


@pytest.mark.parametrize("which", ["pr-state", "worktrees", "required-contexts", "ci-red"])
def test_the_runs_of_one_snapshot_instrument_go_one_at_a_time(tmp_path, which):
    """As for `verdict-freshness`: run A takes its list before #9 opens and waits; run B starts and lists
    #9. Left to overlap, B writes first and A, seeing #9 absent from its complete list, reports it gone and
    drops it. The runs of one instrument go one at a time, so #9 is B's first sight, never A's closure."""
    store = fresh_store().notes("agent")
    if which == "worktrees":
        key, gone, first = "worktree list --porcelain", "worktree gone", f"{tmp_path}/wt9: new worktree"
        snapshot, rows = f"worktrees:{tmp_path}/main", {f"{tmp_path}/wt{n}" for n in (7, 8, 9)}

        def listing(ns):
            trees = ("main", *(f"wt{n}" for n in ns))
            return "".join(f"worktree {tmp_path}/{t}\0HEAD abc\0branch refs/heads/{t}\0\0" for t in trees)

        answers = {
            "remote get-url origin": "https://github.com/o/r.git\n",
            "status --porcelain": "",
            "log -1": f"{int(time.time())}\n",
            "pr list -R o/r --head": "[]",
        }

        def build(sh):
            return Worktrees([f"{tmp_path}/main"], store, every=None, run=sh)

    else:
        key, snapshot, rows = "pr list -R o/r", f"{which}:o/r", {"7", "8", "9"}
        gone, first = (
            ("closed or merged", "o/r#9: first sight")
            if which == "pr-state"
            else ("no longer open", "o/r#9 ")
        )
        row = {
            "pr-state": lambda n: {**PR, "number": n, "title": f"pr {n}"},
            "required-contexts": lambda n: {"number": n, "headRefOid": SHA, "baseRefName": "main"},
            "ci-red": lambda n: {"number": n, "headRefOid": SHA},
        }[which]

        def listing(ns):
            return json.dumps([row(n) for n in ns])

        answers = {
            "pr-state": lambda: {"api repos/o/r --jq": "o/r\n", "actions/runs?head_sha=": RUNS},
            "required-contexts": lambda: required_sh().answers,
            "ci-red": lambda: ci_sh().answers,
        }[which]()

        def build(sh):
            if which == "pr-state":
                return PrState(["o/r"], store, every=None, run=sh)
            return required(sh, state=store) if which == "required-contexts" else ci_red(sh, state=store)

    async def both():
        sh = LateList(key, **{**answers, key: listing((7, 8))})
        inst = build(sh)
        first_run = asyncio.create_task(inst.run({}))
        await asyncio.wait_for(sh.began.wait(), 5)  # A holds the list it took
        sh.set(key, listing((7, 8, 9)))  # #9 opens
        second = asyncio.create_task(inst.run({}))
        await asyncio.wait({second}, timeout=0)  # B goes as far as nothing holds it: to its end, unserialized
        sh.release.set()
        return await first_run, await second

    out_a, out_b = asyncio.run(both())
    assert [f for o in (out_a, out_b) for f in o.findings if gone in f] == []
    assert set(store.get(snapshot).value) == rows
    assert out_a.control and out_b.control
    assert out_b.findings and all(f.startswith(first) for f in out_b.findings), out_b.findings


async def overlap(paused, gate: LateList, other):
    """Run `paused` into its gated read, run `other`, an instrument over the same store, to its end, then let
    the read answer."""
    first = asyncio.create_task(paused.run({}))
    await asyncio.wait_for(gate.began.wait(), 5)
    out_other = await other.run({})
    gate.release.set()
    return await first, out_other


@pytest.mark.parametrize("moved", ["a PR closed", "a PR opened"])
def test_pr_state_a_retaining_run_merges_the_snapshot_a_concurrent_run_wrote(monkeypatch, moved):
    """Run A begins on a snapshot of #7 and #8, finds #7 absent from its full page and waits on its own read
    of #7; run B reads #7 closed (or open, beside a new #10) and writes. A's read of #7 then fails, so it
    keeps the rows it holds that its page lacks: the snapshot's as it stands then, not the copy it began
    with."""
    monkeypatch.setattr(instr, "PR_PAGE", 2)
    store = fresh_store().notes("agent")

    def answers(*ns):
        return {
            "api repos/o/r --jq": "o/r\n",
            "pr list -R o/r": json.dumps([{**PR, "number": n, "title": f"pr {n}"} for n in ns]),
            "actions/runs?head_sha=": RUNS,
        }

    asyncio.run(PrState(["o/r"], store, every=None, run=FakeSh(**answers(7, 8))).run({}))
    closed = moved == "a PR closed"
    b_sh = FakeSh(
        **answers(8, 9 if closed else 10), **{"pr view 7 -R o/r": "CLOSED\n" if closed else "OPEN\n"}
    )

    async def both():
        gate = LateList("pr view 7 -R o/r", **answers(8, 9))  # no answer for a view: each of A's views fails
        a = PrState(["o/r"], store, every=None, run=gate)
        return await overlap(a, gate, PrState(["o/r"], store, every=None, run=b_sh))

    out_a, out_b = asyncio.run(both())
    assert out_b.control and ("o/r#7: closed or merged" in out_b.findings) is closed
    snap = store.get("pr-state:o/r").value
    assert set(snap) == ({"8", "9"} if closed else {"7", "8", "9", "10"})  # a closed #7 is not written back
    assert "o/r#7: closed or merged" not in out_a.findings


def test_worktrees_a_retaining_run_merges_the_snapshot_a_concurrent_run_wrote(tmp_path):
    """Run A begins on a snapshot of `wt` and `old` and waits on its first read of `wt`; run B finds `old`
    gone and writes. A's reads of `old` then fail, so it keeps that tree's row: the snapshot's as it stands
    then, which holds no `old`, not the copy A began with."""
    main, wt, old = (f"{tmp_path}/{t}" for t in ("main", "wt", "old"))

    def porcelain(*trees):
        return "".join(f"worktree {t}\0HEAD abc\0branch refs/heads/{t.rsplit('/', 1)[1]}\0\0" for t in trees)

    read = {
        "remote get-url origin": "https://github.com/o/r.git\n",
        f"-C {wt} status --porcelain": "",
        f"-C {wt} log -1": f"{int(time.time())}\n",
        "pr list -R o/r --head wt": "[]",
    }
    store = fresh_store().notes("agent")
    seed = FakeSh(
        **read, **{"worktree list --porcelain": porcelain(main, wt, old), "status": "", "log -1": "1\n"}
    )
    seed.set("pr list -R o/r --head old", "[]")
    assert asyncio.run(Worktrees([main], store, every=None, run=seed).run({})).control

    async def both():
        gate = LateList(
            f"-C {wt} status --porcelain", **read, **{"worktree list --porcelain": porcelain(main, wt, old)}
        )
        a = Worktrees([main], store, every=None, run=gate)
        b = Worktrees(
            [main],
            store,
            every=None,
            run=FakeSh(**read, **{"worktree list --porcelain": porcelain(main, wt)}),
        )
        return await overlap(a, gate, b)

    out_a, out_b = asyncio.run(both())
    assert out_b.control and out_b.findings == (f"{old}: worktree gone",)
    assert (
        not out_a.control and f"{old}: the status, log, PR read failed; the tree is unread" in out_a.findings
    )
    assert set(store.get(f"worktrees:{main}").value) == {wt}  # a gone tree is not written back


@pytest.mark.parametrize("moved", ["a PR closed", "a PR opened"])
def test_required_contexts_a_retaining_run_merges_the_snapshot_a_concurrent_run_wrote(monkeypatch, moved):
    """Run A lists #7 without its head beside #8, a full page, and waits on #8's head; run B finds #7 closed
    (or opens #9) and writes. A keeps #7's row and the rows its full page lacks: the snapshot's as it stands
    then, not the copy it began with."""
    monkeypatch.setattr(instr, "PR_PAGE", 2)
    store = fresh_store().notes("agent")

    def listing(*prs):
        return json.dumps([{"number": n, "headRefOid": h, "baseRefName": "main"} for n, h in prs])

    asyncio.run(required(required_sh(**{"pr list -R o/r": listing((7, SHA), (8, SHA))}), state=store).run({}))
    now = ((8, SHA),) if moved == "a PR closed" else ((7, SHA), (8, SHA), (9, SHA))

    async def both():
        gate = LateList(
            f"commits/{SHA}/status", **required_sh(**{"pr list -R o/r": listing((7, ""), (8, SHA))}).answers
        )
        b = required(required_sh(**{"pr list -R o/r": listing(*now)}), state=store)
        return await overlap(required(gate, state=store), gate, b)

    out_a, out_b = asyncio.run(both())
    assert out_b.control and not out_a.control  # B read every head; A did not read #7's
    snap = store.get("required-contexts:o/r").value
    assert set(snap) == {str(n) for n, _ in now}  # what B wrote, whole
    assert ("7" in snap) is (moved == "a PR opened")  # a closed #7 is not written back


@pytest.mark.parametrize("moved", ["a PR closed", "a PR opened"])
def test_ci_red_a_retaining_run_merges_the_snapshot_a_concurrent_run_wrote(monkeypatch, moved):
    """Run A lists #7 without its head beside #8, a full page, and waits on #8's runs; run B finds #7 closed
    (or opens #9) and writes. A keeps #7's last red and the rows its full page lacks: the snapshot's as it
    stands then, not the copy it began with."""
    monkeypatch.setattr(instr, "PR_PAGE", 2)
    store = fresh_store().notes("agent")
    asyncio.run(ci_red(ci_sh(prs=((7, SHA), (8, SHA))), state=store).run({}))
    now = ((8, SHA),) if moved == "a PR closed" else ((7, SHA), (8, SHA), (9, SHA))

    async def both():
        gate = LateList(f"actions/runs?head_sha={SHA}", **ci_sh(prs=((7, ""), (8, SHA))).answers)
        return await overlap(ci_red(gate, state=store), gate, ci_red(ci_sh(prs=now), state=store))

    out_a, out_b = asyncio.run(both())
    assert out_b.control and not out_a.control  # B read every head; A did not read #7's
    snap = store.get("ci-red:o/r").value
    assert set(snap) == {str(n) for n, _ in now}  # what B wrote, whole
    assert ("7" in snap) is (moved == "a PR opened")  # a closed #7 is not written back


WORKFLOW_IDS = {"ci": 501, "lint": 502, "docs": 503, "bench": 504}


def ci_runs(*rows: tuple) -> str:
    """The workflow runs at a head: (id, workflow, conclusion, created_at[, workflow_id]); a workflow's
    id is its name's in WORKFLOW_IDS unless given."""
    return runs_page(
        [
            {
                "id": r[0],
                "workflow_id": r[4] if len(r) > 4 else WORKFLOW_IDS[r[1]],
                "name": r[1],
                "event": "pull_request",
                "status": "completed",
                "conclusion": r[2],
                "created_at": r[3],
            }
            for r in rows
        ]
    )


RED_ROWS = (
    (10, "ci", "success", "2026-09-22T09:00:00Z"),
    (11, "ci", "failure", "2026-09-22T10:00:00Z"),
    (12, "lint", "failure", "2026-09-22T10:00:00Z"),
    (13, "docs", "success", "2026-09-22T10:00:00Z"),
    (14, "bench", "timed_out", "2026-09-22T10:00:00Z"),
)
RED_RUNS = ci_runs(*RED_ROWS)
BOOM = "FAILED tests/test_x.py::test_a - AssertionError: boom"
RERUN = "error: test failed, to rerun pass --lib"


def failed_log(job: str, *lines: str) -> str:
    """gh run view --log-failed: <job>\\t<step>\\t<stamp> <text>, the first stamp behind a BOM."""
    return "".join(
        f"{job}\tRun tests\t{chr(0xFEFF) if i == 0 else ''}2026-09-22T10:00:0{i}.1234567Z {line}\n"
        for i, line in enumerate(lines)
    )


def ci_sh(runs: str = RED_RUNS, sha: str = SHA, prs=((7, SHA),), **over) -> FakeSh:
    answers = {
        "api repos/o/r --jq": json.dumps({"full_name": "o/r", "id": 42, "admin": True}),
        "pr list -R o/r": json.dumps([{"number": n, "headRefOid": h} for n, h in prs]),
        f"actions/runs?head_sha={sha}": runs,
        "run view 11 -R o/r --log-failed": failed_log(
            "test (3.12)",
            "Current runner version: '2.337.0'",
            f"\x1b[31m{BOOM}\x1b[0m",
            "thread 'main' panicked at src/lib.rs:3:5:",
            "left != right",
            "note: run with RUST_BACKTRACE=1 to display a backtrace",
            RERUN,
        ),
        "run view 12 -R o/r --log-failed": failed_log(
            "lint", RERUN, BOOM, "E501 Line too long", "Error: Process completed with exit code 1."
        ),
    }
    return FakeSh(**{**answers, **over})


def ci_red(sh: FakeSh, channel: str = "actions", state=None) -> CiRed:
    return CiRed(
        [{"name": "o/r", "channel": channel}], state or fresh_store().notes("agent"), every=None, run=sh
    )


@pytest.mark.parametrize("answer", ["", "{}", "null", '[{"number": ' + "9" * 4301 + "}]"])
@pytest.mark.parametrize("which", ["required-contexts", "verdict-freshness", "ci-red"])
def test_a_pr_list_that_is_not_a_json_list_is_unread_by_every_instrument_reading_through_it(
    tmp_path, which, answer
):
    verdict(tmp_path, 7, SHA, head8=SHA[:8], mtime=1000)
    store = fresh_store().notes(
        "agent"
    )  # one store across the reads: the unread read must keep the last rows
    sh = {"required-contexts": required_sh, "verdict-freshness": freshness_sh, "ci-red": ci_sh}[which]()
    inst = {
        "required-contexts": lambda: required(sh, state=store),
        "verdict-freshness": lambda: VerdictFreshness(
            store, every=None, verdict_dir=str(tmp_path), repos=["o/r"], run=sh
        ),
        "ci-red": lambda: ci_red(sh, state=store),
    }[which]()
    key = {
        "required-contexts": "required-contexts:o/r",
        "verdict-freshness": "verdict-freshness",
        "ci-red": "ci-red:o/r",
    }
    assert asyncio.run(inst.run({})).control  # the control: a real list reads
    kept = store.get(key[which]).value
    assert kept
    sh.set("pr list -R o/r", answer)
    out = asyncio.run(inst.run({}))
    assert not out.control and f"o/r: gh pr list gave no JSON list: {answer[:120]!r}" in out.findings
    assert store.get(key[which]).value == kept  # the last rows stay
    sh.set("pr list -R o/r", "[]")  # a decoded empty list is a value: no pull request to judge
    assert asyncio.run(inst.run({})).control


def test_ci_red_actions_extracts_each_failed_run_strips_ansi_and_stamps_and_counts_the_dedupe():
    sh = ci_sh()
    out = asyncio.run(ci_red(sh).run({}))
    assert out.control and out.count == 1 and out.predicate.endswith("(1 match)")
    assert out.findings == (
        f"o/r#7 aaaaaaaa: ci run 11 red in [test (3.12)]; first: {BOOM}",
        f"o/r#7 aaaaaaaa: lint run 12 red in [lint]; first: {RERUN}",
    )
    (row,) = out.data
    assert set(row) == {"repo", "number", "head", "channel", "runs", "dropped_by_dedupe"}
    assert all(set(r) == {"workflow", "run_id", "job", "lines"} for r in row["runs"])
    assert (row["repo"], row["number"], row["head"], row["channel"]) == ("o/r", 7, SHA, "actions")
    assert row["runs"] == [
        {
            "workflow": "ci",
            "run_id": 11,
            "job": "test (3.12)",
            "lines": [BOOM, "thread 'main' panicked at src/lib.rs:3:5:", "left != right", RERUN],
        },
        {
            "workflow": "lint",
            "run_id": 12,
            "job": "lint",
            "lines": ["Error: Process completed with exit code 1."],
        },
    ]
    assert row["dropped_by_dedupe"] == 2  # lint's copies of the two lines ci already holds
    assert "bench=timed_out (run 14)" in out.evidence and "ci=failure (run 11)" in out.evidence
    assert "ci run 11 job test (3.12): 4 line(s) kept of 6 read" in out.evidence
    assert "lint run 12 job lint: 1 line(s) kept of 4 read" in out.evidence
    assert "2 line(s) dropped by dedupe" in out.evidence
    assert "\x1b" not in out.evidence and "2026-09-22T10:00:0" not in out.evidence
    viewed = [c for c in sh.calls if "view" in c]
    assert [c[3] for c in viewed] == ["11", "12"]  # never the success, never the timed_out run


def test_ci_red_first_sight_then_only_new_red_then_no_red_then_closed():
    store = fresh_store().notes("agent")
    sh = ci_sh()
    first = asyncio.run(ci_red(sh, state=store).run({}))
    assert len(first.findings) == 2
    assert asyncio.run(ci_red(sh, state=store).run({})).findings == ()
    sh.set(  # ci re-ran green; lint is still red and was already reported
        f"actions/runs?head_sha={SHA}", ci_runs(*RED_ROWS, (15, "ci", "success", "2026-09-22T11:00:00Z"))
    )
    out = asyncio.run(ci_red(sh, state=store).run({}))
    assert out.control and out.findings == ()
    sh.set(f"actions/runs?head_sha={SHA}", ci_runs((15, "ci", "success", "2026-09-22T11:00:00Z")))
    out = asyncio.run(ci_red(sh, state=store).run({}))
    assert out.findings == ("o/r#7 aaaaaaaa: no red at this head",) and out.data == ()
    sh.set("pr list -R o/r", json.dumps([{"number": 7, "headRefOid": "b" * 40}]))
    sh.set("actions/runs?head_sha=" + "b" * 40, ci_runs((12, "lint", "failure", "2026-09-22T12:00:00Z")))
    out = asyncio.run(ci_red(sh, state=store).run({}))
    assert out.findings == (f"o/r#7 bbbbbbbb: lint run 12 red in [lint]; first: {RERUN}",)
    sh.set("pr list -R o/r", "[]")
    assert asyncio.run(ci_red(sh, state=store).run({})).findings == ("o/r#7: no longer open",)


def test_ci_red_a_head_with_no_red_is_not_a_finding_and_a_named_ask_answers_every_row():
    store = fresh_store().notes("agent")
    sh = ci_sh(runs=ci_runs((13, "docs", "success", "2026-09-22T10:00:00Z")))
    out = asyncio.run(ci_red(sh, state=store).run({}))
    assert out.control and out.findings == () and out.data == () and out.count == 1
    out = asyncio.run(ci_red(ci_sh(), state=store).run({"prs": ["o/r#7", "o/r#99"]}))
    assert out.findings == (
        f"o/r#7 aaaaaaaa: ci run 11 red in [test (3.12)]; first: {BOOM}",
        f"o/r#7 aaaaaaaa: lint run 12 red in [lint]; first: {RERUN}",
        "o/r#99: not among the open PRs read",
    )
    assert store.get("ci-red:o/r").value == {}  # the named ask left the snapshot as the last run wrote it


def test_ci_red_control_fails_when_the_runs_read_fails():
    sh = ci_sh(**{f"actions/runs?head_sha={SHA}": (1, "", "HTTP 502: Bad Gateway\n")})
    out = asyncio.run(ci_red(sh).run({}))
    assert not out.control and out.data == ()
    assert out.findings == ("o/r#7 aaaaaaaa: the runs read failed: HTTP 502: Bad Gateway",)


def now_sh(**over) -> FakeSh:
    """ci_sh with what PrState.now reads besides the runs: the PR's view and both required surfaces."""
    view = {"headRefOid": SHA, "state": "OPEN", "mergeStateStatus": "BLOCKED"}
    view |= {"reviewDecision": "REVIEW_REQUIRED", "isDraft": False, "baseRefName": "main"}
    return ci_sh(
        **{
            "pr view 7 -R o/r --json": json.dumps(view),
            "rules/branches/main": "[]",
            "branches/main/protection/required_status_checks": CLASSIC_404,
            **over,
        }
    )


def first_page(total: int, n: int = 50) -> FakeSh:
    """now_sh whose head holds `total` runs and whose one page read holds n green ci runs, answered as
    the query with total_count prints them and as the query before it printed the same page (no count)."""
    rows = [
        {
            "id": i,
            "workflow_id": 501,
            "name": "ci",
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            "created_at": f"2026-09-22T10:{i:02d}:00Z",
        }
        for i in range(1, n + 1)
    ]
    return now_sh(
        **{
            f"actions/runs?head_sha={SHA}": runs_page(rows, total),
            f"actions/runs?head_sha={SHA}&per_page=50 --jq [.workflow_runs": json.dumps(rows),
        }
    )


def now_row(sh: FakeSh) -> dict:
    return asyncio.run(
        PrState([{"name": "o/r", "ci": "actions"}], fresh_store().notes("agent"), every=None, run=sh).now(
            "o/r", 7
        )
    )


def test_pr_state_now_a_first_page_of_runs_short_of_its_total_count_does_not_answer_ci():
    """One page of 50 runs without the head's count would never see a red run beyond it, so a page short
    of the total_count does not answer CI."""
    row = now_row(first_page(51))
    assert row["ci_answered"] is False and row["ci"] == "runs read incomplete: 50 of 51"
    assert row["errors"] == ["CI channel did not answer: runs read incomplete: 50 of 51"]


@pytest.mark.parametrize(
    ("total", "said"), [(51, "50 of 51"), (50, "50 of 50 (a full page of 50)")], ids=["short", "full"]
)
def test_ci_red_a_first_page_of_runs_short_of_its_total_count_fails_the_control(total, said):
    """A page short of the head's total, or a full page, is not the head's every run."""
    out = asyncio.run(ci_red(first_page(total)).run({}))
    assert not out.control and out.findings == (f"o/r#7 aaaaaaaa: runs read incomplete: {said}",)


SHARED_NAME = ci_runs(
    (11, "CI", "failure", "2026-09-22T09:00:00Z", 501), (12, "CI", "success", "2026-09-22T10:00:00Z", 502)
)


def test_ci_red_two_workflows_sharing_a_name_are_two_workflows_and_the_failed_one_is_read():
    """The newest run is chosen per workflow id, never per name: per name, workflow 502's newer green
    run would hide workflow 501's failure and its log would never be read."""
    sh = ci_sh(runs=SHARED_NAME)
    out = asyncio.run(ci_red(sh).run({}))
    assert out.control and out.findings == (
        f"o/r#7 aaaaaaaa: CI#501 run 11 red in [test (3.12)]; first: {BOOM}",
    )
    assert [(r["workflow"], r["run_id"]) for r in out.data[0]["runs"]] == [("CI#501", 11)]
    assert [c[3] for c in sh.calls if "view" in c] == ["11"]
    assert "CI#501=failure (run 11), CI#502=success (run 12)" in out.evidence


def test_pr_state_now_two_workflows_sharing_a_name_each_answer_at_the_head():
    """The same two workflows through PrState.now."""
    row = now_row(now_sh(runs=SHARED_NAME))
    assert (
        row["ci_answered"] is True
        and row["ci"] == "CI#501=failure(pull_request), CI#502=success(pull_request)"
    )


THREE_IDS = ci_runs(
    (11, "CI", "failure", "2026-09-22T09:00:00Z", 501),
    (12, "CI", "success", "2026-09-22T10:00:00Z", 502),
    (13, "CI#501", "success", "2026-09-22T11:00:00Z", 503),
)


def test_pr_state_now_display_labels_cannot_collapse_three_distinct_workflow_ids():
    """Workflow 503 is literally named CI#501, the label workflow 501 is shown under; a result keyed by
    label would keep one of the two and drop 501's failure."""
    row = now_row(now_sh(runs=THREE_IDS))
    assert row["ci_answered"] is True and row["errors"] == []
    assert row["ci"] == (
        "CI#501=failure(pull_request), CI#501=success(pull_request), CI#502=success(pull_request)"
    )


def test_ci_red_a_label_collision_keeps_the_failed_workflow_and_its_red_is_not_news_again():
    """Workflow 501's failure is read, then workflows 502 CI and 503 CI#501 appear at the head; runs
    keyed by label would drop 501, never fetch its log and clear its red."""
    store = fresh_store().notes("agent")
    sh = ci_sh(runs=ci_runs((11, "CI", "failure", "2026-09-22T09:00:00Z", 501)))
    inst = ci_red(sh, state=store)
    first = f"o/r#7 aaaaaaaa: CI run 11 red in [test (3.12)]; first: {BOOM}"
    assert asyncio.run(inst.run({})).findings == (first,)
    before = store.get("ci-red:o/r").value
    sh.set(f"actions/runs?head_sha={SHA}", THREE_IDS)
    sh.calls.clear()
    out = asyncio.run(inst.run({}))
    assert out.control and [(r["workflow"], r["run_id"]) for d in out.data for r in d["runs"]] == [
        ("CI#501", 11)
    ]
    assert [c[3] for c in sh.calls if "view" in c] == ["11"]  # its failed log is read
    assert out.findings == ()  # the same red run under its new label is not a new red
    assert store.get("ci-red:o/r").value == before
    assert asyncio.run(ci_red(sh).run({})).findings == (first.replace(": CI run", ": CI#501 run"),)


def test_pr_state_now_and_ci_red_a_literal_hash_in_a_name_without_a_collision_reads_as_named():
    """A label collides only when names do: workflow 502 literally named CI#501 beside workflow 501
    named CI, which no other workflow shares, so 501 shows as CI."""
    runs = ci_runs(
        (11, "CI", "failure", "2026-09-22T09:00:00Z", 501),
        (12, "CI#501", "success", "2026-09-22T10:00:00Z", 502),
    )
    row = now_row(now_sh(runs=runs))
    assert (
        row["ci_answered"] is True and row["ci"] == "CI=failure(pull_request), CI#501=success(pull_request)"
    )
    out = asyncio.run(ci_red(ci_sh(runs=runs)).run({}))
    assert out.control and out.findings == (f"o/r#7 aaaaaaaa: CI run 11 red in [test (3.12)]; first: {BOOM}",)


def test_ci_red_a_workflows_newer_green_run_still_supersedes_its_older_failure():
    """Within one workflow id the newest run still wins: its newer run is green."""
    runs = ci_runs(
        (11, "CI", "failure", "2026-09-22T09:00:00Z", 501), (12, "CI", "success", "2026-09-22T10:00:00Z", 501)
    )
    out = asyncio.run(ci_red(ci_sh(runs=runs)).run({}))
    assert out.control and out.findings == () and out.data == ()
    assert now_row(now_sh(runs=runs))["ci"] == "CI=success(pull_request)"


def test_pr_state_now_and_ci_red_a_page_holding_every_run_at_the_head_answer_as_before():
    """A page holding every run at the head answers: three runs, total_count 3."""
    sh = first_page(3, n=3)
    row = now_row(sh)
    assert row["ci_answered"] is True and row["ci"] == "ci=success(pull_request)" and row["errors"] == []
    out = asyncio.run(ci_red(sh).run({}))
    assert out.control and out.findings == () and out.data == ()


def test_ci_red_control_fails_when_a_red_runs_failed_log_does_not_read():
    sh = ci_sh(**{"run view 12 -R o/r --log-failed": (1, "", "HTTP 410: logs expired\n")})
    out = asyncio.run(ci_red(sh).run({}))
    assert not out.control
    assert out.findings[0] == "o/r#7 aaaaaaaa: lint run 12: --log-failed rc 1: HTTP 410: logs expired"
    assert [r["run_id"] for r in out.data[0]["runs"]] == [11]  # the run that read is still extracted


def test_ci_red_control_fails_when_the_repo_does_not_resolve_or_the_local_dir_is_absent(tmp_path):
    sh = ci_sh(**{"api repos/o/r --jq": json.dumps({"full_name": "o/elsewhere", "id": 1, "admin": True})})
    out = asyncio.run(ci_red(sh).run({}))
    assert not out.control and out.findings[0].startswith("o/r: gh does not resolve it as itself")
    out = asyncio.run(ci_red(ci_sh(), channel=f"local:{tmp_path / 'absent'}").run({}))
    assert not out.control
    assert out.findings == (
        f"o/r: the local log dir {tmp_path / 'absent'} is not a directory; no PR is judged",
    )
    with pytest.raises(ValueError, match="actions or local:<log dir>"):
        CiRed([{"name": "o/r", "channel": "statuses"}], fresh_store().notes("agent"), every=None)


def transcript(sha: str, *jobs: tuple[str, str, list[str]]) -> str:
    """A local-ci transcript: its header, then each job's section and verdict line."""
    head = f"# ci transcript\n# repo=o/r pr=7\n# head={sha}\n# base={'c' * 40}\n\n"
    return head + "".join(
        f"\n===== job {name} =====\n\n"
        + "\n".join(body)
        + f"\n\n===== {name}: {status} -- {name} said so\n\n"
        for name, status, body in jobs
    )


def test_ci_red_local_extracts_each_red_jobs_failing_step_from_the_heads_own_newest_transcript(tmp_path):
    pr = tmp_path / "pr-7"
    pr.mkdir()
    rust = [
        "$ gh api repos/o/r/pulls/7",
        "[cwd=None rc=0 #2]",
        '{"body":"a gate that stays green while asserting nothing"}',  # a passing step's output: no red
        "",
        "$ cargo test --workspace",
        "[cwd=/t/tree rc=101 #3]",
        "   Compiling x v0.1.0",
        "test tests::a ... \x1b[31mFAILED\x1b[0m",
        "thread 'tests::a' panicked at src/lib.rs:9:5:",
        "assertion failed: left == right",
        "  left: 1",
        "note: run with RUST_BACKTRACE=1 to display a backtrace",
        RERUN,
    ]
    older = pr / "20260922T090000Z-aaaaaaaa.log"
    older.write_text(transcript(SHA, ("Rust", "success", ["$ true", "[cwd=/t rc=0 #1]"])))
    newest = pr / "20260922T100000Z-aaaaaaaa.log"
    newest.write_text(
        transcript(
            SHA,
            ("Changes", "success", ["$ git merge-base x y", "[cwd=/t/tree rc=0 #1]"]),
            ("Rust", "failure", rust),
            ("Timeouts", "error", ["$ bash slow.sh", "[cwd=/t/tree TIMED OUT after 600s]"]),
            ("Ready-run guard", "error", []),
        )
    )
    os.utime(older, (1000, 1000))
    other = pr / "20260922T110000Z-bbbbbbbb.log"
    other.write_text(transcript("b" * 40, ("Rust", "failure", ["$ false", "[cwd=/t rc=1 #1]"])))
    (pr / "latest.log").symlink_to(other.name)
    out = asyncio.run(ci_red(ci_sh(), channel=f"local:{tmp_path}").run({}))
    assert out.control
    assert out.findings == (
        "o/r#7 aaaaaaaa: local-ci run 20260922T100000Z-aaaaaaaa red in [Rust, Timeouts, Ready-run guard]; "
        "first: test tests::a ... FAILED",
    )
    (row,) = out.data
    assert row["channel"] == f"local:{tmp_path}" and row["dropped_by_dedupe"] == 0
    assert row["runs"] == [
        {
            "workflow": "local-ci",
            "run_id": "20260922T100000Z-aaaaaaaa",
            "job": "Rust",
            "lines": [
                "$ cargo test --workspace",
                "[cwd=/t/tree rc=101 #3]",
                "test tests::a ... FAILED",
                "thread 'tests::a' panicked at src/lib.rs:9:5:",
                "assertion failed: left == right",
                "  left: 1",
                RERUN,
                "===== Rust: failure -- Rust said so",
            ],
        },
        {
            "workflow": "local-ci",
            "run_id": "20260922T100000Z-aaaaaaaa",
            "job": "Timeouts",
            "lines": [
                "$ bash slow.sh",
                "[cwd=/t/tree TIMED OUT after 600s]",
                "===== Timeouts: error -- Timeouts said so",
            ],
        },
        {  # a job red with no failing step of its own: its verdict line is the extract
            "workflow": "local-ci",
            "run_id": "20260922T100000Z-aaaaaaaa",
            "job": "Ready-run guard",
            "lines": ["===== Ready-run guard: error -- Ready-run guard said so"],
        },
    ]
    assert (
        "o/r#7 aaaaaaaa: 20260922T100000Z-aaaaaaaa.log: Changes=success, Rust=failure, Timeouts=error, "
        "Ready-run guard=error" in out.evidence
    )


@pytest.mark.parametrize("ending", ["unfinished", "discarded"])
def test_ci_red_local_an_unfinished_or_discarded_retry_keeps_the_prior_red_and_names_the_gap(
    tmp_path, ending
):
    """The local runner creates and flushes the head's transcript before its jobs finish, and writes
    `===== <job>: DISCARDED -- <reason>` when a run loses its lock; neither retry clears the prior red."""
    pr = tmp_path / "pr-7"
    pr.mkdir()
    first = pr / "20260922T090000Z-aaaaaaaa.log"
    first.write_text(
        transcript(SHA, ("Rust", "failure", ["$ cargo test", "[cwd=/t rc=101 #1]", "error: a real failure"]))
    )
    os.utime(first, (1000, 1000))
    store = fresh_store().notes("agent")
    inst = ci_red(ci_sh(), channel=f"local:{tmp_path}", state=store)
    assert asyncio.run(inst.run({})).data
    red = store.get("ci-red:o/r").value
    retry = f"# ci transcript\n# repo=o/r pr=7\n# head={SHA}\n\n===== job Rust =====\n\n$ cargo test\n"
    if ending == "discarded":
        retry += (
            "[cwd=/t rc=101 #1]\n\n===== Rust: DISCARDED -- lost its lock\n"
            "===== the run computed 'failure' / 'cargo test' and it is not trustworthy\n"
        )
    (pr / "20260922T100000Z-aaaaaaaa.log").write_text(retry)
    out = asyncio.run(inst.run({}))
    gap = {"unfinished": "job Rust: no verdict yet", "discarded": "job Rust: DISCARDED lost its lock"}
    assert not out.control and out.findings == (f"o/r#7 aaaaaaaa: {gap[ending]}",)
    assert store.get("ci-red:o/r").value == red  # the prior red is kept, never cleared


def test_ci_red_local_a_header_only_retry_keeps_the_prior_red_and_names_the_gap(tmp_path):
    """The local runner writes and flushes the transcript's header before its first job, so a retry
    holding only that header does not clear the red."""
    pr = tmp_path / "pr-7"
    pr.mkdir()
    first = pr / "20260922T090000Z-aaaaaaaa.log"
    first.write_text(
        transcript(SHA, ("Rust", "failure", ["$ cargo test", "[cwd=/t rc=101 #1]", "error: a real failure"]))
    )
    os.utime(first, (1000, 1000))
    store = fresh_store().notes("agent")
    inst = ci_red(ci_sh(), channel=f"local:{tmp_path}", state=store)
    assert asyncio.run(inst.run({})).data
    red = store.get("ci-red:o/r").value
    (pr / "20260922T100000Z-aaaaaaaa.log").write_text(
        f"# ci transcript\n# repo=o/r pr=7\n# head={SHA}\n# base=main\n# started=20260922T100000Z\n"
        "# machine at admission: load5=3.1 swap_free=2048M free_mem_mb=4096\n"
    )
    out = asyncio.run(inst.run({}))
    assert not out.control and out.findings == ("o/r#7 aaaaaaaa: no job section yet",)
    assert "o/r#7 aaaaaaaa: 20260922T100000Z-aaaaaaaa.log: no job verdict yet" in out.evidence
    assert store.get("ci-red:o/r").value == red  # the prior red is kept, never cleared


def test_ci_red_local_a_job_left_without_a_verdict_before_the_next_began_keeps_the_prior_red(tmp_path):
    """A new job header while a job has no verdict leaves that job unread, even when the next is green."""
    pr = tmp_path / "pr-7"
    pr.mkdir()
    first = pr / "20260922T090000Z-aaaaaaaa.log"
    first.write_text(
        transcript(SHA, ("Rust", "failure", ["$ cargo test", "[cwd=/t rc=101 #1]", "error: a real failure"]))
    )
    os.utime(first, (1000, 1000))
    store = fresh_store().notes("agent")
    inst = ci_red(ci_sh(), channel=f"local:{tmp_path}", state=store)
    assert asyncio.run(inst.run({})).data
    red = store.get("ci-red:o/r").value
    retry = transcript(SHA, ("Lint", "success", ["$ true", "[cwd=/t rc=0 #2]"]))
    retry = retry.replace("\n===== job Lint", "\n===== job Rust =====\n\n$ cargo test\n\n===== job Lint", 1)
    (pr / "20260922T100000Z-aaaaaaaa.log").write_text(retry)
    out = asyncio.run(inst.run({}))
    assert not out.control and out.findings == ("o/r#7 aaaaaaaa: job Rust: no verdict before job Lint began",)
    assert store.get("ci-red:o/r").value == red  # the prior red is kept, never cleared


def test_ci_red_local_a_completed_green_replacement_clears_the_prior_red(tmp_path):
    """A completed green transcript of the same head clears the prior red."""
    pr = tmp_path / "pr-7"
    pr.mkdir()
    first = pr / "20260922T090000Z-aaaaaaaa.log"
    first.write_text(transcript(SHA, ("Rust", "failure", ["$ false", "[cwd=/t rc=1 #1]"])))
    os.utime(first, (1000, 1000))
    store = fresh_store().notes("agent")
    inst = ci_red(ci_sh(), channel=f"local:{tmp_path}", state=store)
    assert asyncio.run(inst.run({})).data
    (pr / "20260922T100000Z-aaaaaaaa.log").write_text(
        transcript(SHA, ("Rust", "success", ["$ true", "[cwd=/t rc=0 #1]"]))
    )
    out = asyncio.run(inst.run({}))
    assert out.control and out.findings == ("o/r#7 aaaaaaaa: no red at this head",)
    assert store.get("ci-red:o/r").value == {}


def test_ci_red_local_latest_log_without_the_heads_own_log_is_no_log_for_this_head(tmp_path):
    pr = tmp_path / "pr-7"
    pr.mkdir()
    other = pr / "20260922T110000Z-bbbbbbbb.log"
    other.write_text(transcript("b" * 40, ("Rust", "failure", ["$ false", "[cwd=/t rc=1 #1]"])))
    (pr / "latest.log").symlink_to(other.name)
    out = asyncio.run(ci_red(ci_sh(), channel=f"local:{tmp_path}").run({}))
    assert not out.control and out.data == ()
    assert out.findings == ("o/r#7 aaaaaaaa: no log for this head",)


def test_ci_red_local_a_transcript_whose_header_names_another_head_fails_the_control(tmp_path):
    pr = tmp_path / "pr-7"
    pr.mkdir()
    (pr / "20260922T100000Z-aaaaaaaa.log").write_text(
        transcript("aaaaaaaa" + "b" * 32, ("Rust", "failure", ["$ false", "[cwd=/t rc=1 #1]"]))
    )
    out = asyncio.run(ci_red(ci_sh(), channel=f"local:{tmp_path}").run({}))
    assert not out.control and out.data == ()
    assert out.findings == (
        "o/r#7 aaaaaaaa: 20260922T100000Z-aaaaaaaa.log: its header head aaaaaaaa is not the PR's head",
    )


def test_instruments_wires_the_desk_chores_and_signs_as_the_owners_desk_by_default():
    cfg = {
        "owner": "lambda:x",
        "chores": {
            "required-contexts": {"every": "every:30m", "repos": [{"name": "o/r", "classic": True}]},
            "verdict-freshness": {"every": "every:30m", "repos": ["o/r"], "verdict_dir": "/x/verdicts"},
            "ci-red": {"every": "every:30m", "repos": [{"name": "o/r", "channel": "local:/x"}]},
        },
    }
    built = instr.instruments(cfg, None, fresh_store().notes("agent"))
    assert sorted(built) == ["ci-red", "required-contexts", "verdict-freshness"]
    assert built["required-contexts"].source_identity == "lambda:x:desk"
    assert str(built["verdict-freshness"].dir) == "/x/verdicts"
    assert built["verdict-freshness"].repos == ["o/r"]
    assert built["ci-red"].repos == [{"name": "o/r", "channel": "local:/x"}]
    assert {i.every for i in built.values()} == {"every:30m"}
    assert instr.VERDICT_DIR is None  # no default: a verdict-freshness chore names its verdict_dir
    del cfg["chores"]["verdict-freshness"]["verdict_dir"]
    with pytest.raises(ValueError, match="verdict-freshness reads verdicts from a verdict_dir"):
        instr.instruments(cfg, None, fresh_store().notes("agent"))
