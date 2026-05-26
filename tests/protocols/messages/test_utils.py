# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import pytest
from pydantic import BaseModel

from lionagi.protocols.generic.element import Element
from lionagi.protocols.messages.base import MessageRole, validate_sender_recipient
from lionagi.protocols.messages.instruction import InstructionContent
from lionagi.protocols.messages.system import SystemContent


@pytest.fixture
def sample_element():
    class SampleElement(Element):
        """Sample element class for ID testing"""

        pass

    return SampleElement()


def test_systemcontent_from_dict_datetime_handling():
    """Test SystemContent.from_dict datetime handling"""
    message = "Test system message"

    # Without datetime
    content = SystemContent.from_dict({"system_message": message})
    assert content.system_message == message
    assert content.system_datetime is None

    # With datetime boolean True
    content = SystemContent.from_dict({"system_datetime": True})
    assert content.system_datetime is not None
    assert isinstance(content.system_datetime, str)

    # With datetime boolean False
    content = SystemContent.from_dict({"system_datetime": False})
    assert content.system_datetime is None

    # With custom datetime string
    custom_datetime = "2023-01-01"
    content = SystemContent.from_dict({"system_datetime": custom_datetime})
    assert content.system_datetime == custom_datetime


def test_json_structure_format_response_format():
    """Test JsonStructure._format_response_format static method"""
    from lionagi.protocols.structure.json_structure import JsonStructure

    response_format = {"name": "string", "age": "integer"}
    result = JsonStructure._format_response_format(response_format)

    assert "MUST RETURN JSON-PARSEABLE RESPONSE" in result
    assert "```json" in result
    assert "name" in result
    assert "age" in result

    # Test with None
    result = JsonStructure._format_response_format(None)
    assert result == ""

    # Test with empty dict
    result = JsonStructure._format_response_format({})
    assert result == ""


def test_instructioncontent_format_image_item():
    """Test InstructionContent._format_image_item static method"""
    # Base64 image
    image_id = "test_image_base64"
    detail = "low"
    result = InstructionContent._format_image_item(image_id, detail)

    assert result["type"] == "image_url"
    assert "data:image/jpeg;base64" in result["image_url"]["url"]
    assert result["image_url"]["detail"] == detail

    # HTTP URL
    http_url = "http://example.com/image.jpg"
    result = InstructionContent._format_image_item(http_url, "high")
    assert result["image_url"]["url"] == http_url
    assert result["image_url"]["detail"] == "high"

    # Data URL
    data_url = "data:image/png;base64,abc123"
    result = InstructionContent._format_image_item(data_url, "auto")
    assert result["image_url"]["url"] == data_url


def test_instructioncontent_format_image_content():
    """Test InstructionContent._format_image_content class method"""
    text = "Test text"
    images = ["image1_base64", "image2_base64"]
    detail = "low"

    result = InstructionContent._format_image_content(text, images, detail)

    assert isinstance(result, list)
    assert result[0]["type"] == "text"
    assert result[0]["text"] == text
    assert all(item["type"] == "image_url" for item in result[1:])
    assert len(result) == len(images) + 1


def test_instructioncontent_from_dict_with_request_model():
    """Test InstructionContent.from_dict with Pydantic models"""

    class RequestModel(BaseModel):
        name: str
        age: int

    # Test with BaseModel class (using response_format now)
    data = {"response_format": RequestModel, "instruction": "Test"}
    content = InstructionContent.from_dict(data)

    assert content.response_format == RequestModel
    assert content._structure_instance is not None
    assert content._structure_instance.base == RequestModel

    schema = content._structure_instance.request_schema()
    assert isinstance(schema, type)
    assert "name" in schema.model_fields
    assert "age" in schema.model_fields


def test_instructioncontent_from_dict_with_images():
    """Test InstructionContent.from_dict with images"""
    data = {
        "instruction": "Test",
        "images": ["image1"],
        "image_detail": "low",
    }
    content = InstructionContent.from_dict(data)

    assert content.images == ["image1"]
    assert content.image_detail == "low"


def test_validate_sender_recipient(sample_element):
    """Test sender/recipient validation"""
    # Test valid MessageRole enum
    assert validate_sender_recipient(MessageRole.SYSTEM) == MessageRole.SYSTEM
    assert validate_sender_recipient(MessageRole.USER) == MessageRole.USER

    # Test valid string roles
    assert validate_sender_recipient("system") == MessageRole.SYSTEM
    assert validate_sender_recipient("user") == MessageRole.USER
    assert validate_sender_recipient("assistant") == MessageRole.ASSISTANT
    assert validate_sender_recipient("action") == MessageRole.ACTION
    assert validate_sender_recipient("unset") == MessageRole.UNSET

    # Test None value
    assert validate_sender_recipient(None) == MessageRole.UNSET

    # Test with valid Element instance (returns ID)
    result = validate_sender_recipient(sample_element)
    assert result == sample_element.id

    # Test invalid values
    with pytest.raises(ValueError):
        validate_sender_recipient(123)

    with pytest.raises(ValueError):
        validate_sender_recipient({"invalid": "type"})
