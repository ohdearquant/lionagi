# SPDX-License-Identifier: Apache-2.0

"""Pin how ``EndpointRegistry.match`` resolves every built-in provider.

``ROWS`` is a literal table of the built-in endpoints: provider, provider
aliases, endpoint key, endpoint aliases, the endpoint class as
``module:qualname`` and whether it is the provider's default (the first endpoint
registered for it). The resolution tests go through the public
``match_endpoint`` and compare against that table, with one exception: with no
endpoint named, the provider's default is the first entry registered for it,
and which entry registers first depends on which provider modules the process
imported before the registry loaded. So the in-process test pins that rule
against the registry's own entries, and the table's absolute defaults are
pinned in a fresh interpreter, where nothing else has been imported first. One
more fresh-interpreter test records what importing a provider module ahead of
the registry does to the default today. A separate group of tests checks the
table against what is registered, so a stale table fails loudly. Order across
providers is not significant; order inside one provider matters only through
its default.
"""

import importlib
import json
import os
import subprocess
import sys
import warnings
from typing import NamedTuple

import pytest

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.connections.registry import EndpointRegistry, ProviderNotFoundError
from tests._core_imports import ROOT


class Row(NamedTuple):
    """One built-in endpoint."""

    provider: str
    provider_aliases: tuple[str, ...]
    endpoint: str
    aliases: tuple[str, ...]
    cls: str
    default: bool


# fmt: off
ROWS: tuple[Row, ...] = (
    Row("openai", (), "chat/completions", ("chat",),
        "lionhub.providers.openai.chat:OpenaiChatEndpoint", True),
    Row("openai", (), "responses", ("response",),
        "lionhub.providers.openai.response:OpenaiResponseEndpoint", False),
    Row("openai", (), "embeddings", ("embed",),
        "lionhub.providers.openai.embed:OpenaiEmbedEndpoint", False),
    Row("openai", (), "audio/speech", ("tts",),
        "lionhub.providers.openai.audio:OpenaiAudioSpeechEndpoint", False),
    Row("openai", (), "audio/transcriptions", ("stt", "whisper"),
        "lionhub.providers.openai.audio:OpenaiAudioTranscriptionEndpoint", False),
    Row("openai", (), "images/generations", ("dalle", "image"),
        "lionhub.providers.openai.images:OpenaiImageGenerationEndpoint", False),
    Row("openai", (), "images/edits", ("image_edit",),
        "lionhub.providers.openai.images:OpenaiImageEditEndpoint", False),
    Row("openai", (), "batches", ("batch",),
        "lionhub.providers.openai.batch:OpenaiBatchEndpoint", False),
    Row("codex", ("openai-codex",), "query_cli", ("cli", "code"),
        "lionhub.providers.openai.codex:CodexCLIEndpoint", True),
    Row("anthropic", (), "messages", ("chat",),
        "lionhub.providers.anthropic.messages:AnthropicMessagesEndpoint", True),
    Row("claude_code", ("claude-code", "claude"), "query_cli", ("cli", "code"),
        "lionhub.providers.anthropic.claude_code:ClaudeCodeCLIEndpoint", True),
    Row("ollama", (), "chat/completions", ("chat",),
        "lionhub.providers.ollama.chat:OllamaChatEndpoint", True),
    Row("ollama", (), "embeddings", ("embed",),
        "lionhub.providers.ollama.embed:OllamaEmbedEndpoint", False),
    Row("ollama", (), "generate", ("generate", "completion"),
        "lionhub.providers.ollama.generate:OllamaGenerateEndpoint", False),
    Row("lattice", (), "chat/completions", ("chat",),
        "lionhub.providers.lattice.chat:LatticeChatEndpoint", True),
    Row("lattice", (), "embeddings", ("embed",),
        "lionhub.providers.lattice.embed:LatticeEmbedEndpoint", False),
    Row("tavily", (), "search", (),
        "lionhub.providers.tavily.search:TavilySearchEndpoint", True),
    Row("tavily", (), "extract", (),
        "lionhub.providers.tavily.search:TavilyExtractEndpoint", False),
    Row("exa", (), "search", (),
        "lionhub.providers.exa.search:ExaSearchEndpoint", True),
    Row("exa", (), "contents", ("get_contents",),
        "lionhub.providers.exa.contents:ExaContentsEndpoint", False),
    Row("exa", (), "findSimilar", ("similar", "find_similar"),
        "lionhub.providers.exa.find_similar:ExaFindSimilarEndpoint", False),
    Row("firecrawl", (), "v1/scrape", ("scrape",),
        "lionhub.providers.firecrawl.scrape:FirecrawlScrapeEndpoint", True),
    Row("firecrawl", (), "v1/map", ("map",),
        "lionhub.providers.firecrawl.map:FirecrawlMapEndpoint", False),
    Row("firecrawl", (), "v1/crawl", ("crawl",),
        "lionhub.providers.firecrawl.crawl:FirecrawlCrawlEndpoint", False),
    Row("perplexity", (), "chat/completions", ("chat",),
        "lionhub.providers.perplexity.chat:PerplexityChatEndpoint", True),
    Row("nvidia_nim", ("nvidia", "nim"), "chat/completions", ("chat",),
        "lionhub.providers.nvidia_nim.chat:NvidiaNimChatEndpoint", True),
    Row("nvidia_nim", ("nvidia", "nim"), "embeddings", ("embed",),
        "lionhub.providers.nvidia_nim.embed:NvidiaNimEmbedEndpoint", False),
    Row("deepseek", (), "chat/completions", ("chat",),
        "lionhub.providers.deepseek.chat:DeepseekChatEndpoint", True),
    Row("gemini", ("gemini-api",), "chat/completions", ("chat",),
        "lionhub.providers.google.chat:GeminiChatEndpoint", True),
    Row("gemini_code", ("gemini-code", "gemini_cli", "gemini-cli"), "query_cli", ("cli",),
        "lionhub.providers.google.gemini_code:GeminiCLIEndpoint", True),
    Row("groq", (), "chat/completions", ("chat",),
        "lionhub.providers.groq.chat:GroqChatEndpoint", True),
    Row("groq", (), "audio/transcriptions", ("whisper", "stt"),
        "lionhub.providers.groq.audio_transcription:GroqAudioTranscriptionEndpoint", False),
    Row("pi", ("pi-code", "pi_code"), "query_cli", ("cli", "code"),
        "lionhub.providers.pi.cli:PiCLIEndpoint", True),
    Row("openrouter", ("open-router",), "chat/completions", ("chat", "chat/completions"),
        "lionhub.providers.openrouter.chat:OpenRouterEndpoint", True),
    Row("ag2", ("autogen",), "group_chat", ("groupchat", "chat"),
        "lionhub.providers.ag2.groupchat:AG2GroupChatEndpoint", True),
    Row("ag2", ("autogen",), "agent", ("beta", "ask"),
        "lionhub.providers.ag2.agent:AG2BetaEndpoint", False),
    Row("ag2", ("autogen",), "nlip", ("nlip_remote", "remote"),
        "lionhub.providers.ag2.nlip:AG2NlipEndpoint", False),
    Row("scripted", (), "chat/completions", ("chat", "query_cli", "cli"),
        "lionagi.testing._endpoint:ScriptedEndpoint", True),
)
# fmt: on

UNLISTED_ENDPOINT = "unlisted/endpoint"
UNKNOWN_PROVIDER = "no_such_provider"
CUSTOM_BASE_URL = "https://custom.example.com/v1"
BUILTIN_MODULES = ("lionhub.providers.", "lionagi.testing._endpoint")

BY_PROVIDER: dict[str, list[Row]] = {}
for _row in ROWS:
    BY_PROVIDER.setdefault(_row.provider, []).append(_row)


def _class_of(row: Row) -> type:
    """Import and return the class a table row names."""
    module, _, name = row.cls.partition(":")
    return getattr(importlib.import_module(module), name)


def _spellings(*names: str) -> list[str]:
    """The distinct names, in the order given."""
    return list(dict.fromkeys(names))


def _mixed_case(name: str) -> str:
    """Alternate upper and lower case letter by letter, starting upper."""
    return "".join(c.upper() if i % 2 == 0 else c.lower() for i, c in enumerate(name))


def _resolve(provider: str, endpoint: str, **kwargs):
    """Call ``match_endpoint`` and return the result with every warning raised."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = match_endpoint(provider=provider, endpoint=endpoint, **kwargs)
    return result, caught


# This checkout and its lionhub package, put first on each child's path so a fresh
# interpreter reads the providers in this tree and not an installed copy.
_CHILD_PATH = (str(ROOT), str(ROOT / "lionhub" / "src"))


def _run_child(code, *args):
    """Run ``code`` in a fresh interpreter and return the JSON object it printed last."""
    env = dict(os.environ)
    inherited = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([*_CHILD_PATH, *([inherited] if inherited else [])])
    proc = subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=240,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _builtin_entries():
    """Registry entries contributed by the built-in providers, in registration order."""
    return [e for e in EndpointRegistry._entries if e.cls.__module__.startswith(BUILTIN_MODULES)]


def _first_registered_class(name: str) -> type:
    """The class of the first registered entry whose provider or provider alias is
    ``name``, read from the registry now."""
    for entry in EndpointRegistry._entries:
        if name == entry.meta.provider or name in entry.meta.provider_aliases:
            return entry.cls
    raise AssertionError(f"no registered entry for provider {name!r}")


KEY_CASES = [pytest.param(r, id=f"{r.provider}/{r.endpoint}") for r in ROWS]

ENDPOINT_ALIAS_CASES = [
    pytest.param(r, alias, id=f"{r.provider}/{alias}")
    for r in ROWS
    for alias in _spellings(*r.aliases)
    if alias != r.endpoint
]

PROVIDER_ALIAS_CASES = [
    pytest.param(r, alias, id=f"{alias}/{r.endpoint}") for r in ROWS for alias in r.provider_aliases
]

CASE_VARIANT_CASES = [
    pytest.param(r, variant, id=f"{variant}/{r.endpoint}")
    for r in ROWS
    for name in (r.provider, *r.provider_aliases)
    for variant in _spellings(name.upper(), _mixed_case(name))
]

# Every provider name and provider alias, mapped to its table default class.
DEFAULT_CLASS = {
    name: r.cls for r in ROWS if r.default for name in (r.provider, *r.provider_aliases)
}

SINGLE_ENTRY_CASES = [
    pytest.param(rows[0], name, id=name)
    for rows in BY_PROVIDER.values()
    if len(rows) == 1
    for name in (rows[0].provider, *rows[0].provider_aliases)
]

MULTI_ENTRY_CASES = [
    pytest.param(name, id=name)
    for provider, rows in BY_PROVIDER.items()
    if len(rows) > 1
    for name in (provider, *rows[0].provider_aliases)
]


@pytest.fixture(scope="module", autouse=True)
def _builtin_providers_loaded():
    """Make sure the registry has loaded the built-in providers before any test reads it.

    It does not decide which endpoint registers first for a provider: that follows
    import order, so a provider module another test module imported earlier in the
    same process can already have registered a different one first.
    """
    EndpointRegistry.list_providers()


# -- the table itself -------------------------------------------------------


def test_table_marks_one_default_and_shares_provider_aliases_per_provider():
    for provider, rows in BY_PROVIDER.items():
        assert sum(r.default for r in rows) == 1, provider
        assert {r.provider_aliases for r in rows} == {rows[0].provider_aliases}, provider


@pytest.mark.parametrize("row", KEY_CASES)
def test_table_row_matches_the_registered_entry(row):
    entries = {(e.meta.provider, e.meta.endpoint): e for e in _builtin_entries()}
    entry = entries.get((row.provider, row.endpoint))
    assert entry is not None, f"{row.provider}/{row.endpoint} is not registered"
    assert f"{entry.cls.__module__}:{entry.cls.__qualname__}" == row.cls
    assert tuple(entry.meta.aliases) == row.aliases
    assert tuple(entry.meta.provider_aliases) == row.provider_aliases


def test_table_lists_every_built_in_entry():
    table = {(r.provider, r.endpoint) for r in ROWS}
    registered = {(e.meta.provider, e.meta.endpoint) for e in _builtin_entries()}
    assert registered == table


@pytest.mark.parametrize("provider", list(BY_PROVIDER))
def test_table_default_is_one_of_the_registered_entries(provider):
    default = next(r for r in BY_PROVIDER[provider] if r.default)
    registered = {e.meta.endpoint for e in _builtin_entries() if e.meta.provider == provider}
    assert default.endpoint in registered


# -- 1. every pair resolves by key, endpoint alias and provider alias -------


@pytest.mark.parametrize("row", KEY_CASES)
def test_endpoint_key_resolves_to_its_class(row):
    result = match_endpoint(provider=row.provider, endpoint=row.endpoint)
    assert type(result) is _class_of(row)


@pytest.mark.parametrize(("row", "alias"), ENDPOINT_ALIAS_CASES)
def test_endpoint_alias_resolves_to_its_class(row, alias):
    result = match_endpoint(provider=row.provider, endpoint=alias)
    assert type(result) is _class_of(row)


@pytest.mark.parametrize(("row", "alias"), PROVIDER_ALIAS_CASES)
def test_provider_alias_resolves_to_its_class(row, alias):
    result = match_endpoint(provider=alias, endpoint=row.endpoint)
    assert type(result) is _class_of(row)


# -- 2. provider case and padding -------------------------------------------


@pytest.mark.parametrize(("row", "provider"), CASE_VARIANT_CASES)
def test_provider_letter_case_does_not_change_the_result(row, provider):
    result = match_endpoint(provider=provider, endpoint=row.endpoint)
    assert type(result) is _class_of(row)


@pytest.mark.parametrize("provider", [" openai", "openai ", " openai ", "\topenai"])
def test_padded_provider_is_not_found(provider):
    with pytest.raises(ProviderNotFoundError) as excinfo:
        match_endpoint(provider=provider, endpoint="chat/completions")
    assert f"provider {provider!r};" in str(excinfo.value)


def test_padded_provider_with_base_url_warns_and_routes_generic():
    with pytest.warns(DeprecationWarning, match="provider ' openai' is not registered"):
        result = match_endpoint(
            provider=" openai", endpoint="chat/completions", base_url=CUSTOM_BASE_URL
        )
    assert type(result) is Endpoint
    # The registry lookup keeps the padding, so the name is unregistered; the
    # endpoint config then strips and lower-cases the name it stores.
    assert result.config.provider == "openai"


# -- 3. no endpoint named: the provider's default ---------------------------


@pytest.mark.parametrize("provider", list(DEFAULT_CLASS))
def test_no_endpoint_named_resolves_to_the_first_registered_entry(provider):
    expected = _first_registered_class(provider)
    result = match_endpoint(provider=provider, endpoint="")
    assert type(result) is expected


_DEFAULTS_CHILD = r"""
import json
import sys

from lionagi.service.connections.match_endpoint import match_endpoint

classes = {}
for name in sys.argv[1:]:
    cls = type(match_endpoint(provider=name, endpoint=""))
    classes[name] = f"{cls.__module__}:{cls.__qualname__}"
print(json.dumps(classes))
"""

_AUDIO_FIRST_CHILD = r"""
import json

import lionhub.providers.openai.audio
from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.connections.registry import EndpointRegistry

loaded_before_match = EndpointRegistry._loaded
cls = type(match_endpoint(provider="openai", endpoint=""))
print(
    json.dumps(
        {
            "loaded_before_match": loaded_before_match,
            "cls": f"{cls.__module__}:{cls.__qualname__}",
        }
    )
)
"""


def test_default_endpoint_in_a_fresh_interpreter_is_the_table_default():
    classes = _run_child(_DEFAULTS_CHILD, *DEFAULT_CLASS)
    assert classes == DEFAULT_CLASS


def test_default_endpoint_follows_import_order_today():
    # Records today's behaviour, not a promise: a provider module imported before the
    # registry loads registers its endpoints first, and the first one registered for a
    # provider is what an empty endpoint resolves to.
    result = _run_child(_AUDIO_FIRST_CHILD)
    assert result["loaded_before_match"] is False
    assert result["cls"] == "lionhub.providers.openai.audio:OpenaiAudioSpeechEndpoint"


# -- 4. an endpoint the provider does not list ------------------------------


@pytest.mark.parametrize(("row", "provider"), SINGLE_ENTRY_CASES)
def test_single_entry_provider_accepts_an_unlisted_endpoint(row, provider):
    result = match_endpoint(provider=provider, endpoint=UNLISTED_ENDPOINT)
    assert type(result) is _class_of(row)


@pytest.mark.parametrize("provider", MULTI_ENTRY_CASES)
def test_multi_entry_provider_routes_an_unlisted_endpoint_generic(provider):
    result, caught = _resolve(provider, UNLISTED_ENDPOINT)
    assert type(result) is Endpoint
    assert result.config.provider == provider
    assert result.config.endpoint == UNLISTED_ENDPOINT
    assert result.config.openai_compatible is True
    assert caught == []


@pytest.mark.parametrize("endpoint", ["CHAT", " chat", "chat ", "Chat/Completions"])
def test_endpoint_is_compared_exactly(endpoint):
    result, caught = _resolve("openai", endpoint)
    assert type(result) is Endpoint
    assert result.config.endpoint == endpoint
    assert caught == []


# -- 5. a provider nothing registered ---------------------------------------


@pytest.mark.parametrize("endpoint", ["chat", ""])
def test_unknown_provider_with_base_url_warns_and_routes_generic(endpoint):
    with pytest.warns(
        DeprecationWarning, match=f"provider '{UNKNOWN_PROVIDER}' is not registered"
    ) as record:
        result = match_endpoint(
            provider=UNKNOWN_PROVIDER, endpoint=endpoint, base_url=CUSTOM_BASE_URL
        )
    assert len(record) == 1
    assert record[0].filename == __file__
    assert type(result) is Endpoint
    assert result.config.provider == UNKNOWN_PROVIDER
    assert result.config.endpoint == (endpoint or "chat/completions")
    assert result.config.base_url == CUSTOM_BASE_URL
    assert result.config.openai_compatible is True


@pytest.mark.parametrize("endpoint", ["chat", ""])
def test_unknown_provider_without_base_url_raises_naming_the_provider(endpoint):
    with pytest.raises(ProviderNotFoundError) as excinfo:
        match_endpoint(provider=UNKNOWN_PROVIDER, endpoint=endpoint)
    assert f"provider '{UNKNOWN_PROVIDER}';" in str(excinfo.value)


@pytest.mark.parametrize("extra", [{}, {"base_url": CUSTOM_BASE_URL}], ids=["bare", "base_url"])
def test_unknown_provider_with_openai_compatible_routes_generic_without_warning(extra):
    result, caught = _resolve(UNKNOWN_PROVIDER, "chat", openai_compatible=True, **extra)
    assert type(result) is Endpoint
    assert result.config.provider == UNKNOWN_PROVIDER
    assert result.config.endpoint == "chat"
    assert result.config.openai_compatible is True
    assert caught == []


@pytest.mark.parametrize("row", KEY_CASES)
def test_registered_provider_with_openai_compatible_keeps_its_class(row):
    result = match_endpoint(provider=row.provider, endpoint=row.endpoint, openai_compatible=True)
    assert type(result) is _class_of(row)
