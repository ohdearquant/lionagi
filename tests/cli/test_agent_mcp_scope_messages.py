"""The spawn-time MCP messages must claim only the scope they can observe.

Each of these messages is emitted while setting up one leg. A message that says
"this run" is read as covering every leg of a multi-agent run, but sibling legs
resolve their own providers and their own MCP config, which this code never
sees. So the text has to stay leg-scoped.
"""

import logging

from lionagi.cli._logging import _WARN_LOGGER_NAME
from lionagi.cli._mcp_resolve import McpResolution
from lionagi.cli.agent import _report_mcp_resolution


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


def test_non_claude_provider_warning_is_leg_scoped():
    resolution = McpResolution({"khive": {"command": "kkernel"}}, None, "/tmp/.mcp.json", "/tmp")
    messages = _capture_warnings(
        lambda: _report_mcp_resolution(resolution, provider="codex", cwd="/tmp")
    )

    assert len(messages) == 1
    text = messages[0]
    assert "this leg" in text.lower()
    assert "this run" not in text.lower()
    # The set is not carried here; that is not the same as no other lane in
    # lionagi being able to accept one, which this function cannot know.
    assert "only the claude cli lane accepts" not in text.lower()


def test_no_servers_warning_is_leg_scoped():
    resolution = McpResolution(None, "no_mcp_config_found", None, "/tmp")
    messages = _capture_warnings(
        lambda: _report_mcp_resolution(resolution, provider="claude_code", cwd="/tmp")
    )

    assert len(messages) == 1
    assert "this leg" in messages[0].lower()
    assert "this run" not in messages[0].lower()
