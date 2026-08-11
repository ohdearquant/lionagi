# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Binary payloads must not reach the log files, and the payload sent to the
provider must be identical whether or not redaction is on."""

import base64
import copy
import json
import os
from pathlib import Path

import pytest

from lionagi.protocols.generic.log import (
    DataLogger,
    DataLoggerConfig,
    Log,
    redact_binary_content,
)

IMAGE_BYTES = 48 * 1024
B64 = base64.b64encode(os.urandom(IMAGE_BYTES)).decode("ascii")
PNG_URI = f"data:image/png;base64,{B64}"


def _vision_payload() -> dict:
    return {
        "model": "gpt-4.1-mini",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is in this image?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": PNG_URI, "detail": "auto"},
                    },
                ],
            }
        ],
    }


def _dump(logger: DataLogger, tmp_path: Path, name: str = "out.json") -> tuple[int, dict]:
    fp = tmp_path / name
    logger.dump(clear=False, persist_path=fp)
    return fp.stat().st_size, json.loads(fp.read_text())


# the reported case


def test_image_payload_is_not_written_to_the_log_file(tmp_path):
    logger = DataLogger(persist_dir=str(tmp_path), auto_save_on_exit=False, capacity=None)
    logger.log({"payload": _vision_payload()})

    size, data = _dump(logger, tmp_path)

    assert B64 not in json.dumps(data)
    # The 48 KiB image becomes a placeholder; the whole file stays far under
    # the size of the raw payload it replaced.
    assert size < IMAGE_BYTES // 8

    url = data["content"]["payload"]["messages"][0]["content"][1]["image_url"]["url"]
    assert url.startswith("<lionagi:redacted-binary")
    assert "field=url" in url
    assert "media_type=image/png" in url
    assert f"bytes={IMAGE_BYTES}" in url


def test_redaction_does_not_mutate_the_payload_sent_to_the_provider(tmp_path):
    """The object the caller holds -- and hands to the endpoint -- is untouched."""
    payload = _vision_payload()
    before = copy.deepcopy(payload)

    logger = DataLogger(persist_dir=str(tmp_path), auto_save_on_exit=False, capacity=None)
    logger.log({"payload": payload})
    _dump(logger, tmp_path)

    assert payload == before
    assert payload["messages"][0]["content"][1]["image_url"]["url"] == PNG_URI


def test_redaction_survives_an_api_calling_event(tmp_path):
    """End to end over the type actually logged by branch.emit_and_log()."""
    from lionagi.service.connections.api_calling import APICalling
    from lionagi.service.imodel import iModel

    imodel = iModel(provider="openai", model="gpt-4.1-mini", api_key="dummy")
    payload = _vision_payload()
    call = APICalling(endpoint=imodel.endpoint, payload=payload, headers={})

    logger = DataLogger(persist_dir=str(tmp_path), auto_save_on_exit=False, capacity=None)
    logger.log(call)
    size, data = _dump(logger, tmp_path)

    assert B64 not in json.dumps(data)
    assert size < IMAGE_BYTES // 8
    # The live event still carries the real bytes for the request.
    assert call.payload["messages"][0]["content"][1]["image_url"]["url"] == PNG_URI


# the knob


def test_redaction_is_on_by_default():
    assert DataLoggerConfig().redact_binary is True


def test_redaction_can_be_turned_off(tmp_path):
    logger = DataLogger(
        persist_dir=str(tmp_path),
        auto_save_on_exit=False,
        capacity=None,
        redact_binary=False,
    )
    logger.log({"payload": _vision_payload()})
    size, data = _dump(logger, tmp_path)

    assert B64 in json.dumps(data)
    assert size > IMAGE_BYTES


def test_threshold_keeps_small_payloads(tmp_path):
    small = base64.b64encode(b"x" * 64).decode("ascii")
    logger = DataLogger(persist_dir=str(tmp_path), auto_save_on_exit=False, capacity=None)
    logger.log({"url": f"data:image/png;base64,{small}"})
    _, data = _dump(logger, tmp_path)

    assert data["content"]["url"].endswith(small)


def test_threshold_is_configurable(tmp_path):
    small = base64.b64encode(b"x" * 64).decode("ascii")
    logger = DataLogger(
        persist_dir=str(tmp_path),
        auto_save_on_exit=False,
        capacity=None,
        redact_binary_threshold=16,
    )
    logger.log({"url": f"data:image/png;base64,{small}"})
    _, data = _dump(logger, tmp_path)

    assert data["content"]["url"].startswith("<lionagi:redacted-binary")


def test_settings_expose_the_knob():
    from lionagi.config import settings

    assert "redact_binary" in settings.LOG_CONFIG
    assert "redact_binary_threshold" in settings.LOG_CONFIG


def test_a_prebuilt_log_is_redacted_too(tmp_path):
    """Redaction is a property of writing to disk, not of how the Log was made."""
    log = Log.create({"payload": _vision_payload()})
    logger = DataLogger(persist_dir=str(tmp_path), auto_save_on_exit=False, capacity=None)
    logger.log(log)
    _, data = _dump(logger, tmp_path)

    assert B64 not in json.dumps(data)
    # Identity is preserved across the rebuild, so the entry still correlates.
    assert data["id"] == str(log.id)


@pytest.mark.anyio
async def test_async_dump_redacts(tmp_path):
    logger = DataLogger(persist_dir=str(tmp_path), auto_save_on_exit=False, capacity=None)
    await logger.alog({"payload": _vision_payload()})
    fp = tmp_path / "async.json"
    await logger.adump(clear=False, persist_path=fp)

    assert B64 not in fp.read_text()


# the predicate


@pytest.mark.parametrize(
    "payload,path",
    [
        # Anthropic content block
        (
            {
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/webp",
                            "data": B64,
                        },
                    }
                ]
            },
            lambda d: d["content"][0]["source"]["data"],
        ),
        # Ollama multimodal
        ({"images": [B64]}, lambda d: d["images"][0]),
        # OpenAI image generation response
        ({"b64_json": B64}, lambda d: d["b64_json"]),
        # A tool result carrying an inline image
        (
            {"media_type": "image/jpeg", "content": f"data:image/jpeg;base64,{B64}"},
            lambda d: d["content"],
        ),
    ],
)
def test_known_binary_shapes_are_caught(payload, path):
    out = redact_binary_content(payload, threshold=1024)
    assert path(out).startswith("<lionagi:redacted-binary")
    assert f"bytes={IMAGE_BYTES}" in path(out)


def test_media_type_comes_from_a_sibling_when_the_string_has_none():
    out = redact_binary_content(
        {"source": {"type": "base64", "media_type": "image/gif", "data": B64}},
        threshold=1024,
    )
    assert "media_type=image/gif" in out["source"]["data"]


def test_large_text_is_kept():
    """The predicate is 'base64-encoded binary', not 'large'. Prose stays."""
    prose = "the quick brown fox jumps over the lazy dog. " * 5000
    out = redact_binary_content({"data": prose, "text": prose}, threshold=1024)
    assert out["data"] == prose
    assert out["text"] == prose


def test_base64_shaped_string_under_an_unrelated_key_is_kept():
    out = redact_binary_content({"reasoning": B64}, threshold=1024)
    assert out["reasoning"] == B64


def test_unchanged_input_is_returned_as_is():
    """No copying when there is nothing to redact -- redaction is on by default,
    so the common no-image path must not pay for it."""
    data = {"messages": [{"role": "user", "content": "hello"}]}
    assert redact_binary_content(data, threshold=1024) is data
