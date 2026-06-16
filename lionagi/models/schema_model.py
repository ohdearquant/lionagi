# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from pydantic import ConfigDict

from .hashable_model import HashableModel

__all__ = ("SchemaModel",)


class SchemaModel(HashableModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_default=False,
        populate_by_name=True,
        arbitrary_types_allowed=True,
        use_enum_values=True,
    )

    @classmethod
    def keys(cls) -> list[str]:
        """Return the list of field names defined in this model."""
        return list(cls.model_fields.keys())
