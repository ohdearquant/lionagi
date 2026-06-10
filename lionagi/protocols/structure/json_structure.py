# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

# Deprecated: use lionagi.operations.schema.json_structure instead.
# The extra names below mirror the old module's top-level namespace so that
# pre-relocation imports keep working; __all__ stays byte-equivalent to the
# old module (class-only) so star-import behavior is unchanged.
from __future__ import annotations

import logging as logging
from typing import Any as Any

import orjson as orjson
from pydantic import BaseModel as BaseModel

from lionagi.ln import extract_json as extract_json
from lionagi.ln import fuzzy_validate_mapping as fuzzy_validate_mapping
from lionagi.ln import to_list as to_list
from lionagi.ln.fuzzy import FuzzyMatchKeysParams as FuzzyMatchKeysParams
from lionagi.operations.schema.json_structure import _DEFAULT_FUZZY as _DEFAULT_FUZZY
from lionagi.operations.schema.json_structure import JsonStructure as JsonStructure
from lionagi.operations.schema.json_structure import logger as logger
from lionagi.operations.schema.structure import Structure as Structure

__all__ = ("JsonStructure",)
