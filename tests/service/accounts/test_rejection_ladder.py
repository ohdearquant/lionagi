# SPDX-License-Identifier: Apache-2.0
"""One status ladder settles a rejected Anthropic Messages response.

The non-streamed request, the streamed request and the blocked-account probe all read the
settle reason and the zero-cost decision from the same two helpers. The endpoint tests drive
each path through a stub transport; this table pins the ladder itself over every status
class, so a change to it shows up as one row here, whichever path a caller reaches it on.
"""

from __future__ import annotations

import pytest

from lionhub.providers.anthropic import messages

REASONS = [
    (400, "request_rejected"),
    (401, "auth_required"),
    (402, "request_rejected"),
    (403, "auth_required"),
    (404, "request_rejected"),
    (413, "request_rejected"),
    (429, "rate_limited"),
    (499, "request_rejected"),
    (500, "provider_error"),
    (503, "provider_error"),
    (529, "provider_error"),
    (599, "provider_error"),
    # A non-200 success code reaches the ladder only on the request paths, which read a body
    # from a 200 alone; there it is a rejection.
    (201, "request_rejected"),
    (304, "request_rejected"),
]


@pytest.mark.parametrize(("status", "reason"), REASONS)
def test_each_status_has_one_settle_reason(status, reason):
    assert messages._rejection_reason(status) == reason


ZERO = [
    (400, "invalid_request_error", True),
    (529, "overloaded_error", True),
    # The type a status names is its own; the other status's type does not carry over.
    (400, "overloaded_error", False),
    (529, "invalid_request_error", False),
    (400, "other", False),
    (529, "other", False),
    (400, None, False),
    (529, None, False),
    (500, "overloaded_error", False),
    (503, "overloaded_error", False),
    (401, "authentication_error", False),
    (429, "rate_limit_error", False),
    (413, "request_too_large", False),
    (404, "not_found_error", False),
]


@pytest.mark.parametrize(("status", "error_type", "zero"), ZERO)
def test_only_two_status_and_type_pairs_settle_at_zero(status, error_type, zero):
    assert messages._settles_at_zero(status, error_type) is zero


def test_the_zero_cost_table_is_closed():
    assert dict(messages._ZERO_COST_REJECTION_TYPES) == {
        400: "invalid_request_error",
        529: "overloaded_error",
    }
    with pytest.raises(TypeError):
        messages._ZERO_COST_REJECTION_TYPES[500] = "api_error"  # type: ignore[index]
