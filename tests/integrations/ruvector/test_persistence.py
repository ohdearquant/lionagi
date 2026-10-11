# SPDX-License-Identifier: Apache-2.0
"""Persistence, process recovery, and trusted feedback contracts."""

import json
import os
import subprocess
import sys

import pytest

ruvector = pytest.importorskip("ruvector")
pytestmark = pytest.mark.integration

from lionagi import Node
from lionagi.integrations.ruvector.persistent import PersistentRuVectorStore


@pytest.fixture(autouse=True)
def require_rvf_capabilities(request):
    if request.node.name == "test_missing_native_apis":
        return
    native = getattr(ruvector, "RvfStore", None)
    required = ("set_metadata", "get_metadata", "export_vectors", "metadata_recovery")
    if native is None or any(not hasattr(native, name) for name in required):
        pytest.skip("RVF persistence tests require RuVector SDK 0.1.4 metadata/export APIs")


def make(path, **kwargs):
    return PersistentRuVectorStore(path, 3, embedding_id="test:v1", **kwargs)


def node():
    return Node(content="deployment", metadata={"tenant": "a"}, embedding=[1, 0, 0])


def test_roundtrip_delete_feedback(tmp_path):
    path = tmp_path / "memory.rvf"
    n = node()
    key = str(n.id)
    with make(path) as store:
        store.add_nodes([n])
        assert store.record_feedback("ctx", key, 1, "event1")
        assert not store.record_feedback("ctx", key, 1, "event1")
        assert store.record_feedback("ctx", key, -0.5, "event2")
        assert store.has_feedback("ctx")
        with pytest.raises(ValueError, match="different"):
            store.record_feedback("ctx", key, -1, "event1")
    with make(path) as store:
        assert store.feedback("ctx", [key]) == {key: (0.5, 2)}
        assert store.search([1, 0, 0])[0].node_id == key
        assert store.get(key).metadata == {"tenant": "a"}
        assert store.delete([key]) == 1
        assert store.feedback("ctx", [key]) == {}
    with make(path) as store:
        assert len(store) == 0
        assert not store.has_feedback("ctx")


def test_reset_and_context_isolation(tmp_path):
    with make(tmp_path / "memory.rvf") as store:
        key = store.add_nodes([node()])[0]
        store.record_feedback("a", key, 1, "a")
        store.record_feedback("b", key, -1, "b")
        assert store.reset_feedback("a") == 1
        assert not store.has_feedback("a")
        assert store.feedback("b", [key]) == {key: (-1, 1)}
        assert store.record_feedback("a", key, 0, "a")


def test_exclusive_and_closed(tmp_path):
    path = tmp_path / "memory.rvf"
    store = make(path)
    with pytest.raises(OSError):
        make(path)
    store.close()
    store.close()
    with pytest.raises(RuntimeError, match="closed"):
        store.search([1, 0, 0])
    make(path).close()


@pytest.mark.parametrize("reward", [True, float("nan"), float("inf"), -2, 2, "1"])
def test_invalid_feedback(tmp_path, reward):
    with make(tmp_path / "memory.rvf") as store:
        key = store.add_nodes([node()])[0]
        with pytest.raises(ValueError):
            store.record_feedback("a", key, reward, "event")
        assert not store.has_feedback("a")


def test_feedback_bounds(tmp_path):
    with make(tmp_path / "memory.rvf") as store:
        key = store.add_nodes([node()])[0]
        for ctx, key_, event in [
            ("", key, "x"),
            ("a" * 257, key, "x"),
            ("a", "missing", "x"),
            ("a", key, "x" * 129),
        ]:
            with pytest.raises(ValueError):
                store.record_feedback(ctx, key_, 1, event)
        with pytest.raises(ValueError):
            store.feedback("a", [key] * 1001)
        assert store.feedback("a", []) == {}


def test_abrupt_process_exit(tmp_path):
    path = tmp_path / "memory.rvf"
    code = """import os,sys
from lionagi import Node
from lionagi.integrations.ruvector.persistent import PersistentRuVectorStore
s=PersistentRuVectorStore(sys.argv[1],3,embedding_id="test:v1")
k=s.add_nodes([Node(content="committed",embedding=[1,0,0])])[0]
s.record_feedback("ctx",k,1,"event")
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, str(path)], check=True)
    with make(path) as store:
        hit = store.search([1, 0, 0])[0]
        assert hit.content == "committed"
        assert store.feedback("ctx", [hit.node_id]) == {hit.node_id: (1, 1)}


@pytest.mark.parametrize("operation", ["add", "delete"])
def test_failed_commit_reopen_canonical(tmp_path, monkeypatch, operation):
    path = tmp_path / "memory.rvf"
    with make(path) as store:
        key = store.add_nodes([node()])[0]

        def fail():
            raise OSError("injected commit failure")

        monkeypatch.setattr(store, "_checkpoint", fail)
        with pytest.raises(OSError):
            if operation == "add":
                store.add_nodes([node()])
            else:
                store.delete([key])
        with pytest.raises(RuntimeError):
            len(store)
    with make(path) as store:
        assert len(store) == 1
        assert store.get(key) is not None


def test_config_and_schema_rejected(tmp_path):
    path = tmp_path / "memory.rvf"
    make(path).close()
    with pytest.raises(ValueError, match="configuration"):
        make(path, ef_search=101)
    with pytest.raises(ValueError, match="configuration"):
        PersistentRuVectorStore(path, 3, embedding_id="other")
    rewrite_manifest(path, lambda state: state.update(unexpected=True))
    with pytest.raises(ValueError, match="schema"):
        make(path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_path_guards(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "memory.rvf"
    make(path).close()
    assert path.stat().st_mode & 0o777 == 0o600
    link = private / "link.db"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        make(link)
    os.chmod(private, 0o755)
    with pytest.raises(ValueError, match="private"):
        make(path)


@pytest.mark.asyncio
async def test_memory_envelope_roundtrip(tmp_path):
    from lionagi.integrations.ruvector.memory import RuVectorMemoryStore
    from lionagi.protocols.memory import MemoryItem, MemoryQuery

    async def embed(text):
        return [1.0, 0.0, 0.0]

    path = tmp_path / "memory.rvf"
    item = MemoryItem(content="deployment", tags=["ops"], metadata={"revision": 3})
    with make(path) as store:
        memory = RuVectorMemoryStore(store, embed)
        key = await memory.store(item)
    with make(path) as store:
        memory = RuVectorMemoryStore(store, embed)
        found = await memory.retrieve(key)
        assert found.content == item.content
        assert found.tags == item.tags
        assert found.metadata == item.metadata
        assert found.created_at == item.created_at
        hits = await memory.search(MemoryQuery(text="release", tags=["ops"]))
        assert hits[0].id == key


def test_ambiguous_commit_recovers_committed_record(tmp_path, monkeypatch):
    path = tmp_path / "memory.rvf"
    with make(path) as store:
        real_commit = store._checkpoint

        def ambiguous():
            real_commit()
            raise OSError("lost acknowledgment")

        monkeypatch.setattr(store, "_checkpoint", ambiguous)
        n = node()
        with pytest.raises(OSError):
            store.add_nodes([n])
        with pytest.raises(RuntimeError):
            store.get(str(n.id))
    with make(path) as store:
        assert store.get(str(n.id)) is not None


def test_invalid_add_leaves_instance_usable(tmp_path):
    with make(tmp_path / "memory.rvf") as store:
        with pytest.raises(ValueError):
            store.add_nodes([Node(content="bad", embedding=[0, 0, 0])])
        assert len(store) == 0
        store.add_nodes([node()])
        assert len(store) == 1


def rewrite_manifest(path, edit):
    native = ruvector.RvfStore.open(str(path), read_only=False)
    try:
        state = json.loads(PersistentRuVectorStore._read_manifest(native))
        edit(state)
        PersistentRuVectorStore._write_manifest(native, json.dumps(state))
    finally:
        native.close()


def test_malformed_metadata_rejected(tmp_path):
    path = tmp_path / "memory.rvf"
    with make(path) as store:
        store.add_nodes([node()])
    rewrite_manifest(path, lambda state: state["records"][0].__setitem__(2, {"bad": float("nan")}))
    with pytest.raises(ValueError):
        make(path)


def test_feedback_capacity(tmp_path):
    with make(tmp_path / "memory.rvf") as store:
        key = store.add_nodes([node()])[0]
        store._events = {str(i): ("ctx", key, 1.0) for i in range(10000)}
        store._reindex_feedback()
        store._checkpoint()
        with pytest.raises(ValueError, match="capacity"):
            store.record_feedback("ctx", key, 1, "extra")
        assert store.feedback("ctx", [key]) == {key: (10000, 10000)}
        assert store.reset_feedback("ctx") == 10000


def test_missing_native_apis(tmp_path, monkeypatch):
    monkeypatch.setattr(ruvector, "RvfStore", object, raising=False)
    with pytest.raises(RuntimeError, match="requires a RuVector SDK"):
        make(tmp_path / "missing.rvf")


def test_native_rvf_readable(tmp_path):
    path = tmp_path / "memory.rvf"
    with make(path) as store:
        key = store.add_nodes([node()])[0]
    native = ruvector.RvfStore.open(str(path))
    try:
        assert native.dimension == 3
        assert native.export_vectors() == [(0, [1.0, 0.0, 0.0])]
        assert json.loads(PersistentRuVectorStore._read_manifest(native))["records"][0][0] == key
    finally:
        native.close()


def test_invalid_native_vector_rejected(tmp_path):
    path = tmp_path / "memory.rvf"
    with make(path) as store:
        store.add_nodes([node()])
    old = ruvector.RvfStore.open(str(path))
    state = PersistentRuVectorStore._read_manifest(old)
    old.close()
    path.unlink()
    native = ruvector.RvfStore.create(str(path), 3)
    native.ingest([[2.0, 0.0, 0.0]], [0])
    PersistentRuVectorStore._write_manifest(native, state)
    native.close()
    os.chmod(path, 0o600)
    with pytest.raises(ValueError, match="normalized"):
        make(path)


def test_crash_before_replace_keeps_previous_checkpoint(tmp_path):
    path = tmp_path / "memory.rvf"
    with make(path) as store:
        key = store.add_nodes([node()])[0]
    code = """import os,sys
from lionagi import Node
from lionagi.integrations.ruvector.persistent import PersistentRuVectorStore
s=PersistentRuVectorStore(sys.argv[1],3,embedding_id="test:v1")
s._replace_checkpoint=lambda _:os._exit(43)
s.add_nodes([Node(content="uncommitted",embedding=[0,1,0])])
"""
    result = subprocess.run([sys.executable, "-c", code, str(path)], check=False)
    assert result.returncode == 43
    with make(path) as store:
        assert len(store) == 1
        assert store.get(key) is not None


def test_manifest_chunk_roundtrip_and_corruption(tmp_path):
    path = tmp_path / "chunks.rvf"
    native = ruvector.RvfStore.create(str(path), 3)
    try:
        value = "é" * 1100000
        PersistentRuVectorStore._write_manifest(native, value)
        assert PersistentRuVectorStore._read_manifest(native) == value
        native.set_metadata("lionagi_state_v1.1", "changed")
        with pytest.raises(ValueError, match="checksum"):
            PersistentRuVectorStore._read_manifest(native)
    finally:
        native.close()


@pytest.mark.parametrize("operation", ["feedback", "reset"])
def test_feedback_checkpoint_failure_recovers(tmp_path, monkeypatch, operation):
    path = tmp_path / "memory.rvf"
    with make(path) as store:
        key = store.add_nodes([node()])[0]
        store.record_feedback("ctx", key, 1, "first")

        def fail(_):
            raise OSError("rename denied")

        monkeypatch.setattr(store, "_replace_checkpoint", fail)
        with pytest.raises(OSError):
            if operation == "feedback":
                store.record_feedback("ctx", key, -1, "second")
            else:
                store.reset_feedback("ctx")
        with pytest.raises(RuntimeError):
            store.feedback("ctx", [key])
    with make(path) as store:
        assert store.feedback("ctx", [key]) == {key: (1, 1)}


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory fsync")
def test_directory_fsync_failure_recovers_complete_new_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "memory.rvf"
    with make(path) as store:
        key = store.add_nodes([node()])[0]

        def fail(_):
            raise OSError("directory fsync failed")

        monkeypatch.setattr(os, "fsync", fail)
        with pytest.raises(OSError):
            store.record_feedback("ctx", key, 1, "event")
        with pytest.raises(RuntimeError):
            len(store)
    monkeypatch.undo()
    with make(path) as store:
        assert store.feedback("ctx", [key]) == {key: (1, 1)}


def test_checkpoint_budget_checked_before_replace(tmp_path, monkeypatch):
    path = tmp_path / "memory.rvf"
    with make(path) as store:
        key = store.add_nodes([node()])[0]
        validate = store._validate_path

        def reject_temporary(candidate, **kwargs):
            if candidate != path:
                raise ValueError("RVF exceeds byte budget")
            return validate(candidate, **kwargs)

        monkeypatch.setattr(store, "_validate_path", reject_temporary)
        with pytest.raises(ValueError, match="byte budget"):
            store.delete([key])
    with make(path) as store:
        assert store.get(key) is not None
