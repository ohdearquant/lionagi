# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

# Deprecated: lionagi.protocols.structure has moved to lionagi.operations.schema.
# These re-exports will be removed in a future release.

from lionagi.operations.schema.json_structure import JsonStructure
from lionagi.operations.schema.structure import Structure

__all__ = ("JsonStructure", "Structure")
