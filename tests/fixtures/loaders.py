# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Re-export shim. Real home: ``lionagi.testing.loaders``.

Data files are now bundled at ``lionagi/testing/data/`` — the loader looks
there by default, so this shim works without copying files around.
"""

from lionagi.testing.loaders import (
    TestDataLoader,
    get_api_response,
    get_conversation,
    get_error_scenario,
    load_test_data,
)

__all__ = (
    "TestDataLoader",
    "get_api_response",
    "get_conversation",
    "get_error_scenario",
    "load_test_data",
)
