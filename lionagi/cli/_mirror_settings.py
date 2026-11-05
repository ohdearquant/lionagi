# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Settings read from the environment by the transcript mirror.

Standard library only: the studio configuration and the mirror service both
read these values at import time, so this module must not import either.
"""

from __future__ import annotations

import os

# Bounded display preview stored in messages.content for mirror-ingested rows
# (Unicode code points, not bytes). 0 is valid (empty preview + pointer only).
# Negative values are a configuration error — there is no "unbounded" sentinel.
MIRROR_PREVIEW_CHARS: int = int(os.environ.get("LIONAGI_STUDIO_MIRROR_PREVIEW_CHARS", "500"))
if MIRROR_PREVIEW_CHARS < 0:
    raise ValueError(
        f"LIONAGI_STUDIO_MIRROR_PREVIEW_CHARS must be >= 0, got {MIRROR_PREVIEW_CHARS}"
    )
