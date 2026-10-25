# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pin the serialized bytes behind persisted schedule and engine fingerprints."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from lionagi.cli.engine import _engine_outcome
from lionagi.ln._hash import MAX_HASH_INPUT_BYTES
from lionagi.service.accounts._pricing import CURRENT_REVISION, quote_ceiling
from lionagi.studio.services.schedule_declaration import _digest_of
from lionhub.serve.mcp._child_actor import _CONFIG, mint_child_actor_config


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        ({"z": "é", "a": [1, True, None]}, b'{"a": [1, true, null], "z": "\\u00e9"}'),
        ("flow:\n  task: café", b'"flow:\\n  task: caf\\u00e9"'),
        (Path("/workspace/model.json"), b'"/workspace/model.json"'),
        ({"v": float("nan")}, b'{"v": NaN}'),
        ({"v": float("inf")}, b'{"v": Infinity}'),
    ],
)
def test_schedule_digest_preserves_serialized_bytes(value, encoded):
    assert _digest_of(value) == hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize(
    ("engine", "encoded"),
    [
        (
            None,
            b'{"kind": "research", "max_agents": null, "max_depth": null, "model_known": false}',
        ),
        (
            SimpleNamespace(served_model="model", max_depth=2, max_agents=4),
            b'{"kind": "research", "max_agents": 4, "max_depth": 2, "model_known": true}',
        ),
        (
            SimpleNamespace(served_model="", max_depth=Path("café"), max_agents=0),
            b'{"kind": "research", "max_agents": 0, "max_depth": "caf\\u00e9", "model_known": false}',
        ),
    ],
)
def test_engine_fingerprint_preserves_serialized_bytes_and_width(engine, encoded):
    outcome = _engine_outcome(
        status="completed", kind="research", started_at=1.0, ended_at=2.0, engine=engine
    )
    assert outcome["engine"]["config_fingerprint"] == hashlib.sha256(encoded).hexdigest()[:16]


def _request_body(content: bytes) -> bytes:
    return (
        b'{"messages": [{"content": "' + content + b'", "role": "user"}], "max_tokens": 1, '
        b'"service_tier": "standard_only", "model": "claude-opus-5-5"}\r\n'
    )


@pytest.mark.parametrize("content", [b"caf\\u00e9", "café".encode()])
@pytest.mark.parametrize("container", [bytes, bytearray])
def test_request_fingerprint_preserves_raw_json_encoding(content, container):
    body = container(_request_body(content))
    assert quote_ceiling(CURRENT_REVISION, body).request_digest == hashlib.sha256(body).hexdigest()


def test_request_fingerprint_keeps_rejecting_text_after_request_validation():
    body = _request_body(b"hello").decode()
    with pytest.raises(TypeError, match="Strings must be encoded before hashing"):
        quote_ceiling(CURRENT_REVISION, body)


def _assert_child_config_fingerprint(tmp_path, source_bytes):
    submitter = tmp_path / "submitter"
    source = submitter / _CONFIG
    source.parent.mkdir(parents=True)
    source.write_bytes(source_bytes)
    child = tmp_path / "child"
    child.mkdir()
    minted = mint_child_actor_config(str(submitter), str(child), ":worker")
    header = Path(minted.config_path).read_text().splitlines()[0]
    prefix = hashlib.sha256(source_bytes).hexdigest()[:8]
    assert f"(source sha256 prefix {prefix}); only [actor] differs." in header


def test_child_config_fingerprint_preserves_raw_source_bytes(tmp_path):
    source_bytes = '# café\r\n[actor]\r\nid = "agent:parent"\r\n'.encode()
    _assert_child_config_fingerprint(tmp_path, source_bytes)


@pytest.mark.parametrize("consumer", ["schedule", "engine", "child"])
def test_fingerprint_inputs_keep_their_existing_unbounded_size(consumer, tmp_path):
    value = "x" * (MAX_HASH_INPUT_BYTES + 1)
    if consumer == "schedule":
        encoded = b'"' + value.encode() + b'"'
        assert _digest_of(value) == hashlib.sha256(encoded).hexdigest()
    elif consumer == "engine":
        encoded = (
            b'{"kind": "research", "max_agents": null, "max_depth": "'
            + value.encode()
            + b'", "model_known": false}'
        )
        outcome = _engine_outcome(
            status="completed",
            kind="research",
            started_at=1.0,
            ended_at=2.0,
            engine=SimpleNamespace(max_depth=value),
        )
        assert outcome["engine"]["config_fingerprint"] == hashlib.sha256(encoded).hexdigest()[:16]
    else:
        source_bytes = b"# " + value.encode() + b'\n[actor]\nid = "agent:parent"\n'
        _assert_child_config_fingerprint(tmp_path, source_bytes)
