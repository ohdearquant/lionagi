---
tags:
  - Tool
  - API
  - BaseObj
created: 2024-02-26
completed: true
---

# Component

> Child class of [`pydantic.BaseModel`](https://docs.pydantic.dev/latest/) [^1]and [`abc.ABC`](https://docs.python.org/3/library/abc.html)[^2]

The `Component` class serves as an abstract base class providing common attributes and utility methods for metadata management across different components within the Lionagi framework.

### Attributes

- `id_`: A unique identifier for the component, automatically generated.
- `timestamp`: A timestamp indicating the creation or last modification time, automatically generated.
- `metadata`: A dictionary to store metadata associated with the component.

### Class Methods

#### `class_name() -> str`

Returns the class name of the component.

#### `from_obj(obj: Any, *args, **kwargs) -> T`

A generic class method to create an instance from a given object. This method utilizes single dispatch to support various input types including dictionaries, JSON strings, pandas Series and DataFrame objects, lists, and instances of Pydantic's `BaseModel`. It raises a `NotImplementedError` for unsupported types.

- **Dictionary**: Creates an instance from a dictionary.
- **JSON String**: Parses a JSON string to create an instance.
- **Pandas Series**: Converts a pandas Series into an instance.
- **Pandas DataFrame**: Creates a list of instances from a pandas DataFrame.
- **List**: Generates instances from a list of compatible objects.
- **Pydantic BaseModel**: Creates an instance from another Pydantic model, applying conversion as necessary.

### Instance Methods

#### `to_json_data(*args, **kwargs) -> str`

Serializes the instance to a JSON string, customizable with additional Pydantic serialization options.

#### `to_dict(*args, **kwargs) -> dict[str, Any]`

Converts the instance to a dictionary, with options for custom serialization behaviors.

#### `to_xml() -> str`

Serializes the instance to an XML string, suitable for simple conversion cases.

#### `to_pd_series(*args, pd_kwargs: dict | None = None, **kwargs) -> dataframe.ln_Series`

Converts the instance into a pandas Series object, with additional options for pandas constructor.

#### `copy(*args, **kwargs) -> T`

Creates a deep copy of the instance, allowing for modifications through keyword arguments.

### Metadata Management Methods

#### `meta_keys(flattened: bool = False, **kwargs) -> list[str]`

Lists the keys in the metadata dictionary, with an option for flattened representation.

#### `meta_has_key(key: str, flattened: bool = False, **kwargs) -> bool`

Checks if a specific key exists in the metadata dictionary.

#### `meta_get(key: str, indices=None, default: Any = None) -> Any`

Retrieves a metadata value by key, with support for nested access and default values.

#### `meta_change_key(old_key: str, new_key: str) -> bool`

Renames a key in the metadata dictionary, if it exists.

#### `meta_insert(indices: str | list, value: Any, **kwargs) -> bool`

Inserts a value into the metadata dictionary at the specified path.

#### `meta_pop(key: str, default: Any = None) -> Any`

Removes a key from the metadata dictionary, returning its value or a default if not found.

#### `meta_merge(additional_metadata: dict[str, Any], overwrite: bool = False, **kwargs) -> None`

Merges another metadata dictionary into the existing one, with control over overwrite behavior.

#### `meta_clear() -> None`

Clears all keys and values from the metadata dictionary.

#### `meta_filter(condition: Callable[[Any, Any], bool]) -> dict[str, Any]`

Filters the metadata dictionary based on a condition function.

#### `meta_validate(schema: dict[str, Type | Callable]) -> bool`

Validates the metadata dictionary against a specified schema, checking types or custom validation functions.

[^1]: https://docs.pydantic.dev/latest/
[^2]: https://docs.python.org/3/library/abc.html
