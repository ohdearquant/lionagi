# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A routed account's key resolves from the user-wide `.env` under LIONAGI_HOME."""

import pytest

from lionagi._paths import LIONAGI_HOME
from lionagi.config import AppSettings
from lionhub.providers.anthropic.accounts._credentials import CredentialSources, resolve_account_key

KEY = "LIONAGI_TEST_HOME_ACCOUNT_KEY"
VALUE = "home-key-value-0123456789"


@pytest.fixture
def home_dotenv(monkeypatch, tmp_path):
    """Write the test home's `.env` for one test, run from an empty directory, then remove it."""
    path = LIONAGI_HOME / ".env"
    if path.exists():
        pytest.skip(f"{path} already exists; this test does not replace a file it did not write")
    monkeypatch.delenv(KEY, raising=False)
    monkeypatch.chdir(tmp_path)
    LIONAGI_HOME.mkdir(parents=True, exist_ok=True)
    yield path
    path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_a_routed_account_key_resolves_from_the_home_dotenv(home_dotenv):
    home_dotenv.write_text(f"{KEY}={VALUE}\n")
    sources = CredentialSources.capture(AppSettings(), lookup_settings={})
    key = await resolve_account_key(KEY, sources)
    assert key.get_secret_value() == VALUE
