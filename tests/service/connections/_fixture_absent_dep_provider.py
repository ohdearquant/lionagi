# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Fixture for test_registry.py: importing this module is a test failure.

Used with a ``_PROVIDER_OPTIONAL_DEPENDENCIES`` entry pointing at a package
that is guaranteed absent, to prove the preflight check skips the import
before this body ever runs.
"""

raise RuntimeError("this fixture provider module must never be imported")
