# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Endpoints built without a config take the ``EndpointConfig`` default ``max_retries`` of 3,
a cap on total attempts."""

from importlib import import_module

import pytest

ENDPOINTS = [
    ("lionhub.providers.exa.contents", "ExaContentsEndpoint"),
    ("lionhub.providers.exa.find_similar", "ExaFindSimilarEndpoint"),
    ("lionhub.providers.exa.search", "ExaSearchEndpoint"),
    ("lionhub.providers.firecrawl.crawl", "FirecrawlCrawlEndpoint"),
    ("lionhub.providers.firecrawl.map", "FirecrawlMapEndpoint"),
    ("lionhub.providers.firecrawl.scrape", "FirecrawlScrapeEndpoint"),
    ("lionhub.providers.groq.audio_transcription", "GroqAudioTranscriptionEndpoint"),
    ("lionhub.providers.openai.audio", "OpenaiAudioSpeechEndpoint"),
    ("lionhub.providers.openai.audio", "OpenaiAudioTranscriptionEndpoint"),
    ("lionhub.providers.openai.images", "OpenaiImageGenerationEndpoint"),
    ("lionhub.providers.openai.images", "OpenaiImageEditEndpoint"),
    ("lionhub.providers.tavily.search", "TavilySearchEndpoint"),
    ("lionhub.providers.tavily.search", "TavilyExtractEndpoint"),
]


@pytest.mark.parametrize(("module", "name"), ENDPOINTS, ids=[name for _, name in ENDPOINTS])
def test_an_endpoint_without_a_config_allows_three_attempts(module, name):
    endpoint = getattr(import_module(module), name)()

    assert endpoint.config.max_retries == 3


@pytest.mark.parametrize(("module", "name"), ENDPOINTS, ids=[name for _, name in ENDPOINTS])
def test_an_endpoint_keeps_the_attempt_cap_it_is_given(module, name):
    endpoint = getattr(import_module(module), name)(max_retries=5)

    assert endpoint.config.max_retries == 5
