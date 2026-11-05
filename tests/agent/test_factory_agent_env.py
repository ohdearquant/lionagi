# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""create_agent(agent_env=...): an environment overlay for the spawned CLI child.

The overlay is for callers that run several agents at once in one process,
where a per-agent value cannot ride ``os.environ``. The assertions read the
request the child is spawned from, not the config field that carries it: that
field is drained into runtime state, so asserting it would test the carrier
while the child got nothing.
"""

import os
import pathlib

import pytest

from lionagi._errors import ConfigurationError
from lionagi.agent.factory import create_agent
from lionagi.agent.spec import AgentSpec
from lionagi.service.imodel import iModel
from lionhub.providers._cli_subprocess import reveal_redacted


async def _request_env(**create_kwargs) -> dict[str, str] | None:
    """The env the child process would be spawned with."""
    config = AgentSpec.compose("reviewer", model="codex/gpt-5.5")
    branch = await create_agent(config, load_settings=False, **create_kwargs)
    payload, _ = branch.chat_model.endpoint.create_payload({"prompt": "hi"})
    return reveal_redacted(payload["request"].env)


async def test_agent_env_reaches_the_spawned_request():
    env = await _request_env(agent_env={"LEG_ID": "leg-7"})
    assert env is not None
    assert env["LEG_ID"] == "leg-7"


async def test_agent_env_merges_over_os_environ():
    """A request env replaces the child environment wholesale, so a bare
    overlay would cost the child PATH and everything else it inherits."""
    env = await _request_env(agent_env={"LEG_ID": "leg-7"})
    assert env["PATH"] == os.environ["PATH"]
    assert set(os.environ) <= set(env)


async def test_agent_env_wins_over_an_inherited_value_of_the_same_name(monkeypatch):
    monkeypatch.setenv("CARGO_TARGET_DIR", "/inherited/target")
    env = await _request_env(agent_env={"CARGO_TARGET_DIR": "/agent/target"})
    assert env["CARGO_TARGET_DIR"] == "/agent/target"


async def test_no_agent_env_leaves_the_child_inheriting():
    """Control: without the overlay the request env stays None, which means
    inherit this process's environment."""
    assert await _request_env() is None


async def test_agent_env_does_not_reach_the_serializable_config():
    """``config.kwargs`` reaches ``to_dict``, and a child environment there is
    a credential about to be serialized. The overlay rides runtime state."""
    config = AgentSpec.compose("reviewer", model="codex/gpt-5.5")
    branch = await create_agent(config, load_settings=False, agent_env={"LEG_ID": "leg-7"})
    endpoint = branch.chat_model.endpoint
    assert "env" not in endpoint.config.kwargs
    assert "leg-7" not in str(endpoint.to_dict())
    # The absence above is the drain, not a dropped overlay.
    assert reveal_redacted(endpoint._runtime_state["env"])["LEG_ID"] == "leg-7"


async def test_agent_env_with_a_caller_supplied_chat_model_raises():
    """A model built elsewhere never passes the point where the overlay is applied."""
    config = AgentSpec.compose("reviewer", model="codex/gpt-5.5")
    with pytest.raises(ConfigurationError, match="chat_model"):
        await create_agent(
            config,
            load_settings=False,
            agent_env={"LEG_ID": "leg-7"},
            chat_model=iModel(provider="codex", model="gpt-5.5"),
        )


async def test_agent_env_without_a_spec_model_raises():
    config = AgentSpec.compose("reviewer")
    config.model = None
    with pytest.raises(ConfigurationError, match="spec.model"):
        await create_agent(config, load_settings=False, agent_env={"LEG_ID": "leg-7"})


@pytest.mark.parametrize("model", ["openai/gpt-4o", "openrouter/z-ai/glm-5.2"])
async def test_agent_env_on_a_provider_with_no_child_process_raises(model):
    """An API provider spawns nothing, so the overlay would sit unread in a
    serializable config field."""
    config = AgentSpec.compose("reviewer", model=model)
    with pytest.raises(ConfigurationError, match="takes no environment"):
        await create_agent(config, load_settings=False, agent_env={"LEG_ID": "leg-7"})


async def test_claude_code_is_also_a_carrier():
    """The check asks the endpoint, not a provider name, so every CLI
    endpoint that declares env as runtime state is accepted."""
    config = AgentSpec.compose("reviewer", model="claude_code/sonnet")
    branch = await create_agent(config, load_settings=False, agent_env={"LEG_ID": "leg-7"})
    env = reveal_redacted(branch.chat_model.endpoint._runtime_state["env"])
    assert env["LEG_ID"] == "leg-7"
    assert env["PATH"] == os.environ["PATH"]


@pytest.mark.parametrize(
    "bad_env,named,hidden",
    [
        ({"PORT": 8080}, "entry 0 (value is int)", "PORT"),
        ({"TOKEN_FILE": pathlib.Path("/x/secret-y")}, "entry 0 (value is", "secret-y"),
        ({"LEG_ID": None}, "entry 0 (value is NoneType)", "LEG_ID"),
        ({b"sk-not-a-key": "leg-7"}, "entry 0 (key is bytes)", "sk-not-a-key"),
        ({"TOKEN=sk-not-a-key": 8080}, "entry 0 (value is int)", "sk-not-a-key"),
        ({"LEG\0sk-not-a-key": 8080}, "entry 0 (value is int)", "sk-not-a-key"),
        # keys a shape test would pass as names, holding a credential
        ({"ghp_notarealtoken0123456789": 8080}, "entry 0 (value is int)", "notarealtoken"),
        ({"TOKEN sk-not-a-key": 8080}, "entry 0 (value is int)", "sk-not-a-key"),
        ({"TOKEN:sk-not-a-key": 8080}, "entry 0 (value is int)", "sk-not-a-key"),
        ({"TOKEN\nsk-not-a-key": 8080}, "entry 0 (value is int)", "sk-not-a-key"),
    ],
    ids=[
        "int-value",
        "path-value",
        "none-value",
        "bytes-key",
        "equals-in-name",
        "nul-in-name",
        "token-shaped-name",
        "space-in-name",
        "colon-in-name",
        "newline-in-name",
    ],
)
async def test_agent_env_rejects_non_string_entries_at_construction(bad_env, named, hidden):
    """The request model checks env only when a call is built, so without the
    factory check these fail at the first call. The message names the entry by
    position and type and prints nothing from the mapping: a key can be a
    credential as easily as a value."""
    config = AgentSpec.compose("reviewer", model="codex/gpt-5.5")
    with pytest.raises(ConfigurationError, match="agent_env") as exc:
        await create_agent(config, load_settings=False, agent_env=bad_env)
    message = str(exc.value)
    assert named in message
    if hidden is not None:
        assert hidden not in message


@pytest.mark.parametrize(
    "bad_env,hidden",
    [
        ({"": "leg-7"}, None),
        ({"TOKEN=sk-not-a-key": "leg-7"}, "sk-not-a-key"),
        ({"LEG\0ID": "leg-7"}, None),
        ({"LEG_ID": "leg\0-7"}, "leg"),
        ({"LEG\ud800": "leg-7"}, None),
        ({"LEG_ID": "leg-\ud800"}, "leg"),
    ],
    ids=[
        "empty-name",
        "equals-in-name",
        "nul-in-name",
        "nul-in-value",
        "surrogate-in-name",
        "surrogate-in-value",
    ],
)
async def test_agent_env_rejects_entries_no_environment_can_hold(bad_env, hidden):
    """A name with '=' splits where the child reads it, and a NUL fails the spawn.
    The entry is named by position: a name written as NAME=value carries the value."""
    config = AgentSpec.compose("reviewer", model="codex/gpt-5.5")
    with pytest.raises(ConfigurationError, match="entry 1") as exc:
        await create_agent(config, load_settings=False, agent_env={"OK": "1", **bad_env})
    message = str(exc.value)
    assert "entry 0" not in message
    if hidden is not None:
        assert hidden not in message


@pytest.mark.parametrize("key", ["CODEX_HOME", "HOME"])
@pytest.mark.parametrize("model", ["codex/gpt-5.5", "openai-codex/gpt-5.5"])
async def test_a_codex_agent_env_may_not_move_the_codex_home(key, model):
    """This process writes the MCP server profile and reads the default effort from
    the codex home; a child reading another home would miss both."""
    config = AgentSpec.compose("reviewer", model=model)
    with pytest.raises(ConfigurationError, match=key):
        await create_agent(config, load_settings=False, agent_env={key: "/elsewhere"})


async def test_a_claude_code_agent_env_may_set_home():
    """Control: the codex home check is about codex, not every carrier."""
    config = AgentSpec.compose("reviewer", model="claude_code/sonnet")
    branch = await create_agent(config, load_settings=False, agent_env={"HOME": "/elsewhere"})
    assert reveal_redacted(branch.chat_model.endpoint._runtime_state["env"])["HOME"] == "/elsewhere"


async def test_a_restored_model_spawns_with_the_current_process_environment():
    """The overlay is runtime state and is not serialized: a restored model's
    child inherits this process's environment unless the overlay is passed again."""
    config = AgentSpec.compose("reviewer", model="codex/gpt-5.5")
    branch = await create_agent(config, load_settings=False, agent_env={"LEG_ID": "leg-7"})
    restored = iModel.from_dict(branch.chat_model.to_dict())
    payload, _ = restored.endpoint.create_payload({"prompt": "hi"})
    assert reveal_redacted(payload["request"].env) is None


@pytest.mark.parametrize(
    "key",
    [
        "TOKEN=sk-not-a-key",
        "ghp_notarealtoken0123456789",
        "TOKEN sk-not-a-key",
        "LEG\0sk-not-a-key",
        "TOKEN:sk-not-a-key",
        "TOKEN\nsk-not-a-key",
    ],
)
def test_a_model_env_key_is_never_printed(key):
    """The same check guards a model's own env, which the request model applies when
    a call is built."""
    model = iModel(provider="codex", model="gpt-5.5", env={key: 8080})
    with pytest.raises(TypeError, match="entry 0") as exc:
        model.endpoint.create_payload({"prompt": "hi"})
    assert key not in str(exc.value)
    assert "notarealtoken" not in str(exc.value)
    assert "sk-not-a-key" not in str(exc.value)


async def test_agent_env_that_is_not_a_mapping_raises():
    config = AgentSpec.compose("reviewer", model="codex/gpt-5.5")
    with pytest.raises(ConfigurationError, match="mapping"):
        await create_agent(config, load_settings=False, agent_env=[("LEG_ID", "leg-7")])


async def test_a_wholly_valid_agent_env_passes_the_type_check():
    """Control for the arms above: the check rejects the shape, not the parameter."""
    env = await _request_env(agent_env={"LEG_ID": "leg-7", "PORT": "8080"})
    assert env["PORT"] == "8080"
    assert env["LEG_ID"] == "leg-7"
