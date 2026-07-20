# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Fixture for test_registry.py: simulates a real bug during provider import.

Raises an ``ImportError`` whose ``name`` looks like an absent third-party
package, even though nothing about this module is actually missing -- the
forged-exception shape a broken bundled module (or an attacker-controlled
one) could produce.
"""

raise ImportError("simulated bug during provider import", name="not_actually_a_missing_dependency")
