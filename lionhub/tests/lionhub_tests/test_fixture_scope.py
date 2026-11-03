"""The repository test configuration also applies to the hub test tree."""

import os
from pathlib import Path


def test_shared_isolation_and_fixtures(test_data_loader):
    assert Path(os.environ["HOME"]).name.startswith("lionagi-tests-home-")
    assert Path(os.environ["CODEX_HOME"]).name.startswith("lionagi-tests-codex-")
    home = Path(os.environ["LIONAGI_HOME"])
    if os.environ.get("LIONAGI_TEST_HOME"):
        assert home == Path(os.environ["LIONAGI_TEST_HOME"])
    else:
        assert home.name.startswith("lionagi-tests-")
    assert not any(key.upper() == "LIONAGI_STATE_DB_URL" for key in os.environ)
    assert "LIONAGI_AGENT_DEPTH" not in os.environ
    assert "LIONAGI_MAX_SUBAGENTS" not in os.environ
    assert "LIONAGI_STUDIO_FRONTEND_DIST" not in os.environ
    assert test_data_loader is not None


async def test_shared_async_marker():
    assert True
