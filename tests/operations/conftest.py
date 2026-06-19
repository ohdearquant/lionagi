# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for tests/operations/."""

import pytest


@pytest.fixture
def branch_with_mock_imodel(make_mocked_branch):
    """Single branch with a string-response mock iModel (legacy alias)."""
    return make_mocked_branch(
        name="BranchForTests",
        user="tester_fixture",
        response="mocked_response_string",
        model="gpt-4.1-mini",
    )
