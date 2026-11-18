# SPDX-License-Identifier: Apache-2.0

"""Collect the checked provider inventory from static declarations.

With no flags, check the committed inventory. Use --write to regenerate it
after reviewing an intentional provider contract change.
"""

from __future__ import annotations

import argparse
import ast
import json
from collections.abc import Callable
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUB = "lionhub/src/lionhub/providers"
CORE = "lionagi/service/connections"
INVENTORY = Path("tests/service/connections/fixtures/provider_inventory.json")
PUBLIC_CALLS = {
    f"{HUB}/manifest.py": ("get_provider_manifest",),
    f"{CORE}/registry.py": (
        "register_endpoint",
        "EndpointRegistry.register",
        "EndpointRegistry.match",
        "EndpointRegistry.canonical_provider",
        "EndpointRegistry.list_providers",
        "EndpointRegistry.failed_providers",
    ),
    f"{CORE}/provider_catalog.py": ("compose_provider_catalog",),
    f"{CORE}/provider_config.py": ("ProviderConfig.register", "ProviderConfig.available"),
    f"{CORE}/match_endpoint.py": ("match_endpoint",),
    "lionagi/service/imodel.py": ("iModel.__init__",),
    "lionagi/service/token_budget.py": (
        "lookup_context_window",
        "get_context_window",
        "get_token_budget",
    ),
}


def assignments(tree: ast.Module | ast.ClassDef) -> dict[str, ast.expr]:
    result = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                result[ast.unparse(target)] = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            result[ast.unparse(node.target)] = node.value
    return result


def definition(tree: ast.Module | ast.ClassDef, name: str) -> ast.ClassDef | ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == name:
            return node
    raise ValueError(f"Definition {name!r} is missing")


def elements(node: ast.expr) -> list[ast.expr]:
    if not isinstance(node, (ast.Tuple, ast.List)):
        raise ValueError(f"Expected a literal sequence, found {ast.unparse(node)}")
    return node.elts


def call(node: ast.expr, name: str) -> ast.Call:
    if not isinstance(node, ast.Call) or ast.unparse(node.func) != name:
        raise ValueError(f"Expected {name}, found {ast.unparse(node)}")
    return node


def literal(node: ast.expr, bindings: dict[str, ast.expr]) -> object:
    if isinstance(node, ast.Name):
        node = bindings[node.id]
    return ast.literal_eval(node)


def default_settings(config: ast.ClassDef) -> dict[str, object]:
    defaults = {}
    for name, index in (("base_url", 4), ("auth_type", 5), ("content_type", 6)):
        accessor = definition(config, name)
        [returned] = [node for node in accessor.body if isinstance(node, ast.Return)]
        args = call(returned.value, "_get").args
        if len(args) not in (2, 3) or ast.unparse(args[0]) != "self.value":
            raise ValueError(f"ProviderConfig.{name} no longer reads its tuple member")
        if ast.literal_eval(args[1]) != index:
            raise ValueError(f"ProviderConfig.{name} reads a different tuple member")
        defaults[name] = ast.literal_eval(args[2]) if len(args) == 3 else None
    return defaults


def endpoint_row(node, members, config, kinds, defaults, manifest):
    member, route, aliases, kind, target, *requirements = call(node, "_Endpoint").args
    name, key, aliases, kind, target = map(ast.literal_eval, (member, route, aliases, kind, target))
    effective_key, effective_aliases, kind_node, options, *fields = elements(members[name])
    if (ast.literal_eval(effective_key), tuple(ast.literal_eval(effective_aliases))) != (
        key,
        aliases,
    ):
        raise ValueError(f"Manifest and configuration disagree on {target} names")
    if kinds[ast.unparse(kind_node)] != kind:
        raise ValueError(f"Manifest and configuration disagree on {target} kind")
    settings = dict(defaults)
    for field, value in zip(defaults, fields, strict=False):
        settings[field] = literal(value, config)
    if len(fields) > len(defaults):
        raise ValueError(f"Unexpected configuration tuple fields for {target}")
    if isinstance(options, ast.Call):
        [reference] = call(options, "LazyType").args
        request_options = ast.literal_eval(reference)
    elif isinstance(options, ast.Constant) and options.value is None:
        request_options = None
    else:
        raise ValueError(f"Unexpected request-options declaration for {target}")
    dependencies = [
        {
            item.arg: ast.literal_eval(item.value)
            for item in call(dep, "DependencyRequirement").keywords
        }
        for binding in requirements
        for dep in elements(manifest[binding.id])
    ]
    return {
        "member": name,
        "endpoint": key,
        "aliases": aliases,
        "kind": kind,
        "implementation": target,
        "request_options": request_options,
        "settings": settings,
        "requirements": dependencies,
    }


def signature(node: ast.FunctionDef) -> str:
    annotation = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"({ast.unparse(node.args)}){annotation}"


def public_exports(tree: Callable[[str], ast.Module]) -> dict[str, str]:
    exports = {}
    for path, package in (
        ("lionagi/__init__.py", "lionagi"),
        ("lionagi/service/__init__.py", "lionagi.service"),
    ):
        bindings = assignments(tree(path))
        if "iModel" not in ast.literal_eval(bindings["__all__"]):
            raise ValueError(f"iModel is missing from {package} exports")
        owner, name = ast.literal_eval(bindings["_LAZY_MAP"])["iModel"]
        exports[f"{package}.iModel"] = f"{package}.{owner}:{name}"
    package = "lionagi.service.connections"
    connections = tree(f"{CORE}/__init__.py")
    declared = set(ast.literal_eval(assignments(connections)["__all__"]))
    expected = {
        "EndpointRegistry",
        "register_endpoint",
        "EndpointType",
        "ProviderConfig",
        "match_endpoint",
        "ProviderNotFoundError",
        "ProviderAliasCollisionError",
    }
    for node in connections.body:
        if isinstance(node, ast.ImportFrom):
            for item in node.names:
                name = item.asname or item.name
                if name in expected and name in declared:
                    owner = resolve_name("." * node.level + (node.module or ""), package)
                    exports[f"{package}.{name}"] = f"{owner}:{item.name}"
    if not {f"{package}.{name}" for name in expected} <= exports.keys():
        raise ValueError(f"Provider exports are missing from {package}")
    return exports


def context_examples(providers: list[dict], order: tuple[str, ...]) -> list[dict]:
    tables = {
        fragment["provider"]: dict(fragment["context_windows"] or ()) for fragment in providers
    }
    examples = []
    for model in ("gemini-2.5-pro", "o3/gemini-2.5-pro"):
        for hint in (None, "gemini", "gemini-api"):
            scan = (hint, *order) if hint in tables else order
            for provider in dict.fromkeys(scan):
                table = tables[provider]
                match = max((name for name in table if name in model), key=len, default=None)
                if match is not None:
                    examples.append({"model": model, "hint": hint, "window": table[match]})
                    break
            else:
                raise ValueError(f"No window declared for {model!r}")
    return examples


def collect(read_source: Callable[[str], str]) -> dict:
    trees: dict[str, ast.Module] = {}

    def tree(path: str) -> ast.Module:
        if path not in trees:
            trees[path] = ast.parse(read_source(path), filename=path)
        return trees[path]

    manifest_path = f"{HUB}/manifest.py"
    manifest = assignments(tree(manifest_path))
    namespace = ast.literal_eval(manifest["_PROVIDERS"])
    registry_path = f"{CORE}/registry.py"
    registry = tree(registry_path)
    kinds = {
        f"EndpointType.{key}": ast.literal_eval(value)
        for key, value in assignments(definition(registry, "EndpointType")).items()
    }
    config_class = definition(tree(f"{CORE}/provider_config.py"), "ProviderConfig")
    defaults = default_settings(config_class)
    providers = []
    for declaration in elements(manifest["_DECLARATIONS"]):
        package, enum, provider, aliases, window, entries = call(declaration, "_Provider").args
        package, enum, provider, aliases, window = map(
            ast.literal_eval, (package, enum, provider, aliases, window)
        )
        if not package.isidentifier():
            raise ValueError(f"Invalid provider configuration package {package!r}")
        path = f"{HUB}/{package}/_config.py"
        config = assignments(tree(path))
        members = assignments(definition(tree(path), enum))
        names = ast.literal_eval(config[f"{enum}._PROVIDER"])
        effective_aliases = tuple(ast.literal_eval(config[f"{enum}._PROVIDER_ALIASES"]))
        if (provider, aliases) != (names, effective_aliases):
            raise ValueError(f"Manifest and configuration disagree on {provider} names")
        api_key_env = config.get(f"{enum}._API_KEY_ENV")
        endpoints = [
            endpoint_row(node, members, config, kinds, defaults, manifest)
            for node in elements(entries)
        ]
        for row in endpoints:
            row["implementation"] = f"{namespace}.{row['implementation']}"
            row["settings"]["api_key_env"] = ast.literal_eval(api_key_env) if api_key_env else None
        providers.append(
            {
                "provider": provider,
                "aliases": aliases,
                "configuration": f"{path}::{enum}",
                "default_endpoint": endpoints[0]["endpoint"],
                "endpoints": endpoints,
                "context_windows_source": window,
                "context_windows": list(ast.literal_eval(config[window]).items())
                if window
                else None,
            }
        )

    scripted_path = "lionagi/testing/_endpoint.py"
    scripted = definition(tree(scripted_path), "ScriptedEndpoint")
    [decorator] = scripted.decorator_list
    script = {item.arg: item.value for item in call(decorator, "register_endpoint").keywords}
    kind = kinds[ast.unparse(script.pop("endpoint_type"))]
    register = definition(registry, "register_endpoint")
    default_nodes = dict(
        zip(
            (arg.arg for arg in register.args.args[-len(register.args.defaults) :]),
            register.args.defaults,
            strict=True,
        )
    )
    script_settings = {
        field: ast.literal_eval(script.get(field, default_nodes[field]))
        for field in ("base_url", "auth_type", "content_type", "api_key_env", "options")
    }

    signatures = {}
    for path, calls in PUBLIC_CALLS.items():
        for name in calls:
            start, _, method = name.partition(".")
            node = definition(tree(path), start)
            signatures[f"{path}::{name}"] = signature(definition(node, method) if method else node)

    registry_class = definition(registry, "EndpointRegistry")
    returned = definition(registry_class, "list_providers").body[-1]
    if not (
        isinstance(returned, ast.Return)
        and isinstance(returned.value, ast.ListComp)
        and isinstance(returned.value.elt, ast.Dict)
    ):
        raise ValueError("EndpointRegistry.list_providers has a different projection")
    projection = [ast.literal_eval(key) for key in returned.value.elt.keys]
    order = ast.literal_eval(manifest["_CONTEXT_WINDOW_ORDER"])
    return {
        "schema_version": 1,
        "historical_bootstrap": {
            "commit": "166d7b001f1e56e129dab28ed1f111967a1e56b8",
            "modules": 33,
            "decorated_endpoint_classes": 36,
        },
        "manifest": {
            "path": manifest_path,
            "contract_version": ast.literal_eval(manifest["_CONTRACT_VERSION"]),
            "source": ast.literal_eval(manifest["_SOURCE"]),
            "providers": providers,
            "context_window_order": order,
        },
        "compatibility_endpoint": {
            "path": scripted_path,
            "module": ast.literal_eval(assignments(registry)["_SCRIPTED_MODULE"]),
            "class": scripted.name,
            "provider": ast.literal_eval(script["provider"]),
            "endpoint": ast.literal_eval(script["endpoint"]),
            "aliases": ast.literal_eval(script["aliases"]),
            "kind": kind,
            "settings": script_settings,
        },
        "compatibility_projection": projection,
        "context_examples": context_examples(providers, order),
        "public_exports": public_exports(tree),
        "signatures": signatures,
    }


def encode(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def verify_inventory(read_source: Callable[[str], str], expected: bytes) -> None:
    if encode(collect(read_source)).encode() != expected:
        raise ValueError(f"{INVENTORY} differs from the provider source inventory")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate checked inventory")
    args = parser.parse_args()

    def read_source(path: str) -> str:
        return (ROOT / path).read_text(encoding="utf-8")

    if args.write:
        (ROOT / INVENTORY).write_text(encode(collect(read_source)), encoding="utf-8")
    else:
        try:
            verify_inventory(read_source, (ROOT / INVENTORY).read_bytes())
        except (OSError, SyntaxError, KeyError, TypeError, ValueError) as exc:
            parser.exit(1, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
