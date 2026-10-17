# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The Docker ports scripts/build-studio-docker.sh --run publishes.

The image's uvicorn binds 0.0.0.0 inside the container, so a bare `PORT:PORT`
mapping in `docker run` puts the daemon on every host interface, where anyone on
the network reaches an unauthenticated API. The script's `--run` mode therefore
writes the same loopback host half `li studio --docker` does.
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build-studio-docker.sh"


def _published_mappings() -> list[str]:
    """The `-p` values the script's --run block hands docker."""
    text = SCRIPT.read_text()
    assert "--run" in text, "the script no longer has a --run mode"
    body = text.split("--run", maxsplit=1)[1]
    return re.findall(r"-p (\S+)", body)


def test_run_publishes_the_api_on_loopback():
    assert "127.0.0.1:8765:8765" in _published_mappings()


def test_run_publishes_the_frontend_on_loopback():
    assert "127.0.0.1:3000:3000" in _published_mappings()


def test_run_writes_a_host_half_on_every_mapping():
    """An omitted host half is every interface, which is what this avoids."""
    mappings = _published_mappings()
    assert mappings, "no -p mapping found in the --run block"
    for mapping in mappings:
        assert mapping.count(":") == 2, f"{mapping!r} has no host half"
