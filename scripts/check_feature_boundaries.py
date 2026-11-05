#!/usr/bin/env python3
"""Inventory static imports and report feature-boundary debt without importing the package.

The scanner resolves literal imports against the source tree. Computed imports and
runtime-dependent aliases cannot be resolved statically. The JSON output is stable
across hash seeds and does not depend on Python's import order.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

LAYERS = ("foundation", "contracts", "sdk", "adapter", "root")
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
REQ = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


class ManifestError(ValueError):
    """The inventory cannot be trusted until the input is fixed."""


def module_files(root: Path) -> dict[str, Path]:
    result = {}
    for path in sorted((root / "lionagi").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        name = ".".join(parts)
        if name in result:
            raise ManifestError(f"duplicate module {name}: {result[name]} and {path}")
        result[name] = path
    if not result:
        raise ManifestError("no production modules found under lionagi/")
    return result


def lionhub_targets(root: Path) -> set[str]:
    """Enumerate graph targets without assigning or scanning their source."""
    base = root / "lionhub" / "src"
    package = base / "lionhub"
    if not package.is_dir():
        raise ManifestError("lionhub graph target tree is missing")
    names = {"lionhub"}
    for path in sorted(package.rglob("*.py")):
        parts = list(path.relative_to(base).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        names.add(".".join(parts))
        for count in range(1, len(parts)):
            names.add(".".join(parts[:count]))
    return names


def requirements(path: Path) -> set[str]:
    """Read requirement arrays from supported pyproject sections without importing TOML packages.

    Dependency entries in these sections must be literal strings; unsupported TOML
    forms fail closed rather than silently omitting a requirement.
    """
    text = path.read_text(encoding="utf-8")
    section = ""
    lines = text.splitlines(keepends=True)
    found: set[str] = set()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        header = re.match(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$", line.strip())
        if header:
            raw = header.group(1).strip()
            components = re.findall(r"""(?:[\w-]+|"(?:[^"\\]|\\.)*"|'[^']*')""", raw)
            if not components or ".".join(components) != re.sub(r"\s*\.\s*", ".", raw):
                raise ManifestError(f"unsupported TOML table name: {raw}")
            try:
                section = ".".join(
                    ast.literal_eval(part) if part.startswith(('"', "'")) else part
                    for part in components
                )
            except (SyntaxError, ValueError):
                raise ManifestError(f"unsupported TOML table name: {raw}") from None
            continue
        assignment = re.match(
            r"""^\s*((?:[\w.-]+)|(?:"(?:[^"\\]|\\.)*")|(?:'[^']*'))\s*=\s*(.*)""", line
        )
        if not assignment:
            continue
        key, value = assignment.groups()
        if key.startswith(('"', "'")):
            try:
                key = ast.literal_eval(key)
            except (SyntaxError, ValueError):
                raise ManifestError(f"unsupported requirement key: {key}") from None
        relevant = (
            (section == "project" and key == "dependencies")
            or section in {"project.optional-dependencies", "dependency-groups"}
            or (section == "build-system" and key == "requires")
            or (section == "tool.uv" and key == "constraint-dependencies")
        )
        if not relevant:
            continue
        # Tokenize brackets while ignoring brackets in quoted requirement strings and comments.
        buffer = value + "\n"
        while True:
            try:
                entries = ast.literal_eval(buffer)
                break
            except (SyntaxError, ValueError):
                if i >= len(lines) or re.match(r"^\s*\[[\w.-]+\]\s*$", lines[i]):
                    raise ManifestError(
                        f"unsupported requirement array: [{section}] {key}"
                    ) from None
                buffer += lines[i]
                i += 1
        if not isinstance(entries, list) or not all(isinstance(item, str) for item in entries):
            raise ManifestError(f"unsupported requirement array: [{section}] {key}")
        for entry in entries:
            match = REQ.match(entry)
            if not match:
                raise ManifestError(f"invalid requirement: {entry!r}")
            name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
            if name != "lionagi":
                found.add(name)
    return found


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_manifest(
    path: Path, files: dict[str, Path], declared: set[str], targets: set[str] | None = None
) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read manifest: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {
        "version",
        "modules",
        "requirements",
        "external_targets",
        "exceptions",
    }:
        raise ManifestError("manifest must contain version, modules, requirements and exceptions")
    if type(data["version"]) is not int or data["version"] != 1:
        raise ManifestError("unsupported manifest version")
    for field, expected in (("modules", set(files)), ("requirements", declared)):
        entries = data[field]
        if not isinstance(entries, dict):
            raise ManifestError(f"{field} must be an object")
        missing, extra = expected - set(entries), set(entries) - expected
        if missing or extra:
            raise ManifestError(
                f"{field} coverage: missing={sorted(missing)}, extra={sorted(extra)}"
            )
        for name, entry in entries.items():
            allowed = (
                {"layer", "owner", "target_owner", "additional_targets"}
                if field == "modules"
                else {"layer", "owner", "secondary_users"}
            )
            if not isinstance(entry, dict) or not {"layer", "owner"} <= set(entry) <= allowed:
                raise ManifestError(f"invalid {field} entry: {name}")
            if (
                not isinstance(entry["layer"], str)
                or entry["layer"] not in LAYERS
                or not isinstance(entry["owner"], str)
                or not entry["owner"].strip()
                or entry["owner"].strip().casefold() == "common"
            ):
                raise ManifestError(f"invalid layer or owner: {name}")
            if "target_owner" in entry and (
                not isinstance(entry["target_owner"], str) or not entry["target_owner"].strip()
            ):
                raise ManifestError(f"invalid target owner: {name}")
            if "additional_targets" in entry:
                targets_value = entry["additional_targets"]
                if (
                    not isinstance(targets_value, list)
                    or not targets_value
                    or any(not isinstance(item, str) or not item.strip() for item in targets_value)
                    or len(set(targets_value)) != len(targets_value)
                ):
                    raise ManifestError(f"invalid additional targets: {name}")
            if "secondary_users" in entry:
                users = entry["secondary_users"]
                if (
                    not isinstance(users, list)
                    or not users
                    or any(not isinstance(user, str) or not user.strip() for user in users)
                    or len(set(users)) != len(users)
                    or entry["owner"] in users
                ):
                    raise ManifestError(f"invalid secondary users: {name}")
    external = data["external_targets"]
    if not isinstance(external, dict) or set(external) != {"lionhub"}:
        raise ManifestError("external_targets must declare the lionhub layer and owner")
    target = external["lionhub"]
    if (
        not isinstance(target, dict)
        or set(target) != {"layer", "owner"}
        or target["layer"] not in LAYERS
        or not isinstance(target["owner"], str)
        or not target["owner"].strip()
    ):
        raise ManifestError("invalid lionhub layer or owner")
    exceptions = data["exceptions"]
    if not isinstance(exceptions, list):
        raise ManifestError("exceptions must be an array")
    seen = set()
    for entry in exceptions:
        if not isinstance(entry, dict) or set(entry) != {
            "source",
            "target",
            "issue",
            "owner",
            "phase",
        }:
            raise ManifestError(
                "exception must have exactly source, target, issue, owner and phase"
            )
        if any(
            not isinstance(v, str) or not v.strip() or any(c in v for c in "*?[]")
            for v in entry.values()
        ):
            raise ManifestError(f"invalid exception fields: {entry}")
        if entry["source"] not in files or entry["target"] not in (set(files) | (targets or set())):
            raise ManifestError(f"exception must name exact modules: {entry}")
        if not re.fullmatch(r"#[1-9][0-9]*", entry["issue"]):
            raise ManifestError(f"invalid exception issue: {entry}")
        if entry["phase"] not in {"P1", "P2", "P3", "P4", "P5"}:
            raise ManifestError(f"invalid exception removal phase: {entry['phase']}")
        pair = (entry["source"], entry["target"])
        if pair in seen:
            raise ManifestError(f"duplicate exception: {pair}")
        seen.add(pair)
    return data


def validate_target_notes(root: Path, manifest: dict[str, Any], targets: set[str]) -> None:
    """Only name a destination that exists or is stated in a public allocation."""
    docs = sorted((root / "docs/adr").glob("ADR-013[89]-*.md"))
    if len(docs) != 2:
        raise ManifestError("destination allocation documents are missing or ambiguous")
    allocations = "\n".join(path.read_text(encoding="utf-8") for path in docs)
    for name, entry in manifest["modules"].items():
        notes = ([entry["target_owner"]] if "target_owner" in entry else []) + entry.get(
            "additional_targets", []
        )
        for target in notes:
            if (
                target
                in {
                    "lionhub (destination not yet recorded)",
                    "pending the CLI location decision (ADR-0139 D1)",
                }
                or target in targets
            ):
                continue
            if target == "lionhub.cli" or target.startswith("lionhub.cli."):
                raise ManifestError(f"pending CLI destination for {name}: {target}")
            if not NAME.fullmatch(target) or not re.search(
                rf"(?<![\w.]){re.escape(target)}(?![\w.])", allocations
            ):
                raise ManifestError(f"unrecorded destination for {name}: {target}")


def dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


class Scanner(ast.NodeVisitor):
    def __init__(self, source: str, path: Path, modules: set[str]):
        self.source, self.modules = source, modules
        self.package = source if path.name == "__init__.py" else source.rpartition(".")[0]
        self.edges: set[tuple[str, str, int, str]] = set()
        self.flags: set[str] = set()
        self.aliases: dict[str, str] = {}

    def add(self, target: str, line: int, kind: str | None = None) -> None:
        if target and NAME.fullmatch(target):
            label = kind or (
                "type_checking"
                if "type_checking" in self.flags
                else "optional"
                if "optional" in self.flags
                else "import"
            )
            self.edges.add((self.source, target, line, label))

    def resolve(self, module: str) -> str:
        if module.startswith("."):
            try:
                return importlib.util.resolve_name(module, self.package)
            except (ImportError, ValueError):
                return ""
        return module

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.add(alias.name, node.lineno)
            self.aliases[alias.asname or alias.name.split(".")[0]] = (
                alias.name if alias.asname else alias.name.split(".")[0]
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = self.resolve("." * node.level + (node.module or ""))
        if not module:
            return
        self.add(module, node.lineno)
        for alias in node.names:
            candidate = f"{module}.{alias.name}"
            if candidate in self.modules:
                self.add(candidate, node.lineno)
            self.aliases[alias.asname or alias.name] = candidate

    def visit_If(self, node: ast.If) -> None:
        type_only = self._type_test(node.test)
        type_in_else = (
            isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and self._type_test(node.test.operand)
        )
        self.visit(node.test)
        if type_only:
            previous = self.flags.copy()
            self.flags.add("type_checking")
            for child in node.body:
                self.visit(child)
            self.flags = previous
            for child in node.orelse:
                self.visit(child)
        elif type_in_else:
            for child in node.body:
                self.visit(child)
            previous = self.flags.copy()
            self.flags.add("type_checking")
            for child in node.orelse:
                self.visit(child)
            self.flags = previous
        else:
            for child in (*node.body, *node.orelse):
                self.visit(child)

    def _type_test(self, node: ast.AST) -> bool:
        name = dotted(node)
        if name:
            head, sep, tail = name.partition(".")
            expanded = self.aliases.get(head, head) + (sep + tail if sep else "")
            return expanded in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            return any(self._type_test(value) for value in node.values)
        return False

    def visit_Try(self, node: ast.Try) -> None:
        optional = any(
            dotted(part)
            in {
                "ImportError",
                "ModuleNotFoundError",
                "builtins.ImportError",
                "builtins.ModuleNotFoundError",
            }
            for handler in node.handlers
            if handler.type
            for part in ast.walk(handler.type)
        )
        previous = self.flags.copy()
        if optional:
            self.flags.add("optional")
        for child in node.body:
            self.visit(child)
        self.flags = previous
        for child in (*node.handlers, *node.orelse, *node.finalbody):
            self.visit(child)

    def visit_Call(self, node: ast.Call) -> None:
        name = dotted(node.func)
        if name:
            head, sep, tail = name.partition(".")
            name = self.aliases.get(head, head) + (sep + tail if sep else "")
        wrapped = name in {"lionagi.ln.import_module", "lionagi.ln._utils.import_module"}
        if (name in {"importlib.import_module", "__import__"} or wrapped) and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                module = arg.value
                child = node.args[1] if wrapped and len(node.args) > 1 else None
                if wrapped:
                    child = next(
                        (kw.value for kw in node.keywords if kw.arg == "module_name"), child
                    )
                if wrapped and child is not None:
                    if isinstance(child, ast.Constant) and isinstance(child.value, str):
                        module += "." + child.value
                    elif not isinstance(child, ast.Constant) or child.value is not None:
                        self.generic_visit(node)
                        return
                package_node = node.args[1] if len(node.args) > 1 and not wrapped else None
                for kw in node.keywords:
                    if kw.arg == "package":
                        package_node = kw.value
                package = self.package
                if isinstance(package_node, ast.Constant) and isinstance(package_node.value, str):
                    package = package_node.value
                elif isinstance(package_node, ast.Name) and package_node.id == "__name__":
                    package = self.source
                elif package_node is not None and not (
                    isinstance(package_node, ast.Name) and package_node.id == "__package__"
                ):
                    # An expression-valued package cannot be resolved without executing code.
                    package = ""
                try:
                    target = (
                        importlib.util.resolve_name(module, package)
                        if module.startswith(".")
                        else module
                    )
                except (ImportError, ValueError):
                    target = ""
                self.add(target, node.lineno, "dynamic_literal")
        self.generic_visit(node)

    def annotation(self, node: ast.AST | None) -> None:
        if node is None:
            return
        pending = [
            part.value
            for part in ast.walk(node)
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        ]
        seen = set()
        while pending:
            text = pending.pop()
            if text in seen:
                continue
            seen.add(text)
            try:
                expression = ast.parse(text, mode="eval")
            except SyntaxError:
                continue
            parts = list(ast.walk(expression))
            attribute_values = {id(part.value) for part in parts if isinstance(part, ast.Attribute)}
            for inner in parts:
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    pending.append(inner.value)
                name = dotted(inner)
                if not name or id(inner) in attribute_values:
                    continue
                head, sep, tail = name.partition(".")
                expanded = self.aliases.get(head, head) + (sep + tail if sep else "")
                matches = [
                    mod for mod in self.modules if expanded == mod or expanded.startswith(mod + ".")
                ]
                if matches:
                    self.add(max(matches, key=len), getattr(node, "lineno", 0), "string_annotation")

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.annotation(node.annotation)
        self._lazy(node.target, node.value)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._lazy(target, node.value)
        self.generic_visit(node)

    def _lazy(self, target: ast.AST, value: ast.AST | None) -> None:
        name = (dotted(target) or "").lower()
        if isinstance(value, ast.Dict) and any(
            word in name for word in ("lazy", "export", "import")
        ):
            items = value.values
        elif name == "_source_modules" and isinstance(value, (ast.Tuple, ast.List)):
            # The compatibility type facade searches these modules in __getattr__.
            items = value.elts
        else:
            return
        for item in items:
            if isinstance(item, (ast.Tuple, ast.List)) and item.elts:
                item = item.elts[0]
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                raw = item.value
                if raw.startswith("."):
                    raw = self.resolve(raw)
                elif not raw.startswith(("lionagi.", "lionhub.")) and raw not in {
                    "lionagi",
                    "lionhub",
                }:
                    raw = self.package + "." + raw
                self.add(raw, item.lineno, "lazy_export")

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.annotation(node.returns)
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            self.annotation(arg.annotation)
        if node.args.vararg:
            self.annotation(node.args.vararg.annotation)
        if node.args.kwarg:
            self.annotation(node.args.kwarg.annotation)
        aliases = self.aliases.copy()
        try:
            self.generic_visit(node)
        finally:
            self.aliases = aliases

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        aliases = self.aliases.copy()
        try:
            self.generic_visit(node)
        finally:
            self.aliases = aliases


def scan(files: dict[str, Path], targets: set[str] | None = None) -> list[dict[str, Any]]:
    modules = set(files) | (targets or set())
    edges = set()
    for source, path in sorted(files.items()):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise ManifestError(f"cannot parse {source}: {exc}") from exc
        scanner = Scanner(source, path, modules)
        scanner.visit(tree)
        edges.update(scanner.edges)
    return [
        dict(source=source, target=target, line=line, kind=kind)
        for source, target, line, kind in sorted(edges)
    ]


def classify(
    edges: list[dict[str, Any]], manifest: dict[str, Any], targets: set[str] | None = None
) -> list[dict[str, Any]]:
    modules = manifest["modules"]
    exceptions = {(item["source"], item["target"]): item for item in manifest["exceptions"]}
    findings = []
    for edge in edges:
        source, target = edge["source"], edge["target"]
        if target in modules:
            dst = modules[target]
        elif target in (targets or set()):
            dst = manifest["external_targets"]["lionhub"]
        else:
            continue
        src = modules[source]
        if LAYERS.index(src["layer"]) < LAYERS.index(dst["layer"]):
            violation = "upward"
        elif src["layer"] == dst["layer"] == "adapter" and src["owner"] != dst["owner"]:
            violation = "sibling_adapter"
        elif src["layer"] == dst["layer"] == "root" and src["owner"] != dst["owner"]:
            violation = "cross_root"
        else:
            continue
        findings.append(
            {
                **edge,
                "violation": violation,
                "target_owner": dst["owner"],
                "exception": exceptions.get((source, target)),
            }
        )
    return findings


def validate_exception_edges(findings: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    pairs = {(edge["source"], edge["target"]) for edge in findings}
    for entry in manifest["exceptions"]:
        if (entry["source"], entry["target"]) not in pairs:
            raise ManifestError(
                f"exception does not name a current boundary finding: {entry['source']} -> {entry['target']}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--json", action="store_true", help="print the complete graph as JSON")
    args = parser.parse_args(argv)
    try:
        files = module_files(args.root)
        declared = requirements(args.root / "pyproject.toml")
        targets = lionhub_targets(args.root)
        manifest = load_manifest(
            args.manifest or args.root / "scripts/feature_boundaries.json", files, declared, targets
        )
        validate_target_notes(args.root, manifest, targets)
        edges = scan(files, targets)
        findings = classify(edges, manifest, targets)
        validate_exception_edges(findings, manifest)
    except (ManifestError, OSError) as exc:
        print(f"feature boundary error: {exc}", file=sys.stderr)
        return 1
    unresolved = [
        edge
        for edge in edges
        if (edge["target"].startswith("lionagi.") and edge["target"] not in files)
        or (edge["target"].startswith("lionhub.") and edge["target"] not in targets)
    ]
    if args.json:
        print(
            json.dumps(
                {"edges": edges, "findings": findings, "unresolved_internal": unresolved},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for edge in unresolved:
            print(
                f"unresolved: {edge['source']}:{edge['line']} -> {edge['target']} (kind={edge['kind']})"
            )
        for finding in findings:
            print(
                f"{finding['violation']}: {finding['source']}:{finding['line']} -> {finding['target']} (owner={finding['target_owner']}, kind={finding['kind']})"
            )
        counts = Counter(finding["violation"] for finding in findings)
        print(
            f"modules={len(files)} requirements={len(declared)} edges={len(edges)} unresolved={len(unresolved)} findings={len(findings)} classes={dict(sorted(counts.items()))} exceptions={len(manifest['exceptions'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
