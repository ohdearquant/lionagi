"""Unit tests for lionbench's instance schema: JSON round-trip + subject-first layout."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schema import (  # noqa: E402
    Instance,
    OracleSpec,
    Provenance,
    Validation,
    list_subjects,
    load_manifest,
    save_instance,
    save_rejection,
    usable_instances,
)


def _make_instance(instance_id="lionagi__1843", subject="lionagi", validated=True) -> Instance:
    inst = Instance(
        instance_id=instance_id,
        repo="ohdearquant/lionagi",
        base_commit="deadbeef",
        task_text="Something breaks when X happens.",
        oracle=OracleSpec(
            kind="pytest",
            held_out_paths=["tests/cli/test_x.py::test_y"],
            command="uv run pytest tests/cli/test_x.py::test_y -q",
            test_patch="diff --git a/tests/cli/test_x.py b/tests/cli/test_x.py\n",
        ),
        gold_patch="diff --git a/src/x.py b/src/x.py\n",
        merged_at="2026-06-14T00:00:00Z",
        subject=subject,
        provenance=Provenance(pr=1843, issue=1791, nominated_by="lambda:lionagi", why="regression"),
    )
    if validated:
        inst.validation = Validation(gold_passes=True, null_fails=True, leak_review="pass")
    return inst


def test_instance_json_round_trip():
    inst = _make_instance()
    d = inst.to_dict()
    restored = Instance.from_dict(d)
    assert restored == inst


def test_round_trip_preserves_unvalidated_state():
    inst = _make_instance(validated=False)
    restored = Instance.from_dict(inst.to_dict())
    assert restored.validation.gold_passes is None
    assert restored.validation.ok is False


def test_save_instance_writes_under_subject_dir(tmp_path):
    inst = _make_instance(subject="python-framework")
    path = save_instance(inst, tmp_path)
    assert path == tmp_path / "python-framework" / "lionagi__1843.json"
    assert path.exists()
    manifest = tmp_path / "python-framework" / "manifest.json"
    assert manifest.exists()


def test_list_subjects_and_load_manifest_scoped(tmp_path):
    save_instance(_make_instance("a__1", subject="rust-systems"), tmp_path)
    save_instance(_make_instance("a__2", subject="lean-proofs"), tmp_path)

    assert list_subjects(tmp_path) == ["lean-proofs", "rust-systems"]

    only_rust = load_manifest(tmp_path, subject="rust-systems")
    assert [i.instance_id for i in only_rust] == ["a__1"]

    everything = load_manifest(tmp_path)
    assert {i.instance_id for i in everything} == {"a__1", "a__2"}


def test_usable_instances_filters_on_validation(tmp_path):
    save_instance(_make_instance("good__1", subject="kg-agentic", validated=True), tmp_path)
    save_instance(_make_instance("bad__2", subject="kg-agentic", validated=False), tmp_path)

    usable = usable_instances(tmp_path, subject="kg-agentic")
    assert [i.instance_id for i in usable] == ["good__1"]


def test_save_instance_redacts_validation_output_and_nominated_by_on_disk(tmp_path):
    """Local-run validation stdout/stderr (absolute worktree paths, uv cache paths,
    test-generated UUIDs), the internal nominated_by actor name, and the internal
    nomination rationale (can name the fix) must never reach a committed instance
    JSON — the write path redacts them, not a manual step."""
    inst = _make_instance(subject="python-framework")
    inst.validation.gold_output = "at /Users/lion/.lionagi/swebench-work/repos/_x ok"
    inst.validation.null_output = "some local failure trace"
    path = save_instance(inst, tmp_path)

    on_disk = json.loads(path.read_text())
    assert on_disk["validation"]["gold_output"] == ""
    assert on_disk["validation"]["null_output"] == ""
    assert on_disk["provenance"]["nominated_by"] == ""
    assert on_disk["provenance"]["why"] == ""
    # in-memory instance is untouched -- redaction only applies at the write boundary
    assert inst.validation.gold_output != ""
    assert inst.provenance.nominated_by == "lambda:lionagi"
    assert inst.provenance.why == "regression"


def test_save_rejection_lands_under_subject_rejected_dir(tmp_path):
    path = save_rejection("nope__9", "empty gold_patch", tmp_path, subject="numerics-parity")
    assert path == tmp_path / "numerics-parity" / "rejected" / "nope__9.json"
    assert path.exists()
