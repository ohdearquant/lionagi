from __future__ import annotations

import subprocess
import sys

_MODULES = (
    "lionhub.orchestration.scheduler.settings",
    "lionhub.orchestration.scheduler.liveness",
    "lionhub.orchestration.scheduler.show_artifacts",
)


def _run_isolated(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )


def test_scheduler_package_import_is_lazy_in_fresh_process() -> None:
    result = _run_isolated(
        "import sys; import lionhub.orchestration.scheduler; "
        "assert not any(name.startswith('lionhub.orchestration.scheduler.') "
        "for name in sys.modules)"
    )
    assert result.returncode == 0, result.stderr


def test_scheduler_leaves_import_without_studio_or_web_frameworks() -> None:
    for module in _MODULES:
        result = _run_isolated(
            f"import importlib, sys; importlib.import_module('{module}'); "
            "assert not any(name == 'lionagi.studio' or name.startswith('lionagi.studio.') "
            "or name in {'fastapi', 'starlette', 'uvicorn'} for name in sys.modules)"
        )
        assert result.returncode == 0, f"{module}: {result.stderr}"
