# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

# Deprecated: use lionagi.operations.schema.structure instead.
# The extra names below mirror the old module's top-level namespace so that
# pre-relocation imports keep working; __all__ stays byte-equivalent to the
# old module (class-only) so star-import behavior is unchanged.
from __future__ import annotations

from typing import Any as Any

from pydantic import BaseModel as BaseModel

from lionagi.ln.types import Operable as Operable
from lionagi.ln.types import Spec as Spec
from lionagi.operations.schema.structure import Structure as Structure

__all__ = ("Structure",)
