# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from lionagi.ln.types import Operable, Spec

__all__ = ("Structure",)


class Structure:
    """Composable schema builder for structured LLM interactions.

    Wraps an Operable with a base model or dict reference and convenience
    flags for actions/reason. Subclasses implement format-specific render/parse.

    Accepts either a BaseModel class or a dict as the response format:
    - BaseModel: full Pydantic model with validation, Operable composition
    - dict: raw key→type mapping for prompting and fuzzy-key parsing

    Immutable — each ``with_*`` returns a new instance.
    """

    __slots__ = (
        "_base",
        "_base_dict",
        "_operable",
        "_actions",
        "_reason",
        "_request_cls",
        "_response_cls",
        "_name",
    )

    def __init__(
        self,
        base: type[BaseModel] | dict[str, Any] | None = None,
        *,
        specs: list[Spec] | tuple[Spec, ...] | None = None,
        actions: bool = False,
        reason: bool = False,
        name: str | None = None,
    ):
        if isinstance(base, dict):
            self._base = None
            self._base_dict = base
        else:
            self._base = base
            self._base_dict = None

        self._actions = actions
        self._reason = reason
        self._name = name or (base.__name__ if isinstance(base, type) else "Structure")
        self._operable = self._build_operable(specs or ()) if self._base_dict is None else None
        self._request_cls: type[BaseModel] | None = None
        self._response_cls: type[BaseModel] | None = None

    @property
    def base(self) -> type[BaseModel] | None:
        return self._base

    @property
    def base_dict(self) -> dict[str, Any] | None:
        return self._base_dict

    @property
    def is_dict_mode(self) -> bool:
        return self._base_dict is not None

    @property
    def name(self) -> str:
        return self._name

    @property
    def operable(self) -> Operable | None:
        return self._operable

    # ------------------------------------------------------------------
    # Builder
    # ------------------------------------------------------------------

    def _clone(self, **overrides) -> Structure:
        base = overrides.pop("base", self._base_dict if self.is_dict_mode else self._base)
        kw = dict(
            base=base,
            actions=self._actions,
            reason=self._reason,
            name=self._name,
        )
        kw.update(overrides)
        if "specs" not in kw and not self.is_dict_mode:
            kw["specs"] = self._user_specs()
        return type(self)(**kw)

    def _user_specs(self) -> list[Spec]:
        if self._operable is None:
            return []
        auto_names = set()
        if self._reason:
            auto_names.add("reason")
        if self._actions:
            auto_names |= {"action_required", "action_requests", "action_responses"}
        return [s for s in self._operable.__op_fields__ if s.name not in auto_names]

    def with_specs(self, *specs: Spec) -> Structure:
        return self._clone(specs=[*self._user_specs(), *specs])

    def with_actions(self) -> Structure:
        return self._clone(actions=True)

    def with_reason(self) -> Structure:
        return self._clone(reason=True)

    # ------------------------------------------------------------------
    # Schema generation — delegates to Operable (model mode only)
    # ------------------------------------------------------------------

    def request_schema(self) -> type[BaseModel] | dict[str, Any]:
        """Pydantic model for model mode; dict for dict mode."""
        if self.is_dict_mode:
            return self._base_dict
        if self._request_cls is None:
            exclude = {"action_responses"} if self._actions else None
            self._request_cls = self._operable.create_model(
                adapter="pydantic",
                model_name=f"{self._name}Request",
                exclude=exclude,
                base_type=self._base,
            )
        return self._request_cls

    def response_schema(self) -> type[BaseModel] | dict[str, Any]:
        """Pydantic model for model mode; dict for dict mode."""
        if self.is_dict_mode:
            return self._base_dict
        if self._response_cls is None:
            self._response_cls = self._operable.create_model(
                adapter="pydantic",
                model_name=f"{self._name}Response",
                base_type=self.request_schema(),
            )
        return self._response_cls

    def to_format(self, parsed: BaseModel | dict) -> BaseModel | dict:
        """Extract clean base from a parsed response."""
        if self.is_dict_mode:
            if isinstance(parsed, dict):
                return {k: parsed.get(k) for k in self._base_dict}
            return parsed
        if self._base is None:
            return parsed
        base_fields = set(self._base.model_fields.keys())
        data = {k: v for k, v in parsed.model_dump(mode="python").items() if k in base_fields}
        return self._base.model_validate(data)

    # ------------------------------------------------------------------
    # Subclass interface
    # ------------------------------------------------------------------

    def render(self) -> str:
        raise NotImplementedError

    def parse(self, text: str, **kw) -> BaseModel | dict:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _build_operable(self, user_specs: list[Spec] | tuple[Spec, ...]) -> Operable:
        from lionagi.operations.fields import get_default_field

        all_specs: list[Spec] = []
        if self._reason:
            all_specs.append(get_default_field("reason").to_spec())
        if self._actions:
            all_specs.append(get_default_field("action_required").to_spec())
            all_specs.append(get_default_field("action_requests").to_spec())
            all_specs.append(get_default_field("action_responses").to_spec())
        all_specs.extend(user_specs)
        return Operable(tuple(all_specs), name=self._name)

    def __repr__(self) -> str:
        parts = [f"{type(self).__name__}({self._name}"]
        if self._base:
            parts.append(f", base={self._base.__name__}")
        if self._base_dict:
            parts.append(f", dict_keys={list(self._base_dict.keys())}")
        if self._operable and len(self._operable.__op_fields__):
            parts.append(f", fields={len(self._operable.__op_fields__)}")
        if self._actions:
            parts.append(", actions")
        if self._reason:
            parts.append(", reason")
        parts.append(")")
        return "".join(parts)
