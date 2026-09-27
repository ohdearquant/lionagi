"""The record, looked up: is what a neighbour asks about already an issue, an ADR or a decision?

Every actor is an area with a chair, and mail between areas is chatty. A desk agent
reads it first; what a lookup may settle, it answers with candidates ("the record holds these, one may
settle it: issue #N; ADR-00XX, alternatives row"), never a verdict, and the rest it leaves for the chair.
The common replies are few, so they are cases in a table, one lookup each, and a new case is added
when it is met. Three lookups here: the area's repositories through `gh` (`Issues`), its ADR corpus on
disk (`Adrs`), and its khive notes (`Decisions`). `Record` runs them together as one chore the desk
answers with, so one `answer(msg, chore="record", args={"query": ...})` reads all three; the words of
the ask are the query, the model extracts them and nothing else. Every lookup is a word search and
misses a differently worded record (under-report), which is the safe direction: a miss leaves the
message for the chair, never answers wrongly. A lookup with no hit is not an answer (`empty_expected`
is False), so the desk leaves it.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from ..khive import Khive, lit
from .chores import Instrument, Outcome
from .instruments import GH, Runner, sh

__all__ = ("STOP", "TAIL", "terms", "stem", "Issues", "Adrs", "Decisions", "Record")

# words of an ask that select nothing: the question's own furniture
STOP = frozenset(
    "the a an is are was were be been this that these those there here it its of to in on for with and or "
    "not no yes do does did have has had can could would should will what which who whom whose when where "
    "why how about from any all some already please thanks thank hi hello you your yours we our ours i me my "
    "issue issues bug bugs adr adrs decision decisions record question ask asked report reported know".split()
)
TAIL = (
    "If one of these settles it, read it and say whether it is complete. If you want it reopened or "
    "discussed, say so on this thread: the chair reads what the desk leaves."
)


def terms(query: str) -> list[str]:
    """The words of an ask worth searching for, lowercased, in order, without repeats."""
    out: list[str] = []
    for t in re.findall(r"[a-z0-9][a-z0-9_.-]{2,}", query.lower()):
        t = t.strip(".-_")
        if len(t) >= 3 and t not in STOP and t not in out:
            out.append(t)
    return out


def stem(word: str) -> str:
    """A word without its plural or tense ending, so `rejections` finds `rejection` and `refusing`
    finds `refuses`; a short word keeps its ending (`bus` stays `bus`)."""
    for suffix in ("ing", "es", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _query(args: dict[str, Any]) -> str:
    q = str(args.get("query") or "").strip()
    if not q:
        raise ValueError("record: args.query is the words of the ask; none given")
    return q


class Issues:
    """Issues and PRs of the area's repositories whose text carries the words of the ask, any state.
    The control is resolution: `gh repo view` must return each repository's own name (a 404 and a
    permission denial fail alike), in the same run as the search."""

    name = "issues"
    every = None
    about = "issues and PRs of the area's repos matching the words of an ask, any state"
    answers = "which issues and PRs in the area's repositories carry the words of the ask, and their state"
    direction = "under-report"
    mechanism = "gh --search matches words, not meaning: a differently worded issue is missed"
    empty_expected = False

    def __init__(self, repos: list[str], *, run: Runner = sh, limit: int = 8, words: int = 5):
        self.repos, self.sh, self.limit, self.words = list(repos), run, limit, words

    async def run(self, args: dict[str, Any]) -> Outcome:
        # GitHub search wants every word (AND), so the ask's furniture would empty it: its first five
        # content words go, stop words and repeats out
        q = " ".join(terms(_query(args))[: self.words])
        findings: list[str] = []
        evidence: list[str] = []
        positives: list[str] = []
        control = True
        fields = "number,title,state,url,updatedAt"
        for repo in self.repos:
            rc, out, err = await self.sh([GH, "repo", "view", repo, "--json", "nameWithOwner"])
            name = (json.loads(out).get("nameWithOwner") if rc == 0 and out.strip() else "") or ""
            if name != repo:  # a redirect answers rc 0 with another name: unresolved all the same
                control = False
                evidence.append(f"{repo}: not resolved ({err.strip()[:80] or name or 'no name'})")
                continue
            positives.append(f"{repo} resolved as {name}")
            if not q:
                evidence.append(f"{repo}: no searchable word in the ask; nothing listed")
                continue
            for kind, verb in (("issue", "issue"), ("PR", "pr")):
                argv = [GH, verb, "list", "-R", repo, "--search", q, "--state", "all"]
                argv += ["--limit", str(self.limit), "--json", fields]
                rc, out, err = await self.sh(argv)
                if rc != 0:
                    control = False
                    evidence.append(f"{repo} {verb} list: exited {rc}: {err.strip()[:120]}")
                    continue
                rows = json.loads(out or "[]")
                evidence.append(f"{repo} {verb} list --search {q!r}: {len(rows)} row(s)")
                for r in rows:
                    findings.append(
                        f"{repo}#{r['number']} {kind} {str(r.get('state', '')).lower()} updated "
                        f"{str(r.get('updatedAt', ''))[:10]}: {r.get('title', '')} {r.get('url', '')}"
                    )
        return Outcome(
            findings=tuple(findings),
            evidence="\n".join(evidence),
            control=control,
            population=f"issues and PRs of {', '.join(self.repos)} matching {q!r}, any state",
            predicate="gh --search word match over title and body",
            count=len(findings),
            positive="; ".join(positives),
        )


class Adrs:
    """The ADR corpus on disk: each record's title, status and liveness (frontmatter), claim and
    decision headings, and its Alternatives rows, scored by how many distinct words of the ask they
    carry. An Alternatives row that matches is reported as "alternatives:", the table where an ADR says
    what it chose not to do; whether that settles the ask is the asker's and the chair's reading. The
    control is the corpus itself: files were read."""

    name = "adrs"
    every = None
    about = "the ADR corpus: titles, headings and alternatives carrying the words of an ask"
    answers = (
        "which ADRs' titles, claim or decision headings, or rejected alternatives carry the words of the ask"
    )
    direction = "under-report"
    mechanism = (
        "word match over headings and alternatives, never the body: a record that says it in prose is missed"
    )
    empty_expected = False

    HEAD = re.compile(r"^###? (C\d+|D\d+|ADR-\d+):? ?(.*)$")
    MARK = re.compile(r"\s*_\(enforced: \w+\)_|\s*\^\w+$")  # the lint's markers, not words of the record

    def __init__(self, dirs: list[Path], *, limit: int = 5, min_terms: int = 2):
        self.dirs, self.limit, self.min_terms = [Path(d) for d in dirs], limit, min_terms

    def _read(self, path: Path) -> dict[str, Any]:
        text = path.read_text(errors="replace")
        fm: dict[str, str] = {}
        body = text
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end > 0:
                for ln in text[4:end].splitlines():
                    if ":" in ln and not ln.startswith(" "):
                        k, _, v = ln.partition(":")
                        fm[k.strip()] = v.strip().strip('"')
                body = text[end + 5 :]
        title = next((ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")), path.stem)
        heads = [self.MARK.sub("", ln.lstrip("#").strip()) for ln in body.splitlines() if self.HEAD.match(ln)]
        alts: list[str] = []
        in_alts = False
        for ln in body.splitlines():
            if ln.startswith("## "):
                in_alts = ln.strip() == "## Alternatives"
            elif in_alts and ln.startswith("|") and not ln.startswith("| -") and not ln.startswith("|-"):
                cells = [c.strip() for c in ln.strip("|").split("|")]
                if cells and cells[0] not in ("approach", ""):
                    alts.append(" | ".join(cells))
        return {"path": path, "title": title, "fm": fm, "heads": heads, "alts": alts}

    async def run(self, args: dict[str, Any]) -> Outcome:
        q = _query(args)
        words = terms(q)
        stems = [stem(w) for w in words]
        # a third of a long ask's words, never under min_terms: with 2 of 10 the reply carried four
        # ADRs sharing two words with the ask beside the one that settled it
        need = min(max(self.min_terms, -(-len(words) // 3)), len(words))
        files = sorted(p for d in self.dirs for p in d.rglob("ADR-*.md"))
        hits: list[tuple[int, int, dict[str, Any], list[str], list[str]]] = []
        for path in files:
            rec = await asyncio.to_thread(self._read, path)
            heads_hit = [h for h in rec["heads"] if any(w in h.lower() for w in stems)]
            alts_hit = [a for a in rec["alts"] if any(w in a.lower() for w in stems)]
            text = " ".join([rec["title"], *rec["heads"], *rec["alts"]]).lower()
            distinct = sum(1 for w in stems if w in text)
            if words and distinct >= need:
                hits.append((distinct, len(heads_hit) + len(alts_hit), rec, heads_hit, alts_hit))
        hits.sort(key=lambda h: (-h[0], -h[1], str(h[2]["path"])))
        findings: list[str] = []
        for distinct, _, rec, heads_hit, alts_hit in hits[: self.limit]:
            fm = rec["fm"]
            line = (
                f"{fm.get('adr', rec['path'].stem)} {rec['title']} ({fm.get('status', '?')}, "
                f"{fm.get('liveness', '?')[:60]}) {rec['path']}: {distinct} of {len(words)} word(s)"
            )
            if heads_hit:
                line += "; " + "; ".join(heads_hit[:2])
            if alts_hit:
                line += "; alternatives: " + "; ".join(alts_hit[:2])
            findings.append(line)
        return Outcome(
            findings=tuple(findings),
            evidence=f"{len(files)} ADR file(s) under {', '.join(map(str, self.dirs))}; words {words}; "
            f"{len(hits)} carrying >= {need} of them",
            control=bool(files),
            population=f"ADR records under {', '.join(map(str, self.dirs))}",
            predicate=f"title, headings or alternatives carry >= {need} of {words}",
            count=len(findings),
            positive=f"{len(files)} file(s) read, first {files[0].name}" if files else "",
        )


class Decisions:
    """khive notes of the given kinds whose TEXT carries the words of the ask: a hit that only the
    vector arm returned is dropped (every vector result scores alike; the text arm is the match).
    The control is a known-positive search in the same batch: `positive` (an exact name, short form)
    must come back through the text arm."""

    name = "decisions"
    every = None
    about = "khive decision and insight notes whose text carries the words of an ask"
    answers = "which khive notes of the configured kinds carry the words of the ask by text match"
    direction = "under-report"
    mechanism = "the text arm is conjunctive over the words; a note phrased differently is missed"
    empty_expected = False

    def __init__(
        self,
        khive: Khive,
        *,
        positive: str,
        positive_kind: str = "entity",
        kinds: tuple[str, ...] = ("decision", "insight"),
        limit: int = 5,
    ):
        self.khive, self.positive, self.positive_kind, self.kinds, self.limit = (
            khive,
            positive,
            positive_kind,
            tuple(kinds),
            limit,
        )

    async def run(self, args: dict[str, Any]) -> Outcome:
        q = _query(args)
        ops = [f"search(query={lit(q)}, kind={lit(k)}, limit={self.limit})" for k in self.kinds]
        ops.append(f"search(query={lit(self.positive)}, kind={lit(self.positive_kind)}, limit=1)")
        results = await self.khive.exec("[" + ", ".join(ops) + "]")
        *pages, control_page = results
        by_text = lambda rows: [r for r in (rows or []) if r.get("source") in ("text", "both")]  # noqa: E731
        positive_hits = by_text(control_page)
        findings: list[str] = []
        evidence: list[str] = []
        for kind, rows in zip(self.kinds, pages, strict=True):
            hits = by_text(rows)
            evidence.append(f"{kind}: {len(rows or [])} returned, {len(hits)} by text")
            for r in hits:
                snippet = str(r.get("snippet") or "").replace("\n", " ")[:160]
                findings.append(
                    f"{kind} {r.get('id')} ({str(r.get('updated_at', ''))[:16]}): "
                    f"{r.get('name') or r.get('title')}" + (f": {snippet}" if snippet else "")
                )
        return Outcome(
            findings=tuple(findings),
            evidence="\n".join(evidence)
            + f"\npositive {self.positive!r} ({self.positive_kind}): {len(positive_hits)} by text",
            control=bool(positive_hits),
            population=f"khive notes of kinds {list(self.kinds)} matching {q!r} by text",
            predicate="search text arm matched (source text or both)",
            count=len(findings),
            positive=(
                f"{self.positive!r} found as {positive_hits[0].get('name') or positive_hits[0].get('id')}"
                if positive_hits
                else ""
            ),
        )


class Record:
    """The three lookups as one chore: every part runs on the same words, each finding carries its
    part's name, and the outcome is the union. One part's failure fails the control: a reply that
    cites two of three records reads as complete and is not."""

    name = "record"
    every = None
    about = (
        "is this already an issue, an ADR or a decision: repos, ADR corpus, khive notes, by the ask's words"
    )
    answers = (
        "which existing issues, PRs, ADRs and decision notes carry the words of the ask, so the asker "
        "reads them before the chair does"
    )
    direction = "under-report"
    mechanism = (
        "word search over three records: a differently worded one is missed and the chair reads the ask"
    )
    empty_expected = False
    senders = ("lambda:",)  # the reply carries record content back: actors only
    lead = "the record holds these, one may settle it:"
    tail = TAIL

    def __init__(self, parts: list[Instrument]):
        if not parts:
            raise ValueError("record: no lookup configured (repos, adr_dirs or decisions)")
        self.parts = list(parts)

    async def _part(self, p: Instrument, args: dict[str, Any]) -> Outcome:
        try:
            return await p.run(dict(args))
        except Exception as e:
            return Outcome(
                findings=(f"{p.name} failed: {type(e).__name__}: {e}",),
                evidence=f"{type(e).__name__}: {e}",
                control=False,
                population=p.name,
                predicate="ran",
                count=0,
            )

    async def run(self, args: dict[str, Any]) -> Outcome:
        outs = await asyncio.gather(*(self._part(p, args) for p in self.parts))
        findings = tuple(f"[{p.name}] {f}" for p, o in zip(self.parts, outs, strict=True) for f in o.findings)
        return Outcome(
            findings=findings,
            evidence="\n".join(f"[{p.name}]\n{o.evidence}" for p, o in zip(self.parts, outs, strict=True)),
            control=all(o.control for o in outs),
            population="; ".join(o.population for o in outs),
            predicate="; ".join(o.predicate for o in outs),
            count=sum(o.count or 0 for o in outs),
            positive="; ".join(o.positive for o in outs if o.positive),
        )
