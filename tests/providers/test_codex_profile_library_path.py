# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A codex config profile must resolve on the LIBRARY path, not only the CLI.

``codex/<name>`` may name a codex config profile rather than a model, and
resolving it used to live under ``lionagi/cli/``. Every caller that reached
codex through the CLI got the resolution; every caller that built a request in
process -- ``Branch(chat_model="codex/deepseek-flash")`` -- did not. Those legs
carried the profile NAME all the way to the spawn, codex read it as a model id,
and the run died with an unsupported-model error naming a model nobody had
asked for.

The tests here enter through the same door a library consumer does. The CLI's
own behaviour is covered by ``tests/cli/test_codex_config_profile_spec.py``,
and those tests keep passing against the re-export, which is what says the move
did not change the CLI path.
"""

from __future__ import annotations

import pytest

from lionagi.providers.openai._codex_profile import resolve_codex_config_profile
from lionagi.providers.openai.codex import CodexCodeRequest


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex_home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _write(home, name: str, body: str) -> None:
    (home / f"{name}.config.toml").write_text(body)


class TestTheLibraryEntryPoint:
    def test_a_request_built_in_process_resolves_the_profile(self, codex_home):
        """THE regression. Without it the bare name reaches codex as a model id."""
        _write(
            codex_home,
            "deepseek-flash",
            'model_provider = "openrouter"\nmodel = "deepseek/deepseek-v4-flash-0731"\n',
        )
        req = CodexCodeRequest(model="deepseek-flash", prompt="x")
        assert req.model == "deepseek/deepseek-v4-flash-0731"
        assert req.config_overrides == {"model_provider": "openrouter"}

    def test_branch_reaches_the_same_resolution(self, codex_home):
        """The consumer-facing entry, not just the request object underneath.

        An application that constructs a Branch on a profile name reaches the
        provider through this path and never touches the CLI, so it is the one
        that has to work for the fix to mean anything."""
        from lionagi import Branch

        _write(
            codex_home,
            "deepseek-flash",
            'model_provider = "openrouter"\nmodel = "deepseek/deepseek-v4-flash-0731"\n',
        )
        branch = Branch(chat_model="codex/deepseek-flash")
        payload, _ = branch.chat_model.endpoint.create_payload({"prompt": "x"})
        assert payload["request"].model == "deepseek/deepseek-v4-flash-0731"

    def test_a_plain_model_id_is_left_alone(self, codex_home):
        """The narrowing that keeps this from becoming a substitution engine: a
        vendor id carries a slash and dots, so it is never looked up as a file."""
        req = CodexCodeRequest(model="deepseek/deepseek-v4-flash-0731", prompt="x")
        assert req.model == "deepseek/deepseek-v4-flash-0731"
        assert req.config_overrides == {}

    def test_a_bare_name_with_no_profile_file_is_left_alone(self, codex_home):
        req = CodexCodeRequest(model="no-such-profile", prompt="x")
        assert req.model == "no-such-profile"

    def test_resolution_is_idempotent_so_the_cli_path_is_unaffected(self, codex_home):
        """The CLI resolves before constructing the request, so the request sees
        an already-resolved id. Resolving twice must be a no-op, or the CLI path
        would break the moment this hook landed."""
        _write(
            codex_home,
            "deepseek-flash",
            'model_provider = "openrouter"\nmodel = "deepseek/deepseek-v4-flash-0731"\n',
        )
        once = resolve_codex_config_profile("deepseek-flash")
        assert once is not None
        assert resolve_codex_config_profile(once[0]) is None
        req = CodexCodeRequest(model=once[0], prompt="x", config_overrides=dict(once[1]))
        assert req.model == "deepseek/deepseek-v4-flash-0731"
        assert req.config_overrides == {"model_provider": "openrouter"}


class TestOrderingAndPrecedence:
    def test_the_effort_clamp_sees_the_PROFILES_model_not_the_profile_name(self, codex_home):
        """Ordering, asserted through an OUTCOME that differs between the two.

        The clamp's ceilings are keyed on the model id, and only some models
        have one. ``gpt-5.6-luna`` clamps ``ultra`` down to ``max``; the bare
        string ``luna-profile`` has no ceiling and would pass ``ultra``
        through. So the clamped value is a direct readout of which string the
        clamp saw, and it can only be ``max`` if resolution ran first.

        A test that merely asserted the effort survived would pass under either
        order.
        """
        _write(codex_home, "luna-profile", 'model = "gpt-5.6-luna"\n')
        req = CodexCodeRequest(model="luna-profile", prompt="x", reasoning_effort="ultra")
        assert req.model == "gpt-5.6-luna"
        assert req.reasoning_effort == "max"

        # The control that makes the above mean something: with no profile to
        # resolve, the same effort on the same raw name is NOT clamped.
        untouched = CodexCodeRequest(
            model="luna-profile-absent", prompt="x", reasoning_effort="ultra"
        )
        assert untouched.reasoning_effort == "ultra"

    def test_caller_supplied_overrides_beat_the_profiles(self, codex_home):
        """An override at the call site is an instruction; the profile is a
        default sitting in a file."""
        _write(
            codex_home,
            "deepseek-flash",
            'model_provider = "openrouter"\nsandbox = "read-only"\n'
            'model = "deepseek/deepseek-v4-flash-0731"\n',
        )
        req = CodexCodeRequest(
            model="deepseek-flash",
            prompt="x",
            config_overrides={"model_provider": "caller-wins"},
        )
        assert req.config_overrides == {
            "model_provider": "caller-wins",
            "sandbox": "read-only",
        }

    def test_a_broken_profile_refuses_rather_than_running_something_else(self, codex_home):
        """The whole point of the mechanism is that a silent substitution is
        worse than a loud refusal, and that has to hold on this path too."""
        _write(codex_home, "no-model", 'model_provider = "openrouter"\n')
        with pytest.raises(ValueError, match="declares no 'model'"):
            CodexCodeRequest(model="no-model", prompt="x")
