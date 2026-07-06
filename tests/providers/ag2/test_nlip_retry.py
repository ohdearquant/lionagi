# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for NLIP retry/backoff: timing, re-raise on exhaustion, exception surface, logging."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from lionagi.providers.ag2 import nlip as nlip_mod

# ---------------------------------------------------------------------------
# Fakes: httpx.AsyncClient + nlip_sdk (not an installed dependency here)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data=None, raise_exc: Exception | None = None):
        self._data = data
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc

    def json(self):
        return self._data


def _patch_async_client(monkeypatch, post_mock):
    """Patch the real httpx.AsyncClient (shared via sys.modules regardless of import site)."""

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None):
            return await post_mock(url, json=json)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)


class _FakeNlipMsg:
    def __init__(self, content: str):
        self._content = content

    def model_dump(self, exclude_none=True):
        return {"content": self._content}

    def add_json(self, data, label=None):
        pass


class _FakeNlipFactory:
    @staticmethod
    def create_text(text, language="english"):
        return _FakeNlipMsg(text)


class _FakeNlipMessage:
    def __init__(self, content: str):
        self.content = content

    @classmethod
    def model_validate(cls, data):
        return cls(data.get("content", "") if isinstance(data, dict) else "")


def _nlip_sdk_stubs() -> dict:
    nlip_submodule = MagicMock()
    nlip_submodule.NLIP_Factory = _FakeNlipFactory
    nlip_submodule.NLIP_Message = _FakeNlipMessage
    return {
        "nlip_sdk": MagicMock(),
        "nlip_sdk.nlip": nlip_submodule,
    }


MESSAGES = [{"role": "user", "content": "hello"}]


# ---------------------------------------------------------------------------
# (a) Backoff timing: sleeps grow, non-zero
# ---------------------------------------------------------------------------


class TestBackoffTiming:
    @pytest.mark.asyncio
    async def test_call_direct_backoff_delays_increase(self, monkeypatch):
        sleep_calls: list[float] = []

        async def fake_sleep(d):
            sleep_calls.append(d)

        post_mock = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        _patch_async_client(monkeypatch, post_mock)
        monkeypatch.setattr("lionagi.ln.concurrency.patterns.anyio.sleep", fake_sleep)

        with pytest.raises(httpx.TimeoutException):
            await nlip_mod._call_direct(
                "https://nlip.example.com", MESSAGES, timeout=5.0, max_retries=4
            )

        # 4 attempts -> 3 inter-attempt sleeps, strictly increasing (exponential).
        assert len(sleep_calls) == 3
        assert all(d > 0 for d in sleep_calls)
        assert sleep_calls[0] < sleep_calls[1] < sleep_calls[2]

    @pytest.mark.asyncio
    async def test_call_nlip_sdk_backoff_delays_increase(self, monkeypatch):
        sleep_calls: list[float] = []

        async def fake_sleep(d):
            sleep_calls.append(d)

        post_mock = AsyncMock(side_effect=httpx.ConnectError("connect failed"))
        _patch_async_client(monkeypatch, post_mock)
        monkeypatch.setattr("lionagi.ln.concurrency.patterns.anyio.sleep", fake_sleep)

        with patch.dict(sys.modules, _nlip_sdk_stubs()):
            with pytest.raises(httpx.ConnectError):
                await nlip_mod._call_nlip_sdk(
                    "https://nlip.example.com", MESSAGES, timeout=5.0, max_retries=3
                )

        assert len(sleep_calls) == 2
        assert all(d > 0 for d in sleep_calls)
        assert sleep_calls[0] < sleep_calls[1]


# ---------------------------------------------------------------------------
# (b) Raise on final attempt: original exception propagates
# ---------------------------------------------------------------------------


class TestRaiseOnFinalAttempt:
    @pytest.mark.asyncio
    async def test_call_direct_raises_original_timeout_after_exhaustion(self, monkeypatch):
        async def fake_sleep(d):
            pass

        post_mock = AsyncMock(side_effect=httpx.TimeoutException("boom"))
        _patch_async_client(monkeypatch, post_mock)
        monkeypatch.setattr("lionagi.ln.concurrency.patterns.anyio.sleep", fake_sleep)

        with pytest.raises(httpx.TimeoutException, match="boom"):
            await nlip_mod._call_direct(
                "https://nlip.example.com", MESSAGES, timeout=5.0, max_retries=2
            )

        assert post_mock.call_count == 2

    @pytest.mark.asyncio
    async def test_call_nlip_sdk_raises_original_connect_error_after_exhaustion(self, monkeypatch):
        async def fake_sleep(d):
            pass

        post_mock = AsyncMock(side_effect=httpx.ConnectError("unreachable"))
        _patch_async_client(monkeypatch, post_mock)
        monkeypatch.setattr("lionagi.ln.concurrency.patterns.anyio.sleep", fake_sleep)

        with patch.dict(sys.modules, _nlip_sdk_stubs()):
            with pytest.raises(httpx.ConnectError, match="unreachable"):
                await nlip_mod._call_nlip_sdk(
                    "https://nlip.example.com", MESSAGES, timeout=5.0, max_retries=2
                )

        assert post_mock.call_count == 2


# ---------------------------------------------------------------------------
# (c) Narrow exception surface: non-retryable errors raise immediately
# ---------------------------------------------------------------------------


class TestNarrowExceptionSurface:
    @pytest.mark.asyncio
    async def test_call_direct_http_status_error_not_retried(self, monkeypatch):
        sleep_calls: list[float] = []

        async def fake_sleep(d):
            sleep_calls.append(d)

        request = httpx.Request("POST", "https://nlip.example.com/nlip/")
        http_response = httpx.Response(500, request=request)
        status_error = httpx.HTTPStatusError(
            "server error", request=request, response=http_response
        )

        post_mock = AsyncMock(return_value=_FakeResponse(raise_exc=status_error))
        _patch_async_client(monkeypatch, post_mock)
        monkeypatch.setattr("lionagi.ln.concurrency.patterns.anyio.sleep", fake_sleep)

        with pytest.raises(httpx.HTTPStatusError):
            await nlip_mod._call_direct(
                "https://nlip.example.com", MESSAGES, timeout=5.0, max_retries=3
            )

        assert post_mock.call_count == 1
        assert sleep_calls == []

    @pytest.mark.asyncio
    async def test_call_nlip_sdk_validation_error_not_retried(self, monkeypatch):
        """model_validate failures happen outside the retry helper and propagate on first occurrence."""
        sleep_calls: list[float] = []

        async def fake_sleep(d):
            sleep_calls.append(d)

        post_mock = AsyncMock(return_value=_FakeResponse(data={"content": "ok"}))
        _patch_async_client(monkeypatch, post_mock)
        monkeypatch.setattr("lionagi.ln.concurrency.patterns.anyio.sleep", fake_sleep)

        class _ExplodingNlipMessage(_FakeNlipMessage):
            @classmethod
            def model_validate(cls, data):
                raise ValueError("bad NLIP payload")

        stubs = _nlip_sdk_stubs()
        stubs["nlip_sdk.nlip"].NLIP_Message = _ExplodingNlipMessage

        with patch.dict(sys.modules, stubs):
            with pytest.raises(ValueError, match="bad NLIP payload"):
                await nlip_mod._call_nlip_sdk(
                    "https://nlip.example.com", MESSAGES, timeout=5.0, max_retries=3
                )

        assert post_mock.call_count == 1
        assert sleep_calls == []


# ---------------------------------------------------------------------------
# (d) sdk-path warning logging preserved; direct path stays silent
# ---------------------------------------------------------------------------


class TestRetryLogging:
    @pytest.mark.asyncio
    async def test_call_nlip_sdk_warns_once_per_non_final_retry(self, monkeypatch, caplog):
        async def fake_sleep(d):
            pass

        post_mock = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        _patch_async_client(monkeypatch, post_mock)
        monkeypatch.setattr("lionagi.ln.concurrency.patterns.anyio.sleep", fake_sleep)

        with patch.dict(sys.modules, _nlip_sdk_stubs()):
            with caplog.at_level("WARNING", logger="lionagi.providers.ag2.nlip"):
                with pytest.raises(httpx.TimeoutException):
                    await nlip_mod._call_nlip_sdk(
                        "https://nlip.example.com", MESSAGES, timeout=5.0, max_retries=3
                    )

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        # 3 attempts -> warn on the first 2 (non-final), silent on the final raise.
        assert len(warnings) == 2
        assert "NLIP timeout (attempt 1/3)" in warnings[0].getMessage()
        assert "NLIP timeout (attempt 2/3)" in warnings[1].getMessage()

    @pytest.mark.asyncio
    async def test_call_direct_stays_silent_on_retry(self, monkeypatch, caplog):
        async def fake_sleep(d):
            pass

        post_mock = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        _patch_async_client(monkeypatch, post_mock)
        monkeypatch.setattr("lionagi.ln.concurrency.patterns.anyio.sleep", fake_sleep)

        with caplog.at_level("WARNING", logger="lionagi.providers.ag2.nlip"):
            with pytest.raises(httpx.TimeoutException):
                await nlip_mod._call_direct(
                    "https://nlip.example.com", MESSAGES, timeout=5.0, max_retries=3
                )

        assert [r for r in caplog.records if r.levelname == "WARNING"] == []


# ---------------------------------------------------------------------------
# Success paths (sanity: retry then succeed still parses correctly)
# ---------------------------------------------------------------------------


class TestSuccessAfterTransientFailure:
    @pytest.mark.asyncio
    async def test_call_direct_succeeds_after_one_timeout(self, monkeypatch):
        async def fake_sleep(d):
            pass

        post_mock = AsyncMock(
            side_effect=[
                httpx.TimeoutException("timeout"),
                _FakeResponse(data={"content": "hi there"}),
            ]
        )
        _patch_async_client(monkeypatch, post_mock)
        monkeypatch.setattr("lionagi.ln.concurrency.patterns.anyio.sleep", fake_sleep)

        result = await nlip_mod._call_direct(
            "https://nlip.example.com", MESSAGES, timeout=5.0, max_retries=3
        )

        assert result == {"content": "hi there", "context": None, "input_required": None}
        assert post_mock.call_count == 2
