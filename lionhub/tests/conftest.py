"""Shared fixtures for the hub test tree."""

from tests import _shared_conftest as _shared
from tests._shared_conftest import (  # noqa: F401
    _contain_the_frontend_dist_variable,
    _keep_the_interpreter_default_sigpipe,
    _outside_any_agent,
    _reset_plugin_registry,
    anthropic_endpoint_config,
    anthropic_imodel,
    base_imodel,
    ensure_fake_lionagi,
    mock_streaming_response,
    mock_sync_response,
    mod_paths,
    openai_endpoint_config,
    plugin_home,
    pytest_addoption,
    pytest_collection_modifyitems,
    pytest_pycollect_makeitem,
    pytest_runtest_makereport,
    write_plugin,
)

pytest_plugins = ["lionagi.testing.pytest_plugin"]
if hasattr(_shared, "pytest_runtest_protocol"):
    pytest_runtest_protocol = _shared.pytest_runtest_protocol
