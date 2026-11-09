# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Check that schedule declarations and exports load without Studio runtime modules."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from lionhub_tests.orchestration.schedule._test_support_schedule_service_import_closure import (
    _BLOCKER,
    REPO_ROOT,
    _run_child,
)


def test_import_blocker_rejects_scheduler_runtime():
    result = _run_child(
        """
        try:
            import lionagi.studio.scheduler.engine
        except ImportError:
            pass
        else:
            raise AssertionError("blocked scheduler import unexpectedly succeeded")
        print(json.dumps({"refused": refused}))
        """
    )
    assert "lionagi.studio.scheduler" in result["refused"]
