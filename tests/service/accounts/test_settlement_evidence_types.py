from __future__ import annotations

import pytest

from lionagi.service.accounts._types import (
    PricingInputs,
    Rate,
    RejectionZero,
    StreamUsageEstimate,
    UsageCounts,
    UsageRates,
)


def test_rejection_zero_defaults_the_400_class_and_normalizes_stream_request_id():
    assert (
        RejectionZero(400, "invalid_request_error", None, "request_rejected").rejection_class
        == "other"
    )
    estimate = StreamUsageEstimate(
        "cancelled",
        "model",
        UsageCounts(1, 0, 0, 0, 1),
        "contains space",
        PricingInputs(
            10, (), None, "messages_standard_v1", "max_applicable_at_reserve", Rate(1, 1)
        ),
        "reservation",
        UsageRates(*(Rate(1, 1) for _ in range(5))),
        1,
        1024,
        1,
        2,
    )
    assert estimate.request_id is None


@pytest.mark.parametrize(
    "build",
    [
        lambda: Rate(True, 1),
        lambda: Rate(1, 0),
        lambda: UsageCounts(-1, 0, 0, 0, 0),
        lambda: PricingInputs(
            1, ("1h", "5m"), None, "messages_standard_v1", "max_applicable_at_reserve", Rate(1, 1)
        ),
        lambda: RejectionZero(400, "overloaded_error", None, "request_rejected"),
        lambda: RejectionZero(400, "invalid_request_error", "unknown", "request_rejected"),
        lambda: StreamUsageEstimate(
            "mystery",
            "model",
            UsageCounts(0, 0, 0, 0, 0),
            None,
            PricingInputs(
                1, (), None, "messages_standard_v1", "max_applicable_at_reserve", Rate(1, 1)
            ),
            "reservation",
            UsageRates(*(Rate(1, 1) for _ in range(5))),
            0,
            0,
            0,
            0,
        ),
    ],
)
def test_evidence_types_refuse_malformed_values(build):
    with pytest.raises(ValueError):
        build()
