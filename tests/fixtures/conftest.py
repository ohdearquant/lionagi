"""
Shared Fixtures for LionAGI Test Suite

Provides pytest fixtures that can be used across all test modules.
"""

import pytest

from tests.utils.helpers import AsyncTestHelpers, TestDataHelpers, ValidationHelpers
from tests.utils.mock_factory import LionAGIMockFactory

from .loaders import TestDataLoader


@pytest.fixture
def mock_factory():
    """Provide LionAGI mock factory for tests."""
    return LionAGIMockFactory


@pytest.fixture
def async_helpers():
    """Provide async testing helpers."""
    return AsyncTestHelpers


@pytest.fixture
def validation_helpers():
    """Provide validation helpers."""
    return ValidationHelpers


@pytest.fixture
def test_data_helpers():
    """Provide test data helpers."""
    return TestDataHelpers


@pytest.fixture
def test_data_loader():
    """Provide test data loader."""
    return TestDataLoader()


@pytest.fixture
def sample_conversation_data(test_data_loader):
    """Load sample conversation data."""
    return test_data_loader.get_conversation_data("basic_chat")


@pytest.fixture
def sample_api_responses(test_data_loader):
    """Load sample API response data."""
    return test_data_loader.load_json("api_responses")


@pytest.fixture
def sample_error_scenarios(test_data_loader):
    """Load sample error scenarios."""
    return test_data_loader.load_json("error_scenarios")


@pytest.fixture
def mocked_branch(mock_factory):
    """Create a standard mocked branch for testing."""
    return mock_factory.create_mocked_branch()


@pytest.fixture
def mocked_branch_with_custom_response(mock_factory):
    """Factory for creating mocked branches with custom responses."""

    def _create_branch(response="custom test response", **kwargs):
        return mock_factory.create_mocked_branch(response=response, **kwargs)

    return _create_branch


@pytest.fixture
def mocked_error_branch(mock_factory):
    """Create a mocked branch that returns error responses."""
    error_response = mock_factory.create_error_response_mock(
        error_message="Test API Error", error_code="test_error"
    )
    return mock_factory.create_mocked_branch(response=error_response)


@pytest.fixture
def test_messages(test_data_helpers):
    """Create standard test messages."""
    return test_data_helpers.create_test_messages()


@pytest.fixture
def test_payload(test_data_helpers):
    """Create standard test API payload."""
    return test_data_helpers.create_test_payload()


@pytest.fixture(scope="session")
def performance_benchmark():
    """Fixture for performance benchmarking across test session."""
    benchmarks = {}

    def record_benchmark(test_name: str, duration: float, memory_usage: float = None):
        benchmarks[test_name] = {
            "duration": duration,
            "memory_usage": memory_usage,
        }

    def get_benchmarks():
        return benchmarks.copy()

    # Return object with methods
    class BenchmarkRecorder:
        def record(self, test_name: str, duration: float, memory_usage: float = None):
            record_benchmark(test_name, duration, memory_usage)

        def get_all(self):
            return get_benchmarks()

    return BenchmarkRecorder()
