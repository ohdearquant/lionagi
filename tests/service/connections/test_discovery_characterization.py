# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Characterize what a cold provider load registers, before the discovery contract replaces it.

Every test runs the cold load in a fresh interpreter, so module caches from other tests cannot
leak in, and asserts on the JSON the child prints. Expected values are literals written out in
this file, not derived from the registry's module list, so a change to that list is a failure.
"""

import json
import os
import subprocess
import sys

import pytest

from tests._core_imports import ROOT

# Marks "leave LIONAGI_CHAT_PROVIDER in the child exactly as this process has it".
_INHERIT = object()

# This checkout and its lionhub package, put first on each child's path so the cold load reads
# the providers in this tree and not an installed copy.
_CHILD_PATH = (str(ROOT), str(ROOT / "lionhub" / "src"))

_COLD_CHILD = r"""
import json
import sys

from lionagi.service.connections.registry import EndpointRegistry


def provider_modules():
    return sorted(
        m
        for m in sys.modules
        if m.startswith("lionhub.providers.") or m == "lionagi.testing._endpoint"
    )


before = provider_modules()
loaded_before = EndpointRegistry._loaded
EndpointRegistry._ensure_loaded()
print(
    json.dumps(
        {
            "loaded_before": loaded_before,
            "loaded_after": EndpointRegistry._loaded,
            "modules_before": before,
            "modules_after": provider_modules(),
            "rows": EndpointRegistry.list_providers(),
        }
    )
)
"""

_FAILING_IMPORT_CHILD = r"""
import json
import logging
import sys

import lionagi.service.connections.registry as registry
from lionagi.service.connections.registry import EndpointRegistry

BAD = sys.argv[1]
real_import_module = registry.import_module


def import_module_failing_for_one(name, *args, **kwargs):
    if name == BAD:
        raise ImportError("simulated missing module for " + name)
    return real_import_module(name, *args, **kwargs)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append([record.levelname, record.getMessage()])


capture = Capture()
registry.logger.addHandler(capture)
registry.logger.setLevel(logging.DEBUG)
registry.import_module = import_module_failing_for_one
EndpointRegistry._ensure_loaded()
print(
    json.dumps(
        {
            "records": capture.records,
            "bad_module_imported": BAD in sys.modules,
            "rows": EndpointRegistry.list_providers(),
        }
    )
)
"""

_SCRIPTED_CHILD = r"""
import json
import sys

from lionagi.service.connections.registry import EndpointRegistry

EndpointRegistry._ensure_loaded()
print(
    json.dumps(
        {
            "scripted_rows": [
                row for row in EndpointRegistry.list_providers() if row["provider"] == "scripted"
            ],
            "scripted_module_imported": "lionagi.testing._endpoint" in sys.modules,
        }
    )
)
"""

# The one provider module the failing-import test makes raise ImportError: the groq
# transcription endpoint.
_FAILED_MODULE = "lionhub.providers.groq.audio_transcription"

# Every (provider, endpoint) pair a cold load registers today, with its endpoint aliases (sorted).
# ADR-0140 D1 replaces the fixed module list behind this table with a hub-owned manifest and
# requires the replacement catalog to preserve current matching and endpoint aliases; D7 compares
# the new catalog against pre-change results like this table. Registration order is not pinned.
_EXPECTED_ROWS = [
    ("openai", "chat/completions", ("chat",)),
    ("codex", "query_cli", ("cli", "code")),
    ("openai", "audio/speech", ("tts",)),
    ("openai", "audio/transcriptions", ("stt", "whisper")),
    ("openai", "batches", ("batch",)),
    ("openai", "images/generations", ("dalle", "image")),
    ("openai", "images/edits", ("image_edit",)),
    ("openai", "embeddings", ("embed",)),
    ("openai", "responses", ("response",)),
    ("anthropic", "messages", ("chat",)),
    ("claude_code", "query_cli", ("cli", "code")),
    ("ollama", "chat/completions", ("chat",)),
    ("ollama", "embeddings", ("embed",)),
    ("ollama", "generate", ("completion", "generate")),
    ("lattice", "chat/completions", ("chat",)),
    ("lattice", "embeddings", ("embed",)),
    ("tavily", "search", ()),
    ("tavily", "extract", ()),
    ("exa", "search", ()),
    ("exa", "contents", ("get_contents",)),
    ("exa", "findSimilar", ("find_similar", "similar")),
    ("firecrawl", "v1/scrape", ("scrape",)),
    ("firecrawl", "v1/map", ("map",)),
    ("firecrawl", "v1/crawl", ("crawl",)),
    ("outcrawl", "v1/scrape", ("scrape",)),
    ("outcrawl", "v1/search", ("search",)),
    ("perplexity", "chat/completions", ("chat",)),
    ("nvidia_nim", "chat/completions", ("chat",)),
    ("nvidia_nim", "embeddings", ("embed",)),
    ("deepseek", "chat/completions", ("chat",)),
    ("gemini", "chat/completions", ("chat",)),
    ("gemini_code", "query_cli", ("cli",)),
    ("groq", "chat/completions", ("chat",)),
    ("groq", "audio/transcriptions", ("stt", "whisper")),
    ("pi", "query_cli", ("cli", "code")),
    ("openrouter", "chat/completions", ("chat", "chat/completions")),
    ("ag2", "group_chat", ("chat", "groupchat")),
    ("ag2", "agent", ("ask", "beta")),
    ("ag2", "nlip", ("nlip_remote", "remote")),
    ("scripted", "chat/completions", ("chat", "cli", "query_cli")),
]

# Every provider implementation module (and the scripted test endpoint module) a cold load leaves
# in sys.modules. Today the load imports every provider implementation. ADR-0140 D1 makes
# selecting one endpoint load only that factory's implementation closure and says merely listing
# declarations must not load endpoint implementations, so this set shrinks on purpose.
_EXPECTED_PROVIDER_MODULES = frozenset(
    {
        "lionagi.testing._endpoint",
        "lionhub.providers._agentic_handlers",
        "lionhub.providers._cli_subprocess",
        "lionhub.providers._multipart",
        "lionhub.providers.ag2",
        "lionhub.providers.ag2._config",
        "lionhub.providers.ag2.agent",
        "lionhub.providers.ag2.groupchat",
        "lionhub.providers.ag2.nlip",
        "lionhub.providers.anthropic",
        "lionhub.providers.anthropic._config",
        "lionhub.providers.anthropic.claude_code",
        "lionhub.providers.anthropic.messages",
        "lionhub.providers.deepseek",
        "lionhub.providers.deepseek._config",
        "lionhub.providers.deepseek.chat",
        "lionhub.providers.exa",
        "lionhub.providers.exa._config",
        "lionhub.providers.exa._schemas",
        "lionhub.providers.exa.contents",
        "lionhub.providers.exa.find_similar",
        "lionhub.providers.exa.search",
        "lionhub.providers.firecrawl",
        "lionhub.providers.firecrawl._config",
        "lionhub.providers.firecrawl._schemas",
        "lionhub.providers.firecrawl.crawl",
        "lionhub.providers.firecrawl.map",
        "lionhub.providers.firecrawl.scrape",
        "lionhub.providers.google",
        "lionhub.providers.google._config",
        "lionhub.providers.google.chat",
        "lionhub.providers.google.gemini_code",
        "lionhub.providers.groq",
        "lionhub.providers.groq._config",
        "lionhub.providers.groq.audio_transcription",
        "lionhub.providers.groq.chat",
        "lionhub.providers.lattice",
        "lionhub.providers.lattice._config",
        "lionhub.providers.lattice.chat",
        "lionhub.providers.lattice.embed",
        "lionhub.providers.nvidia_nim",
        "lionhub.providers.nvidia_nim._config",
        "lionhub.providers.nvidia_nim.chat",
        "lionhub.providers.nvidia_nim.embed",
        "lionhub.providers.ollama",
        "lionhub.providers.ollama._config",
        "lionhub.providers.ollama.chat",
        "lionhub.providers.ollama.embed",
        "lionhub.providers.ollama.generate",
        "lionhub.providers.openai",
        "lionhub.providers.openai._audio_schemas",
        "lionhub.providers.openai._codex_profile",
        "lionhub.providers.openai._codex_project_layers",
        "lionhub.providers.openai._config",
        "lionhub.providers.openai.audio",
        "lionhub.providers.openai.batch",
        "lionhub.providers.openai.chat",
        "lionhub.providers.openai.codex",
        "lionhub.providers.openai.embed",
        "lionhub.providers.openai.images",
        "lionhub.providers.openai.response",
        "lionhub.providers.openrouter",
        "lionhub.providers.openrouter._config",
        "lionhub.providers.openrouter.chat",
        "lionhub.providers.outcrawl",
        "lionhub.providers.outcrawl._config",
        "lionhub.providers.outcrawl._schemas",
        "lionhub.providers.outcrawl.scrape",
        "lionhub.providers.outcrawl.search",
        "lionhub.providers.perplexity",
        "lionhub.providers.perplexity._config",
        "lionhub.providers.perplexity.chat",
        "lionhub.providers.pi",
        "lionhub.providers.pi._config",
        "lionhub.providers.pi.cli",
        "lionhub.providers.tavily",
        "lionhub.providers.tavily._config",
        "lionhub.providers.tavily.search",
    }
)


def _run_child(code, *args, chat_provider=_INHERIT):
    """Run ``code`` in a fresh interpreter and return the JSON object it printed last.

    ``chat_provider`` leaves LIONAGI_CHAT_PROVIDER as this process has it by default; ``None``
    removes it from the child's environment and a string sets it.
    """
    env = dict(os.environ)
    inherited = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([*_CHILD_PATH, *([inherited] if inherited else [])])
    if chat_provider is not _INHERIT:
        env.pop("LIONAGI_CHAT_PROVIDER", None)
        if chat_provider is not None:
            env["LIONAGI_CHAT_PROVIDER"] = chat_provider
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


def _pairs(rows):
    """Project ``list_providers()`` rows to sorted (provider, endpoint, sorted aliases) tuples."""
    return sorted((r["provider"], r["endpoint"], tuple(sorted(r["aliases"]))) for r in rows)


@pytest.fixture(scope="module")
def cold_load():
    return _run_child(_COLD_CHILD)


def test_cold_load_registers_exact_provider_endpoint_pairs_and_aliases(cold_load):
    # The child starts cold and the load marks the registry loaded, so the table below is the
    # result of the real cold path.
    assert cold_load["loaded_before"] is False
    assert cold_load["loaded_after"] is True
    assert _pairs(cold_load["rows"]) == sorted(_EXPECTED_ROWS)


def test_cold_load_imports_exact_provider_modules(cold_load):
    # Importing the registry loads no provider module. ADR-0140 D1 requires that importing the
    # registry not load the hub, so the hub provider modules stay absent at this point.
    assert cold_load["modules_before"] == []
    assert sorted(cold_load["modules_after"]) == sorted(_EXPECTED_PROVIDER_MODULES)


def test_provider_import_failure_is_skipped_with_warning_and_others_register():
    result = _run_child(_FAILING_IMPORT_CHILD, _FAILED_MODULE)

    # Today an ImportError from one provider module is logged as one warning that names the
    # module, and the load goes on. ADR-0140 D4 replaces that skip, for an internal import
    # failure, with a ProviderActivationError raised when the failed endpoint is selected, and D7
    # requires a failed endpoint not to break unrelated providers.
    assert result["records"] == [
        [
            "WARNING",
            f"provider module '{_FAILED_MODULE}' failed to import and was not registered: "
            f"simulated missing module for {_FAILED_MODULE}",
        ]
    ]
    assert result["bad_module_imported"] is False

    # Every other pair still registers; only the failed module's pair is missing.
    surviving = [row for row in _EXPECTED_ROWS if row[:2] != ("groq", "audio/transcriptions")]
    assert len(surviving) == len(_EXPECTED_ROWS) - 1
    assert _pairs(result["rows"]) == sorted(surviving)


@pytest.mark.parametrize(
    "chat_provider",
    [None, "scripted", "openai"],
    ids=["unset", "scripted", "other-provider"],
)
def test_scripted_provider_registers_on_cold_load_whatever_chat_provider_is(chat_provider):
    result = _run_child(_SCRIPTED_CHILD, chat_provider=chat_provider)

    # The scripted provider registers with LIONAGI_CHAT_PROVIDER unset, set to "scripted" and set
    # to another provider: the registry does not read the variable. ADR-0140 D1 and D7 step 3
    # convert this registration into an explicitly selected core test fragment that keeps the
    # LIONAGI_CHAT_PROVIDER=scripted activation gate, so this ungated row changes on purpose.
    assert result["scripted_rows"] == [
        {
            "provider": "scripted",
            "endpoint": "chat/completions",
            "aliases": ["chat", "query_cli", "cli"],
            "type": "agentic",
            "class": "ScriptedEndpoint",
            "options": None,
        }
    ]
    assert result["scripted_module_imported"] is True
