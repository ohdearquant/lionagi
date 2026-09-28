"""The scripts under examples/ keep working: each one imports, each scripted one runs to its end, and the
live one, with no key and no CLI to reach a model, stops with one line naming what is missing."""

import ast
import asyncio
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
NOTEBOOKS = ROOT / "notebooks"
SCRIPTED = ("tools_and_gates", "notes_and_context", "watch_the_artifact", "budgets_and_outcomes")
LIVE = ("backends",)
# tests/test_bench_agent.py drives this one: a fake store, and a child that writes a ledger
OWN_TESTS = ("bench_agent",)


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
    assert sorted(p.stem for p in EXAMPLES.glob("*.py")) == sorted(SCRIPTED + LIVE + OWN_TESTS)


def load(script: str):
    spec = importlib.util.spec_from_file_location(f"example_{script}", EXAMPLES / f"{script}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("script", SCRIPTED + LIVE)
def test_a_script_imports_without_running_its_main(script, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))  # a script's scratch directory lands here
    assert callable(load(script).main)


@pytest.mark.parametrize("script", SCRIPTED)
def test_a_scripted_example_runs_to_its_end_and_takes_its_scratch_with_it(script, tmp_path):
    done = run(script, env={**os.environ, "TMPDIR": str(tmp_path)})
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert done.stdout.count("\n") >= 5, done.stdout
    assert sorted(tmp_path.iterdir()) == []  # a scratch directory made on import is gone at exit


def test_the_live_example_says_what_the_calls_cost_when_a_later_call_fails(tmp_path, monkeypatch, capsys):
    """The table of calls is printed on the way out of a failure too: the calls before it were paid."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    module = load("backends")

    class Backend:
        def __init__(self):
            self.calls = []

        async def __call__(self, messages):
            if self.calls:
                raise module.OpenRouterError("429 rate limited")
            self.calls.append(
                {
                    "usage": {"input_tokens": 12, "output_tokens": 3},
                    "total_cost_usd": 0.00021,
                    "finish_reason": "stop",
                    "provider": "p",
                    "duration_ms": 40,
                }
            )
            return '<lact c1>word_count(text="a b")</lact>'

    backend = Backend()
    monkeypatch.setattr(module, "build_backend", lambda kind, model: backend)
    with pytest.raises(module.OpenRouterError, match="429"):
        asyncio.run(module.main("openrouter", None))
    out = capsys.readouterr().out
    assert "   1     40     12      0      3      0   0.00021  stop / p" in out.splitlines()
    assert "openrouter:" not in out  # no run to summarize: the failure is the caller's to say


@pytest.mark.parametrize("name", sorted(p.name for p in NOTEBOOKS.glob("*.ipynb")))
def test_a_notebook_runs_to_its_end(name, tmp_path, monkeypatch):
    """Every code cell, in order, in one namespace, top-level awaits included; the notebooks are
    scripted, so this is the whole notebook and it runs offline."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    cells = json.loads((NOTEBOOKS / name).read_text())["cells"]
    ns: dict = {"__name__": f"notebook_{name}"}

    async def run_cells():
        for i, cell in enumerate(c for c in cells if c["cell_type"] == "code"):
            code = compile("".join(cell["source"]), f"{name}:cell{i}", "exec", ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            result = eval(code, ns)  # noqa: S307
            if inspect.iscoroutine(result):
                await result

    asyncio.run(run_cells())
    if name.startswith("03"):  # the peer's task is held and awaited, so its error would be this test's
        assert ns["peer_tasks"] and all(t.done() and t.exception() is None for t in ns["peer_tasks"])


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
