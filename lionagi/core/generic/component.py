"""This module provides base components used in lionagi"""

from abc import ABC
from functools import singledispatchmethod
from typing import Any, TypeVar

from pydantic import AliasChoices, BaseModel, Field, ValidationError
from pandas import DataFrame, Series

from lionagi.libs import SysUtil, func_call, convert, ParseUtil, nested


T = TypeVar("T")


class BaseComponent(BaseModel, ABC):
    """
    Base class for creating component models.

    Attributes:
        id_ (str): A 32-char unique hash identifier for the node.
        timestamp (str): The timestamp of when the node was created.
    """

    id_: str = Field(
        title="ID",
        default_factory=SysUtil.create_id,
        validation_alias=AliasChoices("node_id", "ID", "id"),
        description="A 32-char unique hash identifier for the node.",
    )

    timestamp: str = Field(
        default_factory=lambda: SysUtil.get_timestamp(sep=None),
        description="The timestamp of when the node was created.",
    )

    class Config:
        """Model configuration settings."""

        extra = "allow"
        arbitrary_types_allowed = True
        populate_by_name = True
        validate_assignment = True
        validate_return = True
        str_strip_whitespace = True

    @classmethod
    def class_name(cls) -> str:
        """
        Retrieve the name of the class.

        Returns:
            str: The name of the class.
        """
        return cls.__name__

    @property
    def _field_annotations(self) -> dict:
        """
        Return the annotations for each field in the model.

        Returns:
            dict: A dictionary mapping field names to their annotations.
        """
        return self._get_field_annotation(list(self.model_fields.keys()))

    def _get_field_attr(self, k: str, attr: str, default=False) -> Any:
        """
        Get the value of a field attribute.

        Args:
            k (str): The field name.
            attr (str): The attribute name.
            default (Any): Default value to return if the attribute is not
                found.

        Returns:
            Any: The value of the field attribute, or the default value if not
                found.

        Raises:
            ValueError: If the field does not have the specified attribute.
        """
        try:
            if not self._field_has_attr(k, attr):
                raise ValueError(f"field {k} has no attribute {attr}")
            field = self.model_fields[k]
            a = getattr(field, attr)
            if not a:
                try:
                    a = field.json_schema_extra[attr]
                    return a
                except Exception:
                    return None
            return a
        except Exception as e:
            if default is not False:
                return default
            raise e

    @singledispatchmethod
    def _get_field_annotation(self, field_name: str) -> Any:
        """
        Get the annotation for a field.

        Args:
            field_name (str): The name of the field.

        Raises:
            TypeError: If the field_name is of an unsupported type.
        """
        raise TypeError(f"Unsupported type {type(field_name)}")

    @_get_field_annotation.register(str)
    def _(self, field_name) -> dict[str, Any]:
        """
        Get the annotation for a field as a dictionary.

        Args:
            field_name (str): The name of the field.

        Returns:
            dict[str, Any]: A dictionary mapping the field name to its
                annotation.
        """
        dict_ = {field_name: self.model_fields[field_name].annotation}
        for k, v in dict_.items():
            if "|" in str(v):
                v = str(v)
                v = v.split("|")
                dict_[k] = func_call.lcall(v, convert.strip_lower)
            else:
                dict_[k] = [v.__name__]
        return dict_

    @_get_field_annotation.register(list)
    def _(self, field_name) -> dict[str, Any]:
        """
        Get the annotations for multiple fields as a dictionary.

        Args:
            field_name (list): A list or tuple of field names.

        Returns:
            dict[str, Any]: A dictionary mapping field names to their
                annotations.
        """
        dict_ = {}
        for i in field_name:
            dict_.update(self._get_field_annotation(i))
        return dict_

    @_get_field_annotation.register(tuple)
    def _(self, field_name) -> dict[str, Any]:
        """
        Get the annotations for multiple fields as a dictionary.

        Args:
            field_name (tuple): A list or tuple of field names.

        Returns:
            dict[str, Any]: A dictionary mapping field names to their
                annotations.
        """
        dict_ = {}
        for i in field_name:
            dict_.update(self._get_field_annotation(i))
        return dict_

    def _field_has_attr(self, k: str, attr: str) -> bool:
        """
        Check if a field has a specific attribute.

        Args:
            k (str): The field name.
            attr (str): The attribute name.

        Returns:
            bool: True if the field has the attribute, False otherwise.
        """
        field = self.model_fields.get(k, None)
        if field is None:
            raise ValueError(f"Field {k} not found in model fields.")

        a = attr in str(field)
        if not a:
            try:
                a = (
                    self.model_fields[k].json_schema_extra[attr] is not None
                    and attr in self.model_fields[k].json_schema_extra
                )
                return a if isinstance(a, bool) else False
            except Exception:
                return False
        return a

    def to_json_str(self, *args, **kwargs) -> str:
        """
        Convert the component to a JSON string.

        Returns:
            str: The JSON string representation of the component.
        """
        return self.model_dump_json(*args, by_alias=True, **kwargs)

    def to_dict(self, *args, **kwargs) -> dict[str, Any]:
        """
        Convert the component to a dictionary.

        Returns:
            dict[str, Any]: The dictionary representation of the component.
        """
        return self.model_dump(*args, by_alias=True, **kwargs)

    def to_xml(self) -> str:
        """
        Convert the component to an XML string.

        Returns:
            str: The XML string representation of the component.
        """
        import xml.etree.ElementTree as ET

        root = ET.Element(self.__class__.__name__)

        def convert(dict_obj, parent):
            for key, val in dict_obj.items():
                if isinstance(val, dict):
                    element = ET.SubElement(parent, key)
                    convert(val, element)
                else:
                    element = ET.SubElement(parent, key)
                    element.text = str(val)

        convert(self.to_dict(), root)
        return ET.tostring(root, encoding="unicode")

    def to_pd_series(self, *args, pd_kwargs: dict | None = None, **kwargs) -> Series:
        """
        Convert the node to a Pandas Series.

        Returns:
            Series: The Pandas Series representation of the node.
        """
        pd_kwargs = {} if pd_kwargs is None else pd_kwargs
        dict_ = self.to_dict(*args, **kwargs)
        return Series(dict_, **pd_kwargs)


class BaseNode(BaseComponent):
    """
    Base class for creating node models.

    Attributes:
        content (Any | None): The optional content of the node.
        metadata (dict[str, Any]): Additional metadata for the node.
    """

    content: Any | None = Field(
        default=None,
        validation_alias=AliasChoices("text", "page_content", "chunk_content", "data"),
        description="The optional content of the node.",
    )

    metadata: dict[str, Any] = Field(
        default_factory=dict,
        alias="meta",
        description="Additional metadata for the node.",
    )

    @singledispatchmethod
    @classmethod
    def from_obj(cls, obj: Any, *args, **kwargs) -> T:
        """
        Create a node instance from an object.

        Args:
            obj (Any): The object to create the node from.

        Raises:
            NotImplementedError: If the object type is not supported.
        """
        raise NotImplementedError(f"Unsupported type: {type(obj)}")

    @from_obj.register(dict)
    @classmethod
    def _from_dict(cls, obj, *args, **kwargs) -> T:
        """
        Create a node instance from a dictionary.

        Args:
            obj (dict): The dictionary to create the node from.

        Returns:
            T: The created node instance.
        """
        return cls.model_validate(obj, *args, **kwargs)

    @from_obj.register(str)
    @classmethod
    def _from_str(cls, obj, *args, fuzzy_parse=False, **kwargs) -> T:
        """
        Create a node instance from a JSON string.

        Args:
            obj (str): The JSON string to create the node from.
            fuzzy_parse (bool): Whether to perform fuzzy parsing.

        Returns:
            T: The created node instance.
        """
        obj = ParseUtil.fuzzy_parse_json(obj) if fuzzy_parse else convert.to_dict(obj)
        try:
            return cls.from_obj(obj, *args, **kwargs)
        except ValidationError as e:
            raise ValueError(f"Invalid JSON for deserialization: {e}") from e

    @from_obj.register(list)
    @classmethod
    def _from_list(cls, obj, *args, **kwargs) -> list[T]:
        """
        Create a list of node instances from a list of objects.

        Args:
            obj (list): The list of objects to create nodes from.

        Returns:
            list[T]: The list of created node instances.
        """
        return [cls.from_obj(item, *args, **kwargs) for item in obj]

    @from_obj.register(Series)
    @classmethod
    def _from_pd_series(cls, obj, *args, pd_kwargs=None, **kwargs) -> T:
        """
        Create a node instance from a Pandas Series.

        Args:
            obj (Series): The Pandas Series to create the node from.

        Returns:
            T: The created node instance.
        """
        if pd_kwargs is None:
            pd_kwargs = {}
        return cls.from_obj(obj.to_dict(**pd_kwargs), *args, **kwargs)

    @from_obj.register(DataFrame)
    @classmethod
    def _from_pd_dataframe(cls, obj, *args, pd_kwargs=None, **kwargs) -> list[T]:
        """
        Create a list of node instances from a Pandas DataFrame.

        Args:
            obj (DataFrame): The Pandas DataFrame to create nodes from.

        Returns:
            list[T]: The list of created node instances.
        """
        if pd_kwargs is None:
            pd_kwargs = {}
        return [
            cls.from_obj(row, *args, **pd_kwargs, **kwargs) for _, row in obj.iterrows()
        ]

    @from_obj.register(BaseModel)
    @classmethod
    def _from_base_model(cls, obj, pydantic_kwargs=None, **kwargs) -> T:
        """
        Create a node instance from a Pydantic BaseModel.

        Args:
            obj (BaseModel): The Pydantic BaseModel to create the node from.

        Returns:
            T: The created node instance.
        """
        if pydantic_kwargs is None:
            pydantic_kwargs = {"by_alias": True}

        config_ = {}
        try:
            config_ = obj.model_dump(**pydantic_kwargs)
        except:
            config_ = obj.dict(**pydantic_kwargs)

        return cls.from_obj(config_ | kwargs)

    def meta_get(
        self, key: str, indices: list[str | int] = None, default: Any = None
    ) -> Any:
        """
        Get a value from the metadata dictionary.

        Args:
            key (str): The key to retrieve the value for.
            indices (list[str | int]): Optional list of indices for nested
                retrieval.
            default (Any): The default value to return if the key is not found.

        Returns:
            Any: The retrieved value or the default value if not found.
        """
        if indices:
            return nested.nget(self.metadata, indices, default)
        return self.metadata.get(key, default)

    def meta_change_key(self, old_key: str, new_key: str) -> bool:
        """
        Change a key in the metadata dictionary.

        Args:
            old_key (str): The old key to be changed.
            new_key (str): The new key to replace the old key.

        Returns:
            bool: True if the key was changed successfully, False otherwise.
        """
        if old_key in self.metadata:
            SysUtil.change_dict_key(self.metadata, old_key, new_key)
            return True
        return False

    def meta_insert(self, indices: str | list, value: Any, **kwargs) -> bool:
        """
        Insert a value into the metadata dictionary at the specified indices.

        Args:
            indices (str | list): The indices to insert the value at.
            value (Any): The value to be inserted.
            **kwargs: Additional keyword arguments for the `nested.ninsert`
                function.

        Returns:
            bool: True if the value was inserted successfully, False otherwise.
        """
        return nested.ninsert(self.metadata, indices, value, **kwargs)

    def meta_merge(
        self, additional_metadata: dict[str, Any], overwrite: bool = False, **kwargs
    ) -> None:
        """
        Merge additional metadata into the existing metadata dictionary.

        Args:
            additional_metadata (dict[str, Any]): The additional metadata to be
                merged.
            overwrite (bool): Whether to overwrite existing keys with the new
                values.
            **kwargs: Additional keyword arguments for the `nested.nmerge`
                function.
        """
        self.metadata = nested.nmerge(
            [self.metadata, additional_metadata], overwrite=overwrite, **kwargs
        )
