# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Run-local, detached version1 projection of private API observations."""

from __future__ import annotations

import math

from lionagi.ln import copy
from lionagi.operations._api_hooks import _safe_identifier
from lionagi.service._api_telemetry import InputDecision


def _identifier(value: str | None) -> str | None:
    safe = _safe_identifier(value)
    return None if safe == "unknown" else safe


def _count(value: int | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _scale(value: float | None) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value >= 1 else None
    return None


class APIUsageCollector:
    def __init__(self, *, run_id: str, branch_id: str) -> None:
        self.closed = False
        self._requests: list[dict] = []
        self._guard_calls: set[str] = set()
        self._labels: list[dict] = []
        self._label_pairs: set[tuple[str, str]] = set()
        self._run_id = run_id
        self._branch_id = branch_id

    def record_request(
        self, *, turn_index: int, decision: InputDecision, api_call_id: str | None
    ) -> int | None:
        if self.closed:
            return None
        index = len(self._requests) + 1
        self._requests.append(
            {
                "request_index": index,
                "turn_index": turn_index,
                "api_call_id": _identifier(api_call_id),
                "provider": _identifier(decision.provider),
                "model": _identifier(decision.model),
                "model_semantics": "requested",
                "prompt_chars": _count(decision.prompt_chars),
                "input_decision": {
                    "kind": decision.kind,
                    "estimate": _count(decision.estimate),
                    "cap": _count(decision.cap),
                    "counted": _count(decision.counted),
                    "scale": _scale(decision.scale),
                },
                "dispatch_started": False,
                "selected_attempts": [],
            }
        )
        return index

    def _row(self, request_index: int) -> dict | None:
        if self.closed or not 1 <= request_index <= len(self._requests):
            return None
        return self._requests[request_index - 1]

    def mark_dispatch(self, request_index: int) -> None:
        if row := self._row(request_index):
            row["dispatch_started"] = True

    def record_selected_attempt(
        self,
        request_index: int,
        *,
        attempt_id: str,
        provider: str,
        account_label: str | None,
    ) -> None:
        row = self._row(request_index)
        attempt_id = _identifier(attempt_id)
        if row is None or attempt_id is None:
            return
        if any(item["attempt_id"] == attempt_id for item in row["selected_attempts"]):
            return
        provider = _identifier(provider)
        account_label = _identifier(account_label)
        row["selected_attempts"].append(
            {"attempt_id": attempt_id, "provider": provider, "account_label": account_label}
        )
        if provider is not None and account_label is not None:
            pair = (provider, account_label)
            if pair not in self._label_pairs:
                self._label_pairs.add(pair)
                self._labels.append({"provider": provider, "label": account_label})

    def record_guard_refusal(self, *, call_id: str) -> None:
        if not self.closed and (safe_id := _identifier(call_id)) is not None:
            self._guard_calls.add(safe_id)

    def snapshot(self) -> dict:
        return copy(
            {
                "schema_version": 1,
                "run_id": self._run_id,
                "branch_id": self._branch_id,
                "requests": self._requests,
                "guard_refusal_count": len(self._guard_calls),
                "guard_refusal_coverage": "act_policy_denial_v1",
                "account_labels": self._labels,
            }
        )

    def close(self) -> dict:
        self.closed = True
        return self.snapshot()
