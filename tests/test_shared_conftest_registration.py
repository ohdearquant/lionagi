"""Shared test hooks are registered as one pytest plugin."""


def test_shared_conftest_plugin_is_registered_once(pytestconfig):
    names = [
        name
        for name, plugin in pytestconfig.pluginmanager.list_name_plugin()
        if name == "tests._shared_conftest"
    ]
    assert names == ["tests._shared_conftest"]
