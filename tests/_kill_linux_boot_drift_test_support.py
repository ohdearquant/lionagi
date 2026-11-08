# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Setup shared by module and adapter contracts."""

import json


def _imported_run_manifest(**overrides) -> str:
    """A running manifest recording a live process; a ``None`` override drops that field."""
    manifest = {
        "status": "running",
        "pid": 4242,
        "pid_create_time": 1005.0,
        "pid_boot_time": 1000.0,
    }
    manifest.update(overrides)
    return json.dumps({key: value for key, value in manifest.items() if value is not None})
