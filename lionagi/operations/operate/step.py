# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Step factory methods for creating configured Operative instances."""

import warnings
from typing import TYPE_CHECKING, Literal

from lionagi.ln.types import Operable, Spec

from ..fields import get_default_field
from .operative import Operative

if TYPE_CHECKING:
    from pydantic import BaseModel


_DEFAULT_SPECS = {}


def _get_default_spec(kind: str):
    """Return the immutable Spec used by a standard Operative field."""
    if kind not in _DEFAULT_SPECS:
        _DEFAULT_SPECS[kind] = get_default_field(kind).to_spec()
    return _DEFAULT_SPECS[kind]


class Step:
    """Factory methods for creating pre-configured Operative instances (ReAct, QA, task execution)."""

    @staticmethod
    def request_operative(
        *,
        name: str | None = None,
        operative_name: str | None = None,  # backward compat
        adapter: Literal["pydantic"] = "pydantic",
        reason: bool = False,
        actions: bool = False,
        fields: dict[str, Spec] | None = None,
        field_models: list | None = None,  # backward compat
        max_retries: int = 3,
        auto_retry_parse: bool = True,
        base_type: type["BaseModel"] | None = None,
        # Deprecated/ignored parameters for backward compatibility
        parse_kwargs: dict | None = None,
        exclude_fields: list | None = None,
        field_descriptions: dict | None = None,
        config_dict: dict | None = None,
        doc: str | None = None,
        new_model_name: str | None = None,
        parameter_fields: dict | None = None,
        request_params: dict | None = None,
        **kwargs,
    ) -> Operative:
        """Build a request-phase Operative with optional reason/action fields; deprecated params are silently ignored."""
        from .._guards import reject_removed_kwargs

        reject_removed_kwargs(
            kwargs,
            {
                "inherit_base": "fields=/base_type=",
                "frozen": "",
            },
            where="Step.request_operative",
        )

        # Warn on deprecated parameters that are silently ignored
        _deprecated_ignored = {
            "parse_kwargs": parse_kwargs,
            "exclude_fields": exclude_fields,
            "field_descriptions": field_descriptions,
            "config_dict": config_dict,
            "doc": doc,
            "new_model_name": new_model_name,
            "parameter_fields": parameter_fields,
            "request_params": request_params,
        }
        for _pname, _pval in _deprecated_ignored.items():
            if _pval is not None:
                warnings.warn(
                    f"{_pname} is deprecated and will be removed in v0.29.0",
                    DeprecationWarning,
                    stacklevel=2,
                )
        # Handle backward compatibility
        name = name or operative_name

        if field_models and not fields:
            from lionagi.models import FieldModel

            fields = {}
            for fm in field_models:
                if isinstance(fm, FieldModel):
                    spec = fm.to_spec()
                elif isinstance(fm, Spec):
                    spec = fm
                else:
                    continue
                if spec.name:
                    fields[spec.name] = spec

        fields_dict = {}

        if reason:
            reason_spec = _get_default_spec("reason")
            fields_dict["reason"] = reason_spec

        if actions:
            fields_dict["action_required"] = _get_default_spec("action_required")
            fields_dict["action_requests"] = _get_default_spec("action_requests")
            fields_dict["action_responses"] = _get_default_spec("action_responses")

        if fields:
            for field_name, spec in fields.items():
                if not spec.name:
                    spec = Spec(
                        spec.base_type,
                        name=field_name,
                        metadata=spec.metadata,
                    )
                fields_dict[spec.name] = spec

        all_fields = list(fields_dict.values())

        operable = Operable(
            tuple(all_fields),
            name=name or (base_type.__name__ if base_type else "Operative"),
        )

        # action_responses excluded from request schema; included in response schema
        request_exclude = {"action_responses"} if actions else set()

        return Operative(
            name=name,
            adapter=adapter,
            max_retries=max_retries,
            auto_retry_parse=auto_retry_parse,
            base_type=base_type,
            operable=operable,
            request_exclude=request_exclude,
        )

    @staticmethod
    def respond_operative(
        operative: Operative,
        additional_fields: dict[str, Spec] | None = None,
    ) -> Operative:
        """Extend an operative with optional additional response fields and materialize its response model."""
        if additional_fields:
            # Get existing fields
            existing_fields = list(operative.operable.__op_fields__)

            # Add new fields
            for field_name, spec in additional_fields.items():
                if not spec.name:
                    spec = Spec(
                        spec.base_type,
                        name=field_name,
                        metadata=spec.metadata,
                    )
                existing_fields.append(spec)

            # Create new Operable
            new_operable = Operable(
                tuple(existing_fields),
                name=operative.name,
            )

            # Create new Operative
            return Operative(
                name=operative.name,
                adapter=operative.adapter,
                max_retries=operative.max_retries,
                auto_retry_parse=operative.auto_retry_parse,
                base_type=operative.base_type,
                operable=new_operable,
                request_exclude=operative.request_exclude,
            )

        # Otherwise just create response model
        operative.create_response_model()
        return operative


__all__ = ("Step",)
