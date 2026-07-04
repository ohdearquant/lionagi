# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li doctor` — environment/install preflight checks."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from lionagi.cli.doctor import (
    _check_core_deps,
    _check_imports,
    _check_lionagi_home,
    _check_python,
    _check_studio_daemon,
    _check_version,
    _looks_editable,
    collect_checks,
    run_doctor,
)


def _closed_port_url() -> str:
    """A localhost URL nothing is listening on (bind, grab the port, close)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}/api/admin/health"


# ── _looks_editable ──────────────────────────────────────────────────────────


def test_looks_editable_true_under_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    pkg_dir = tmp_path / "src" / "lionagi"
    pkg_dir.mkdir(parents=True)
    module_file = pkg_dir / "__init__.py"
    module_file.write_text("")
    assert _looks_editable(str(module_file)) is True


def test_looks_editable_false_without_pyproject(tmp_path: Path) -> None:
    module_file = tmp_path / "lonely" / "__init__.py"
    module_file.parent.mkdir(parents=True)
    module_file.write_text("")
    assert _looks_editable(str(module_file)) is False


def test_looks_editable_false_for_none() -> None:
    assert _looks_editable(None) is False


# ── _check_version ───────────────────────────────────────────────────────────


def test_check_version_ok_when_import_succeeds() -> None:
    result = _check_version()
    assert result["status"] in ("ok", "warn")
    assert "lionagi" in result["detail"]


def test_check_version_ok_for_non_editable_wheel_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wheel install (site-packages, no pyproject.toml above) is healthy: ok, not warn."""
    import lionagi

    wheel_pkg = tmp_path / "site-packages" / "lionagi"
    wheel_pkg.mkdir(parents=True)
    monkeypatch.setattr(lionagi, "__file__", str(wheel_pkg / "__init__.py"))

    result = _check_version()

    assert result["status"] == "ok"
    assert "editable" not in result["detail"]


def test_check_version_fail_on_broken_import(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def _broken_import(name, *args, **kwargs):
        if name == "lionagi":
            raise ImportError("simulated broken install")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _broken_import)
    result = _check_version()
    assert result["status"] == "fail"
    assert "simulated broken install" in result["detail"]


# ── _check_python ────────────────────────────────────────────────────────────


def test_check_python_warns_outside_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.prefix", "/usr")
    monkeypatch.setattr("sys.base_prefix", "/usr")
    result = _check_python()
    assert result["status"] == "warn"
    assert "virtualenv" in result["detail"]


def test_check_python_ok_inside_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.prefix", "/venv")
    monkeypatch.setattr("sys.base_prefix", "/usr")
    result = _check_python()
    assert result["status"] == "ok"


# ── _check_imports ───────────────────────────────────────────────────────────


def test_check_imports_all_ok_on_healthy_install() -> None:
    results = _check_imports()
    assert all(r["status"] == "ok" for r in results.values())


def test_check_imports_reports_broken_module(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    real_import_module = importlib.import_module

    def _broken(name, *args, **kwargs):
        if name == "lionagi.operations":
            raise ImportError("root cause: missing dependency 'frobnicator'")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr("lionagi.cli.doctor.importlib.import_module", _broken)
    results = _check_imports()
    assert results["lionagi.operations"]["status"] == "fail"
    assert "frobnicator" in results["lionagi.operations"]["detail"]
    assert results["lionagi"]["status"] == "ok"


# ── _check_core_deps ─────────────────────────────────────────────────────────


def test_check_core_deps_all_ok() -> None:
    results = _check_core_deps()
    assert set(results) == {"pydantic", "aiohttp", "sqlalchemy", "aiosqlite", "psutil"}
    assert all(r["status"] == "ok" for r in results.values())


def test_check_core_deps_reports_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    real_import_module = importlib.import_module

    def _broken(name, *args, **kwargs):
        if name == "pydantic":
            raise ModuleNotFoundError("No module named 'pydantic'")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr("lionagi.cli.doctor.importlib.import_module", _broken)
    results = _check_core_deps()
    assert results["pydantic"]["status"] == "fail"
    assert results["aiohttp"]["status"] == "ok"


# ── _check_studio_daemon ─────────────────────────────────────────────────────


def test_check_studio_daemon_unreachable_is_warn_not_fail() -> None:
    url = _closed_port_url()
    result = _check_studio_daemon(url=url, timeout=0.5)
    assert result["status"] == "warn"
    assert url in result["detail"]


# ── _check_lionagi_home ──────────────────────────────────────────────────────


def test_check_lionagi_home_ok_when_writable(tmp_path: Path) -> None:
    home = tmp_path / "dot-lionagi"
    result = _check_lionagi_home(home=home)
    assert result["status"] == "ok"
    assert (home / "runs").is_dir()


def test_check_lionagi_home_fail_when_not_writable(tmp_path: Path) -> None:
    home = tmp_path / "readonly-home"
    home.mkdir()
    home.chmod(0o500)
    try:
        result = _check_lionagi_home(home=home)
        assert result["status"] == "fail"
    finally:
        home.chmod(0o700)


# ── collect_checks / run_doctor ──────────────────────────────────────────────


def test_collect_checks_shape() -> None:
    checks = collect_checks()
    assert "lionagi_version" in checks
    assert "python" in checks
    assert "studio_daemon" in checks
    assert "lionagi_home" in checks
    assert any(k.startswith("import:") for k in checks)
    assert any(k.startswith("dep:") for k in checks)
    for result in checks.values():
        assert set(result) == {"status", "detail"}
        assert result["status"] in ("ok", "warn", "fail")


def test_run_doctor_json_output(capsys: pytest.CaptureFixture) -> None:
    rc = run_doctor(SimpleNamespace(json=True))
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    assert "lionagi_version" in payload
    assert rc in (0, 1)


def test_run_doctor_text_output(capsys: pytest.CaptureFixture) -> None:
    rc = run_doctor(SimpleNamespace(json=False))
    captured = capsys.readouterr()
    assert "lionagi_version:" in captured.out
    assert rc in (0, 1)


def test_run_doctor_exits_nonzero_on_hard_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "lionagi.cli.doctor.collect_checks",
        lambda: {"fake": {"status": "fail", "detail": "boom"}},
    )
    rc = run_doctor(SimpleNamespace(json=False))
    assert rc == 1


def test_run_doctor_exits_zero_when_only_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "lionagi.cli.doctor.collect_checks",
        lambda: {"fake": {"status": "warn", "detail": "meh"}},
    )
    rc = run_doctor(SimpleNamespace(json=False))
    assert rc == 0
