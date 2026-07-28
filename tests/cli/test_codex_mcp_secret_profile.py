"""Whose profile is already on the request decides whether forwarding can proceed.

codex takes one `-p` profile per invocation, so forwarding secret-bearing MCP
server fields through a generated profile has to refuse when the caller asked for
a profile of their own. A resumed leg re-spawns from its persisted request, and
that request carries the profile the *first* run generated, so the same guard was
refusing to let lionagi replace its own profile: every resume of a leg that got
secret-bearing servers died before spawn.
"""

import os
import re
from pathlib import Path

import pytest

from lionagi._errors import ConfigurationError
from lionagi.agent.factory import (
    _CODEX_MCP_PROFILE_PREFIX,
    _write_codex_mcp_secret_profile,
)

SECRET_FIELDS = {"khive": {"env": {"KHIVE_TOKEN": "x"}}}


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _written_profile(codex_home: Path) -> Path:
    files = sorted(codex_home.glob(f"{_CODEX_MCP_PROFILE_PREFIX}*.config.toml"))
    assert len(files) == 1, f"expected exactly one generated profile, got {files}"
    return files[0]


def test_fresh_request_gets_a_generated_profile(codex_home):
    kwargs: dict = {}
    _write_codex_mcp_secret_profile(kwargs, SECRET_FIELDS)

    assert kwargs["profile"].startswith(_CODEX_MCP_PROFILE_PREFIX)
    written = _written_profile(codex_home)
    assert written.stem.removesuffix(".config") == kwargs["profile"]
    assert oct(written.stat().st_mode)[-3:] == "600"
    assert "KHIVE_TOKEN" in written.read_text()


def test_resume_replaces_the_profile_a_previous_run_generated(codex_home):
    """The persisted request carries the first run's generated profile name.

    Its file is already gone, deleted by that run's own exit cleanup, so keeping
    the name would point codex at a profile that does not exist. Replacing it is
    both allowed and required.
    """
    stale = f"{_CODEX_MCP_PROFILE_PREFIX}b961dde4c5af46de82922a3b57dfc89d"
    kwargs = {"profile": stale, "model": "gpt-5.6-terra"}

    _write_codex_mcp_secret_profile(kwargs, SECRET_FIELDS)

    assert kwargs["profile"] != stale
    assert kwargs["profile"].startswith(_CODEX_MCP_PROFILE_PREFIX)
    assert kwargs["model"] == "gpt-5.6-terra"
    assert _written_profile(codex_home).stem.removesuffix(".config") == kwargs["profile"]


def test_a_caller_supplied_profile_still_refuses(codex_home):
    kwargs = {"profile": "my-own-profile"}

    with pytest.raises(ConfigurationError) as excinfo:
        _write_codex_mcp_secret_profile(kwargs, SECRET_FIELDS)

    assert "my-own-profile" in str(excinfo.value)
    assert kwargs["profile"] == "my-own-profile"
    assert not list(codex_home.glob("*.config.toml"))


def test_the_prefix_is_the_only_thing_that_distinguishes_the_two(codex_home):
    """A caller profile that merely resembles ours is still a caller profile.

    The prefix is a namespace, and the check is a prefix check, so this records
    where the boundary sits rather than leaving it to be inferred.
    """
    kwargs = {"profile": "lionagi-mcp"}  # the prefix without its trailing dash

    with pytest.raises(ConfigurationError):
        _write_codex_mcp_secret_profile(kwargs, SECRET_FIELDS)


def test_generated_names_match_the_pattern_the_reaper_globs(codex_home):
    """Minting and reaping have to agree, or abandoned profiles never get swept."""
    kwargs: dict = {}
    _write_codex_mcp_secret_profile(kwargs, SECRET_FIELDS)

    assert re.fullmatch(rf"{re.escape(_CODEX_MCP_PROFILE_PREFIX)}[0-9a-f]{{32}}", kwargs["profile"])
    assert list(codex_home.glob(f"{_CODEX_MCP_PROFILE_PREFIX}*.config.toml"))


def test_stale_generated_profiles_are_reaped_and_live_ones_are_not(codex_home):
    old = codex_home / f"{_CODEX_MCP_PROFILE_PREFIX}deadbeef.config.toml"
    old.write_text("")
    os.utime(old, (0, 0))
    unrelated = codex_home / "someones-own.config.toml"
    unrelated.write_text("")
    os.utime(unrelated, (0, 0))

    _write_codex_mcp_secret_profile({}, SECRET_FIELDS)

    assert not old.exists()
    assert unrelated.exists(), "the reaper must only touch names it minted"
