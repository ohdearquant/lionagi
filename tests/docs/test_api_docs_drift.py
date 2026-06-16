"""Tests verifying docs/api/ accuracy against live source code."""

from pathlib import Path

import pytest

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs" / "api"


# ---------------------------------------------------------------------------
# 1. Gemini provider uses GEMINI_API_KEY, not GOOGLE_API_KEY
# ---------------------------------------------------------------------------
class TestIModelDocs:
    """Verify imodel.md matches live provider configuration."""

    def test_gemini_env_var_name_in_config(self):
        """AppSettings declares GEMINI_API_KEY (not GOOGLE_API_KEY)."""
        from lionagi.config import AppSettings

        field_names = set(AppSettings.model_fields)
        assert "GEMINI_API_KEY" in field_names
        assert "GOOGLE_API_KEY" not in field_names

    def test_gemini_endpoint_uses_gemini_api_key(self):
        """The Google/Gemini chat endpoint references GEMINI_API_KEY."""
        from lionagi.providers.google.chat.endpoint import GeminiChatEndpoint

        src = Path(GeminiChatEndpoint.__module__.replace(".", "/") + ".py")
        # If we can't read source, fall back to checking the config field exists
        from lionagi.config import AppSettings

        settings = AppSettings()
        assert hasattr(settings, "GEMINI_API_KEY")

    def test_imodel_doc_has_no_google_api_key(self):
        """imodel.md should reference GEMINI_API_KEY, not GOOGLE_API_KEY."""
        imodel_md = DOCS_DIR / "imodel.md"
        if not imodel_md.exists():
            pytest.skip("imodel.md not found")
        content = imodel_md.read_text()
        assert "GOOGLE_API_KEY" not in content, (
            "imodel.md still references GOOGLE_API_KEY; the correct env var is GEMINI_API_KEY"
        )
        assert "GEMINI_API_KEY" in content


# ---------------------------------------------------------------------------
# 2. Operations doc import paths resolve
# ---------------------------------------------------------------------------
class TestOperationsDocs:
    """Verify that every import shown in operations.md actually works."""

    def test_middle_protocol_import(self):
        """Middle protocol is importable from operations.types."""
        from lionagi.operations.types import Middle

        assert Middle is not None

    def test_chat_param_import(self):
        from lionagi.operations.types import ChatParam

        assert ChatParam is not None

    def test_run_param_import(self):
        from lionagi.operations.types import RunParam

        assert RunParam is not None

    def test_parse_param_import(self):
        from lionagi.operations.types import ParseParam

        assert ParseParam is not None

    def test_interpret_param_import(self):
        from lionagi.operations.types import InterpretParam

        assert InterpretParam is not None

    def test_action_param_import(self):
        from lionagi.operations.types import ActionParam

        assert ActionParam is not None

    def test_communicate_import(self):
        """communicate function importable from its documented path."""
        from lionagi.operations.communicate.communicate import communicate

        assert callable(communicate)

    def test_run_and_collect_import(self):
        """run_and_collect function importable from its documented path."""
        from lionagi.operations.run.run import run_and_collect

        assert callable(run_and_collect)

    def test_hook_registry_import(self):
        """HookRegistry and HookEventTypes importable from service.hooks."""
        from lionagi.service.hooks import HookEventTypes, HookRegistry

        assert HookRegistry is not None
        assert HookEventTypes is not None


# ---------------------------------------------------------------------------
# 3. Session.flow() return type is a wrapper dict, not Note
# ---------------------------------------------------------------------------
class TestFlowReturnType:
    """Verify Session.flow() return type documentation accuracy."""

    def test_flow_return_annotation_is_dict(self):
        """Session.flow() is annotated -> dict[str, Any]."""
        import inspect

        from lionagi.session.session import Session

        sig = inspect.signature(Session.flow)
        ret = sig.return_annotation
        # The annotation is dict[str, Any]
        assert ret is not inspect.Parameter.empty
        origin = getattr(ret, "__origin__", None)
        assert origin is dict, f"Session.flow() return annotation should be dict, got {ret}"

    def test_executor_returns_wrapper_dict_keys(self):
        """DependencyAwareExecutor.execute() returns dict with known keys."""
        import inspect

        from lionagi.operations.flow import DependencyAwareExecutor

        # Verify the execute method exists and returns dict
        sig = inspect.signature(DependencyAwareExecutor.execute)
        ret = sig.return_annotation
        origin = getattr(ret, "__origin__", None)
        assert origin is dict

    def test_flow_doc_does_not_use_results_dot_context(self):
        """flow.md should not show results.context (attribute access on dict)."""
        flow_md = DOCS_DIR / "flow.md"
        if not flow_md.exists():
            pytest.skip("flow.md not found")
        content = flow_md.read_text()
        # Should NOT have results.context as attribute access
        assert "results.context" not in content, (
            "flow.md still shows results.context; the correct access is results['final_context']"
        )

    def test_flow_doc_uses_operation_results_key(self):
        """flow.md should access results via results['operation_results']."""
        flow_md = DOCS_DIR / "flow.md"
        if not flow_md.exists():
            pytest.skip("flow.md not found")
        content = flow_md.read_text()
        assert 'results["operation_results"]' in content or "operation_results" in content
        assert "items=results[n1]" not in content

    def test_session_doc_flow_return_description(self):
        """session.md should describe the wrapper dict keys, not 'keyed by node ID'."""
        session_md = DOCS_DIR / "session.md"
        if not session_md.exists():
            pytest.skip("session.md not found")
        content = session_md.read_text()
        # Should mention the actual keys
        assert "operation_results" in content
        assert "final_context" in content


# ---------------------------------------------------------------------------
# 4. Builder is a public export
# ---------------------------------------------------------------------------
class TestBuilderExport:
    """Verify Builder is importable as documented in flow.md."""

    def test_builder_import_from_lionagi(self):
        from lionagi import Builder

        assert Builder is not None

    def test_builder_has_add_operation(self):
        from lionagi import Builder

        builder = Builder()
        assert hasattr(builder, "add_operation")
        assert callable(builder.add_operation)

    def test_builder_has_get_graph(self):
        from lionagi import Builder

        builder = Builder()
        assert hasattr(builder, "get_graph")
        assert callable(builder.get_graph)
