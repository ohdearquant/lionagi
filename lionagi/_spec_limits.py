# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Bounds shared by every surface that validates an orchestration spec.

Deliberately importing nothing. Three modules read from here — the CLI spec
validator and the two Studio services — and two of those are request-path
services whose import cost is paid on startup. A constant that arrives with a
module graph behind it charges every one of them for a number.
"""

from __future__ import annotations

# How long a spec file's prompt may be, in characters.
#
# One number, read by every surface that validates a spec. It used to be written
# out three times, which meant three chances for them to disagree and no way to
# raise it in one place.
#
# The bound exists for the pathological file, not for the long prompt. An
# orchestration prompt carries the whole task — the brief, the constraints, the
# exit criteria — and a real one had already been squeezed to fit the old 8192,
# which is close enough to normal writing that an ordinary edit could push a
# working spec over it and kill the run at submit. Set far enough out that no
# honest spec reaches it, while still refusing a file that is not a prompt.
MAX_SPEC_PROMPT_CHARS = 256 * 1024
