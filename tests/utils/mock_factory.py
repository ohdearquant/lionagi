# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Re-export shim for lionagi.testing; exists for backwards compatibility with tests.utils.mock_factory imports."""

from lionagi.testing._legacy import LionAGIMockFactory, _get_oai_config

__all__ = ("LionAGIMockFactory", "_get_oai_config")
