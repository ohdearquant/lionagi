# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import logging
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined
from typing_extensions import Self, override

from lionagi.utils import UNDEFINED, is_same_dtype

from .field_model import FieldModel
from .hashable_model import HashableModel

logger = logging.getLogger(__name__)
from .model_params import ModelParams  # noqa: E402

FieldName = TypeVar("FieldName", bound=str)


__all__ = ("OperableModel",)


class OperableModel(HashableModel):
    """Base model supporting dynamic field management and operations.

    A Pydantic model extension that enables runtime field modifications while
    maintaining type safety and validation. Supports dynamic field addition,
    updates, and removal, with full serialization capabilities.

    Args:
        **kwargs: Key-value pairs for initial field values.

    Attributes:
        extra_fields: Dictionary mapping field names to their FieldInfo
            definitions for dynamically added fields.
        extra_field_models: Dictionary mapping field names to their FieldModel
            instances for fields with additional configuration.

    Examples:
        >>> class UserModel(OperableModel):
        ...     name: str = "default"
        ...
        >>> user = UserModel()
        >>> user.add_field("age", value=25, annotation=int)
        >>> user.age
        25
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_default=False,
        populate_by_name=True,
        arbitrary_types_allowed=True,
        use_enum_values=True,
    )

    extra_fields: dict[str, FieldInfo] | Any = Field(
        default_factory=dict,
        description="Dictionary of dynamically added field definitions",
        exclude=True,
    )

    extra_field_models: dict[str, FieldModel] = Field(
        default_factory=dict,
        description="Dictionary of field models for dynamic fields",
        exclude=True,
    )

    def _serialize_extra_fields(
        self,
        value: dict[str, FieldInfo],
    ) -> dict[str, Any]:
        """Serialize extra fields to dictionary format.

        Args:
            value: Dictionary mapping field names to their FieldInfo objects.

        Returns:
            dict[str, Any]: Serialized field values.
        """
        output_dict = {}
        for k in value.keys():
            k_value = self.__dict__.get(k)
            if hasattr(k_value, "to_dict"):
                k_value = k_value.to_dict()
            elif hasattr(k_value, "model_dump"):
                k_value = k_value.model_dump()
            output_dict[k] = k_value
        return output_dict

    @field_validator("extra_fields")
    def _validate_extra_fields(
        cls,
        value: list[FieldModel] | dict[str, FieldModel | FieldInfo],
    ) -> dict[str, FieldInfo]:
        """Validate and normalize extra field definitions.

        Args:
            value: Field definitions as list or dictionary.

        Returns:
            dict[str, FieldInfo]: Normalized field definitions.

        Raises:
            ValueError: If value format is invalid.
        """
        out = {}
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(v, FieldModel):
                    out[k] = v.create_field()
                elif isinstance(v, FieldInfo):
                    out[k] = v
            return out

        elif isinstance(value, list) and is_same_dtype(value, FieldModel):
            return {v.name: v.create_field() for v in value}

        raise ValueError("Invalid extra_fields value")

    @model_validator(mode="after")
    def _validate_extra_field_models(self) -> Self:
        """Validate and normalize extra field models after initial validation.

        Returns:
            Self: Validated instance with normalized extra fields.
        """
        extra_fields = {}
        extra_field_models = {}

        # Handle dict[str, FieldInfo | FieldModel]
        if isinstance(self.extra_fields, dict):
            for k, v in self.extra_fields.items():
                if isinstance(v, FieldModel):
                    extra_fields[k] = v.create_field()
                    extra_field_models[k] = v
                elif isinstance(v, FieldInfo):
                    extra_fields[k] = v

        # Handle list input
        elif isinstance(self.extra_fields, list):
            for v in self.extra_fields:
                # list[FieldModel]
                if isinstance(v, FieldModel):
                    extra_fields[v.name] = v.create_field()
                    extra_field_models[v.name] = v

                # Handle list[tuple[str, FieldInfo | FieldModel]]
                if isinstance(v, tuple) and len(v) == 2:
                    if isinstance(v[0], str):
                        if isinstance(v[1], FieldInfo):
                            extra_fields[v[0]] = v[1]
                        if isinstance(v[1], FieldModel):
                            extra_fields[v[1].name] = v[1].create_field()
                            extra_field_models[v[1].name] = v[1]

        object.__setattr__(self, "extra_fields", extra_fields)
        object.__setattr__(self, "extra_field_models", extra_field_models)
        return self

    @override
    def __getattr__(self, field_name: str) -> Any:
        """Get attribute value with metadata tracking.

        Args:
            field_name: Name of the field to get

        Returns:
            Field value

        Raises:
            AttributeError: If field not found
        """
        if field_name == "extra_field" or field_name in self.all_fields:
            return self.__dict__.get(field_name, UNDEFINED)
        raise AttributeError(f"Field {field_name} not found in object fields.")

    @override
    def __setattr__(self, field_name: str, value: Any) -> None:
        """Set attribute value with metadata tracking.

        This method prevents direct assignment to metadata and extra_fields,
        and tracks the last update time of modified fields.

        Args:
            field_name: Name of the field to set
            value: Value to set

        Raises:
            AttributeError: If attempting to directly assign to metadata or
                extra_fields
        """
        if not callable(value) and field_name.startswith("__"):
            raise AttributeError("Cannot directly assign to dunder fields")

        if (
            field_name in self.extra_field_models
            and self.extra_field_models[field_name].has_validator()
        ):
            # Use the validate method to check value - let validation errors propagate
            self.extra_field_models[field_name].validate(value, field_name)
        if field_name in self.extra_fields:
            object.__setattr__(self, field_name, value)
        else:
            super().__setattr__(field_name, value)

    def __delattr__(self, field_name):
        """Delete an attribute from the model.

        For extra fields, attempts to reset to default value if one exists,
        otherwise removes the field completely. For regular fields, delegates
        to parent class deletion behavior.

        Args:
            field_name: Name of the field to delete.
        """
        if field_name in self.extra_fields:
            if self.extra_fields[field_name].default not in [
                UNDEFINED,
                PydanticUndefined,
            ]:
                setattr(self, field_name, self.extra_fields[field_name].default)
                return
            if self.extra_fields[field_name].default_factory is not UNDEFINED:
                setattr(
                    self,
                    field_name,
                    self.extra_fields[field_name].default_factory(),
                )
                return

        super().__delattr__(field_name)

    @override
    def to_dict(self) -> dict:
        """Convert model to dictionary including extra fields.

        Args:
            clean: If True, exclude UNDEFINED values

        Returns:
            Dictionary containing all fields and their values
        """
        dict_ = self.model_dump()
        dict_.update(self._serialize_extra_fields(self.extra_fields))
        logger.debug("OperableModel.to_dict(): %s", dict_)
        return {k: v for k, v in dict_.items() if v is not UNDEFINED}

    @property
    def all_fields(self) -> dict[str, FieldInfo]:
        """Get all fields including model fields and extra fields.

        Returns:
            Dictionary mapping field names to FieldInfo objects,
            excluding the extra_fields field itself
        """
        a = {**type(self).model_fields, **self.extra_fields}
        a.pop("extra_fields", None)
        a.pop("extra_field_models", None)  # Exclude internal field tracking
        return a

    def add_field(
        self,
        field_name: FieldName,
        /,
        value: Any = UNDEFINED,
        annotation: type = UNDEFINED,
        field_obj: FieldInfo = UNDEFINED,
        field_model: FieldModel = UNDEFINED,
        **kwargs,
    ) -> None:
        """Add a new field to the model's extra fields.

        Args:
            field_name: Name of the field to add
            value: Field value
            annotation: Type annotation
            field_obj: Pre-configured FieldInfo object
            field_model: Pre-configured FieldModel object
            **kwargs: Additional field configuration

        Raises:
            ValueError: If field already exists or invalid configuration
        """
        if field_name in self.all_fields:
            raise ValueError(f"Field '{field_name}' already exists")

        self.update_field(
            field_name,
            value=value,
            annotation=annotation,
            field_obj=field_obj,
            field_model=field_model,
            **kwargs,
        )

    def update_field(
        self,
        field_name: FieldName,
        /,
        value: Any = UNDEFINED,
        annotation: type = UNDEFINED,
        field_obj: FieldInfo = UNDEFINED,
        field_model: FieldModel = UNDEFINED,
        **kwargs,
    ) -> None:
        """Update existing field or create new one.

        Args:
            field_name: Name of field to update
            value: New field value
            annotation: Type annotation
            field_obj: Pre-configured FieldInfo object
            field_model: Pre-configured FieldModel object
            **kwargs: Additional field configuration

        Raises:
            ValueError: If invalid configuration provided
        """
        if "default" in kwargs and "default_factory" in kwargs:
            raise ValueError(
                "Cannot provide both 'default' and 'default_factory'",
            )

        if field_obj and field_model:
            raise ValueError(
                "Cannot provide both 'field_obj' and 'field_model'",
            )

        # Handle field_obj
        if field_obj:
            if not isinstance(field_obj, FieldInfo):
                raise ValueError("Invalid field_obj, should be a pydantic FieldInfo object")
            self.extra_fields[field_name] = field_obj

        if field_model:
            if not isinstance(field_model, FieldModel):
                raise ValueError("Invalid field_model, should be a FieldModel object")
            self.extra_fields[field_name] = field_model.create_field()
            self.extra_field_models[field_name] = field_model

        # Handle kwargs
        if kwargs:
            if field_name in self.all_fields:  # existing field
                for k, v in kwargs.items():
                    self.field_setattr(field_name, k, v)
            else:
                _kwargs = {
                    k: v
                    for k, v in kwargs.items()
                    if k not in ("name", "annotation", "validator_kwargs")
                }
                self.extra_fields[field_name] = Field(**_kwargs)

        # Handle no explicit defined field
        if not field_obj and not kwargs:
            if field_name not in self.all_fields:
                self.extra_fields[field_name] = Field()

        field_obj = self.extra_fields[field_name]

        # Handle annotation
        if annotation is not None:
            field_obj.annotation = annotation
        if not field_obj.annotation:
            field_obj.annotation = Any

        # Handle value
        if value is UNDEFINED:
            if field_name in self.all_fields:
                if self.__dict__.get(field_name, UNDEFINED) is not UNDEFINED:
                    value = self.__dict__.get(field_name)
            if getattr(self, field_name, UNDEFINED) is not UNDEFINED:
                value = getattr(self, field_name)
            elif field_obj.default is not PydanticUndefined:
                value = field_obj.default
            elif field_obj.default_factory:
                value = field_obj.default_factory()

        setattr(self, field_name, value)

    def remove_field(self, field_name: FieldName, /):
        if field_name in self.extra_fields:
            del self.extra_fields[field_name]
        if field_name in self.__dict__:
            del self.__dict__[field_name]

    def field_setattr(
        self,
        field_name: FieldName,
        attr: str,
        value: Any,
        /,
    ) -> None:
        """Set attribute value for a field.

        Args:
            field_name: Name of field to modify
            attr: Name of attribute to set
            value: Value to set

        Raises:
            KeyError: If field not found
        """
        all_fields = self.all_fields
        if field_name not in all_fields:
            raise KeyError(f"Field {field_name} not found in object fields.")
        field_obj = all_fields[field_name]
        if hasattr(field_obj, attr):
            setattr(field_obj, attr, value)
        else:
            if not isinstance(field_obj.json_schema_extra, dict):
                field_obj.json_schema_extra = {}
            field_obj.json_schema_extra[attr] = value

    def field_hasattr(
        self,
        field_name: FieldName,
        attr: str,
        /,
    ) -> bool:
        """Check if field has specific attribute.

        Args:
            field_name: Name of field to check
            attr: Name of attribute to check

        Returns:
            True if attribute exists, False otherwise

        Raises:
            KeyError: If field not found
        """
        all_fields = self.all_fields
        if field_name not in all_fields:
            raise KeyError(f"Field {field_name} not found in object fields.")
        field_obj = all_fields[field_name]
        if hasattr(field_obj, attr):
            return True
        elif isinstance(field_obj.json_schema_extra, dict):
            if field_name in field_obj.json_schema_extra:
                return True
        else:
            return False

    def field_getattr(
        self,
        field_name: FieldName,
        attr: str,
        default: Any = UNDEFINED,
        /,
    ) -> Any:
        """Get attribute value for a field.

        Args:
            field_name: Name of field to access
            attr: Name of attribute to get
            default: Default value if attribute not found

        Returns:
            Attribute value

        Raises:
            KeyError: If field not found
            AttributeError: If attribute not found and no default
        """
        all_fields = self.all_fields

        if field_name not in all_fields:
            raise KeyError(f"Field {field_name} not found in object fields.")

        if str(attr).strip("s").lower() == "annotation":
            return type(self).model_fields[field_name].annotation

        field_obj = all_fields[field_name]

        # Check fieldinfo attr
        value = getattr(field_obj, attr, UNDEFINED)
        if value is not UNDEFINED:
            return value
        else:
            if isinstance(field_obj.json_schema_extra, dict):
                value = field_obj.json_schema_extra.get(attr, UNDEFINED)
                if value is not UNDEFINED:
                    return value

        # Handle undefined attr
        if default is not UNDEFINED:
            return default
        else:
            raise AttributeError(
                f"field {field_name} has no attribute {attr}",
            )

    def new_model(
        self,
        name: str | None = None,
        use_fields: set[str] | None = None,  # default is all_fields
        base_type: type[BaseModel] | None = None,  # default is BaseModel
        exclude_fields: list = None,
        inherit_base: bool = True,
        config_dict: ConfigDict | dict | None = None,
        doc: str | None = None,
        frozen: bool = False,
        update_forward_refs: bool = True,
    ) -> type[BaseModel]:
        """Create a new Pydantic model type from the current model's fields.

        Generates a new model class using the specified fields and configuration.
        The new model can inherit from a base type and include a subset of fields
        from the current model.

        Args:
            name: Name for the new model class. If None, a default name will be
                generated.
            use_fields: Set of field names to include in the new model. If None,
                includes all fields.
            base_type: Base model class to inherit from. Defaults to BaseModel
                if None.
            exclude_fields: List of field names to exclude from the new model.
            inherit_base: Whether to inherit fields from the base_type.
                Defaults to True.
            config_dict: Pydantic model configuration for the new model.
            doc: Docstring for the new model class.
            frozen: If True, creates an immutable model where fields cannot be
                modified after creation. Defaults to False.
            update_forward_refs: Whether to attempt updating forward references
                in the new model. Defaults to True.

        Returns:
            A new Pydantic model class with the specified configuration.

        Raises:
            ValueError: If use_fields contains invalid field names.

        Examples:
            >>> model = OperableModel()
            >>> model.add_field("name", value="test", annotation=str)
            >>> model.add_field("age", value=25, annotation=int)
            >>> NewModel = model.new_model(
            ...     name="UserModel",
            ...     use_fields={"name", "age"},
            ...     frozen=True
            ... )
            >>> user = NewModel(name="Alice", age=30)
        """

        use_fields = set(use_fields) if use_fields else set(self.all_fields.keys())
        if not use_fields.issubset(self.all_fields.keys()):
            raise ValueError("Invalid field names in use_fields")

        field_models = []
        parameter_fields = {}

        for field_name in use_fields:
            if field_name in self.extra_field_models:
                field_models.append(self.extra_field_models[field_name])
            elif field_name in self.all_fields:
                parameter_fields[field_name] = self.all_fields[field_name]

        model_params = ModelParams(
            name=name,
            parameter_fields=parameter_fields,
            base_type=base_type,
            field_models=field_models,
            exclude_fields=exclude_fields,
            inherit_base=inherit_base,
            config_dict=config_dict,
            doc=doc,
            frozen=frozen,
        )
        model_cls = model_params.create_new_model()

        # Update forward references if requested
        if update_forward_refs:
            try:
                model_cls.model_rebuild()
            except Exception:  # noqa: S110
                pass  # Ignore rebuild errors for forward refs that can't be resolved yet

        return model_cls
