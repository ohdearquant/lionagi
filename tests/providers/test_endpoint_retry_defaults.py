# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Endpoints built without a config take the ``EndpointConfig`` default ``max_retries`` of 3,
a cap on total attempts."""

from importlib import import_module

import pytest

ENDPOINTS = [
    ("lionagi.providers.exa.contents", "ExaContentsEndpoint"),
    ("lionagi.providers.exa.find_similar", "ExaFindSimilarEndpoint"),
    ("lionagi.providers.exa.search", "ExaSearchEndpoint"),
    ("lionagi.providers.firecrawl.crawl", "FirecrawlCrawlEndpoint"),
    ("lionagi.providers.firecrawl.map", "FirecrawlMapEndpoint"),
    ("lionagi.providers.firecrawl.scrape", "FirecrawlScrapeEndpoint"),
    ("lionagi.providers.groq.audio_transcription", "GroqAudioTranscriptionEndpoint"),
    ("lionagi.providers.openai.audio", "OpenaiAudioSpeechEndpoint"),
    ("lionagi.providers.openai.audio", "OpenaiAudioTranscriptionEndpoint"),
    ("lionagi.providers.openai.images", "OpenaiImageGenerationEndpoint"),
    ("lionagi.providers.openai.images", "OpenaiImageEditEndpoint"),
    ("lionagi.providers.tavily.search", "TavilySearchEndpoint"),
    ("lionagi.providers.tavily.search", "TavilyExtractEndpoint"),
]


@pytest.mark.parametrize(("module", "name"), ENDPOINTS, ids=[name for _, name in ENDPOINTS])
def test_an_endpoint_without_a_config_allows_three_attempts(module, name):
    endpoint = getattr(import_module(module), name)()

    assert endpoint.config.max_retries == 3


@pytest.mark.parametrize(("module", "name"), ENDPOINTS, ids=[name for _, name in ENDPOINTS])
def test_an_endpoint_keeps_the_attempt_cap_it_is_given(module, name):
    endpoint = getattr(import_module(module), name)(max_retries=5)

    assert endpoint.config.max_retries == 5
