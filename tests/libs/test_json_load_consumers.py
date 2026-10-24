# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Existing orjson consumers must keep rejecting non-finite source documents."""

from __future__ import annotations

import orjson
import pytest

from lionagi.libs.schema.minimal_yaml import minimal_yaml
from lionagi.models.hashable_model import HashableModel
from lionagi.protocols.generic.element import Element
from lionagi.protocols.graph.node import Node


class _FloatValues(HashableModel):
    values: list[float]


@pytest.mark.parametrize("consumer", ("hashable-model", "element", "minimal-yaml"))
@pytest.mark.parametrize("token", ("NaN", "Infinity", "-Infinity", "1e999"))
def test_native_json_consumers_keep_refusing_non_finite_documents(consumer, token):
    with pytest.raises(orjson.JSONDecodeError):
        if consumer == "hashable-model":
            _FloatValues.from_dict('{"values":[' + token + "]}", mode="json")
        elif consumer == "element":
            Element.from_json('{"metadata":{"value":' + token + "}}")
        else:
            minimal_yaml('{"value":' + token + "}")


@pytest.mark.parametrize("token", ("NaN", "Infinity", "-Infinity", "1e999"))
def test_node_embedding_keeps_its_invalid_string_error_for_non_finite_json(token):
    with pytest.raises(ValueError, match="Invalid embedding string"):
        Node(embedding="[" + token + "]")
