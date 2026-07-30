"""The version the server states is the version of the code serving it.

A client reading the OpenAPI document to decide what the server supports gets
whatever that document says. Left unset, FastAPI supplies its own default, which
is the same string for every release, so a client comparing versions across two
servers cannot tell them apart and a client gating a feature on a version gates
on a constant.

This guards the whole class rather than the one string: any surface that states
a version has to state the running one.
"""

import pytest

from lionagi.version import __version__

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")


def test_the_openapi_document_states_the_running_package_version():
    from lionagi.studio.app import create_app

    schema = create_app().openapi()

    assert schema["info"]["version"] == __version__


def test_the_stated_version_is_not_fastapis_placeholder():
    """The assertion above passes trivially if the package ever reports the
    same string FastAPI defaults to, which would hide the defect it exists to
    catch. Pin that the two are actually distinct."""
    from lionagi.studio.app import create_app

    assert create_app().openapi()["info"]["version"] != "0.1.0"
