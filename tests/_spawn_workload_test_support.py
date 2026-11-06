# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Setup shared by module and adapter contracts."""

from __future__ import annotations

_LIGHT_ROLE = "---\nmodel: codex/gpt-5.5\nworkload: light\n---\n\nRead.\n"


_PLAIN_ROLE = "---\nmodel: codex/gpt-5.5\n---\n\nWork.\n"


def _named_as_a_whole_word(field: str) -> str:
    return rf"(?<![a-z_]){field}(?![a-z_])"
