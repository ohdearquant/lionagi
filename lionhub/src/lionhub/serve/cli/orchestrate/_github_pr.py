# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Fetch a pull request for a playbook and post its review, from the launching process.

A playbook that declares ``github-pr`` receives the pull request's metadata and diff as the
prompt arguments ``{pr_meta}`` and ``{pr_diff}``, and, when its comment style is not ``none``,
has the run's synthesis posted to the pull request as one comment. Both happen here, in the
process that launched the run, under that process's own GitHub sign-in. The agents the run
starts read the pull request as text in their prompt and need no GitHub access: the child
sandbox denies them the GitHub CLI's configuration and their environment never carries
``GH_TOKEN``.

A launcher that predates this field refuses the playbook with "unknown spec field
'github_pr'", so the review never runs on an empty pull request.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from lionhub.orchestration.flow.spec import GITHUB_PR_COMMENT_STYLES

__all__ = (
    "GITHUB_COMMENT_MAX_CHARS",
    "PR_META_FIELDS",
    "GithubPrError",
    "GithubPrTarget",
    "fetch_pull_request",
    "post_review",
    "resolve_target",
    "synthesis_text",
)

#: The ``gh pr view --json`` fields handed to the run as ``{pr_meta}``.
PR_META_FIELDS = "title,body,author,baseRefName,headRefName,additions,deletions,changedFiles"

#: GitHub refuses a comment body longer than this.
GITHUB_COMMENT_MAX_CHARS = 65536

_GH_TIMEOUT_S = 120
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_REPO_RE = re.compile(r"^(?:[A-Za-z0-9.-]+/)?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# The header ``_format_result_text`` writes above a flow's synthesis.
_RULE = "\u2550" * 60
_SYNTHESIS_HEADER = "  Synthesis  ["


class GithubPrError(RuntimeError):
    """The pull request could not be fetched, or the review could not be posted."""


@dataclass(frozen=True)
class GithubPrTarget:
    """One pull request, as the launcher addresses it to ``gh``."""

    pr: str
    repo: str
    comment: str

    def repo_args(self) -> list[str]:
        return ["--repo", self.repo] if self.repo else []


def _fill(template: str, ctx: dict[str, Any]) -> str:
    """Replace ``{name}`` with the playbook argument of that name; leave unknown names."""

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(ctx[key]) if key in ctx else match.group(0)

    return _PLACEHOLDER_RE.sub(_sub, template).strip()


def resolve_target(
    field: dict[str, str], positional: str | None, playbook_args: dict[str, Any]
) -> GithubPrTarget:
    """Fill the field's placeholders and check what ``gh`` will be handed.

    ``{input}`` is the run's positional prompt, as in the playbook's own prompt. A value that
    still holds a placeholder after filling, or that ``gh`` would read as a flag, is refused.
    """
    ctx = dict(playbook_args)
    if positional is not None:
        ctx["input"] = positional

    pr = _fill(field.get("pr", ""), ctx)
    repo = _fill(field.get("repo", ""), ctx)
    comment = _fill(field.get("comment", "none"), ctx) or "none"

    if not pr or _PLACEHOLDER_RE.search(pr):
        raise GithubPrError(
            "github-pr: no pull request to fetch; pass its number or URL as the prompt"
        )
    if pr.startswith("-") or any(ch.isspace() for ch in pr):
        raise GithubPrError(f"github-pr: {pr!r} is not a pull request number, URL or branch name")
    if repo and not _REPO_RE.fullmatch(repo):
        raise GithubPrError(f"github-pr: repository {repo!r} is not in [HOST/]OWNER/REPO form")
    if comment not in GITHUB_PR_COMMENT_STYLES:
        raise GithubPrError(
            f"github-pr: comment style must be one of {list(GITHUB_PR_COMMENT_STYLES)}, "
            f"got {comment!r}"
        )
    return GithubPrTarget(pr=pr, repo=repo, comment=comment)


def _gh() -> str:
    path = shutil.which("gh")
    if path is None:
        raise GithubPrError(
            "github-pr: the GitHub CLI `gh` is not on PATH; the launcher fetches the pull "
            "request and posts the review with it"
        )
    return path


def _run_gh(argv: list[str], *, stdin: str | None = None) -> str:
    action = " ".join(argv[1:3])
    try:
        proc = subprocess.run(  # noqa: S603 - argv is fixed apart from checked values
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GithubPrError(
            f"github-pr: `gh {action}` did not finish within {_GH_TIMEOUT_S}s"
        ) from exc
    except OSError as exc:
        raise GithubPrError(f"github-pr: `gh {action}` could not start: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or "no output"
        raise GithubPrError(
            f"github-pr: `gh {action}` failed with exit {proc.returncode}: {detail}"
        )
    return proc.stdout


def fetch_pull_request(target: GithubPrTarget) -> tuple[str, str]:
    """Return the pull request's metadata (JSON text) and its diff.

    An empty diff is refused: a review of nothing would read as a clean review.
    """
    gh = _gh()
    meta = _run_gh([gh, "pr", "view", target.pr, *target.repo_args(), "--json", PR_META_FIELDS])
    diff = _run_gh([gh, "pr", "diff", target.pr, *target.repo_args()])
    if not diff.strip():
        raise GithubPrError(
            f"github-pr: `gh pr diff` returned an empty diff for {target.pr}; nothing to review"
        )
    return meta.strip(), diff


def synthesis_text(output: str) -> str | None:
    """The synthesis section of a flow's output, or ``None`` when the run produced none.

    Reads both output formats: the JSON object's ``synthesis.response``, and in text the
    section after the last synthesis header ``_format_result_text`` writes.
    """
    stripped = output.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict):
            synthesis = data.get("synthesis")
            if isinstance(synthesis, dict) and isinstance(synthesis.get("response"), str):
                return synthesis["response"].strip() or None
            return None

    lines = output.split("\n")
    for i in range(len(lines) - 4, -1, -1):
        if (
            lines[i] == _RULE
            and lines[i + 1].startswith(_SYNTHESIS_HEADER)
            and lines[i + 3] == _RULE
        ):
            return "\n".join(lines[i + 4 :]).strip() or None
    return None


def post_review(target: GithubPrTarget, output: str) -> bool:
    """Post the run's synthesis to the pull request; return whether anything was posted."""
    if target.comment == "none":
        return False
    body = synthesis_text(output)
    if body is None:
        raise GithubPrError(
            f"github-pr: the run produced no synthesis, so nothing was posted to {target.pr}"
        )
    if len(body) > GITHUB_COMMENT_MAX_CHARS:
        raise GithubPrError(
            f"github-pr: the review is {len(body)} characters and GitHub accepts at most "
            f"{GITHUB_COMMENT_MAX_CHARS} in one comment; nothing was posted to {target.pr}"
        )
    gh = _gh()
    _run_gh([gh, "pr", "comment", target.pr, *target.repo_args(), "--body-file", "-"], stdin=body)
    return True
