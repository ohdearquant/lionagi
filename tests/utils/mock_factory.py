# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Re-export shim. Real home: ``lionagi.testing``.

This file exists so any external test code that imports
``from tests.utils.mock_factory import LionAGIMockFactory`` keeps working
after the move into the library. New tests should import from
``lionagi.testing`` directly.
"""

from lionagi.testing._legacy import LionAGIMockFactory, _get_oai_config

__all__ = ("LionAGIMockFactory", "_get_oai_config")
