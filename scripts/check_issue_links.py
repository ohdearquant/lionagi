"""Check that a PR body says what happens to every phase-tracking issue it references.

Phase-tracking issues are opened in bulk when an ADR lands, one per phase, and
each closes when its phase ships. That only happens if the PR body spells a
closing keyword, so this asserts it does, or that the author declared the
reference deliberate with a ``Refs-only:`` line.

Usage: ``uv run python scripts/check_issue_links.py``, reading PR_BODY,
PR_BRANCH and REPO from the environment.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

# A PR body is attacker-supplied on a fork, and every reference costs one
# authenticated API call, so the count is bounded and an overflow is refused
# rather than silently truncated: a skipped reference is a reference this gate
# did not check, which must not read as a reference it approved.
MAX_REFS = 40

_BODY_REF_RE = re.compile(r"#(\d{2,6})\b")
_BRANCH_REF_RE = re.compile(r"(?<!\d)(\d{3,6})(?!\d)")
_ADR_RE = re.compile(r"ADR-\d{3,4}", re.I)
# "does not close", "won't fix", "never resolves". GitHub's own parser ignores
# negation and would close the issue anyway, so a negated keyword is read here
# as not-a-closure and reported, rather than being taken at face value.
# Anywhere earlier in the same clause counts, with no distance limit: a window
# of N words is defeated by N+1 ("does not in any way intend to close #1").
_NEGATION_RE = re.compile(r"\bnot\b|n['’]t\b|\bnever\b|\bwithout\b|\bno\b", re.I)
# A contrastive coordinator ends the clause the same way punctuation does, or
# "not a revert, but closes #1" reads as a non-closure and the gate demands a
# keyword the author already wrote. "and" and "or" are left out: they carry
# negation forward often enough ("won't fix #1 and close #2") that splitting on
# them trades a noisy refusal for a missed one, and "yet" is left out because
# its adverbial reading sits inside the negation it would end ("does not yet
# close #1"). A leading subordinate clause
# ("Although it does not revert, it closes #1") still reads as negated, since
# ending that one needs the comma and a comma is what "not, in any way, close
# #1" hides behind.
_CLAUSE_SPLIT_RE = re.compile(r"[.;:!?\n]|\bbut\b|\bhowever\b|\b(?:al)?though\b", re.I)


def refs_from(body: str, branch: str) -> set[int]:
    """Every issue number the PR points at, from its body and its branch name."""
    refs = {int(n) for n in _BODY_REF_RE.findall(body)}
    refs |= {int(n) for n in _BRANCH_REF_RE.findall(branch)}
    return refs


def is_phase_tracking(title: str) -> bool:
    """Whether a title marks the issue as one phase of an ADR rollout.

    Keyed on the ADR number rather than the word "phase": the same issues are
    titled "Phase 1", "P3", "G0-P1" and "P5b". The epic that umbrellas a
    rollout is excluded; it closes when its last phase does.
    """
    return bool(_ADR_RE.search(title)) and not title.lower().startswith("epic(")


def _closure_pattern(repo: str, number: int) -> re.Pattern[str]:
    return re.compile(
        rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:{re.escape(repo)})?#{number}\b",
        re.I,
    )


def _is_negated(body: str, start: int) -> bool:
    """Whether the clause leading up to *start* negates what follows it."""
    clause = _CLAUSE_SPLIT_RE.split(body[:start])[-1]
    return bool(_NEGATION_RE.search(clause))


def closed_by_body(body: str, repo: str, number: int) -> bool:
    """Whether the body states this PR closes *number*, ignoring negated prose."""
    return any(
        not _is_negated(body, m.start()) for m in _closure_pattern(repo, number).finditer(body)
    )


def negated_closure(body: str, repo: str, number: int) -> bool:
    """Whether the only closing keyword for *number* sits inside a negation."""
    matches = list(_closure_pattern(repo, number).finditer(body))
    return bool(matches) and all(_is_negated(body, m.start()) for m in matches)


def declared_reference_only(body: str, number: int) -> bool:
    """An author's explicit statement that this PR does not finish the phase.

    The ADR document PR is the case this exists for: it names every phase it
    plans and finishes none of them.
    """
    return any(
        re.match(r"\s*refs-only\s*:", line, re.I) and re.search(rf"#{number}\b", line)
        for line in body.splitlines()
    )


def fetch_issue(repo: str, number: int) -> dict | None:
    """The issue, or None when it does not exist or is a pull request."""
    proc = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{number}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        # A reference to something that was never an issue is ordinary. Any
        # other failure is a failed read, and a failed read must not be
        # reported as nothing to do.
        if "Not Found" in stderr or "HTTP 404" in stderr:
            return None
        print(f"could not read issue #{number}: {stderr}")
        sys.exit(1)
    data = json.loads(proc.stdout)
    return None if "pull_request" in data else data


def main() -> int:
    body = os.environ.get("PR_BODY") or ""
    branch = os.environ.get("PR_BRANCH") or ""
    repo = os.environ["REPO"]

    refs = refs_from(body, branch)
    if len(refs) > MAX_REFS:
        print(
            f"This PR references {len(refs)} issue numbers, over the {MAX_REFS} this check "
            "will resolve. Nothing was verified, because verifying part of the list and "
            "passing would say the rest were checked.\n\nTrim the references, or split the PR."
        )
        return 1

    missing = []
    negated = []
    governed = []
    for number in sorted(refs):
        data = fetch_issue(repo, number)
        if data is None or not is_phase_tracking(data["title"]):
            continue
        governed.append((number, data["title"]))
        if closed_by_body(body, repo, number) or declared_reference_only(body, number):
            continue
        missing.append((number, data["title"]))
        if negated_closure(body, repo, number):
            negated.append(number)

    if missing:
        print("This PR references phase-tracking issues without saying what happens to them.\n")
        for number, title in missing:
            print(f"  #{number}  {title}")
        if negated:
            print(
                "\nA closing keyword for "
                + ", ".join(f"#{n}" for n in negated)
                + " appears inside a negation, so it is not read as a closure here. GitHub's own "
                "parser does not read negation and would close the issue on merge, so rewrite the "
                "sentence rather than relying on the word 'not'."
            )
        print(
            "\nAdd a closing keyword to the PR body for each one it finishes, "
            '"Closes #NNNN".'
            "\nFor a phase this PR only mentions, add a line naming it instead:"
            "\n\n    Refs-only: #NNNN"
            "\n\nA closing keyword on a PR stacked under another branch is not a "
            "problem: GitHub fires it when the stack drains and the tail retargets "
            "main, so write it when the work lands, not when the base does."
        )
        return 1

    if governed:
        print(f"{len(governed)} referenced phase issue(s), each closed or declared reference-only:")
        for number, title in governed:
            print(f"  #{number}  {title}")
    else:
        print("No phase-tracking issues referenced.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
