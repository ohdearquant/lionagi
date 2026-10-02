import gc
import os

import pytest
from hypothesis import settings

# `ci` searches wider and drops the per-example deadline, which a loaded shared runner can miss;
# `deep` is for the scheduled run. Pick one with HYPOTHESIS_PROFILE.
settings.register_profile("dev", max_examples=100)
settings.register_profile("ci", max_examples=1_000, deadline=None, print_blob=True)
settings.register_profile("deep", max_examples=20_000, deadline=None, print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture
def kkernel_stub(tmp_path, monkeypatch):
    """A `kkernel` on PATH that only exits, so a test of the wiring around the khive client passes on a
    machine without the binary (a sandbox VM, CI); nothing in such a test calls it."""
    b = tmp_path / "stub-bin"
    b.mkdir()
    (b / "kkernel").write_text("#!/bin/sh\necho 'stub kkernel: not for calls' >&2\nexit 1\n")
    (b / "kkernel").chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ.get('PATH', '')}")
    return b / "kkernel"


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
