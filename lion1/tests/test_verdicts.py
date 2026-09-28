"""The review daemon's verdict notice triaged by code: the artifact the notice names, read under the
verdict dir only, must name the notice's PR and head; one gh read gives the head now and the ownership
inputs; the ladder (branch prefix, commit author name, actor map; never the login) decides the owner or
names every rung's reading; anything undecided is an error, never a guess. The desk then triages such a
notice in discovery and files, leaves or escalates it by code, never listing it to the model."""

import asyncio
import importlib
import importlib.util
import json
import os
import time

import pytest

from hub.agent import Posture
from hub.agent import desk as dm
from hub.agent.chores import Bounded
from hub.agent.instruments import GH, VerdictFreshness
from hub.khive import KhiveError
from lionagi import RunRequest
from lionagi.notes import FileNoteStore
from tests.test_chores import DONE, OWNER, SELF, STEWARD, FakeKhive, Scripted
from tests.test_desk import (
    LIVE,
    SWEEP,
    TASK2,
    TASK2_CALL,
    FakeReader,
    assigns,
    inbox_row,
    pending,
    run_turns,
    sweep_run,
)
from tests.test_desk import build as desk_build
from tests.test_instruments import FakeSh

EV = "f00dcafe" + "e" * 32  # the head the verdict evaluated
NOW = "c0ffee00" + "1" * 32  # the PR's head after a later push
COVERAGE = "- coverage bounds: diff-chars=ok(10/100), debt-hits=hit(?/25)"
PREFIX = {"x": "lambda:x", "b": "lambda:b"}
AUTHORS = {"Ann": "lambda:a", "b": "lambda:b"}
VIEW = [GH, "pr", "view", "7", "-R", "o/r", "--json", "headRefOid,headRefName,state,commits"]


def verdicts_module():
    """hub/agent/verdicts.py; a tree without it fails every case here, at this assertion."""
    assert importlib.util.find_spec("hub.agent.verdicts") is not None, "hub.agent has no verdict triage"
    return importlib.import_module("hub.agent.verdicts")


def pair(
    d,
    *,
    repo="o/r",
    n=7,
    evaluated=EV,
    json_file=True,
    md_file=True,
    blocking=("a", "b"),
    decision="REQUEST-CHANGES",
    degraded=False,
    cause=None,
) -> None:
    """A verdict as the daemon writes it: <owner>-<repo>-<n>-<head8>.json beside its .md."""
    d.mkdir(parents=True, exist_ok=True)
    stem = d / f"o-r-{n}-{evaluated[:8]}"
    if json_file:
        body = {
            "repo": repo,
            "number": n,
            "evaluated_head": evaluated,
            "decision": decision,
            "blocking": list(blocking),
            "asserted_blocking": len(blocking),
            "degraded": degraded,
        } | ({"cause": cause} if cause else {})
        stem.with_suffix(".json").write_text(json.dumps(body))
    if md_file:
        why = "review crashed before verdict emission: ProviderQuotaError('usage limit')" if degraded else ""
        stem.with_suffix(".md").write_text(
            f"# Review verdict — {repo}#{n} @ {evaluated[:8]}\n- decision: **{decision}**\n"
            f"- degraded: {degraded} ({why})\n"
            + (f"- cause: {cause} (NOT a reasoned verdict)\n" if cause else "")
            + f"{COVERAGE}\n- evaluated head: {evaluated}\n\n## Blocking\n"
            + ("".join(f"- {b}\n" for b in blocking) or "- none\n")
            + "\n## Issues (0)\n"
        )


def gh(head=EV, branch="x/work", authors=("Pat",), state="OPEN") -> FakeSh:
    pr = {
        "headRefOid": head,
        "headRefName": branch,
        "state": state,
        "commits": [{"oid": "1" * 40, "authors": [{"name": a, "login": "shared"}]} for a in authors],
    }
    return FakeSh(
        **{
            "api repos/o/r --jq": json.dumps({"full_name": "o/r", "id": 42, "admin": False}),
            "pr view 7 -R o/r --json headRefOid,headRefName,state,commits": json.dumps(pr),
        }
    )


def notice(d, artifact: bool = True, stub: bool = False, at: str = "o-r-7-f00dcafe.md") -> str:
    text = (
        "PR review verdict: o/r#7 @ f00dcafe -> ENGINE-UNAVAILABLE [OUTAGE: provider-quota, not a reasoned "
        "verdict; see artifact] [PARKED: no verdict at this head] [NOT POSTED: outage] (0 blocking recorded "
        "before the run died)."
        if stub
        else "PR review verdict: o/r#7 @ f00dcafe -> REQUEST-CHANGES [NOT POSTED: off-list] (2 blocking)."
    )
    return text + (f" Artifact: {d}/{at}" if artifact else "")


ASIDE = "o-r-7-f00dcafe-20260903T050005-00001-2"


def set_aside(d, name=ASIDE, *, evaluated=EV, decision="ENGINE-UNAVAILABLE", json_file=False) -> str:
    """What the daemon writes to outage-aside/ when a reasoned verdict already holds the head: the header
    lines of a set-aside file as the daemon writes it, with a quota cause and the evaluated head line the
    newer ones carry; returns the name the notice gives."""
    side = d / "outage-aside"
    side.mkdir(parents=True, exist_ok=True)
    stub = decision == "ENGINE-UNAVAILABLE"
    why = 'review crashed before verdict emission: ProviderQuotaError("You\'ve hit your usage limit.")'
    if json_file:
        body = {
            "artifact_schema_version": 10,
            "repo": "o/r",
            "number": 7,
            "head_sha": evaluated,
            "evaluated_head": evaluated,
            "decision": decision,
            "degraded": stub,
            "blocking": [],
            "asserted_blocking": 0,
            "round_no": 3,
        } | ({"degrade_reason": why, "cause": "provider-quota"} if stub else {})
        (side / f"{name}.json").write_text(json.dumps(body))
    (side / f"{name}.md").write_text(
        f"# Review verdict — o/r#7 @ {evaluated[:8]}\n- decision: **{decision}**\n"
        "- when: 2026-09-03T09:00:05.466888+00:00\n"
        "- title: feat(core): add a feature\n"
        + (
            f"- degraded: True ({why})\n- cause: provider-quota (NOT a reasoned verdict)\n"
            "- findings-register hash: (none, no engine run)\n"
            if stub
            else "- degraded: False\n"
        )
        + f"- context inputs: git.digest=present\n{COVERAGE}\n- evaluated head: {evaluated}\n\n"
        "## Blocking\n- none\n\n## Issues (0)\n\n## Verdict text\n"
        "The engine run raised before an artifact could be written.\n"
    )
    return f"outage-aside/{name}.md"


def triage(d, sh: FakeSh, text: str, actor_map=None, **stubs) -> dict:
    v = verdicts_module().Verdicts(
        verdict_dir=str(d),
        actor_map={"o/r": "lambda:x"} if actor_map is None else actor_map,
        actor_prefix=PREFIX,
        author_actor=AUTHORS,
        run=sh,
        **stubs,
    )
    return asyncio.run(v.triage(text))


def test_a_fresh_verdict_on_the_chairs_pr_is_decided_with_its_triage_line(tmp_path):
    d = tmp_path / "verdicts"
    pair(d)
    sh = gh()
    got = triage(d, sh, notice(d))
    assert got["errors"] == [] and got["fresh"] is True
    assert got["line"] == (
        "o/r#7 @ f00dcafe: REQUEST-CHANGES, 2 blocking, head fresh, OPEN, owner lambda:x by branch prefix, "
        f"json: {d}/o-r-7-f00dcafe.json"
    )
    assert (got["owner"], got["owner_by"], got["coverage"]) == ("lambda:x", "branch prefix", COVERAGE)
    assert got["rungs"] == {
        "branch prefix": "x -> lambda:x",
        "commit author": "Pat -> no actor",
        "actor map": "o/r -> lambda:x",
    }
    read = [got[k] for k in ("evaluated_head", "current_head", "state", "branch")]
    assert read == [EV, EV, "OPEN", "x/work"]
    assert sh.calls[-1] == VIEW and len(sh.calls) == 2  # one resolve, one read; `author` is never asked


def test_a_stale_verdict_names_the_evaluated_head_and_the_head_now(tmp_path):
    d = tmp_path / "verdicts"
    pair(d)
    got = triage(d, gh(head=NOW), notice(d))
    assert got["errors"] == [] and got["fresh"] is False
    assert got["line"] == (
        "o/r#7 @ f00dcafe: REQUEST-CHANGES, 2 blocking, head STALE (now c0ffee00), OPEN, owner lambda:x by "
        f"branch prefix, json: {d}/o-r-7-f00dcafe.json"
    )


@pytest.mark.parametrize(
    ("branch", "authors", "named"),
    [
        (
            "b/layer-corpus",
            ("b",),
            "branch prefix b -> lambda:b; commit author b -> lambda:b; actor map o/r -> lambda:x",
        ),
        (
            "x/work",
            ("Ann", "b"),
            "branch prefix x -> lambda:x; commit author Ann -> lambda:a, b -> lambda:b; "
            "actor map o/r -> lambda:x",
        ),
    ],
    ids=["later-rung", "two-authors"],
)
def test_an_ownership_disagreement_is_undecided_and_names_every_rung(tmp_path, branch, authors, named):
    d = tmp_path / "verdicts"
    pair(d)
    got = triage(d, gh(branch=branch, authors=authors), notice(d))
    assert got["errors"] == [f"ownership disagreement: {named}"]
    assert got["owner"] is None and got["line"] == f"UNDECIDED: ownership disagreement: {named}"


def test_no_rung_naming_an_actor_is_undecided(tmp_path):
    d = tmp_path / "verdicts"
    pair(d)
    got = triage(d, gh(branch="codex/fix"), notice(d), actor_map={})
    assert got["errors"] == [
        "ownership: no rung names an actor: branch prefix codex -> no actor; commit author Pat -> no actor; "
        "actor map o/r -> no actor"
    ]
    assert got["owner"] is None


@pytest.mark.parametrize(("head", "state"), [(NOW, "OPEN"), (EV, "MERGED"), (NOW, "CLOSED")])
@pytest.mark.parametrize(
    ("branch", "actor_map"), [("b/x", None), ("codex/fix", {})], ids=["disagree", "none"]
)
def test_a_stale_or_closed_verdict_is_decided_whatever_its_ownership(
    tmp_path, head, state, branch, actor_map
):
    """Only a live verdict needs its owner: a stale or closed one is left for that reason, so a ladder that
    disagrees or names no one is said on its line, never raised as an error."""
    d = tmp_path / "verdicts"
    pair(d)
    got = triage(d, gh(head=head, state=state, branch=branch), notice(d), actor_map=actor_map)
    assert (
        got["errors"] == [] and got["owner"] is None and (got["fresh"], got["state"]) == (head == EV, state)
    )
    assert "owner unsettled (ownership" in got["line"]


@pytest.mark.parametrize(
    "head", ["f00dcafe", "f00dcafe" + "e" * 31, "f00dcafe" + "e" * 33, "f00dcafe" + "z" * 32]
)
def test_an_artifact_whose_evaluated_head_is_not_a_full_sha_is_undecided_never_stale(tmp_path, head):
    d = tmp_path / "verdicts"
    pair(d, md_file=False)
    body = json.loads((d / "o-r-7-f00dcafe.json").read_text()) | {"evaluated_head": head}
    (d / "o-r-7-f00dcafe.json").write_text(json.dumps(body))
    sh = gh()
    got = triage(d, sh, notice(d, at="o-r-7-f00dcafe.json"))
    assert got["errors"] == [
        f"artifact unreadable: {d}/o-r-7-f00dcafe.json: ValueError: evaluated_head {head!r} is not a full sha"
    ]
    assert sh.calls == []  # nothing is asked of gh about an artifact whose identity did not read


def test_the_jsons_own_coverage_is_the_coverage_and_a_companion_of_another_round_lends_none(tmp_path):
    d = tmp_path / "verdicts"
    pair(d)
    j, md = d / "o-r-7-f00dcafe.json", d / "o-r-7-f00dcafe.md"
    body = json.loads(j.read_text())
    j.write_text(json.dumps(body | {"coverage_bounds": {"diff-chars": "ok(1/9)", "debt-hits": "ok(0/25)"}}))
    got = triage(d, gh(), notice(d))
    assert got["coverage"] == "- coverage bounds: debt-hits=ok(0/25), diff-chars=ok(1/9)"
    # no coverage in the JSON: the .md lends its line only when it names the JSON's register
    j.write_text(json.dumps(body | {"register_hash": "abc123"}))
    text = md.read_text()
    md.write_text(text.replace(COVERAGE, f"- findings-register hash: abc123\n{COVERAGE}"))
    assert triage(d, gh(), notice(d))["coverage"] == COVERAGE
    md.write_text(text.replace(COVERAGE, f"- findings-register hash: def456\n{COVERAGE}"))
    got = triage(d, gh(), notice(d))
    assert got["coverage"] == "coverage: companion .md is another round's (register def456)"
    md.write_text(text)
    got = triage(d, gh(), notice(d))
    assert got["coverage"] == "coverage: companion .md is another round's (register none)"
    # a JSON naming no register takes no line from a .md that names one
    j.write_text(json.dumps(body))
    md.write_text(text.replace(COVERAGE, f"- findings-register hash: def456\n{COVERAGE}"))
    got = triage(d, gh(), notice(d))
    assert got["coverage"] == "coverage: companion .md is another round's (register def456)"


@pytest.mark.parametrize("name", ["o-r-7-f00dcafe.md", "o-r-7-f00dcafe.json"])
@pytest.mark.parametrize("folder", ["review verdicts", "review.md archive", "a.json b"])
def test_an_artifact_path_holding_a_space_is_read_whole(tmp_path, name, folder):
    d = tmp_path / folder
    pair(d)
    got = triage(d, gh(), notice(d, at=name) + "\nnext line")
    assert got["errors"] == [] and got["path"] == f"{d}/o-r-7-f00dcafe.json"


def test_an_md_only_verdict_naming_its_register_keeps_its_own_coverage(tmp_path):
    d = tmp_path / "verdicts"
    pair(d, json_file=False)
    md = d / "o-r-7-f00dcafe.md"
    md.write_text(md.read_text().replace(COVERAGE, f"- findings-register hash: abc123\n{COVERAGE}"))
    got = triage(d, gh(), notice(d))
    assert got["errors"] == [] and got["read"] == "md header (no JSON)" and got["coverage"] == COVERAGE


@pytest.mark.parametrize("state", [None, "", "DRAFT"])
def test_a_pr_state_that_did_not_read_is_undecided_whatever_the_ownership(tmp_path, state):
    d = tmp_path / "verdicts"
    pair(d)
    got = triage(d, gh(state=state, branch="b/x"), notice(d))
    assert got["errors"] == [f"o/r#7: state not read: {state!r}"]


def test_a_notice_without_an_artifact_line_reads_the_md_header_when_the_json_is_missing(tmp_path):
    d = tmp_path / "verdicts"
    pair(d, json_file=False)
    got = triage(d, gh(), notice(d, artifact=False))
    assert got["errors"] == [] and got["read"] == "md header (no JSON)"
    assert got["line"].endswith(
        f", owner lambda:x by branch prefix, md header (no JSON): {d}/o-r-7-f00dcafe.md"
    )
    assert (got["decision"], got["blocking"], got["coverage"]) == ("REQUEST-CHANGES", 2, COVERAGE)


@pytest.mark.parametrize("md_head", [NOW, None], ids=["another-head", "no-head-line"])
def test_a_companion_md_of_another_head_lends_the_json_no_coverage(tmp_path, md_head):
    """The daemon publishes the .md before the .json, so for a moment the .md beside a JSON can be
    another head's: its coverage is not the JSON's, and the triage's head and decision stay the JSON's."""
    d = tmp_path / "verdicts"
    pair(d)
    md = d / "o-r-7-f00dcafe.md"
    text = md.read_text().replace(COVERAGE, "- coverage bounds: ANOTHER ROUND")
    text = text.replace("**REQUEST-CHANGES**", "**APPROVE**")
    head = f"- evaluated head: {md_head}\n" if md_head else ""
    md.write_text(text.replace(f"- evaluated head: {EV}\n", head))
    got = triage(d, gh(), notice(d))
    want = "coverage: companion .md is for head c0ffee00" if md_head else "coverage: not in the artifact"
    assert got["coverage"] == want, got["coverage"]
    read = [got[k] for k in ("errors", "read", "evaluated_head", "decision", "fresh")]
    assert read == [[], "json", EV, "REQUEST-CHANGES", True]


def test_an_artifact_naming_another_pr_is_undecided_before_any_gh_read(tmp_path):
    d = tmp_path / "verdicts"
    pair(d, repo="o/other")
    sh = gh()
    got = triage(d, sh, notice(d))
    assert got["errors"] == ["artifact names o/other#7 @ f00dcafe, notice names o/r#7 @ f00dcafe"]
    assert sh.calls == []


@pytest.mark.parametrize("shape", ["absolute", "dotdot", "symlink"])
def test_an_artifact_outside_the_verdict_dir_is_refused_without_reading_it(tmp_path, monkeypatch, shape):
    d, elsewhere = tmp_path / "verdicts", tmp_path / "elsewhere"
    pair(elsewhere)  # a readable verdict for the same PR and head, outside the dir
    d.mkdir()
    named = {
        "absolute": f"{elsewhere}/o-r-7-f00dcafe.md",
        "dotdot": f"{d}/../elsewhere/o-r-7-f00dcafe.md",
        "symlink": f"{d}/o-r-7-f00dcafe.md",
    }[shape]
    if shape == "symlink":
        os.symlink(elsewhere / "o-r-7-f00dcafe.md", named)
    reads = []
    monkeypatch.setattr(VerdictFreshness, "_read", staticmethod(lambda files: reads.append(files) or {}))
    sh = gh()
    text = f"PR review verdict: o/r#7 @ f00dcafe -> REQUEST-CHANGES (2 blocking). Artifact: {named}"
    got = triage(d, sh, text)
    assert got["errors"] == [f"artifact outside the verdict dir: {named}"]
    assert reads == [] and sh.calls == []


def test_a_notice_naming_no_pr_is_undecided(tmp_path):
    sh = gh()
    got = triage(tmp_path, sh, "PR review verdict: nothing parsable here")
    assert got["errors"] == ["no PR in the notice"] and got["line"] == "UNDECIDED: no PR in the notice"
    assert sh.calls == []


@pytest.mark.parametrize("json_file", [True, False], ids=["json", "md-only"])
def test_an_engine_unavailable_stub_is_decided_as_a_stub_before_any_gh_read(tmp_path, json_file):
    """The daemon's stub when its engine did not answer is no reasoned verdict: triage names it with its
    cause, a decision and no error, and neither gh nor the ownership ladder is read."""
    d = tmp_path / "verdicts"
    stub = {"decision": "ENGINE-UNAVAILABLE", "degraded": True, "cause": "provider-quota", "blocking": ()}
    pair(d, json_file=json_file, **stub)
    sh = gh()
    got = triage(d, sh, notice(d, stub=True))
    assert sh.calls == []
    assert got["errors"] == [] and got.get("stub") == "provider-quota" and got["rungs"] == {}
    read, path = (
        ("json", f"{d}/o-r-7-f00dcafe.json")
        if json_file
        else ("md header (no JSON)", f"{d}/o-r-7-f00dcafe.md")
    )
    assert got["line"] == f"o/r#7 @ f00dcafe: ENGINE-UNAVAILABLE (provider-quota), {read}: {path}"
    assert (got["owner"], got["current_head"], got["decision"]) == (None, None, "ENGINE-UNAVAILABLE")


def test_a_degraded_but_reasoned_verdict_is_no_stub_and_reads_gh_and_the_ladder(tmp_path):
    d = tmp_path / "verdicts"
    pair(d, degraded=True, cause="provider-quota")
    sh = gh()
    got = triage(d, sh, notice(d))
    assert got["errors"] == [] and got.get("stub") is None and got["degraded"] is True
    assert (got["owner"], got["owner_by"], got["fresh"]) == ("lambda:x", "branch prefix", True)
    assert sh.calls[-1] == VIEW and len(sh.calls) == 2


@pytest.mark.parametrize(
    ("decision", "stub"), [("ENGINE-TIMEOUT", "unstated"), ("ENGINE-UNAVAILABLE", None)], ids=["in", "out"]
)
def test_a_configured_stub_set_replaces_the_default(tmp_path, decision, stub):
    d = tmp_path / "verdicts"
    pair(d, decision=decision, degraded=True, blocking=())
    sh = gh()
    got = triage(d, sh, notice(d), stub_decisions=frozenset({"ENGINE-TIMEOUT"}))
    assert got["errors"] == [] and got.get("stub") == stub
    assert len(sh.calls) == (0 if stub else 2) and got["owner"] == (None if stub else "lambda:x")


@pytest.mark.parametrize("json_file", [False, True], ids=["md-only", "json-pair"])
def test_a_stub_set_aside_is_read_by_its_own_name_and_decided_as_a_stub(tmp_path, json_file):
    """The notice names the timestamped file under outage-aside/: that file, or its .json of the same
    name, is the record, never the reasoned verdict holding the head in the dir above; no gh read."""
    d = tmp_path / "verdicts"
    pair(d)  # the reasoned verdict that holds the head
    sh = gh()
    got = triage(d, sh, notice(d, stub=True, at=set_aside(d, json_file=json_file)))
    read = f"{d}/outage-aside/{ASIDE}.{'json' if json_file else 'md'}"
    assert (got["errors"], got["stub"], got["path"]) == ([], "provider-quota", read)
    assert sh.calls == [] and got["owner"] is None
    kind = "json" if json_file else "md header (no JSON)"
    assert got["line"] == f"o/r#7 @ f00dcafe: ENGINE-UNAVAILABLE (provider-quota), {kind}: {read}"


@pytest.mark.parametrize(
    ("name", "evaluated"),
    [("o-r-7-f00dcafe-2026-09-03", EV), ("o-r-7-c0ffee00-20260903T050005-00001-2", NOW)],
    ids=["malformed-suffix", "other-head"],
)
def test_an_aside_name_off_the_daemons_shape_or_of_another_head_is_undecided(tmp_path, name, evaluated):
    d = tmp_path / "verdicts"
    sh = gh()
    got = triage(d, sh, notice(d, stub=True, at=set_aside(d, name, evaluated=evaluated)))
    assert len(got["errors"]) == 1 and got["stub"] is None, got["errors"]
    assert got["line"].startswith("UNDECIDED: ") and sh.calls == []


@pytest.mark.parametrize("name", ["o-r-7-f00dcafe", ASIDE], ids=["plain-name", "timestamped"])
def test_a_reasoned_verdict_set_aside_reads_gh_and_the_ladder_as_usual(tmp_path, name):
    d = tmp_path / "verdicts"
    sh = gh()
    got = triage(d, sh, notice(d, at=set_aside(d, name, decision="APPROVE")))
    assert (got["errors"], got["stub"], got["decision"]) == ([], None, "APPROVE")
    assert (got["owner"], got["owner_by"], got["fresh"]) == ("lambda:x", "branch prefix", True)
    assert sh.calls[-1] == VIEW and len(sh.calls) == 2


# the desk: a verdict notice from `verdict_from` is triaged by code in discovery and never listed

VMID, VAT = "m-v1", "2026-09-22T10:00:04Z"
FILED = "7a5c0001-0000-4000-8000-000000000000"  # the fake store's first row


class Stalling(FakeKhive):
    """The store, whose `gtd.assign` does not answer while `down`, as a timed-out kkernel call."""

    down = False

    async def exec(self, ops: str):
        if self.down and ops.startswith("gtd.assign("):
            self.ops.append(ops)
            raise KhiveError("kkernel did not answer within 60s")
        return await super().exec(ops)


def verdict_desk(
    tmp_path, sh: FakeSh, actor_map=None, verdict: bool = True, stub: bool = False, aside: bool = False
):
    """A front desk of OWNER behind the bounded client, the chair's box holding one daemon notice whose
    thread holds it word for word; `verdict` pairs its artifact in the verdict dir, `stub` makes it the
    daemon's ENGINE-UNAVAILABLE stub, `aside` adds the stub the daemon sets aside beside that verdict and
    makes the notice name it."""
    d = tmp_path / "verdicts"
    d.mkdir()
    if verdict:
        kind = {"decision": "ENGINE-UNAVAILABLE", "degraded": True, "cause": "provider-quota", "blocking": ()}
        pair(d, **(kind if stub else {}))
    named = set_aside(d) if aside else "o-r-7-f00dcafe.md"
    note = inbox_row(
        "daemon:review",
        "PR review verdict: o/r#7",
        notice(d, stub=stub or aside, at=named),
        VMID,
        "t-v1",
        at=VAT,
    )
    reader = FakeReader(note)
    reader.threads["t-v1"] = [note]
    inner = Stalling()
    khive = Bounded(inner, recipients=frozenset({OWNER, STEWARD, SELF}))
    desk, _, agent = desk_build(tmp_path, khive, reader, [])
    agent.state.put("desk_since", LIVE, run="-", seq=0)
    desk.verdicts = verdicts_module().Verdicts(
        verdict_dir=str(d),
        actor_map={"o/r": OWNER} if actor_map is None else actor_map,
        actor_prefix=PREFIX,
        author_actor=AUTHORS,
        run=sh,
    )
    desk.verdict_from = ("daemon:review",)
    return desk, inner, agent, d


def forwarded(d) -> str:
    return f"originating msg {VMID} from daemon:review on thread t-v1 at {VAT}\n\n{notice(d)}"


def discover(desk) -> str:
    """One discovery, then the sweep the model would read."""
    assert asyncio.run(desk.watch(0)) == []  # nothing wakes a model
    return asyncio.run(desk.sweep(sweep_run()))


def ledger(desk) -> list[tuple]:
    return [(r["msg"], r["class"], r.get("why"), r.get("task")) for r in desk.rows()]


def test_a_fresh_notice_on_the_chairs_pr_files_one_row_by_code_and_is_never_listed(tmp_path):
    desk, inner, agent, d = verdict_desk(tmp_path, gh())
    assert discover(desk).startswith(f"sweep: nothing new for {OWNER}")
    line = (
        "o/r#7 @ f00dcafe: REQUEST-CHANGES, 2 blocking, head fresh, OPEN, owner lambda:x by branch prefix, "
        f"json: {d}/o-r-7-f00dcafe.json"
    )
    tags = json.dumps(["desk", "from-mail", "from:daemon:review", "verdict", "pr:o/r#7"])
    description = json.dumps(line + "\n" + COVERAGE + "\n\n" + forwarded(d))
    assert assigns(inner) == [
        f'gtd.assign(title="o/r#7 @ f00dcafe: REQUEST-CHANGES (2 blocking)", namespace="{OWNER}", '
        f'assignee="{OWNER}", status="inbox", priority="p2", tags={tags}, description={description}, '
        f'idempotency_key="desk:{SELF}:{VMID}:task")'
    ]
    (row,) = desk.rows()
    assert (row["class"], row["task"], row["title"], row["verdict"]) == (
        "tasked",
        FILED,
        "o/r#7 @ f00dcafe: REQUEST-CHANGES (2 blocking)",
        line,
    )
    assert inner.sent == [] and pending(agent) == {}
    agent.clock = lambda: time.time() + 2  # the brief's window ends past the row's second
    brief = desk.digest_lines(time.strftime("%Y-%m-%d"))
    assert any(ln.startswith(f"desk tasked {VMID[:8]}") and ln.endswith(f" verdict {line}") for ln in brief)


@pytest.mark.parametrize(
    ("head", "state", "why"),
    [(NOW, "OPEN", "verdict: stale head f00dcafe, now c0ffee00"), (EV, "MERGED", "verdict: pr MERGED")],
    ids=["stale", "merged"],
)
@pytest.mark.parametrize("branch", ["x/work", "b/x"], ids=["own", "ownership-disagreement"])
def test_a_stale_or_closed_verdict_is_left_with_why_and_nothing_filed_or_sent(
    tmp_path, head, state, why, branch
):
    desk, inner, agent, _ = verdict_desk(tmp_path, gh(head=head, state=state, branch=branch))
    assert discover(desk).startswith(f"sweep: nothing new for {OWNER}")
    assert ledger(desk) == [(VMID, "left", why, None)]
    assert assigns(inner) == [] and inner.sent == [] and pending(agent) == {}


def test_another_actors_verdict_is_left_owner_and_nothing_filed_or_sent(tmp_path):
    desk, inner, agent, _ = verdict_desk(tmp_path, gh(branch="b/x"), actor_map={"o/r": "lambda:b"})
    assert discover(desk).startswith(f"sweep: nothing new for {OWNER}")
    assert ledger(desk) == [(VMID, "left", "verdict: owner lambda:b by branch prefix", None)]
    assert assigns(inner) == [] and inner.sent == [] and pending(agent) == {}


@pytest.mark.parametrize("case", ["disagreement", "artifact-missing"])
def test_an_undecided_triage_is_escalated_to_the_chair_with_its_reason(tmp_path, case):
    sh = gh(branch="b/x")
    desk, inner, agent, d = verdict_desk(tmp_path, sh, verdict=case != "artifact-missing")
    assert discover(desk).startswith(f"sweep: nothing new for {OWNER}")
    lead = {
        "disagreement": "verdict triage: ownership disagreement: branch prefix b -> lambda:b; "
        "commit author Pat -> no actor; actor map o/r -> lambda:x\nrungs: branch prefix b -> "
        "lambda:b; commit author Pat -> no actor; actor map o/r -> lambda:x",
        "artifact-missing": f"verdict triage: no verdict artifact at {d}/o-r-7-f00dcafe.json or .md\n"
        "rungs: not read",
    }[case]
    assert [(s["to"], s["subject"], s["content"]) for s in inner.sent] == [
        (OWNER, "[NOW] daemon:review: PR review verdict: o/r#7", f"{lead}\n\n{forwarded(d)}")
    ]
    assert [r[:2] for r in ledger(desk)] == [(VMID, "escalated")] and assigns(inner) == []


@pytest.mark.parametrize("branch", ["x/work", "b/x"], ids=["own-pr", "ownership-disagreement"])
def test_an_engine_unavailable_stub_is_left_with_nothing_filed_or_escalated(tmp_path, branch):
    """The stub is no verdict: on the chair's own PR it files no task, and where the ladder would disagree
    it escalates nothing; one `left` row, briefed under its reason."""
    sh = gh(branch=branch)
    desk, inner, agent, d = verdict_desk(tmp_path, sh, stub=True)
    assert discover(desk).startswith(f"sweep: nothing new for {OWNER}")
    assert ledger(desk) == [(VMID, "left", "verdict: engine unavailable (provider-quota)", None)]
    assert assigns(inner) == [] and inner.sent == [] and pending(agent) == {} and sh.calls == []
    line = f"o/r#7 @ f00dcafe: ENGINE-UNAVAILABLE (provider-quota), json: {d}/o-r-7-f00dcafe.json"
    assert desk.rows()[0]["verdict"] == line
    agent.clock = lambda: time.time() + 2  # the brief's window ends past the row's second
    brief = desk.digest_lines(time.strftime("%Y-%m-%d"))
    left = f"left verdict: engine unavailable (provider-quota): {VMID[:8]} from daemon:review"
    assert f"{left} 'PR review verdict: o/r#7'" in brief, brief


def test_a_stub_set_aside_is_left_with_nothing_filed_or_escalated(tmp_path):
    sh = gh()
    desk, inner, agent, d = verdict_desk(tmp_path, sh, aside=True)
    assert discover(desk).startswith(f"sweep: nothing new for {OWNER}")
    assert ledger(desk) == [(VMID, "left", "verdict: engine unavailable (provider-quota)", None)]
    assert assigns(inner) == [] and inner.sent == [] and pending(agent) == {} and sh.calls == []
    read = f"{d}/outage-aside/{ASIDE}.md"
    assert (
        desk.rows()[0]["verdict"]
        == f"o/r#7 @ f00dcafe: ENGINE-UNAVAILABLE (provider-quota), md header (no JSON): {read}"
    )


def test_a_lost_code_filed_task_is_replayed_by_the_next_discovery_under_its_key_and_files_once(tmp_path):
    """The row lands and its answer is lost; while the store does not answer, discoveries replay and the
    model is neither woken nor shown the message; the replay the store answers files nothing new."""
    desk, inner, agent, _ = verdict_desk(tmp_path, gh())
    inner.gtd_lose = 1
    assert asyncio.run(desk.watch(0)) == []
    held = pending(agent)[VMID]
    assert held["by"] == "code" and held["assign"]["idempotency_key"] == f"desk:{SELF}:{VMID}:task"
    assert desk.rows() == [] and agent.errors[-1].startswith(f"desk: task of {VMID} unresolved: KhiveError")
    inner.down = True
    assert discover(desk).startswith(f"sweep: nothing new for {OWNER}")
    assert desk.rows() == [] and pending(agent)[VMID] == held
    inner.down = False
    assert asyncio.run(desk.watch(0)) == []
    ops = assigns(inner)
    assert len(ops) == 4 and len(set(ops)) == 1 and len(inner.tasks) == 1, ops
    assert [(r["class"], r["task"], r["replayed"]) for r in desk.rows()] == [("tasked", FILED, True)]
    assert pending(agent) == {} and inner.sent == []


@pytest.mark.parametrize(
    ("verdict_rows", "today", "want"),
    [(20, 20, ("left", "cap: tasks")), (10, 20, ("left", "cap: tasks")), (19, 19, ("tasked", None))],
    ids=["twenty-code-filed", "mixed-with-model-tasks", "nineteen"],
)
def test_the_twenty_first_task_of_a_day_code_filed_or_mixed_is_left_cap_tasks(
    tmp_path, verdict_rows, today, want
):
    desk, inner, agent, _ = verdict_desk(tmp_path, gh())
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        {"at": now, "msg": f"m-{i:02d}", "from": "lambda:a", "subject": "s", "thread": f"t-{i:02d}"}
        | {"class": "tasked", "run": "-", "task": f"x{i}", "title": "t"}
        | ({"verdict": "o/r#1 @ 00000000: APPROVE"} if i < verdict_rows else {})
        for i in range(today)
    ]
    desk.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    discover(desk)
    got = [(r["class"], r.get("why")) for r in desk.rows() if r["msg"] == VMID]
    assert got == [want] and len(assigns(inner)) == (1 if want[0] == "tasked" else 0)


# two discoveries at once (each sweep runs its own, beside `watch`'s): a code-filed task settles once

TWO_SWEEPS = "<lact s1>sweep()</lact><lact s2>sweep()</lact>"


def persistent_desk(tmp_path):
    desk, inner, agent, _ = verdict_desk(tmp_path, gh())
    agent.state = FileNoteStore(tmp_path / "state.json")
    agent.state.put("desk_since", LIVE, run="-", seq=0)
    return desk, inner, agent


@pytest.mark.parametrize("unknown", [False, True], ids=["original-versus-replay", "two-replays"])
def test_two_discoveries_at_once_settle_a_code_filed_task_once_and_spend_one_slot(tmp_path, unknown):
    """The actor's two sweeps: the first assignment in flight when the second discovery sees its
    recorded request, or a landed assignment whose answer was lost replayed by both. One row, one task,
    and the day's second slot files the next task. The store is released once two discoveries have
    entered, never once two assignments have."""
    desk, inner, agent = persistent_desk(tmp_path)
    desk.tasks_per_day = 2
    if unknown:
        inner.gtd_lose = 1
        assert asyncio.run(desk.watch(0)) == []
        assert len(inner.tasks) == 1 and pending(agent)[VMID]["by"] == "code" and desk.rows() == []

    async def race():
        release, entered, discoveries = asyncio.Event(), asyncio.Event(), 0
        original_discover, original_exec = desk._discover, inner.exec

        async def discover(*args, **kwargs):
            nonlocal discoveries
            discoveries += 1
            if discoveries == 2:
                entered.set()
            return await original_discover(*args, **kwargs)

        async def exec(ops):
            if ops.startswith("gtd.assign("):
                await release.wait()
            return await original_exec(ops)

        desk._discover, inner.exec = discover, exec
        profile, _ = desk.chores.profiles()
        req = RunRequest(inputs=("tick: sweep",), emits=(Posture,), max_rounds=3, accept=desk._accept)
        running = asyncio.create_task(agent.actor.run(profile, req, Scripted(TWO_SWEEPS, DONE)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            release.set()
            return await asyncio.wait_for(running, 2)
        finally:
            release.set()
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            desk._discover, inner.exec = original_discover, original_exec

    first = asyncio.run(race())
    assert len(inner.tasks) == 1 and pending(agent) == {}
    desk.reader.rows.append(TASK2)
    second = run_turns(desk, agent, SWEEP, TASK2_CALL, DONE)
    got = [(r["msg"], r["class"], r.get("why")) for r in desk.rows()]
    assert got == [(VMID, "tasked", None), ("m-t2", "tasked", None)], got
    assert len(inner.tasks) == 2 and pending(agent) == {}
    assert [type(r.outcome).__name__ for r in (first, second)] == ["Success", "Success"]


def test_two_triages_already_waiting_find_the_reservation_and_file_once(tmp_path):
    """Both discoveries triage the new notice before either admits it: the second finds the first's
    reservation or row and files nothing."""
    desk, inner, agent = persistent_desk(tmp_path)
    original, original_exec = desk.verdicts.triage, inner.exec
    arrived, gate = 0, None

    async def triage(text):
        nonlocal arrived, gate
        gate = gate or asyncio.Event()
        got = await original(text)
        arrived += 1
        if arrived == 2:
            gate.set()
        await asyncio.wait_for(gate.wait(), 2)
        return got

    async def exec(ops):
        if ops.startswith("gtd.assign("):
            await asyncio.sleep(0)
        return await original_exec(ops)

    desk.verdicts.triage, inner.exec = triage, exec
    run_turns(desk, agent, TWO_SWEEPS, DONE)
    assert arrived == 2 and len(assigns(inner)) == 1 and len(inner.tasks) == 1
    assert len(desk.rows()) == 1 and pending(agent) == {}


@pytest.mark.parametrize("order", ["code-first", "model-first"])
def test_a_code_filed_and_a_model_task_share_the_last_slot_of_the_day(tmp_path, order):
    """One slot left, a verdict notice and a model's task in one turn, either first: one task filed, the
    other left `cap: tasks`."""
    desk, inner, agent = persistent_desk(tmp_path)
    desk.tasks_per_day = 1
    note = {**desk.reader.rows.pop(), "created_at": "2026-09-22T10:00:06.000000Z"}
    desk.reader.rows = [TASK2]
    original_exec = inner.exec

    async def exec(ops):
        if ops.startswith("gtd.assign("):
            await asyncio.sleep(0)
        return await original_exec(ops)

    inner.exec = exec
    calls = 0
    sweep2 = "<lact s2>sweep()</lact>"
    script = Scripted(SWEEP, sweep2 + TASK2_CALL if order == "code-first" else TASK2_CALL + sweep2, DONE)

    async def backend(messages):
        nonlocal calls
        calls += 1
        if calls == 2:
            desk.reader.rows.append(note)
        return await script(messages)

    profile, _ = desk.chores.profiles()
    req = RunRequest(inputs=("tick: sweep",), emits=(Posture,), max_rounds=3, accept=desk._accept)
    asyncio.run(agent.actor.run(profile, req, backend))
    assert len(inner.tasks) == 1 and len(assigns(inner)) == 1
    got = sorted((r["class"], r.get("why")) for r in desk.rows())
    assert got == [("left", "cap: tasks"), ("tasked", None)], got


def test_a_task_left_cap_tasks_stays_left_past_midnight_and_on_every_later_read(tmp_path, monkeypatch):
    """`cap: tasks` is final: the next day and a forced re-read of the same message file nothing."""
    desk, inner, agent = persistent_desk(tmp_path)
    desk.tasks_per_day = 1
    at = time.strftime("%Y-%m-%d %H:%M:%S")
    old = {"at": at, "msg": "previous-task", "from": "lambda:a", "subject": "work", "thread": "t-old"}
    desk.ledger.write_text(
        json.dumps(old | {"class": "tasked", "run": "-", "task": "old", "title": "t"}) + "\n"
    )
    assert asyncio.run(desk.watch(0)) == []
    final = [r for r in desk.rows() if r["msg"] == VMID]
    assert len(final) == 1 and final[0]["why"] == "cap: tasks" and assigns(inner) == []
    next_midnight = dm._midnight() + 86400
    monkeypatch.setattr(dm, "_midnight", lambda: next_midnight)
    for _ in range(2):  # the ledger alone keeps it final, not the cursor
        agent.state.put("desk_watch", LIVE, run="force-reread", seq=0)
        assert asyncio.run(desk.watch(0)) == []
    assert [r for r in desk.rows() if r["msg"] == VMID] == final
    assert assigns(inner) == [] and pending(agent) == {}
