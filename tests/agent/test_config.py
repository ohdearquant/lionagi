# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for AgentConfig: defaults, presets, hooks, and YAML roundtrip."""

from lionagi.agent.config import AgentConfig


def test_coding_preset_defaults():
    config = AgentConfig.coding()
    assert config.name == "coder"
    assert config.effort == "high"
    assert config.tools == ["coding"]
    assert "coding agent" in config.system_prompt.lower()


def test_coding_preset_name_override():
    config = AgentConfig.coding(name="custom")
    assert config.name == "custom"
    # other defaults preserved
    assert config.tools == ["coding"]
    assert config.effort == "high"


def test_coding_preset_model_override():
    config = AgentConfig.coding(model="anthropic/claude-sonnet-4-6")
    assert config.model == "anthropic/claude-sonnet-4-6"


def test_coding_preset_custom_prompt():
    config = AgentConfig.coding(system_prompt="Be precise.")
    assert config.system_prompt == "Be precise."


def test_pre_hook_registration():
    config = AgentConfig()
    calls = []

    async def hook(tool_name, action, args):
        calls.append((tool_name, action))

    result = config.pre("bash", hook)
    # fluent API: returns self
    assert result is config
    assert "pre:bash" in config.hook_handlers
    assert hook in config.hook_handlers["pre:bash"]


def test_post_hook_registration():
    config = AgentConfig()

    async def hook(tool_name, action, args, result):
        return result

    config.post("editor", hook)
    assert "post:editor" in config.hook_handlers
    assert hook in config.hook_handlers["post:editor"]


def test_on_error_hook_registration():
    config = AgentConfig()

    async def hook(tool_name, action, args, error):
        return None

    config.on_error("reader", hook)
    assert "error:reader" in config.hook_handlers
    assert hook in config.hook_handlers["error:reader"]


def test_multiple_pre_hooks_same_tool():
    config = AgentConfig()

    async def hook1(tool_name, action, args):
        pass

    async def hook2(tool_name, action, args):
        pass

    config.pre("bash", hook1).pre("bash", hook2)
    handlers = config.hook_handlers["pre:bash"]
    assert hook1 in handlers
    assert hook2 in handlers
    assert handlers.index(hook1) < handlers.index(hook2)


def test_yaml_roundtrip(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    config = AgentConfig(
        name="tester",
        model="openai/gpt-4.1",
        effort="high",
        system_prompt="You test things.",
        tools=["coding"],
        yolo=False,
        lion_system=False,
        cwd="/tmp",
        permissions={"mode": "deny_all"},
    )
    config.to_yaml(yaml_path)

    loaded = AgentConfig.from_yaml(yaml_path)
    assert loaded.name == "tester"
    assert loaded.model == "openai/gpt-4.1"
    assert loaded.effort == "high"
    assert loaded.system_prompt == "You test things."
    assert loaded.tools == ["coding"]
    assert loaded.yolo is False
    assert loaded.lion_system is False
    assert loaded.cwd == "/tmp"
    assert loaded.permissions == {"mode": "deny_all"}


def test_yaml_roundtrip_omits_hooks(tmp_path):
    """Hook callables are code-only and must not appear in YAML."""
    yaml_path = tmp_path / "config.yaml"
    config = AgentConfig.coding()

    async def my_hook(tool_name, action, args):
        pass

    config.pre("bash", my_hook)
    config.to_yaml(yaml_path)

    import yaml

    with open(yaml_path) as f:
        raw = yaml.safe_load(f)
    assert "hook_handlers" not in raw


def test_from_yaml_uses_stem_as_name(tmp_path):
    """When YAML has no 'name' key, filename stem is used."""
    yaml_path = tmp_path / "myagent.yaml"
    import yaml

    with open(yaml_path, "w") as f:
        yaml.dump({"model": "openai/gpt-4.1", "tools": []}, f)

    loaded = AgentConfig.from_yaml(yaml_path)
    assert loaded.name == "myagent"


def test_from_yaml_extra_keys_collected(tmp_path):
    yaml_path = tmp_path / "cfg.yaml"
    import yaml

    with open(yaml_path, "w") as f:
        yaml.dump({"name": "x", "custom_key": "hello"}, f)

    loaded = AgentConfig.from_yaml(yaml_path)
    assert loaded.extra.get("custom_key") == "hello"
