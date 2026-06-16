# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Fixtures for tests/operations/; adds legacy branch_with_mock_imodel alias over make_mocked_branch."""

import pytest


@pytest.fixture
def branch_with_mock_imodel(make_mocked_branch):
    """Legacy fixture name — single branch with a string-response mock iModel."""
    return make_mocked_branch(
        name="BranchForTests",
        user="tester_fixture",
        response="mocked_response_string",
        model="gpt-4.1-mini",
    )
