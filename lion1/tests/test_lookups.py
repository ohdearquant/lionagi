"""The record lookups behind the desk's `record` chore: issues and PRs through gh with a resolution
control, the ADR corpus by title, headings and rejected alternatives, khive notes by the text arm with
a known-positive in the same batch, the three as one outcome whose control fails when any part's
does, a lookup with no hit left for the chair, the tail on the desk's reply, and the factory."""

import asyncio
import json
import time
from pathlib import Path

import pytest

from hub.agent import instruments as instr
from hub.agent.chores import Chores, Outcome
from hub.agent.lookups import TAIL, Adrs, Decisions, Issues, Record, stem, terms
from lion1 import MemoryNoteStore
from tests.test_chores import DONE, OWNER, SELF, FakeKhive, Probe, full, mail
from tests.test_desk import ASKER, SWEEP, FakeReader, build, inbox_row, results
from tests.test_instruments import FakeSh

ADR_A = """---
adr: ADR-0023
status: signed
liveness: "partial (nothing serves before the cutover)"
---

# ADR-0023: The build queue

## Claims

### C1: Code writes from the script's class alone _(enforced: mechanical)_ ^c1

### C6: A timeout is never surfaced to the person _(enforced: process)_ ^c6

## Decisions

### D1: `hub/agent/queue.py`: three handlers on the chores actor ^d1

## Alternatives

| approach                    | rejected because                                   |
| --------------------------- | -------------------------------------------------- |
| Timeouts escalated too    | the person: the agent handles them; a person reads reports |
| A full scan of the queue    | one read per entry against one read by id          |
"""
ADR_B = """---
adr: ADR-0020
status: draft
liveness: operating
---

# ADR-0020: The box

### D1: `exec` is one argv with stdout and stderr apart ^d1
"""


def corpus(tmp_path: Path) -> Path:
    d = tmp_path / "adr"
    (d / "008-agent").mkdir(parents=True)
    (d / "008-agent" / "ADR-0023-the-build-queue.md").write_text(ADR_A)
    (d / "008-agent" / "ADR-0020-the-box.md").write_text(ADR_B)
    return d


def test_a_stem_drops_a_plural_or_tense_ending_of_a_long_word_only():
    assert [stem(w) for w in ("timeouts", "refusing", "escalated", "uses", "bus", "boxes")] == [
        "timeout",
        "refus",
        "escalat",
        "use",
        "bus",
        "box",
    ]


def test_terms_drop_the_asks_furniture_and_keep_its_words_once():
    assert terms("Is there already an issue about the build queue refusing a live branch? build, BUILD!") == [
        "build",
        "queue",
        "refusing",
        "live",
        "branch",
    ]
    assert terms("bug report: please?") == []


def test_issues_search_both_lists_after_resolving_the_repo_and_an_unresolved_repo_fails_the_control():
    rows = [
        {
            "number": 7,
            "title": "build queue refuses a live branch",
            "state": "OPEN",
            "url": "u7",
            "updatedAt": "2026-09-22T10:00:00Z",
        }
    ]
    sh = FakeSh(
        **{
            "repo view o/r": '{"nameWithOwner": "o/r"}',
            "repo view o/gone": (1, "", "GraphQL: Could not resolve to a Repository"),
            "issue list -R o/r": json.dumps(rows),
            "pr list -R o/r": "[]",
        }
    )
    out = asyncio.run(Issues(["o/r"], run=sh).run({"query": "build queue live branch"}))
    assert out.control and out.count == 1 and out.positive == "o/r resolved as o/r"
    assert out.findings == ("o/r#7 issue open updated 2026-09-22: build queue refuses a live branch u7",)
    assert "o/r issue list --search 'build queue live branch': 1 row(s)" in out.evidence
    search = [c for c in sh.calls if "issue" in c][0]
    assert search[search.index("--search") + 1] == "build queue live branch" and "--state" in search
    # GitHub search is an AND of its words: the ask's furniture goes, and at most five words are sent
    sh.calls.clear()
    asyncio.run(
        Issues(["o/r"], run=sh).run(
            {"query": "Is there already an issue on the build queue refusing a live branch? old row"}
        )
    )
    search = [c for c in sh.calls if "issue" in c][0]
    assert search[search.index("--search") + 1] == "build queue refusing live branch"
    out = asyncio.run(Issues(["o/gone", "o/r"], run=sh).run({"query": "x"}))
    assert not out.control and "o/gone: not resolved (GraphQL: Could not resolve" in out.evidence
    assert out.positive == "o/r resolved as o/r"  # the other repo still answered
    assert "o/r: no searchable word in the ask; nothing listed" in out.evidence  # "x" is not a word
    with pytest.raises(ValueError, match="args.query is the words of the ask"):
        asyncio.run(Issues(["o/r"], run=sh).run({}))


def test_adrs_score_titles_headings_and_rejected_alternatives_by_the_asks_words(tmp_path):
    d = corpus(tmp_path)
    out = asyncio.run(Adrs([d]).run({"query": "should the build queue escalate timeouts too?"}))
    assert out.control and out.count == 1 and out.positive == "2 file(s) read, first ADR-0020-the-box.md"
    (f,) = out.findings
    assert f.startswith(
        "ADR-0023 ADR-0023: The build queue (signed, partial (nothing serves before the cutover))"
    )
    assert "5 of 5 word(s); C6: A timeout is never surfaced to the person; D1: `hub/agent/queue.py`" in f
    assert "alternatives: Timeouts escalated too | the person: the agent handles them" in f
    # one word only: one match is enough; nothing carrying it: no finding, control still passes
    out = asyncio.run(Adrs([d]).run({"query": "the box exec"}))
    assert out.count == 1 and out.findings[0].startswith("ADR-0020 ADR-0020: The box (draft, operating)")
    # a long ask needs a third of its words: 9 words, 3 needed; ADR-0023 carries 5, ADR-0020 only "box"
    long = "does the build queue escalate timeouts too, or settle them by code, and what about the box?"
    assert len(terms(long)) == 9
    out = asyncio.run(Adrs([d]).run({"query": long}))
    assert out.count == 1 and out.findings[0].startswith("ADR-0023") and "9 carrying" not in out.evidence
    assert "1 carrying >= 3 of them" in out.evidence
    out = asyncio.run(Adrs([d]).run({"query": "quantum teleportation"}))
    assert out.control and out.count == 0 and out.findings == ()
    empty = tmp_path / "none"
    empty.mkdir()
    out = asyncio.run(Adrs([empty]).run({"query": "mail"}))
    assert not out.control and out.positive == ""


@pytest.mark.parametrize("second", ["missing", "a-file", "empty"])
def test_adrs_a_configured_root_that_holds_no_record_fails_the_control_beside_one_that_answers(
    tmp_path, second
):
    d = corpus(tmp_path)
    other = tmp_path / "other"
    if second == "a-file":
        other.write_text("not a directory")
    elif second == "empty":
        other.mkdir()
    out = asyncio.run(Adrs([d, other]).run({"query": "should the build queue escalate timeouts too?"}))
    assert not out.control and out.positive == "" and out.count == 1  # the hit is still reported
    assert out.evidence.endswith(f"; no record read under {other}")


ADR_FENCED = """---
adr: ADR-0030
status: draft
liveness: operating
---

```markdown
# ADR-9999: A fenced title
### C9: Widgets escalate gizmos ^c9
## Alternatives
| widgets gizmos | a fenced row |
```

# ADR-0030: The example

~~~~
### C8: Widgets fenced by tildes
~~~~

``` not a fence `here`
### C1: The real claim about sprockets ^c1
"""


def test_adrs_read_no_heading_title_or_row_from_a_fenced_example(tmp_path):
    d = tmp_path / "adr"
    d.mkdir()
    (d / "ADR-0030-the-example.md").write_text(ADR_FENCED)
    out = asyncio.run(Adrs([d]).run({"query": "widgets gizmos"}))
    assert out.control and out.count == 0 and out.findings == ()
    out = asyncio.run(Adrs([d]).run({"query": "real sprockets"}))
    (f,) = out.findings
    assert f.startswith("ADR-0030 ADR-0030: The example (draft, operating)")
    assert f.endswith("2 of 2 word(s); C1: The real claim about sprockets")


class Part:
    name = "custom"

    def __init__(self, **fields):
        self.fields = fields

    async def run(self, args):
        base = dict(findings=("x",), evidence="line", control=True, population="p", predicate="q", count=1)
        return Outcome(**(base | {"positive": "known"} | self.fields))


@pytest.mark.parametrize(
    "missing",
    [
        {"population": ""},
        {"predicate": " "},
        {"positive": ""},
        {"count": None},
        {"evidence": ""},
    ],
    ids=["population", "predicate", "positive", "count", "evidence"],
)
def test_record_refuses_a_part_whose_answer_lacks_a_field_its_siblings_fill(tmp_path, missing):
    d = corpus(tmp_path)
    rec = Record([Adrs([d]), Part(**missing)])
    out = asyncio.run(rec.run({"query": "build queue timeouts"}))
    assert not out.control
    assert out.findings[-1] == (
        "[custom] its answer lacks its own output, population, predicate, count or known-positive; refused"
    )
    assert asyncio.run(Record([Adrs([d]), Part()]).run({"query": "build queue"})).control


class SearchKhive:
    """khive `search` as the lookup sees it: one page per op of a batch, in order."""

    def __init__(self, pages: list[list[dict]]):
        self.pages, self.ops = pages, []

    async def exec(self, ops: str):
        self.ops.append(ops)
        return list(self.pages)


def hit(kind_id: str, name: str, source: str, snippet: str = "") -> dict:
    return {
        "id": kind_id,
        "name": name,
        "source": source,
        "snippet": snippet,
        "updated_at": "2026-09-22T10:00",
    }


def test_decisions_keep_text_hits_only_and_the_positive_search_is_the_control():
    khive = SearchKhive(
        [
            [
                hit("d1", "build queue: timeouts by code", "text", "decided\n2026-09-22"),
                hit("d2", "noise", "vector"),
            ],
            [hit("i1", "build queue insight", "both")],
            [hit("e1", "example PR#12", "both")],
        ]
    )
    dec = Decisions(khive, positive="lambda:owner")
    out = asyncio.run(dec.run({"query": "build queue timeouts"}))
    assert out.control and out.count == 2 and out.positive == "'lambda:owner' found as example PR#12"
    assert out.findings == (
        "decision d1 (2026-09-22T10:00): build queue: timeouts by code: decided 2026-09-22",
        "insight i1 (2026-09-22T10:00): build queue insight",
    )
    assert (
        "decision: 2 returned, 1 by text" in out.evidence
        and "positive 'lambda:owner' (entity): 1 by text" in (out.evidence)
    )
    (ops,) = khive.ops
    assert ops.startswith('[search(query="build queue timeouts", kind="decision", limit=5), ')
    assert ops.endswith('search(query="lambda:owner", kind="entity", limit=1)]')
    khive.pages[2] = [hit("e1", "example PR#12", "vector")]  # the positive came back by vector only
    out = asyncio.run(dec.run({"query": "build queue timeouts"}))
    assert not out.control and out.positive == ""


def test_record_unions_its_parts_and_one_failed_part_fails_the_control(tmp_path):
    sh = FakeSh(**{"repo view": '{"nameWithOwner": "o/r"}', "issue list": "[]", "pr list": "[]"})
    rec = Record([Issues(["o/r"], run=sh), Adrs([corpus(tmp_path)])])
    out = asyncio.run(rec.run({"query": "build queue timeouts"}))
    assert out.control and out.count == 1 and out.findings[0].startswith("[adrs] ADR-0023")
    assert out.positive == "o/r resolved as o/r; 2 file(s) read, first ADR-0020-the-box.md"
    assert out.evidence.startswith("[issues]\no/r issue list") and "[adrs]\n2 ADR file(s)" in out.evidence
    assert rec.tail == TAIL and rec.every is None and rec.empty_expected is False
    assert rec.senders == ("lambda:",) and rec.lead == "the record holds these, one may settle it:"
    sh.set("repo view", (1, "", "no gh"))
    out = asyncio.run(rec.run({"query": "build queue timeouts"}))
    assert not out.control and out.count == 1  # the ADR hit is still there; the answer is not complete
    with pytest.raises(ValueError, match="no lookup configured"):
        Record([])


def test_the_desk_answers_a_record_ask_with_the_tail_and_leaves_one_the_record_does_not_carry(tmp_path):
    ask = inbox_row(ASKER, "bug?", "the build queue refuses a live branch, known?", "m-r1", "t-r1")
    miss = inbox_row(ASKER, "another", "does the agent fold its context?", "m-r2", "t-r2")
    outsider = inbox_row("daemon:review", "bug?", "is the live branch refusal known?", "m-r3", "t-r3")
    khive = FakeKhive(mail(SELF, "tick: sweep"))
    found = full(
        findings=("[adrs] ADR-0023 The build queue (signed): C1",),
        evidence="2 files",
        population="records",
        predicate="words",
        count=1,
    )
    none = full(findings=(), evidence="0 rows", population="records", predicate="words", count=0)
    probe = Probe("record", found, none, found, every=None)
    probe.tail, probe.lead, probe.senders = TAIL, Record.lead, Record.senders
    probe.empty_expected = False
    desk, chores, agent = build(
        tmp_path,
        khive,
        FakeReader(ask, miss, outsider),
        [
            SWEEP,
            '<lact a1>answer(msg="m-r1", chore="record", args={"query": "build queue refuses branch"})</lact>'
            '<lact a2>answer(msg="m-r2", chore="record", args={"query": "agent fold context"})</lact>'
            '<lact a3>answer(msg="m-r3", chore="record", args={"query": "live branch refusal"})</lact>',
            DONE,
        ],
        record=probe,
    )
    run = asyncio.run(agent.wake())
    (sent,) = khive.sent
    assert sent["to"] == ASKER and sent["thread_id"] == "t-r1"
    assert sent["content"].startswith(f"[desk] answered by {SELF} from its `record` instrument")
    assert "- [adrs] ADR-0023 The build queue (signed): C1" in sent["content"]
    assert sent["content"].endswith("\n\n" + TAIL)
    # candidates, never adjudication: the lead names them so, and no verdict word appears
    assert sent["content"].splitlines()[1] == "the record holds these, one may settle it:"
    assert not any(w in sent["content"].lower() for w in ("decided against", "already exists", "duplicate"))
    rows = desk.rows()
    assert [(r["msg"], r["class"]) for r in rows] == [
        ("m-r1", "answered"),
        ("m-r2", "left"),
        ("m-r3", "left"),
    ]
    assert rows[2]["why"] == "sender: record answers lambda:* senders only"  # no reply to an outsider
    # the chair still hears of the answered report: the digest lists it with its pointer
    day = rows[0]["at"][:10]
    t = time.time() + 2  # the brief's window ends at the clock's second; the three rows are inside it
    agent.clock = lambda: t
    since = agent.state.get("desk_since").value
    lines = desk.digest_lines(day)
    assert lines == [
        f"front desk {since} to {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))}: 1 sweep(s), "
        "3 message(s) read, 1 answered, 0 routed, 0 tasked, 0 escalated, 2 left, 0 asked; "
        "ANOMALOUS: 0 escalated, 0 delivery unresolved, 0 clarify open, 1 refused",
        f"desk answered m-r1 from {ASKER} 'bug?' with record on thread t-r1: "
        "[adrs] ADR-0023 The build queue (signed): C1",
        f"left {rows[1]['why']}: m-r2 from {ASKER} 'another'",
        "left sender: record answers lambda:* senders only: m-r3 from daemon:review 'bug?'",
    ]
    assert desk.summary(day) == f"desk {day}: 3 message(s) read, 1 answered, 2 left (instrument 1, sender 1)"
    digest = asyncio.run(chores.digest(day, run))
    assert lines[1] in digest and khive.sent[-1]["to"] == OWNER
    assert rows[1]["why"].startswith("instrument:") and rows[0]["args"] == {
        "query": "build queue refuses branch"
    }
    assert any("m-r2: left, the instrument did not answer" in r for r in results(run))


def test_the_factory_builds_the_record_chore_from_its_config(tmp_path):
    cfg = {
        "owner": OWNER,
        "chores": {
            "record": {
                "repos": ["o/r"],
                "adr_dirs": [str(corpus(tmp_path))],
                "positive": "ADR-0017",
                "limit": 3,
            }
        },
    }
    out = instr.instruments(cfg, SearchKhive([]), MemoryNoteStore())
    rec = out["record"]
    assert isinstance(rec, Record) and [type(p).__name__ for p in rec.parts] == [
        "Issues",
        "Adrs",
        "Decisions",
    ]
    assert rec.parts[0].repos == ["o/r"] and rec.parts[0].limit == 3
    assert rec.parts[2].positive == "ADR-0017" and rec.parts[2].kinds == ("decision", "insight")
    only = instr.instruments(
        {"owner": OWNER, "chores": {"record": {"decisions": False, "repos": ["o/r"]}}}, None, None
    )
    assert [type(p).__name__ for p in only["record"].parts] == ["Issues"]
    assert only["record"].parts[0].limit == 8
    chores_profile = Chores  # the chore is on-ask only: no tick, so the digest does not count it
    assert rec.every is None and chores_profile is Chores
