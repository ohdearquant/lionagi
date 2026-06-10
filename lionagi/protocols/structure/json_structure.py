# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

# Deprecated: use lionagi.operations.schema.json_structure instead.
from typing import Any

import orjson
from pydantic import BaseModel

from lionagi.ln import extract_json, fuzzy_validate_mapping, to_list
from lionagi.ln.fuzzy import FuzzyMatchKeysParams
from lionagi.operations.schema.json_structure import (
    _DEFAULT_FUZZY,
    JsonStructure,
    logger,
)
from lionagi.operations.schema.structure import Structure

__all__ = (
    "Any",
    "BaseModel",
    "FuzzyMatchKeysParams",
    "JsonStructure",
    "Structure",
    "_DEFAULT_FUZZY",
    "extract_json",
    "fuzzy_validate_mapping",
    "logger",
    "orjson",
    "to_list",
)
