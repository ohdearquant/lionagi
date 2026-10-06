import gc
import os
import sys
from pathlib import Path

import pytest
from hypothesis import settings

# `ci` searches wider and drops the per-example deadline, which a loaded shared runner can miss;
# `deep` is for the scheduled run. Pick one with HYPOTHESIS_PROFILE.
settings.register_profile("dev", max_examples=100)
settings.register_profile("ci", max_examples=1_000, deadline=None, print_blob=True)
settings.register_profile("deep", max_examples=20_000, deadline=None, print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


# the stub `kkernel`: the lion pack's note verbs over a JSON file beside it, and a refusal of every
# other call. Written by `kkernel_stub` under the test's interpreter.
_STUB_KKERNEL = """\
\"\"\"The stub `kkernel` of the tests: the lion pack answered by `tests/lionfake.py` over a JSON file
beside this script (an empty store at first); any other pack is refused.\"\"\"
import fcntl, json, os, sys
from pathlib import Path

sys.path.insert(0, REPO)
from lion1.khive import KhiveError  # noqa: E402
from tests.lionfake import LionFake  # noqa: E402

actor = sys.argv[sys.argv.index("--expect-actor") + 1]  # one store per actor, as the binary's namespaces
store = Path(__file__).with_name("lion.json")
# one call at a time, as the binary serialises its writers: a server thread reads a resident's notes
# while the test puts them, and a read of a half-written file is a crash the real store never has
with open(store.with_name("lion.lock"), "w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    stores = json.loads(store.read_text()) if store.exists() else {}
    fake = LionFake.from_json(stores[actor]) if actor in stores else LionFake()
    try:
        results = [{"ok": True, "tool": "lion", "result": r} for r in fake.answer(sys.argv[-1])]
        status = "success"
    except KhiveError as e:
        results = [{"ok": False, "tool": "lion", "error": {"kind": e.kind, "message": str(e)}}]
        status = "failed"
    stores[actor] = fake.to_json()
    tmp = store.with_name("lion.json.tmp")
    tmp.write_text(json.dumps(stores))
    os.replace(tmp, store)
print(json.dumps({"results": results, "status": status}))
sys.exit(status != "success")
"""


@pytest.fixture(autouse=True)
def _no_khive_daemon(monkeypatch):
    # a test that reaches a real `kkernel` (one on PATH) runs it in-process: `exec` would otherwise
    # start a daemon under the test's home that nothing stops, or hand the call to the machine's own
    monkeypatch.setenv("KHIVE_NO_DAEMON", "1")


@pytest.fixture
def kkernel_stub(tmp_path_factory, monkeypatch):
    """A `kkernel` on PATH for a test of the wiring around the khive client, on a machine without the
    binary (a sandbox VM, CI). It serves the lion pack (ADR-0025 D4, D6) from `tests/lionfake.py` over
    a file of its own and refuses every other pack."""
    b = tmp_path_factory.mktemp("kkernel-stub")  # beside the test's tmp_path, never inside it
    repo = repr(str(Path(__file__).resolve().parent.parent))
    (b / "kkernel").write_text(f"#!{sys.executable}\n" + _STUB_KKERNEL.replace("REPO", repo))
    (b / "kkernel").chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ.get('PATH', '')}")
    return b / "kkernel"


@pytest.fixture
def store():
    """The store over the in-memory pack, for a test that builds an `Actor` (ADR-0026 D7)."""
    from tests.lionfake import store as _store

    return _store()


@pytest.fixture
def console_khive(tmp_path, kkernel_stub, monkeypatch):
    """`tmp_path` made the directory of the actor `console`, with every khive op answered in memory: what
    `console` and `lion chat` need to start (ADR-0025 D3). The `LionFake` behind it."""
    from tests.lionfake import actor_dir, patch_khive

    actor_dir(tmp_path)
    return patch_khive(monkeypatch)


@pytest.fixture
def bench_store(monkeypatch):
    """`run_set` takes the store a run writes its rows to from `--khive-dir`: here the in-memory pack."""
    from tests._swe import run_set
    from tests.lionfake import store as fresh_store

    monkeypatch.setattr(run_set, "store_of", lambda directory: fresh_store())


@pytest.fixture
def no_collection():
    """For a test that takes the interpreter to its recursion limit on purpose. A garbage collection
    that happened to run there would run some other object's finalizer with no stack left, and the
    RecursionError it raises, unraisable, would fail the test by chance. Collection comes back as it
    was, whether the test passed or failed."""
    was = gc.isenabled()
    gc.disable()
    yield
    if was:
        gc.enable()
