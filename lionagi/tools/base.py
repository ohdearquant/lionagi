# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from pydantic import BaseModel, Field, JsonValue, field_serializer, field_validator

from lionagi.ln.types import Enum
from lionagi.protocols.action.lion_tool import LionTool as LionTool
from lionagi.protocols.action.tool import Tool as Tool
from lionagi.protocols.graph.node import Node


class ResourceCategory(str, Enum):
    FRAMEWORK = "framework"
    TEMPLATE = "template"
    UTILITY = "utility"
    PROMPT = "prompt"
    OTHER = "other"


class ResourceMeta(BaseModel):
    title: str | None = None
    domain: str | None = None
    version: str = Field(default="0.1.0", frozen=True)
    overview: str | None = None

    @property
    def keys(self):
        return set(type(self).model_fields.keys())


class Resource(Node):
    content: JsonValue = None
    category: ResourceCategory = Field(default=ResourceCategory.OTHER, frozen=True)

    @property
    def meta_obj(self) -> ResourceMeta:
        return ResourceMeta.model_validate(self.metadata)

    @field_serializer("category")
    def _serialize_resource_category(self, value: ResourceCategory) -> str:
        if isinstance(value, ResourceCategory):
            return value.value
        return str(value)

    @field_validator("category", mode="before")
    def _validate_resource_category(cls, value: str) -> ResourceCategory:
        if isinstance(value, ResourceCategory):
            return value
        if isinstance(value, str):
            return ResourceCategory(value)
        raise ValueError(f"Invalid resource category: {str(value)}")


class Prompt(Resource):
    category: ResourceCategory = Field(default=ResourceCategory.PROMPT, frozen=True)
