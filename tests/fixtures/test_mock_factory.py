"""
Simple Phase 1 Infrastructure Validation

Basic tests to validate infrastructure components work without complex fixtures.
"""

import asyncio

import pytest

from tests.fixtures.loaders import load_test_data
from tests.utils.helpers import AsyncTestHelpers, ValidationHelpers
from tests.utils.mock_factory import LionAGIMockFactory


def test_mock_factory_creates_branch():
    """Test that mock factory can create a basic branch."""
    branch = LionAGIMockFactory.create_mocked_branch(
        name="SimpleTestBranch", response="Simple test response"
    )

    # Basic validations
    assert branch is not None
    assert branch.name == "SimpleTestBranch"
    assert branch.chat_model is not None


def test_mock_factory_creates_imodel():
    """Test that mock factory can create an iModel."""
    imodel = LionAGIMockFactory.create_mocked_imodel(
        provider="openai", model="gpt-4o-mini", response="Test response"
    )

    assert imodel is not None
    # iModel attributes may be different - let's just check it was created
    assert hasattr(imodel, "invoke")  # Should have invoke method


@pytest.mark.asyncio
async def test_async_branch_communication():
    """Test async communication with mocked branch."""
    branch = LionAGIMockFactory.create_mocked_branch(
        response="Async communication test"
    )

    # Test async communication
    result = await branch.communicate("Test message", skip_validation=True)
    assert result == "Async communication test"


def test_test_data_loading():
    """Test that test data loading works."""
    # Test loading conversation data
    conversations = load_test_data("sample_conversations")
    assert "basic_chat" in conversations
    assert "messages" in conversations["basic_chat"]

    # Test loading API responses
    api_responses = load_test_data("api_responses")
    assert "successful_chat_response" in api_responses

    # Test loading error scenarios
    error_scenarios = load_test_data("error_scenarios")
    assert "api_rate_limit_error" in error_scenarios


@pytest.mark.asyncio
async def test_async_helpers():
    """Test async helper utilities."""
    # Test simple async condition
    condition_met = False

    def check_condition():
        return condition_met

    async def set_condition():
        await asyncio.sleep(0.05)
        nonlocal condition_met
        condition_met = True

    # Start task to set condition
    task = asyncio.create_task(set_condition())

    # Wait for condition
    await AsyncTestHelpers.assert_eventually(
        check_condition, timeout=1.0, interval=0.01
    )

    await task  # Ensure task completes


def test_validation_helpers_node():
    """Test validation helpers with Node objects."""
    branch = LionAGIMockFactory.create_mocked_branch()

    # Test node validation - should not raise exception
    ValidationHelpers.assert_valid_node(branch)


def test_validation_helpers_api_response():
    """Test validation helpers with API responses."""
    api_call = LionAGIMockFactory.create_api_calling_mock()

    # Test API response validation
    ValidationHelpers.assert_api_response_structure(api_call)


def test_error_response_creation():
    """Test error response mock creation."""
    error_response = LionAGIMockFactory.create_error_response_mock(
        error_message="Test error", error_code="test_error"
    )

    assert error_response is not None
    # The error response should have the mocked data
    assert error_response.execution.response["error"]["message"] == "Test error"


@pytest.mark.asyncio
async def test_end_to_end_simple():
    """Simple end-to-end test using infrastructure components."""
    # Create branch with custom response
    branch = LionAGIMockFactory.create_mocked_branch(
        name="E2EBranch", response="End-to-end test response"
    )

    # Validate branch
    ValidationHelpers.assert_valid_node(branch)

    # Test communication
    result = await branch.communicate("Test query", skip_validation=True)
    assert result == "End-to-end test response"

    # Test that we can create multiple responses
    imodel = LionAGIMockFactory.create_mocked_imodel(
        responses=["First", "Second", "Third"]
    )

    # Test sequence of responses
    call1 = await imodel.invoke()
    call2 = await imodel.invoke()
    call3 = await imodel.invoke()

    assert call1.execution.response == "First"
    assert call2.execution.response == "Second"
    assert call3.execution.response == "Third"


if __name__ == "__main__":
    # Allow running this test file directly
    pytest.main([__file__, "-v"])
