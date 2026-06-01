import pytest
from pydantic import BaseModel

from lionagi.protocols.messages.instruction import Instruction, InstructionContent
from lionagi.protocols.messages.message import MessageRole


class SampleRequestModel(BaseModel):
    """Model for testing Pydantic schema handling"""

    name: str
    age: int
    optional: str | None = None


class NestedRequestModel(BaseModel):
    """Model for testing nested Pydantic schemas"""

    user: SampleRequestModel
    timestamp: str


# ============================================================================
# InstructionContent Tests
# ============================================================================


def test_instruction_content_basic_initialization():
    """Test basic initialization of InstructionContent"""
    content = InstructionContent(instruction="Test instruction")

    assert content.instruction == "Test instruction"
    assert content.guidance is None
    assert content.prompt_context == []
    assert content.plain_content is None
    assert content.tool_schemas == []
    assert content.response_format is None
    assert content.images == []
    assert content.image_detail is None


def test_instruction_content_all_fields():
    """Test InstructionContent with all fields populated"""
    content = InstructionContent(
        instruction="Do something",
        guidance="Be careful",
        prompt_context=["context1", {"key": "value"}],
        tool_schemas=[{"name": "tool1", "type": "function"}],
        response_format={"result": "value"},
        images=["image1.jpg"],
        image_detail="high",
    )

    assert content.instruction == "Do something"
    assert content.guidance == "Be careful"
    assert len(content.prompt_context) == 2
    assert len(content.tool_schemas) == 1
    assert content.response_format == {"result": "value"}
    assert len(content.images) == 1
    assert content.image_detail == "high"


def test_instruction_content_rendered_text_only():
    """Test rendered property returns minimal_yaml formatted text"""
    content = InstructionContent(instruction="Test instruction", guidance="Test guidance")

    rendered = content.rendered
    assert isinstance(rendered, str)
    assert "Instruction:" in rendered or "instruction" in rendered.lower()
    assert "Guidance:" in rendered or "guidance" in rendered.lower()


def test_instruction_content_rendered_with_context():
    """Test rendered property includes context items"""
    content = InstructionContent(
        instruction="Test",
        prompt_context=["context1", {"nested": "context"}],
    )

    rendered = content.rendered
    assert isinstance(rendered, str)
    assert "context" in rendered.lower()


def test_instruction_content_rendered_with_response_format():
    """Test rendered property includes response format as JSON example"""
    content = InstructionContent(
        instruction="Test",
        response_format={"name": "John", "age": 30},
    )

    rendered = content.rendered
    assert isinstance(rendered, str)
    assert "responseformat" in rendered.lower() or "ResponseFormat" in rendered
    assert "```json" in rendered
    assert "name" in rendered
    assert "John" in rendered


def test_instruction_content_rendered_plain_content_override():
    """Test plain_content bypasses structured rendering"""
    content = InstructionContent(
        instruction="This should be ignored",
        guidance="Also ignored",
        plain_content="Only this text",
    )

    rendered = content.rendered
    assert rendered == "Only this text"
    assert "This should be ignored" not in rendered


def test_instruction_content_rendered_with_images():
    """Test rendered property returns list[dict] when images present"""
    content = InstructionContent(
        instruction="Analyze this image",
        images=["https://example.com/image.jpg"],
        image_detail="high",
    )

    rendered = content.rendered
    assert isinstance(rendered, list)
    assert len(rendered) == 2  # text + image
    assert rendered[0]["type"] == "text"
    assert rendered[1]["type"] == "image_url"
    assert rendered[1]["image_url"]["url"] == "https://example.com/image.jpg"
    assert rendered[1]["image_url"]["detail"] == "high"


def test_instruction_content_rendered_multiple_images():
    """Test rendered property with multiple images"""
    content = InstructionContent(
        instruction="Compare these images",
        images=["image1.jpg", "data:image/png;base64,abc123", "image3.jpg"],
        image_detail="low",
    )

    rendered = content.rendered
    assert isinstance(rendered, list)
    assert len(rendered) == 4  # text + 3 images
    assert all(item["type"] in ("text", "image_url") for item in rendered)


def test_instruction_content_image_detail_auto():
    """Test image_detail defaults to 'auto' when images present"""
    content = InstructionContent(instruction="Test", images=["image.jpg"], image_detail="auto")

    rendered = content.rendered
    assert rendered[1]["image_url"]["detail"] == "auto"


def test_instruction_content_base64_image_handling():
    """Test base64 images are properly formatted"""
    base64_str = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    content = InstructionContent(instruction="Test", images=[base64_str], image_detail="high")

    rendered = content.rendered
    assert rendered[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert base64_str in rendered[1]["image_url"]["url"]


# ============================================================================
# InstructionContent.from_dict() Tests
# ============================================================================


def test_from_dict_basic():
    """Test from_dict with basic fields"""
    data = {"instruction": "Do this", "guidance": "Carefully"}
    content = InstructionContent.from_dict(data)

    assert content.instruction == "Do this"
    assert content.guidance == "Carefully"


def test_from_dict_with_context():
    """Test from_dict with context as list"""
    data = {"instruction": "Test", "context": ["ctx1", "ctx2"]}
    content = InstructionContent.from_dict(data)

    assert len(content.prompt_context) == 2
    assert "ctx1" in content.prompt_context


def test_from_dict_context_single_item():
    """Test from_dict with context as single item (converts to list)"""
    data = {"instruction": "Test", "context": "single_context"}
    content = InstructionContent.from_dict(data)

    assert isinstance(content.prompt_context, list)
    assert "single_context" in content.prompt_context


def test_from_dict_with_tool_schemas():
    """Test from_dict with tool_schemas"""
    schemas = [{"name": "tool1"}, {"name": "tool2"}]
    data = {"instruction": "Test", "tool_schemas": schemas}
    content = InstructionContent.from_dict(data)

    assert len(content.tool_schemas) == 2
    assert content.tool_schemas[0]["name"] == "tool1"


def test_from_dict_with_images():
    """Test from_dict with images"""
    data = {
        "instruction": "Test",
        "images": ["img1.jpg", "img2.jpg"],
        "image_detail": "high",
    }
    content = InstructionContent.from_dict(data)

    assert len(content.images) == 2
    assert content.image_detail == "high"


def test_from_dict_images_default_detail():
    """Test from_dict sets image_detail to 'auto' when images present but detail not specified"""
    data = {"instruction": "Test", "images": ["img.jpg"]}
    content = InstructionContent.from_dict(data)

    assert content.image_detail == "auto"


def test_from_dict_with_pydantic_model_instance():
    """Test from_dict with Pydantic model class for response_format"""
    # Pass the CLASS to from_dict for proper schema generation
    data = {"instruction": "Test", "response_format": SampleRequestModel}
    content = InstructionContent.from_dict(data)

    assert content.response_format == SampleRequestModel
    assert content._structure_instance is not None
    assert content._structure_instance.base == SampleRequestModel


def test_from_dict_with_pydantic_model_class():
    """Test from_dict with Pydantic model class for response_format"""
    data = {"instruction": "Test", "response_format": SampleRequestModel}
    content = InstructionContent.from_dict(data)

    assert content.response_format == SampleRequestModel
    assert content._structure_instance is not None
    schema = content._structure_instance.request_schema()
    assert "name" in schema.model_fields
    assert "age" in schema.model_fields


def test_from_dict_with_nested_pydantic_model():
    """Test from_dict with nested Pydantic model"""
    data = {"instruction": "Test", "response_format": NestedRequestModel}
    content = InstructionContent.from_dict(data)

    assert content.response_format == NestedRequestModel
    assert content._structure_instance is not None
    schema = content._structure_instance.request_schema()
    assert "user" in schema.model_fields


def test_from_dict_pydantic_model_auto_derives_response_format():
    """Test from_dict derives schema via structure from Pydantic model"""
    data = {"instruction": "Test", "response_format": SampleRequestModel}
    content = InstructionContent.from_dict(data)

    assert content.response_format == SampleRequestModel
    assert content._structure_instance is not None
    schema = content._structure_instance.request_schema()
    assert isinstance(schema, type)
    assert "name" in schema.model_fields


def test_from_dict_with_dict_response_schema():
    """Test from_dict with dict response_schema"""
    schema = {"type": "object", "properties": {"field": {"type": "string"}}}
    data = {"instruction": "Test", "response_format": schema}
    content = InstructionContent.from_dict(data)

    assert content.response_format == schema


def test_from_dict_with_explicit_response_format():
    """Test from_dict with explicit response_format dict"""
    rf = {"name": "example", "age": 25}
    data = {"instruction": "Test", "response_format": rf}
    content = InstructionContent.from_dict(data)

    assert content.response_format == rf


def test_from_dict_response_format_overrides_auto_derive():
    """Test explicit response_format prevents auto-derivation from Pydantic model"""
    rf = {"custom": "format"}
    data = {
        "instruction": "Test",
        "response_format": SampleRequestModel,
        "response_format": rf,
    }
    content = InstructionContent.from_dict(data)

    assert content.response_format == rf  # Should use explicit, not auto-derived


def test_from_dict_invalid_response_schema_type():
    """Test from_dict silently ignores invalid response_format type (fuzzy handling)"""
    data = {"instruction": "Test", "response_format": "invalid"}

    # Fuzzy handling: invalid types are silently ignored
    content = InstructionContent.from_dict(data)
    assert content.response_format is None


def test_from_dict_invalid_response_format_type():
    """Test from_dict silently ignores invalid response_format type (fuzzy handling)"""
    data = {"instruction": "Test", "response_format": "invalid"}

    # Fuzzy handling: invalid types are silently ignored
    content = InstructionContent.from_dict(data)
    assert content.response_format is None


def test_from_dict_empty_dict():
    """Test from_dict with empty dict creates default InstructionContent"""
    content = InstructionContent.from_dict({})

    assert content.instruction is None
    assert content.guidance is None
    assert content.prompt_context == []


# ============================================================================
# Instruction (Message) Tests
# ============================================================================


def test_instruction_basic_initialization():
    """Test basic initialization of Instruction message"""
    instruction = Instruction(
        content={"instruction": "Test instruction"},
        sender="user",
        recipient="assistant",
    )

    assert instruction.role == MessageRole.USER
    assert instruction.content.instruction == "Test instruction"
    assert instruction.sender == "user"
    assert instruction.recipient == "assistant"


def test_instruction_with_instruction_content_instance():
    """Test Instruction with InstructionContent instance"""
    content = InstructionContent(instruction="Test", guidance="Guide")
    instruction = Instruction(content=content, sender="user", recipient="assistant")

    assert instruction.content.instruction == "Test"
    assert instruction.content.guidance == "Guide"


def test_instruction_content_validator_with_dict():
    """Test Instruction content validator converts dict to InstructionContent"""
    instruction = Instruction(
        content={
            "instruction": "Do this",
            "guidance": "Carefully",
            "context": ["ctx1"],
        },
        sender="user",
        recipient="assistant",
    )

    assert isinstance(instruction.content, InstructionContent)
    assert instruction.content.instruction == "Do this"
    assert instruction.content.guidance == "Carefully"
    assert len(instruction.content.prompt_context) == 1


def test_instruction_content_validator_with_none():
    """Test Instruction content validator creates empty InstructionContent for None"""
    instruction = Instruction(content=None, sender="user", recipient="assistant")

    assert isinstance(instruction.content, InstructionContent)
    assert instruction.content.instruction is None


def test_instruction_content_validator_invalid_type():
    """Test Instruction content validator raises TypeError for invalid type"""
    with pytest.raises(TypeError, match="content must be dict or InstructionContent"):
        Instruction(content="invalid", sender="user", recipient="assistant")


def test_instruction_with_context():
    """Test Instruction with context"""
    instruction = Instruction(
        content={
            "instruction": "Test",
            "context": [{"key": "value"}, "string_context"],
        },
        sender="user",
        recipient="assistant",
    )

    assert len(instruction.content.prompt_context) == 2
    assert {"key": "value"} in instruction.content.prompt_context


def test_instruction_with_guidance():
    """Test Instruction with guidance"""
    instruction = Instruction(
        content={"instruction": "Test", "guidance": "Important guidance"},
        sender="user",
        recipient="assistant",
    )

    assert instruction.content.guidance == "Important guidance"


def test_instruction_with_tool_schemas():
    """Test Instruction with tool schemas"""
    schemas = [
        {"type": "function", "function": {"name": "tool1"}},
        {"type": "function", "function": {"name": "tool2"}},
    ]
    instruction = Instruction(
        content={"instruction": "Use tools", "tool_schemas": schemas},
        sender="user",
        recipient="assistant",
    )

    assert len(instruction.content.tool_schemas) == 2
    assert instruction.content.tool_schemas[0]["function"]["name"] == "tool1"


def test_instruction_with_pydantic_response_schema():
    """Test Instruction with Pydantic model response format"""
    instruction = Instruction(
        content={
            "instruction": "Return structured data",
            "response_format": SampleRequestModel,
        },
        sender="user",
        recipient="assistant",
    )

    assert instruction.content.response_format == SampleRequestModel
    assert instruction.content._structure_instance is not None
    schema = instruction.content._structure_instance.request_schema()
    assert isinstance(schema, type)
    assert "name" in schema.model_fields


def test_instruction_with_explicit_response_format():
    """Test Instruction with explicit response_format"""
    rf = {"name": "John Doe", "age": 35, "optional": "value"}
    instruction = Instruction(
        content={"instruction": "Test", "response_format": rf},
        sender="user",
        recipient="assistant",
    )

    assert instruction.content.response_format == rf


def test_instruction_with_images():
    """Test Instruction with images"""
    instruction = Instruction(
        content={
            "instruction": "Analyze image",
            "images": ["https://example.com/img.jpg", "local_image.png"],
            "image_detail": "low",
        },
        sender="user",
        recipient="assistant",
    )

    assert len(instruction.content.images) == 2
    assert instruction.content.image_detail == "low"


def test_instruction_with_plain_content():
    """Test Instruction with plain_content bypasses structured rendering"""
    instruction = Instruction(
        content={
            "instruction": "Ignored",
            "plain_content": "Use only this text",
        },
        sender="user",
        recipient="assistant",
    )

    rendered = instruction.content.rendered
    assert rendered == "Use only this text"


def test_instruction_rendered_property_text():
    """Test Instruction rendered content for text-only"""
    instruction = Instruction(
        content={
            "instruction": "Complete this task",
            "guidance": "Follow best practices",
        },
        sender="user",
        recipient="assistant",
    )

    rendered = instruction.content.rendered
    assert isinstance(rendered, str)
    assert "Instruction:" in rendered or "instruction" in rendered.lower()


def test_instruction_rendered_property_with_images():
    """Test Instruction rendered content with images"""
    instruction = Instruction(
        content={
            "instruction": "Describe image",
            "images": ["test.jpg"],
            "image_detail": "high",
        },
        sender="user",
        recipient="assistant",
    )

    rendered = instruction.content.rendered
    assert isinstance(rendered, list)
    assert len(rendered) == 2  # text + image
    assert rendered[0]["type"] == "text"
    assert rendered[1]["type"] == "image_url"


def test_instruction_role_fixed_as_user():
    """Test Instruction role is always USER"""
    instruction = Instruction(content={"instruction": "Test"}, sender="user", recipient="assistant")

    assert instruction.role == MessageRole.USER


def test_instruction_serialization():
    """Test Instruction can be serialized/deserialized"""
    original = Instruction(
        content={
            "instruction": "Test",
            "guidance": "Guide",
            "context": [{"key": "value"}],
        },
        sender="user",
        recipient="assistant",
    )

    # Test model_dump
    data = original.model_dump()
    assert data["role"] == "user"
    assert "content" in data

    # Test model_validate (round-trip)
    restored = Instruction.model_validate(data)
    assert restored.role == original.role
    assert restored.content.instruction == original.content.instruction
    assert restored.content.guidance == original.content.guidance


# ============================================================================
# Minimal YAML Rendering Tests
# ============================================================================


def test_minimal_yaml_rendering_strips_empty_fields():
    """Test minimal_yaml rendering strips None, empty lists, empty dicts"""
    content = InstructionContent(
        instruction="Test",
        guidance=None,  # Should be stripped
        prompt_context=[],  # Should be stripped
        tool_schemas=[],  # Should be stripped
    )

    rendered = content.rendered
    assert "guidance" not in rendered.lower() or "Guidance:" not in rendered
    assert "context" not in rendered.lower() or "Context:" not in rendered
    assert "tools" not in rendered.lower() or "Tools:" not in rendered


def test_minimal_yaml_rendering_includes_non_empty_fields():
    """Test minimal_yaml includes all non-empty fields"""
    content = InstructionContent(
        instruction="Do this",
        guidance="Be careful",
        prompt_context=["ctx1"],
        tool_schemas=[{"name": "tool"}],
    )

    rendered = content.rendered
    # All non-empty fields should appear
    assert "instruction" in rendered.lower() or "Instruction:" in rendered
    assert "guidance" in rendered.lower() or "Guidance:" in rendered
    assert "context" in rendered.lower() or "Context:" in rendered
    assert "tool" in rendered.lower() or "Tools:" in rendered


def test_minimal_yaml_response_format_as_json_block():
    """Test minimal_yaml renders response_format as JSON code block"""
    content = InstructionContent(
        instruction="Test",
        response_format={"key": "value", "nested": {"field": 123}},
    )

    rendered = content.rendered
    assert "```json" in rendered
    assert "```" in rendered
    assert "key" in rendered
    assert "value" in rendered
    assert "MUST RETURN JSON-PARSEABLE RESPONSE" in rendered


def test_minimal_yaml_response_schema_included():
    """Test minimal_yaml includes ResponseSchema for BaseModel, ResponseFormat for dict"""
    # Dict mode: only ResponseFormat (no model_json_schema available)
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    content = InstructionContent(instruction="Test", response_format=schema)
    rendered = content.rendered
    assert "responseformat" in rendered.lower() or "ResponseFormat:" in rendered
    assert "type" in rendered

    # BaseModel mode: both ResponseSchema and ResponseFormat
    content2 = InstructionContent(instruction="Test", response_format=SampleRequestModel)
    rendered2 = content2.rendered
    assert "responseschema" in rendered2.lower() or "ResponseSchema:" in rendered2


# ============================================================================
# Edge Cases and Error Handling
# ============================================================================


def test_instruction_content_empty():
    """Test empty InstructionContent renders to empty string"""
    content = InstructionContent()
    rendered = content.rendered

    # Should render to minimal/empty YAML structure
    assert isinstance(rendered, str)


def test_instruction_with_http_image_url():
    """Test HTTP image URLs are passed through correctly"""
    instruction = Instruction(
        content={
            "instruction": "Test",
            "images": ["http://example.com/image.jpg"],
            "image_detail": "auto",
        },
        sender="user",
        recipient="assistant",
    )

    rendered = instruction.content.rendered
    assert rendered[1]["image_url"]["url"] == "http://example.com/image.jpg"


def test_instruction_with_https_image_url():
    """Test HTTPS image URLs are passed through correctly"""
    instruction = Instruction(
        content={
            "instruction": "Test",
            "images": ["https://example.com/image.jpg"],
            "image_detail": "auto",
        },
        sender="user",
        recipient="assistant",
    )

    rendered = instruction.content.rendered
    assert rendered[1]["image_url"]["url"] == "https://example.com/image.jpg"


def test_instruction_with_data_url_image():
    """Test data URL images are passed through correctly"""
    data_url = "data:image/png;base64,iVBORw0KGgo="
    instruction = Instruction(
        content={
            "instruction": "Test",
            "images": [data_url],
            "image_detail": "high",
        },
        sender="user",
        recipient="assistant",
    )

    rendered = instruction.content.rendered
    assert rendered[1]["image_url"]["url"] == data_url


def test_instruction_complex_nested_context():
    """Test deeply nested context structures"""
    complex_context = {
        "level1": {"level2": {"level3": ["value1", "value2"]}},
        "array": [1, 2, 3],
    }
    instruction = Instruction(
        content={"instruction": "Test", "context": [complex_context]},
        sender="user",
        recipient="assistant",
    )

    rendered = instruction.content.rendered
    assert isinstance(rendered, str)
    # Should include context in YAML format
    assert "level1" in rendered or "context" in rendered.lower()


def test_instruction_large_response_format():
    """Test large response_format structures"""
    large_format = {f"field_{i}": f"value_{i}" for i in range(20)}
    instruction = Instruction(
        content={"instruction": "Test", "response_format": large_format},
        sender="user",
        recipient="assistant",
    )

    rendered = instruction.content.rendered
    assert "```json" in rendered
    # Should include multiple fields
    assert "field_0" in rendered
    assert "field_19" in rendered


def test_from_dict_preserves_field_types():
    """Test from_dict preserves correct field types"""
    data = {
        "instruction": "Test",
        "context": [{"a": 1}, "string"],
        "tool_schemas": [{"name": "t1"}],
        "images": ["img.jpg"],
    }
    content = InstructionContent.from_dict(data)

    assert isinstance(content.prompt_context, list)
    assert isinstance(content.tool_schemas, list)
    assert isinstance(content.images, list)
    assert isinstance(content.instruction, str)


def test_instruction_model_fields_immutable_slots():
    """Test InstructionContent uses slots for efficiency"""
    content = InstructionContent(instruction="Test")

    # InstructionContent uses slots=True, so __dict__ should not exist
    assert not hasattr(content, "__dict__")


def test_with_updates_preserves_response_schema():
    """Regression: with_updates() must preserve the response schema.

    response_format/structure/_structure_instance are excluded from to_dict
    (types can't round-trip through JSON), so the generic with_updates — which
    goes through to_dict → constructor — used to silently drop the response
    schema. In chat prep the instruction is rebuilt via with_updates() to fold
    the system message into its guidance; dropping the schema there meant roled
    agents never saw the action_requests format and never called tools.
    """
    content = InstructionContent.from_dict(
        {
            "instruction": "do it",
            "tool_schemas": [{"type": "function", "function": {"name": "reader"}}],
            "response_format": SampleRequestModel,
        }
    )
    assert content._structure_instance is not None
    assert "name" in content.rendered  # schema field rendered

    updated = content.with_updates(guidance="SYSTEM PREFIX")
    assert updated._structure_instance is not None
    assert updated.response_format is SampleRequestModel
    assert "name" in updated.rendered  # schema survives the copy
    assert "reader" in updated.rendered  # tools survive too
    assert "SYSTEM PREFIX" in updated.rendered


def test_with_updates_explicit_response_format_none_clears_schema():
    """Passing response_format=None explicitly still clears it (chat prep relies
    on this to strip prior turns' schemas)."""
    content = InstructionContent.from_dict(
        {"instruction": "do it", "response_format": SampleRequestModel}
    )
    assert content._structure_instance is not None
    cleared = content.with_updates(response_format=None)
    assert cleared._structure_instance is None
