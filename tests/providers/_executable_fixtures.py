# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Executable metadata for tests that replace the process launch boundary."""

from __future__ import annotations

from pathlib import Path


def cli_executable(tmp_path: Path, name: str) -> str:
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    executable = directory / name
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    return str(executable)
