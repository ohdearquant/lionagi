# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import logging
import subprocess
import sys
from io import StringIO

import pytest

from lionagi.cli._logging import configure_cli_logging


@pytest.fixture(autouse=True)
def restore_cli_logging_state():
    names = (
        "claude-cli",
        "codex-cli",
        "pi-cli",
        "gemini-cli",
        "lionagi",
        "lionagi.cli.progress",
        "lionagi.cli.hint",
        "lionagi.cli.warn",
        "lionagi.cli.error",
    )
    saved = {
        name: (
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
            list(logging.getLogger(name).handlers),
        )
        for name in names
    }
    logger = logging.getLogger("pi-cli")
    marker = object()
    previous = getattr(logger, "_lionagi_cli_fallback_level", marker)
    yield
    for name, (level, propagate, handlers) in saved.items():
        current = logging.getLogger(name)
        current.setLevel(level)
        current.propagate = propagate
        current.handlers[:] = handlers
    if previous is marker:
        if hasattr(logger, "_lionagi_cli_fallback_level"):
            del logger._lionagi_cli_fallback_level
    else:
        logger._lionagi_cli_fallback_level = previous


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


@pytest.mark.parametrize(
    ("name", "verbose", "visible"),
    [
        ("claude-cli", False, False),
        ("claude-cli", True, True),
        ("codex-cli", False, False),
        ("codex-cli", True, True),
        ("pi-cli", False, True),
        ("pi-cli", True, True),
    ],
)
def test_cli_provider_completion_logging_without_a_root_handler(
    monkeypatch, capsys, name, verbose, visible
):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    for logger_name in (
        "claude-cli",
        "codex-cli",
        "pi-cli",
        "gemini-cli",
        "lionagi",
        "lionagi.cli.progress",
        "lionagi.cli.hint",
        "lionagi.cli.warn",
        "lionagi.cli.error",
    ):
        logger = logging.getLogger(logger_name)
        monkeypatch.setattr(logger, "handlers", [])
        monkeypatch.setattr(logger, "level", logging.NOTSET)
        monkeypatch.setattr(logger, "propagate", True)

    configure_cli_logging(verbose)
    configure_cli_logging(verbose)
    logging.getLogger(name).info("provider completed")
    output = capsys.readouterr().err
    assert output.count(f"INFO:{name}:provider completed") == int(visible)


@pytest.mark.parametrize("name", ["claude-cli", "codex-cli", "pi-cli"])
def test_cli_preserves_existing_root_and_named_handlers(monkeypatch, capsys, name):
    root_output, named_output = StringIO(), StringIO()
    root_handler, named_handler = (
        logging.StreamHandler(root_output),
        logging.StreamHandler(named_output),
    )
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [root_handler])
    monkeypatch.setattr(root, "level", logging.INFO)
    for logger_name in (
        "claude-cli",
        "codex-cli",
        "pi-cli",
        "gemini-cli",
        "lionagi",
        "lionagi.cli.progress",
        "lionagi.cli.hint",
        "lionagi.cli.warn",
        "lionagi.cli.error",
    ):
        logger = logging.getLogger(logger_name)
        monkeypatch.setattr(logger, "handlers", [named_handler] if logger_name == name else [])
        monkeypatch.setattr(logger, "level", logging.NOTSET)
        monkeypatch.setattr(logger, "propagate", True)

    configure_cli_logging(True)
    logging.getLogger(name).info("provider completed")
    assert root_output.getvalue().count("provider completed") == 1
    assert named_output.getvalue().count("provider completed") == 1
    assert capsys.readouterr().err == ""
    assert root.handlers == [root_handler]
    assert root.level == logging.INFO


def test_pi_fallback_returns_to_inherited_level_when_root_is_configured(monkeypatch, capsys):
    root = logging.getLogger()
    logger = logging.getLogger("pi-cli")
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logging.NOTSET)
    monkeypatch.setattr(logger, "propagate", True)
    configure_cli_logging(False)
    logger.info("first")
    assert "first" in capsys.readouterr().err
    output = StringIO()
    root.handlers = [logging.StreamHandler(output)]
    configure_cli_logging(False)
    assert logger.level == logging.NOTSET
    logger.info("hidden")
    logger.warning("visible")
    assert output.getvalue().strip() == "visible"
    assert capsys.readouterr().err == ""


def test_cli_does_not_override_nonpropagating_pi_logger(monkeypatch):
    root = logging.getLogger()
    logger = logging.getLogger("pi-cli")
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logging.NOTSET)
    monkeypatch.setattr(logger, "propagate", False)
    configure_cli_logging(False)
    assert logger.level == logging.NOTSET
    assert not logger.handlers


def test_reconfiguring_cli_changes_provider_verbosity(monkeypatch, capsys):
    root = logging.getLogger()
    logger = logging.getLogger("codex-cli")
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "propagate", True)
    monkeypatch.setattr(logger, "level", logging.NOTSET)
    for verbose in (True, False, True):
        configure_cli_logging(verbose)
        logger.info("provider completed")
        assert capsys.readouterr().err.count("provider completed") == int(verbose)
