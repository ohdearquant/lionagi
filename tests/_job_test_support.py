# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Allocation-state isolation shared by job and command tests."""

import pytest

from lionhub.orchestration.job import clear_run_allocation


@pytest.fixture(autouse=True)
def _no_run_allocated():
    """Keep the process-level allocation marker from leaking between tests.

    Some tests here set it deliberately, and any other test in the session that
    allocates a run sets it too. Left set, it would silently turn the assertions
    below into their opposite.
    """
    clear_run_allocation()
    yield
    clear_run_allocation()
