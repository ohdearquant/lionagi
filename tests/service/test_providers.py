import pytest

from lionagi.service.providers import (
    CLI_PROVIDERS,
    PROVIDER_BYPASS_KWARGS,
    PROVIDER_YOLO_KWARGS,
    _validate_provider_permission_tables,
)


def test_cli_providers_have_permission_table_entries():
    assert CLI_PROVIDERS <= PROVIDER_YOLO_KWARGS.keys()
    assert CLI_PROVIDERS <= PROVIDER_BYPASS_KWARGS.keys()


def test_provider_permission_table_validation_names_missing_provider():
    with pytest.raises(RuntimeError) as exc_info:
        _validate_provider_permission_tables(
            {"covered", "missing-provider"},
            {"covered": {}},
            {"covered": {}, "missing-provider": {}},
        )
    assert "missing-provider" in str(exc_info.value)
    assert "PROVIDER_YOLO_KWARGS" in str(exc_info.value)


@pytest.mark.parametrize(
    ("alias", "sibling"),
    [
        ("claude-code", "claude_code"),
        ("gemini-cli", "gemini_code"),
        ("gemini_cli", "gemini_code"),
    ],
)
def test_permission_aliases_match_provider_family(alias, sibling):
    assert PROVIDER_YOLO_KWARGS[alias] == PROVIDER_YOLO_KWARGS[sibling]
    assert PROVIDER_BYPASS_KWARGS[alias] == PROVIDER_BYPASS_KWARGS[sibling]
