"""Tests for quickstart documentation examples (installation.md, your-first-flow.md)."""

import pytest


# 1. Import lionagi
def test_import_lionagi():
    """Verify that `import lionagi` succeeds and exposes the version."""
    import lionagi

    assert hasattr(lionagi, "__version__")
    assert isinstance(lionagi.__version__, str)


# 2. __version__ is a string
def test_version_is_string():
    """lionagi.__version__ should be a non-empty string."""
    import lionagi

    assert isinstance(lionagi.__version__, str)
    assert len(lionagi.__version__) > 0


# 3. Core public exports
def test_core_exports():
    """The top-level package exposes Branch, iModel, and Session."""
    from lionagi import Branch, Session, iModel

    assert Branch is not None
    assert iModel is not None
    assert Session is not None


# 4. Minimal Branch construction
def test_branch_minimal_construction():
    """Branch() with no arguments should produce a valid instance."""
    from lionagi import Branch

    branch = Branch()
    assert branch is not None
    assert branch.id is not None


# 5. Branch with system prompt
def test_branch_with_system_prompt():
    """Branch(system='...') sets the system message."""
    from lionagi import Branch

    branch = Branch(system="You are a helpful assistant.")
    assert branch is not None
    # The system message should be stored in the message manager.
    assert len(branch.msgs.messages) > 0


# 6. iModel construction
def test_imodel_construction():
    """iModel can be constructed with explicit provider, model, and api_key."""
    from lionagi import iModel

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test")
    assert model is not None


# 7. communicate returns a response (mocked)
@pytest.mark.asyncio
async def test_communicate_returns_response(mocked_branch):
    """await branch.communicate('...') should return a response string."""
    result = await mocked_branch.communicate("Hello, world!")
    assert result is not None
    assert isinstance(result, str)
    assert len(result) > 0


# 8. Tool registration via Branch constructor
def test_tool_registration():
    """Branch(tools=[fn1, fn2]) registers callable tools properly."""
    from lionagi import Branch

    def calculate(expression: str) -> str:
        """Evaluate a mathematical expression and return the result."""
        return str(eval(expression))

    def lookup_constant(name: str) -> str:
        """Look up a mathematical or physical constant by name."""
        constants = {"pi": "3.14159", "e": "2.71828"}
        return constants.get(name.lower(), f"Unknown: {name}")

    branch = Branch(tools=[calculate, lookup_constant])

    # Both tools should be registered in the action manager registry.
    registry = branch.acts.registry
    assert "calculate" in registry
    assert "lookup_constant" in registry

    # operate should be a callable method on the branch.
    assert callable(branch.operate)
