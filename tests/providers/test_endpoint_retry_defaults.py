# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Endpoints built without a config retry three times, the ``EndpointConfig`` default."""

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
def test_an_endpoint_without_a_config_retries_three_times(module, name):
    endpoint = getattr(import_module(module), name)()

    assert endpoint.config.max_retries == 3


@pytest.mark.parametrize(("module", "name"), ENDPOINTS, ids=[name for _, name in ENDPOINTS])
def test_an_endpoint_keeps_the_retry_count_it_is_given(module, name):
    endpoint = getattr(import_module(module), name)(max_retries=5)

    assert endpoint.config.max_retries == 5
