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
SCRIPTED = (
    "tools_and_gates",
    "notes_and_context",
    "watch_the_artifact",
    "budgets_and_outcomes",
    "structured_output",
    "serve_an_actor",
)
LIVE = ("backends",)
# tests/test_bench_agent.py drives this one: a fake store, and a child that writes a ledger
OWN_TESTS = ("bench_agent",)


def run(script: str, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {
        **env,
        "PYTHONPATH": os.pathsep.join(filter(None, (str(ROOT), str(ROOT / "src"), env.get("PYTHONPATH")))),
    }
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


SERVED = """\
=== GET /: the card says what a client may start and what the model is told it may do
200 | actor: extractor | protocol: 1 | offer: extractor
command: route | pure | The standard name of a route as a note writes it.
outputs: ['extraction'] | converse: False
bounds: max_turns 4, max_idle 3, context_budget 200000, max_refusals 3

=== POST /runs for what the owner never offered: refused, and nothing starts
404 {'error': 'unknown_offer', 'reason': "no offer is named 'writer'"}
400 {'error': 'bad_request', 'reason': "max_turns 9 is above the offer's 4"}

=== POST /runs: the run starts with the note as its input
201 | offer: extractor | resumed: None

=== GET /runs/{id}/events: read to the first state that says ended
the stream opened with: state
phase: ended | outcome: Success | turn: 2
output: {
 "extraction": {
  "medications": [
   {
    "name": "metformin",
    "dose": "500 mg",
    "route": "oral",
    "frequency": "twice daily"
   },
   {
    "name": "lisinopril",
    "dose": "10 mg",
    "route": "oral",
    "frequency": "once daily"
   }
  ]
 }
}

=== GET /runs/{id}/record: the rows the log holds, by kind
200 | more: False | ['INPUT', 'SYSTEM', 'TEXT', 'RESULT', 'RESULT', 'SYSTEM', 'TEXT', 'SYSTEM']
"""


def test_the_served_example_reads_the_card_a_run_and_its_end_over_the_socket(tmp_path, monkeypatch, capsys):
    """Its output pinned: nothing in it depends on the port, the run's id, or whether the run ended
    before its stream opened."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    asyncio.run(load("serve_an_actor").main())
    assert capsys.readouterr().out == SERVED


def test_the_live_example_says_what_the_calls_cost_when_a_later_call_fails(tmp_path, monkeypatch, capsys):
    """The table of calls is printed on the way out of a failure too: the calls before it were paid."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    module = load("backends")

    class Backend:
        error = module.OpenRouterError

        def __init__(self):
            self.calls = []

        async def __call__(self, messages):
            if self.calls:
                raise self.error("429 rate limited")
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
    backend = Backend()
    backend.error = asyncio.CancelledError  # an interrupt after the paid call: the table still prints
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(module.main("openrouter", None))
    assert (
        "   1     40     12      0      3      0   0.00021  stop / p" in capsys.readouterr().out.splitlines()
    )


async def run_cells(name: str, ns: dict, before=None) -> None:
    """Every code cell of a notebook, in order, in one namespace, top-level awaits included. `before`,
    given a cell's source, runs on the namespace ahead of that cell."""
    for i, cell in enumerate(
        c for c in json.loads((NOTEBOOKS / name).read_text())["cells"] if c["cell_type"] == "code"
    ):
        source = "".join(cell["source"])
        if before is not None:
            before(source, ns)
        result = eval(compile(source, f"{name}:cell{i}", "exec", ast.PyCF_ALLOW_TOP_LEVEL_AWAIT), ns)  # noqa: S307
        if inspect.iscoroutine(result):
            await result


def test_the_call_table_marks_a_partial_cost_and_names_a_failed_calls_error(tmp_path, monkeypatch, capsys):
    """A subtotal an attempt is missing from prints as at least that much, so a partial zero does not
    read as a free call; a failed call's row carries its error where a finished one names its end."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    module = load("backends")

    class Backend:
        calls = [
            {
                "usage": {"input_tokens": 1},
                "total_cost_usd": 0.0002,
                "cost_complete": True,
                "finish_reason": "stop",
                "provider": "p",
            },
            {
                "usage": {"input_tokens": 1},
                "total_cost_usd": 0.0002,
                "cost_complete": False,
                "finish_reason": "stop",
                "provider": "p",
            },
            {
                "usage": {},
                "total_cost_usd": 0.0,
                "cost_complete": False,
                "error": "cancelled\nafter 2 attempts",
            },
            {
                "usage": {},
                "total_cost_usd": 0.0,
                "cost_complete": True,
                "finish_reason": "stop",
                "provider": "p",
            },
            {"usage": {}, "total_cost_usd": None, "finish_reason": "stop", "provider": "p"},
        ]

    module.spent(Backend())
    rows = capsys.readouterr().out.splitlines()
    assert [r.split(maxsplit=7)[6:] for r in rows if r[:5].strip().isdigit()] == [
        ["0.00020", "stop / p"],
        [">=0.00020", "stop / p"],
        [">=0.00000", "cancelled"],
        ["0.00000", "stop / p"],
        ["unknown", "stop / p"],
    ]
    assert rows[-1].endswith(">= marks a subtotal")


@pytest.mark.parametrize("name", sorted(p.name for p in NOTEBOOKS.glob("*.ipynb")))
def test_a_notebook_runs_to_its_end(name, tmp_path, monkeypatch):
    """The notebooks are scripted, so this is the whole notebook and it runs offline."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    ns: dict = {"__name__": f"notebook_{name}"}
    asyncio.run(run_cells(name, ns))
    if name.startswith("03"):  # the peer's task is held and awaited, so its error would be this test's
        assert ns["peer_tasks"] and all(t.done() and t.exception() is None for t in ns["peer_tasks"])


def test_the_two_actor_notebook_raises_a_peers_error_on_the_page(tmp_path, monkeypatch):
    """A researcher whose backend raises settles the reviewer's ask naming the error, and the
    reviewer's run goes on; the peer's own task is awaited where the peer runs are shown, so the
    error is raised there rather than left to the loop to report at exit."""
    monkeypatch.chdir(tmp_path)
    ns: dict = {"__name__": "notebook_03_failing_peer"}

    async def failing(messages):
        raise RuntimeError("the researcher's backend is down")

    seen: list[str] = []

    def before(source, ns):
        if source.startswith("run = await reviewer.run("):
            seen.append("run")
            ns["researcher_turns"] = failing
        if source.startswith("await asyncio.gather"):
            seen.append("gather")
            assert ns["run"].output["verdict"].decision == "request_changes"  # the reviewer finished
            assert "RuntimeError" in str(ns["run"].resolve("q1"))  # and its ask says why the peer failed

    with pytest.raises(RuntimeError, match="backend is down"):
        asyncio.run(run_cells("03_two_actors.ipynb", ns, before))
    assert seen == ["run", "gather"]  # both cells were met, so the checks inside ran
    assert [t.exception().args for t in ns["peer_tasks"]] == [("the researcher's backend is down",)]


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
