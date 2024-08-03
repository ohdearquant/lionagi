from lion_core.generic.note import Note as CoreNote


class Note(CoreNote):
    """
    A versatile container for managing nested dictionary data structures in the Lion framework.

    The Note class provides a flexible and powerful way to handle complex, nested data
    with an intuitive interface. It combines the functionalities of Pydantic's BaseModel
    and the Lion framework's Container, offering robust data validation and rich
    nested data manipulation capabilities.

    Key Features:
    1. Nested Data Management: Efficiently handles multi-level nested dictionary structures.
    2. Flexible Access: Supports both dictionary-style and attribute-style access to data.
    3. Path-based Operations: Allows getting, setting, and manipulating data using path indices.
    4. Serialization: Provides methods for converting to and from dictionary representations.
    5. Update Mechanisms: Offers various ways to update data, including from dictionaries and strings.
    6. Integration with Lion Framework: Seamlessly works with other Lion framework components.

    Attributes:
        content (dict[str, Any]): The underlying nested dictionary structure.

    Usage:
        note = Note(key1="value1", nested={"key2": "value2"})
        note.set(["nested", "key3"], "value3")
        value = note.get(["nested", "key2"])
        note.update({"new_key": "new_value"})

    The Note class is particularly useful in scenarios requiring:
    - Complex data structures with nested levels
    - Flexible and intuitive data access and manipulation
    - Integration with other Lion framework components
    - Serialization and deserialization of nested data

    Advanced Features:
    - Flattened views: Access keys, values, or items in a flattened structure.
    - Type-specific updates: Handle updates from various input types (dict, str, Element, etc.).
    - Clone tracking: Special handling for cloned BaseMail objects.

    Note:
        - The Note class is designed to be thread-safe for basic operations.
        - It leverages Pydantic for data validation and serialization.
        - The class supports both list-style and string indices for nested access.

    Performance Considerations:
    - For very large nested structures, consider using flattened views for improved performance.
    - The update method can handle various input types but may have performance implications
      for large datasets or complex conversions.

    See Also:
        BaseModel: Pydantic model providing data validation.
        Container: Lion framework interface for container operations.
        Element: Base class for Lion framework objects, often used with Note.
        BaseMail: Special handling for cloned BaseMail objects in serialization.
    """


note = Note

__all__ = ["Note", "note"]
