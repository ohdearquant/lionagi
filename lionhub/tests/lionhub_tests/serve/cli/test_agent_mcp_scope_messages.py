"""The spawn-time MCP messages must claim only the scope they can observe.

Each of these messages is emitted while setting up one leg. A message that says
"this run" is read as covering every leg of a multi-agent run, but sibling legs
resolve their own providers and their own MCP config, which this code never
sees. So the text has to stay leg-scoped.
"""

import logging
from pathlib import Path

import pytest

from lionhub.orchestration.console import _WARN_LOGGER_NAME
from lionhub.orchestration.mcp_config import McpResolution
from lionhub.serve.cli.agent import _report_mcp_resolution

_QUIET_ON = "mcp:\n  quiet_discovery: true\n"
_QUIET_OFF = "mcp:\n  quiet_discovery: false\n"


@pytest.fixture
def write_settings(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    leg = project / "work" / "leg"
    invocation = tmp_path / "invocation"
    leg.mkdir(parents=True)
    invocation.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(invocation)

    def write(user=None, local=None):
        for root, contents in ((home, user), (project, local)):
            if contents is not None:
                layer = root / ".lionagi" / "settings.yaml"
                layer.parent.mkdir(parents=True)
                layer.write_text(contents, encoding="utf-8")
        return str(leg)

    return write


def _capture_warnings(fn):
    """Collect `warn()` output without depending on propagation to the root
    logger, which `configure_cli_logging` disables process-wide."""
    records: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger(_WARN_LOGGER_NAME)
    handler = _Collect()
    logger.addHandler(handler)
    try:
        fn()
    finally:
        logger.removeHandler(handler)
    return records


@pytest.mark.parametrize("quiet", [False, True])
def test_non_claude_provider_warning_is_leg_scoped(quiet, write_settings):
    cwd = write_settings(user=_QUIET_ON if quiet else _QUIET_OFF)
    resolution = McpResolution({"khive": {"command": "kkernel"}}, None, "/tmp/.mcp.json", "/tmp")
    messages = _capture_warnings(
        lambda: _report_mcp_resolution(resolution, provider="codex", cwd=cwd, forwarded=False)
    )

    assert len(messages) == 1
    text = messages[0]
    assert "this leg" in text.lower()
    assert "this run" not in text.lower()
    # The set is not carried here; that is not the same as no other lane in
    # lionagi being able to accept one, which this function cannot know.
    assert "only the claude cli lane accepts" not in text.lower()


def test_no_servers_warning_is_leg_scoped(write_settings):
    """`forwarded=False` is the answer both callers read for a not-found
    resolution: nothing was found, so there is nothing to hand over and nothing
    to carry. The spawn starts without the servers its instructions assume, and
    the launch says so rather than leaving it to be inferred later."""
    cwd = write_settings()
    resolution = McpResolution(None, "no_mcp_config_found", None, "/tmp")
    messages = _capture_warnings(
        lambda: _report_mcp_resolution(resolution, provider="claude_code", cwd=cwd, forwarded=False)
    )

    assert len(messages) == 1
    assert "this leg" in messages[0].lower()
    assert "this run" not in messages[0].lower()


@pytest.mark.parametrize(
    ("user", "local", "warns"),
    [
        pytest.param(_QUIET_ON, None, False, id="user-on"),
        pytest.param(_QUIET_OFF, None, True, id="user-off"),
        pytest.param(_QUIET_ON, _QUIET_OFF, True, id="project-overrides-to-off"),
        pytest.param(_QUIET_OFF, _QUIET_ON, False, id="project-overrides-to-on"),
        pytest.param(_QUIET_ON, "mcp: {quiet_discovery: 'true'}", True, id="quoted-true"),
        pytest.param(None, "mcp: {quiet_discovery: 1}", True, id="integer"),
        pytest.param(None, "mcp: [true]", True, id="non-mapping-mcp"),
        pytest.param(_QUIET_ON, "mcp: [", True, id="malformed-yaml"),
        pytest.param(_QUIET_ON, "true", True, id="non-mapping-document"),
    ],
)
def test_missing_config_warning_honors_layered_boolean_setting(write_settings, user, local, warns):
    cwd = write_settings(user, local)
    resolution = McpResolution(None, "no_mcp_config_found", None, "/tmp")
    messages = _capture_warnings(
        lambda: _report_mcp_resolution(resolution, provider="codex", cwd=cwd, forwarded=False)
    )
    assert len(messages) == int(warns)
    if warns:
        assert "no_mcp_config_found" in messages[0]


def test_relative_leg_directory_finds_project_setting(write_settings, monkeypatch):
    leg = Path(write_settings(user=_QUIET_OFF, local=_QUIET_ON))
    monkeypatch.chdir(leg.parent)
    resolution = McpResolution(None, "no_mcp_config_found", None, "/tmp")

    assert (
        _capture_warnings(
            lambda: _report_mcp_resolution(
                resolution, provider="codex", cwd=leg.name, forwarded=False
            )
        )
        == []
    )


@pytest.mark.parametrize(
    "reason",
    ["mcp_config_unusable:invalid test configuration", "mcp_config_declares_no_servers"],
)
def test_quiet_discovery_still_reports_config_that_was_found(write_settings, reason):
    cwd = write_settings(user=_QUIET_ON)
    resolution = McpResolution(None, reason, "/tmp/.mcp.json", "/tmp")
    messages = _capture_warnings(
        lambda: _report_mcp_resolution(resolution, provider="codex", cwd=cwd, forwarded=False)
    )
    assert len(messages) == 1
    [message] = messages
    assert reason in message


def test_quiet_discovery_still_reports_the_forwarded_servers(monkeypatch, write_settings):
    cwd = write_settings(user=_QUIET_ON)
    resolution = McpResolution(
        {"test-server": {"command": "test-tool"}}, None, "/tmp/.mcp.json", "/tmp"
    )
    hints: list[str] = []
    monkeypatch.setattr("lionhub.serve.cli.agent.hint", hints.append)
    messages = _capture_warnings(
        lambda: _report_mcp_resolution(resolution, provider="codex", cwd=cwd, forwarded=True)
    )
    assert messages == []
    assert hints == ["[mcp] 1 server(s) from /tmp/.mcp.json: test-server"]


def test_a_declared_empty_set_is_not_warned_about():
    """`--no-mcp-config` is a choice: no servers are handed over because the
    caller said so, which is not the degradation the warning describes."""
    resolution = McpResolution(None, None, None, "/tmp")
    messages = _capture_warnings(
        lambda: _report_mcp_resolution(resolution, provider="codex", cwd="/tmp", forwarded=False)
    )

    assert messages == []


@pytest.mark.parametrize("forwarded", [False, True])
def test_a_named_config_listing_no_servers_is_not_reported(forwarded, monkeypatch):
    """A named config with an empty server list is the same choice as
    `--no-mcp-config`: the spawn hands the empty set over exclusively, so a
    warning that the set is "not carried" contradicts it, and a hint counting
    zero servers tells the caller nothing they did not state."""
    resolution = McpResolution({}, None, "/tmp/.mcp.json", "/tmp", explicit=True)
    assert resolution.declared_empty

    hints: list[str] = []
    monkeypatch.setattr("lionhub.serve.cli.agent.hint", hints.append)
    messages = _capture_warnings(
        lambda: _report_mcp_resolution(
            resolution, provider="claude_code", cwd="/tmp", forwarded=forwarded
        )
    )

    assert messages == []
    assert hints == []
