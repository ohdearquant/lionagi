# tests/conftest.py

# Run directory isolation: must stay above every other import in this file,
# since lionagi._paths binds LIONAGI_HOME/RUNS_ROOT at import time. See
# docs/internals/ci.md#run-directory-isolation for why the redirect is
# unconditional and what LIONAGI_TEST_HOME is for.
import atexit
import os
import shutil
import sys
import tempfile


def _remove_test_home(root):
    """Delete the suite's temporary root; report to stderr rather than raise if it survives.

    Runs from ``atexit`` after the session pytest reported on is over, so a
    raised exception here has no test to attach to. Only OSError/RecursionError
    (permissions, a busy file, a full disk, or rmtree exhausting the stack on a
    deep tree) are turned into a message; anything else is a bug in this
    function, not a cleanup failure, and is left to propagate.
    """
    try:
        shutil.rmtree(root)
    except (OSError, RecursionError) as e:
        sys.stderr.write(
            f"\nlionagi tests: could not remove the temporary run directory {root}\n"
            f"  {e}\n"
            "  It is still on disk; remove it by hand.\n"
        )


if os.environ.get("LIONAGI_TEST_HOME"):
    os.environ["LIONAGI_HOME"] = os.environ["LIONAGI_TEST_HOME"]
else:
    _TEST_LIONAGI_HOME = tempfile.mkdtemp(prefix="lionagi-tests-")
    os.environ["LIONAGI_HOME"] = _TEST_LIONAGI_HOME
    atexit.register(_remove_test_home, _TEST_LIONAGI_HOME)

# A store URL exported by the calling shell outranks LIONAGI_HOME, so without this a suite run
# from that shell would open the caller's real store. The settings loader matches environment
# names without regard to case, so every spelling goes. Tests that need the setting set their own.
for _name in [name for name in os.environ if name.upper() == "LIONAGI_STATE_DB_URL"]:
    del os.environ[_name]

# A machine-wide agent cap exported by the calling shell would make every test that spawns a
# CLI child wait for, or be refused, a slot held by work outside the suite. Tests that exercise
# admission set their own caps.
for _name in [
    name
    for name in os.environ
    if name.startswith("LIONAGI_MAX_SUBAGENTS") or name == "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS"
]:
    del os.environ[_name]

# A suite run by an agent lionagi started inherits that agent's depth, and lionagi reads it once
# at import, so it goes before anything imports lionagi; _outside_any_agent below covers the rest.
os.environ.pop("LIONAGI_AGENT_DEPTH", None)

# The provider layer reads $CODEX_HOME (global codex config and config
# profiles). Tests must never see the developer's real ~/.codex OR an
# externally exported CODEX_HOME: a machine-local model_reasoning_effort would
# change what the builders emit for an effort-less codex spawn, so the
# replacement is unconditional. Tests that need a config write into this
# directory or monkeypatch CODEX_HOME themselves.
_TEST_CODEX_HOME = tempfile.mkdtemp(prefix="lionagi-tests-codex-")
os.environ["CODEX_HOME"] = _TEST_CODEX_HOME
atexit.register(_remove_test_home, _TEST_CODEX_HOME)

# Discovery reads the user-level `.lionagi` (profiles, MCP config, settings)
# through $HOME on every call, so a run that keeps the developer's HOME hands
# any test that builds a profiled agent the real directory. The replacement is
# unconditional, made before anything imports lionagi, and inherited by every
# child process the suite starts. Tests that need a user-level directory write
# into this one or monkeypatch HOME themselves.
_TEST_USER_HOME = os.path.realpath(tempfile.mkdtemp(prefix="lionagi-tests-home-"))
os.environ["HOME"] = _TEST_USER_HOME
atexit.register(_remove_test_home, _TEST_USER_HOME)

if "lionagi.config" in sys.modules:
    # Settings are read once, when lionagi.config is first imported, so a store URL the caller
    # exported is already bound and removing it above changed nothing. lionagi.config imports
    # lionagi._paths, so this check goes first to name the settings that were read.
    raise RuntimeError(
        "lionagi.config was imported before tests/conftest.py could remove "
        "LIONAGI_STATE_DB_URL; this session's settings may name the caller's store."
    )

if "lionagi._paths" in sys.modules:
    # The constants are already bound to the old value, so the redirect above
    # did nothing and every run-directory write in this session lands in the
    # real one. Fail here rather than let the suite report itself as isolated.
    raise RuntimeError(
        "lionagi._paths was imported before tests/conftest.py could redirect "
        "LIONAGI_HOME; the run directory used by this session is not isolated."
    )

import inspect
import json
import types

import pytest

from scripts.quarantine import apply_quarantine_markers, load_manifest

# Load shared scripted/mock fixtures from the library so any test under tests/
# can ask for ``mocked_branch``, ``scripted_branch``, ``test_data_loader``, etc.
# Sub-conftests can override specific fixtures (see tests/docs/conftest.py).
pytest_plugins = ["lionagi.testing.pytest_plugin"]

_QUARANTINE = load_manifest()


def pytest_collection_modifyitems(items):
    """Apply quarantine markers from the checked-in exact-nodeid manifest."""

    apply_quarantine_markers(items, _QUARANTINE, pytest.mark.flaky_quarantine)


def pytest_pycollect_makeitem(collector, name, obj):
    """Mark every coroutine test not written for anyio as an asyncio test.

    pytest-asyncio runs in strict mode (see pyproject.toml), so it only takes
    tests that carry its marker, and anyio's plugin keeps the ones marked
    ``anyio`` or taking ``anyio_backend`` as an argument. This marks the rest
    before the test item is built, the same way anyio's plugin attaches its
    fixture, so that no coroutine test is left without a runner. A test that
    reaches ``anyio_backend`` only through another fixture is not seen here and
    is marked asyncio; mark such a test ``anyio``.
    """
    if not collector.istestfunction(obj, name):
        return
    func = obj.hypothesis.inner_test if hasattr(obj, "hypothesis") else obj
    if not inspect.iscoroutinefunction(func):
        return
    own_marks = {mark.name for mark in getattr(obj, "pytestmark", ())}
    if own_marks & {"anyio", "asyncio"}:
        return
    if collector.get_closest_marker("anyio") or collector.get_closest_marker("asyncio"):
        return
    if "anyio_backend" in inspect.signature(func).parameters:
        return
    pytest.mark.asyncio(obj)


@pytest.fixture(autouse=True)
def _keep_the_interpreter_default_sigpipe():
    """Stop one test's signal policy from following every later test.

    The CLI sets SIGPIPE to SIG_DFL on entry (so `li ... | head` dies quietly
    when head exits, instead of a traceback), but signal.signal is
    process-wide and permanent, so a test that drives the CLI in-process
    would otherwise hand that disposition to every later test in the worker.
    That matters because Python's default, SIG_IGN, is what several things
    under test rely on -- asyncio's event-loop shutdown writes to its
    self-pipe's write end after the read end is closed, expects the
    resulting OSError, and swallows it. Under SIG_DFL the kernel kills the
    process on that write instead: no traceback, no failing assertion, just
    a worker that stopped, blamed on whichever test it happened to be
    holding. Restoring the disposition after each test keeps that failure
    mode inside the test that actually changes the policy.
    """
    import signal

    previous = signal.getsignal(signal.SIGPIPE)
    try:
        yield
    finally:
        if signal.getsignal(signal.SIGPIPE) is not previous:
            signal.signal(signal.SIGPIPE, previous)


# Hypothesis: coverage instrumentation (5-10x slowdown) makes the default
# 200ms deadline trip on async property tests. Register a "ci" profile with
# no deadline and load it whenever coverage is active or CI=true.
try:
    from hypothesis import HealthCheck, settings

    settings.register_profile(
        "ci",
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    import os as _os

    if _os.environ.get("CI") or "coverage" in sys.modules or sys.gettrace() is not None:
        settings.load_profile("ci")
except ImportError:
    # hypothesis not installed (e.g., light test runs)
    pass


import os

_RSS_LOG_DIR = os.environ.get("PYTEST_RSS_LOG")

if _RSS_LOG_DIR:
    # Peak-RSS tracker for hunting worker OOM kills ("node down: Not properly
    # terminated" with no traceback). ru_maxrss is the process-lifetime PEAK,
    # so a nonzero delta marks the tests that pushed the high-water mark up —
    # exactly the ones to inspect when a CI worker is killed by memory
    # pressure. Off (zero overhead) unless PYTEST_RSS_LOG names a directory.
    import resource as _resource

    # ru_maxrss unit: kilobytes on Linux, bytes on macOS.
    _RSS_DIV = 1024 if sys.platform == "darwin" else 1

    os.makedirs(_RSS_LOG_DIR, exist_ok=True)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(item, nextitem):
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        log_path = os.path.join(_RSS_LOG_DIR, f"rss-{worker}.jsonl")
        before = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
        # Write a "start" row before running the test: if the worker is killed
        # mid-test (the exact OOM/SIGKILL crash this log exists for), the
        # crashing test never reaches the "end" row below, so a plain
        # after-only log would silently omit it. The start row is the only
        # trace of which test the worker was actually executing when it died.
        with open(log_path, "a") as f:
            f.write(
                json.dumps(
                    {
                        "worker": worker,
                        "test": item.nodeid,
                        "phase": "start",
                        "peak_kb": before // _RSS_DIV,
                    }
                )
                + "\n"
            )
        yield
        after = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
        delta_kb = (after - before) // _RSS_DIV
        peak_kb = after // _RSS_DIV
        with open(log_path, "a") as f:
            f.write(
                json.dumps(
                    {
                        "worker": worker,
                        "test": item.nodeid,
                        "phase": "end",
                        "peak_kb": peak_kb,
                        "delta_kb": delta_kb,
                    }
                )
                + "\n"
            )


def pytest_addoption(parser):
    parser.addoption(
        "--skip-missing-deps",
        action="store_true",
        default=False,
        help="Skip (instead of fail) tests that error solely due to a missing optional dependency.",
    )


@pytest.fixture
def plugin_home(monkeypatch, tmp_path):
    """Point HOME at a scratch dir and cd into it, so plugin discovery only sees test bundles."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def write_plugin(plugin_home):
    """Factory: write_plugin(dir_name, manifest_yaml, files={"rel/path": "content"}) -> bundle dir.

    Writes under the global ``~/.lionagi/plugins/<dir_name>/`` (== ``plugin_home``
    since HOME and cwd are the same scratch dir here).
    """

    def _write(dir_name, manifest_yaml, files=None):
        bundle = plugin_home / ".lionagi" / "plugins" / dir_name
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "plugin.yaml").write_text(manifest_yaml)
        for rel, content in (files or {}).items():
            p = bundle / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return bundle

    return _write


@pytest.fixture(autouse=True)
def _outside_any_agent(monkeypatch: pytest.MonkeyPatch):
    """Run every test as if the suite had been started by hand, outside any agent.

    A suite an agent runs inherits that agent's depth and runs under its
    process, so every test that starts a CLI child in this process would be refused as a nested
    agent. Tests of that refusal set their own depth and ancestry. A test that starts lionagi in
    a subprocess is not covered: the child reads its real ancestry and, under an agent, is
    refused, which is the behaviour, not a fault of the test.
    """
    from lionagi.service import _subagent_admission

    # setenv first so teardown also removes a depth a test's stamp writes straight into
    # os.environ: delenv alone records nothing to restore when the name was absent.
    monkeypatch.setenv("LIONAGI_AGENT_DEPTH", "0")
    monkeypatch.delenv("LIONAGI_AGENT_DEPTH")
    monkeypatch.setattr(_subagent_admission, "_INHERITED_AGENT_DEPTH", 0)
    monkeypatch.setattr(_subagent_admission, "_ancestor_agent_depth", lambda: (0, None))
    monkeypatch.setattr(_subagent_admission, "_child_agent_depth", 1)


@pytest.fixture(autouse=True)
def _contain_the_frontend_dist_variable(monkeypatch: pytest.MonkeyPatch):
    """Stop the CLI's frontend-dist path from following every later test.

    ``studio/cli.py`` sets ``LIONAGI_STUDIO_FRONTEND_DIST`` directly in
    ``os.environ`` on the production start path -- correctly, since the app
    reads it at import to decide whether to mount the SPA. But a test that
    drives that path leaves the variable set for every later test in the same
    worker, and those tests then build an app with the SPA mounted without
    asking for one.

    The failures that causes do not look like leakage. The SPA's 404 handler
    answers unrouted paths with the shell at 200, and mounting it registers an
    extra route, so what shows up is a traversal assertion getting 200 instead
    of 404 in one file and a route-count baseline off by one in another --
    both far from the test that actually set the variable, and both dependent
    on execution order, so neither reproduces when its own file is run alone.

    This removes an inherited value before each test. If the name was already
    absent, ``delenv`` records no original value to restore, so a later raw
    write to ``os.environ`` must be cleaned up by that test. Tests that want
    the SPA mounted should use ``monkeypatch.setenv`` so teardown restores the
    prior value.
    """
    monkeypatch.delenv("LIONAGI_STUDIO_FRONTEND_DIST", raising=False)


@pytest.fixture(autouse=True)
def _reset_plugin_registry():
    """Reset PluginRegistry's process-lifetime scan cache around every test.

    The registry caches its filesystem scan (keyed off HOME/.lionagi) for the
    life of the process, mirroring EndpointRegistry's ``_ensure_loaded``
    pattern. Tests routinely monkeypatch HOME per-test, so without a reset the
    first test to touch plugin resolution would leak its cached snapshot into
    every later test in the same worker.
    """
    from lionagi.plugins import PluginRegistry

    PluginRegistry.reset()
    yield
    PluginRegistry.reset()


_MISSING_DEP_HINTS = ("not installed", "is required for", "no module named")

# Optional packages whose absence should be skipped (not failed) under --skip-missing-deps.
# Bounds the captured-output scan so an unrelated assertion can't be silently masked.
_OPTIONAL_DEPS = (
    "pandas",
    "fastmcp",
    "ollama",
    "xmltodict",
    "matplotlib",
)


def _missing_optional_dep(exc):
    """Return the dep message if exc (or its cause chain) names a missing OPTIONAL extra, else None.

    Gated on _OPTIONAL_DEPS: a missing required/internal import (e.g. a typo or a
    broken core dependency like orjson) is NOT a missing-optional-dep and must still
    fail loudly rather than be silently skipped.
    """
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        low = str(exc).lower()
        is_missing = isinstance(exc, ModuleNotFoundError) or any(
            h in low for h in _MISSING_DEP_HINTS
        )
        if is_missing and any(d in low for d in _OPTIONAL_DEPS):
            return str(exc)
        exc = exc.__cause__ or exc.__context__
    return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if not item.config.getoption("--skip-missing-deps", default=False):
        return
    if not report.failed:
        return
    reason = _missing_optional_dep(call.excinfo.value) if call.excinfo is not None else None
    if reason is None:
        # Some paths swallow the ImportError and only log it (e.g. DataLogger.dump),
        # so the failure surfaces as a plain assertion. Scan captured output, but only
        # treat it as a missing-dep skip when a known optional extra is named alongside.
        captured = "\n".join(content for _, content in report.sections).lower()
        if any(h in captured for h in _MISSING_DEP_HINTS) and any(
            d in captured for d in _OPTIONAL_DEPS
        ):
            reason = "missing optional dependency (captured in test output)"
    if reason:
        report.outcome = "skipped"
        report.longrepr = (
            str(item.fspath),
            (item.location[1] or 0) + 1,
            f"Skipped: missing optional dependency ({reason})",
        )


@pytest.fixture
def ensure_fake_lionagi(monkeypatch):
    """Install minimal lionagi stubs if the real package is absent."""
    if "lionagi" in sys.modules:
        # Real lionagi present; do nothing.
        yield
        return

    pkg = types.ModuleType("lionagi")

    # ln: provide lcall (with optional flatten) and json_dumps
    ln_ns = types.SimpleNamespace()

    def lcall(items, func, *args, flatten=False, output_flatten=False, **kwargs):
        results = []
        for x in items:
            r = func(x, *args, **kwargs)
            if (flatten or output_flatten) and isinstance(r, list):
                results.extend(r)
            else:
                results.append(r)
        return results

    ln_ns.lcall = lcall
    ln_ns.json_dumps = staticmethod(lambda d: json.dumps(d))
    pkg.ln = ln_ns

    # utils: is_import_installed
    utils_mod = types.ModuleType("lionagi.utils")

    def is_import_installed(name: str) -> bool:
        try:
            __import__(name)
            return True
        except ImportError:
            return False

    utils_mod.is_import_installed = is_import_installed

    # protocols.graph.node: Node
    protocols_mod = types.ModuleType("lionagi.protocols")
    graph_mod = types.ModuleType("lionagi.protocols.graph")
    node_mod = types.ModuleType("lionagi.protocols.graph.node")

    class Node:
        def __init__(self, content, metadata):
            self.content = content
            self.metadata = metadata

        def __repr__(self):
            return f"Node(content={self.content!r}, metadata={self.metadata!r})"

    node_mod.Node = Node

    sys.modules["lionagi"] = pkg
    sys.modules["lionagi.utils"] = utils_mod
    sys.modules["lionagi.protocols"] = protocols_mod
    sys.modules["lionagi.protocols.graph"] = graph_mod
    sys.modules["lionagi.protocols.graph.node"] = node_mod
    yield


@pytest.fixture(scope="session")
def mod_paths():
    """Resolve module paths from env vars (UUT_CHUNK_MOD, UUT_API_MOD, UUT_SCHEMA_MOD)."""
    import os

    return {
        "chunk_mod": os.getenv("UUT_CHUNK_MOD", "lionagi.libs.file.chunk"),
        "api_mod": os.getenv("UUT_API_MOD", "lionagi.libs.file.process"),
        "schema_mod": os.getenv(
            "UUT_SCHEMA_MOD",
            "lionagi.libs.schema.load_pydantic_model_from_schema",
        ),
    }


# =============================================================================
# Shared Service Layer Fixtures (Phase 2 Consolidation)
# =============================================================================


@pytest.fixture
def openai_endpoint_config():
    """Standard OpenAI endpoint configuration for testing."""
    from lionagi.service.connections.endpoint_config import EndpointConfig

    return EndpointConfig(
        name="test_endpoint",
        provider="openai",
        endpoint="chat",
        base_url="https://api.openai.com/v1",
        endpoint_params=["chat", "completions"],
        openai_compatible=True,
        api_key="test-key",
    )


@pytest.fixture
def anthropic_endpoint_config():
    """Standard Anthropic endpoint configuration for testing."""
    from lionagi.service.connections.endpoint_config import EndpointConfig

    return EndpointConfig(
        name="anthropic_chat",
        provider="anthropic",
        endpoint="messages",
        base_url="https://api.anthropic.com/v1",
        endpoint_params=["messages"],
        openai_compatible=False,
        api_key="test-key",
    )


@pytest.fixture
def base_imodel():
    """Basic OpenAI iModel instance for testing."""
    from lionagi.service.imodel import iModel

    return iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")


@pytest.fixture
def anthropic_imodel():
    """Anthropic iModel instance for testing."""
    from lionagi.service.imodel import iModel

    return iModel(
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        api_key="test-key",
    )


@pytest.fixture
def mock_sync_response():
    """Standard mock API response for testing (sync shape, for non-service tests)."""
    from unittest.mock import MagicMock

    response = MagicMock()
    response.json.return_value = {
        "choices": [{"message": {"content": "Test response", "role": "assistant"}}],
        "model": "gpt-4.1-mini",
        "usage": {
            "total_tokens": 50,
            "prompt_tokens": 20,
            "completion_tokens": 30,
        },
    }
    return response


@pytest.fixture
def mock_streaming_response():
    """Mock streaming response for testing streaming operations."""

    class MockStreamingResponse:
        def __init__(self):
            self.chunks = [
                {"choices": [{"delta": {"content": "Hello"}}]},
                {"choices": [{"delta": {"content": " world"}}]},
                {"choices": [{"delta": {}}]},  # End marker
            ]

        async def __aiter__(self):
            for chunk in self.chunks:
                yield chunk

    return MockStreamingResponse()
