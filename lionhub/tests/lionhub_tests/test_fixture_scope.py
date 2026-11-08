"""The repository test configuration also applies to the hub test tree."""

import ast
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


def test_hub_tests_do_not_require_a_separate_absolute_import_root():
    root = Path(__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and not node.level:
                if node.module == "lionhub_tests" or (node.module or "").startswith(
                    "lionhub_tests."
                ):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                if any(
                    alias.name == "lionhub_tests" or alias.name.startswith("lionhub_tests.")
                    for alias in node.names
                ):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, offenders
