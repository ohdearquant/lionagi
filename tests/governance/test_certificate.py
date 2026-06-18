# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lionagi.governance.certificate import CertificateGrade, TaskCertificate
from lionagi.governance.evidence import GENESIS_HASH


def _now():
    return datetime.now(tz=timezone.utc)


def _mint(**kwargs):
    defaults = dict(
        task_id="t1",
        evidence_chain_head=GENESIS_HASH,
        started_at=_now(),
        completed_at=_now(),
        op_count=0,
        ops_allowed=0,
        ops_denied=0,
        ops_advisory=0,
    )
    defaults.update(kwargs)
    return TaskCertificate.mint(**defaults)


class TestCertificateGrade:
    def test_full_when_no_denials_no_advisories(self):
        cert = _mint(op_count=3, ops_allowed=3)
        assert cert.grade == CertificateGrade.FULL

    def test_partial_when_only_advisories(self):
        cert = _mint(op_count=2, ops_allowed=1, ops_advisory=1)
        assert cert.grade == CertificateGrade.PARTIAL

    def test_failed_when_any_denial(self):
        cert = _mint(op_count=2, ops_allowed=1, ops_denied=1)
        assert cert.grade == CertificateGrade.FAILED

    def test_failed_takes_precedence_over_advisory(self):
        cert = _mint(op_count=3, ops_allowed=1, ops_denied=1, ops_advisory=1)
        assert cert.grade == CertificateGrade.FAILED

    def test_unverified_chain_forces_failed(self):
        cert = _mint(op_count=3, ops_allowed=3, chain_verified=False)
        assert cert.grade == CertificateGrade.FAILED
        assert cert.chain_verified is False

    def test_verified_chain_defaults_true(self):
        cert = _mint(op_count=1, ops_allowed=1)
        assert cert.chain_verified is True
        assert cert.grade == CertificateGrade.FULL

    def test_fields_stored(self):
        t0 = _now()
        t1 = _now()
        cert = TaskCertificate.mint(
            task_id="run-99",
            evidence_chain_head="abc",
            started_at=t0,
            completed_at=t1,
            op_count=5,
            ops_allowed=4,
            ops_denied=1,
            ops_advisory=0,
            gate_results_summary={"g1": 2},
        )
        assert cert.task_id == "run-99"
        assert cert.evidence_chain_head == "abc"
        assert cert.op_count == 5
        assert cert.gate_results_summary == {"g1": 2}

    def test_is_element(self):
        from lionagi.protocols.generic.element import Element

        cert = _mint()
        assert isinstance(cert, Element)
        assert cert.id is not None
