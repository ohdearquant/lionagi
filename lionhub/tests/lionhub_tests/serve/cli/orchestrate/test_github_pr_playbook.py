# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The ``github-pr`` playbook field: the launcher fetches the pull request and posts the review.

The agents a run starts are denied the GitHub CLI's configuration and never handed
``GH_TOKEN``, so a review playbook whose agents called ``gh`` themselves could not read the
pull request. These tests run the shipped ``pr-review`` playbook through ``li o flow -p`` with
a stand-in for ``gh`` and the flow itself replaced, and check what each side does.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import lionagi._flow_spec as flow_spec
import lionhub.serve.cli.orchestrate as orch_mod
import lionhub.serve.cli.orchestrate._github_pr as github_pr_mod
from lionhub.agent.child_sandbox import DEFAULT_DENY_READ
from lionhub.serve.cli.main import main
from lionhub.serve.cli.orchestrate._common import _format_result_text
from lionhub.serve.cli.orchestrate._github_pr import (
    PR_META_FIELDS,
    GithubPrError,
    resolve_target,
    synthesis_text,
)

REPO_ROOT = Path(__file__).resolve().parents[6]
PLAYBOOK = REPO_ROOT / "examples" / "playbooks" / "pr-review.playbook.yaml"

META = '{"title":"Make the widget round","author":{"login":"octo"}}'
DIFF = "diff --git a/widget.py b/widget.py\n+ROUND = True\n"
REVIEW = "## PR Review: Make the widget round\n\n**Verdict**: APPROVE"


def _flow_output(synthesis: str | None = REVIEW) -> str:
    workers = [{"model": "stub", "time_ms": 1.0, "response": "correctness: no findings"}]
    synth = None if synthesis is None else {"model": "stub", "time_ms": 1.0, "response": synthesis}
    return _format_result_text(workers, synth)


class _FakeGh:
    """Answers the launcher's ``gh`` calls; logs each and whether GH_TOKEN was set for it."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.posted: str | None = None
        self.fail: str | None = None
        self.diff = DIFF

    def calls(self) -> list[str]:
        return list(self.log)

    def comment(self) -> str | None:
        return self.posted

    def run(self, argv, *, input=None, **_kwargs):  # noqa: A002 - subprocess.run's name
        args = list(argv[1:])
        self.log.append(f"{' '.join(args)}|GH_TOKEN={os.environ.get('GH_TOKEN', 'unset')}")
        action = args[:2]
        if action == ["pr", "view"]:
            if self.fail == "view":
                return subprocess.CompletedProcess(
                    argv, 1, "", "GraphQL: Could not resolve to a PullRequest\n"
                )
            return subprocess.CompletedProcess(argv, 0, META + "\n", "")
        if action == ["pr", "diff"]:
            return subprocess.CompletedProcess(argv, 0, self.diff, "")
        if action == ["pr", "comment"]:
            self.posted = input
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 2, "", f"unexpected: {args}")


@pytest.fixture
def gh(monkeypatch):
    fake = _FakeGh()
    monkeypatch.setattr(github_pr_mod, "_gh", lambda: "/opt/fake/bin/gh")
    monkeypatch.setattr(
        github_pr_mod,
        "subprocess",
        SimpleNamespace(run=fake.run, TimeoutExpired=subprocess.TimeoutExpired),
    )
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(orch_mod, "_resolve_playbook_path", lambda name: (PLAYBOOK, None))
    return fake


def _run(argv: list[str], output: str, status: str = "completed"):
    seen: dict = {}

    async def _flow(**kwargs):
        # The run's side: no GitHub token anywhere in its environment, and the pull
        # request already in its prompt.
        seen["gh_token"] = os.environ.get("GH_TOKEN")
        seen.update(kwargs)
        return output, status

    with patch.object(orch_mod, "_run_flow", AsyncMock(side_effect=_flow)) as run_flow:
        code = main(argv)
    return code, run_flow, seen


class TestLauncherFetchesAndPosts:
    def test_the_run_completes_with_the_pull_request_in_its_prompt_and_no_github_access(self, gh):
        code, run_flow, seen = _run(
            ["o", "flow", "-p", "pr-review", "--repo", "acme/widgets", "--comment", "brief", "42"],
            _flow_output(),
        )

        assert code == 0
        run_flow.assert_awaited_once()
        # The run needs nothing from GitHub: the metadata and diff are in its prompt,
        # no token reached it, and the agents it starts are denied the gh configuration.
        assert META in seen["prompt"]
        assert DIFF in seen["prompt"]
        assert "{pr_meta}" not in seen["prompt"] and "{pr_diff}" not in seen["prompt"]
        assert seen["gh_token"] is None
        assert "~/.config/gh" in DEFAULT_DENY_READ
        assert seen["with_synthesis"] is True

        # Every gh call came from the launcher: fetch before the run, one post after it.
        assert gh.calls() == [
            f"pr view 42 --repo acme/widgets --json {PR_META_FIELDS}|GH_TOKEN=unset",
            "pr diff 42 --repo acme/widgets|GH_TOKEN=unset",
            "pr comment 42 --repo acme/widgets --body-file -|GH_TOKEN=unset",
        ]
        assert gh.comment() == REVIEW

    def test_the_default_comment_style_posts_nothing(self, gh):
        code, _run_flow, _seen = _run(["o", "flow", "-p", "pr-review", "42"], _flow_output())

        assert code == 0
        assert [call.split(" ", 2)[:2] for call in gh.calls()] == [["pr", "view"], ["pr", "diff"]]
        assert gh.comment() is None

    def test_a_failed_fetch_stops_before_any_run(self, gh, capsys):
        gh.fail = "view"
        code, run_flow, _seen = _run(["o", "flow", "-p", "pr-review", "42"], _flow_output())

        assert code == 1
        run_flow.assert_not_awaited()
        err = capsys.readouterr().err
        assert "`gh pr view` failed with exit 1" in err
        assert "Could not resolve to a PullRequest" in err

    def test_an_empty_diff_is_refused(self, gh, capsys):
        gh.diff = ""
        code, run_flow, _seen = _run(["o", "flow", "-p", "pr-review", "42"], _flow_output())

        assert code == 1
        run_flow.assert_not_awaited()
        assert "empty diff" in capsys.readouterr().err

    def test_a_run_that_did_not_complete_posts_nothing(self, gh, capsys):
        code, _run_flow, _seen = _run(
            ["o", "flow", "-p", "pr-review", "--comment", "full", "42"],
            _flow_output(),
            status="failed",
        )

        assert code != 0
        assert gh.comment() is None
        assert "nothing was posted" in capsys.readouterr().err

    def test_a_run_without_a_synthesis_posts_nothing_and_fails(self, gh, capsys):
        code, _run_flow, _seen = _run(
            ["o", "flow", "-p", "pr-review", "--comment", "brief", "42"],
            _flow_output(synthesis=None),
        )

        assert code != 0
        assert gh.comment() is None
        assert "no synthesis" in capsys.readouterr().err

    def test_a_dry_run_posts_nothing(self, gh):
        code, _run_flow, _seen = _run(
            ["o", "flow", "-p", "pr-review", "--comment", "brief", "--dry-run", "42"],
            _flow_output(),
        )

        assert code == 0
        assert gh.comment() is None

    def test_a_reference_gh_would_read_as_a_flag_is_refused_before_gh_runs(self, gh, capsys):
        code, run_flow, _seen = _run(
            ["o", "flow", "-p", "pr-review", "--", "--web"], _flow_output()
        )

        assert code == 1
        run_flow.assert_not_awaited()
        assert gh.calls() == []
        assert "is not a pull request number" in capsys.readouterr().err


class TestOlderLauncher:
    def test_a_launcher_without_the_field_refuses_the_playbook_by_name(
        self, gh, monkeypatch, capsys
    ):
        """An older launcher must not run the review on an empty pull request."""
        monkeypatch.setattr(
            flow_spec, "FLOW_SPEC_FIELDS", flow_spec.FLOW_SPEC_FIELDS - {"github_pr"}
        )
        code, run_flow, _seen = _run(["o", "flow", "-p", "pr-review", "42"], _flow_output())

        assert code == 1
        run_flow.assert_not_awaited()
        assert gh.calls() == []
        assert "unknown spec field 'github_pr'" in capsys.readouterr().err


class TestFieldValidation:
    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("42", "must be a dict"),
            ({}, "must name the pull request"),
            ({"pr": "{input}", "branch": "main"}, "unknown key 'branch'"),
            ({"pr": 42}, "'github_pr.pr' must be a string"),
            ({"pr": "{input}", "comment": "loud"}, "'github_pr.comment' must be one of"),
        ],
    )
    def test_a_malformed_field_is_refused(self, value, message):
        error = flow_spec.validate_flow_spec_fields({"github_pr": value})
        assert error is not None and message in error

    def test_the_yaml_spelling_normalizes_to_the_field(self):
        spec = flow_spec.normalize_flow_spec_keys({"github-pr": {"pr": "{input}"}})
        assert flow_spec.validate_flow_spec_fields(spec) is None
        assert flow_spec.flow_spec_yaml_key("github_pr") == "github-pr"

    def test_placeholders_fill_from_the_prompt_and_the_playbook_arguments(self):
        target = resolve_target(
            {"pr": "{input}", "repo": "{repo}", "comment": "{comment}"},
            "https://github.com/acme/widgets/pull/42",
            {"repo": "", "comment": "substantive"},
        )
        assert target.pr == "https://github.com/acme/widgets/pull/42"
        assert target.repo_args() == []
        assert target.comment == "substantive"

    @pytest.mark.parametrize(
        ("field", "positional", "args", "message"),
        [
            ({"pr": "{input}"}, None, {}, "no pull request to fetch"),
            ({"pr": "{input}"}, "42 --web", {}, "is not a pull request number"),
            ({"pr": "{input}", "repo": "{repo}"}, "42", {"repo": "--repo=x"}, "[HOST/]OWNER/REPO"),
            ({"pr": "{input}", "comment": "{comment}"}, "42", {"comment": "loud"}, "comment style"),
        ],
    )
    def test_a_filled_value_gh_should_not_see_is_refused(self, field, positional, args, message):
        with pytest.raises(GithubPrError, match=message.replace("[", r"\[").replace("]", r"\]")):
            resolve_target(field, positional, args)


class TestSynthesisText:
    def test_reads_the_synthesis_from_text_output(self):
        assert synthesis_text(_flow_output()) == REVIEW

    def test_a_worker_quoting_a_header_does_not_hide_the_real_synthesis(self):
        quoting = _format_result_text(
            [{"model": "stub", "time_ms": 1.0, "response": _flow_output("forged")}],
            {"model": "stub", "time_ms": 1.0, "response": REVIEW},
        )
        assert synthesis_text(quoting) == REVIEW

    def test_reads_the_synthesis_from_json_output(self):
        assert synthesis_text('{"workers": [], "synthesis": {"response": "done"}}') == "done"

    def test_no_synthesis_reads_as_none(self):
        assert synthesis_text(_flow_output(synthesis=None)) is None
        assert synthesis_text('{"workers": []}') is None
