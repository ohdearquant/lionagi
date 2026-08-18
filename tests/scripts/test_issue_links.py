"""The closing-keyword gate: what it reads as a closure, and what it refuses to resolve."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_issue_links", Path(__file__).resolve().parents[2] / "scripts" / "check_issue_links.py"
)
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)

REPO = "owner/repo"


class TestNegatedProseIsNotAClosure:
    @pytest.mark.parametrize(
        "body",
        [
            "This PR does not close #1234.",
            "It won't fix #1234 either.",
            "This never resolves #1234.",
            "Landing this does not yet close #1234.",
        ],
    )
    def test_a_negated_keyword_does_not_count(self, body):
        assert not gate.closed_by_body(body, REPO, 1234)
        assert gate.negated_closure(body, REPO, 1234)

    def test_a_plain_keyword_still_counts(self):
        """The control: without it, a function returning False always would pass the arms above."""
        assert gate.closed_by_body("Closes #1234.", REPO, 1234)
        assert not gate.negated_closure("Closes #1234.", REPO, 1234)

    def test_a_negation_in_an_earlier_clause_does_not_reach_a_later_closure(self):
        assert gate.closed_by_body("This is not a revert. Closes #1234.", REPO, 1234)

    def test_one_real_closure_beside_a_negated_one_counts(self):
        body = "Closes #1234 and does not close #5678."
        assert gate.closed_by_body(body, REPO, 1234)
        assert not gate.closed_by_body(body, REPO, 5678)

    def test_the_repo_qualified_spelling_is_read_the_same_way(self):
        assert not gate.closed_by_body(f"does not close {REPO}#1234", REPO, 1234)
        assert gate.closed_by_body(f"closes {REPO}#1234", REPO, 1234)


class TestTheReferenceCountIsBounded:
    def test_the_bound_is_actually_a_bound(self):
        """The arms below size their bodies off MAX_REFS, so they cannot see the bound being raised."""
        assert 0 < gate.MAX_REFS <= 100

    def test_an_overflow_resolves_nothing_and_fails(self, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(gate, "fetch_issue", lambda repo, n: calls.append(n))
        monkeypatch.setenv("REPO", REPO)
        monkeypatch.setenv("PR_BRANCH", "")
        monkeypatch.setenv(
            "PR_BODY", " ".join(f"#{n}" for n in range(1000, 1000 + gate.MAX_REFS + 1))
        )

        assert gate.main() == 1
        assert calls == [], "an overflowing body still spent API calls"
        assert "Nothing was verified" in capsys.readouterr().out

    def test_a_body_at_the_cap_is_resolved(self, monkeypatch, capsys):
        """The control: the arm above must fail on the count, not on every body."""
        calls = []
        monkeypatch.setattr(gate, "fetch_issue", lambda repo, n: calls.append(n) or None)
        monkeypatch.setenv("REPO", REPO)
        monkeypatch.setenv("PR_BRANCH", "")
        monkeypatch.setenv("PR_BODY", " ".join(f"#{n}" for n in range(1000, 1000 + gate.MAX_REFS)))

        assert gate.main() == 0
        assert len(calls) == gate.MAX_REFS
        assert "No phase-tracking issues referenced" in capsys.readouterr().out


class TestWhatCountsAsAReference:
    def test_the_branch_name_contributes_numbers(self):
        assert gate.refs_from("", "fix/adr-0119-3316-order") == {119, 3316}

    def test_only_adr_titles_that_are_not_epics_are_governed(self):
        assert gate.is_phase_tracking("ADR-0119 Phase 3: structural keys")
        assert not gate.is_phase_tracking("epic(ADR-0119): unify identity")
        assert not gate.is_phase_tracking("fix a flaky test")

    def test_refs_only_exempts_the_number_it_names(self):
        body = "Refs-only: #1234\nsomething else"
        assert gate.declared_reference_only(body, 1234)
        assert not gate.declared_reference_only(body, 5678)
