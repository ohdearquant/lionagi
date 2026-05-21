
from lionagi.libs.schema.load_pydantic_model_from_schema import (
    load_pydantic_model_from_schema,
)


def test_schema_loader_resolves_refs_and_additional_properties():
    schema = {
        "$defs": {
            "Address": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "zip": {"type": "string"},
                },
                "required": ["city", "zip"],
            }
        },
        "type": "object",
        "properties": {
            "address": {"$ref": "#/$defs/Address"},
            "tags": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["address", "tags"],
    }

    Model = load_pydantic_model_from_schema(schema, "AddressRecord")
    instance = Model(address={"city": "NYC", "zip": "10001"}, tags={"tier": "gold"})

    assert instance.address.city == "NYC"
    assert instance.tags == {"tier": "gold"}
