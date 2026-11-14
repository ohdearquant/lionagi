"""Legacy MCP replay must classify the same bytes it will use."""

import json

import pytest

from lionhub.orchestration.mcp_config import McpConfigError, resolve_spawn_mcp_servers


@pytest.mark.parametrize("marker", ["/tmp/original", None, {"unexpected": True}])
def test_marked_legacy_file_is_refused(tmp_path, marker):
    path = tmp_path / "recorded.json"
    path.write_text(json.dumps({"mcpServers": {}, "lionagiSnapshotOf": marker}))
    with pytest.raises(McpConfigError, match="recorded MCP.*fresh run") as raised:
        resolve_spawn_mcp_servers(path, launch_dir=tmp_path, reject_legacy_snapshot=True)
    assert str(path) in str(raised.value)


@pytest.mark.parametrize("data", ["{bad", '{"mcpServers":' + "[" * 1100 + "0" + "]" * 1100 + "}"])
def test_unreadable_legacy_file_is_refused(tmp_path, data):
    path = tmp_path / "recorded.json"
    path.write_text(data)
    with pytest.raises(McpConfigError, match="recorded MCP.*fresh run"):
        resolve_spawn_mcp_servers(path, launch_dir=tmp_path, reject_legacy_snapshot=True)


def test_unmarked_legacy_file_stays_explicit(tmp_path):
    path = tmp_path / "recorded.json"
    path.write_text(json.dumps({"mcpServers": {"one": {"command": "echo"}}}))
    found = resolve_spawn_mcp_servers(path, launch_dir=tmp_path, reject_legacy_snapshot=True)
    assert found.explicit is True
    assert set(found.servers) == {"one"}


def test_fresh_named_marker_remains_explicit(tmp_path):
    path = tmp_path / "named.json"
    path.write_text(json.dumps({"mcpServers": {}, "lionagiSnapshotOf": "/tmp/original"}))
    assert resolve_spawn_mcp_servers(path, launch_dir=tmp_path).explicit is True


def test_legacy_check_and_use_the_same_read(tmp_path, monkeypatch):
    from lionhub.orchestration import mcp_config

    path = tmp_path / "recorded.json"
    path.write_text(json.dumps({"mcpServers": {"before": {"command": "echo"}}}))
    original = mcp_config._read_config
    calls = []

    def changed_after_read(value):
        calls.append(value)
        data = original(value)
        path.write_text(
            json.dumps(
                {"mcpServers": {"after": {"command": "echo"}}, "lionagiSnapshotOf": "/tmp/source"}
            )
        )
        return data

    monkeypatch.setattr(mcp_config, "_read_config", changed_after_read)
    resolved = resolve_spawn_mcp_servers(path, launch_dir=tmp_path, reject_legacy_snapshot=True)
    assert calls == [path]
    assert resolved.explicit is True
    assert set(resolved.servers) == {"before"}


def test_legacy_error_hides_malformed_file_contents(tmp_path):
    path = tmp_path / "recorded.json"
    path.write_text('{"private_payload": "sentinel-secret",')
    with pytest.raises(McpConfigError) as raised:
        resolve_spawn_mcp_servers(path, launch_dir=tmp_path, reject_legacy_snapshot=True)
    assert "sentinel-secret" not in str(raised.value)


def test_legacy_error_escapes_path_controls(tmp_path):
    path = tmp_path / "recorded\nname.json"
    path.write_text(json.dumps({"mcpServers": {}, "lionagiSnapshotOf": "/tmp/source"}))
    with pytest.raises(McpConfigError) as raised:
        resolve_spawn_mcp_servers(path, launch_dir=tmp_path, reject_legacy_snapshot=True)
    assert "\n" not in str(raised.value)
    assert r"recorded\nname.json" in str(raised.value)
