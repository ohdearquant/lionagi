# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The user-wide `.env` under LIONAGI_HOME: read by default, below every other source."""

import pytest

from lionagi._paths import LIONAGI_HOME
from lionagi.config import AppSettings
from lionagi.service.accounts._credentials import CredentialSources, resolve_account_key

NAME = "LIONAGI_CHAT_MODEL"
KEY = "LIONAGI_TEST_HOME_ACCOUNT_KEY"
VALUE = "home-key-value-0123456789"


@pytest.fixture
def home_dotenv(monkeypatch, tmp_path):
    """Write the test home's `.env` for one test, run from an empty directory, then remove it."""
    path = LIONAGI_HOME / ".env"
    if path.exists():
        pytest.skip(f"{path} already exists; this test does not replace a file it did not write")
    monkeypatch.delenv(NAME, raising=False)
    monkeypatch.delenv(KEY, raising=False)
    monkeypatch.chdir(tmp_path)
    LIONAGI_HOME.mkdir(parents=True, exist_ok=True)
    yield path
    path.unlink(missing_ok=True)


def test_the_home_dotenv_is_the_first_and_lowest_file(home_dotenv):
    settings = AppSettings()
    assert settings._dotenv_files[0] == home_dotenv
    assert [p.name for p in settings._dotenv_files[1:]] == [".env", ".env.local", ".secrets.env"]


def test_a_missing_home_dotenv_leaves_the_default(home_dotenv):
    assert not home_dotenv.exists()
    assert AppSettings().LIONAGI_CHAT_MODEL == AppSettings.model_fields[NAME].default


def test_the_home_dotenv_supplies_a_setting_no_other_source_sets(home_dotenv):
    home_dotenv.write_text(f"{NAME}=from-home\n")
    assert AppSettings().LIONAGI_CHAT_MODEL == "from-home"


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (".env", "from-cwd"),
        (".env.local", "from-cwd"),
        (".secrets.env", "from-cwd"),
        (None, "from-process"),
    ],
)
def test_every_other_source_outranks_the_home_dotenv(
    home_dotenv, tmp_path, monkeypatch, override, expected
):
    home_dotenv.write_text(f"{NAME}=from-home\n")
    if override is None:
        monkeypatch.setenv(NAME, "from-process")
    else:
        (tmp_path / override).write_text(f"{NAME}=from-cwd\n")
    assert AppSettings().LIONAGI_CHAT_MODEL == expected


@pytest.mark.asyncio
async def test_a_routed_account_key_resolves_from_the_home_dotenv(home_dotenv):
    home_dotenv.write_text(f"{KEY}={VALUE}\n")
    sources = CredentialSources.capture(AppSettings(), lookup_settings={})
    key = await resolve_account_key(KEY, sources)
    assert key.get_secret_value() == VALUE
