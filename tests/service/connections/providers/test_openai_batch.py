# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from lionagi.providers.openai._config import OpenAIConfigs
from lionagi.providers.openai.batch import OpenaiBatchEndpoint, OpenAIBatchRequest
from lionagi.service.connections.match_endpoint import match_endpoint


def test_batch_config_exposes_request_schema():
    assert OpenAIConfigs.BATCH.endpoint_path == "batches"
    assert OpenAIConfigs.BATCH.options is OpenAIBatchRequest


def test_batch_endpoint_is_discoverable_by_path_and_alias():
    assert isinstance(match_endpoint("openai", "batches"), OpenaiBatchEndpoint)
    assert isinstance(match_endpoint("openai", "batch"), OpenaiBatchEndpoint)


def test_batch_payload_keeps_only_api_fields():
    endpoint = OpenaiBatchEndpoint()
    payload, headers = endpoint.create_payload(
        {
            "input_file_id": "file-input",
            "endpoint": "/v1/responses",
            "completion_window": "24h",
            "metadata": {"job": "nightly"},
            "internal_option": "drop-me",
        }
    )

    assert payload == {
        "input_file_id": "file-input",
        "endpoint": "/v1/responses",
        "completion_window": "24h",
        "metadata": {"job": "nightly"},
    }
    assert headers["Content-Type"] == "application/json"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("endpoint", "/v1/unknown"),
        ("completion_window", "1h"),
    ],
)
def test_batch_request_rejects_unsupported_contract_values(field, value):
    request = {
        "input_file_id": "file-input",
        "endpoint": "/v1/responses",
        "completion_window": "24h",
    }
    request[field] = value

    with pytest.raises(ValueError, match="Invalid payload"):
        OpenaiBatchEndpoint().create_payload(request)
