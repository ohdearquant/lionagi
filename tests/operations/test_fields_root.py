"""Tests for lionagi.operations.fields imports, __getattr__ field generation, and __all__ exports."""

import threading

import pytest


class TestImports:
    """Test that all exports are importable."""

    def test_action_request_model_import(self):
        from lionagi.operations.fields import ActionRequestModel

        assert ActionRequestModel is not None
        assert isinstance(ActionRequestModel, type)

    def test_action_response_model_import(self):
        from lionagi.operations.fields import ActionResponseModel

        assert ActionResponseModel is not None
        assert isinstance(ActionResponseModel, type)

    def test_instruct_import(self):
        from lionagi.operations.fields import Instruct

        assert Instruct is not None
        assert isinstance(Instruct, type)

    def test_reason_import(self):
        from lionagi.operations.fields import Reason

        assert Reason is not None
        assert isinstance(Reason, type)

    def test_get_default_field_import(self):
        from lionagi.operations.fields import get_default_field

        assert get_default_field is not None
        assert callable(get_default_field)

    def test_action_requests_field_import(self):
        from lionagi.operations.fields import ACTION_REQUESTS_FIELD

        assert ACTION_REQUESTS_FIELD is not None

    def test_action_responses_field_import(self):
        from lionagi.operations.fields import ACTION_RESPONSES_FIELD

        assert ACTION_RESPONSES_FIELD is not None

    def test_action_required_field_import(self):
        from lionagi.operations.fields import ACTION_REQUIRED_FIELD

        assert ACTION_REQUIRED_FIELD is not None

    def test_instruct_field_import(self):
        from lionagi.operations.fields import INSTRUCT_FIELD

        assert INSTRUCT_FIELD is not None

    def test_list_instruct_field_model_import(self):
        from lionagi.operations.fields import LIST_INSTRUCT_FIELD_MODEL

        assert LIST_INSTRUCT_FIELD_MODEL is not None

    def test_reason_field_import(self):
        from lionagi.operations.fields import REASON_FIELD

        assert REASON_FIELD is not None

    def test_multiple_imports_same_object(self):
        import lionagi.operations.fields as fields_module

        first = fields_module.ActionRequestModel
        second = fields_module.ActionRequestModel
        assert first is second


class TestExports:
    """Test __all__ exports."""

    def test_all_exports_present(self):
        import lionagi.operations.fields as fields_module

        for name in fields_module.__all__:
            assert hasattr(fields_module, name), f"Missing: {name}"
            assert getattr(fields_module, name) is not None

    def test_all_exports_list(self):
        from lionagi.operations.fields import __all__

        expected = {
            "ACTION_REQUESTS_FIELD",
            "ACTION_RESPONSES_FIELD",
            "ACTION_REQUIRED_FIELD",
            "INSTRUCT_FIELD",
            "LIST_INSTRUCT_FIELD_MODEL",
            "REASON_FIELD",
            "ActionRequestModel",
            "ActionResponseModel",
            "Instruct",
            "Reason",
            "get_default_field",
        }
        assert set(__all__) == expected


class TestErrorHandling:
    """Test error handling for invalid imports."""

    def test_invalid_attribute_raises_attribute_error(self):
        import lionagi.operations.fields as fields_module

        with pytest.raises(AttributeError) as exc_info:
            _ = fields_module.NonExistentAttribute

        assert "has no attribute" in str(exc_info.value)

    def test_invalid_attribute_error_message(self):
        import lionagi.operations.fields as fields_module

        with pytest.raises(AttributeError) as exc_info:
            _ = fields_module.InvalidField

        error_msg = str(exc_info.value)
        assert "lionagi.operations.fields" in error_msg
        assert "InvalidField" in error_msg


class TestFieldGeneration:
    """Test dynamic field generation using get_default_field."""

    def test_action_requests_field_uses_correct_params(self):
        from lionagi.operations.fields import ACTION_REQUESTS_FIELD, get_default_field

        manual_field = get_default_field("action_requests")
        assert type(ACTION_REQUESTS_FIELD) == type(manual_field)

    def test_list_instruct_field_uses_listable_param(self):
        from lionagi.operations.fields import (
            LIST_INSTRUCT_FIELD_MODEL,
            get_default_field,
        )

        manual_field = get_default_field("instruct", listable=True)
        assert type(LIST_INSTRUCT_FIELD_MODEL) == type(manual_field)

    def test_all_field_constants_created(self):
        from lionagi.operations.fields import (
            ACTION_REQUESTS_FIELD,
            ACTION_REQUIRED_FIELD,
            ACTION_RESPONSES_FIELD,
            INSTRUCT_FIELD,
            LIST_INSTRUCT_FIELD_MODEL,
            REASON_FIELD,
        )

        for field in [
            ACTION_REQUESTS_FIELD,
            ACTION_RESPONSES_FIELD,
            ACTION_REQUIRED_FIELD,
            INSTRUCT_FIELD,
            LIST_INSTRUCT_FIELD_MODEL,
            REASON_FIELD,
        ]:
            assert field is not None

    def test_classes_and_functions_are_correct_types(self):
        from lionagi.operations.fields import (
            ActionRequestModel,
            ActionResponseModel,
            Instruct,
            Reason,
            get_default_field,
        )

        assert isinstance(ActionRequestModel, type)
        assert isinstance(ActionResponseModel, type)
        assert isinstance(Instruct, type)
        assert isinstance(Reason, type)
        assert callable(get_default_field)


def test_first_concurrent_field_access_returns_one_object(monkeypatch):
    import lionagi.operations.fields as fields_module

    name = "LIST_INSTRUCT_FIELD_MODEL"
    fields_module.__dict__.pop(name, None)
    first_entered = threading.Event()
    second_attempting = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    built: list[object] = []
    results: list[object] = []

    def build(_field_name, **_kwargs):
        value = object()
        built.append(value)
        if len(built) == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
        else:
            second_entered.set()
        return value

    monkeypatch.setattr(fields_module, "get_default_field", build)

    def read_constant(attempting=None):
        if attempting is not None:
            attempting.set()
        results.append(getattr(fields_module, name))

    first = threading.Thread(target=read_constant)
    second = threading.Thread(target=read_constant, args=(second_attempting,))
    try:
        first.start()
        assert first_entered.wait(timeout=2)
        second.start()
        assert second_attempting.wait(timeout=2)
        assert not second_entered.wait(timeout=0.5)
        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)

        assert not first.is_alive()
        assert not second.is_alive()
        assert len(built) == 1
        assert len(results) == 2
        assert results[0] is results[1]
    finally:
        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)
        fields_module.__dict__.pop(name, None)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
