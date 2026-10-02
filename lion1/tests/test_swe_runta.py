"""The bench over a Runta box: `testbed` builds one with the testbed's shape, the instance's image is
found or built, and `run_set --where runta` reaches the testbed through `one`."""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from hub import RuntaSandbox
from tests._swe import Started
from tests._swe import run_set as swe_run_set
from tests._swe import sandbox as swe_sandbox
from tests.test_swe_run_set import _args, _one

# --- bench/swe/sandbox.py: testbed over Runta ---------------------------------------------


def test_testbed_runta_gives_a_runta_box_with_the_testbed_shape():
    box = swe_sandbox.testbed("img-1", "runta", cpu=4, memory=6, label="swe-x")
    assert isinstance(box, RuntaSandbox)
    assert (box.workdir, box.prelude) == (swe_sandbox.WORKDIR, swe_sandbox.PRELUDE)
    assert (box.cpu, box.memory, box.name, box.image) == (4, 6, "swe-x", "img-1")


def test_testbed_refuses_an_unknown_where():
    with pytest.raises(ValueError, match="nowhere"):
        swe_sandbox.testbed("img", "nowhere")


def test_runta_name_is_swe_prefixed_lowercase_after_the_owner():
    assert swe_sandbox.runta_name({"instance_id": "django__django-11951"}) == "swe-django-11951"


def test_runta_image_returns_a_found_image_without_building(monkeypatch):
    seen = []

    async def fake_find_image(name, *, api=None):
        seen.append(name)
        return "img-existing"

    async def fail_build_image(*a, **k):
        raise AssertionError("build_image was called although an image was found")

    monkeypatch.setattr(swe_sandbox, "find_image", fake_find_image)
    monkeypatch.setattr(swe_sandbox, "build_image", fail_build_image)
    row = {"instance_id": "org__has-image-1", "image": "base:1"}
    assert asyncio.run(swe_sandbox.runta_image(row)) == "img-existing"
    assert seen == ["swe-has-image-1"]


def test_runta_image_builds_from_the_rows_image_when_none_is_found(monkeypatch):
    async def fake_find_image(name, *, api=None):
        return None

    built = []

    async def fake_build_image(name, dockerfile, *, architecture="x86_64", timeout=3600.0, api=None):
        built.append((name, dockerfile, architecture))
        return "img-built"

    monkeypatch.setattr(swe_sandbox, "find_image", fake_find_image)
    monkeypatch.setattr(swe_sandbox, "build_image", fake_build_image)
    row = {"instance_id": "org__needs-build-1", "image": "base:2"}
    assert asyncio.run(swe_sandbox.runta_image(row)) == "img-built"
    assert built == [("swe-needs-build-1", "FROM base:2\n", "x86_64")]


# --- bench/swe/run_set.py: --where runta --------------------------------------------------


def test_the_cli_parser_accepts_where_runta():
    script = Path(__file__).resolve().parents[1] / "bench" / "swe" / "run_set.py"
    out = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True)
    assert out.returncode == 0
    assert "{local,daytona,runta}" in out.stdout


def test_where_runta_reaches_testbed_through_one(tmp_path, monkeypatch):
    seen = []

    def fake_testbed(image, where, *, cpu, memory, label):
        seen.append((image, where, cpu, memory, label))
        return Started()

    async def fake_runta_image(row, *, timeout=3600.0):
        return row["image"]  # the image build itself is sandbox.py's own concern, not one()'s

    async def fake_solve(row, *a, **k):
        return {"instance_id": row["instance_id"], "model_patch": ""}

    monkeypatch.setattr(swe_run_set, "testbed", fake_testbed)
    monkeypatch.setattr(swe_run_set, "runta_image", fake_runta_image)
    monkeypatch.setattr(swe_run_set, "solve", fake_solve)
    _one(_args(where="runta"), tmp_path)
    assert seen == [("img", "runta", 2, 2, "lionagi-swe-r1")]
