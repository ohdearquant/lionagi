# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Path constants — leaf module, no lionagi deps. Imported by state, cli, etc."""

import os
from pathlib import Path

LIONAGI_HOME: Path = Path(os.environ.get("LIONAGI_HOME", Path.home() / ".lionagi")).expanduser()
