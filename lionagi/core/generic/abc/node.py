"""base nodes in lionagi"""
from abc import ABC
from functools import singledispatchmethod
from typing import Any, TypeVar
from pydantic import AliasChoices, BaseModel, Field, ValidationError
from pandas import DataFrame, Series
from lionagi.libs import SysUtil, convert, ParseUtil, nested
from .component import BaseComponent

T = TypeVar("T")


class BaseNode(BaseComponent, ABC):
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
        validation_alias="meta",
        description="Additional metadata for the node.",
    )

    @singledispatchmethod
    @classmethod
    def from_obj(cls, obj: Any, *args, **kwargs) -> T:
        """
        Create a node instance from an object.

        Args:
            obj (Any): The object to create the node from.
            *args: Additional positional arguments.
            **kwargs: Additional keyword arguments.

        Raises:
            NotImplementedError: If the object type is not supported.
        """
        if not isinstance(obj, (dict, str, list, Series, DataFrame, BaseModel)):
            type_ = str(type(obj))
            if "llama_index" in type_:
                return cls.from_obj(obj.to_dict())
            elif "langchain" in type_:
                langchain_json = obj.to_json()
                langchain_dict = {
                    "lc_id": langchain_json["id"],
                    **langchain_json["kwargs"],
                }
                return cls.from_obj(langchain_dict)

        raise NotImplementedError(f"Unsupported type: {type(obj)}")

    @from_obj.register(dict)
    @classmethod
    def _from_dict(cls, obj: dict, *args, **kwargs) -> T:
        """
        Create a node instance from a dictionary.

        Args:
            obj (dict): The dictionary to create the node from.
            *args: Additional positional arguments.
            **kwargs: Additional keyword arguments.

        Returns:
            T: The created node instance.
        """
        return cls.model_validate(obj, *args, **kwargs)

    @from_obj.register(str)
    @classmethod
    def _from_str(cls, obj: str, *args, fuzzy_parse: bool = False, **kwargs) -> T:
        """
        Create a node instance from a JSON string.

        Args:
            obj (str): The JSON string to create the node from.
            *args: Additional positional arguments.
            fuzzy_parse (bool): Whether to perform fuzzy parsing.
            **kwargs: Additional keyword arguments.

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
    def _from_list(cls, obj: list, *args, **kwargs) -> list[T]:
        """
        Create a list of node instances from a list of objects.

        Args:
            obj (list): The list of objects to create nodes from.
            *args: Additional positional arguments.
            **kwargs: Additional keyword arguments.

        Returns:
            list[T]: The list of created node instances.
        """
        return [cls.from_obj(item, *args, **kwargs) for item in obj]

    @from_obj.register(Series)
    @classmethod
    def _from_pd_series(
        cls, obj: Series, *args, pd_kwargs: dict | None = None, **kwargs
    ) -> T:
        """
        Create a node instance from a Pandas Series.

        Args:
            obj (Series): The Pandas Series to create the node from.
            *args: Additional positional arguments.
            pd_kwargs (dict | None): Additional keyword arguments for Pandas Series.
            **kwargs: Additional keyword arguments.

        Returns:
            T: The created node instance.
        """
        pd_kwargs = pd_kwargs or {}
        return cls.from_obj(obj.to_dict(**pd_kwargs), *args, **kwargs)

    @from_obj.register(DataFrame)
    @classmethod
    def _from_pd_dataframe(
        cls, obj: DataFrame, *args, pd_kwargs: dict | None = None, **kwargs
    ) -> list[T]:
        """
        Create a list of node instances from a Pandas DataFrame.

        Args:
            obj (DataFrame): The Pandas DataFrame to create nodes from.
            *args: Additional positional arguments.
            pd_kwargs (dict | None): Additional keyword arguments for Pandas DataFrame.
            **kwargs: Additional keyword arguments.

        Returns:
            list[T]: The list of created node instances.
        """
        if pd_kwargs is None:
            pd_kwargs = {}

        _objs = []
        for index, row in obj.iterrows():
            _obj = cls.from_obj(row, *args, **pd_kwargs, **kwargs)
            _obj.metadata["df_index"] = index
            _objs.append(_obj)

        return _objs

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
        pydantic_kwargs = pydantic_kwargs or {"by_alias": True}
        try:
            config_ = {}
            try:
                config_ = obj.model_dump(**pydantic_kwargs)
            except:
                config_ = obj.to_dict(**pydantic_kwargs)
            else:
                config_ = obj.dict(**pydantic_kwargs)
        except Exception as e:
            raise ValueError(f"Invalid Pydantic model for deserialization: {e}") from e

        return cls.from_obj(config_ | kwargs)

    def meta_get(
        self, key: str, indices: list[str | int] | None = None, default: Any = None
    ) -> Any:
        """
        Get a value from the metadata dictionary.

        Args:
            key (str): The key to retrieve the value for.
            indices (list[str | int] | None): Optional list of indices for nested retrieval.
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
