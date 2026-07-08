"""lionbench harvester — turn a nominated merged PR into a validated instance.

    uv run python -m benchmarks.orchestration.suites.lionbench.harvest \\
        --repo ohdearquant/lionagi --pr 1843 [--out DIR]

Pipeline per nomination (DESIGN_CONTRACT §7):
  a. resolve base_commit (the PR's baseRefOid — these repos squash-merge, so the merge
     commit's own parent is main's tip at merge time, NOT the PR's base; see harvest()) +
     full diff + PR/issue body via `gh`.
  b. split the diff into test_patch (tests/, test_*.py, *_test.py, conftest.py) and gold_patch
     (everything else); reject if either side is empty.
  c. scrub the PR/issue text into a draft task_text (``needs_review=True`` — a human finalizes it).
  d. validate both directions (sandbox when DAYTONA_API_KEY is set, local git-worktree fallback
     otherwise — see validate.py).
  e. emit the instance JSON, or a rejection record with reason.

``gh`` calls run with ``CLAUDECODE`` unset in a cleaned env dict (this harness may itself run
under Claude Code, and `gh` misbehaves — hangs on a pager / auth prompt — when it sees that var).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parents[1])):  # self dir + benchmarks/orchestration
    if _p not in sys.path:
        sys.path.insert(0, _p)

from schema import Instance, OracleSpec, Provenance, save_instance, save_rejection  # noqa: E402
from suites.swebench import workspace  # noqa: E402 — reuse the cached-clone worktree helpers
from validate import validate_both_directions  # noqa: E402

DATA_DIR = _HERE / "data"

_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|conftest\.py$)", re.IGNORECASE
)


def _clean_env() -> dict[str, str]:
    """Env for `gh` subprocess calls: drop CLAUDECODE, which makes `gh` hang/misbehave."""
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    return env


def _gh(args: list[str], *, timeout: int = 60) -> str:
    r = subprocess.run(  # noqa: S603 — fixed argv (gh + literal flags), no shell
        ["gh", *args],  # noqa: S607 — gh resolved on PATH by design
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_clean_env(),
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {r.stderr.strip()[-800:]}")
    return r.stdout


def gh_pr_view(repo: str, pr: int) -> dict:
    out = _gh(
        [
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "mergeCommit,baseRefOid,body,title,number,mergedAt,state",
        ]
    )
    return json.loads(out)


def gh_pr_diff(repo: str, pr: int) -> str:
    return _gh(["pr", "diff", str(pr), "--repo", repo])


def gh_issue_body(repo: str, issue: int) -> str:
    out = _gh(["issue", "view", str(issue), "--repo", repo, "--json", "body"])
    return json.loads(out).get("body", "")


_ISSUE_REF_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d+)", re.IGNORECASE
)


def linked_issue(pr_body: str) -> int | None:
    m = _ISSUE_REF_RE.search(pr_body or "")
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Pure functions — diff splitting + task-text scrub. No network, fully testable.
# ---------------------------------------------------------------------------


def split_diff(diff_text: str) -> tuple[str, str]:
    """Split a unified diff into (test_patch, gold_patch) by touched-file path.

    A file's hunk goes to ``test_patch`` iff its path (the ``b/`` side) matches
    a test-path convention: under ``tests/``, named ``test_*.py``/``*_test.py``,
    or ``conftest.py`` anywhere. Everything else is ``gold_patch``.
    """
    if not diff_text.strip():
        return "", ""
    # Split on "diff --git a/X b/Y" boundaries, keeping the marker with each chunk.
    parts = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)
    test_chunks: list[str] = []
    gold_chunks: list[str] = []
    header_re = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)
    for chunk in parts:
        if not chunk.strip():
            continue
        m = header_re.match(chunk)
        path = m.group(2) if m else ""
        if _TEST_PATH_RE.search(path):
            test_chunks.append(chunk)
        else:
            gold_chunks.append(chunk)
    return "".join(test_chunks), "".join(gold_chunks)


_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
# Stateful raw-diff stripper (see _strip_raw_diff_blocks below). A bare "line
# starts with + or -" pattern is a false-positive magnet on its own — it nukes
# ordinary markdown bullet lists ("- **file.py** — did X") in PR bodies, which
# is exactly the prose we want to KEEP (confirmed live against PR #1665's
# body). But dropping that alternative entirely leaves *unfenced* raw diffs
# untouched: a PR/issue body is not guaranteed to fence a pasted patch, and a
# flat regex with no notion of "am I inside a diff block" can't tell a diff's
# `-`/`+` content lines from a bullet list. _strip_raw_diff_blocks resolves
# this with a small state machine: only lines that already look like +/-
# content *after* a real `diff --git` header are treated as diff content.
_DIFF_START_RE = re.compile(r"^diff --git ")
_DIFF_STRUCT_RE = re.compile(r"^(index [0-9a-f]|@@ .*@@)")
# +/- prefixed (added/removed content, and the --- / +++ file markers, which
# also start with -/+) or a single leading space (unified-diff context line).
_DIFF_CONTENT_RE = re.compile(r"^[+\- ]")
# Git's *extended* header lines: unprefixed (no +/-/space), so none of the
# patterns above catch them, and a naive scanner disarms on the first one it
# sees — leaking the rest of a new-file/deleted-file/rename/copy diff.
_DIFF_EXTENDED_HEADER_RE = re.compile(
    r"^(old mode |new mode |deleted file mode |new file mode |"
    r"copy from |copy to |rename from |rename to |"
    r"similarity index |dissimilarity index |"
    r"Binary files |\\ No newline at end of file)"
)
_BINARY_PATCH_START_RE = re.compile(r"^GIT binary patch")
_ISSUE_NUM_RE = re.compile(r"#\d+")
_AUDIT_LABEL_RE = re.compile(r"\b[A-Z][A-Z0-9]*-\d+\b|\bFinding\s+\d+\b", re.IGNORECASE)
_FIX_LANGUAGE_RE = re.compile(
    r"\b(the fix is|fixed by|solved by|solution is to|patch(?:ed)? (?:it )?by|"
    r"the (?:root cause|approach) (?:is|was)|we (?:should|need to) change|"
    r"changed .* to |renamed .* to )",
    re.IGNORECASE,
)


def _split_sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+", text)


def _strip_raw_diff_blocks(text: str) -> str:
    """Drop unfenced raw diff blocks line-by-line, without touching prose outside them.

    A `diff --git ` line arms diff-mode. While armed, this is a leak scrubber —
    it errs toward over-stripping, not under-stripping — so a line only survives
    if it matches NONE of: blank, a structural line (index/@@ hunk header), a
    +/-/space-prefixed content line, or one of Git's extended-header lines
    (new/deleted file mode, rename/copy from/to, similarity index, ...). A
    `GIT binary patch` marker arms a nested binary-blob mode that drops every
    line unconditionally (base85 patch data has no fixed shape, and a real
    `git diff --binary` puts blank-line separators *between* the literal/delta
    hunk records of a single file, so a blank line is not a reliable
    end-of-binary-patch marker) — binary mode stays armed until the next
    `diff --git ` header or end of input, never disarming on a blank line.
    The first line that fails every diff-mode check disarms diff-mode — so a
    markdown bullet list, which never starts with a real `diff --git ` header,
    is never touched."""
    out_lines = []
    in_diff = False
    in_binary = False
    for line in text.split("\n"):
        if _DIFF_START_RE.match(line):
            in_diff = True
            in_binary = False
            continue
        if in_diff:
            if in_binary:
                continue
            if _BINARY_PATCH_START_RE.match(line):
                in_binary = True
                continue
            if (
                line.strip() == ""
                or _DIFF_STRUCT_RE.match(line)
                or _DIFF_CONTENT_RE.match(line)
                or _DIFF_EXTENDED_HEADER_RE.match(line)
            ):
                continue
            in_diff = False
        out_lines.append(line)
    return "\n".join(out_lines)


def scrub_task_text(pr_body: str, issue_body: str | None = None) -> str:
    """Best-effort automated scrub of PR/issue text into a task description.

    Strips fenced code blocks, raw diff blocks (fenced or not), PR/issue number
    references, internal audit labels, and sentences that name the fix approach.
    This is a DRAFT — the caller must still hand-review before an instance is
    trusted (DESIGN_CONTRACT §5.3); it never claims to be a complete scrub."""
    combined = "\n\n".join(t for t in (pr_body, issue_body) if t)
    combined = _FENCE_RE.sub("", combined)
    combined = _strip_raw_diff_blocks(combined)
    combined = _ISSUE_NUM_RE.sub("", combined)
    combined = _AUDIT_LABEL_RE.sub("", combined)
    kept = [s for s in _split_sentences(combined) if not _FIX_LANGUAGE_RE.search(s)]
    text = " ".join(s.strip() for s in kept if s.strip())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def infer_held_out_paths(test_patch: str) -> list[str]:
    """Test file paths touched by the test_patch, in diff order, deduplicated."""
    paths: list[str] = []
    for m in re.finditer(r"^diff --git a/(.+?) b/(.+)$", test_patch, re.MULTILINE):
        path = m.group(2)
        if path not in paths:
            paths.append(path)
    return paths


def default_oracle_command(held_out_paths: list[str]) -> str:
    # --all-extras: this repo's test suite spans optional extras (studio's
    # fastapi/uvicorn, etc.) — a plain `uv run pytest` only syncs the base
    # project deps, so any held-out test that imports an extra-gated module
    # fails at COLLECTION (ModuleNotFoundError), which is indistinguishable
    # from a real regression unless you inspect the traceback. Confirmed live
    # against PR #1643 (tests/cli/test_argv_injection.py imports
    # lionagi.studio.services.schedules -> fastapi).
    return f"uv run --all-extras pytest {' '.join(held_out_paths)} -q"


# ---------------------------------------------------------------------------
# Orchestration — network + subprocess. Not unit-tested directly (no gh/network
# in CI); exercised manually against real nominations ahead of Thursday.
# ---------------------------------------------------------------------------


def harvest(
    repo: str,
    pr: int,
    *,
    subject: str = "lionagi",
    nominated_by: str = "",
    why: str = "",
    held_out_paths: list[str] | None = None,
    oracle_command: str | None = None,
    out_dir: Path = DATA_DIR,
    validate: bool = True,
) -> Instance | None:
    view = gh_pr_view(repo, pr)
    instance_id = f"{repo.split('/')[-1]}__{pr}"
    if view.get("state") != "MERGED":
        save_rejection(instance_id, "PR is not merged", out_dir, subject=subject)
        return None
    # base_commit = the PR's baseRefOid, NOT the merge commit's first parent.
    # These repos squash-merge onto main: the merge commit is a brand-new commit
    # whose single parent is main's tip AT MERGE TIME, which has almost always
    # moved past the PR's actual base — using it as base_commit silently pulls
    # in unrelated later history. baseRefOid is "world before the fix" for a
    # squash merge (verified against LIONAGI_NOMINATIONS.md's methodology, and
    # confirmed live: PR #1665's merge-commit parent b01d8190.. != its
    # baseRefOid b1fd2ad2..).
    base_commit = view.get("baseRefOid")
    if not base_commit:
        save_rejection(instance_id, "no baseRefOid recorded", out_dir, subject=subject)
        return None

    diff = gh_pr_diff(repo, pr)
    test_patch, gold_patch = split_diff(diff)
    if not test_patch.strip() or not gold_patch.strip():
        save_rejection(
            instance_id, "empty test_patch or gold_patch after split", out_dir, subject=subject
        )
        return None

    issue_num = linked_issue(view.get("body", ""))
    issue_body = gh_issue_body(repo, issue_num) if issue_num else None
    task_text = scrub_task_text(view.get("body", ""), issue_body)
    if not task_text:
        save_rejection(instance_id, "scrub produced empty task_text", out_dir, subject=subject)
        return None

    hop = held_out_paths or infer_held_out_paths(test_patch)
    command = oracle_command or default_oracle_command(hop)

    instance = Instance(
        instance_id=instance_id,
        repo=repo,
        base_commit=base_commit,
        task_text=task_text,
        oracle=OracleSpec(
            kind="pytest", held_out_paths=hop, command=command, test_patch=test_patch
        ),
        gold_patch=gold_patch,
        merged_at=view.get("mergedAt", ""),
        subject=subject,
        source_pr=f"{repo.split('/')[-1]}#{pr}",
        provenance=Provenance(pr=pr, issue=issue_num, nominated_by=nominated_by, why=why),
        needs_review=True,
    )

    if validate:
        repo_dir = workspace.ensure_repo(repo)
        result = validate_both_directions(repo_dir, base_commit, gold_patch, test_patch, command)
        instance.validation.gold_passes = result["gold_passes"]
        instance.validation.null_fails = result["null_fails"]
        instance.validation.gold_output = result["gold_output"]
        instance.validation.null_output = result["null_output"]
        if not (result["gold_passes"] and result["null_fails"]):
            reason = (
                f"validation failed: gold_passes={result['gold_passes']} "
                f"null_fails={result['null_fails']}"
            )
            save_rejection(instance_id, reason, out_dir, subject=subject)
            return None

    save_instance(instance, out_dir)
    return instance


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name, e.g. ohdearquant/lionagi")
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument(
        "--subject",
        default="lionagi",
        help="grouping axis, e.g. rust-systems|lean-proofs|python-framework|numerics-parity|kg-agentic",
    )
    ap.add_argument("--nominated-by", default="")
    ap.add_argument("--why", default="")
    ap.add_argument("--held-out", default=None, help="comma-separated pytest paths, optional")
    ap.add_argument("--oracle-command", default=None)
    ap.add_argument("--out", default=str(DATA_DIR))
    ap.add_argument("--no-validate", action="store_true")
    args = ap.parse_args()

    held_out = [s.strip() for s in args.held_out.split(",")] if args.held_out else None
    instance = harvest(
        args.repo,
        args.pr,
        subject=args.subject,
        nominated_by=args.nominated_by,
        why=args.why,
        held_out_paths=held_out,
        oracle_command=args.oracle_command,
        out_dir=Path(args.out),
        validate=not args.no_validate,
    )
    if instance is None:
        print(
            f"REJECTED {args.repo}#{args.pr} — see {args.out}/{args.subject}/rejected/ for reason"
        )
        raise SystemExit(1)
    print(f"OK {instance.instance_id} — {args.out}/{args.subject}/{instance.instance_id}.json")


if __name__ == "__main__":
    main()
