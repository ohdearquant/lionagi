# tests/libs/test_schema_loader.py
"""Tests for load_pydantic_model_from_schema using pydantic.create_model."""

import json

from pydantic import BaseModel

from lionagi.libs.schema.load_pydantic_model_from_schema import (
    load_pydantic_model_from_schema,
)

# ---------------------------------------------------------------------------
# Basic input validation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Simple object schemas
# ---------------------------------------------------------------------------


def test_simple_object_with_required_fields():
    schema = {
        "title": "UserProfile",
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"},
        },
        "required": ["name", "age"],
    }
    cls = load_pydantic_model_from_schema(schema)
    assert issubclass(cls, BaseModel)
    assert cls.__name__ == "UserProfile"
    inst = cls(name="Ocean", age=30)
    assert inst.model_dump() == {"name": "Ocean", "age": 30}


def test_schema_from_json_string():
    schema_str = json.dumps(
        {
            "title": "Point",
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
            },
            "required": ["x", "y"],
        }
    )
    cls = load_pydantic_model_from_schema(schema_str)
    assert cls.__name__ == "Point"
    inst = cls(x=1.5, y=2.5)
    assert inst.x == 1.5


def test_optional_fields_default_to_none():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "nickname": {"type": "string"},
        },
        "required": ["name"],
    }
    cls = load_pydantic_model_from_schema(schema, "Person")
    inst = cls(name="Alice")
    assert inst.nickname is None


def test_field_with_default_value():
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "default": 10},
        },
    }
    cls = load_pydantic_model_from_schema(schema, "Counter")
    inst = cls()
    assert inst.count == 10


# ---------------------------------------------------------------------------
# Type coverage
# ---------------------------------------------------------------------------


def test_all_primitive_types():
    schema = {
        "type": "object",
        "properties": {
            "s": {"type": "string"},
            "n": {"type": "number"},
            "i": {"type": "integer"},
            "b": {"type": "boolean"},
        },
        "required": ["s", "n", "i", "b"],
    }
    cls = load_pydantic_model_from_schema(schema, "AllTypes")
    inst = cls(s="hello", n=3.14, i=42, b=True)
    assert inst.s == "hello"
    assert inst.n == 3.14
    assert inst.i == 42
    assert inst.b is True


def test_array_type():
    schema = {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["tags"],
    }
    cls = load_pydantic_model_from_schema(schema, "Tagged")
    inst = cls(tags=["a", "b", "c"])
    assert inst.tags == ["a", "b", "c"]


def test_array_without_items():
    schema = {
        "type": "object",
        "properties": {
            "data": {"type": "array"},
        },
        "required": ["data"],
    }
    cls = load_pydantic_model_from_schema(schema, "RawArray")
    inst = cls(data=[1, "two", 3.0])
    assert inst.data == [1, "two", 3.0]


# ---------------------------------------------------------------------------
# Enum support
# ---------------------------------------------------------------------------


def test_enum_constraint():
    schema = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["active", "inactive", "pending"],
            },
        },
        "required": ["status"],
    }
    cls = load_pydantic_model_from_schema(schema, "StatusModel")
    inst = cls(status="active")
    assert inst.status.value == "active"


# ---------------------------------------------------------------------------
# Nested objects
# ---------------------------------------------------------------------------


def test_nested_object():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "address": {
                "type": "object",
                "properties": {
                    "street": {"type": "string"},
                    "city": {"type": "string"},
                },
                "required": ["street", "city"],
            },
        },
        "required": ["name", "address"],
    }
    cls = load_pydantic_model_from_schema(schema, "Person")
    inst = cls(name="Alice", address={"street": "123 Main", "city": "NYC"})
    assert inst.address.street == "123 Main"
    assert inst.address.city == "NYC"


# ---------------------------------------------------------------------------
# $ref and $defs
# ---------------------------------------------------------------------------


def test_ref_with_defs():
    schema = {
        "type": "object",
        "$defs": {
            "Address": {
                "type": "object",
                "properties": {
                    "street": {"type": "string"},
                    "zip": {"type": "string"},
                },
                "required": ["street"],
            },
        },
        "properties": {
            "name": {"type": "string"},
            "home": {"$ref": "#/$defs/Address"},
            "work": {"$ref": "#/$defs/Address"},
        },
        "required": ["name", "home"],
    }
    cls = load_pydantic_model_from_schema(schema, "Employee")
    inst = cls(name="Bob", home={"street": "Oak Ave"})
    assert inst.home.street == "Oak Ave"
    assert inst.work is None


def test_ref_with_definitions():
    """Test 'definitions' key (older JSON Schema draft)."""
    schema = {
        "type": "object",
        "definitions": {
            "Color": {
                "type": "object",
                "properties": {
                    "r": {"type": "integer"},
                    "g": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["r", "g", "b"],
            },
        },
        "properties": {
            "foreground": {"$ref": "#/definitions/Color"},
        },
        "required": ["foreground"],
    }
    cls = load_pydantic_model_from_schema(schema, "Theme")
    inst = cls(foreground={"r": 255, "g": 0, "b": 0})
    assert inst.foreground.r == 255


# ---------------------------------------------------------------------------
# anyOf / oneOf
# ---------------------------------------------------------------------------


def test_anyof_nullable():
    """anyOf with null type is common for nullable fields."""
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "null"},
                ],
            },
        },
        "required": ["value"],
    }
    cls = load_pydantic_model_from_schema(schema, "Nullable")
    inst = cls(value="hello")
    assert inst.value == "hello"
    inst2 = cls(value=None)
    assert inst2.value is None


# ---------------------------------------------------------------------------
# Generic object (dict without properties)
# ---------------------------------------------------------------------------


def test_generic_dict_object():
    schema = {
        "type": "object",
        "properties": {
            "metadata": {"type": "object"},
        },
    }
    cls = load_pydantic_model_from_schema(schema, "WithMeta")
    inst = cls(metadata={"key": "value"})
    assert inst.metadata == {"key": "value"}


# ---------------------------------------------------------------------------
# Title sanitization
# ---------------------------------------------------------------------------


def test_title_sanitization():
    schema = {
        "title": "User Profile!",
        "type": "object",
        "properties": {
            "name": {"type": "string"},
        },
    }
    cls = load_pydantic_model_from_schema(schema)
    assert cls.__name__ == "UserProfile"


def test_title_with_numbers():
    schema = {
        "title": "123BadTitle",
        "type": "object",
        "properties": {
            "x": {"type": "integer"},
        },
    }
    # Starts with number, so title is rejected; falls back to default
    cls = load_pydantic_model_from_schema(schema, "Fallback")
    assert cls.__name__ == "Fallback"


# ---------------------------------------------------------------------------
# Array of objects
# ---------------------------------------------------------------------------


def test_array_of_nested_objects():
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "label": {"type": "string"},
                    },
                    "required": ["id", "label"],
                },
            },
        },
        "required": ["items"],
    }
    cls = load_pydantic_model_from_schema(schema, "ItemList")
    inst = cls(items=[{"id": 1, "label": "a"}, {"id": 2, "label": "b"}])
    assert len(inst.items) == 2
    assert inst.items[0].id == 1
    assert inst.items[1].label == "b"


# ---------------------------------------------------------------------------
# Field descriptions
# ---------------------------------------------------------------------------


def test_field_descriptions_preserved():
    schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The user's full name",
            },
        },
        "required": ["name"],
    }
    cls = load_pydantic_model_from_schema(schema, "Described")
    field_info = cls.model_fields["name"]
    assert field_info.description == "The user's full name"


# ---------------------------------------------------------------------------
# Type list (e.g. ["string", "null"])
# ---------------------------------------------------------------------------


def test_type_as_list():
    schema = {
        "type": "object",
        "properties": {
            "value": {"type": ["string", "null"]},
        },
        "required": ["value"],
    }
    cls = load_pydantic_model_from_schema(schema, "TypeList")
    inst = cls(value="hello")
    assert inst.value == "hello"
    inst2 = cls(value=None)
    assert inst2.value is None


# ---------------------------------------------------------------------------
# Round-trip: model -> schema -> model
# ---------------------------------------------------------------------------


def test_roundtrip_schema():
    """A model's own json_schema should produce an equivalent model."""

    class Original(BaseModel):
        name: str
        count: int = 0

    schema = Original.model_json_schema()
    cls = load_pydantic_model_from_schema(schema)
    inst = cls(name="test")
    assert inst.name == "test"
    assert inst.count == 0


# ---------------------------------------------------------------------------
# allOf (merge)
# ---------------------------------------------------------------------------


def test_allof_merge():
    schema = {
        "type": "object",
        "properties": {
            "combined": {
                "allOf": [
                    {
                        "type": "object",
                        "properties": {"a": {"type": "string"}},
                        "required": ["a"],
                    },
                    {
                        "type": "object",
                        "properties": {"b": {"type": "integer"}},
                        "required": ["b"],
                    },
                ],
            },
        },
        "required": ["combined"],
    }
    cls = load_pydantic_model_from_schema(schema, "Merged")
    inst = cls(combined={"a": "hello", "b": 42})
    assert inst.combined.a == "hello"
    assert inst.combined.b == 42
