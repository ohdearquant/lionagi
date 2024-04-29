"""base components in lionagi"""
from abc import ABC
from functools import singledispatchmethod
from typing import Any, Type
from pydantic import AliasChoices, BaseModel, Field
from pandas import Series

from lionagi.libs import SysUtil, func_call, convert


class Component(BaseModel, ABC):
    """a distuinguishable temporal entity in lionagi"""

    id_: str = Field(
        title="ID",
        default_factory=SysUtil.create_id,
        validation_alias=AliasChoices("node_id", "ID", "id"),
        description="A 32-char unique hash identifier for the node.",
        frozen=True,
    )
    timestamp: str = Field(
        default_factory=lambda: SysUtil.get_timestamp(sep=None)[:-6],
        description="The utc timestamp of when the node was created.",
        frozen=True,
    )

    extra_fields: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices(
            "extra", "additional_fields", "schema_extra", "extra_schema"
        ),
        description="Additional fields for the component.",
    )

    class Config:
        """Model configuration settings."""

        extra = "allow"
        arbitrary_types_allowed = True
        populate_by_name = True


    @classmethod
    def class_name(cls) -> str:
        """
        Retrieve the name of the class.

        Returns:
            str: The name of the class.
        """
        return "lionagi."+cls.__name__

    def to_json_str(self, *args, **kwargs) -> str:
        """
        Convert the component to a JSON string.

        Returns:
            str: The JSON string representation of the component.
        """
        dict_ = self.to_dict(*args, **kwargs)
        return convert.to_str(dict_)

    def to_dict(self, *args, **kwargs) -> dict[str, Any]:
        """
        Convert the component to a dictionary.

        Returns:
            dict[str, Any]: The dictionary representation of the component.
        """
        dict_ = self.model_dump(*args, by_alias=True, **kwargs)
        for field_name in list(self.extra_fields.keys()):
            if field_name not in dict_:
                dict_[field_name] = getattr(self, field_name, None)
        dict_.pop("extra_fields", None)
        return dict_

    def to_xml(self, *args, **kwargs) -> str:
        """
        Convert the component to an XML string.

        Returns:
            str: The XML string representation of the component.
        """
        import xml.etree.ElementTree as ET

        root = ET.Element(self.__class__.__name__)

        def convert(dict_obj: dict, parent: ET.Element) -> None:
            for key, val in dict_obj.items():
                if isinstance(val, dict):
                    element = ET.SubElement(parent, key)
                    convert(val, element)
                else:
                    element = ET.SubElement(parent, key)
                    element.text = str(val)

        convert(self.to_dict(*args, **kwargs), root)
        return ET.tostring(root, encoding="unicode")

    def to_pd_series(self, *args, pd_kwargs: dict | None = None, **kwargs) -> Series:
        """
        Convert the node to a Pandas Series.

        Args:
            pd_kwargs (dict | None): Additional keyword arguments for Pandas Series.

        Returns:
            Series: The Pandas Series representation of the node.
        """
        pd_kwargs = {} if pd_kwargs is None else pd_kwargs
        dict_ = self.to_dict(*args, **kwargs)
        return Series(dict_, **pd_kwargs)

    def _add_field(
        self,
        field_name: str,
        annotation: Any | Type | None = Any,
        default: Any | None = None,
        value: Any | None = None,
        field: Any = None,
        **kwargs,
    ) -> None:
        """
        Add a field to the model after initialization.

        Args:
            field_name (str): The name of the field.
            annotation (Any | Type | None): The type annotation for the field.
            default (Any | None): The default value for the field.
            value (Any | None): The initial value for the field.
            field (Any): The Field object for the field.
            **kwargs: Additional keyword arguments for the Field object.
        """
        field = field or Field(default=default, **kwargs)
        self.extra_fields[field_name] = field
        if annotation:
            self.extra_fields[field_name].annotation = annotation

        if not value and (a := self._get_field_attr(field_name, "default", None)):
            value = a

        self.__setattr__(field_name, value)

    @property
    def _all_fields(self):
        return {**self.model_fields, **self.extra_fields}

    @property
    def _field_annotations(self) -> dict:
        """
        Return the annotations for each field in the model.

        Returns:
            dict: A dictionary mapping field names to their annotations.
        """

        return self._get_field_annotation(list(self._all_fields.keys()))

    def _get_field_attr(self, k: str, attr: str, default: Any = False) -> Any:
        """
        Get the value of a field attribute.

        Args:
            k (str): The field name.
            attr (str): The attribute name.
            default (Any): Default value to return if the attribute is not found.

        Returns:
            Any: The value of the field attribute, or the default value if not found.

        Raises:
            ValueError: If the field does not have the specified attribute.
        """
        try:
            if not self._field_has_attr(k, attr):
                raise ValueError(f"field {k} has no attribute {attr}")

            field = self._all_fields[k]
            a = getattr(field, attr, None)
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
    def _get_field_annotation(self, field_name: Any) -> Any:
        """
        Get the annotation for a field.

        Args:
            field_name (str): The name of the field.

        Raises:
            TypeError: If the field_name is of an unsupported type.
        """
        raise TypeError(f"Unsupported type {type(field_name)}")

    @_get_field_annotation.register(str)
    def _(self, field_name: str) -> dict[str, Any]:
        """
        Get the annotation for a field as a dictionary.

        Args:
            field_name (str): The name of the field.

        Returns:
            dict[str, Any]: A dictionary mapping the field name to its annotation.
        """
        dict_ = {field_name: self._all_fields[field_name].annotation}
        for k, v in dict_.items():
            if "|" in str(v):
                v = str(v)
                v = v.split("|")
                dict_[k] = func_call.lcall(v, convert.strip_lower)
            else:
                dict_[k] = [v.__name__]
        return dict_

    @_get_field_annotation.register(list)
    @_get_field_annotation.register(tuple)
    def _(self, field_names: list | tuple) -> dict[str, Any]:
        """
        Get the annotations for multiple fields as a dictionary.

        Args:
            field_names (list | tuple): A list or tuple of field names.

        Returns:
            dict[str, Any]: A dictionary mapping field names to their annotations.
        """
        dict_ = {}
        for field_name in field_names:
            dict_.update(self._get_field_annotation(field_name))
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
        field = self._all_fields.get(k, None)
        if field is None:
            raise ValueError(f"Field {k} not found in model fields.")

        a = attr in str(field)
        if not a:
            try:
                a = (
                    self._all_fields[k].json_schema_extra[attr] is not None
                    and attr in self._all_fields[k].json_schema_extra
                )
                return a if isinstance(a, bool) else False
            except Exception:
                return False
        return a

    def __str__(self):
        return f"{self.__class__.__name__}(id: {self.id_}, created: {self.timestamp})"
