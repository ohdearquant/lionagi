# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Journal-aware recovery, dispatch and legacy reader compatibility."""

import json
import os
import sys

import pytest

from lionagi import _checkpoint_v3_codec as codec
from lionagi import _checkpoint_v3_reader as reader
from lionagi._checkpoint_version import UnsupportedCheckpointVersionError

GEN = "0123456789abcdef0123456789abcdef"
OLD = "fedcba9876543210fedcba9876543210"


def base(generation=GEN):
    return {
        "version": 3,
        "generation": generation,
        "journal_seq": 0,
        "session_id": "s",
        "prompt": "secret prompt",
        "plan": [],
        "config": {},
        "flow_context": {"list": []},
        "ops": {},
        "spawned": [],
    }


def line(seq=1, generation=GEN, kind="op", entry=None, patch=None):
    entry = entry or {"agent_id": "a", "status": "completed", "response": "private response"}
    obj = {"version": 3, "generation": generation, "seq": seq, "kind": kind, "entry": entry}
    if patch is not None:
        obj["flow_context"] = {"patch": patch}
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode()


def write_unit(tmp_path, *, snapshot=None, journal=b""):
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(base() if snapshot is None else snapshot))
    path.with_name(path.name + ".journal").write_bytes(journal)
    return path


def test_complete_replay_upserts_and_preserves_extensions(tmp_path):
    spawned = {
        "node_id": "child",
        "status": "failed",
        "response": None,
        "operation": "work",
        "parent_id": "a",
        "custom": {"x": 1},
    }
    journal = (
        line(patch=[{"op": "add", "path": "/list/-", "value": "x"}])
        + line(2, kind="spawned", entry=spawned)
        + line(3, entry={"agent_id": "a", "status": "failed", "response": "later"})
    )
    state, report = reader.load_checkpoint_v3(write_unit(tmp_path, journal=journal))
    assert state["flow_context"] == {"list": ["x"]}
    assert state["ops"]["a"]["response"] == "later"
    assert state["spawned"] == [spawned]
    assert report["disposition"] == "clean"
    assert report["applied_records"] == 3
    assert report["last_applied_sequence"] == 3
    assert report["valid_prefix_bytes"] == len(journal)
    assert "checkpoint_recovery" not in state


def test_torn_tail_and_stale_line_are_counted_without_application(tmp_path):
    stale = line(12, generation=OLD)
    current = line()
    path = write_unit(tmp_path, journal=stale + current + b'{"partial":')
    state, report = reader.load_checkpoint_v3(path)
    assert list(state["ops"]) == ["a"]
    assert report["valid_prefix_bytes"] == len(stale + current)
    assert report["ignored"] == {"stale_generation_record": 1, "torn_final_record": 1}
    assert report["problem"]["code"] == "torn_final_record"
    assert report["disposition"] == "recovered_prefix"


def test_complete_invalid_line_refuses_with_prefix_and_sanitized_error(tmp_path):
    first = line()
    secret = b'{"secret":"private response","secret":1}\n'
    path = write_unit(tmp_path, journal=first + secret)
    with pytest.raises(reader.CheckpointRecoveryError) as exc:
        reader.load_checkpoint_v3(path)
    error = exc.value
    assert str(error) == "invalid_record"
    assert "private response" not in str(error)
    assert error.checkpoint_recovery["valid_prefix_bytes"] == len(first)
    assert error.checkpoint_recovery["problem"]["line"] == 2
    assert error.valid_prefix["ops"]["a"]["status"] == "completed"


@pytest.mark.parametrize("suffix", [line(3), line(1)])
def test_sequence_gap_and_duplicate_refuse(tmp_path, suffix):
    with pytest.raises(reader.CheckpointRecoveryError) as exc:
        reader.load_checkpoint_v3(write_unit(tmp_path, journal=line() + suffix))
    problem = exc.value.checkpoint_recovery["problem"]
    assert problem["code"] == "invalid_sequence"
    assert problem["expected_seq"] == 2
    assert problem["actual_seq"] in (1, 3)
    assert exc.value.checkpoint_recovery["applied_records"] == 1


def test_failed_late_patch_does_not_apply_outcome_or_earlier_patch(tmp_path):
    broken = line(
        patch=[{"op": "add", "path": "/list/-", "value": "x"}, {"op": "remove", "path": "/missing"}]
    )
    with pytest.raises(reader.CheckpointRecoveryError) as exc:
        reader.load_checkpoint_v3(write_unit(tmp_path, journal=broken))
    assert exc.value.valid_prefix["ops"] == {}
    assert exc.value.valid_prefix["flow_context"] == {"list": []}
    assert exc.value.checkpoint_recovery["valid_prefix_bytes"] == 0


def test_duplicate_decoded_base_key_has_no_prefix(tmp_path):
    path = write_unit(tmp_path)
    path.write_bytes(b'{"version":3,"\\u0076ersion":3}')
    with pytest.raises(reader.CheckpointRecoveryError) as exc:
        reader.load_checkpoint_v3(path)
    assert exc.value.checkpoint_recovery["problem"]["code"] == "invalid_record"
    assert exc.value.checkpoint_recovery["valid_prefix_bytes"] is None
    assert exc.value.valid_prefix is None


def test_carriage_return_does_not_terminate_a_record(tmp_path):
    with pytest.raises(reader.CheckpointRecoveryError) as exc:
        reader.load_checkpoint_v3(write_unit(tmp_path, journal=b"{}\r{}\n"))
    assert exc.value.checkpoint_recovery["problem"]["code"] == "invalid_record"
    assert exc.value.checkpoint_recovery["ignored"]["torn_final_record"] == 0


def test_missing_journal_is_refused_after_bounded_retries(tmp_path):
    path = write_unit(tmp_path)
    path.with_name(path.name + ".journal").unlink()
    with pytest.raises(reader.CheckpointRecoveryError) as exc:
        reader.load_checkpoint_v3(path, retries=2)
    assert exc.value.checkpoint_recovery["problem"]["code"] == "missing_journal"
    assert exc.value.checkpoint_recovery["valid_prefix_bytes"] is None


def test_compaction_between_base_and_journal_retries_not_old_base_with_reset(tmp_path, monkeypatch):
    old = base(OLD)
    old["ops"] = {"old": {"agent_id": "old", "status": "completed", "response": 1}}
    new = base(GEN)
    new["ops"] = {"new": {"agent_id": "new", "status": "completed", "response": 2}}
    path = write_unit(tmp_path, snapshot=old, journal=line(generation=OLD))
    replacement = tmp_path / "replacement"
    replacement.write_text(json.dumps(new))
    real_read = reader._read_bound
    switched = False

    def read_with_compaction(target):
        nonlocal switched
        if target.name.endswith(".journal") and not switched:
            switched = True
            os.replace(replacement, path)
            replacement_journal = tmp_path / "replacement-journal"
            replacement_journal.write_bytes(b"")
            os.replace(replacement_journal, target)
        return real_read(target)

    monkeypatch.setattr(reader, "_read_bound", read_with_compaction)
    state, report = reader.load_checkpoint_v3(path)
    assert switched
    assert list(state["ops"]) == ["new"]
    assert report["generation"] == GEN


def test_interrupted_reset_ignores_old_generation_journal(tmp_path):
    path = write_unit(tmp_path, journal=line(generation=OLD))
    state, report = reader.load_checkpoint_v3(path)
    assert state["ops"] == {}
    assert report["ignored"]["stale_generation_record"] == 1
    assert report["disposition"] == "recovered_prefix"


def test_bounded_identity_churn_refuses_without_prefix(tmp_path, monkeypatch):
    path = write_unit(tmp_path)
    real_read = reader._read_bound

    def always_race(target):
        if target.name.endswith(".journal"):
            raise reader._CheckpointReadRaceError()
        return real_read(target)

    monkeypatch.setattr(reader, "_read_bound", always_race)
    with pytest.raises(reader.CheckpointRecoveryError) as exc:
        reader.load_checkpoint_v3(path, retries=2)
    assert exc.value.checkpoint_recovery["problem"]["code"] == "incoherent_view"
    assert exc.value.checkpoint_recovery["valid_prefix_bytes"] is None


@pytest.mark.parametrize("version", [None, 1, 2])
def test_shared_dispatch_preserves_legacy_nested_duplicate_behavior(tmp_path, version):
    path = tmp_path / "checkpoint.json"
    version_text = "" if version is None else f'"version":{version},'
    path.write_text("{" + version_text + '"nested":{"x":1,"x":2}}')
    state, report = reader.load_checkpoint_unit(path)
    assert state["nested"]["x"] == 2
    assert report["source_version"] == version
    assert report["generation"] is None
    assert report["valid_prefix_bytes"] is None
    assert report["disposition"] == "clean"


@pytest.mark.parametrize(
    "payload",
    [
        '{"version":2,"\\u0076ersion":3}',
        '{"version":3,"version":2}',
        '{"version":4}',
        '{"version":true}',
        '{"version":3.0}',
    ],
)
def test_shared_dispatch_refuses_ambiguous_or_unsupported_versions(tmp_path, payload):
    path = tmp_path / "checkpoint.json"
    path.write_text(payload)
    with pytest.raises(UnsupportedCheckpointVersionError):
        reader.load_checkpoint_unit(path)


def test_shared_dispatch_reconstructs_v3_journal(tmp_path):
    path = write_unit(tmp_path, journal=line())
    state, report = reader.load_checkpoint_unit(path)
    assert "a" in state["ops"]
    assert report["source_version"] == 3
    assert report["applied_records"] == 1


def test_v3_journal_nested_duplicate_escaped_member_is_refused(tmp_path):
    bad = (
        b'{"version":3,"generation":"'
        + GEN.encode()
        + b'","seq":1,"kind":"op","entry":{"agent_id":"a",'
        b'"status":"completed","response":{"x":1,"\\u0078":2}}}\n'
    )
    with pytest.raises(reader.CheckpointRecoveryError) as exc:
        reader.load_checkpoint_unit(write_unit(tmp_path, journal=bad))
    assert exc.value.checkpoint_recovery["problem"]["code"] == "invalid_record"
    assert exc.value.checkpoint_recovery["valid_prefix_bytes"] == 0


def test_shared_dispatch_preserves_unclassifiable_legacy_json_error(tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_text('{"version": 2,')
    with pytest.raises(json.JSONDecodeError):
        reader.load_checkpoint_unit(path)


def test_dispatch_reports_known_v3_for_duplicate_nested_base(tmp_path):
    path = write_unit(tmp_path)
    text = path.read_text()
    text = text.replace('"config": {}', '"config": {"x": 1, "\\u0078": 2}')
    path.write_text(text)
    with pytest.raises(reader.CheckpointRecoveryError) as exc:
        reader.load_checkpoint_unit(path)
    report = exc.value.checkpoint_recovery
    assert report["source_version"] == 3
    assert report["valid_prefix_bytes"] is None
    assert report["problem"]["code"] == "invalid_record"
    assert exc.value.valid_prefix is None
    assert "secret prompt" not in str(exc.value)


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"),
    reason="FIFO/nonblocking open unavailable",
)
def test_fifo_base_or_journal_refuses_without_blocking(tmp_path):
    path = write_unit(tmp_path)
    journal = path.with_name(path.name + ".journal")
    journal.unlink()
    os.mkfifo(journal)
    with pytest.raises(reader.CheckpointRecoveryError) as exc:
        reader.load_checkpoint_v3(path, retries=2)
    assert exc.value.checkpoint_recovery["problem"]["code"] == "incoherent_view"
    assert exc.value.checkpoint_recovery["valid_prefix_bytes"] is None
    journal.unlink()
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(reader.CheckpointRecoveryError) as exc:
        reader.load_checkpoint_unit(path, retries=2)
    assert exc.value.checkpoint_recovery["problem"]["code"] == "incoherent_view"


def test_nonblocking_read_preserves_regular_file_identity(tmp_path):
    path = tmp_path / "regular"
    path.write_bytes(b"hello")
    contents, identity = reader._read_bound(path)
    assert contents == b"hello"
    assert identity == (path.stat().st_dev, path.stat().st_ino)


def test_malformed_base_is_json_compatible_and_never_retains_document(tmp_path):
    text = '{\n  "private": "sensitive-é",\n  "broken": }'
    path = tmp_path / "checkpoint.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(json.JSONDecodeError) as baseline:
        json.loads(text)
    with pytest.raises(reader.CheckpointMalformedBaseError) as caught:
        reader.load_checkpoint_unit(path)
    error = caught.value
    original = baseline.value
    assert isinstance(error, reader.CheckpointRecoveryError)
    assert isinstance(error, json.JSONDecodeError)
    assert (error.msg, error.pos, error.lineno, error.colno) == (
        original.msg,
        original.pos,
        original.lineno,
        original.colno,
    )
    assert error.doc == ""
    assert error.__context__ is None
    for value in (str(error), repr(error), repr(error.args), error.doc):
        assert "sensitive" not in value
    report = error.checkpoint_recovery
    assert report["source_version"] is None
    assert report["disposition"] == "refused"
    assert report["valid_prefix_bytes"] is None
    assert report["problem"]["code"] == "invalid_record"
    assert report["problem"]["line"] == original.lineno
    assert report["problem"]["byte_offset"] == len(text[: original.pos].encode("utf-8"))
    assert error.valid_prefix is None


def test_direct_v3_loader_malformed_base_has_json_and_recovery_error(tmp_path):
    path = write_unit(tmp_path)
    path.write_text('{"secret": "private",', encoding="utf-8")
    with pytest.raises(reader.CheckpointMalformedBaseError) as caught:
        reader.load_checkpoint_v3(path)
    assert isinstance(caught.value, json.JSONDecodeError)
    assert caught.value.checkpoint_recovery["source_version"] is None
    assert caught.value.doc == ""
    assert "private" not in str(caught.value)
    assert caught.value.__context__ is None


def test_replaced_malformed_base_does_not_inherit_old_identified_version(tmp_path, monkeypatch):
    path = write_unit(tmp_path)
    replacement = tmp_path / "replacement-base"
    replacement.write_text('{"sensitive": "private",', encoding="utf-8")
    original_read = reader._read_bound
    swapped = False

    def read_then_replace(target):
        nonlocal swapped
        captured = original_read(target)
        if target == path and not swapped:
            swapped = True
            os.replace(replacement, path)
        return captured

    monkeypatch.setattr(reader, "_read_bound", read_then_replace)
    with pytest.raises(reader.CheckpointMalformedBaseError) as caught:
        reader.load_checkpoint_unit(path)
    assert swapped
    assert caught.value.checkpoint_recovery["source_version"] is None
    assert caught.value.doc == ""
    assert "private" not in repr(caught.value)


@pytest.mark.parametrize("loader", [reader.load_checkpoint_unit, reader.load_checkpoint_v3])
def test_invalid_utf8_base_is_dual_typed_with_redacted_real_offset(tmp_path, loader):
    path = write_unit(tmp_path)
    prefix = '{"secret":"private payload","é":"'.encode()
    path.write_bytes(prefix + b"\xff" + b'"}')
    with pytest.raises(reader.CheckpointInvalidUTF8Error) as caught:
        loader(path)
    error = caught.value
    assert isinstance(error, reader.CheckpointRecoveryError)
    assert isinstance(error, UnicodeDecodeError)
    assert (error.start, error.end) == (len(prefix), len(prefix) + 1)
    assert error.object == b""
    assert error.__context__ is None
    for text in (str(error), repr(error), repr(error.args), repr(error.object)):
        assert "private payload" not in text
    report = error.checkpoint_recovery
    assert report["disposition"] == "refused"
    assert report["source_version"] is None
    assert report["valid_prefix_bytes"] is None
    assert report["problem"]["code"] == "invalid_record"
    assert report["problem"]["byte_offset"] == len(prefix)
    assert report["problem"]["line"] is None
    assert error.valid_prefix is None


def test_replaced_invalid_utf8_base_does_not_inherit_old_version(tmp_path, monkeypatch):
    path = write_unit(tmp_path)
    replacement = tmp_path / "replacement-base"
    replacement.write_bytes(b'{"private":"secret",\xff}')
    original_read = reader._read_bound
    swapped = False

    def read_then_replace(target):
        nonlocal swapped
        captured = original_read(target)
        if target == path and not swapped:
            swapped = True
            os.replace(replacement, path)
        return captured

    monkeypatch.setattr(reader, "_read_bound", read_then_replace)
    with pytest.raises(reader.CheckpointInvalidUTF8Error) as caught:
        reader.load_checkpoint_unit(path)
    assert swapped
    assert caught.value.checkpoint_recovery["source_version"] is None
    assert caught.value.checkpoint_recovery["problem"]["byte_offset"] == len(
        b'{"private":"secret",'
    )
    assert caught.value.object == b""
    assert "secret" not in repr(caught.value)


@pytest.mark.parametrize(
    ("replacement_text", "expected_version"),
    [
        ('{"version":2,"nested":{"x":1,"x":2}}', 2),
        ('{"version":2,"nested":{"x":NaN}}', 2),
        ('{"nested":{"x":1,"x":2}}', None),
        ('{"version":2,"version":3,"nested":{}}', None),
    ],
)
def test_replaced_invalid_base_reports_only_captured_version(
    tmp_path, monkeypatch, replacement_text, expected_version
):
    path = write_unit(tmp_path)
    replacement = tmp_path / "replacement-base"
    replacement.write_text(replacement_text, encoding="utf-8")
    original_read = reader._read_bound
    swapped = False

    def read_then_replace(target):
        nonlocal swapped
        captured = original_read(target)
        if target == path and not swapped:
            swapped = True
            os.replace(replacement, path)
        return captured

    monkeypatch.setattr(reader, "_read_bound", read_then_replace)
    with pytest.raises(reader.CheckpointRecoveryError) as caught:
        reader.load_checkpoint_unit(path)
    assert swapped
    assert caught.value.checkpoint_recovery["source_version"] == expected_version
    assert caught.value.checkpoint_recovery["problem"]["code"] == "invalid_record"
    assert caught.value.checkpoint_recovery["valid_prefix_bytes"] is None


def assert_resource_refusal(error, message):
    assert str(error) == message
    assert error.__context__ is None
    assert error.__cause__ is None
    assert "secret payload" not in repr(error)
    report = error.checkpoint_recovery
    assert report["source_version"] is None
    assert report["valid_prefix_bytes"] is None
    assert report["disposition"] == "refused"
    assert report["problem"] == {
        "code": "invalid_record",
        "line": None,
        "byte_offset": None,
        "expected_seq": None,
        "actual_seq": None,
    }


@pytest.mark.parametrize("version", [2, 3])
def test_dispatch_decoder_nesting_failure_is_redacted_and_unclassified(
    tmp_path, monkeypatch, version
):
    # Exercise the error-handling seam deterministically. Python versions use
    # different decoder depth limits, so this does not measure that limit.
    snapshot = base() if version == 3 else {"version": 2, "prompt": "secret payload"}
    path = write_unit(tmp_path, snapshot=snapshot)
    state, report = reader.load_checkpoint_unit(path)
    assert state["version"] == version
    assert report["source_version"] == version
    original_loads = reader.json.loads
    injected_calls = []

    def inject_depth_error(data, *args, **kwargs):
        if kwargs.get("object_pairs_hook") is reader._ObjectPairs:
            injected_calls.append(True)
            raise RecursionError("secret payload")
        return original_loads(data, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(reader.json, "loads", inject_depth_error)
        with pytest.raises(reader.CheckpointRecoveryError) as caught:
            reader.load_checkpoint_unit(path)
    assert injected_calls == [True]
    assert_resource_refusal(caught.value, "nesting depth")


@pytest.mark.parametrize("version", [2, 3])
def test_dispatch_decoder_integer_digit_limit_is_redacted_and_unclassified(tmp_path, version):
    get_limit = getattr(sys, "get_int_max_str_digits", None)
    if get_limit is None or not (0 < get_limit() <= 10000):
        pytest.skip("integer digit limit is unavailable, disabled, or above fixture bound")
    digits = max(5000, get_limit() + 1)
    path = tmp_path / "checkpoint.json"
    path.write_text(
        f'{{"version":{version},"secret":"secret payload","n":' + "1" * digits + "}",
        encoding="utf-8",
    )
    with pytest.raises(reader.CheckpointRecoveryError) as caught:
        reader.load_checkpoint_unit(path)
    assert_resource_refusal(caught.value, "integer digit limit")


def test_dispatch_does_not_intercept_memory_or_unicode_errors(tmp_path, monkeypatch):
    path = tmp_path / "checkpoint.json"
    path.write_text('{"version":2}', encoding="utf-8")

    def raise_memory(*args, **kwargs):
        raise MemoryError("unchanged")

    with monkeypatch.context() as patch:
        patch.setattr(reader.json, "loads", raise_memory)
        with pytest.raises(MemoryError, match="unchanged"):
            reader.load_checkpoint_unit(path)

    def raise_unicode(*args, **kwargs):
        raise UnicodeError("unchanged")

    with monkeypatch.context() as patch:
        patch.setattr(reader.json, "loads", raise_unicode)
        with pytest.raises(UnicodeError, match="unchanged"):
            reader.load_checkpoint_unit(path)


@pytest.mark.parametrize(
    "bad_line",
    [
        b'{"secret":"sensitive-journal",}\n',
        b'{"secret":"sensitive-journal\xff"}\n',
    ],
)
def test_journal_parser_error_does_not_retain_secret_in_exception_chain(tmp_path, bad_line):
    prefix = line()
    path = write_unit(tmp_path, journal=prefix + bad_line)
    with pytest.raises(reader.CheckpointRecoveryError) as caught:
        reader.load_checkpoint_unit(path)
    error = caught.value
    assert error.__context__ is None
    assert error.__cause__ is None
    assert "sensitive-journal" not in str(error)
    assert "sensitive-journal" not in repr(error)
    assert error.checkpoint_recovery["problem"]["code"] == "invalid_record"
    assert error.checkpoint_recovery["problem"]["line"] == 2
    assert error.checkpoint_recovery["valid_prefix_bytes"] == len(prefix)
    assert error.valid_prefix["ops"]["a"]["status"] == "completed"


def test_invalid_base_codec_error_is_not_retained_in_exception_chain(tmp_path):
    path = write_unit(tmp_path)
    text = path.read_text().replace('"config": {}', '"config": {"secret": 1, "secret": 2}')
    path.write_text(text)
    with pytest.raises(reader.CheckpointRecoveryError) as caught:
        reader.load_checkpoint_unit(path)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert caught.value.checkpoint_recovery["source_version"] == 3
    assert caught.value.checkpoint_recovery["valid_prefix_bytes"] is None


def test_invalid_patch_codec_error_is_not_retained_in_exception_chain(tmp_path):
    journal = line(patch=[{"op": "remove", "path": "/missing"}])
    with pytest.raises(reader.CheckpointRecoveryError) as caught:
        reader.load_checkpoint_unit(write_unit(tmp_path, journal=journal))
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert caught.value.valid_prefix["flow_context"] == {"list": []}
    assert caught.value.checkpoint_recovery["valid_prefix_bytes"] == 0


def _reference_replay_appends(snapshot, journal):
    """Independent small-fixture oracle using a fresh JSON context per record."""
    state = json.loads(json.dumps(snapshot))
    sequence = applied = stale = 0
    torn = False
    error = None
    for part in journal.splitlines(keepends=True):
        if not part.endswith(b"\n"):
            torn = True
            break
        record = json.loads(part)
        if record["generation"] != snapshot["generation"]:
            stale += 1
            continue
        if record["seq"] != sequence + 1:
            error = "invalid_sequence"
            break
        candidate = json.loads(json.dumps(state["flow_context"]))
        for operation in record.get("flow_context", {}).get("patch", []):
            if operation["op"] == "add" and operation["path"] == "/list/-":
                candidate["list"].append(operation["value"])
            else:
                error = "invalid_record"
                break
        if error:
            break
        entry = record["entry"]
        if record["kind"] == "op":
            state["ops"][entry["agent_id"]] = entry
        else:
            for index, previous in enumerate(state["spawned"]):
                if previous["node_id"] == entry["node_id"]:
                    state["spawned"][index] = entry
                    break
            else:
                state["spawned"].append(entry)
        state["flow_context"] = candidate
        sequence = record["seq"]
        applied += 1
    return state, applied, stale, torn, error


@pytest.mark.parametrize("mode", ["clean", "stale_torn", "bad_patch", "sequence"])
def test_replay_context_traversed_once_and_matches_per_record_oracle(tmp_path, monkeypatch, mode):
    snapshot = base()
    snapshot["flow_context"]["large"] = {
        "__sentinel_large__": True,
        "items": [{"value": number} for number in range(80)],
    }
    records = [
        line(
            number,
            kind="spawned" if number in (6, 10) else "op",
            entry=(
                {
                    "node_id": "child",
                    "status": "completed",
                    "response": number,
                    "parent_id": "parent",
                    "extension": {"kept": True},
                }
                if number in (6, 10)
                else {"agent_id": f"op-{number % 4}", "status": "completed", "response": number}
            ),
            patch=[{"op": "add", "path": "/list/-", "value": number}],
        )
        for number in range(1, 13)
    ]
    if mode == "stale_torn":
        records.insert(0, line(25, generation=OLD))
        records.append(b'{"unterminated":')
    elif mode == "bad_patch":
        records.append(
            line(
                13,
                entry={"agent_id": "bad", "status": "completed", "response": "bad"},
                patch=[
                    {"op": "add", "path": "/list/-", "value": "must-not-commit"},
                    {"op": "remove", "path": "/missing"},
                ],
            )
        )
    elif mode == "sequence":
        records.append(line(14))
    journal = b"".join(records)
    expected, applied, stale, torn, failure = _reference_replay_appends(snapshot, journal)
    path = write_unit(tmp_path, snapshot=snapshot, journal=journal)

    counts = {"context_freeze": 0, "context_thaw": 0, "large_subtree_visits": 0}
    original_freeze = reader.freeze_json
    original_reader_thaw = reader.thaw_json
    original_codec_thaw = codec.thaw_json

    def count_freeze(value, *args):
        if isinstance(value, dict) and "large" in value:
            counts["context_freeze"] += 1
        return original_freeze(value, *args)

    def count_reader_thaw(value):
        if isinstance(value, codec.FrozenObject) and "large" in value:
            counts["context_thaw"] += 1
        return original_reader_thaw(value)

    def count_codec_thaw(value):
        if isinstance(value, codec.FrozenObject) and "__sentinel_large__" in value:
            counts["large_subtree_visits"] += 1
        return original_codec_thaw(value)

    monkeypatch.setattr(reader, "freeze_json", count_freeze)
    monkeypatch.setattr(reader, "thaw_json", count_reader_thaw)
    monkeypatch.setattr(codec, "thaw_json", count_codec_thaw)
    if failure:
        with pytest.raises(reader.CheckpointRecoveryError) as caught:
            reader.load_checkpoint_v3(path)
        actual = caught.value.valid_prefix
        report = caught.value.checkpoint_recovery
        assert report["problem"]["code"] == failure
        assert caught.value.__context__ is None
    else:
        actual, report = reader.load_checkpoint_v3(path)

    def canonical(value):
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()

    assert canonical(actual) == canonical(expected)
    assert report["applied_records"] == applied
    assert report["ignored"] == {"stale_generation_record": stale, "torn_final_record": int(torn)}
    # Base validation visits the large subtree once; final materialization or
    # diagnostic-prefix exposure visits it once, independent of 12 records.
    assert counts == {"context_freeze": 1, "context_thaw": 1, "large_subtree_visits": 2}


def test_future_journal_version_refuses_with_valid_prefix_and_compatibility_code(tmp_path):
    prefix = line()
    future = (
        json.dumps(
            {
                "version": 4,
                "generation": GEN,
                "seq": 2,
                "kind": "op",
                "entry": {"agent_id": "later", "status": "completed", "response": "ok"},
                "future_field": True,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    with pytest.raises(reader.CheckpointRecoveryError) as caught:
        reader.load_checkpoint_unit(write_unit(tmp_path, journal=prefix + future))
    error = caught.value
    assert error.checkpoint_recovery["problem"]["code"] == "unsupported_version"
    assert error.checkpoint_recovery["problem"]["line"] == 2
    assert error.checkpoint_recovery["valid_prefix_bytes"] == len(prefix)
    assert error.valid_prefix["ops"]["a"]["status"] == "completed"
    assert error.__context__ is None
    assert error.__cause__ is None


def test_replay_remove_then_add_moves_object_member_to_end(tmp_path):
    snapshot = base()
    snapshot["flow_context"] = {"a": 1, "b": 2}
    journal = line(patch=[{"op": "remove", "path": "/a"}, {"op": "add", "path": "/a", "value": 1}])
    path = write_unit(tmp_path, snapshot=snapshot, journal=journal)

    state, report = reader.load_checkpoint_unit(path)

    assert list(state["flow_context"].items()) == [("b", 2), ("a", 1)]
    assert report["disposition"] == "clean"
    assert report["applied_records"] == 1
    assert report["valid_prefix_bytes"] == len(journal)


@pytest.mark.parametrize(
    "payload",
    [
        '{"version":1,"nested":{"x":1,"x":2,"version":1,"version":3}}',
        '{"version":2,"first":1,"second":2,"first":3,"array":[{"x":1,"x":4},[]]}',
        '{"nested":{"version":1,"version":2},"items":[null,true,3.5]}',
        '[{"version":1,"version":2},[1,{"x":1,"x":2}]]',
        '"scalar"',
        "null",
        "[" * 160 + '{"x":1,"x":2}' + "]" * 160,
    ],
    ids=["v1-nested", "v2-order", "version-absent", "array", "string", "null", "deep"],
)
def test_legacy_dispatch_reuses_parse_and_matches_legacy_semantics(tmp_path, monkeypatch, payload):
    from lionagi._checkpoint_version import load_legacy_checkpoint_json

    expected = load_legacy_checkpoint_json(payload)
    path = tmp_path / "checkpoint.json"
    path.write_text(payload, encoding="utf-8")
    calls = []
    original_loads = json.loads

    def counted_loads(*args, **kwargs):
        calls.append(1)
        return original_loads(*args, **kwargs)

    monkeypatch.setattr(reader.json, "loads", counted_loads)
    state, report = reader.load_checkpoint_unit(path)
    assert len(calls) == 1
    assert state == expected
    assert json.dumps(state) == json.dumps(expected)
    assert report["disposition"] == "clean"


@pytest.mark.parametrize("version", ["true", "2.0", '"2"', "null", "0", "4"])
def test_legacy_dispatch_rejects_invalid_version_after_one_parse(tmp_path, monkeypatch, version):
    path = tmp_path / "checkpoint.json"
    path.write_text('{"version":' + version + "}", encoding="utf-8")
    calls = []
    original_loads = json.loads

    def counted_loads(*args, **kwargs):
        calls.append(1)
        return original_loads(*args, **kwargs)

    monkeypatch.setattr(reader.json, "loads", counted_loads)
    with pytest.raises(UnsupportedCheckpointVersionError):
        reader.load_checkpoint_unit(path)
    assert len(calls) == 1


@pytest.mark.parametrize("error", [PermissionError, IsADirectoryError])
def test_dispatch_reports_incoherent_view_for_unreadable_base(tmp_path, monkeypatch, error):
    attempts = []

    def fail_read(path):
        attempts.append(path)
        raise error("private path")

    monkeypatch.setattr(reader, "_read_bound", fail_read)
    with pytest.raises(reader.CheckpointRecoveryError) as caught:
        reader.load_checkpoint_unit(tmp_path / "checkpoint.json", retries=2)
    assert len(attempts) == 2
    assert caught.value.checkpoint_recovery["problem"]["code"] == "incoherent_view"
    assert caught.value.checkpoint_recovery["valid_prefix_bytes"] is None
    assert "private path" not in str(caught.value)
