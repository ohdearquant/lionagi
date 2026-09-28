"""The instruments: the read-only measurements an actor's long-running agent runs as chores
(hub.agent.chores). gh for PR state, ps and launchctl for served things, khive for a gtd inbox and
mail, df and du for disk, git for worktrees, the bench actor's notes for its jobs.
Every instrument runs its known-positive in the invocation that produces its answer and hands what
it read to the handler, which refuses an outcome without one, so an empty answer from a broken
instrument escalates instead of reading clean. `instruments(cfg, ...)` builds the ones a
`chores.toml` names, with their arguments.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from lionagi.notes import NoteStore

from ..khive import Khive, lit
from .chores import Outcome

__all__ = (
    "Runner",
    "GIB",
    "PR_PAGE",
    "DEFAULT_FLOORS",
    "VERDICT_DIR",
    "GH",
    "GIT",
    "LAUNCHCTL",
    "sh",
    "PrState",
    "Served",
    "GtdInbox",
    "InboxSla",
    "Disk",
    "Worktrees",
    "BenchJobs",
    "RequiredContexts",
    "VerdictFreshness",
    "CiRed",
    "instruments",
)

Runner = Callable[..., Awaitable[tuple[int, str, str]]]
GIB = 2**30
PR_PAGE = 100  # rows one `gh pr list` returns; a full page is not a complete list
_RUNS_PAGE = 50  # workflow runs one read of a head returns; a full page is not every run
# the default floor, in the unit floors are written in (GiB base-1024, the number `df -g` prints); a
# config without `floors` inherits it, a config with `floors = []` declines
DEFAULT_FLOORS = [{"name": "steady (default)", "budget_gib": 0, "floor_gib": 300}]
# where the review daemon writes its verdicts, <owner>-<repo>-<n>-<head8>.json beside its .md copy: no
# default, a verdict-freshness chore names its `verdict_dir`
VERDICT_DIR: str | None = None


def _bin(name: str, *fallbacks: str) -> str:
    """An absolute program path: under launchd there is no brew or cargo on PATH, so the head of
    every subprocess is resolved here, once, and read back in the log line at start."""
    found = shutil.which(name)
    if found:
        return found
    for f in fallbacks:
        if Path(f).exists():
            return f
    return name  # left for the instrument to fail loudly on


GH = _bin("gh", "/opt/homebrew/bin/gh", "/usr/local/bin/gh")
GIT = _bin("git", "/usr/bin/git")
LAUNCHCTL = "/bin/launchctl"


async def sh(argv: list[str], *, timeout: float = 60, cwd: str | None = None) -> tuple[int, str, str]:
    """One read-only command: rc, stdout, stderr. Every program path is the caller's; nothing here
    is a shell."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        await _stop(proc)
        return 124, "", f"{argv[0]} did not finish within {timeout}s"
    except BaseException:  # a cancelled read still stops the child it started
        await _stop(proc)
        raise
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def _stop(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):  # it may have exited on its own
        proc.kill()
    await proc.wait()


def _age_h(iso: str, now: float | None = None) -> float:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return ((now or time.time()) - t.timestamp()) / 3600


def _snapshot(state: NoteStore, key: str) -> dict | None:
    note = state.get(key)
    return dict(note.value) if note is not None and isinstance(note.value, dict) else None


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _resolve(run: Runner, repo: str) -> tuple[dict | None, str]:
    """The destination check: gh must return the repo's own name for it (a rename redirects with rc
    0). The row carries the immutable id and whether this token is admin there."""
    rc, out, err = await run(
        [GH, "api", f"repos/{repo}", "--jq", "{full_name, id, admin: .permissions.admin}"]
    )
    try:
        info = json.loads(out) if rc == 0 else None
    except json.JSONDecodeError:
        info = None
    if not isinstance(info, dict) or info.get("full_name") != repo:
        return None, (err or out).strip()[:200]
    return info, ""


async def _open_prs(run: Runner, repo: str, fields: str) -> tuple[list[dict] | None, str]:
    rc, out, err = await run(
        [GH, "pr", "list", "-R", repo, "--state", "open", "--limit", str(PR_PAGE), "--json", fields]
    )
    if rc != 0:
        return None, f"gh pr list failed: {err.strip()[:200]}"
    try:
        return json.loads(out or "[]"), ""
    except json.JSONDecodeError:  # rc 0 with unreadable output is a broken read, not an empty set
        return None, f"gh pr list gave no JSON: {out[:120]!r}"


async def _runs_at(run: Runner, repo: str, sha: str) -> tuple[int, str, list[dict], dict[int, dict], str]:
    """The workflow runs at a head, the newest run per workflow id (never the rollup, which splices
    runs), keyed by that id, and why the read is incomplete when the page read is not every run at
    the head."""
    rc, out, err = await run(
        [
            GH,
            "api",
            f"repos/{repo}/actions/runs?head_sha={sha}&per_page={_RUNS_PAGE}",
            "--jq",
            "{total_count, runs: [.workflow_runs[] | {id, workflow_id, name, event, status, conclusion, "
            "created_at}]}",
        ]
    )
    page = json.loads(out or "{}") if rc == 0 else {}
    rows, total = page.get("runs") or [], page.get("total_count")
    short = ""
    if rc == 0 and not (isinstance(total, int) and total <= len(rows) < _RUNS_PAGE):
        short = f"runs read incomplete: {len(rows)} of {total}"
        short += f" (a full page of {_RUNS_PAGE})" if total == len(rows) else ""
    newest: dict[int, dict] = {}
    for r in rows:  # the name is display only: two workflows can share one
        w = r["workflow_id"]
        if w not in newest or r["created_at"] > newest[w]["created_at"]:
            newest[w] = r
    return rc, err, rows, newest, short


def _labels(newest: dict[int, dict]) -> dict[int, str]:
    """Each workflow's display label: its name, or name#id where another workflow at the head shares
    the name. Rendered only, never a key: a literal name can equal another workflow's name#id."""
    names = [r["name"] for r in newest.values()]
    return {w: r["name"] if names.count(r["name"]) == 1 else f"{r['name']}#{w}" for w, r in newest.items()}


async def _required(
    run: Runner, repo: str, base: str, admin: bool | None
) -> tuple[list[dict] | None, dict[str, str]]:
    """The contexts a base requires, one row per rule {context, source, app}, from both surfaces, and
    how each surface read. A classic 404 is absence only with admin (without, it is the same code as
    permission). None when the set cannot be stated: the rulesets did not read and classic gave none."""
    b = quote(base, safe="")
    rules: list[dict] = []
    rc, out, err = await run([GH, "api", f"repos/{repo}/rules/branches/{b}"])
    try:
        rows = json.loads(out) if rc == 0 else None
    except json.JSONDecodeError:
        rows, err = None, f"no JSON: {out[:80]!r}"
    if isinstance(rows, list):
        for r in rows:
            if r.get("type") == "required_status_checks":
                for c in (r.get("parameters") or {}).get("required_status_checks") or []:
                    rules.append(
                        {"context": c["context"], "source": "ruleset", "app": c.get("integration_id")}
                    )
        surfaces = {"ruleset": f"read ({len(rows)} rule(s))"}
    else:
        surfaces = {"ruleset": f"UNREAD ({(err or out).strip()[:120]})"}
    rc, out, err = await run([GH, "api", f"repos/{repo}/branches/{b}/protection/required_status_checks"])
    try:
        classic = json.loads(out) if rc == 0 else None
    except json.JSONDecodeError:
        classic, err = None, f"no JSON: {out[:80]!r}"
    if isinstance(classic, dict):
        checks = {c["context"]: c.get("app_id") for c in classic.get("checks") or []}
        for c in classic.get("contexts") or []:
            checks.setdefault(c, None)
        rules += [{"context": c, "source": "classic", "app": a} for c, a in checks.items()]
        surfaces["classic"] = "read"
    elif rc != 0 and "HTTP 404" in err and admin is True:
        surfaces["classic"] = "none (404 with admin: no required checks)"
    elif rc != 0 and "HTTP 404" in err:
        surfaces["classic"] = "UNREAD (404 without admin reads the same as unprotected)"
    else:
        surfaces["classic"] = f"UNREAD ({(err or out).strip()[:120]})"
    if not surfaces["ruleset"].startswith("read") and not rules:
        return None, surfaces
    return sorted(rules, key=lambda r: (r["context"], r["source"])), surfaces


class PrState:
    """Open PRs per repo: head, the CI verdict at that head, review decision, mergeability, age. The
    first sight of a repo or a PR reports its whole row; later runs report transitions, and a PR
    crossing the stale bound once. The control is resolution: gh must return the repo's own name for
    it (a 404 and a permission denial both fail here), and the list must parse; an empty open set
    after that is a value only where the chore declared it expected.

    Each repo declares where its CI answer lives (`ci`): `actions` reads the newest run per workflow at
    the head (never the rollup, which splices runs); `statuses` reads the commit statuses and asserts
    at least one, because that channel answers `[]` for a missing subject; `none` means the repo has
    no CI by decision and an empty read is its expected value. A PR absent from a full page is read
    on its own before it is called closed: page omission is not closure."""

    name = "pr-state"
    about = "open PRs: head, CI at the head, review; reports first sight, transitions, staleness"
    answers = (
        "for each open PR of each repo: its head, the CI verdict at that head on the repo's declared "
        "channel, its review decision and mergeability, and which of those changed since the last run"
    )
    direction = "over-report"
    mechanism = "every transition is a finding, so a flapping head or a re-run reports again"

    def __init__(
        self,
        repos: list[str | dict],
        state: NoteStore,
        *,
        every: str | None,
        stale_days: float = 3,
        run: Runner = sh,
        empty_expected: bool = False,
    ):
        self.repos = [{"name": r, "ci": "actions"} if isinstance(r, str) else dict(r) for r in repos]
        self.state, self.every, self.stale_days, self.sh = state, every, stale_days, run
        self.empty_expected = empty_expected
        bad = [r for r in self.repos if r.get("ci", "actions") not in ("actions", "statuses", "none")]
        if bad:
            raise ValueError(f"a repo's ci is actions, statuses or none: {bad}")

    async def _verdict(self, repo: str, ci: str, sha: str) -> tuple[str, bool]:
        """The CI verdict at a head on the repo's declared channel; the bool is that channel's own
        control (a statuses read must be non-empty; an actions read must hold every run at the head)."""
        if ci == "none":
            return "no CI by declaration", True
        if ci == "statuses":
            rc, out, err = await self.sh(
                [GH, "api", f"repos/{repo}/commits/{sha}/status", "--jq", "[.statuses[] | {context, state}]"]
            )
            rows = json.loads(out or "[]") if rc == 0 else []
            if rc != 0 or not rows:
                return (
                    f"statuses read gave {'error ' + err.strip()[:60] if rc else 'no rows'} "
                    "(a missing subject reads the same)",
                    False,
                )
            newest: dict[str, str] = {}
            for r in rows:  # the API lists newest first per context
                newest.setdefault(r["context"], r["state"])
            return ", ".join(f"{c}={s}" for c, s in sorted(newest.items())), True
        # newest run per workflow, then that run's verdict
        rc, err, _, newest_run, short = await _runs_at(self.sh, repo, sha)
        if rc != 0:
            return "gh api failed: " + err.strip()[:80], False
        if short:  # a run not read may be the red one: no verdict from part of the list
            return short, False
        label = _labels(newest_run)
        verdict = ", ".join(
            f"{label[w]}={r['conclusion'] or r['status']}({r['event']})"
            for w, r in sorted(newest_run.items(), key=lambda i: (label[i[0]], i[0]))
        )
        return verdict or "REQUIRED CHECKS DO NOT EXIST AT THIS HEAD (no runs)", True

    async def now(self, repo: str, number: int) -> dict:
        """One PR's row from gh at the moment of asking, for a peer's state question: never from the
        snapshot, and the snapshot and transitions are not touched. `errors` names every read that
        failed; a field it left None was not read."""
        fields = ("headRefOid", "state", "mergeStateStatus", "reviewDecision", "isDraft", "baseRefName")
        row: dict[str, Any] = {"repo": repo, "number": number, "read_at": _utc(), "errors": []}
        row |= dict.fromkeys((*fields, "ci", "ci_answered", "required", "required_surfaces"))
        info, err = await _resolve(self.sh, repo)
        if info is None:
            row["errors"].append(f"gh does not resolve {repo} as itself: {err}")
            return row
        rc, out, err = await self.sh([GH, "pr", "view", str(number), "-R", repo, "--json", ",".join(fields)])
        if rc != 0:
            row["errors"].append(f"gh pr view failed: {err.strip()[:200]}")
            return row
        pr = json.loads(out)
        row |= {k: pr.get(k) for k in fields}
        spec = next((r for r in self.repos if r["name"] == repo), {"ci": "actions"})
        row["ci"], row["ci_answered"] = await self._verdict(repo, spec.get("ci", "actions"), pr["headRefOid"])
        rules, row["required_surfaces"] = await _required(self.sh, repo, pr["baseRefName"], info.get("admin"))
        row["required"] = None if rules is None else sorted({r["context"] for r in rules})
        if not row["ci_answered"]:
            row["errors"].append(f"CI channel did not answer: {row['ci']}")
        if rules is None:
            row["errors"].append(f"required set UNREAD: {row['required_surfaces']}")
        return row

    async def run(self, args: dict[str, Any]) -> Outcome:
        repos = [{"name": r, "ci": "actions"} if isinstance(r, str) else r for r in args.get("repos") or []]
        repos = repos or self.repos
        findings, table, positives, control, stale_n, pr_n = [], [], [], True, 0, 0
        for spec in repos:
            repo, ci = spec["name"], spec.get("ci", "actions")
            rc, out, err = await self.sh([GH, "api", f"repos/{repo}", "--jq", ".full_name"])
            if rc != 0 or out.strip() != repo:  # the destination check: the name must come back as given
                control = False
                findings.append(f"{repo}: gh does not resolve it as itself: {(err or out).strip()[:200]}")
                continue
            fields = "number,title,headRefOid,updatedAt,reviewDecision,isDraft,mergeable,url"
            rc, out, err = await self.sh(
                [GH, "pr", "list", "-R", repo, "--state", "open", "--limit", str(PR_PAGE), "--json", fields]
            )
            if rc != 0:
                control = False
                findings.append(f"{repo}: gh pr list failed: {err.strip()[:200]}")
                continue
            try:
                prs = json.loads(out)
            except ValueError:  # a JSONDecodeError, or an integer past the conversion limit
                prs = None
            if not isinstance(prs, list):  # rc 0 without a decoded list is a broken read, not an empty set
                control = False
                findings.append(f"{repo}: gh pr list gave no JSON list: {out[:120]!r}")
                continue
            full = len(prs) >= PR_PAGE
            positives.append(
                f"{repo}: api repos returned its own name; pr list answered {len(prs)} row(s) as JSON"
                + (f" (a full page of {PR_PAGE}: absences read on their own)" if full else "")
            )
            table.append(f"control: gh resolves {repo} as itself; ci channel {ci}")
            now, snap = time.time(), {}
            pr_n += len(prs)
            for pr in prs:
                sha = pr["headRefOid"]
                verdict, ok = await self._verdict(repo, ci, sha)
                if not ok:
                    control = False
                    findings.append(f"{repo}#{pr['number']}: CI channel {ci} did not answer: {verdict}")
                age_d = _age_h(pr["updatedAt"], now) / 24
                stale = age_d > self.stale_days and not pr.get("isDraft")
                stale_n += stale
                snap[str(pr["number"])] = {
                    "sha": sha[:10],
                    "verdict": verdict,
                    "review": pr.get("reviewDecision") or "",
                    "mergeable": pr.get("mergeable") or "",
                    "stale": stale,
                    "title": pr["title"][:60],
                }
                review, mergeable = pr.get("reviewDecision") or "-", pr.get("mergeable") or "-"
                draft = " | draft" if pr.get("isDraft") else ""
                table.append(
                    f"{repo}#{pr['number']} {pr['title'][:50]} | {sha[:10]} | {verdict} | review={review} | "
                    f"mergeable={mergeable} | updated {age_d:.1f}d ago{draft}"
                )
            prev = _snapshot(self.state, f"pr-state:{repo}")
            if prev is None:
                findings.append(f"{repo}: first sight, {len(prs)} open PR(s); the table is the row")
            else:
                for n in sorted(set(prev) | set(snap), key=int):
                    a, b = prev.get(n), snap.get(n)
                    if a is None:  # first sight of a PR: its full row, whatever state it was born in
                        findings.append(
                            f"{repo}#{n}: first sight: {b['title']} | {b['sha']} | {b['verdict']} | "
                            f"review={b['review'] or '-'} | mergeable={b['mergeable'] or '-'}"
                            + (" | STALE" if b["stale"] else "")
                        )
                    elif b is None:
                        if full:  # absent from a capped page: the PR itself says whether it is open
                            rc, st, err = await self.sh(
                                [GH, "pr", "view", n, "-R", repo, "--json", "state", "--jq", ".state"]
                            )
                            st = st.strip()
                            if rc != 0 or st not in ("OPEN", "CLOSED", "MERGED"):  # only a closure retires
                                control = False
                                why = (
                                    f"failed: {err.strip()[:120]}"
                                    if rc != 0
                                    else f"gave no state: {st[:60]!r}"
                                )
                                findings.append(f"{repo}#{n}: absent from a full page and gh pr view {why}")
                                snap[n] = a
                                continue
                            if st == "OPEN":
                                snap[n] = a
                                table.append(f"{repo}#{n}: open beyond the page; its last row is kept")
                                continue
                        findings.append(f"{repo}#{n}: closed or merged")
                    else:
                        for k in ("sha", "verdict", "review", "mergeable"):
                            if a.get(k) != b.get(k):
                                findings.append(f"{repo}#{n}: {k} {a.get(k) or '-'} -> {b.get(k) or '-'}")
                        if b["stale"] and not a.get("stale"):
                            findings.append(f"{repo}#{n}: untouched for more than {self.stale_days} days")
            self.state.put(f"pr-state:{repo}", snap, run="-", seq=0)
        return Outcome(
            findings=tuple(findings),
            evidence="\n".join(table),
            control=control,
            population=f"open PRs in {[r['name'] for r in repos]}",
            predicate=f"non-draft and updatedAt older than {self.stale_days} days ({stale_n} match)",
            count=pr_n,
            positive="; ".join(positives),
        )


class Served:
    """Served agents, artifact first: the cursor (the agent's own last-wake note) is younger than its
    bound, the pid in the pid file is alive, and a launchd label is running when it is loaded, in that
    order of weight (a clean launchd row beside a stale cursor is a finding). The control is this
    process's own pid through the same read. The remedy (a restart) is the owner's; this reports."""

    name = "served"
    about = "served agents: cursor age first, pid alive, launchd state; reports the stale and the dead"
    answers = (
        "is each served agent alive by its own last-wake note, its pid and its launchd row, "
        "read in that order of weight"
    )
    direction = "under-report"
    mechanism = "a pid read answers empty for a missing subject and an unloaded label prints nothing"

    def __init__(
        self, agents: list[dict], *, every: str | None, run: Runner = sh, empty_expected: bool = False
    ):
        self.agents, self.every, self.sh, self.empty_expected = list(agents), every, run, empty_expected

    async def _alive(self, pid: int) -> tuple[bool, str]:
        rc, out, _ = await self.sh(["/bin/ps", "-p", str(pid), "-o", "stat=,etime="])
        stat = out.strip()
        return bool(stat) and not stat.startswith("Z"), stat or "no such process"

    async def run(self, args: dict[str, Any]) -> Outcome:
        findings, lines, matched = [], [], 0
        alive, stat = await self._alive(os.getpid())
        control = alive
        positive = f"own pid {os.getpid()} reads {stat!r}" + ("" if alive else " (NOT alive: read broken)")
        lines.append("control: " + positive)
        for a in self.agents:
            d = Path(a["dir"])
            pid_file, line, bad = d / "agent.pid", f"{d}:", False
            if pid_file.exists():
                pid = pid_file.read_text().strip()
                ok, stat = await self._alive(int(pid)) if pid.isdigit() else (False, "unreadable pid file")
                line += f" pid {pid} {'alive' if ok else 'DEAD'} ({stat})"
                if not ok:
                    bad = True
                    findings.append(f"{d.name}: pid file names {pid} and it is not running ({stat})")
            else:
                line += " no pid file (not serving)"
            notes = d / "notes" / "agent.json"
            if notes.exists():
                cursor = (json.loads(notes.read_text()).get("notes", {}).get("cursor") or {}).get(
                    "value"
                ) or {}
                at = cursor.get("at")
                if at:
                    age_min = (time.time() - time.mktime(time.strptime(at, "%Y-%m-%d %H:%M:%S"))) / 60
                    line += f" | last wake {age_min:.0f} min ago ({cursor.get('outcome')})"
                    bound = a.get("stale_min")
                    if bound and age_min > bound and pid_file.exists():
                        bad = True
                        findings.append(
                            f"{d.name}: serving but last wake {age_min:.0f} min ago, bound {bound}"
                        )
            if a.get("label"):
                rc, out, _ = await self.sh([LAUNCHCTL, "print", f"gui/{os.getuid()}/{a['label']}"])
                m = re.search(r"^\s*state = (\w+)", out, re.M)
                state = m.group(1) if m else ("not loaded" if rc != 0 else "unknown")
                line += f" | launchd {a['label']}: {state}"
                if rc == 0 and state != "running":
                    bad = True
                    findings.append(f"{d.name}: launchd {a['label']} is loaded and {state}")
            matched += bad
            lines.append(line)
        return Outcome(
            findings=tuple(findings),
            evidence="\n".join(lines),
            control=control,
            population=f"served agents configured: {[a['dir'] for a in self.agents]}",
            predicate=f"cursor past its bound, pid dead, or launchd loaded and not running ({matched} match)",
            count=len(self.agents),
            positive=positive if control else "",
        )


class GtdInbox:
    """The assignee's gtd inbox rows past the triage SLA. Reads through khive as the agent (the
    assignee's namespace is visible to it); the control, in the same call, is that the assignee has
    any task at all, because an unshared namespace answers empty rather than failing."""

    name = "gtd-inbox"
    about = "the assignee's gtd inbox rows older than the SLA; reports them, never triages"
    answers = "which gtd rows assigned to the assignee are still in inbox past the triage SLA, and how old"
    direction = "under-report"
    mechanism = "an unshared namespace answers EMPTY, not an error; the control is any task at all"

    def __init__(
        self,
        khive: Khive,
        assignee: str,
        *,
        every: str | None,
        max_age_h: float = 24,
        empty_expected: bool = True,
    ):
        self.khive, self.assignee, self.every, self.max_age_h = khive, assignee, every, max_age_h
        self.empty_expected = empty_expected

    PAGE = 100

    async def run(self, args: dict[str, Any]) -> Outcome:
        rows, any_ = await self.khive.exec(  # one invocation: the answer and its known-positive
            f'[gtd.tasks(status="inbox", assignee={lit(self.assignee)}, limit={self.PAGE}), '
            f"gtd.tasks(assignee={lit(self.assignee)}, limit=1)]"
        )
        read, kind = isinstance(rows, list), type(rows).__name__  # a non-list is unread, never empty
        full = read and len(rows) >= self.PAGE  # a full page is a floor: the rows past it are unread
        control = isinstance(any_, list) and len(any_) > 0 and read and not full
        rows = rows if read else []
        old = [(r, _age_h(r["created_at"])) for r in rows if _age_h(r["created_at"]) > self.max_age_h]
        positive = (
            f"{self.assignee} has a task at all ({any_[0].get('id')}) in the call that listed the inbox"
            if control
            else ""
        )
        has = isinstance(any_, list) and len(any_) > 0
        lines = [f"control: {self.assignee} has {'a' if has else 'NO'} task at all; {len(rows)} in inbox"]
        if not read:
            lines.append(f"the inbox read answered {kind}, not a list of tasks")
        if full:
            lines.append(f"the inbox read returned a full page of {self.PAGE}; rows past it are unread")
        lines += [f"{r['id']} {r.get('priority', '')} {r['title'][:70]} ({age:.0f}h)" for r, age in old]
        findings = (
            (f"{len(old)} gtd inbox row(s) older than {self.max_age_h:.0f}h of {len(rows)} in inbox",)
            if old
            else ()
        )
        return Outcome(
            findings=findings,
            evidence="\n".join(lines),
            control=control,
            population=f"gtd tasks assigned to {self.assignee} with status inbox",
            predicate=f"created_at older than {self.max_age_h:.0f}h ({len(old)} match)",
            count=len(rows),
            positive=positive,
        )


class InboxSla:
    """Unread mail older than the SLA in each watched actor's inbox, by `comm.probe`: metadata and a
    stale count, no bodies, any actor. The control is the probe's own page: a live actor has mail, so
    an empty page with a zero count is a dead read, not a clean inbox."""

    name = "inbox-sla"
    about = "unread mail past the SLA per watched actor, by comm.probe; reports counts, reads no body"
    answers = "how many unread messages older than the SLA sit in each watched actor's inbox right now"
    direction = "under-report"
    mechanism = "comm.probe answers 0 and an empty page alike for a dead read and a clean inbox"

    def __init__(
        self,
        khive: Khive,
        actors: list[str],
        *,
        every: str | None,
        stale_minutes: int = 20,
        empty_expected: bool = True,
    ):
        self.khive, self.actors, self.every, self.stale_minutes = khive, list(actors), every, stale_minutes
        self.empty_expected = empty_expected

    async def run(self, args: dict[str, Any]) -> Outcome:
        actors = list(args.get("actors") or self.actors)
        ops = ", ".join(f"comm.probe(actor={lit(a)}, stale_minutes={self.stale_minutes})" for a in actors)
        results = await self.khive.exec(f"[{ops}]")
        findings, lines, positives, control, stale_total, paged = [], [], [], True, 0, 0
        for actor, r in zip(actors, results, strict=True):
            page = (r or {}).get("new_messages") or []
            stale = int((r or {}).get("stale_unread_count") or 0)
            if not page and not stale:
                control = False
                lines.append(f"{actor}: empty page and zero count (dead read or an actor with no mail)")
                continue
            newest = max((m.get("created_at_us") or 0) for m in page) if page else 0
            paged += len(page)
            positives.append(f"{actor}: page {len(page)}, stale count {stale}")
            lines.append(
                f"{actor}: {stale} unread older than {self.stale_minutes} min; "
                f"page {len(page)}, newest {newest}"
            )
            if stale:
                stale_total += stale
                findings.append(f"{actor}: {stale} unread message(s) older than {self.stale_minutes} min")
        return Outcome(
            findings=tuple(findings),
            evidence="\n".join(lines),
            control=control,
            population=f"messages on the probe pages of {actors}",
            predicate=f"unread and older than {self.stale_minutes} minutes ({stale_total} match)",
            count=paged,
            positive="; ".join(positives) if control else "",
        )


class Disk:
    """Free space per volume under the roots, against every floor the config names, and the build
    targets beneath the roots. Free space is a property of the volume, so roots on one device are
    grouped and each floor is read once per device with the roots named. A floor belongs to the
    instrument that enforces it (the local CI runner admits on free minus a provisioning budget; a
    steady-state figure is another), so each floor is read with its own budget and name in GiB
    base-1024, the unit floors are written in, with the base-1000 reading beside it. A crossing of an
    `enforcing` floor escalates because the remedy is a deletion; a crossing of any other floor
    reports to the owner. The finding is keyed on the floor's name and the value stays in the
    evidence, so a hovering number is told once until it recovers. Big targets are reported with
    their size; the owner measures occupancy before it removes anything."""

    name = "disk"
    about = "free space per volume against each named floor, in the floor's unit; targets over the cap"
    answers = (
        "is free space on each volume under the roots, minus each named floor's budget, above that "
        "floor in GiB base-1024; and which build targets under the roots exceed the size cap"
    )
    direction = "over-report"
    mechanism = "a target counted while a build holds it reports a size its owner removes when the work ends"

    def __init__(
        self,
        roots: list[str],
        *,
        every: str | None,
        floors: list[dict] | None = None,
        target_cap_gib: float = 20,
        run: Runner = sh,
        empty_expected: bool = False,
    ):
        self.roots, self.every, self.cap, self.sh = list(roots), every, target_cap_gib, run
        self.empty_expected = empty_expected
        self.floors = [dict(f) for f in DEFAULT_FLOORS] if floors is None else [dict(f) for f in floors]
        for fl in self.floors:
            if "floor_gib" not in fl or "name" not in fl:
                raise ValueError(f"a floor has a name and a floor_gib (base-1024): {fl}")
        # every comparison with nan is false, so a nan floor reads as above on every pass
        gib = [("target_cap_gib", target_cap_gib)] + [
            (f"{k} of floor {fl['name']!r}", fl[k])
            for fl in self.floors
            for k in ("floor_gib", "budget_gib")
            if k in fl
        ]
        for field, v in gib:
            if not isinstance(v, int | float) or isinstance(v, bool) or not math.isfinite(v) or v < 0:
                raise ValueError(f"{field} is a finite number of GiB, zero or more: {v!r}")

    async def run(self, args: dict[str, Any]) -> Outcome:
        findings, lines, control, escalate, matched = [], [], True, False, 0
        devices: dict[str, dict] = {}
        for root in self.roots:  # -P: one line per volume; GNU wraps a long device name without it
            rc, out, err = await self.sh(["/bin/df", "-P", "-k", root])
            rows = out.strip().splitlines()
            cols = rows[1].split() if len(rows) >= 2 else []
            if rc != 0 or len(cols) < 6 or not cols[3].isdigit():
                control = False
                findings.append(f"{root}: df gave no free-space number: {(err or out).strip()[:120]}")
                continue
            # the POSIX row: the mount point is everything past the fifth column, spaces and all
            mount = rows[1].split(None, 5)[5]
            dev = devices.setdefault(cols[0], {"free_kib": int(cols[3]), "mount": mount, "roots": []})
            dev["roots"].append(root)
        positive = (
            f"df parsed {len(devices)} device(s) for {len(self.roots)} root(s); floors read in GiB "
            "base-1024, the unit floors are written in"
            if devices
            else ""
        )
        lines.append(f"control: {positive or 'df parsed no device'}")
        for device, d in devices.items():
            gib, gb = d["free_kib"] / 2**20, d["free_kib"] * 1024 / 1e9
            roots = ", ".join(d["roots"])
            lines.append(
                f"{device} ({roots}): free {gib:.1f} GiB base-1024 / {gb:.1f} GB base-1000, "
                f"mounted on {d['mount']}"
            )
            for fl in self.floors:
                budget = float(fl.get("budget_gib", 0))
                projected = gib - budget
                under = projected < float(fl["floor_gib"])
                lines.append(
                    f"  floor {fl['name']}: {projected:.1f} GiB projected (free minus {budget:g} GiB "
                    f"budget) against {fl['floor_gib']} GiB: {'UNDER' if under else 'above'}"
                    + (" [enforcing]" if fl.get("enforcing") else "")
                )
                if under:
                    matched += 1
                    findings.append(f"{fl['name']}: under the floor on {device} ({roots})")
                    if fl.get("enforcing"):
                        escalate = True
        targets: dict[str, None] = {}  # one entry per target however many roots reach it
        for root in self.roots:
            rc, out, err = await self.sh(
                [
                    "/usr/bin/find",
                    root,
                    "-maxdepth",
                    "4",
                    "-type",
                    "d",
                    "(",
                    "-name",
                    "target",
                    "-o",
                    "-name",
                    "node_modules",
                    ")",
                    "-prune",
                    "-print0",
                ]
            )
            if rc != 0:  # a partial enumeration is not an answer; what it did find is still sized
                control = False
                findings.append(f"{root}: find failed (rc {rc}): {err.strip()[:120]}")
            targets.update(dict.fromkeys(p for p in out.split("\0") if p))
        for d in sorted(targets):
            rc, out2, err2 = await self.sh(["/usr/bin/du", "-sk", d], timeout=120)
            cols = out2.split()
            if rc != 0 or not cols or not cols[0].isdigit():
                control = False
                findings.append(f"{d}: du failed (rc {rc}): {(err2 or out2).strip()[:120]}")
                continue
            size = int(cols[0]) / 2**20
            lines.append(f"  {d}: {size:.1f} GiB")
            if size > self.cap:
                findings.append(f"{d}: over the {self.cap:g} GiB target cap")
        return Outcome(
            findings=tuple(findings),
            evidence="\n".join(lines),
            control=control,
            escalate=escalate,
            population=f"volumes under {self.roots}",
            predicate=f"free minus budget under a named floor, in GiB ({matched} match)",
            count=len(devices),
            positive=positive if control else "",
        )


# an origin names GitHub only in its host: https or ssh with github.com as the authority, or scp-style
_GITHUB_ORIGIN = re.compile(
    r"(?:(?:https?|ssh|git)://(?:[^@/\s]+@)?github\.com(?::\d+)?/|[^@/:\s]+@github\.com:)"
    r"([^/\s]+/[^/\s]+?)(?:\.git)?/?"
)


def _pr_number(p: Any) -> bool:
    return isinstance(p, dict) and isinstance(p.get("number"), int) and not isinstance(p["number"], bool)


class Worktrees:
    """Worktrees per repo: branch, dirty count, HEAD age, an open PR for the branch. A tree first seen
    emits its whole row; after that the transitions: a branch or PR change, a tree gone, a tree
    crossing into parked (dirty, days old, no PR) once, with its age in the evidence."""

    name = "worktrees"
    about = "worktrees: branch, dirty, HEAD age, open PR; reports parked-dirty trees and changes"
    answers = (
        "which worktrees of each repo exist beyond the main checkout, on which branch, how dirty, how "
        "old at HEAD, with which open PR; and which of them crossed into parked since the last run"
    )
    direction = "over-report"
    mechanism = "a tree a spawn is writing into reads dirty and PR-less until the spawn pushes"

    def __init__(
        self,
        repos: list[str],
        state: NoteStore,
        *,
        every: str | None,
        parked_days: float = 3,
        run: Runner = sh,
        empty_expected: bool = False,
    ):
        self.repos, self.state, self.every, self.parked_days, self.sh = (
            list(repos),
            state,
            every,
            parked_days,
            run,
        )
        self.empty_expected = empty_expected

    async def run(self, args: dict[str, Any]) -> Outcome:
        findings, lines, positives, control, total, parked_n = [], [], [], True, 0, 0
        for repo in self.repos:
            rc, out, err = await self.sh([GIT, "-C", repo, "worktree", "list", "--porcelain", "-z"])
            if rc != 0:
                control = False
                findings.append(f"{repo}: git worktree list failed: {err.strip()[:120]}")
                continue
            rc, slug_out, slug_err = await self.sh([GIT, "-C", repo, "remote", "get-url", "origin"])
            m = _GITHUB_ORIGIN.fullmatch(slug_out.strip())
            slug = m.group(1) if rc == 0 and m else None
            if slug is None:  # no repository to ask, so every branch's PR state is unread
                control = False
                why = (
                    f"failed: {slug_err.strip()[:120]}"
                    if rc != 0
                    else f"names no GitHub repo: {slug_out.strip()[:120]}"
                )
                findings.append(f"{repo}: origin {why}")
            # -z: each attribute ends in NUL and each record in one more, so a path holding a line
            # break stays whole
            trees = [
                dict(f.split(" ", 1) for f in block.split("\0") if " " in f)
                for block in out.split("\0\0")
                if block
            ]
            if not trees:  # the main checkout always lists itself: an empty list is a broken read
                control = False
                findings.append(f"{repo}: git worktree list named no tree, not even the main checkout")
                continue
            positives.append(f"{repo} lists {len(trees)} tree(s) incl. the main checkout; origin {slug}")
            lines.append(f"control: {positives[-1]}")
            prev = _snapshot(self.state, f"worktrees:{repo}")
            snap = {}
            for t in trees[1:]:  # the first entry is the main checkout
                path, branch = (
                    t.get("worktree", "?"),
                    (t.get("branch") or "detached").removeprefix("refs/heads/"),
                )
                failed = []
                rc, st, _ = await self.sh([GIT, "-C", path, "status", "--porcelain"])
                dirty = len([ln for ln in st.splitlines() if ln.strip()]) if rc == 0 else -1
                if rc != 0:
                    failed.append("status")
                rc, ts, _ = await self.sh([GIT, "-C", path, "log", "-1", "--format=%ct"])
                logged = rc == 0 and ts.strip().isdigit()
                age_d = (time.time() - int(ts.strip())) / 86400 if logged else -1
                if not logged:
                    failed.append("log")
                pr = "?"
                if slug is None and branch != "detached":
                    failed.append("PR")
                elif branch != "detached":
                    rc, prs, _ = await self.sh(
                        [
                            GH,
                            "pr",
                            "list",
                            "-R",
                            slug,
                            "--head",
                            branch,
                            "--state",
                            "open",
                            "--json",
                            "number",
                        ]
                    )
                    try:
                        listed = json.loads(prs) if rc == 0 else None
                    except ValueError:  # a JSONDecodeError, or an integer past the conversion limit
                        listed = None
                    if isinstance(listed, list) and all(_pr_number(p) for p in listed):
                        pr = ",".join(f"#{p['number']}" for p in listed) or "none"
                    else:  # a failed call and an answer that is not a list of PRs are both unread
                        pr = "gh failed" if rc != 0 else "not a PR list"
                        failed.append("PR")
                parked = dirty > 0 and age_d > self.parked_days and pr == "none"
                lines.append(
                    f"{path} | {branch} | dirty {dirty} | HEAD {age_d:.1f}d | PR {pr}"
                    + (" | PARKED" if parked else "")
                )
                if failed:  # the last good reading stands until a read succeeds
                    control = False
                    findings.append(f"{path}: the {', '.join(failed)} read failed; the tree is unread")
                    if path in (prev or {}):
                        snap[path] = prev[path]
                    continue
                parked_n += parked
                snap[path] = {"branch": branch, "dirty": dirty, "pr": pr, "parked": parked}
            total += len(snap)
            if prev is None:
                findings.append(f"{repo}: first snapshot, {len(snap)} worktree(s)")
            for p in sorted(set(prev or {}) | set(snap)):
                a, b = (prev or {}).get(p), snap.get(p)
                if b is None:
                    findings.append(f"{p}: worktree gone")
                elif a is None:
                    findings.append(
                        f"{p}: new worktree on {b['branch']}"
                        + (", parked: dirty, old, no PR" if b["parked"] else "")
                        if prev is not None
                        else f"{p} ({b['branch']}): parked: dirty, HEAD older than {self.parked_days:g} "
                        "days, no open PR"
                        if b["parked"]
                        else ""
                    )
                else:
                    if a.get("pr") != b["pr"] or a.get("branch") != b["branch"]:
                        was, now_ = f"{a.get('branch')}/{a.get('pr')}", f"{b['branch']}/{b['pr']}"
                        findings.append(f"{p}: {was} -> {now_}")
                    if b["parked"] and not a.get("parked"):
                        findings.append(
                            f"{p} ({b['branch']}): parked: dirty, HEAD older than {self.parked_days:g} "
                            "days, no open PR"
                        )
            findings = [f for f in findings if f]
            self.state.put(f"worktrees:{repo}", snap, run="-", seq=0)
        return Outcome(
            findings=tuple(findings),
            evidence="\n".join(lines),
            control=control,
            population=f"worktrees beyond the main checkout of {self.repos}",
            predicate=f"dirty, HEAD older than {self.parked_days:g} days, no open PR ({parked_n} match)",
            count=total,
            positive="; ".join(positives) if control else "",
        )


class BenchJobs:
    """Bench jobs the bench actor launched and never reported whose process is gone: a finish mail
    that never arrived, or a run that died before its ledger. Reads the bench actor's own notes."""

    name = "bench-jobs"
    about = "bench jobs launched, unreported, process gone; reports them"
    answers = "which bench jobs the bench actor noted as launched are unreported with their process gone"
    direction = "under-report"
    mechanism = "a job the bench actor never noted is invisible here; the notes are the population"

    def __init__(
        self,
        bench_dir: str,
        results: str,
        *,
        every: str | None,
        run: Runner = sh,
        empty_expected: bool = True,
    ):
        self.dir, self.results, self.every, self.sh = Path(bench_dir), Path(results), every, run
        self.empty_expected = empty_expected

    async def run(self, args: dict[str, Any]) -> Outcome:
        notes = self.dir / "notes" / "agent.json"
        if not notes.exists():
            return Outcome(
                findings=(f"no bench notes at {notes}",), evidence=f"{notes}: absent", control=False
            )
        jobs = (json.loads(notes.read_text()).get("notes", {}).get("jobs") or {}).get("value") or {}
        positive = f"{notes} read, {len(jobs)} job(s) noted"
        findings, lines, gone = [], [f"control: {positive}"], 0
        for run_id, job in sorted(jobs.items()):
            if job.get("reported"):
                continue
            rc, out, _ = await self.sh(["/bin/ps", "-p", str(job.get("pid")), "-o", "stat="])
            alive = bool(out.strip()) and not out.strip().startswith("Z")
            ledger = self.results / run_id / "ledger.jsonl"
            rows = len([ln for ln in ledger.read_text().splitlines() if ln.strip()]) if ledger.exists() else 0
            state = "alive" if alive else "gone"
            lines.append(
                f"{run_id}: pid {job.get('pid')} {state}, started {job.get('started')}, ledger rows {rows}"
            )
            if not alive:
                gone += 1
                findings.append(
                    f"job {run_id}: unreported, pid {job.get('pid')} gone, {rows} ledger row(s); "
                    f"asked by {job.get('asker')}"
                )
        return Outcome(
            findings=tuple(findings),
            evidence="\n".join(lines),
            control=True,
            population=f"bench jobs noted in {notes}",
            predicate=f"unreported and process gone ({gone} match)",
            count=len(jobs),
            positive=positive,
        )


# a check-run's details_url is .../actions/runs/<run>/job/<job>: the run id, never the trailing job id
_RUN_URL = re.compile(r"/actions/runs/(\d+)")
# a name whose rows cannot be ordered reads pending, never the green one of them
_WAITING = frozenset(
    {"pending", "queued", "in_progress", "waiting", "requested", "expected", "ordering unresolved"}
)


def _conclusion(raw: str | None) -> str:
    return "success" if raw == "success" else "pending" if raw in _WAITING else "failure"


class RequiredContexts:
    """The contexts each open PR's base requires, by name, from both surfaces (rulesets and classic
    protection; one repo can use both), and the state of each at the PR's current head. A required name
    with no row on either surface is ABSENT, never pending and never green. A classic 404 without admin
    is permission, not absence, so classic reads UNREAD; an empty required set is a value only when the
    rulesets read. Each PR's read is a check inventory observation in the outcome's data, with its
    envelope: who observed, on which event, over which interval, how complete, and a digest of the
    rows. A row's app is the producer observed at the head (a check-run's
    app id; null for a status, which carries none) and required_app the rule's integration beside it.
    First sight is the full row, later runs the transitions."""

    name = "required-contexts"
    about = "required contexts by name on each open PR's base and their state at its head; transitions"
    answers = (
        "for each open PR of each repo: which contexts its base requires, by name and rule source, and "
        "which of them are green, failing, pending or absent at the PR's current head"
    )
    direction = "under-report"
    mechanism = (
        "a context posted under a required name by any producer counts; its declared app is not enforced"
    )

    def __init__(
        self,
        repos: list[dict],
        state: NoteStore,
        *,
        every: str | None,
        source_identity: str,
        run: Runner = sh,
        empty_expected: bool = False,
    ):
        self.repos = [{"base": "main", **r} if isinstance(r, dict) else {"name": r} for r in repos]
        bad = [r for r in self.repos if not isinstance(r.get("classic"), bool)]
        if bad:
            raise ValueError(
                f"a repo declares whether classic protection is expected (classic = true|false): {bad}"
            )
        self.state, self.every, self.source_identity, self.sh = state, every, source_identity, run
        self.empty_expected = empty_expected

    async def _at_head(self, repo: str, sha: str) -> tuple[dict[str, dict], list[str], bool]:
        """Each context posted at the head, the newest per name across both surfaces, with what
        produced it; the reads that failed or were capped, and the names nothing orders; whether both
        surfaces answered empty."""
        seen: dict[str, dict] = {}
        gaps: list[str] = []
        rc, out, err = await self.sh(
            [
                GH,
                "api",
                f"repos/{repo}/commits/{sha}/status?per_page=100",
                "--jq",
                "{total: .total_count, rows: [.statuses[] | {context, state, created_at}]}",
            ]
        )
        statuses = json.loads(out) if rc == 0 else None
        if statuses is None:
            gaps.append(f"statuses unread: {err.strip()[:120]}")
        else:
            if statuses["total"] > len(statuses["rows"]):
                gaps.append(f"statuses capped: {len(statuses['rows'])} of {statuses['total']} read")
            for s in statuses["rows"]:  # newest first per context
                if s["context"] not in seen:
                    seen[s["context"]] = {
                        "at": s["created_at"],
                        "raw": s["state"],
                        "app": None,  # a status carries no producing app
                        "app_slug": None,
                        "workflow_id": None,
                        "event": None,
                        "run_id": None,
                        "completed_at": None if s["state"] == "pending" else s["created_at"],
                    }
        rc, out, err = await self.sh(
            [
                GH,
                "api",
                f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
                "--jq",
                "{total: .total_count, rows: [.check_runs[] | {name, app: .app.id, app_slug: .app.slug, "
                "status, conclusion, details_url, started_at, completed_at}]}",
            ]
        )
        checks = json.loads(out) if rc == 0 else None
        if checks is None:
            gaps.append(f"check-runs unread: {err.strip()[:120]}")
        else:
            if checks["total"] > len(checks["rows"]):
                gaps.append(f"check-runs capped: {len(checks['rows'])} of {checks['total']} read")
            ids = {
                int(m.group(1)) for c in checks["rows"] if (m := _RUN_URL.search(c.get("details_url") or ""))
            }
            by_id: dict[int, dict] = {}
            if ids:  # a check-run's workflow and event live on its actions run
                rc, err, rows, _, _ = await _runs_at(self.sh, repo, sha)
                by_id = {r["id"]: r for r in rows}
                if rc != 0:
                    gaps.append(f"actions runs unread: {err.strip()[:120]}")
                elif ids - set(by_id):
                    gaps.append(f"run(s) {sorted(ids - set(by_id))} not among the head's runs read")
            unordered: set[str] = set()
            tied: set[str] = set()  # the newest rows of a name share one stamp and disagree
            for c in checks["rows"]:
                if c["name"] in unordered:
                    continue
                m = _RUN_URL.search(c.get("details_url") or "")
                rid = int(m.group(1)) if m else None
                wr = by_id.get(rid, {})
                # a queued check has no started_at: its run's creation orders it; with neither, nothing does
                at = c.get("started_at") or wr.get("created_at")
                raw = c["conclusion"] if c["status"] == "completed" else c["status"]
                prior = seen.get(c["name"])
                if prior is not None and (at is None or prior["at"] is None):
                    unordered.add(c["name"])
                    gaps.append(f"{c['name']}: ordering unresolved")
                    seen[c["name"]] = dict.fromkeys(prior, None) | {"raw": "ordering unresolved"}
                    continue
                if prior is not None and prior["at"] >= at:
                    if prior["at"] == at and _conclusion(prior["raw"]) != _conclusion(raw):
                        tied.add(c["name"])  # one stamp orders neither; a strictly newer row still wins
                    continue
                tied.discard(c["name"])
                seen[c["name"]] = {
                    "at": at,
                    "raw": raw,
                    "app": c.get("app"),
                    "app_slug": c.get("app_slug"),
                    "workflow_id": wr.get("workflow_id"),
                    "event": wr.get("event"),
                    "run_id": rid,
                    "completed_at": c.get("completed_at"),
                }
            for name in sorted(tied - unordered):
                gaps.append(f"{name}: ordering unresolved")
                seen[name] = dict.fromkeys(seen[name], None) | {"raw": "ordering unresolved"}
        empty = statuses is not None and checks is not None and not statuses["rows"] and not checks["rows"]
        return seen, gaps, empty

    async def run(self, args: dict[str, Any]) -> Outcome:
        wanted = {str(p) for p in args.get("prs") or []}  # an ask narrowed to PRs: rows, no transitions
        event = str(args.get("source_event") or "direct read (no chore run)")
        findings, table, positives, data, control, pr_n, red_n = [], [], [], [], True, 0, 0
        for spec in self.repos:
            repo, declared = spec["name"], spec["base"]
            start = _utc()
            info, err = await _resolve(self.sh, repo)
            if info is None:
                control = False
                findings.append(f"{repo}: gh does not resolve it as itself: {err}")
                continue
            admin = info.get("admin")
            table.append(f"control: gh resolves {repo} as itself (id {info.get('id')}); admin {admin}")
            if not isinstance(admin, bool):
                control = False
                findings.append(f"{repo}: admin not read ({admin!r}); a classic 404 cannot be told from none")
            prs, err = await _open_prs(self.sh, repo, "number,headRefOid,baseRefName")
            if prs is None:
                control = False
                findings.append(f"{repo}: {err}")
                continue
            reads: dict[str, tuple[list[dict] | None, dict[str, str]]] = {}
            for base in [declared] + sorted({p["baseRefName"] for p in prs} - {declared}):
                rules, surf = reads[base] = await _required(self.sh, repo, base, admin)
                names = (
                    "UNREAD" if rules is None else ", ".join(f"{r['context']} [{r['source']}]" for r in rules)
                )
                table.append(
                    f"{repo} {base}: required {names or 'none'}; ruleset {surf['ruleset']}; "
                    f"classic {surf['classic']}"
                )
                if not any(s.startswith("read") for s in surf.values()):
                    control = False
                    findings.append(f"{repo} {base}: neither surface read; the required set is UNREAD")
                elif unread := [f"{k} {v}" for k, v in surf.items() if v.startswith("UNREAD")]:
                    findings.append(f"{repo} {base}: {'; '.join(unread)}; the required set is partial")
            rules, surf = reads[declared]
            has_classic = any(r["source"] == "classic" for r in rules or [])
            if spec["classic"] and surf["classic"].startswith("none"):
                findings.append(f"{repo} {declared}: classic protection declared and none is there")
            elif not spec["classic"] and has_classic:
                findings.append(
                    f"{repo} {declared}: classic protection requires contexts and none is declared"
                )
            if any(s.startswith("read") for s in surf.values()):
                positives.append(
                    f"{repo} resolved as itself (id {info.get('id')}), admin {admin}; {declared}: ruleset "
                    f"{surf['ruleset']}, classic {surf['classic']}; pr list answered {len(prs)} row(s)"
                )
            prev = _snapshot(self.state, f"required-contexts:{repo}") or {}
            snap: dict[str, str] = {}
            for pr in prs:
                n, sha = str(pr["number"]), pr.get("headRefOid") or ""
                if wanted and f"{repo}#{n}" not in wanted:
                    continue
                if not re.fullmatch(r"[0-9a-f]{40}", sha):
                    control = False
                    findings.append(f"{repo}#{n}: head not read ({sha!r})")
                    if n in prev:
                        snap[n] = prev[n]
                    continue
                pr_n += 1
                rules, surf = reads[pr["baseRefName"]]
                observed_at = _utc()
                seen, gaps, empty = await self._at_head(repo, sha) if rules is not None else ({}, [], False)
                if gaps:
                    control = False
                rows = None
                if rules is None:
                    line = f"{repo}#{n} {sha[:8]}: required set UNREAD on {pr['baseRefName']}; nothing judged"
                else:
                    rows = []
                    for r in rules:
                        o = seen.get(r["context"]) or {}
                        rows.append(
                            {
                                "context": r["context"],
                                "source": r["source"],
                                "base": pr["baseRefName"],
                                # app is the producer observed at the head; the rule's integration
                                # is what it requires, never evidence of who produced the row
                                "app": o.get("app"),
                                "app_slug": o.get("app_slug"),
                                "required_app": r["app"],
                                "workflow_id": o.get("workflow_id"),
                                "event": o.get("event"),
                                "run_id": o.get("run_id"),
                                "conclusion": _conclusion(o["raw"]) if o else "ABSENT",
                                "completed_at": o.get("completed_at"),
                            }
                        )
                    names = sorted({r["context"] for r in rules})
                    state = {c: _conclusion(seen[c]["raw"]) if c in seen else "ABSENT" for c in names}
                    green = [c for c in names if state[c] == "success"]
                    missing = [c for c in names if state[c] == "ABSENT"]
                    failing = [f"{c}={seen[c]['raw']}" for c in names if state[c] == "failure"]
                    pending = [f"{c}={seen[c]['raw']}" for c in names if state[c] == "pending"]
                    line = (
                        f"{repo}#{n} {sha[:8]}: {len(green)}/{len(names)} by name; "
                        f"missing: [{', '.join(missing)}]; failing: [{', '.join(failing)}]"
                    )
                    line += f"; pending: [{', '.join(pending)}]" if pending else ""
                    line += f" (no context required on {pr['baseRefName']})" if not names else ""
                    line += "; REQUIRED CHECKS DO NOT EXIST AT THIS HEAD" if empty and names else ""
                    if len(green) < len(names):
                        red_n += 1
                line += f"; head read incomplete: {'; '.join(gaps)}" if gaps else ""
                red_n += rules is None
                snap[n] = line
                table.append(line)
                table += [
                    f"  {r['context']} [{r['source']}] {r['conclusion']} app={r['app']} "
                    f"required_app={r['required_app']} event={r['event']} "
                    f"run={r['run_id']}"
                    for r in rows or []
                ]
                reasons = [f"{k} {v}" for k, v in surf.items() if v.startswith("UNREAD")] + gaps
                payload = {
                    "repository": info.get("id"),
                    "repository_name": repo,
                    "head": sha,
                    "required": rows,
                }
                canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                data.append(
                    {
                        **payload,
                        "observed_at": observed_at,
                        "envelope": {
                            "source_identity": self.source_identity,
                            "source_event": event,
                            "interval": {"start": start, "end": _utc()},
                            # complete | partial | unavailable (nothing read for the PR)
                            "completeness": "unavailable"
                            if rows is None
                            else "partial"
                            if reasons
                            else "complete",
                            "reason": "; ".join(reasons) or None,
                            "payload_digest": hashlib.sha256(canonical.encode()).hexdigest(),
                        },
                    }
                )
            if wanted:  # an on-demand read: every named row, and the snapshot untouched
                findings += [snap[k] for k in sorted(snap, key=int)]
                findings += [
                    f"{w}: not among the open PRs read"
                    for w in sorted(wanted)
                    if w.startswith(f"{repo}#") and w.split("#", 1)[1] not in snap
                ]
                continue
            for k in sorted(set(prev) | set(snap), key=int):
                a, b = prev.get(k), snap.get(k)
                if b is None:
                    if len(prs) >= PR_PAGE:  # absent from a full page is not closed: its last row is kept
                        snap[k] = a
                    else:
                        findings.append(f"{repo}#{k}: no longer open")
                elif a != b:  # first sight of a PR is its full row, whatever state it was born in
                    findings.append(b)
            self.state.put(f"required-contexts:{repo}", snap, run="-", seq=0)
        return Outcome(
            findings=tuple(findings),
            evidence="\n".join(table),
            control=control,
            population=f"open PRs in {[r['name'] for r in self.repos]}"
            + (f" named {sorted(wanted)}" if wanted else ""),
            predicate=f"a required context not green at the head, or the required set unread ({red_n} match)",
            count=pr_n,
            positive="; ".join(positives) if control else "",
            data=tuple(data),
        )


_VERDICT_FILE = re.compile(r"^(.+-\d+)-([0-9a-f]{8})\.(json|md)$")
_TOTAL = re.compile(
    r"^TOTAL (\d+) open PRs authored by (\S+) \(control: closed-PR search returned (\d+)", re.M
)
_LISTED = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)\b", re.M)


class VerdictFreshness:
    """The newest review verdict of each open PR against the PR's head now: AT-HEAD, STALE (both sha8
    shown) or NEVER-REVIEWED, with its decision, blocking count (asserted against the verdict's own
    count), degraded flag and path. The JSON is read, the .md header only when the JSON is missing, and
    the row says which. A file counts for a PR only when its own repo and number say so, since a
    `<owner>-<repo>-<n>` name can be two repos. The PRs are the repos' open PRs, or an enumerator's
    output: a command the chores.toml names, run as given, whose last line must be its TOTAL line with a
    closed-PR control of at least one, so a failed search never reads as zero PRs. The control is the
    world's own newest verdict: the directory exists and its newest verdict parses, read as a PR's is.
    First sight is the row, then transitions."""

    name = "verdict-freshness"
    about = "the newest review verdict per open PR against its head now: AT-HEAD, STALE, NEVER-REVIEWED"
    answers = (
        "for each open PR: does its newest review verdict evaluate the PR's current head, and with which "
        "decision, blocking count and degraded flag"
    )
    direction = "over-report"
    mechanism = "the newest verdict is chosen by mtime, so an older verdict rewritten later reads as newest"

    def __init__(
        self,
        state: NoteStore,
        *,
        every: str | None,
        verdict_dir: str | None = VERDICT_DIR,
        repos: list[str] | None = None,
        enumerator: list[str] | None = None,
        run: Runner = sh,
        empty_expected: bool = False,
    ):
        if bool(repos) == bool(enumerator):
            raise ValueError("verdict-freshness reads `repos` or an `enumerator`, exactly one of them")
        if not verdict_dir:
            raise ValueError("verdict-freshness reads verdicts from a verdict_dir the chore names")
        self.state, self.every, self.dir, self.sh = state, every, Path(verdict_dir), run
        self.repos, self.enumerator = list(repos or []), list(enumerator or [])
        self.empty_expected = empty_expected

    @staticmethod
    def _read(files: dict[str, tuple[str, float]]) -> dict[str, Any]:
        """One verdict: its JSON, the .md header only when the JSON is missing; an unreadable one
        carries its error."""
        path, mtime = files.get("json") or files["md"]
        rec: dict[str, Any] = {"path": path, "mtime": mtime, "round_no": -1}
        try:
            text = Path(path).read_text()
            if "json" in files:
                d = json.loads(text)
                head = d["evaluated_head"]
                if not isinstance(head, str) or not re.fullmatch(
                    r"[0-9a-f]{40}", head
                ):  # an identity, or unread
                    raise ValueError(f"evaluated_head {str(d['evaluated_head'])[:48]!r} is not a full sha")
                rec |= {
                    "read": "json",
                    "repo": d["repo"],
                    "number": d["number"],
                    "evaluated_head": d["evaluated_head"],
                    "decision": d["decision"],
                    "blocking": len(d.get("blocking") or []),
                    "asserted": d.get("asserted_blocking"),
                    "degraded": bool(d.get("degraded")),
                    "round_no": d.get("round_no") or -1,
                    "cause": str(d.get("cause") or "unstated"),
                    "coverage": d.get("coverage_bounds"),
                }
            else:
                head = re.match(r"# Review verdict \u2014 (\S+)#(\d+) @ [0-9a-f]{8}", text)
                decision = re.search(r"^- decision: \*\*([^*]+)\*\*", text, re.M)
                ev = re.search(r"^- evaluated head: ([0-9a-f]{40})\s*$", text, re.M)
                if not (head and decision and ev):
                    raise ValueError("no verdict header (title, decision and evaluated head lines)")
                block = re.search(r"^## Blocking\n(.*?)(?=^## |\Z)", text, re.M | re.S)
                bullets = [ln for ln in (block.group(1) if block else "").splitlines() if ln.startswith("- ")]
                cause = re.search(r"^- cause: (\S+)", text, re.M)
                rec |= {
                    "read": "md header (no JSON)",
                    "repo": head.group(1),
                    "number": int(head.group(2)),
                    "evaluated_head": ev.group(1),
                    "decision": decision.group(1),
                    "blocking": 0 if bullets == ["- none"] else len(bullets),
                    "asserted": None,
                    "degraded": bool(re.search(r"^- degraded: True", text, re.M)),
                    "cause": cause.group(1) if cause else "unstated",
                }
        except (OSError, ValueError, KeyError, TypeError) as e:
            rec["error"] = f"{type(e).__name__}: {e}"
        return rec

    def _newest(self, heads: dict[str, dict[str, tuple[str, float]]], repo: str, n: int) -> dict | None:
        """The newest of a PR's verdicts by mtime, then round; a file whose own repo and number are
        another PR's is not a candidate, an unreadable one stays one (it may be the newest)."""
        recs = [self._read(files) for files in heads.values()]
        recs = [r for r in recs if "error" in r or (r["repo"] == repo and r["number"] == n)]
        return max(recs, key=lambda r: (r["mtime"], r["round_no"]), default=None)

    async def run(self, args: dict[str, Any]) -> Outcome:
        try:
            entries = [(e.name, e.path, e.stat().st_mtime) for e in os.scandir(self.dir)]
        except OSError as e:
            return Outcome(
                findings=(f"{self.dir}: unreadable ({e}); no PR is judged",), evidence=str(e), control=False
            )
        index: dict[str, dict[str, dict[str, tuple[str, float]]]] = {}
        for name, p, m in entries:
            if v := _VERDICT_FILE.match(name):
                index.setdefault(v.group(1), {}).setdefault(v.group(2), {})[v.group(3)] = (p, m)
        # the known-positive from the world: the daemon's newest verdict parses, read as a PR's is read
        pairs = [files for heads in index.values() for files in heads.values()]
        newest = self._read(max(pairs, key=lambda f: (f.get("json") or f["md"])[::-1])) if pairs else None
        name = Path(newest["path"]).name if newest else ""
        if newest is None or "error" in newest or not isinstance(newest["evaluated_head"], str):
            why = (
                f"its newest {'JSON' if name.endswith('.json') else '.md header'} {name} does not parse"
                if newest
                else f"no verdict JSON in {len(entries)} entries"
            )
            return Outcome(
                findings=(f"{self.dir}: {why}; no PR is judged",), evidence=f"control: {why}", control=False
            )
        md = " (read md header)" if newest["read"] != "json" else ""
        positives = [f"{self.dir}: newest verdict {name} parses{md}; {len(entries)} entries"]
        findings, table, control = [], [f"control: {positives[0]}"], True
        prs: list[tuple[str, int, str]] = []
        unread: set[str] = set()  # PRs or repos whose last row is kept: this run could not read them
        if self.enumerator:
            rc, out, err = await self.sh(list(self.enumerator), timeout=180)
            last = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "")
            m = _TOTAL.match(last)  # the last line is the enumerator's verdict; an earlier TOTAL is not
            if rc != 0 or m is None:
                head = (err or out).strip().splitlines()
                why = f"rc {rc}, " + (
                    "TOTAL beside a failure"
                    if m
                    else "its last line is not the TOTAL line"
                    if _TOTAL.search(out)
                    else "no TOTAL line"
                )
                return Outcome(
                    findings=(f"ENUMERATION FAILED ({why}): {head[-1][:200] if head else 'no output'}",),
                    evidence=f"{self.enumerator}: {why}\n{out[-400:]}{err[-400:]}",
                    control=False,
                )
            if int(m.group(3)) < 1:
                return Outcome(
                    findings=(
                        f"ENUMERATION UNTRUSTED: the closed-PR control returned {m.group(3)}; "
                        "the open list is UNREAD, not empty",
                    ),
                    evidence=m.group(0),
                    control=False,
                )
            listed = _LISTED.findall(out)
            if len(listed) != int(m.group(1)):
                control = False
                findings.append(f"enumerator: TOTAL {m.group(1)} beside {len(listed)} PR line(s) parsed")
            positives.append(
                f"enumerator TOTAL {m.group(1)} open PR(s) by {m.group(2)}; closed-PR control {m.group(3)}"
            )
            for repo, n in listed:
                rc, out, err = await self.sh([GH, "pr", "view", n, "-R", repo, "--json", "headRefOid,state"])
                pr = json.loads(out) if rc == 0 else {}
                if pr.get("state") not in (None, "OPEN"):  # closed since the search indexed it
                    table.append(f"{repo}#{n}: listed open, {pr['state']} now; not judged")
                    continue
                if not re.fullmatch(r"[0-9a-f]{40}", pr.get("headRefOid") or ""):
                    control = False
                    unread.add(f"{repo}#{n}")
                    findings.append(f"{repo}#{n}: head not read: {err.strip()[:120] or out[:120]}")
                    continue
                prs.append((repo, int(n), pr["headRefOid"]))
        else:
            for repo in self.repos:
                info, err = await _resolve(self.sh, repo)
                rows, err = await _open_prs(self.sh, repo, "number,headRefOid") if info else (None, err)
                if rows is None:
                    control = False
                    unread.add(repo)
                    findings.append(f"{repo}: {err if info else 'gh does not resolve it as itself: ' + err}")
                    continue
                positives.append(f"{repo} resolved as itself; pr list answered {len(rows)} row(s)")
                prs += [(repo, p["number"], p["headRefOid"]) for p in rows]
        prev = _snapshot(self.state, "verdict-freshness") or {}
        snap = {k: v for k, v in prev.items() if k in unread or k.split("#")[0] in unread}
        data, stale_n = [], 0
        for repo, n, head in prs:
            key = f"{repo}#{n}"
            rec = self._newest(index.get(f"{repo.replace('/', '-')}-{n}", {}), repo, n)
            if rec is None:
                state, line = "NEVER-REVIEWED", f"{key} {head[:8]}: NEVER-REVIEWED"
            elif "error" in rec:
                control = False
                state, line = (
                    "UNREADABLE",
                    f"{key} {head[:8]}: newest verdict unreadable: {rec['path']}: {rec['error']}",
                )
            else:
                state = "AT-HEAD" if rec["evaluated_head"] == head else "STALE"
                line = f"{key} {head[:8]}: {state}"
                line += f" (verdict at {rec['evaluated_head'][:8]})" if state == "STALE" else ""
                line += f", {rec['decision']}, blocking {rec['blocking']}"
                if rec["asserted"] is not None and rec["asserted"] != rec["blocking"]:
                    line += f" (asserted_blocking {rec['asserted']} != {rec['blocking']} listed)"
                line += ", DEGRADED" if rec["degraded"] else ""
                line += f"; read {rec['read']}: {rec['path']}"
            stale_n += state != "AT-HEAD"
            snap[key] = line
            table.append(line)
            data.append({"repo": repo, "number": n, "head": head, "state": state, **(rec or {})})
        for k in sorted(set(prev) | set(snap)):
            a, b = prev.get(k), snap.get(k)
            if b is None:
                findings.append(f"{k}: no longer open")
            elif a != b:  # first sight of a PR is its full row
                findings.append(b)
        self.state.put("verdict-freshness", snap, run="-", seq=0)
        source = f"the enumerator {self.enumerator[0]}" if self.enumerator else f"{self.repos}"
        return Outcome(
            findings=tuple(findings),
            evidence="\n".join(table),
            control=control,
            population=f"open PRs from {source}",
            predicate=f"newest verdict not at the PR's current head, or none ({stale_n} match)",
            count=len(prs),
            positive="; ".join(positives) if control else "",
            data=tuple(data),
        )


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# a --log-failed line's own stamp, after its job and step columns
_STAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ?")
_RED_LINE = re.compile(r"\bFAIL(?:ED)?\b|panicked|error:|Error:|\bassert")
_PANIC_END = re.compile(r"^\s*(?:$|note:|stack backtrace:)")
_LOCAL_JOB = re.compile(r"^===== job (.+) =====$")
_LOCAL_VERDICT = re.compile(r"^===== (.+?): (\w+) -- (.*)")
# a local-ci step's result line: rc=N, with or without its step number, or a timeout
_LOCAL_STEP = re.compile(r"^\[cwd=[^\]]*? (?:rc=(-?\d+)(?: #\d+)?|TIMED OUT[^\]]*)\]")


def _keep(lines: list[str]) -> list[str]:
    """One job's red lines: every line the matcher hits, and the detail block of its first panic."""
    kept, panic = [], 0  # 0 before the first panic, 1 inside its block, 2 after it
    for line in lines:
        if panic == 1 and (_PANIC_END.match(line) or "panicked" in line):
            panic = 2
        if panic == 1 or _RED_LINE.search(line):
            kept.append(line)
        if panic == 0 and "panicked" in line:
            panic = 1
    return kept


class CiRed:
    """The red lines at each open PR's head, on the channel the repo declares. `actions` takes the
    newest run per workflow at the head (PrState's read) and, for each run concluded failure, gh's
    --log-failed text; `local:<dir>` takes the head's own local-ci transcript, <dir>/pr-<n>/*-<head8>.log
    (never latest.log, which names whatever ran last), and for each job whose verdict is failure or
    error, its failing step, that step's rc line, the red lines and the verdict line. The extract keeps
    every line it matched, deduplicated per PR with the drop counted; only the outcome's rendering
    bounds it. A head with no red is not a finding; a read that failed is, and so is a local head with
    no log of its own, a transcript with no job section yet, or a local job with no verdict yet or
    DISCARDED, whose last red is kept. First sight is the red rows, later runs the transitions."""

    name = "ci-red"
    about = "the red lines at each open PR's head on the repo's CI channel; transitions"
    answers = (
        "for each open PR of each repo whose head is red on its declared CI channel: which runs and jobs "
        "failed there, and the failing lines of each"
    )
    direction = "under-report"
    mechanism = (
        "only a run concluded failure, or a local job whose verdict is failure or error, is extracted; "
        "a timed_out, cancelled or startup_failure run is listed in the evidence and never extracted"
    )

    def __init__(
        self,
        repos: list[dict],
        state: NoteStore,
        *,
        every: str | None,
        run: Runner = sh,
        empty_expected: bool = False,
    ):
        self.repos = [dict(r) for r in repos]
        bad = [
            r
            for r in self.repos
            if r.get("channel") != "actions" and not str(r.get("channel")).startswith("local:")
        ]
        if bad:
            raise ValueError(f"a repo's channel is actions or local:<log dir>: {bad}")
        self.state, self.every, self.sh, self.empty_expected = state, every, run, empty_expected

    async def _actions(self, repo: str, sha: str) -> tuple[list[dict], list[str], str]:
        """Each failed job of each red run at the head with its red lines; the reads that failed; what
        the head's runs read as."""
        rc, err, _, newest, short = await _runs_at(self.sh, repo, sha)
        if rc != 0:
            return [], [f"the runs read failed: {err.strip()[:120]}"], "runs UNREAD"
        entries, gaps = [], [short] if short else []
        label = _labels(newest)
        order = sorted(newest, key=lambda w: (label[w], w))
        for w in order:
            r = newest[w]
            if r["conclusion"] != "failure":
                continue
            rc, out, err = await self.sh(
                [GH, "run", "view", str(r["id"]), "-R", repo, "--log-failed"], timeout=180
            )
            if rc != 0 or not out.strip():
                why = f"rc {rc}: {err.strip()[:120]}" if rc else "returned nothing"
                gaps.append(f"{label[w]} run {r['id']}: --log-failed {why}")
                continue
            jobs: dict[str, list[str]] = {}
            job = ""
            for raw in out.splitlines():  # <job>\t<step>\t<stamp> <text>
                cols = raw.split("\t", 2)
                job, text = (cols[0], cols[2]) if len(cols) == 3 else (job, raw)
                jobs.setdefault(job, []).append(_STAMP.sub("", _ANSI.sub("", text)))
            for job, lines in jobs.items():
                kept = _keep(lines)
                entries.append(
                    {
                        "workflow": label[w],
                        "run_id": r["id"],
                        "job": job,
                        "lines": kept,
                        "first": kept[0] if kept else None,
                        "read": len(lines),
                    }
                )
        read = ", ".join(
            f"{label[w]}={newest[w]['conclusion'] or newest[w]['status']} (run {newest[w]['id']})"
            for w in order
        )
        return entries, gaps, read or "no runs at this head"

    def _local(self, root: Path, n: str, sha: str) -> tuple[list[dict], list[str], str]:
        """Each red job of the head's own newest transcript with its failing step, rc line, red lines
        and verdict; the reads that failed and the jobs not concluded; which transcript was read and
        its job verdicts."""
        logs = sorted(root.joinpath(f"pr-{n}").glob(f"*-{sha[:8]}.log"), key=lambda p: p.stat().st_mtime)
        if not logs:
            return [], ["no log for this head"], f"no *-{sha[:8]}.log under {root}/pr-{n}"
        path = logs[-1]
        try:
            text = _ANSI.sub("", path.read_text(errors="replace"))
        except OSError as e:
            return [], [f"{path}: unreadable ({e})"], "transcript UNREAD"
        head = re.search(r"^# head=([0-9a-f]{40})$", text, re.M)
        if head is None or head.group(1) != sha:
            said = head.group(1)[:8] if head else "(none)"
            return (
                [],
                [f"{path.name}: its header head {said} is not the PR's head"],
                "transcript not this head's",
            )
        entries, gaps, verdicts, job, section = [], [], [], None, []
        for line in text.splitlines():
            if m := _LOCAL_JOB.match(line):
                if job is not None:  # the next job began before this one's verdict: it is unread
                    gaps.append(f"job {job}: no verdict before job {m.group(1)} began")
                job, section = m.group(1), []
            elif (m := _LOCAL_VERDICT.match(line)) and m.group(1) == job:
                verdicts.append(f"{job}={m.group(2)}")
                if m.group(2) == "DISCARDED":  # the run said its own verdict is not trustworthy: unread
                    gaps.append(f"job {job}: DISCARDED {m.group(3)}")
                if m.group(2) in ("failure", "error"):
                    # the failing step: its first nonzero rc line, the command above that line, and the
                    # red lines of its own output (up to the next command); other steps' output is not read,
                    # since a passing step can print text that matches (a PR body from gh api)
                    at = next(
                        (
                            i
                            for i, s in enumerate(section)
                            if (x := _LOCAL_STEP.match(s)) and x.group(1) != "0"
                        ),
                        None,
                    )
                    step, kept = [], []
                    if at is not None:
                        end = next(
                            (i for i in range(at + 1, len(section)) if section[i].startswith("$ ")),
                            len(section),
                        )
                        step = [c for c in section[:at] if c.startswith("$ ")][-1:] + [section[at]]
                        kept = _keep(section[at + 1 : end])
                    entries.append(
                        {
                            "workflow": "local-ci",
                            "run_id": path.stem,
                            "job": job,
                            "lines": step + kept + [line],
                            "first": (kept or [line])[0],
                            "read": len(section),
                        }
                    )
                job = None
            elif job is not None:
                section.append(line)
        if job is not None:  # the transcript is written as the jobs run: a section without its verdict
            gaps.append(f"job {job}: no verdict yet")
        elif not verdicts:  # the header is flushed before the first job: every job is unread
            gaps.append("no job section yet")
        return entries, gaps, f"{path.name}: {', '.join(verdicts) or 'no job verdict yet'}"

    async def run(self, args: dict[str, Any]) -> Outcome:
        wanted = {str(p) for p in args.get("prs") or []}  # an ask narrowed to PRs: rows, no transitions
        findings, table, positives, data, control, pr_n, red_n = [], [], [], [], True, 0, 0
        for spec in self.repos:
            repo, channel = spec["name"], spec["channel"]
            root = None if channel == "actions" else Path(channel.removeprefix("local:"))
            info, err = await _resolve(self.sh, repo)
            prs, err = await _open_prs(self.sh, repo, "number,headRefOid") if info else (None, err)
            if prs is None:
                control = False
                findings.append(f"{repo}: {err if info else 'gh does not resolve it as itself: ' + err}")
                continue
            if root is not None and not root.is_dir():
                control = False
                findings.append(f"{repo}: the local log dir {root} is not a directory; no PR is judged")
                continue
            positives.append(
                f"{repo} resolved as itself (id {info.get('id')}); pr list answered {len(prs)} row(s); "
                f"channel {channel}"
            )
            table.append(f"control: gh resolves {repo} as itself; channel {channel}")
            heads = {str(p["number"]): p.get("headRefOid") or "" for p in prs}
            prev = _snapshot(self.state, f"ci-red:{repo}") or {}
            snap: dict[str, list[str]] = {}
            red_lines: dict[str, dict[str, str]] = {}  # per red PR: each red run's line by its snapshot key
            quiet: set[str] = set()  # read, and no red at the head
            for n, sha in heads.items():
                if wanted and f"{repo}#{n}" not in wanted:
                    continue
                if not re.fullmatch(r"[0-9a-f]{40}", sha):
                    control = False
                    findings.append(f"{repo}#{n}: head not read ({sha!r})")
                    if n in prev:
                        snap[n] = prev[n]
                    continue
                pr_n += 1
                if root is None:
                    entries, gaps, read = await self._actions(repo, sha)
                else:
                    entries, gaps, read = self._local(root, n, sha)
                table.append(f"{repo}#{n} {sha[:8]}: {read}")
                if gaps:
                    control = False
                    findings.append(f"{repo}#{n} {sha[:8]}: {'; '.join(gaps)}")
                seen: set[str] = set()
                runs, dropped, by_run = [], 0, {}
                for e in entries:
                    kept = []
                    for x in e["lines"]:
                        if x in seen:
                            dropped += 1
                        else:
                            seen.add(x)
                            kept.append(x)
                    runs.append(
                        {"workflow": e["workflow"], "run_id": e["run_id"], "job": e["job"], "lines": kept}
                    )
                    table.append(
                        f"  {e['workflow']} run {e['run_id']} job {e['job']}: {len(kept)} line(s) kept "
                        f"of {e['read']} read"
                    )
                    table += [f"    {x}" for x in kept]
                    by_run.setdefault(e["run_id"], []).append(e)
                # a red run's snapshot key is its line without the workflow's label, which another
                # workflow sharing the name can change; the label is only shown
                red: dict[str, str] = {}
                for rid, es in by_run.items():
                    said = (
                        f"run {rid} red in [{', '.join(e['job'] for e in es)}]; "
                        f"first: {next((e['first'] for e in es if e['first']), 'no line matched')}"
                    )
                    red[f"{sha[:8]} {said}"] = f"{repo}#{n} {sha[:8]}: {es[0]['workflow']} {said}"
                if runs:
                    red_n += 1
                    snap[n], red_lines[n] = list(red), red
                    table.append(f"  {dropped} line(s) dropped by dedupe")
                    data.append(
                        {
                            "repo": repo,
                            "number": int(n),
                            "head": sha,
                            "channel": channel,
                            "runs": runs,
                            "dropped_by_dedupe": dropped,
                        }
                    )
                elif gaps and n in prev:  # not read: its last red is kept, never cleared
                    snap[n] = prev[n]
                elif not gaps:
                    quiet.add(n)
                if wanted:
                    findings += list(red.values()) or (
                        [] if gaps else [f"{repo}#{n} {sha[:8]}: no red on {channel}"]
                    )
            if wanted:  # an on-demand read: every named row, and the snapshot untouched
                findings += [
                    f"{w}: not among the open PRs read"
                    for w in sorted(wanted)
                    if w.startswith(f"{repo}#") and w.split("#", 1)[1] not in heads
                ]
                continue
            for k in sorted(set(prev) | set(snap), key=int):
                a, b = prev.get(k), snap.get(k)
                if b is None:
                    if k in quiet:
                        findings.append(f"{repo}#{k} {heads[k][:8]}: no red at this head")
                    elif len(prs) >= PR_PAGE:  # absent from a full page is not closed: its last row is kept
                        snap[k] = a
                    else:
                        findings.append(f"{repo}#{k}: no longer open")
                else:  # a red run not reported before; first sight of a red PR is all of them
                    findings += [red_lines[k][x] for x in b if x not in (a or [])]
            self.state.put(f"ci-red:{repo}", snap, run="-", seq=0)
        return Outcome(
            findings=tuple(findings),
            evidence="\n".join(table),
            control=control,
            population=f"open PRs in {[r['name'] for r in self.repos]}"
            + (f" named {sorted(wanted)}" if wanted else ""),
            predicate=f"the head red on the repo's declared CI channel ({red_n} match)",
            count=pr_n,
            positive="; ".join(positives) if control else "",
            data=tuple(data),
        )


def instruments(cfg: dict, khive: Khive, state: NoteStore) -> dict:
    """The chores `chores.toml` names, built with their arguments; a chore the file does not name is
    not served. `expect_empty` per chore declares an empty population as its expected state."""
    out: dict = {}
    c = cfg.get("chores", {})

    def empty(a: dict, default: bool) -> bool:
        return bool(a.get("expect_empty", default))

    if "pr-state" in c:
        a = c["pr-state"]
        out["pr-state"] = PrState(
            a["repos"],
            state,
            every=a.get("every"),
            stale_days=a.get("stale_days", 3),
            empty_expected=empty(a, False),
        )
    if "required-contexts" in c:
        a = c["required-contexts"]
        out["required-contexts"] = RequiredContexts(
            a["repos"],
            state,
            every=a.get("every"),
            source_identity=a.get("source_identity") or f"{cfg['owner']}:desk",
            empty_expected=empty(a, False),
        )
    if "verdict-freshness" in c:
        a = c["verdict-freshness"]
        out["verdict-freshness"] = VerdictFreshness(
            state,
            every=a.get("every"),
            verdict_dir=a.get("verdict_dir", VERDICT_DIR),
            repos=a.get("repos"),
            enumerator=a.get("enumerator"),
            empty_expected=empty(a, False),
        )
    if "ci-red" in c:
        a = c["ci-red"]
        out["ci-red"] = CiRed(a["repos"], state, every=a.get("every"), empty_expected=empty(a, False))
    if "served" in c:
        a = c["served"]
        out["served"] = Served(a["agents"], every=a.get("every"), empty_expected=empty(a, False))
    if "gtd-inbox" in c:
        a = c["gtd-inbox"]
        out["gtd-inbox"] = GtdInbox(
            khive,
            a.get("assignee") or cfg["owner"],
            every=a.get("every"),
            max_age_h=a.get("max_age_h", 24),
            empty_expected=empty(a, True),
        )
    if "inbox-sla" in c:
        a = c["inbox-sla"]
        out["inbox-sla"] = InboxSla(
            khive,
            a.get("actors") or [cfg["owner"]],
            every=a.get("every"),
            stale_minutes=a.get("stale_minutes", 20),
            empty_expected=empty(a, True),
        )
    if "disk" in c:
        a = c["disk"]
        out["disk"] = Disk(
            a["roots"],
            every=a.get("every"),
            floors=a.get("floors"),
            target_cap_gib=a.get("target_cap_gib", 20),
            empty_expected=empty(a, False),
        )
    if "record" in c:  # the desk's lookups (lookups.py): repos, the ADR corpus, khive notes
        from .lookups import Adrs, Decisions, Issues, Record

        a = c["record"]
        parts: list = []
        if a.get("repos"):
            parts.append(Issues(a["repos"], limit=a.get("limit", 8)))
        if a.get("adr_dirs"):
            parts.append(Adrs([Path(d) for d in a["adr_dirs"]], limit=a.get("limit", 5)))
        if a.get("decisions", True):
            parts.append(
                Decisions(
                    khive,
                    positive=a.get("positive") or cfg["owner"],
                    positive_kind=a.get("positive_kind", "entity"),
                    kinds=tuple(a.get("kinds", ("decision", "insight"))),
                    limit=a.get("limit", 5),
                )
            )
        out["record"] = Record(parts)
    if "worktrees" in c:
        a = c["worktrees"]
        out["worktrees"] = Worktrees(
            a["repos"],
            state,
            every=a.get("every"),
            parked_days=a.get("parked_days", 3),
            empty_expected=empty(a, False),
        )
    if "bench-jobs" in c:
        a = c["bench-jobs"]
        out["bench-jobs"] = BenchJobs(
            a["bench_dir"], a["results"], every=a.get("every"), empty_expected=empty(a, True)
        )
    return out
