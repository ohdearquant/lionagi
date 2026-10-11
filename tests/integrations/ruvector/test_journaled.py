# SPDX-License-Identifier: Apache-2.0
"""Real RVF delta replay, publication failures and bounded recovery."""

import json
import os
import subprocess
import sys

import pytest

pytest.importorskip("ruvector")

from lionagi import Node
from lionagi.integrations.ruvector.journaled import JournaledRuVectorStore

pytestmark = pytest.mark.integration


def make(path, **kwargs):
    return JournaledRuVectorStore(path, 3, embedding_id="test:v1", **kwargs)


def node(text="deployment"):
    return Node(content=text, metadata={"tenant": "a"}, embedding=[1, 0, 0])


def test_roundtrip_every_operation(tmp_path):
    path = tmp_path / "journal"
    n = node()
    key = str(n.id)
    with make(path) as store:
        store.add_nodes([n, node("other")])
        assert store.record_feedback("a", key, 1, "event1")
        assert not store.record_feedback("a", key, 1, "event1")
        assert store.record_feedback("b", key, -0.5, "event2")
        assert store.reset_feedback("a") == 1
        assert store.segment_count == 5
    with make(path) as store:
        assert len(store) == 2
        assert store.get(key).content == "deployment"
        assert store.feedback("b", [key]) == {key: (-0.5, 1)}
        assert not store.has_feedback("a")
        assert store.delete([key, "unknown"]) == 1
    with make(path) as store:
        assert len(store) == 1
        assert store.get(key) is None
        assert not store.has_feedback("b")


def test_delta_contains_only_changed_data(tmp_path):
    path = tmp_path / "journal"
    with make(path) as store:
        store.add_nodes([node("first"), node("second")])
        key = store.add_nodes([node("third")])[0]
        pointer = json.loads((path / "CURRENT").read_text())
        native = store._rvf.open(str(path / pointer["segments"][-1]["name"]))
        try:
            value = json.loads(store._read_manifest(native))
            assert [row[0] for row in value["records"]] == [key]
            assert native.status()["total_vectors"] == 1
        finally:
            native.close()
        store.record_feedback("ctx", key, 1, "rating")
        pointer = json.loads((path / "CURRENT").read_text())
        native = store._rvf.open(str(path / pointer["segments"][-1]["name"]))
        try:
            value = json.loads(store._read_manifest(native))
            assert value["records"] == []
            assert native.status()["total_vectors"] == 0
            assert value["payload"] == ["rating", "ctx", key, 1.0]
        finally:
            native.close()


def test_compaction_and_unrelated_file(tmp_path):
    path = tmp_path / "journal"
    with make(path, max_segments=3) as store:
        for i in range(8):
            store.add_nodes([node(str(i))])
            assert store.segment_count <= 3
        (path / "unrelated.rvf").write_text("keep")
        os.chmod(path / "unrelated.rvf", 0o600)
        store.compact()
        assert store.segment_count == 1
        assert len(list(path.glob("segment-*.rvf"))) == 1
        assert (path / "unrelated.rvf").read_text() == "keep"
    with make(path, max_segments=3) as store:
        assert len(store) == 8


@pytest.mark.parametrize("after", [False, True])
def test_pointer_failure_recovers_complete_transaction(tmp_path, monkeypatch, after):
    path = tmp_path / "journal"
    store = make(path)
    first = store.add_nodes([node("first")])[0]
    second = node("second")
    original = store._publish_pointer

    def fail(entries, generation):
        if after:
            original(entries, generation)
        raise OSError("publication failure")

    monkeypatch.setattr(store, "_publish_pointer", fail)
    with pytest.raises(OSError):
        store.add_nodes([second])
    with pytest.raises(RuntimeError):
        store.get(first)
    store.close()
    with make(path) as reopened:
        assert reopened.get(first) is not None
        assert (reopened.get(str(second.id)) is not None) == after
        assert len(reopened) == (2 if after else 1)


def test_crashed_process_replay(tmp_path):
    path = tmp_path / "journal"
    code = (
        "import os; from lionagi import Node; "
        "from lionagi.integrations.ruvector.journaled import JournaledRuVectorStore; "
        f"s=JournaledRuVectorStore({str(path)!r},3,embedding_id='test:v1'); "
        "s.add_nodes([Node(content='crash',embedding=[1,0,0])]); os._exit(0)"
    )
    result = subprocess.run([sys.executable, "-c", code], check=False)
    assert result.returncode == 0
    with make(path) as store:
        assert store.scan()[0].content == "crash"


@pytest.mark.parametrize("mutation", ["hash", "traversal", "generation", "duplicate", "oversize"])
def test_reject_invalid_pointer(tmp_path, mutation):
    path = tmp_path / "journal"
    with make(path) as store:
        store.add_nodes([node()])
    pointer = json.loads((path / "CURRENT").read_text())
    if mutation == "hash":
        pointer["segments"][-1]["sha256"] = "0" * 64
    elif mutation == "traversal":
        pointer["segments"][-1]["name"] = "../external.rvf"
    elif mutation == "generation":
        pointer["generation"] += 1
    elif mutation == "duplicate":
        pointer["segments"].append(pointer["segments"][-1])
    else:
        (path / "CURRENT").write_text(" " * 65537)
    if mutation != "oversize":
        (path / "CURRENT").write_text(json.dumps(pointer))
    with pytest.raises(ValueError):
        make(path)


def test_config_and_exclusive_owner(tmp_path):
    path = tmp_path / "journal"
    with make(path):
        with pytest.raises(OSError):
            make(path)
    with pytest.raises(ValueError):
        JournaledRuVectorStore(path, 3, embedding_id="other")
    with pytest.raises(ValueError):
        make(path, max_segments=4)


def test_noop_does_not_publish(tmp_path):
    with make(tmp_path / "journal") as store:
        key = store.add_nodes([node()])[0]
        store.record_feedback("ctx", key, 1, "rating")
        count = store.segment_count
        assert not store.record_feedback("ctx", key, 1, "rating")
        assert store.reset_feedback("unknown") == 0
        assert store.delete(["unknown"]) == 0
        assert store.segment_count == count


def test_private_directory_and_symlink(tmp_path):
    path = tmp_path / "journal"
    path.mkdir(mode=0o755)
    with pytest.raises(ValueError):
        make(path)
    link = tmp_path / "link"
    link.symlink_to(path, target_is_directory=True)
    with pytest.raises(ValueError):
        make(link)


@pytest.mark.asyncio
async def test_async_operations(tmp_path):
    path = tmp_path / "journal"
    with make(path) as store:
        key = (await store.aadd_nodes([node()]))[0]
        assert (await store.asearch([1, 0, 0]))[0].node_id == key
        await store.acompact()
        assert store.segment_count == 1


@pytest.mark.parametrize("operation", ["feedback", "delete", "reset"])
@pytest.mark.parametrize("after", [False, True])
def test_all_mutation_publication_failures(tmp_path, monkeypatch, operation, after):
    path = tmp_path / "journal"
    store = make(path)
    key = store.add_nodes([node()])[0]
    store.record_feedback("ctx", key, 1, "old")
    publish = store._publish_pointer

    def fail(entries, generation):
        if after:
            publish(entries, generation)
        raise OSError("publication failure")

    monkeypatch.setattr(store, "_publish_pointer", fail)
    with pytest.raises(OSError):
        if operation == "feedback":
            store.record_feedback("ctx", key, -1, "new")
        elif operation == "delete":
            store.delete([key])
        else:
            store.reset_feedback("ctx")
    store.close()
    with make(path) as reopened:
        if operation == "delete":
            assert (reopened.get(key) is None) == after
            assert reopened.has_feedback("ctx") != after
        elif operation == "reset":
            assert reopened.get(key) is not None
            assert reopened.has_feedback("ctx") != after
        else:
            assert reopened.feedback("ctx", [key]) == {key: ((0.0, 2) if after else (1.0, 1))}


def test_native_write_failure_keeps_pointer(tmp_path, monkeypatch):
    path = tmp_path / "journal"
    store = make(path)
    key = store.add_nodes([node()])[0]
    pointer = (path / "CURRENT").read_bytes()

    def fail(*args):
        raise OSError("metadata write failure")

    monkeypatch.setattr(store, "_write_manifest", fail)
    with pytest.raises(OSError):
        store.add_nodes([node("failed")])
    assert (path / "CURRENT").read_bytes() == pointer
    store.close()
    with make(path) as reopened:
        assert len(reopened) == 1
        assert reopened.get(key).content == "deployment"


def test_compaction_failure_after_publish_recovers(tmp_path, monkeypatch):
    path = tmp_path / "journal"
    store = make(path)
    key = store.add_nodes([node()])[0]
    store.record_feedback("ctx", key, 1, "e")

    def fail():
        raise OSError("GC failed")

    monkeypatch.setattr(store, "_collect", fail)
    with pytest.raises(OSError):
        store.compact()
    store.close()
    with make(path) as reopened:
        assert reopened.segment_count == 1
        assert reopened.feedback("ctx", [key]) == {key: (1.0, 1)}
        reopened.compact()
        assert len(list(path.glob("segment-*.rvf"))) == 1


def test_gc_preserves_managed_symlink(tmp_path):
    path = tmp_path / "journal"
    with make(path) as store:
        unrelated = tmp_path / "unrelated"
        unrelated.write_text("keep")
        (path / ("segment-" + "a" * 32 + ".rvf")).symlink_to(unrelated)
        store.compact()
        assert unrelated.read_text() == "keep"


@pytest.mark.parametrize("mutation", ["config", "generation", "operation", "node", "vector"])
def test_reject_invalid_rvf_chain_even_with_updated_pointer_hash(tmp_path, mutation):
    import hashlib

    import ruvector

    path = tmp_path / "journal"
    with make(path) as store:
        store.add_nodes([node()])
        pointer = json.loads((path / "CURRENT").read_text())
        segment = path / pointer["segments"][-1]["name"]
        native = ruvector.RvfStore.open(str(segment))
        try:
            state = json.loads(store._read_manifest(native))
            vectors = native.export_vectors(max_vectors=10000, max_bytes=268435456)
        finally:
            native.close()
        if mutation == "config":
            state["configuration"]["embedding_id"] = "wrong"
        elif mutation == "generation":
            state["generation"] += 1
        elif mutation == "operation":
            state["operation"] = "execute"
        elif mutation == "node":
            state["records"][0][0] = "invalid"
        else:
            vectors = [(0, [0, 0, 0])]
        temporary = path / "rewrite.rvf"
        native = ruvector.RvfStore.create(str(temporary), 3)
        os.chmod(temporary, 0o600)
        try:
            native.ingest([vector for _, vector in vectors], [key for key, _ in vectors])
            store._write_manifest(native, json.dumps(state))
        finally:
            native.close()
        os.replace(temporary, segment)
        pointer["segments"][-1]["sha256"] = hashlib.sha256(segment.read_bytes()).hexdigest()
        (path / "CURRENT").write_text(json.dumps(pointer))
    with pytest.raises(ValueError):
        make(path)


def test_bulk_delete_one_transaction(tmp_path):
    path = tmp_path / "journal"
    with make(path, max_batch_size=2) as store:
        keys = store.add_nodes([node("1"), node("2")])
        keys += store.add_nodes([node("3"), node("4")])
        store.record_feedback("ctx", keys[0], 1, "rating")
        with pytest.raises(ValueError):
            store.delete(keys)
        previous = store.segment_count
        assert store._delete_many(keys) == 4
        assert store.segment_count == previous + 1
    with make(path, max_batch_size=2) as store:
        assert len(store) == 0
        assert not store.has_feedback("ctx")


@pytest.mark.parametrize("version", [True, 1.0])
def test_pointer_version_requires_integer(tmp_path, version):
    path = tmp_path / "journal"
    make(path).close()
    pointer = json.loads((path / "CURRENT").read_text())
    pointer["version"] = version
    (path / "CURRENT").write_text(json.dumps(pointer))
    with pytest.raises(ValueError):
        make(path)


def test_active_byte_budget_checked_before_native_parse(tmp_path):
    path = tmp_path / "journal"
    with make(path, max_journal_bytes=1048576):
        pass
    pointer = json.loads((path / "CURRENT").read_text())
    segment = path / pointer["segments"][0]["name"]
    with segment.open("ab") as stream:
        stream.write(b"x" * 1048576)
    with pytest.raises(ValueError, match="journal exceeds byte budget"):
        make(path, max_journal_bytes=1048576)


def test_oversized_base_keeps_previous_state(tmp_path):
    path = tmp_path / "journal"
    store = make(path, max_journal_bytes=1048576)
    pointer = (path / "CURRENT").read_bytes()
    with pytest.raises(ValueError, match="base exceeds"):
        store.add_nodes([node("x" * 60000) for _ in range(20)])
    assert (path / "CURRENT").read_bytes() == pointer
    store.close()
    with make(path, max_journal_bytes=1048576) as reopened:
        assert len(reopened) == 0


def test_automatic_compaction_at_byte_budget(tmp_path):
    path = tmp_path / "journal"
    compacted = False
    with make(path, max_journal_bytes=1048576, max_segments=256) as store:
        repeated = node("x" * 32768)
        previous = store.segment_count
        for _ in range(50):
            key = store.add_nodes([repeated])[0]
            if store.segment_count < previous:
                compacted = True
            previous = store.segment_count
            store.delete([key])
            if store.segment_count < previous:
                compacted = True
            previous = store.segment_count
            assert store._journal_bytes <= 1048576
        assert compacted
    with make(path, max_journal_bytes=1048576, max_segments=256) as store:
        assert len(store) == 0
        assert store._journal_bytes <= 1048576
