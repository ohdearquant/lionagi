"""The scripts under examples/ keep working: each one imports, each scripted one runs to its end, and the
live one, with no key and no CLI to reach a model, stops with one line naming what is missing."""

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
SCRIPTED = ("tools_and_gates", "notes_and_context", "watch_the_artifact", "budgets_and_outcomes")
LIVE = ("backends",)


def run(script: str, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {**env, "PYTHONPATH": os.pathsep.join(filter(None, (str(ROOT), env.get("PYTHONPATH"))))}
    return subprocess.run(
        [sys.executable, str(EXAMPLES / f"{script}.py"), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_every_script_in_examples_is_listed_here():
    assert sorted(p.stem for p in EXAMPLES.glob("*.py")) == sorted(SCRIPTED + LIVE)


@pytest.mark.parametrize("script", SCRIPTED + LIVE)
def test_a_script_imports_without_running_its_main(script, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))  # a script's scratch directory lands here
    spec = importlib.util.spec_from_file_location(f"example_{script}", EXAMPLES / f"{script}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.main)


@pytest.mark.parametrize("script", SCRIPTED)
def test_a_scripted_example_runs_to_its_end(script, tmp_path):
    done = run(script, env={**os.environ, "TMPDIR": str(tmp_path)})
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert done.stdout.count("\n") >= 5, done.stdout


@pytest.mark.parametrize(
    ("backend", "missing"),
    [
        ("openrouter", "OPENROUTER_API_KEY: not in the environment and not in the keychain"),
        ("claude", "claude CLI not found on PATH"),
        ("session", "claude CLI not found on PATH"),
        ("claude-oneshot", "claude CLI not found on PATH"),
        ("codex", "codex CLI not found on PATH"),
    ],
)
def test_the_live_example_with_nothing_to_reach_a_model_says_what_is_missing(backend, missing, tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
    env["PATH"] = str(tmp_path)  # an empty directory: no model CLI, and no keychain tool to find a key
    done = run("backends", "--backend", backend, env=env)
    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr.splitlines() == [f"{backend}: {missing}"]
