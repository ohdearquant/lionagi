# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Every seam that builds a codex request from an unset effort must clamp.

The defect this covers is one the CLI builders had and the other construction
seams shared: with no explicit effort, nothing was emitted, so the codex CLI
fell back to the global config's ``model_reasoning_effort`` — a value tuned for
that config's own default model. Over the resolved model's ceiling, that is a
400. Fixing only the reported route leaves the same defect on every other
route, so each seam gets an arm here.
"""

import pytest
from pydantic import ValidationError

from lionagi.service.providers import codex_unset_effort_default, reclamp_effort_for_model


@pytest.fixture
def global_ultra(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(
        'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "ultra"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    return tmp_path


class TestTheStudioOperatorSeam:
    def test_an_effortless_codex_turn_gets_the_clamped_global_default(self, global_ultra):
        from lionagi.studio.operator.engine import _apply_operator_effort

        model_kwargs: dict = {}
        _apply_operator_effort("codex", "gpt-5.3-codex-spark", None, model_kwargs)
        assert model_kwargs["reasoning_effort"] == "xhigh"

    def test_an_explicit_effort_still_wins(self, global_ultra):
        from lionagi.studio.operator.engine import _apply_operator_effort

        model_kwargs: dict = {}
        _apply_operator_effort("codex", "gpt-5.3-codex-spark", "low", model_kwargs)
        assert model_kwargs["reasoning_effort"] == "low"

    def test_a_non_codex_provider_is_untouched_when_effort_is_unset(self, global_ultra):
        from lionagi.studio.operator.engine import _apply_operator_effort

        model_kwargs: dict = {}
        _apply_operator_effort("claude_code", "sonnet", None, model_kwargs)
        assert model_kwargs == {}


class TestTheGenericAgentFactorySeam:
    async def test_an_effortless_codex_spec_carries_the_clamped_default(self, global_ultra):
        from lionagi.agent.factory import create_agent
        from lionagi.agent.spec import AgentSpec

        # AgentSpec.coding() supplies effort="high" by default; effort=None is
        # what a caller that states no opinion actually produces.
        branch = await create_agent(
            AgentSpec.coding(model="codex/gpt-5.3-codex-spark", effort=None),
            load_settings=False,
        )
        assert branch.chat_model.endpoint.config.kwargs["reasoning_effort"] == "xhigh"

    async def test_an_explicit_spec_effort_still_wins(self, global_ultra):
        from lionagi.agent.factory import create_agent
        from lionagi.agent.spec import AgentSpec

        branch = await create_agent(
            AgentSpec.coding(model="codex/gpt-5.3-codex-spark", effort="low"),
            load_settings=False,
        )
        assert branch.chat_model.endpoint.config.kwargs["reasoning_effort"] == "low"


class TestReclampOnModelChange:
    def test_an_effort_resolved_for_another_model_is_reclamped(self):
        """A sol-derived ultra replayed on spark is the 400 this guards."""
        assert reclamp_effort_for_model("codex", "ultra", "gpt-5.3-codex-spark") == "xhigh"

    @pytest.mark.parametrize("alias", ["openai-codex", "Codex"])
    def test_an_alias_reclamps_as_the_provider_it_names(self, alias):
        assert reclamp_effort_for_model(alias, "ultra", "gpt-5.3-codex-spark") == "xhigh"

    def test_a_within_ceiling_effort_survives_the_reclamp(self):
        assert reclamp_effort_for_model("codex", "ultra", "gpt-5.6-sol") == "ultra"

    def test_claude_models_reclamp_on_their_own_table(self):
        assert reclamp_effort_for_model("claude_code", "xhigh", "claude-sonnet-4-5") == "high"
        assert reclamp_effort_for_model("claude_code", "xhigh", "sonnet-5-5") == "xhigh"
        assert reclamp_effort_for_model("claude_code", "xhigh", "opus-5") == "xhigh"

    def test_an_unset_effort_stays_unset(self):
        assert reclamp_effort_for_model("codex", None, "gpt-5.3-codex-spark") is None

    def test_a_provider_without_an_effort_table_is_untouched(self):
        assert reclamp_effort_for_model("gemini_code", "ultra", "gemini-3.5-flash") == "ultra"


class TestTheRequestModelBackstop:
    """The seam every codex caller shares, however it built its iModel.

    The per-caller arms above cover the routes that resolve an effort eagerly,
    which is what makes the persisted agent config and the operator's model
    catalog honest. They cannot cover a caller that never asks — a bare
    ``Branch()`` — so the request model itself
    resolves an unset effort as well.
    """

    def test_an_effortless_request_carries_the_clamped_global_default(self, global_ultra):
        from lionhub.providers.openai.codex import CodexCodeRequest

        req = CodexCodeRequest(prompt="hi", model="gpt-5.3-codex-spark")
        assert req.reasoning_effort == "xhigh"

    def test_an_explicit_request_effort_still_wins(self, global_ultra):
        from lionhub.providers.openai.codex import CodexCodeRequest

        req = CodexCodeRequest(prompt="hi", model="gpt-5.3-codex-spark", reasoning_effort="low")
        assert req.reasoning_effort == "low"

    def test_a_config_override_pin_is_left_to_the_cli(self, global_ultra):
        from lionhub.providers.openai.codex import CodexCodeRequest

        req = CodexCodeRequest(
            prompt="hi",
            model="gpt-5.3-codex-spark",
            config_overrides={"model_reasoning_effort": "medium"},
        )
        assert req.reasoning_effort is None

    def test_no_global_default_means_no_derived_effort(self, tmp_path, monkeypatch):
        (tmp_path / "config.toml").write_text('model = "gpt-5.6-sol"\n')
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        from lionhub.providers.openai.codex import CodexCodeRequest

        req = CodexCodeRequest(prompt="hi", model="gpt-5.3-codex-spark")
        assert req.reasoning_effort is None

    def test_the_childs_own_env_decides_which_config_is_read(self, tmp_path, monkeypatch):
        """`env` replaces the child environment, so it names the config codex reads."""
        ambient = tmp_path / "ambient"
        ambient.mkdir()
        (ambient / "config.toml").write_text('model_reasoning_effort = "ultra"\n')
        childs_own = tmp_path / "child"
        childs_own.mkdir()
        (childs_own / "config.toml").write_text('model = "gpt-5.6-sol"\n')
        monkeypatch.setenv("CODEX_HOME", str(ambient))

        from lionhub.providers.openai.codex import CodexCodeRequest

        # The child reads its own empty-of-effort config: clamping against a
        # file it never opens would be an answer about the wrong process.
        req = CodexCodeRequest(
            prompt="hi",
            model="gpt-5.3-codex-spark",
            env={"CODEX_HOME": str(childs_own)},
        )
        assert req.reasoning_effort is None

    def test_the_childs_own_env_also_decides_which_profile_is_read(self, tmp_path, monkeypatch):
        """The same question one step earlier: which file names the model.

        Effort resolution already read the child's env. Profile resolution ran
        before it and read OURS, so a request carrying a replacement env picked
        the profile out of a directory the child never opens and put a
        different model on the command line -- the model is the one thing a
        profile exists to decide, so this is not a degraded default but the
        wrong process being run.
        """
        ambient = tmp_path / "ambient"
        ambient.mkdir()
        (ambient / "work.config.toml").write_text('model = "gpt-5.6-sol"\n')
        childs_own = tmp_path / "child"
        childs_own.mkdir()
        (childs_own / "work.config.toml").write_text('model = "gpt-5.3-codex-spark"\n')
        monkeypatch.setenv("CODEX_HOME", str(ambient))

        from lionhub.providers.openai.codex import CodexCodeRequest

        req = CodexCodeRequest(prompt="hi", model="work", env={"CODEX_HOME": str(childs_own)})
        assert req.model == "gpt-5.3-codex-spark"

    def test_without_a_replacement_env_the_profile_still_comes_from_ours(
        self, tmp_path, monkeypatch
    ):
        """The control for the arm above: the ordinary CLI case is unchanged.

        Without it the fix could read as "never use our own environment", which
        would break every `li agent <profile>` run that has no replacement env
        at all -- the common path.
        """
        ambient = tmp_path / "ambient"
        ambient.mkdir()
        (ambient / "work.config.toml").write_text('model = "gpt-5.6-sol"\n')
        monkeypatch.setenv("CODEX_HOME", str(ambient))

        from lionhub.providers.openai.codex import CodexCodeRequest

        assert CodexCodeRequest(prompt="hi", model="work").model == "gpt-5.6-sol"

    def test_a_tilde_in_the_childs_codex_home_is_refused_rather_than_resolved(
        self, tmp_path, monkeypatch
    ):
        """A `~` names a different directory depending on whose home expands it.

        Resolving it here could read the profile that decides the model from a
        directory the child never opens, so the request is refused instead.
        """
        parent = tmp_path / "parent"
        child = tmp_path / "child"
        (parent / "codex").mkdir(parents=True)
        (child / "codex").mkdir(parents=True)
        (parent / "codex" / "work.config.toml").write_text('model = "gpt-5.6-sol"\n')
        (child / "codex" / "work.config.toml").write_text('model = "gpt-5.3-codex-spark"\n')
        monkeypatch.setenv("HOME", str(parent))

        from lionhub.providers.openai.codex import CodexCodeRequest

        with pytest.raises(ValidationError, match="CODEX_HOME '~/codex' is not an absolute path"):
            CodexCodeRequest(
                prompt="hi",
                model="work",
                env={"HOME": str(child), "CODEX_HOME": "~/codex"},
            )

        # The control: an absolute child home is read from the child's env, so
        # the arm above is about the tilde and not about the env.
        absolute = CodexCodeRequest(
            prompt="hi",
            model="work",
            env={"HOME": str(parent), "CODEX_HOME": str(child / "codex")},
        )
        assert absolute.model == "gpt-5.3-codex-spark"

    def test_a_relative_home_in_the_childs_env_is_refused_rather_than_resolved(self):
        """A HOME that is not absolute names a different codex home depending on where it
        is resolved, so the model-spec lookup refuses it before reading a profile there."""
        from lionhub.providers.openai.codex import CodexCodeRequest

        with pytest.raises(ValidationError, match="HOME 'rel' is not an absolute path"):
            CodexCodeRequest(prompt="hi", model="work", env={"HOME": "rel"})

    def test_a_codex_home_in_the_childs_env_wins_over_a_relative_home(self, tmp_path):
        """CODEX_HOME is the home when it is set; a HOME beside it is not read."""
        from lionhub.providers.openai.codex import CodexCodeRequest

        (tmp_path / "work.config.toml").write_text('model = "gpt-5.5"\n')
        req = CodexCodeRequest(
            prompt="hi", model="work", env={"HOME": "rel", "CODEX_HOME": str(tmp_path)}
        )
        assert req.model == "gpt-5.5"

    def test_a_relative_process_home_is_refused_when_codex_home_is_unset(self, monkeypatch):
        """The process case goes through the same refusal: an environment naming no
        CODEX_HOME reads its HOME, and a relative one names no fixed directory."""
        from lionagi._errors import ConfigurationError
        from lionhub.providers.openai._codex_profile import codex_home_for_env

        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setenv("HOME", "rel")
        with pytest.raises(ConfigurationError, match="HOME 'rel' is not an absolute path"):
            codex_home_for_env(None)

    def test_a_malformed_effort_is_rejected_rather_than_replaced(self, global_ultra):
        """Unset means absent, not "any value that is not a string".

        The unset check asked `not isinstance(effort, str)`, which is true of
        every malformed value, so a non-string was treated as "no effort given"
        and silently replaced with the derived default -- the caller's bad
        input disappeared and a value it never asked for reached the command
        line. Only None is unset; anything else is left to the field's own
        `str | None` validation.
        """
        import pydantic

        from lionhub.providers.openai.codex import CodexCodeRequest

        with pytest.raises(pydantic.ValidationError):
            CodexCodeRequest(prompt="hi", model="gpt-5.3-codex-spark", reasoning_effort=0)

        # The controls: the two shapes that must keep working either side of it.
        assert (
            CodexCodeRequest(
                prompt="hi", model="gpt-5.3-codex-spark", reasoning_effort="low"
            ).reasoning_effort
            == "low"
        )
        assert (
            CodexCodeRequest(prompt="hi", model="gpt-5.3-codex-spark").reasoning_effort == "xhigh"
        )

    def test_an_over_ceiling_value_in_the_childs_own_config_is_clamped(self, tmp_path, monkeypatch):
        ambient = tmp_path / "ambient"
        ambient.mkdir()
        (ambient / "config.toml").write_text('model = "gpt-5.6-sol"\n')
        childs_own = tmp_path / "child"
        childs_own.mkdir()
        (childs_own / "config.toml").write_text('model_reasoning_effort = "ultra"\n')
        monkeypatch.setenv("CODEX_HOME", str(ambient))

        from lionhub.providers.openai.codex import CodexCodeRequest

        req = CodexCodeRequest(
            prompt="hi",
            model="gpt-5.3-codex-spark",
            env={"CODEX_HOME": str(childs_own)},
        )
        assert req.reasoning_effort == "xhigh"

    def test_a_replacement_env_without_codex_home_follows_its_own_home(self, tmp_path, monkeypatch):
        """An env that redirects HOME redirects the default codex home with it."""
        fake_home = tmp_path / "home"
        (fake_home / ".codex").mkdir(parents=True)
        (fake_home / ".codex" / "config.toml").write_text('model_reasoning_effort = "ultra"\n')
        monkeypatch.delenv("CODEX_HOME", raising=False)
        # Our own HOME must hold no codex config, or a build that ignored the
        # request env would read the developer's real one and pass by accident.
        ambient_home = tmp_path / "ambient-home"
        ambient_home.mkdir()
        monkeypatch.setenv("HOME", str(ambient_home))

        from lionhub.providers.openai.codex import CodexCodeRequest

        req = CodexCodeRequest(
            prompt="hi",
            model="gpt-5.3-codex-spark",
            env={"HOME": str(fake_home)},
        )
        assert req.reasoning_effort == "xhigh"

    def test_a_replacement_env_naming_no_home_reads_the_account_home_not_ours(
        self, tmp_path, monkeypatch
    ):
        """No HOME in the child's environment does NOT mean no config.

        The child runs as the same OS account, and codex resolves its home from
        the account when HOME is unset (`env -i PATH=... codex doctor` reports
        the account's ~/.codex). Answering "no config" here would be the
        original 400 with extra steps.

        The parent's own HOME points somewhere ELSE on purpose. An earlier
        version of this arm pointed both at the same directory, so it passed
        whether the resolver read the password database or `$HOME` -- it could
        not see the case it was named for. The decoy carries a LOW effort,
        which would survive unclamped, so reading the wrong home is visible in
        the assertion rather than merely unproven.
        """
        from lionhub.providers.openai import _codex_profile

        account_home = tmp_path / "account"
        (account_home / ".codex").mkdir(parents=True)
        (account_home / ".codex" / "config.toml").write_text('model_reasoning_effort = "ultra"\n')

        decoy_home = tmp_path / "decoy"
        (decoy_home / ".codex").mkdir(parents=True)
        (decoy_home / ".codex" / "config.toml").write_text('model_reasoning_effort = "low"\n')

        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setenv("HOME", str(decoy_home))
        monkeypatch.setattr(_codex_profile, "_account_home", lambda: account_home)

        from lionhub.providers.openai.codex import CodexCodeRequest

        req = CodexCodeRequest(prompt="hi", model="gpt-5.3-codex-spark", env={"A": "1"})
        assert req.reasoning_effort == "xhigh"

    def test_env_values_never_reach_the_argv(self, global_ultra):
        """The invariant `env` actually carries.

        Setting `env` CAN change the argv, because it names the config the
        child reads and therefore whether a protective effort must be emitted
        — that is the whole point of the seam. What must never happen is an
        env VALUE appearing in the arguments.
        """
        from lionhub.providers.openai.codex import CodexCodeRequest

        secret = "sk-not-a-real-key-91746"
        args = CodexCodeRequest(
            prompt="hi", model="gpt-5.3-codex-spark", env={"OPENAI_API_KEY": secret}
        ).as_cmd_args()
        assert not any(secret in str(a) for a in args), args


class TestTheHelperItself:
    def test_a_profile_pin_suppresses_the_global_default(self, global_ultra):
        assert (
            codex_unset_effort_default("gpt-5.3-codex-spark", {"model_reasoning_effort": "medium"})
            is None
        )

    def test_an_unknown_model_is_never_guessed(self, global_ultra):
        assert codex_unset_effort_default("gpt-9.9-future") is None

    def test_a_provider_qualified_model_resolves_to_its_bare_name(self, global_ultra):
        assert codex_unset_effort_default("codex/gpt-5.3-codex-spark") == "xhigh"
