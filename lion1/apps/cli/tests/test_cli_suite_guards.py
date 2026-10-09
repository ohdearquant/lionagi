"""The root suite's two autouse guards reach this suite through its conftest: a khive binary or a provider
CLI found outside the session's temporary directory is refused, and a reached `kkernel` runs without its
daemon."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


def test_the_launch_guard_and_the_no_daemon_setting_reach_the_cli_suite(refused_launches):
    assert os.environ.get("KHIVE_NO_DAEMON") == "1"
    elsewhere = Path(tempfile.mkdtemp(prefix="lion1-cli-guard-"))
    try:
        (elsewhere / "kkernel").write_text(f"#!/bin/sh\ntouch '{elsewhere / 'ran'}'\n")
        (elsewhere / "kkernel").chmod(0o755)
        with pytest.raises(FileNotFoundError, match="launches_cli"):
            subprocess.run([str(elsewhere / "kkernel")], check=False)
        assert not (elsewhere / "ran").exists()
    finally:
        shutil.rmtree(elsewhere)
    refused_launches.expect([str(elsewhere / "kkernel")])
