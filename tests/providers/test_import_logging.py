# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "lionhub.providers.anthropic.claude_code",
        "lionhub.providers.openai.codex",
        "lionhub.providers.pi.cli",
    ],
)
def test_import_preserves_root_logger_in_fresh_interpreter(module):
    code = """
import importlib
import logging
import sys
root = logging.getLogger()
before = root.level, list(root.handlers)
importlib.import_module(sys.argv[1])
assert (root.level, list(root.handlers)) == before
"""
    result = subprocess.run([sys.executable, "-c", code, module], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
