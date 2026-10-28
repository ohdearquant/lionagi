# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""An effort-less codex spawn must not inherit an over-ceiling global effort.

With no explicit effort lionagi used to emit no ``model_reasoning_effort``
override, so the codex CLI fell back to the global config's value — tuned for
the config's own default model. A global value above the resolved model's
ceiling is a 400 at the API (a global ``ultra`` reaching gpt-5.3-codex-spark,
whose ceiling is ``xhigh``). The builders now read that global value
themselves and emit it clamped to the resolved model.
"""

import pytest

from lionagi.cli._providers import build_chat_model, build_imodel_from_spec


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _write_global(codex_home, body: str) -> None:
    (codex_home / "config.toml").write_text(body)


class TestGlobalEffortIsClampedToTheResolvedModel:
    def test_over_ceiling_global_effort_is_clamped_for_a_capped_model(self, codex_home):
        """The defect: global ultra + spark must emit xhigh, not nothing."""
        _write_global(codex_home, 'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "ultra"\n')
        m = build_chat_model(
            provider="codex",
            model="gpt-5.3-codex-spark",
            yolo=True,
            verbose=False,
            theme=None,
            effort=None,
        )
        assert m.endpoint.config.kwargs["reasoning_effort"] == "xhigh"

    def test_global_effort_within_ceiling_is_kept(self, codex_home):
        _write_global(codex_home, 'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "ultra"\n')
        m = build_chat_model(
            provider="codex",
            model="gpt-5.6-sol",
            yolo=True,
            verbose=False,
            theme=None,
            effort=None,
        )
        assert m.endpoint.config.kwargs["reasoning_effort"] == "ultra"

    def test_a_max_only_model_clamps_the_global_ultra_to_max(self, codex_home):
        """gpt-5.6-luna's ceiling is max, not ultra — the arm between the
        xhigh-capped and ultra-capable tables, which neither other case reaches."""
        _write_global(codex_home, 'model_reasoning_effort = "ultra"\n')
        m = build_chat_model(
            provider="codex",
            model="gpt-5.6-luna",
            yolo=True,
            verbose=False,
            theme=None,
            effort=None,
        )
        assert m.endpoint.config.kwargs["reasoning_effort"] == "max"

    def test_an_uppercase_global_value_is_normalized_before_clamping(self, codex_home):
        """The clamp tables key on lowercase; an un-normalized 'ULTRA' would
        miss every ceiling and be emitted verbatim."""
        _write_global(codex_home, 'model_reasoning_effort = "ULTRA"\n')
        m = build_chat_model(
            provider="codex",
            model="gpt-5.3-codex-spark",
            yolo=True,
            verbose=False,
            theme=None,
            effort=None,
        )
        assert m.endpoint.config.kwargs["reasoning_effort"] == "xhigh"

    def test_an_ordinary_global_value_passes_through_unclamped(self, codex_home):
        """Only max/ultra are ceiling-bound; anything below must survive."""
        _write_global(codex_home, 'model_reasoning_effort = "medium"\n')
        m = build_chat_model(
            provider="codex",
            model="gpt-5.3-codex-spark",
            yolo=True,
            verbose=False,
            theme=None,
            effort=None,
        )
        assert m.endpoint.config.kwargs["reasoning_effort"] == "medium"

    def test_no_global_value_emits_nothing(self, codex_home):
        """codex's own built-in default stays in charge when the global config is silent."""
        spec = build_chat_model(
            provider="codex",
            model="gpt-5.3-codex-spark",
            yolo=False,
            verbose=False,
            theme=None,
            effort=None,
        )
        # No flags and no effort: the builder still returns the bare spec string.
        assert spec == "codex/gpt-5.3-codex-spark"

    def test_no_global_value_emits_no_effort_kwarg_on_the_request(self, codex_home):
        """The bare-string case above is a proxy: it proves the builder took the
        no-extras path, not that the REQUEST carries no effort. Force an iModel
        with an unrelated flag and read the kwargs the provider actually gets."""
        m = build_chat_model(
            provider="codex",
            model="gpt-5.3-codex-spark",
            yolo=True,
            verbose=False,
            theme=None,
            effort=None,
        )
        assert "reasoning_effort" not in m.endpoint.config.kwargs

    def test_an_unknown_model_emits_no_effort_kwarg_on_the_request(self, codex_home):
        _write_global(codex_home, 'model_reasoning_effort = "ultra"\n')
        m = build_chat_model(
            provider="codex",
            model="gpt-9.9-future",
            yolo=True,
            verbose=False,
            theme=None,
            effort=None,
        )
        assert "reasoning_effort" not in m.endpoint.config.kwargs

    def test_an_unreadable_global_config_emits_no_effort_kwarg_on_the_request(self, codex_home):
        _write_global(codex_home, "model_reasoning_effort = [not toml")
        m = build_chat_model(
            provider="codex",
            model="gpt-5.3-codex-spark",
            yolo=True,
            verbose=False,
            theme=None,
            effort=None,
        )
        assert "reasoning_effort" not in m.endpoint.config.kwargs

    def test_an_unknown_model_gets_nothing_never_a_guessed_ceiling(self, codex_home):
        """The ceiling tables are hand-typed; a model they don't know has no
        known ceiling, so the default must emit nothing and leave codex's own
        default in charge — never forward a value tuned for another model."""
        _write_global(codex_home, 'model_reasoning_effort = "ultra"\n')
        spec = build_chat_model(
            provider="codex",
            model="gpt-9.9-future",
            yolo=False,
            verbose=False,
            theme=None,
            effort=None,
        )
        assert spec == "codex/gpt-9.9-future"

    def test_explicit_effort_wins_over_the_global_value(self, codex_home):
        _write_global(codex_home, 'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "ultra"\n')
        m = build_chat_model(
            provider="codex",
            model="gpt-5.3-codex-spark",
            yolo=True,
            verbose=False,
            theme=None,
            effort="low",
        )
        assert m.endpoint.config.kwargs["reasoning_effort"] == "low"

    def test_a_profile_pinning_effort_suppresses_the_global_default(self, codex_home):
        """A profile's model_reasoning_effort already rides config_overrides;
        emitting a second value beside it would let the two disagree."""
        _write_global(codex_home, 'model_reasoning_effort = "ultra"\n')
        (codex_home / "pinned.config.toml").write_text(
            'model = "gpt-5.3-codex-spark"\nmodel_reasoning_effort = "medium"\n'
        )
        m = build_chat_model(
            provider="codex",
            model="pinned",
            yolo=True,
            verbose=False,
            theme=None,
            effort=None,
        )
        assert "reasoning_effort" not in m.endpoint.config.kwargs
        assert m.endpoint.config.kwargs["config_overrides"]["model_reasoning_effort"] == "medium"

    def test_build_imodel_from_spec_clamps_the_same_way(self, codex_home, monkeypatch):
        _write_global(codex_home, 'model_reasoning_effort = "ultra"\n')
        import lionagi.cli._providers as pmod
        from lionagi.testing import IModelKwargCaptor

        captor = IModelKwargCaptor.fresh()
        monkeypatch.setattr(pmod, "iModel", captor)

        build_imodel_from_spec("codex/gpt-5.3-codex-spark", yolo=True)

        assert len(captor.captures) == 1
        assert captor.captures[0].get("reasoning_effort") == "xhigh"

    def test_an_unreadable_global_config_falls_back_to_emitting_nothing(self, codex_home):
        _write_global(codex_home, "model_reasoning_effort = [not toml")
        spec = build_chat_model(
            provider="codex",
            model="gpt-5.3-codex-spark",
            yolo=False,
            verbose=False,
            theme=None,
            effort=None,
        )
        assert spec == "codex/gpt-5.3-codex-spark"

    def test_a_non_utf8_global_config_falls_back_to_emitting_nothing(self, codex_home):
        """read_text() decodes; invalid UTF-8 raises UnicodeDecodeError, which
        is not an OSError — an uncaught one crashes the spawn instead of
        falling back to codex's own default."""
        (codex_home / "config.toml").write_bytes(b"\xff\xfe\xfd")
        spec = build_chat_model(
            provider="codex",
            model="gpt-5.3-codex-spark",
            yolo=False,
            verbose=False,
            theme=None,
            effort=None,
        )
        assert spec == "codex/gpt-5.3-codex-spark"

    @pytest.mark.parametrize("target", ["gpt-6-sol", "gpt-6.1-terra"])
    def test_minimal_global_effort_is_omitted_for_a_model_without_minimal(self, codex_home, target):
        _write_global(codex_home, 'model_reasoning_effort = "minimal"\n')
        model = build_chat_model(
            provider="codex",
            model=target,
            yolo=True,
            verbose=False,
            theme=None,
            effort=None,
        )
        assert "reasoning_effort" not in model.endpoint.config.kwargs

    def test_permission_error_checking_global_config_falls_back_to_no_effort(
        self, codex_home, monkeypatch
    ):
        from pathlib import Path

        from lionagi.providers.openai._codex_profile import global_codex_reasoning_effort

        config_path = codex_home / "config.toml"
        _write_global(codex_home, 'model_reasoning_effort = "high"\n')
        real_is_file = Path.is_file

        def is_file(path):
            if path == config_path:
                raise PermissionError("config is not readable")
            return real_is_file(path)

        monkeypatch.setattr(Path, "is_file", is_file)
        assert global_codex_reasoning_effort({"CODEX_HOME": str(codex_home)}) is None


class TestADeeplyNestedGlobalConfigReadsAsNoEffort:
    """A global config nested past the parser's recursion limit must not crash the read.

    The judge path reads the global config whenever the effort is unset, before
    any containment check, so a RecursionError here escapes as an engine error
    instead of the "no effort" answer an unreadable config gets.
    """

    def test_a_deeply_nested_config_answers_no_effort_without_raising(self, codex_home):
        from lionagi.providers.openai._codex_profile import global_codex_reasoning_effort

        _write_global(codex_home, "x = " + "[" * 3000 + "]" * 3000 + "\n")

        assert global_codex_reasoning_effort({"CODEX_HOME": str(codex_home)}) is None

    def test_an_effortless_build_over_a_deeply_nested_config_emits_no_effort(self, codex_home):
        """The read the builder makes must answer None, not raise: the builder
        reads the global config whenever the effort is unset."""
        _write_global(codex_home, "x = " + "[" * 3000 + "]" * 3000 + "\n")

        model = build_chat_model(
            provider="codex",
            model="gpt-5.3-codex-spark",
            yolo=True,
            verbose=False,
            theme=None,
            effort=None,
        )

        assert "reasoning_effort" not in model.endpoint.config.kwargs
