from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _schema_files() -> list[Path]:
    return sorted(
        path
        for path in (REPO_ROOT / "lionagi" / "providers").rglob("*.py")
        if path.name == "_schemas.py" or path.name.endswith("_schemas.py")
    )


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)


def _is_register_decorator(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return isinstance(target, ast.Attribute) and target.attr == "register"


def _run_fresh(script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(REPO_ROOT), env.get("PYTHONPATH"))))
    return subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def test_schema_only_modules_have_no_registration_decorators() -> None:
    files = _schema_files()
    observed = {path.relative_to(REPO_ROOT).as_posix() for path in files}
    expected = {
        "lionagi/providers/openai/_chat_schemas.py",
        "lionagi/providers/openai/_audio_schemas.py",
        "lionagi/providers/exa/_schemas.py",
        "lionagi/providers/firecrawl/_schemas.py",
    }
    assert expected <= observed

    violations: list[tuple[str, int]] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for decorator in getattr(node, "decorator_list", ()):
                if _is_register_decorator(decorator):
                    violations.append((path.relative_to(REPO_ROOT).as_posix(), node.lineno))
    assert violations == []


def test_importing_schema_only_modules_does_not_register_endpoints() -> None:
    modules = [_module_name(path) for path in _schema_files()]
    script = f"""
import importlib
from lionagi.service.connections.registry import EndpointRegistry

assert EndpointRegistry._loaded is False
assert EndpointRegistry._entries == []
for module in {json.dumps(modules)}:
    before = tuple(EndpointRegistry._entries)
    importlib.import_module(module)
    assert tuple(EndpointRegistry._entries) == before, module
assert EndpointRegistry._loaded is False
"""
    _run_fresh(script)


def test_resolving_ollama_chat_schema_does_not_register_endpoint() -> None:
    script = """
from lionagi.service.connections.registry import EndpointRegistry
assert EndpointRegistry._loaded is False
assert EndpointRegistry._entries == []

from lionagi.providers.ollama._config import OllamaConfigs
resolved = OllamaConfigs.CHAT.options

assert resolved.__name__ == "OpenAIChatCompletionsRequest"
assert EndpointRegistry._loaded is False
assert EndpointRegistry._entries == []
"""
    _run_fresh(script)
