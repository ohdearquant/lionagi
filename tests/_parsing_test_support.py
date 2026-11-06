# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Setup shared by module and adapter contracts."""

from __future__ import annotations

import urllib.parse


def _port_of(url: str) -> int:
    port = urllib.parse.urlsplit(url).port
    assert port is not None
    return port
