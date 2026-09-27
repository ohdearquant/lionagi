"""The review daemon's verdict notice, triaged by code, two checks in front of the ownership ladder.
The notice names a PR and the head the verdict evaluated; the artifact is read from the verdict
dir only, as `VerdictFreshness` reads it, and must name that PR and head; one gh read gives the head now
and the ownership inputs. Ownership is a ladder of rungs, branch prefix, then commit author name, then the
daemon's actor map; the account login is never an input, since actors share it. A rung that disagrees with
an earlier one, or no rung at all, is an error that names every rung's reading: never a guess. A stub (a
decision in `stub_decisions`, the daemon's word that its engine did not answer) is decided as a stub with
its cause before any gh read: there is no verdict to own."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .instruments import GH, Runner, VerdictFreshness, _resolve, sh

__all__ = ("Verdicts",)

_NOTICE = re.compile(r"([\w.-]+/[\w.-]+)#(\d+) @ ([0-9a-f]{8})")
_ARTIFACT = re.compile(r"Artifact: (\S+)")
_VERDICT = re.compile(r"^(.+-\d+-[0-9a-f]{8})(?:-\d{8}T\d{6}-\d+-\d+)?\.(json|md)$")  # or set aside, stamped
_COVERAGE = re.compile(r"^- coverage bounds:.*$", re.M)
_EVALUATED = re.compile(r"^- evaluated head: ([0-9a-f]{40})\s*$", re.M)


class Verdicts:
    def __init__(
        self,
        *,
        verdict_dir: str,
        actor_map: dict[str, str],
        actor_prefix: dict[str, str],
        author_actor: dict[str, str],
        run: Runner = sh,
        stub_decisions: frozenset[str] = frozenset({"ENGINE-UNAVAILABLE"}),
    ):
        self.dir, self.sh = verdict_dir, run
        self.actor_map, self.actor_prefix, self.author_actor = actor_map, actor_prefix, author_actor
        self.stub_decisions = stub_decisions

    async def triage(self, text: str) -> dict:
        got: dict[str, Any] = dict.fromkeys(
            ("repo", "number", "decision", "blocking", "asserted", "degraded", "read", "path", "coverage")
            + ("evaluated_head", "current_head", "fresh", "state", "branch", "owner", "owner_by", "stub")
        )
        got |= {"rungs": {}, "errors": []}

        def undecided(why: str) -> dict:
            got["errors"].append(why)
            got["line"] = f"UNDECIDED: {got['errors'][0]}"
            return got

        m = _NOTICE.search(text)
        if m is None:
            return undecided("no PR in the notice")
        repo, n, head8 = m.group(1), int(m.group(2)), m.group(3)
        got |= {"repo": repo, "number": n}
        root = os.path.realpath(self.dir)
        if a := _ARTIFACT.search(text):
            named = a.group(1)
            real = os.path.realpath(named)
            if os.path.commonpath([root, real]) != root:
                return undecided(f"artifact outside the verdict dir: {named}")
            v = _VERDICT.match(os.path.basename(real))
            if v is None:
                return undecided(f"artifact {named} is not a verdict .json or .md")
            stem = os.path.splitext(real)[0]  # the file named, never another round's under the bare stem
        else:
            stem = os.path.join(root, f"{repo.replace('/', '-')}-{n}-{head8}")
        files: dict[str, tuple[str, float]] = {}
        for ext in ("json", "md"):
            p = f"{stem}.{ext}"
            if not os.path.exists(p):
                continue
            if os.path.commonpath([root, os.path.realpath(p)]) != root:
                return undecided(f"artifact outside the verdict dir: {p}")
            files[ext] = (p, os.stat(p).st_mtime)
        if not files:
            return undecided(f"no verdict artifact at {stem}.json or .md")
        rec = VerdictFreshness._read(files)
        if "error" in rec:
            return undecided(f"artifact unreadable: {rec['path']}: {rec['error']}")
        ev = str(rec["evaluated_head"])
        if (rec["repo"], rec["number"], ev[:8]) != (repo, n, head8):
            return undecided(
                f"artifact names {rec['repo']}#{rec['number']} @ {ev[:8]}, notice names {repo}#{n} @ {head8}"
            )
        got |= {k: rec[k] for k in ("decision", "blocking", "asserted", "degraded", "read", "path")}
        got["evaluated_head"] = ev
        if got["decision"] in self.stub_decisions:  # no reasoned verdict: nothing to read gh or a ladder for
            got["stub"] = rec["cause"]
            got["line"] = (
                f"{repo}#{n} @ {ev[:8]}: {got['decision']} ({rec['cause']}), {got['read']}: {got['path']}"
            )
            return got
        got["coverage"] = "coverage: not in the artifact"
        if "md" in files:  # the .md lands before its .json, so it can be another head's for a moment
            try:
                md = Path(files["md"][0]).read_text()
                h = _EVALUATED.search(md)
                if h and h.group(1) != ev:
                    got["coverage"] = f"coverage: companion .md is for head {h.group(1)[:8]}"
                elif h and (c := _COVERAGE.search(md)):
                    got["coverage"] = c.group(0)
            except OSError as e:
                got["coverage"] = f"coverage: the .md is unreadable ({e})"
        info, err = await _resolve(self.sh, repo)
        if info is None:
            return undecided(f"{repo}: gh does not resolve it as itself: {err}")
        rc, out, err = await self.sh(
            [GH, "pr", "view", str(n), "-R", repo, "--json", "headRefOid,headRefName,state,commits"]
        )
        try:
            pr = json.loads(out) if rc == 0 else None
        except json.JSONDecodeError:
            pr = None
        if not isinstance(pr, dict) or not re.fullmatch(r"[0-9a-f]{40}", str(pr.get("headRefOid"))):
            return undecided(f"{repo}#{n}: head not read: rc {rc}, {(err or out).strip()[:200]}")
        cur, branch = pr["headRefOid"], str(pr.get("headRefName") or "")
        got |= {"current_head": cur, "fresh": ev == cur, "state": pr.get("state"), "branch": branch}
        # the ladder: each rung's reading, and the actor it answers, if any
        answers: list[tuple[str, str]] = []
        prefix, slash, _ = branch.partition("/")
        if slash and (actor := self.actor_prefix.get(prefix)):
            answers.append(("branch prefix", actor))
        got["rungs"]["branch prefix"] = (
            f"{prefix} -> {actor or 'no actor'}" if slash else f"{branch} has no prefix"
        )
        names = sorted({str(x.get("name")) for c in pr.get("commits") or [] for x in c.get("authors") or []})
        actors = {self.author_actor[x] for x in names if x in self.author_actor}
        got["rungs"]["commit author"] = (
            ", ".join(f"{x} -> {self.author_actor.get(x, 'no actor')}" for x in names) or "no author name"
        )
        answers += [("commit author", s) for s in sorted(actors)]
        if actor := self.actor_map.get(repo):
            answers.append(("actor map", actor))
        got["rungs"]["actor map"] = f"{repo} -> {actor or 'no actor'}"
        said = "; ".join(f"{k} {v}" for k, v in got["rungs"].items())
        if len({s for _, s in answers}) > 1:
            return undecided(f"ownership disagreement: {said}")
        if not answers:
            return undecided(f"ownership: no rung names an actor: {said}")
        got["owner_by"], got["owner"] = answers[0]
        got["line"] = (
            f"{repo}#{n} @ {ev[:8]}: {got['decision']}, {got['blocking']} blocking, "
            + ("head fresh" if got["fresh"] else f"head STALE (now {cur[:8]})")
            + f", {got['state']}, owner {got['owner']} by {got['owner_by']}, {got['read']}: {got['path']}"
        )
        return got
