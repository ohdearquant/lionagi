"""Build the checked feature-boundary inventory from the ownership rules below.

The generated file is reviewed data, not a runtime registry. New top-level
packages and new requirements need an explicit ownership rule.
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Longer prefixes override broader ones. Owners name a feature, not a team.
MODULE_RULES = {
    "lionagi.ln": ("foundation", "primitives"),
    "lionagi.libs": ("foundation", "utilities"),
    "lionagi.libs.credential_fields": ("foundation", "credential_fields"),
    "lionagi.libs.path_safety": ("foundation", "path_safety"),
    "lionagi.libs.schema": ("foundation", "schema"),
    "lionagi.libs.validate": ("foundation", "validation"),
    "lionagi.models": ("foundation", "models"),
    "lionagi.protocols": ("sdk", "protocols"),
    "lionagi.protocols._concepts": ("contracts", "protocol_contracts"),
    "lionagi.protocols.context_providers": ("contracts", "context_contracts"),
    "lionagi.protocols.flow_requests": ("contracts", "flow_contracts"),
    "lionagi.protocols.ids": ("foundation", "identity"),
    "lionagi.protocols.memory": ("contracts", "memory_contracts"),
    "lionagi.protocols.types": ("contracts", "protocol_contracts"),
    "lionagi.protocols.generic": ("foundation", "collections"),
    "lionagi.protocols.generic.processor": ("sdk", "event_processing"),
    "lionagi.protocols.action": ("sdk", "tools"),
    "lionagi.protocols.graph": ("sdk", "graph"),
    "lionagi.protocols.messages": ("sdk", "messages"),
    "lionagi.session": ("sdk", "session"),
    "lionagi.operations": ("sdk", "operations"),
    "lionagi.operations.lndl_middle": ("adapter", "lndl"),
    "lionagi.service": ("sdk", "model_invocation"),
    "lionagi.service.accounts": ("adapter", "accounts"),
    "lionagi.service.connections": ("adapter", "provider_connections"),
    "lionagi.service.connections.endpoint_config": ("contracts", "provider_contracts"),
    "lionagi.service.connections.registry": ("sdk", "provider_registry"),
    "lionagi.service.types": ("contracts", "provider_contracts"),
    "lionagi.adapters": ("adapter", "data_adapters"),
    "lionagi.agent": ("adapter", "agent"),
    "lionagi.casts": ("adapter", "casts"),
    "lionagi.hooks": ("adapter", "hooks"),
    "lionagi.integrations": ("adapter", "integrations"),
    "lionagi.integrations.ruvector": ("adapter", "ruvector"),
    "lionagi.lndl": ("adapter", "lndl"),
    "lionagi.plugins": ("adapter", "plugins"),
    "lionagi.providers": ("adapter", "provider_support"),
    "lionagi.state": ("adapter", "state"),
    "lionagi.tools": ("adapter", "tools"),
    "lionagi.tools.base": ("sdk", "tools"),
    "lionagi.testing": ("adapter", "testing"),
    "lionagi.cli": ("root", "cli"),
    "lionagi.studio": ("root", "studio"),
    "lionagi.studio.scheduler": ("adapter", "scheduler"),
    "lionagi.studio._timezone_rules": ("adapter", "scheduler"),
    "lionagi.studio.services.scheduler_state": ("adapter", "scheduler"),
    "lionagi.studio.services._schedule_validation": ("adapter", "scheduler"),
    "lionagi.studio.services.schedule_declaration": ("adapter", "scheduler"),
    "lionagi.studio.services.schedule_export": ("adapter", "scheduler"),
}

# Concrete providers are separate features; shared provider support remains
# its own owner so sibling dependencies remain visible in the report.
for name in (
    "ag2",
    "anthropic",
    "deepseek",
    "exa",
    "firecrawl",
    "google",
    "groq",
    "lattice",
    "nvidia_nim",
    "ollama",
    "openai",
    "openrouter",
    "perplexity",
    "pi",
    "tavily",
):
    MODULE_RULES[f"lionagi.providers.{name}"] = ("adapter", f"provider_{name}")

ROOT_MODULES = {
    "lionagi": ("sdk", "public_api"),
    "lionagi._auto": ("root", "application_discovery"),
    "lionagi._checkpoint_v3_codec": ("contracts", "checkpoint_contracts"),
    "lionagi._checkpoint_version": ("contracts", "checkpoint_contracts"),
    "lionagi._class_registry": ("foundation", "class_registry"),
    "lionagi._cli_worker_context": ("adapter", "worker_context"),
    "lionagi._command_env": ("adapter", "command_environment"),
    "lionagi._command_env_metadata": ("contracts", "command_environment"),
    "lionagi._errors": ("contracts", "errors"),
    "lionagi._flow_spec": ("contracts", "flow_contracts"),
    "lionagi._log_channels": ("foundation", "logging_channels"),
    "lionagi._moved": ("foundation", "compatibility"),
    "lionagi._paths": ("foundation", "path_configuration"),
    "lionagi._provider_errors": ("contracts", "errors"),
    "lionagi._settings": ("sdk", "configuration"),
    "lionagi._spec_limits": ("contracts", "specification_limits"),
    "lionagi._types": ("foundation", "types"),
    "lionagi.config": ("sdk", "configuration"),
    "lionagi.utils": ("foundation", "serialization"),
    "lionagi.version": ("foundation", "version"),
}

# Migration notes identify a destination, not a current owner. Mixed modules
# keep their current owner; the destination names the extracted portion.
TARGET_RULES = {
    "lionagi.agent": "lionhub (destination not yet recorded)",
    "lionagi.casts": "lionhub (destination not yet recorded)",
    "lionagi.hooks.external": "lionhub (destination not yet recorded)",
    "lionagi.integrations": "lionhub (destination not yet recorded)",
    "lionagi.lndl": "lionhub (destination not yet recorded)",
    "lionagi.operations.lndl_middle": "lionhub (destination not yet recorded)",
    "lionagi.tools": "lionhub (destination not yet recorded)",
    "lionagi.providers": "lionhub (destination not yet recorded)",
    "lionagi.cli": "pending the CLI location decision (ADR-0139 D1)",
    "lionagi.cli._argtypes": "lionhub.orchestration.arguments",
    "lionagi.cli._code_identity": "lionhub.serve.diagnostics",
    "lionagi.cli._engine_definitions": "lionhub.orchestration.engine_definitions",
    "lionagi.cli._inspection": "lionhub.orchestration.inspection",
    "lionagi.cli._kill_service": "lionhub.orchestration.kill",
    "lionagi.cli._mcp_resolve": "lionhub.orchestration.mcp_config",
    "lionagi.cli._mirror_service": "lionhub.serve.mirror",
    "lionagi.cli._mirror_settings": "lionhub.serve.mirror",
    "lionagi.cli._orchestrate_parsing": "lionhub.orchestration.flow.parsing",
    "lionagi.cli._project": "lionhub.orchestration.project",
    "lionagi.cli._providers": "lionhub.orchestration.spawn",
    "lionagi.cli._runs": "lionhub.orchestration.runs",
    "lionagi.cli._schedule_parsing": "lionhub.orchestration.schedule",
    "lionagi.cli._team_service": "lionhub.orchestration.team",
    "lionagi.cli._util": "lionhub.orchestration.job",
    "lionagi.cli.orchestrate._checkpoint": "lionhub.orchestration.flow.checkpoint",
    "lionagi.cli.orchestrate._orchestration": "lionhub.orchestration.runs",
    "lionagi.cli.status": "lionhub.orchestration.inspection",
    "lionagi.cli.kill": "lionhub.orchestration.kill",
    "lionagi.cli.team": "lionhub.orchestration.team",
    "lionagi.cli.engine": "lionhub.orchestration.engine_definitions",
    "lionagi.cli.mirror": "lionhub.serve.mirror",
    "lionagi.studio.scheduler": "lionhub.orchestration.scheduler",
    "lionagi.studio._timezone_rules": "lionhub.orchestration.scheduler",
    "lionagi.studio.services.scheduler_state": "lionhub.orchestration.scheduler",
    "lionagi.studio.services._schedule_validation": "lionhub.orchestration.schedule",
    "lionagi.studio.services.schedule_declaration": "lionhub.orchestration.schedule",
    "lionagi.studio.services.schedule_export": "lionhub.orchestration.schedule",
    "lionagi.studio.services.schedules": "lionhub.orchestration.schedule",
    "lionagi.studio.cli": "pending the CLI location decision (ADR-0139 D1)",
    "lionagi.studio.services.runs": "lionhub.serve",
    "lionagi.studio.services.run_resume": "lionhub.serve",
    "lionagi.studio.services.engine_runs": "lionhub.serve",
    "lionagi.studio.services.invocations": "lionhub.serve",
    "lionagi.studio.services.launches": "lionhub.serve",
    "lionagi.studio.services.identity": "lionhub.serve",
    "lionagi.studio.services.pairing": "lionhub.serve",
    "lionagi.studio.services.sessions": "lionhub.serve",
    "lionagi.studio.services.admin": "lionhub.serve",
    "lionagi.tools.khive_injection": "lionhub (destination not yet recorded)",
    "lionagi.tools.context.context": "lionhub (destination not yet recorded)",
    "lionagi.tools.code.bash": "lionhub (destination not yet recorded)",
    "lionagi.tools.sandbox_backend": "lionhub (destination not yet recorded)",
    "lionagi.tools.daytona": "lionhub (destination not yet recorded)",
    "lionagi.providers.anthropic.messages": "lionhub (destination not yet recorded)",
    "lionagi.providers.anthropic.claude_code": "lionhub (destination not yet recorded)",
    "lionagi.providers.openai.chat": "lionhub (destination not yet recorded)",
    "lionagi.providers.openai.response": "lionhub (destination not yet recorded)",
    "lionagi.providers.openai.codex": "lionhub (destination not yet recorded)",
    "lionagi.providers.openai._codex_profile": "lionhub (destination not yet recorded)",
    "lionagi.providers.openai._codex_project_layers": "lionhub (destination not yet recorded)",
    "lionagi.providers.openai.embed": "lionhub (destination not yet recorded)",
    "lionagi.providers.google.chat": "lionhub (destination not yet recorded)",
    "lionagi.providers.google.gemini_code": "lionhub (destination not yet recorded)",
}

# Mixed modules retain their existing owner while multiple portions move.
ADDITIONAL_TARGETS = {
    "lionagi.cli.orchestrate": ["lionhub.orchestration.flow.parsing"],
    "lionagi.studio.services.schedules": ["lionhub.serve"],
}
for name in ("openrouter", "exa", "perplexity", "groq", "lattice", "ollama", "nvidia_nim"):
    TARGET_RULES[f"lionagi.providers.{name}"] = "lionhub (destination not yet recorded)"

REQUIREMENT_GROUPS = {
    ("foundation", "concurrency"): "anyio sniffio",
    ("foundation", "serialization"): "json-repair orjson pyyaml toml tomli",
    ("foundation", "models"): "pydantic",
    ("foundation", "version"): "packaging",
    ("sdk", "configuration"): "pydantic-settings python-dotenv",
    ("sdk", "model_invocation"): "aiocache httpx tiktoken",
    ("adapter", "provider_openai"): "markdown-it-py",
    ("adapter", "state"): "aiosqlite sqlalchemy pydapter asyncpg",
    ("adapter", "process_inspection"): "psutil",
    ("adapter", "ruvector"): "ruvector numpy",
    ("adapter", "provider_ollama"): "ollama tqdm",
    ("adapter", "provider_ag2"): "ag2",
    ("adapter", "data_adapters"): "pandas xmltodict",
    ("sdk", "graph"): "networkx",
    ("root", "mcp"): "fastmcp mcp",
    ("root", "studio"): "fastapi uvicorn starlette croniter",
    ("root", "cli"): "rich",
    ("root", "build"): "hatchling",
    (
        "root",
        "testing",
    ): "pytest pytest-asyncio pytest-cov pytest-xdist pytest-timeout trio hypothesis pyarrow msgspec testcontainers",
    ("root", "development"): "ipython ruff",
    (
        "root",
        "documentation",
    ): "mkdocs mkdocs-material mkdocs-minify-plugin mkdocstrings mkdocstrings-python",
    (
        "root",
        "dependency_constraints",
    ): "authlib python-multipart pyjwt urllib3 cryptography requests pygments pymdown-extensions opentelemetry-api",
}


def match(mapping, name):
    candidates = (key for key in mapping if name == key or name.startswith(key + "."))
    key = max(candidates, key=len, default=None)
    return mapping[key] if key else None


def module_name(path):
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "scripts/feature_boundaries.json")
    args = parser.parse_args(argv)
    modules = {}
    for path in sorted((ROOT / "lionagi").rglob("*.py")):
        name = module_name(path)
        assignment = ROOT_MODULES.get(name) or match(MODULE_RULES, name)
        if not assignment or name in modules:
            raise ValueError(f"missing or duplicate module ownership: {name}")
        layer, owner = assignment
        entry = {"layer": layer, "owner": owner}
        target = match(TARGET_RULES, name)
        if target:
            entry["target_owner"] = target
        if name in ADDITIONAL_TARGETS:
            entry["additional_targets"] = ADDITIONAL_TARGETS[name]
        modules[name] = entry
    requirements = {}
    for (layer, owner), names in REQUIREMENT_GROUPS.items():
        for name in names.split():
            if name in requirements:
                raise ValueError(f"duplicate requirement ownership: {name}")
            requirements[name] = {"layer": layer, "owner": owner}
    for name, users in {"tqdm": ["development"], "starlette": ["mcp"]}.items():
        if name not in requirements:
            raise ValueError(f"missing requirement ownership for secondary users: {name}")
        requirements[name]["secondary_users"] = users
    destination = args.output
    exceptions = (
        json.loads(destination.read_text(encoding="utf-8"))["exceptions"]
        if destination.exists()
        else []
    )
    inventory = {
        "version": 1,
        "modules": modules,
        "requirements": dict(sorted(requirements.items())),
        "external_targets": {"lionhub": {"layer": "root", "owner": "lionhub (downstream package)"}},
        "exceptions": exceptions,
    }
    destination.write_text(
        json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
