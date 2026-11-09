# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Setup shared by module and adapter contracts."""

from __future__ import annotations

from lionagi.plugins.discovery import discover_plugins
from lionagi.plugins.trust import trust_plugin

MANIFEST = """\
name: {name}
version: "0.1.0"
lionagi: ">=0.0,<100.0"

capabilities:
  agents: [agents/{agent}.md]
"""


def _trust(dir_name: str) -> None:
    d = next(x for x in discover_plugins() if x.dir_name == dir_name)
    trust_plugin(d)


def _write_plugin_profile(write_plugin, dir_name: str, name: str, agent: str, body: str):
    write_plugin(
        dir_name,
        MANIFEST.format(name=name, agent=agent),
        files={f"agents/{agent}.md": body},
    )
    _trust(dir_name)
