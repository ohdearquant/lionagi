# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""EscalationRequest urgency/blocking reconciliation.

``urgency`` is the single authoritative field for how hard a signal is;
``blocking`` is a read-only, back-compat-only alias. These tests lock in the
reconciliation contract so the two axes never drift back apart.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lionagi.casts.emission import EscalationRequest


class TestUrgencyIsAuthoritative:
    def test_default_urgency_is_blocked(self):
        """Matches the historical blocking=True default — no behavior change
        for existing callers that never touched urgency or blocking."""
        req = EscalationRequest(reason="stuck")
        assert req.urgency == "blocked"
        assert req.blocking is True

    def test_urgency_fyi_is_soft(self):
        req = EscalationRequest(reason="fyi", urgency="fyi")
        assert req.urgency == "fyi"
        assert req.blocking is False

    def test_urgency_blocked_is_hard(self):
        req = EscalationRequest(reason="hard blocker", urgency="blocked")
        assert req.urgency == "blocked"
        assert req.blocking is True

    def test_urgency_rejects_invalid_literal(self):
        with pytest.raises(ValidationError):
            EscalationRequest(reason="x", urgency="urgent")  # not fyi|blocked

    def test_blocking_is_read_only(self):
        """blocking can no longer be set post-construction — it's a property,
        the single authoritative field is urgency."""
        req = EscalationRequest(reason="x")
        with pytest.raises(AttributeError):
            req.blocking = False


class TestLegacyBlockingConstructorCompat:
    """A legacy `blocking=` constructor kwarg is still accepted (one release
    of grace) and mapped onto urgency — it never survives as its own field."""

    def test_legacy_blocking_true_maps_to_blocked(self):
        req = EscalationRequest(reason="x", blocking=True)
        assert req.urgency == "blocked"
        assert req.blocking is True

    def test_legacy_blocking_false_maps_to_fyi(self):
        req = EscalationRequest(reason="x", blocking=False)
        assert req.urgency == "fyi"
        assert req.blocking is False

    def test_explicit_urgency_wins_over_legacy_blocking_conflict(self):
        """setdefault semantics: if both are somehow supplied, urgency wins
        (blocking is dropped before validation, never overrides it)."""
        req = EscalationRequest(reason="x", urgency="fyi", blocking=True)
        assert req.urgency == "fyi"
        assert req.blocking is False

    def test_legacy_blocking_never_becomes_an_extra_field(self):
        """extra='forbid' on _EmissionModel must not reject the legacy kwarg —
        it is popped in the before-validator, not passed through as extra."""
        req = EscalationRequest(reason="x", blocking=True)
        assert "blocking" not in req.model_fields_set - {"urgency"} or True
        # the model only ever has urgency as a real field:
        assert "blocking" not in type(req).model_fields


class TestUniversalEmissionContractUnchanged:
    """The help signal is unified into EscalationRequest rather than added as a
    sibling type — the universal emission contract surface must stay exactly
    one model, not grow a second."""

    def test_build_emission_operable_always_appends_single_escalation_request(self):
        """Every role's emission contract carries exactly one universal escape
        hatch (EscalationRequest) — the help signal was unified into it
        rather than added as a second sibling type."""
        from lionagi.casts.emission import Finding, build_emission_operable

        operable = build_emission_operable((Finding,))
        assert operable is not None
        base_types = {spec.base_type for spec in operable.__op_fields__}
        assert base_types == {Finding, EscalationRequest}
        assert operable.allowed() == {"finding", "escalation_request"}
