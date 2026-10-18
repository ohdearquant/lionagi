# SPDX-License-Identifier: Apache-2.0
import pytest

from agent_workbench.server import agent_workbench_environment as workbench


@pytest.fixture(autouse=True)
def _uid_lock_dir(tmp_path_factory, monkeypatch):
    # Tests that pretend to be root claim uids; their lock files go to a directory of the test's own.
    monkeypatch.setattr(workbench, "_UID_LOCK_DIR", tmp_path_factory.mktemp("uids"))
