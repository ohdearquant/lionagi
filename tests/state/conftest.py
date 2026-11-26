# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Fixtures for the state tests that stay in this tree.

These tests read the Studio application as well as the state store, and the hub's test tree imports
no Studio application module, so they live here. ``pg_url`` is defined beside the hub's state tests;
imported here, it resolves to the same worker database.
"""

from lionhub_tests.state.conftest import pg_url  # noqa: F401
