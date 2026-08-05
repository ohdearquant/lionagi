# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""``codex/<name>`` may name a codex config profile rather than a model.

A codex config profile (``$CODEX_HOME/<name>.config.toml``) names a model and
the provider that serves it, and is how codex reaches models from other
vendors. lionagi cannot forward it as ``-p``: codex accepts one profile per
invocation and lionagi spends that slot on MCP server secrets. So it reads the
file and applies the settings directly.

The failure this guards against is silent rather than loud. Before, the
profile name went to codex as a model id and codex ran *something else*, which
looks like a working leg producing worse answers.
"""

from __future__ import annotations

import logging

import pytest

from lionagi.cli._providers import (
    build_chat_model,
    build_imodel_from_spec,
    resolve_codex_config_profile,
)


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex_home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _write(home, name: str, body: str) -> None:
    (home / f"{name}.config.toml").write_text(body)


class TestWhatCountsAsAProfileName:
    def test_a_profile_file_resolves_to_its_model_and_scalars(self, codex_home):
        _write(
            codex_home,
            "deepseek-flash",
            'model_provider = "openrouter"\nmodel = "deepseek/deepseek-v4-flash-0731"\n',
        )
        assert resolve_codex_config_profile("deepseek-flash") == (
            "deepseek/deepseek-v4-flash-0731",
            {"model_provider": "openrouter"},
        )

    def test_an_ordinary_model_id_is_never_looked_up_as_a_path(self, codex_home):
        """A vendor model id contains slashes. It must stay a model id.

        The nested file is planted deliberately: without it, this passes
        merely because nothing is there, which is true of any name at all and
        so tests nothing about how the name is treated.
        """
        nested = codex_home / "deepseek"
        nested.mkdir()
        (nested / "deepseek-v4-flash-0731.config.toml").write_text('model = "planted"\n')
        assert resolve_codex_config_profile("deepseek/deepseek-v4-flash-0731") is None

    def test_a_name_that_escapes_codex_home_is_refused(self, codex_home, tmp_path):
        """Planted at the traversal target, so refusing is the only thing that
        can produce None. Pointed at a path that does not exist, this arm
        passes with the guard deleted."""
        (tmp_path / "escaped.config.toml").write_text('model = "escaped"\n')
        assert (tmp_path / "escaped.config.toml").is_file()  # the arm is armed
        assert resolve_codex_config_profile("../escaped") is None
        assert resolve_codex_config_profile("..") is None

    def test_an_absent_profile_leaves_the_name_alone(self, codex_home):
        assert resolve_codex_config_profile("no-such-profile") is None

    def test_a_symlink_whose_target_is_unreadable_is_not_an_absent_profile(
        self, codex_home, tmp_path
    ):
        """``is_file()`` follows the link and answers False for a broken one
        exactly as it does for nothing at all, so the two would otherwise
        collapse into the same silent fall-through to a model id — with a file
        sitting in CODEX_HOME that the operator can see.

        The link is planted pointing at a real path that is then removed, so it
        is genuinely a dangling symlink rather than merely a missing name.
        """
        target = tmp_path / "gone.config.toml"
        target.write_text('model = "vendor/m"\n')
        link = codex_home / "dangling.config.toml"
        link.symlink_to(target)
        assert link.is_file()  # the plant landed and resolves while the target exists
        target.unlink()
        assert link.is_symlink() and not link.is_file()  # now genuinely dangling

        with pytest.raises(ValueError, match="is a symlink whose target"):
            resolve_codex_config_profile("dangling")

    def test_a_symlink_that_resolves_is_read_normally(self, codex_home, tmp_path):
        """The other half: linking is not itself suspicious, so a working link
        must still resolve. Without this the arm above would be satisfied by
        refusing every symlink."""
        target = tmp_path / "real.config.toml"
        target.write_text('model = "vendor/m"\nmodel_provider = "openrouter"\n')
        (codex_home / "linked.config.toml").symlink_to(target)
        assert resolve_codex_config_profile("linked") == (
            "vendor/m",
            {"model_provider": "openrouter"},
        )


class TestASuccessfulResolutionSaysWhatItRan:
    def test_the_substituted_model_is_reported(self, codex_home, caplog):
        """One name goes in and a different model runs. Doing that without a
        word is the quiet half of the failure this resolver exists to fix."""
        _write(codex_home, "deepseek-flash", 'model = "deepseek/deepseek-v4-flash-0731"\n')
        # The CLI progress channel sets propagate=False, so caplog's root
        # handler never sees it. Attaching to that logger directly is what
        # makes an empty capture mean "nothing was logged" rather than "the
        # instrument cannot reach this channel" — the two read identically.
        logger = logging.getLogger("lionagi.cli.progress")
        logger.addHandler(caplog.handler)
        try:
            with caplog.at_level(logging.INFO, logger="lionagi.cli.progress"):
                resolved = resolve_codex_config_profile("deepseek-flash")
        finally:
            logger.removeHandler(caplog.handler)
        # The line only runs on a successful resolution, so an arm that never
        # resolved would assert on a log that was never reached and read as a
        # missing log rather than a missing call.
        assert resolved is not None
        assert "deepseek-flash" in caplog.text
        assert "deepseek/deepseek-v4-flash-0731" in caplog.text

    def test_a_name_carrying_a_dot_is_not_resolved_at_all(self, codex_home):
        """Bare excludes dots, so this file is never read. Planted so the
        assertion is about the name rule rather than about an absent file, and
        stated because the same name resolves once the dot is gone."""
        _write(codex_home, "gpt-5.6-sol", 'model = "deepseek/deepseek-v4-flash-0731"\n')
        assert (codex_home / "gpt-5.6-sol.config.toml").is_file()
        assert resolve_codex_config_profile("gpt-5.6-sol") is None

        _write(codex_home, "gpt-56-sol", 'model = "deepseek/deepseek-v4-flash-0731"\n')
        assert resolve_codex_config_profile("gpt-56-sol") is not None


class TestAProfileThatCannotBeHonouredFailsLoudly:
    """Each of these would otherwise send the profile NAME to codex as a model
    id, which runs a different model and reports success."""

    def test_a_profile_declaring_no_model_raises(self, codex_home):
        _write(codex_home, "half-written", 'model_provider = "openrouter"\n')
        with pytest.raises(ValueError, match="declares no 'model'"):
            resolve_codex_config_profile("half-written")

    def test_an_unparseable_profile_raises(self, codex_home):
        _write(codex_home, "broken", "model = = =\n")
        with pytest.raises(ValueError, match="could not be read"):
            resolve_codex_config_profile("broken")

    def test_an_empty_model_value_is_not_accepted(self, codex_home):
        _write(codex_home, "blank", 'model = ""\n')
        with pytest.raises(ValueError, match="declares no 'model'"):
            resolve_codex_config_profile("blank")


class TestServersAreNotAdoptedFromAConfigFile:
    def test_table_values_are_skipped(self, codex_home):
        """lionagi decides a leg's MCP server set explicitly. Adopting servers
        out of a config file would go around that decision silently."""
        _write(
            codex_home,
            "with-servers",
            'model = "vendor/m"\nmodel_provider = "openrouter"\n'
            '[mcp_servers.snuck_in]\ncommand = "/bin/sh"\n',
        )
        resolved, overrides = resolve_codex_config_profile("with-servers")
        assert resolved == "vendor/m"
        assert overrides == {"model_provider": "openrouter"}
        assert "mcp_servers" not in overrides


class TestBothSpecEntryPointsHonourIt:
    def test_the_li_agent_path_resolves(self, codex_home):
        _write(
            codex_home,
            "flash",
            'model_provider = "openrouter"\nmodel = "deepseek/deepseek-v4-flash-0731"\n',
        )
        m = build_chat_model(provider="codex", model="flash", yolo=True, verbose=False, theme=None)
        kwargs = m.endpoint.config.kwargs
        assert kwargs["model"] == "deepseek/deepseek-v4-flash-0731"
        assert kwargs["config_overrides"]["model_provider"] == "openrouter"

    def test_the_orchestrate_path_resolves(self, codex_home):
        _write(
            codex_home,
            "flash",
            'model_provider = "openrouter"\nmodel = "deepseek/deepseek-v4-flash-0731"\n',
        )
        m = build_imodel_from_spec("codex/flash", yolo=True)
        config = m.endpoint.config
        # The provider stays codex and the model becomes the vendor id the
        # profile names — iModel splits the prefix off into its own field.
        assert config.provider == "codex"
        assert config.kwargs["model"] == "deepseek/deepseek-v4-flash-0731"
        assert config.kwargs["config_overrides"]["model_provider"] == "openrouter"
        # And it is still a CLI endpoint, which is what lets `li agent` accept
        # it at all — an openrouter/* spec is refused there.
        assert m.is_cli is True


class TestTheEffortClampSeesTheResolvedModel:
    """The clamp's ceilings are keyed on the model, so it has to run after
    resolution. Pointing a profile at a model that IS in the ceiling table is
    what makes the ordering observable at all."""

    def test_a_profile_pointing_at_a_capped_model_is_clamped(self, codex_home):
        _write(codex_home, "capped", 'model = "gpt-5.3-codex"\n')
        m = build_chat_model(
            provider="codex",
            model="capped",
            yolo=True,
            verbose=False,
            theme=None,
            effort="ultra",
        )
        assert m.endpoint.config.kwargs["reasoning_effort"] == "xhigh"

    def test_a_profile_pointing_at_an_uncapped_model_keeps_its_effort(self, codex_home):
        _write(codex_home, "open", 'model = "vendor/some-model"\n')
        m = build_chat_model(
            provider="codex",
            model="open",
            yolo=True,
            verbose=False,
            theme=None,
            effort="ultra",
        )
        assert m.endpoint.config.kwargs["reasoning_effort"] == "ultra"


class TestForwardedMcpServersSurviveTheMerge:
    def test_profile_overrides_do_not_replace_forwarded_server_config(self, codex_home):
        """Both write ``config_overrides``. Assigning instead of merging would
        drop whichever landed first, and a leg would run with no tools."""
        _write(codex_home, "flash", 'model = "vendor/m"\nmodel_provider = "openrouter"\n')
        m = build_chat_model(
            provider="codex",
            model="flash",
            yolo=True,
            verbose=False,
            theme=None,
            mcp_servers={"khive": {"command": "/bin/true"}},
        )
        overrides = m.endpoint.config.kwargs["config_overrides"]
        assert overrides["model_provider"] == "openrouter"
        # The forwarded server set is still present alongside it.
        assert any("mcp_servers" in str(k) for k in overrides), overrides
