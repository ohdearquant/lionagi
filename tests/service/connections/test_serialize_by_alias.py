# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Payload-parity tests for the serialize_by_alias flag on EndpointConfig.

Each test verifies that the flag-based path in the base Endpoint.create_payload
produces the same dict as the old per-file override did (model_dump by_alias=True,
exclude_none=True).
"""

from __future__ import annotations

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

# ---------------------------------------------------------------------------
# Helper: simulate the old override behaviour for a given model class + inputs
# ---------------------------------------------------------------------------


def _old_override_payload(model_cls, config_kwargs: dict, request: dict, extra_kwargs: dict):
    """Reproduces the pre-refactor override logic verbatim."""
    merged = {**config_kwargs, **request, **extra_kwargs}
    obj = model_cls.model_validate(merged)
    return obj.model_dump(by_alias=True, exclude_none=True)


def _new_flag_payload(endpoint: Endpoint, request: dict):
    payload, _ = endpoint.create_payload(request)
    return payload


# ---------------------------------------------------------------------------
# Exa Search
# ---------------------------------------------------------------------------


class TestExaSearchPayloadParity:
    def test_basic_query(self):
        from lionagi.providers.exa.search import ExaSearchEndpoint, ExaSearchRequest

        ep = ExaSearchEndpoint()
        req = {"query": "lionagi framework", "num_results": 5}

        expected = _old_override_payload(ExaSearchRequest, {}, req, {})
        got = _new_flag_payload(ep, req)

        assert got == expected

    def test_alias_fields_present(self):
        from lionagi.providers.exa.search import ExaSearchEndpoint

        ep = ExaSearchEndpoint()
        req = {"query": "test", "num_results": 3}
        payload, _ = ep.create_payload(req)
        # alias "numResults" must appear, not the Python name
        assert "numResults" in payload
        assert "num_results" not in payload

    def test_none_fields_excluded(self):
        from lionagi.providers.exa.search import ExaSearchEndpoint

        ep = ExaSearchEndpoint()
        req = {"query": "test"}
        payload, _ = ep.create_payload(req)
        assert "includeDomains" not in payload
        assert "excludeDomains" not in payload


# ---------------------------------------------------------------------------
# Exa Contents
# ---------------------------------------------------------------------------


class TestExaContentsPayloadParity:
    def test_basic_ids(self):
        from lionagi.providers.exa.contents import ExaContentsEndpoint, ExaContentsRequest

        ep = ExaContentsEndpoint()
        req = {"ids": ["https://example.com"]}

        expected = _old_override_payload(ExaContentsRequest, {}, req, {})
        got = _new_flag_payload(ep, req)

        assert got == expected

    def test_alias_fields_present(self):
        from lionagi.providers.exa.contents import ExaContentsEndpoint

        ep = ExaContentsEndpoint()
        req = {"ids": ["https://example.com"]}
        payload, _ = ep.create_payload(req)
        assert "ids" in payload


# ---------------------------------------------------------------------------
# Exa FindSimilar
# ---------------------------------------------------------------------------


class TestExaFindSimilarPayloadParity:
    def test_basic_url(self):
        from lionagi.providers.exa.find_similar import ExaFindSimilarEndpoint, ExaFindSimilarRequest

        ep = ExaFindSimilarEndpoint()
        req = {"url": "https://example.com"}

        expected = _old_override_payload(ExaFindSimilarRequest, {}, req, {})
        got = _new_flag_payload(ep, req)

        assert got == expected

    def test_alias_fields_present(self):
        from lionagi.providers.exa.find_similar import ExaFindSimilarEndpoint

        ep = ExaFindSimilarEndpoint()
        req = {"url": "https://example.com", "num_results": 5}
        payload, _ = ep.create_payload(req)
        assert "numResults" in payload
        assert "num_results" not in payload


# ---------------------------------------------------------------------------
# Firecrawl Scrape
# ---------------------------------------------------------------------------


class TestFirecrawlScrapePayloadParity:
    def test_basic_url(self):
        from lionagi.providers.firecrawl.scrape import (
            FirecrawlScrapeEndpoint,
            FirecrawlScrapeRequest,
        )

        ep = FirecrawlScrapeEndpoint()
        req = {"url": "https://example.com"}

        expected = _old_override_payload(FirecrawlScrapeRequest, {}, req, {})
        got = _new_flag_payload(ep, req)

        assert got == expected

    def test_alias_fields_present(self):
        from lionagi.providers.firecrawl.scrape import FirecrawlScrapeEndpoint

        ep = FirecrawlScrapeEndpoint()
        req = {"url": "https://example.com", "only_main_content": True}
        payload, _ = ep.create_payload(req)
        assert "onlyMainContent" in payload
        assert "only_main_content" not in payload

    def test_none_fields_excluded(self):
        from lionagi.providers.firecrawl.scrape import FirecrawlScrapeEndpoint

        ep = FirecrawlScrapeEndpoint()
        req = {"url": "https://example.com"}
        payload, _ = ep.create_payload(req)
        assert "includeTags" not in payload
        assert "excludeTags" not in payload


# ---------------------------------------------------------------------------
# Firecrawl Map
# ---------------------------------------------------------------------------


class TestFirecrawlMapPayloadParity:
    def test_basic_url(self):
        from lionagi.providers.firecrawl.map import FirecrawlMapEndpoint, FirecrawlMapRequest

        ep = FirecrawlMapEndpoint()
        req = {"url": "https://example.com"}

        expected = _old_override_payload(FirecrawlMapRequest, {}, req, {})
        got = _new_flag_payload(ep, req)

        assert got == expected


# ---------------------------------------------------------------------------
# Firecrawl Crawl
# ---------------------------------------------------------------------------


class TestFirecrawlCrawlPayloadParity:
    def test_basic_url(self):
        from lionagi.providers.firecrawl.crawl import FirecrawlCrawlEndpoint, FirecrawlCrawlRequest

        ep = FirecrawlCrawlEndpoint()
        req = {"url": "https://example.com"}

        expected = _old_override_payload(FirecrawlCrawlRequest, {}, req, {})
        got = _new_flag_payload(ep, req)

        assert got == expected

    def test_alias_fields_present(self):
        from lionagi.providers.firecrawl.crawl import FirecrawlCrawlEndpoint

        ep = FirecrawlCrawlEndpoint()
        req = {"url": "https://example.com", "max_depth": 3}
        payload, _ = ep.create_payload(req)
        assert "maxDepth" in payload
        assert "max_depth" not in payload


# ---------------------------------------------------------------------------
# Default-false guard: serialize_by_alias=False must NOT affect non-Exa/Firecrawl
# ---------------------------------------------------------------------------


class TestSerializeByAliasDefault:
    def test_default_is_false(self):
        config = EndpointConfig(
            name="openai_chat",
            provider="openai",
            endpoint="chat/completions",
            api_key="dummy-key-for-testing",
        )
        assert config.serialize_by_alias is False

    def test_openai_payload_unaffected(self):
        from pydantic import BaseModel, Field

        class ChatRequest(BaseModel):
            messages: list
            max_tokens: int | None = Field(None, alias="maxTokens")

        config = EndpointConfig(
            name="openai_chat",
            provider="openai",
            endpoint="chat/completions",
            api_key="dummy-key-for-testing",
            request_options=ChatRequest,
        )
        ep = Endpoint(config=config)
        req = {"messages": [{"role": "user", "content": "hi"}]}
        payload, _ = ep.create_payload(req)
        # Without serialize_by_alias, validate_payload returns the input dict unchanged
        assert "messages" in payload
        # alias field not present because serialize_by_alias=False → raw dict returned
        assert "maxTokens" not in payload

    def test_flag_endpoint_configs_are_true(self):
        from lionagi.providers.exa.contents import ExaContentsEndpoint
        from lionagi.providers.exa.find_similar import ExaFindSimilarEndpoint
        from lionagi.providers.exa.search import ExaSearchEndpoint
        from lionagi.providers.firecrawl.crawl import FirecrawlCrawlEndpoint
        from lionagi.providers.firecrawl.map import FirecrawlMapEndpoint
        from lionagi.providers.firecrawl.scrape import FirecrawlScrapeEndpoint

        for cls in (
            ExaSearchEndpoint,
            ExaContentsEndpoint,
            ExaFindSimilarEndpoint,
            FirecrawlScrapeEndpoint,
            FirecrawlMapEndpoint,
            FirecrawlCrawlEndpoint,
        ):
            ep = cls()
            assert ep.config.serialize_by_alias is True, (
                f"{cls.__name__} must have serialize_by_alias=True"
            )
