"""Shared fixtures for documentation example tests."""

import pytest

from lionagi.testing import LionAGIMockFactory


@pytest.fixture
def mocked_branch():
    """Branch with mocked iModel for testing doc examples that call LLMs."""
    return LionAGIMockFactory.create_mocked_branch(
        name="DocTestBranch",
        user="doc_tester",
        response="mocked doc response",
    )


@pytest.fixture
def mocked_branch_structured():
    """Branch with mocked iModel returning a JSON-like dict response."""
    return LionAGIMockFactory.create_mocked_branch(
        name="StructuredBranch",
        user="doc_tester",
        response={
            "title": "Test",
            "year": 2025,
            "rating": 8.5,
            "summary": "A test summary",
            "pros": ["pro1"],
            "cons": ["con1"],
            "key_points": ["point1", "point2"],
            "sentiment": "positive",
            "label": "positive",
            "score": 0.95,
            "explanation": "test explanation",
            "confidence": 0.9,
            "findings": ["finding1"],
            "sources": ["source1"],
            "name": "John",
            "email": "john@example.com",
            "phone": "555-1234",
        },
    )
