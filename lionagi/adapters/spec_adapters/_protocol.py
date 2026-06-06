"""Abstract base class for Spec adapters.

Adapters convert framework-agnostic Spec objects to framework-specific
field and model definitions (Pydantic, msgspec, attrs, dataclasses).
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lionagi.ln.types import Operable, Spec

__all__ = ("SpecAdapter",)


class SpecAdapter(ABC):
    """Base adapter for converting Spec to framework-specific formats.

    Abstract Methods (must implement):
        - create_field: Spec → framework field
        - create_model: Operable → framework model class
        - validate_model: dict → validated model instance
        - dump_model: model instance → dict

    Concrete Methods (shared):
        - parse_json: Extract JSON from text
        - fuzzy_match_fields: Match dict keys to model fields
        - validate_response: Full validation pipeline
        - update_model: Update model instance (uses dump_model + validate_model)
    """

    # ---- Abstract Methods ----

    @classmethod
    @abstractmethod
    def create_field(cls, spec: "Spec") -> Any:
        """Convert Spec to framework-specific field definition.

        Args:
            spec: Spec object

        Returns:
            Framework-specific field (FieldInfo, Attribute, Field, etc.)
        """
        ...

    @classmethod
    @abstractmethod
    def create_model(
        cls,
        operable: "Operable",
        model_name: str,
        include: set[str] | None = None,
        exclude: set[str] | None = None,
        **kwargs: Any,
    ) -> type:
        """Generate model class from Operable.

        Args:
            operable: Operable containing specs
            model_name: Name for generated model
            include: Only include these field names
            exclude: Exclude these field names
            **kwargs: Framework-specific options

        Returns:
            Generated model class
        """
        ...

    @classmethod
    @abstractmethod
    def validate_model(cls, model_cls: type, data: dict) -> Any:
        """Validate dict data into model instance.

        Framework-agnostic validation hook. Each adapter implements
        the appropriate validation mechanism:
            - Pydantic: model_cls.model_validate(data)
            - msgspec: msgspec.convert(data, type=model_cls)
            - attrs: model_cls(**data)
            - dataclasses: model_cls(**data)

        Args:
            model_cls: Model class
            data: Dictionary data to validate

        Returns:
            Validated model instance
        """
        ...

    @classmethod
    @abstractmethod
    def dump_model(cls, instance: Any) -> dict:
        """Dump model instance to dictionary.

        Framework-agnostic serialization hook. Each adapter implements
        the appropriate serialization mechanism:
            - Pydantic: instance.model_dump()
            - msgspec: msgspec.to_builtins(instance)
            - attrs: attr.asdict(instance)
            - dataclasses: dataclasses.asdict(instance)

        Args:
            instance: Model instance

        Returns:
            Dictionary representation
        """
        ...

    @classmethod
    def create_validator(cls, spec: "Spec") -> Any:
        """Generate framework-specific validators from Spec metadata.

        Args:
            spec: Spec with validator metadata

        Returns:
            Framework-specific validator, or None if not supported
        """
        return None

    # ---- Concrete Methods (Shared) ----

    @classmethod
    def parse_json(cls, text: str, fuzzy: bool = True) -> dict | list | Any:
        """Extract and parse JSON from text.

        Args:
            text: Raw text potentially containing JSON
            fuzzy: Use fuzzy parsing (markdown extraction)

        Returns:
            Parsed JSON object
        """
        from lionagi.ln import extract_json

        data = extract_json(text, fuzzy_parse=fuzzy)

        # Unwrap single-item lists/tuples
        if isinstance(data, (list, tuple)) and len(data) == 1:
            data = data[0]

        return data

    @classmethod
    @abstractmethod
    def fuzzy_match_fields(cls, data: dict, model_cls: type, strict: bool = False) -> dict:
        """Match data keys to model fields with fuzzy matching.

        Framework-specific method - each adapter must implement based on how
        their framework exposes field definitions.

        Args:
            data: Raw data dictionary
            model_cls: Target model class
            strict: If True, raise on unmatched; if False, force coercion

        Returns:
            Dictionary with keys matched to model fields
        """
        ...

    @classmethod
    def validate_response(
        cls,
        text: str,
        model_cls: type,
        strict: bool = False,
        fuzzy_parse: bool = True,
    ) -> Any | None:
        """Validate and parse response text into model instance.

        Pipeline: parse_json → fuzzy_match_fields → validate_model

        Args:
            text: Raw response text
            model_cls: Target model class
            strict: If True, raise on errors; if False, return None
            fuzzy_parse: Use fuzzy JSON parsing

        Returns:
            Validated model instance, or None if validation fails (strict=False)
        """
        try:
            # Step 1: Parse JSON
            data = cls.parse_json(text, fuzzy=fuzzy_parse)

            # Step 2: Fuzzy match fields
            matched_data = cls.fuzzy_match_fields(data, model_cls, strict=strict)

            # Step 3: Validate with framework-specific method
            instance = cls.validate_model(model_cls, matched_data)

            return instance

        except (ValueError, TypeError, KeyError, AttributeError):
            # Catch validation-related exceptions only
            # ValueError: JSON/parsing errors, validation failures
            # TypeError: Type mismatches during validation
            # KeyError: Missing required fields
            # AttributeError: Field access errors
            if strict:
                raise
            return None

    @classmethod
    def update_model(
        cls,
        instance: Any,
        updates: dict,
        model_cls: type | None = None,
    ) -> Any:
        """Update existing model instance with new data.

        Args:
            instance: Existing model instance
            updates: Dictionary of updates
            model_cls: Optional model class (defaults to instance's class)

        Returns:
            New validated model instance with updates applied
        """
        model_cls = model_cls or type(instance)

        # Merge existing data with updates
        current_data = cls.dump_model(instance)
        current_data.update(updates)

        # Validate merged data
        return cls.validate_model(model_cls, current_data)
