# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from typing_extensions import Self

from lionagi.ln.concurrency import is_coro_func

from ..generic.event import Event
from .tool import Tool


class FunctionCalling(Event):
    """Executes a Tool's callable asynchronously with optional pre/postprocessing."""

    func_tool: Tool = Field(
        ...,
        description="Tool instance containing the function to be called",
        exclude=True,
    )

    arguments: dict[str, Any] | BaseModel = Field(
        ..., description="Dictionary of arguments to pass to the function"
    )

    @field_validator("arguments", mode="before")
    def _validate_argument(cls, value):
        if isinstance(value, BaseModel):
            return value.model_dump(exclude_unset=True)
        return value

    @model_validator(mode="after")
    def _validate_strict_tool(self) -> Self:
        if self.func_tool.request_options:
            args: BaseModel = self.func_tool.request_options(**self.arguments)
            self.arguments = args.model_dump(exclude_unset=True)

        if self.func_tool.strict_func_call is True:
            if not set(self.arguments.keys()) == self.func_tool.required_fields:
                raise ValueError("arguments must match the function schema")

        else:
            if not self.func_tool.minimum_acceptable_fields.issubset(set(self.arguments.keys())):
                raise ValueError("arguments must match the function schema")
        return self

    @property
    def function(self):
        return self.func_tool.function

    async def _invoke(self) -> Any:
        """Execute the tool callable with pre/postprocessors; called by Event.invoke()."""

        if self.func_tool.preprocessor:
            if is_coro_func(self.func_tool.preprocessor):
                self.arguments = await self.func_tool.preprocessor(
                    self.arguments, **self.func_tool.preprocessor_kwargs
                )
            else:
                self.arguments = self.func_tool.preprocessor(
                    self.arguments, **self.func_tool.preprocessor_kwargs
                )

        # Re-validate after any pre-stage rewrite (hook layer or preprocessor
        # above) so a rewrite can never bypass the tool's declared schema.
        # Keys outside the schema (e.g. an audit marker a preprocessor adds)
        # are not covered by that validation -- pydantic's default
        # extra="ignore" would otherwise drop them from model_dump, so they
        # are carried through untouched rather than silently discarded.
        #
        # "Outside the schema" must be judged against the model's declared
        # input names (field names + aliases), not against the *serialized*
        # validated dump: a declared field that is aliased and left unset
        # (e.g. `Field(default=0, validation_alias="a_alias")`) is absent
        # from `model_dump(exclude_unset=True)` even though it is a real,
        # schema-covered field. Classifying it as "extra" would let a
        # preprocessor set it by name and forward the raw, unvalidated
        # value straight to the callable -- a schema bypass.
        if self.func_tool.request_options:
            try:
                validated = self.func_tool.request_options(**self.arguments)
            except Exception as e:
                raise PermissionError(
                    f"rewritten arguments failed validation for {self.func_tool.function!r}: {e}"
                ) from e
            validated_args = validated.model_dump(exclude_unset=True)
            declared_keys: set[str] = set()
            for field_name, field_info in self.func_tool.request_options.model_fields.items():
                declared_keys.add(field_name)
                if isinstance(field_info.alias, str):
                    declared_keys.add(field_info.alias)
                validation_alias = field_info.validation_alias
                if isinstance(validation_alias, str):
                    declared_keys.add(validation_alias)
                else:
                    # AliasChoices/AliasPath: collect any plain string
                    # choices so a key reachable via those still counts
                    # as schema-covered.
                    for choice in getattr(validation_alias, "choices", None) or ():
                        if isinstance(choice, str):
                            declared_keys.add(choice)
            extra_args = {k: v for k, v in self.arguments.items() if k not in declared_keys}
            self.arguments = {**validated_args, **extra_args}

        if is_coro_func(self.func_tool.func_callable):
            response = await self.func_tool.func_callable(**self.arguments)
        else:
            response = self.func_tool.func_callable(**self.arguments)

        if self.func_tool.postprocessor:
            if is_coro_func(self.func_tool.postprocessor):
                response = await self.func_tool.postprocessor(
                    response, **self.func_tool.postprocessor_kwargs
                )
            else:
                response = self.func_tool.postprocessor(
                    response, **self.func_tool.postprocessor_kwargs
                )
        return response

    def to_dict(self, *args, **kw) -> dict[str, Any]:
        """Serialize to dict, adding function name and arguments."""
        dict_ = super().to_dict(*args, **kw)
        dict_["function"] = self.function
        dict_["arguments"] = self.arguments
        return dict_
