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


class Step:
    """Factory methods for common Operative patterns.

    Provides methods to create Operative instances with pre-configured
    field specifications for common patterns like ReAct, QA, and task execution.
    """

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
        inherit_base: bool = True,
        config_dict: dict | None = None,
        doc: str | None = None,
        frozen: bool = False,
        new_model_name: str | None = None,
        parameter_fields: dict | None = None,
        request_params: dict | None = None,
        **kwargs,
    ) -> Operative:
        """Create request-configured Operative with common field patterns.

        Args:
            name: Operative name
            operative_name: (Deprecated) Use 'name' instead
            adapter: Validation framework
            reason: Add reasoning trace field
            actions: Add action request/response fields
            fields: Additional custom field specs (dict[str, Spec])
            field_models: (Deprecated) Use 'fields' instead - list of FieldModel/Spec
            max_retries: Max validation retries
            auto_retry_parse: Auto-retry on parse failure
            base_type: Base Pydantic model to extend
            parse_kwargs: (Deprecated) Ignored - parse config handled internally
            exclude_fields: (Deprecated) Ignored
            field_descriptions: (Deprecated) Ignored
            inherit_base: (Deprecated) Ignored
            config_dict: (Deprecated) Ignored
            doc: (Deprecated) Ignored
            frozen: (Deprecated) Ignored
            new_model_name: (Deprecated) Ignored
            parameter_fields: (Deprecated) Ignored
            request_params: (Deprecated) Ignored

        Returns:
            Configured Operative instance
        """
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
                    f"{_pname} is deprecated and will be removed in v0.21.0",
                    DeprecationWarning,
                    stacklevel=2,
                )
        if not inherit_base:
            warnings.warn(
                "inherit_base is deprecated and will be removed in v0.21.0",
                DeprecationWarning,
                stacklevel=2,
            )
        if frozen:
            warnings.warn(
                "frozen is deprecated and will be removed in v0.21.0",
                DeprecationWarning,
                stacklevel=2,
            )

        # Handle backward compatibility
        name = name or operative_name

        # Convert field_models list to fields dict if provided
        if field_models and not fields:
            from lionagi.models import FieldModel

            fields = {}
            for fm in field_models:
                # Convert FieldModel to Spec if needed
                if isinstance(fm, FieldModel):
                    spec = fm.to_spec()
                elif isinstance(fm, Spec):
                    spec = fm
                else:
                    continue  # Skip invalid types

                # Use spec name as key
                if spec.name:
                    fields[spec.name] = spec

        # Build fields dict to avoid duplicates (dict preserves insertion order in Python 3.7+)
        fields_dict = {}

        # Add common fields (convert FieldModel to Spec)
        if reason:
            reason_spec = get_default_field("reason").to_spec()
            fields_dict["reason"] = reason_spec

        if actions:
            fields_dict["action_required"] = get_default_field("action_required").to_spec()
            fields_dict["action_requests"] = get_default_field("action_requests").to_spec()
            fields_dict["action_responses"] = get_default_field("action_responses").to_spec()

        # Add custom fields (will override defaults if same name)
        if fields:
            for field_name, spec in fields.items():
                # Ensure spec has name
                if not spec.name:
                    # Update spec with name using Spec metadata update
                    spec = Spec(
                        spec.base_type,
                        name=field_name,
                        metadata=spec.metadata,
                    )
                fields_dict[spec.name] = spec

        # Convert to list
        all_fields = list(fields_dict.values())

        # Create Operable with all fields
        operable = Operable(
            tuple(all_fields),
            name=name or (base_type.__name__ if base_type else "Operative"),
        )

        # Request excludes action_responses
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
        """Create response type from operative.

        Args:
            operative: Source operative with all fields
            additional_fields: Extra fields for response

        Returns:
            Operative with response type configured
        """
        # If additional fields provided, create new Operative
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
