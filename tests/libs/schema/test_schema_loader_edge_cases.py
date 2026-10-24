import pytest

import lionagi.libs.schema.load_pydantic_model_from_schema as schema_mod
from lionagi.libs.schema.load_pydantic_model_from_schema import (
    load_pydantic_model_from_schema,
)


def test_schema_loader_raises_runtime_error_when_codegen_fallback_unavailable(
    monkeypatch,
):
    schema = {
        "type": "object",
        "properties": {
            "x": {"type": ["string", "unsupported"]},
        },
    }

    monkeypatch.setattr(schema_mod, "_HAS_DATAMODEL_CODE_GENERATOR", False)

    with pytest.raises(RuntimeError, match="datamodel-code-generator"):
        load_pydantic_model_from_schema(schema, "BadModel")


def test_codegen_fallback_without_the_generator_names_the_package_and_floor(monkeypatch):
    """No extra installs the generator, so the error names the package and its version floor."""
    schema = {"type": "object", "properties": {"x": {"type": ["string", "unsupported"]}}}
    monkeypatch.setattr(schema_mod, "_HAS_DATAMODEL_CODE_GENERATOR", False)

    with pytest.raises(RuntimeError) as excinfo:
        load_pydantic_model_from_schema(schema, "BadModel", allow_codegen=True)
    message = str(excinfo.value)
    assert "is not installed for fallback" in message
    assert message.endswith("pip install 'datamodel-code-generator>=0.64.0'")
