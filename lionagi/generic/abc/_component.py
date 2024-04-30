"""base components in lionagi"""
from abc import ABC
from functools import singledispatchmethod
from pydantic import AliasChoices, BaseModel, Field
from pandas import Series

from lionagi.libs.ln_convert import to_str, strip_lower
from lionagi.libs.ln_func_call import lcall
from lionagi.libs import SysUtil

from ._concepts import Record


class Component(BaseModel, Record, ABC):
    """a distuinguishable temporal entity in lionagi"""

    ln_id: str = Field(
        title="ID",
        default_factory=SysUtil.create_id,
        validation_alias=AliasChoices("node_id", "ID", "id", "id_"),
        description="A 32-char unique hash identifier.",
        frozen=True,
    )
    
    timestamp: str = Field(
        default_factory=lambda: SysUtil.get_timestamp(sep=None)[:-6],
        title="Creation Timestamp",
        description="The utc timestamp of when the component was created.",
        frozen=True,
    )

    metadata: dict[str, any] = Field(
        default_factory=dict,
        validation_alias="meta",
        description="Additional metadata for the component.",
    )

    extra_fields: dict[str, any] = Field(
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
        return cls.__name__

    def to_json_str(self, *args, **kwargs) -> str:
        """
        Convert the component to a JSON string.

        Returns:
            str: The JSON string representation of the component.
        """
        dict_ = self.to_dict(*args, **kwargs)
        return to_str(dict_)

    def to_dict(self, *args, **kwargs) -> dict[str, any]:
        """
        Convert the component to a dictionary.

        Returns:
            dict[str, any]: The dictionary representation of the component.
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
        field: str,
        annotation: any = None,
        default: any | None = None,
        value: any | None = None,
        field_obj: any = None,
        **kwargs,
    ) -> None:
        """
        Add a field to the model after initialization.

        Args:
            field_name (str): The name of the field.
            annotation (any | Type | None): The type annotation for the field.
            default (any | None): The default value for the field.
            value (any | None): The initial value for the field.
            field (any): The Field object for the field.
            **kwargs: Additional keyword arguments for the Field object.
        """
        self.extra_fields[field] = field_obj or Field(default=default, **kwargs)
        if annotation:
            self.extra_fields[field].annotation = annotation

        if not value and (a := self._get_field_attr(field, "default", None)):
            value = a

        self.__setattr__(field, value)

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

    def _get_field_attr(self, k: str, attr: str, default: any = False) -> any:
        """
        Get the value of a field attribute.

        Args:
            k (str): The field name.
            attr (str): The attribute name.
            default (any): Default value to return if the attribute is not found.

        Returns:
            any: The value of the field attribute, or the default value if not found.

        Raises:
            ValueError: If the field does not have the specified attribute.
        """
        try:
            if not self._field_has_attr(k, attr):
                raise ValueError(f"field {k} has no attribute {attr}")

            field = self._all_fields[k]
            if not (a := getattr(field, attr, None)):
                try:
                    return field.json_schema_extra[attr]
                except Exception:
                    return None
            return a
        except Exception as e:
            if default is not False:
                return default
            raise e

    @singledispatchmethod
    def _get_field_annotation(self, field_name: any) -> any:
        """
        Get the annotation for a field.

        Args:
            field_name (str): The name of the field.

        Raises:
            TypeError: If the field_name is of an unsupported type.
        """
        raise NotImplementedError

    @_get_field_annotation.register(str)
    def _(self, field_name: str) -> dict[str, any]:
        """
        Get the annotation for a field as a dictionary.

        Args:
            field_name (str): The name of the field.

        Returns:
            dict[str, any]: A dictionary mapping the field name to its annotation.
        """
        dict_ = {field_name: self._all_fields[field_name].annotation}
        for k, v in dict_.items():
            if "|" in str(v):
                v = str(v)
                v = v.split("|")
                dict_[k] = lcall(v, strip_lower)
            else:
                dict_[k] = [v.__name__]
        return dict_

    @_get_field_annotation.register(list)
    @_get_field_annotation.register(tuple)
    def _(self, field_names: list | tuple) -> dict[str, any]:
        """
        Get the annotations for multiple fields as a dictionary.

        Args:
            field_names (list | tuple): A list or tuple of field names.

        Returns:
            dict[str, any]: A dictionary mapping field names to their annotations.
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
        
        if not (field := self._all_fields.get(k, None)):
            raise KeyError(f"Field {k} not found in model fields.")

        if not attr in str(field):
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
