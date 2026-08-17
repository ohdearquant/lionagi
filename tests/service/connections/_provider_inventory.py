# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Read-only AST collector for the checked provider-authority inventory."""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_SCHEMA = "lionagi.provider-authority-inventory"
_VERSION = 1
_REGISTRY_MODULE = "lionagi.service.connections.registry"
_PROVIDER_CONFIG_MODULE = "lionagi.service.connections.provider_config"
_MATCH_MODULE = "lionagi.service.connections.match_endpoint"
_TOKEN_BUDGET_MODULE = "lionagi.service.token_budget"

_COMPATIBILITY_FACADES = (
    (_REGISTRY_MODULE, "EndpointRegistry.match"),
    (_REGISTRY_MODULE, "EndpointRegistry.list_providers"),
    (_MATCH_MODULE, "match_endpoint"),
    (_TOKEN_BUDGET_MODULE, "lookup_context_window"),
)
_MUTATION_SURFACES = (
    (_REGISTRY_MODULE, "EndpointRegistry.register"),
    (_REGISTRY_MODULE, "register_endpoint"),
    (_PROVIDER_CONFIG_MODULE, "ProviderConfig.register"),
)
_DELETION_SYMBOLS = {
    _REGISTRY_MODULE: (
        "EndpointRegistry._entries",
        "EndpointRegistry._loaded",
        "EndpointRegistry._lock",
        "EndpointRegistry._plugin_registration",
        "EndpointRegistry._alias_owners",
        "EndpointRegistry.register",
        "EndpointRegistry._claim_provider_identity",
        "EndpointRegistry._remove_entries",
        "EndpointRegistry._rebuild_alias_owners",
        "EndpointRegistry._is_known_provider",
        "EndpointRegistry._provider_not_found_error",
        "EndpointRegistry._match_registered",
        "EndpointRegistry._revalidate_plugin_entry",
        "EndpointRegistry._plugin_entry_stat",
        "EndpointRegistry._plugin_entry_digest",
        "EndpointRegistry._consult_plugin_providers",
        "EndpointRegistry._reject_builtin_collisions",
        "EndpointRegistry._ensure_loaded",
        "register_endpoint",
        "_PROVIDER_OPTIONAL_DEPENDENCIES",
        "_import_provider_module",
        "_import_all_providers",
    ),
    _PROVIDER_CONFIG_MODULE: ("ProviderConfig.register",),
    _TOKEN_BUDGET_MODULE: (
        "_provider_cache",
        "_PROVIDER_MODULES",
        "_get_provider_windows",
        "_all_provider_windows",
    ),
}
_DOCUMENTATION_SURFACES = (
    (
        "docs/reference/providers.md",
        "# Provider Reference",
        (
            "Canonical names and declared aliases match exactly",
            "trusted and enabled plugin providers are consulted",
            "generic OpenAI-compatible fallback",
        ),
    ),
    (
        "docs/reference/providers.md",
        "## API providers",
        (
            "| OpenAI |",
            "| Anthropic |",
            "| Google Gemini |",
            "| Ollama (local) |",
            "| NVIDIA NIM |",
            "| Perplexity |",
            "| Groq |",
            "| OpenRouter |",
            "| DeepSeek |",
        ),
    ),
    (
        "docs/reference/providers.md",
        "## CLI / agentic providers",
        ("| Claude Code |", "| Codex |", "| Gemini CLI |", "| Pi |"),
    ),
    (
        "docs/reference/providers.md",
        "## Search and scraping providers",
        ("| Exa |", "| Firecrawl |", "| Tavily |"),
    ),
    (
        "docs/reference/providers.md",
        "## AG2 (multi-agent)",
        ("| AG2 GroupChat |", "| AG2 beta Agent |", "| AG2 NLIP remote |"),
    ),
    (
        "docs/reference/providers.md",
        "## Adding a new provider",
        (
            "@MyProviderConfigs.CHAT.register",
            "_import_all_providers()",
            "plugin provider",
        ),
    ),
    (
        "docs/reference/operations-service.md",
        "### Token Budget",
        ("provider longest-prefix lookup", "default (128k)"),
    ),
    (
        "docs/reference/operations-service.md",
        "### EndpointRegistry",
        ("`register_endpoint`", "`_ENDPOINT_META`"),
    ),
    (
        "docs/api/imodel.md",
        "### Fallback",
        ("ProviderNotFoundError", "openai_compatible=True", "registered provider"),
    ),
    (
        "docs/api/imodel.md",
        "## Endpoint matching",
        (
            "`EndpointRegistry`",
            "single-endpoint provider",
            "Built-in provider names win",
            "ProviderNotFoundError",
        ),
    ),
    (
        "docs/migration/0.23.0-to-0.23.1.md",
        "## New: Provider registry",
        ("self-register via decorator at import time", "@register_endpoint("),
    ),
    (
        "docs/adr/ADR-0027-model-service-facade-and-endpoint-resolution.md",
        "### D2 — `EndpointRegistry` is the sole endpoint resolver",
        (
            "Provider classes register by decorator",
            "def register(",
            "_import_all_providers",
        ),
    ),
    (
        "docs/adr/ADR-0052-supported-validation-and-testing-surfaces.md",
        "### D5 — The scripted endpoint uses normal discovery and no external request",
        ("standard decorator", "@register_endpoint(", "lionagi.testing._endpoint"),
    ),
    (
        "docs/adr/ADR-0088-plugin-system.md",
        "### D2 — Manifest schema: declarative, pure data",
        ("providers/searchapi.py", "self-registers"),
    ),
    (
        "docs/adr/ADR-0088-plugin-system.md",
        "### D3 — Lazy activation: manifests at first need, code at first use",
        (
            "self-registers at import time via the existing decorator",
            "declared provider module",
        ),
    ),
    (
        "docs/adr/ADR-0088-plugin-system.md",
        "### D6 — Collisions: built-ins win; peers hard-fail",
        ("**Providers**", "activation trigger", "Two **enabled plugins**"),
    ),
    (
        "docs/adr/ADR-0088-plugin-system.md",
        "## Implementation status (2026-07-13)",
        ("match-miss interception", "`EndpointRegistry._reject_builtin_collisions`"),
    ),
    (
        "docs/internals/runtime.md",
        "### `connections/registry.py`",
        (
            "`_consult_plugin_providers()`",
            "declared provider module",
            "`PluginRegistry.activate_target`",
        ),
    ),
    (
        "docs/internals/core.md",
        "### EndpointRegistry match",
        (
            "`register()`/`_claim_provider_identity`",
            "`ProviderNotFoundError`",
            "`openai_compatible=True`",
        ),
    ),
    (
        "docs/internals/core.md",
        "### EndpointRegistry plugin revalidation",
        ("`_revalidate_plugin_entry`", "`PluginRegistry.activate_target()`"),
    ),
)


def load_checked_inventory(path: Path) -> dict[str, Any]:
    """Load and minimally validate the independent checked artifact."""
    if not path.is_file():
        raise AssertionError(f"checked provider authority inventory is missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("checked provider authority inventory must be a JSON object")
    if value.get("schema") != _SCHEMA or value.get("version") != _VERSION:
        raise AssertionError(
            "checked provider authority inventory has an unsupported schema or version"
        )
    return value


class _Sources:
    def __init__(self, root: Path, overrides: Mapping[str, str] | None) -> None:
        self.root = root
        self.overrides = dict(overrides or {})

    def read(self, relative: str) -> str:
        if relative in self.overrides:
            return self.overrides[relative]
        return (self.root / relative).read_text(encoding="utf-8")

    def tree(self, relative: str) -> ast.Module:
        return ast.parse(self.read(relative), filename=relative)

    def module_tree(self, module: str) -> ast.Module:
        return self.tree(_module_relative_path(module))


def _module_relative_path(module: str) -> str:
    return f"{module.replace('.', '/')}.py"


def _assignment_name(node: ast.Assign | ast.AnnAssign) -> str | None:
    targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
    if len(targets) == 1 and isinstance(targets[0], ast.Name):
        return targets[0].id
    return None


def _assignment_value(node: ast.Assign | ast.AnnAssign) -> ast.expr | None:
    return node.value


def _top_level_value(tree: ast.Module, name: str) -> ast.expr:
    for node in tree.body:
        if isinstance(node, ast.Assign | ast.AnnAssign) and _assignment_name(node) == name:
            value = _assignment_value(node)
            if value is not None:
                return value
    raise AssertionError(f"missing top-level assignment {name!r}")


def _function(tree: ast.Module, qualname: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    parts = qualname.split(".")
    if len(parts) == 1:
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == parts[0]:
                return node
    elif len(parts) == 2:
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.name != parts[0]:
                continue
            for item in node.body:
                if (
                    isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                    and item.name == parts[1]
                ):
                    return item
    raise AssertionError(f"missing function {qualname!r}")


def _symbol_exists(tree: ast.Module, qualname: str) -> bool:
    parts = qualname.split(".")
    body: list[ast.stmt] = tree.body
    if len(parts) == 2:
        owner = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == parts[0]
            ),
            None,
        )
        if owner is None:
            return False
        body = owner.body
    target = parts[-1]
    return any(
        (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and node.name == target
        )
        or (isinstance(node, ast.Assign | ast.AnnAssign) and _assignment_name(node) == target)
        for node in body
    )


def _dotted_name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _dotted_name(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    return None


def _static_value(node: ast.expr) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List | ast.Tuple):
        return [_static_value(item) for item in node.elts]
    if isinstance(node, ast.Dict):
        return {
            _static_value(key): _static_value(value)
            for key, value in zip(node.keys, node.values, strict=True)
            if key is not None
        }
    if isinstance(node, ast.Attribute) and _dotted_name(node.value) == "EndpointType":
        return node.attr.lower()
    if isinstance(node, ast.Call) and _dotted_name(node.func) == "LazyType" and len(node.args) == 1:
        return _static_value(node.args[0])
    raise AssertionError(f"unsupported static provider expression: {ast.unparse(node)}")


def _bootstrap_modules(registry_tree: ast.Module) -> list[str]:
    loader = _function(registry_tree, "_import_all_providers")
    for node in loader.body:
        if isinstance(node, ast.Assign | ast.AnnAssign) and _assignment_name(node) == "_modules":
            value = _assignment_value(node)
            if value is None:
                break
            modules = _static_value(value)
            if isinstance(modules, list) and all(isinstance(item, str) for item in modules):
                return modules
    raise AssertionError("_import_all_providers must declare a literal _modules list")


def _relative_import_module(current_module: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = current_module.rsplit(".", 1)[0].split(".")
    trim = node.level - 1
    if trim:
        package = package[:-trim]
    if node.module:
        package.extend(node.module.split("."))
    return ".".join(package)


def _imported_symbols(tree: ast.Module, module: str) -> dict[str, str]:
    imported: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        source_module = _relative_import_module(module, node)
        for alias in node.names:
            imported[alias.asname or alias.name] = source_module
    return imported


def _class_attribute_values(tree: ast.Module, class_name: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
        if len(targets) != 1 or not isinstance(targets[0], ast.Attribute):
            continue
        target = targets[0]
        if not isinstance(target.value, ast.Name) or target.value.id != class_name:
            continue
        value = _assignment_value(node)
        if value is not None:
            values[target.attr] = _static_value(value)
    return values


def _config_member(tree: ast.Module, class_name: str, member_name: str) -> list[Any]:
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if not isinstance(item, ast.Assign | ast.AnnAssign):
                continue
            if _assignment_name(item) != member_name:
                continue
            value = _assignment_value(item)
            if value is None:
                break
            result = _static_value(value)
            if isinstance(result, list):
                return result
    raise AssertionError(f"missing provider config member {class_name}.{member_name}")


def _row_from_config(
    sources: _Sources,
    bootstrap_module: str,
    implementation_name: str,
    config_module: str,
    config_class: str,
    member_name: str,
) -> dict[str, Any]:
    config_tree = sources.module_tree(config_module)
    attrs = _class_attribute_values(config_tree, config_class)
    member = _config_member(config_tree, config_class, member_name)
    provider = str(attrs["_PROVIDER"]).strip().lower()
    provider_aliases = list(
        dict.fromkeys(str(alias).strip().lower() for alias in attrs["_PROVIDER_ALIASES"])
    )
    return {
        "bootstrap_module": bootstrap_module,
        "provider": provider,
        "provider_aliases": provider_aliases,
        "endpoint": member[0],
        "endpoint_aliases": member[1],
        "endpoint_type": member[2],
        "options_ref": member[3] if len(member) > 3 else None,
        "implementation_ref": f"{bootstrap_module}:{implementation_name}",
        "base_url": member[4] if len(member) > 4 else None,
        "auth_type": member[5] if len(member) > 5 else None,
        "content_type": member[6] if len(member) > 6 else "application/json",
        "api_key_env": attrs.get("_API_KEY_ENV"),
    }


def _row_from_register_call(
    bootstrap_module: str,
    implementation_name: str,
    decorator: ast.Call,
) -> dict[str, Any]:
    values = {keyword.arg: _static_value(keyword.value) for keyword in decorator.keywords}
    provider = str(values["provider"]).strip().lower()
    provider_aliases = list(
        dict.fromkeys(str(alias).strip().lower() for alias in values.get("provider_aliases", []))
    )
    return {
        "bootstrap_module": bootstrap_module,
        "provider": provider,
        "provider_aliases": provider_aliases,
        "endpoint": values["endpoint"],
        "endpoint_aliases": values.get("aliases", []),
        "endpoint_type": values.get("endpoint_type", "api"),
        "options_ref": values.get("options"),
        "implementation_ref": f"{bootstrap_module}:{implementation_name}",
        "base_url": values.get("base_url"),
        "auth_type": values.get("auth_type"),
        "content_type": values.get("content_type"),
        "api_key_env": values.get("api_key_env"),
    }


def _endpoint_rows(sources: _Sources, modules: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for module in modules:
        tree = sources.module_tree(module)
        imported = _imported_symbols(tree, module)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Attribute)
                    and decorator.attr == "register"
                    and isinstance(decorator.value, ast.Attribute)
                    and isinstance(decorator.value.value, ast.Name)
                ):
                    config_class = decorator.value.value.id
                    config_module = imported.get(config_class)
                    if config_module is None:
                        raise AssertionError(f"cannot resolve config class {config_class!r}")
                    rows.append(
                        _row_from_config(
                            sources,
                            module,
                            node.name,
                            config_module,
                            config_class,
                            decorator.value.attr,
                        )
                    )
                elif (
                    isinstance(decorator, ast.Call)
                    and _dotted_name(decorator.func) == "register_endpoint"
                ):
                    rows.append(_row_from_register_call(module, node.name, decorator))
    return rows


def _ordered_string_mapping(node: ast.expr, *, name: str) -> list[tuple[str, Any]]:
    if not isinstance(node, ast.Dict):
        raise AssertionError(f"{name} must be a literal dict")
    result: list[tuple[str, Any]] = []
    for key, value in zip(node.keys, node.values, strict=True):
        if key is None:
            raise AssertionError(f"{name} cannot use dict unpacking")
        parsed_key = _static_value(key)
        if not isinstance(parsed_key, str):
            raise AssertionError(f"{name} keys must be strings")
        result.append((parsed_key, _static_value(value)))
    return result


def _context_windows(sources: _Sources) -> list[dict[str, Any]]:
    token_tree = sources.module_tree(_TOKEN_BUDGET_MODULE)
    provider_modules = _ordered_string_mapping(
        _top_level_value(token_tree, "_PROVIDER_MODULES"), name="_PROVIDER_MODULES"
    )
    result: list[dict[str, Any]] = []
    for provider, source_module in provider_modules:
        if not isinstance(source_module, str):
            raise AssertionError("_PROVIDER_MODULES values must be module names")
        source_tree = sources.module_tree(source_module)
        entries = _ordered_string_mapping(
            _top_level_value(source_tree, "CONTEXT_WINDOWS"), name="CONTEXT_WINDOWS"
        )
        result.append(
            {
                "provider": provider,
                "source_module": source_module,
                "entries": [
                    {"model": model, "context_window": context_window}
                    for model, context_window in entries
                ],
            }
        )
    return result


def _parameter(
    argument: ast.arg,
    *,
    kind: str,
    default: ast.expr | None,
    required: bool,
) -> dict[str, Any]:
    return {
        "name": argument.arg,
        "kind": kind,
        "annotation": ast.unparse(argument.annotation) if argument.annotation else None,
        "required": required,
        "default": ast.unparse(default) if default is not None else None,
    }


def _signature(sources: _Sources, module: str, qualname: str) -> dict[str, Any]:
    node = _function(sources.module_tree(module), qualname)
    positional = [*node.args.posonlyargs, *node.args.args]
    if "." in qualname and positional and positional[0].arg in {"self", "cls"}:
        positional = positional[1:]
    default_offset = len(positional) - len(node.args.defaults)
    parameters: list[dict[str, Any]] = []
    positional_only_count = max(0, len(node.args.posonlyargs) - 1)
    for index, argument in enumerate(positional):
        default_index = index - default_offset
        default = node.args.defaults[default_index] if default_index >= 0 else None
        parameters.append(
            _parameter(
                argument,
                kind="positional_only"
                if index < positional_only_count
                else "positional_or_keyword",
                default=default,
                required=default_index < 0,
            )
        )
    for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
        parameters.append(
            _parameter(
                argument,
                kind="keyword_only",
                default=default,
                required=default is None,
            )
        )
    return {
        "module": module,
        "qualname": qualname,
        "parameters": parameters,
        "return_annotation": ast.unparse(node.returns) if node.returns else None,
        "var_keyword": node.args.kwarg.arg if node.args.kwarg else None,
    }


def _public_authority(sources: _Sources) -> dict[str, Any]:
    registry_exports = _static_value(
        _top_level_value(sources.module_tree(_REGISTRY_MODULE), "__all__")
    )
    return {
        "registry_exports": registry_exports,
        "compatibility_facades": [
            _signature(sources, module, qualname) for module, qualname in _COMPATIBILITY_FACADES
        ],
        "mutation_surfaces": [
            _signature(sources, module, qualname) for module, qualname in _MUTATION_SURFACES
        ],
    }


def _deletion_ledger(sources: _Sources, decorator_count: int) -> dict[str, Any]:
    authorities = []
    for module, symbols in _DELETION_SYMBOLS.items():
        tree = sources.module_tree(module)
        authorities.append(
            {
                "module": module,
                "symbols": [symbol for symbol in symbols if _symbol_exists(tree, symbol)],
            }
        )
    return {
        "authorities": authorities,
        "provider_registration_decorators": decorator_count,
    }


def _assignment_expression(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    name: str,
) -> str | None:
    for node in ast.walk(function):
        if isinstance(node, ast.Assign | ast.AnnAssign) and _assignment_name(node) == name:
            value = _assignment_value(node)
            return ast.unparse(value) if value is not None else None
    return None


def _first_call_node(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    dotted_name: str,
) -> ast.Call | None:
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and _dotted_name(node.func) == dotted_name:
            return node
    return None


def _plugin_fail_soft(sources: _Sources) -> dict[str, Any]:
    tree = sources.module_tree(_REGISTRY_MODULE)
    reject = _function(tree, "EndpointRegistry._reject_builtin_collisions")
    consult = _function(tree, "EndpointRegistry._consult_plugin_providers")
    rejection_guard = next(
        (
            ast.unparse(node.test)
            for node in ast.walk(reject)
            if isinstance(node, ast.If)
            and "is_this_activation" in ast.unparse(node.test)
            and "collides" in ast.unparse(node.test)
        ),
        None,
    )
    alias_handler = next(
        (
            handler
            for handler in ast.walk(consult)
            if isinstance(handler, ast.ExceptHandler)
            and _dotted_name(handler.type) == "ProviderAliasCollisionError"
        ),
        None,
    )
    append_call = _first_call_node(reject, "rejected.append")
    removal_call = _first_call_node(reject, "cls._remove_entries")
    return {
        "builtin_collision": {
            "activation_scope": _assignment_expression(reject, "is_this_activation"),
            "collision_predicate": _assignment_expression(reject, "collides"),
            "rejection_guard": rejection_guard,
            "append_call": ast.unparse(append_call) if append_call else None,
            "removal_call": ast.unparse(removal_call) if removal_call else None,
            "removal_after_append": bool(
                append_call and removal_call and removal_call.lineno > append_call.lineno
            ),
        },
        "provider_alias_collision": {
            "exception": _dotted_name(alias_handler.type) if alias_handler else None,
            "handler_logs_warning": bool(
                alias_handler
                and any(
                    isinstance(node, ast.Call) and _dotted_name(node.func) == "logger.warning"
                    for node in ast.walk(alias_handler)
                )
            ),
            "handler_continues": bool(
                alias_handler
                and any(isinstance(node, ast.Continue) for node in ast.walk(alias_handler))
            ),
        },
    }


def _markdown_fence(line: str) -> tuple[str, int, str] | None:
    candidate = line.lstrip(" ")
    if len(line) - len(candidate) > 3 or not candidate:
        return None
    marker = candidate[0]
    if marker not in {"`", "~"}:
        return None
    width = len(candidate) - len(candidate.lstrip(marker))
    if width < 3:
        return None
    return marker, width, candidate[width:].strip()


def _markdown_headings(source: str) -> list[tuple[int, int, str]]:
    headings: list[tuple[int, int, str]] = []
    fence_marker: str | None = None
    fence_width = 0
    for index, line in enumerate(source.splitlines(keepends=True)):
        fence = _markdown_fence(line)
        if fence_marker is not None:
            if (
                fence is not None
                and fence[0] == fence_marker
                and fence[1] >= fence_width
                and not fence[2]
            ):
                fence_marker = None
                fence_width = 0
            continue
        if fence is not None:
            fence_marker, fence_width, _ = fence
            continue

        candidate = line.rstrip("\r\n").lstrip(" ")
        indent = len(line.rstrip("\r\n")) - len(candidate)
        if indent > 3 or not candidate.startswith("#"):
            continue
        level = len(candidate) - len(candidate.lstrip("#"))
        if level > 6 or candidate[level : level + 1] not in {"", " "}:
            continue
        headings.append((index, level, candidate.rstrip()))
    return headings


def _markdown_section(source: str, *, path: str, anchor: str) -> str:
    lines = source.splitlines(keepends=True)
    headings = _markdown_headings(source)
    matches = [(index, level) for index, level, heading in headings if heading == anchor]
    if len(matches) != 1:
        raise AssertionError(
            f"documentation anchor must occur exactly once: "
            f"path={path!r}, anchor={anchor!r}, occurrences={len(matches)}"
        )
    start, level = matches[0]
    end = next(
        (index for index, next_level, _ in headings if index > start and next_level <= level),
        len(lines),
    )
    return "".join(lines[start:end])


def _documentation_surfaces(sources: _Sources) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path, anchor, tokens in _DOCUMENTATION_SURFACES:
        section = _markdown_section(sources.read(path), path=path, anchor=anchor)
        result.append(
            {
                "path": path,
                "anchor": anchor,
                "token_counts": {token: section.count(token) for token in tokens},
            }
        )
    return result


def collect_provider_authority_inventory(
    root: Path,
    *,
    source_overrides: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Collect provider authority from source text without importing provider modules."""
    sources = _Sources(root, source_overrides)
    registry_tree = sources.module_tree(_REGISTRY_MODULE)
    modules = _bootstrap_modules(registry_tree)
    rows = _endpoint_rows(sources, modules)
    return {
        "schema": _SCHEMA,
        "version": _VERSION,
        "bootstrap_modules": modules,
        "optional_dependencies": _static_value(
            _top_level_value(registry_tree, "_PROVIDER_OPTIONAL_DEPENDENCIES")
        ),
        "endpoint_rows": rows,
        "context_windows": _context_windows(sources),
        "documentation_surfaces": _documentation_surfaces(sources),
        "public_authority": _public_authority(sources),
        "deletion_ledger": _deletion_ledger(sources, len(rows)),
        "plugin_fail_soft": _plugin_fail_soft(sources),
    }
