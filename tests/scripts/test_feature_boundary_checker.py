"""Static inventory and manifest validation tests."""

import json
from pathlib import Path

import pytest

from scripts import build_feature_boundaries as builder
from scripts import check_feature_boundaries as checker


def fixture_project(tmp_path: Path):
    root = tmp_path
    files = {
        "lionagi/__init__.py": """\n_LAZY_MAP = {"thing": ("adapter", "Thing")}\n""",
        "lionagi/foundation.py": """\nimport lionagi.adapter\nfrom lionagi import sibling\nfrom typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from lionagi import contract\ntry:\n    import lionagi.root\nexcept (ImportError, ModuleNotFoundError):\n    pass\nimport importlib as il\nfrom importlib import import_module as load\nil.import_module("lionagi.adapter")\nload(".contract", package="lionagi")\ndef f(value: "lionagi.adapter.Thing") -> "lionagi.contract.Contract":\n    pass\n""",
        "lionagi/adapter.py": "import lionagi.sibling\n",
        "lionagi/sibling.py": "",
        "lionagi/contract.py": "",
        "lionagi/root.py": "import lionagi.other_root\n",
        "lionagi/other_root.py": "",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    hub_init = root / "lionhub/src/lionhub/__init__.py"
    hub_init.parent.mkdir(parents=True)
    hub_init.write_text("", encoding="utf-8")
    adr = root / "docs/adr"
    adr.mkdir(parents=True)
    (adr / "ADR-0138-fixture.md").write_text("lionhub.serve\n", encoding="utf-8")
    (adr / "ADR-0139-fixture.md").write_text("lionhub.cli\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        """\n[project]\ndependencies = ["Example_Package>=1"]\n[project.optional-dependencies]\nextra = ["Some.Extra>=2", "lionagi[extra]"]\n[dependency-groups]\ndev = ["pytest"]\n[build-system]\nrequires = ["hatchling"]\n[tool.uv]\nconstraint-dependencies = ["Safety-Pkg"]\n""",
        encoding="utf-8",
    )
    modules = checker.module_files(root)
    layers = {
        "lionagi": ("foundation", "base"),
        "lionagi.foundation": ("foundation", "base"),
        "lionagi.contract": ("contracts", "contract"),
        "lionagi.adapter": ("adapter", "a"),
        "lionagi.sibling": ("adapter", "b"),
        "lionagi.root": ("root", "cli"),
        "lionagi.other_root": ("root", "studio"),
    }
    data = {
        "version": 1,
        "modules": {
            name: {"layer": layer, "owner": owner} for name, (layer, owner) in layers.items()
        },
        "requirements": {
            name: {"layer": "root", "owner": "tooling"}
            for name in checker.requirements(root / "pyproject.toml")
        },
        "exceptions": [],
        "external_targets": {"lionhub": {"layer": "root", "owner": "lionhub (downstream package)"}},
    }
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    return root, modules, data, manifest


def test_edge_forms_and_boundary_classes(tmp_path):
    root, modules, data, manifest = fixture_project(tmp_path)
    checked = checker.load_manifest(
        manifest, modules, checker.requirements(root / "pyproject.toml")
    )
    graph = checker.scan(modules)
    forms = {(e["source"], e["target"], e["kind"]) for e in graph}
    assert ("lionagi", "lionagi.adapter", "lazy_export") in forms
    assert ("lionagi.foundation", "lionagi.adapter", "import") in forms
    assert ("lionagi.foundation", "lionagi.contract", "type_checking") in forms
    assert ("lionagi.foundation", "lionagi.root", "optional") in forms
    assert ("lionagi.foundation", "lionagi.adapter", "dynamic_literal") in forms
    assert ("lionagi.foundation", "lionagi.contract", "dynamic_literal") in forms
    assert ("lionagi.foundation", "lionagi.adapter", "string_annotation") in forms
    findings = checker.classify(graph, checked)
    assert {f["violation"] for f in findings} == {"upward", "sibling_adapter", "cross_root"}
    assert any(f["target"] == "lionagi.adapter" and f["target_owner"] == "a" for f in findings)
    assert checker.scan(dict(reversed(list(modules.items())))) == graph
    assert checker.main(["--root", str(root), "--manifest", str(manifest)]) == 0


def test_requirement_coverage_and_duplicates(tmp_path):
    root, modules, data, manifest = fixture_project(tmp_path)
    declared = checker.requirements(root / "pyproject.toml")
    assert declared == {"example-package", "some-extra", "pytest", "hatchling", "safety-pkg"}
    del data["requirements"]["pytest"]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(checker.ManifestError, match="coverage"):
        checker.load_manifest(manifest, modules, declared)
    manifest.write_text('{"version": 1, "version": 1}', encoding="utf-8")
    with pytest.raises(checker.ManifestError, match="duplicate"):
        checker.load_manifest(manifest, modules, declared)


def test_exception_exactness_and_evidence(tmp_path):
    root, modules, data, manifest = fixture_project(tmp_path)
    declared = checker.requirements(root / "pyproject.toml")
    entry = {
        "source": "lionagi.foundation",
        "target": "lionagi.adapter",
        "issue": "#3278",
        "owner": "a",
        "phase": "P1",
    }
    data["exceptions"] = [entry]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    checked = checker.load_manifest(manifest, modules, declared)
    findings = checker.classify(checker.scan(modules), checked)
    assert any(f["exception"] == entry for f in findings)
    checker.validate_exception_edges(findings, checked)
    for phase in ("P1", "P2", "P3", "P4", "P5"):
        data["exceptions"] = [{**entry, "phase": phase}]
        manifest.write_text(json.dumps(data), encoding="utf-8")
        checker.load_manifest(manifest, modules, declared)
    with pytest.raises(checker.ManifestError, match="does not name a current boundary finding"):
        checker.validate_exception_edges([], checked)
    for change in (
        {"source": "lionagi.*"},
        {"target": "lionagi.missing"},
        {"issue": ""},
        {"owner": ""},
        {"phase": "*"},
        {"phase": "later"},
        {"issue": "3278"},
    ):
        data["exceptions"] = [{**entry, **change}]
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(checker.ManifestError):
            checker.load_manifest(manifest, modules, declared)
    data["exceptions"] = [entry, entry]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(checker.ManifestError, match="duplicate"):
        checker.load_manifest(manifest, modules, declared)


def test_nonliteral_dependency_array_fails_closed(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\ndependencies = [{ include-group = "dev" }]\n', encoding="utf-8")
    with pytest.raises(checker.ManifestError, match="unsupported"):
        checker.requirements(path)


def test_relative_type_aliases_nested_strings_and_missing_target(tmp_path, capsys):
    root, modules, _, manifest = fixture_project(tmp_path)
    (root / "lionagi/foundation.py").write_text(
        """
import typing as t
from . import adapter
if t.TYPE_CHECKING:
    from . import contract
if not t.TYPE_CHECKING:
    import lionagi.root
from importlib import import_module
import_module(".adapter", package=__package__)
import_module(".missing", package=__package__)
def f(value: "list['lionagi.contract.Contract']"):
    pass
""",
        encoding="utf-8",
    )
    graph = checker.scan(modules)
    forms = {(e["target"], e["kind"]) for e in graph if e["source"] == "lionagi.foundation"}
    assert ("lionagi.adapter", "import") in forms
    assert ("lionagi.contract", "type_checking") in forms
    assert ("lionagi.root", "import") in forms
    assert ("lionagi.adapter", "dynamic_literal") in forms
    assert ("lionagi.missing", "dynamic_literal") in forms
    assert ("lionagi.contract", "string_annotation") in forms
    assert ("lionagi", "string_annotation") not in forms
    assert checker.main(["--root", str(root), "--manifest", str(manifest), "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert any(edge["target"] == "lionagi.missing" for edge in output["unresolved_internal"])


def test_lazy_mapping_relative_and_source_module_sequence(tmp_path):
    root, modules, _, _ = fixture_project(tmp_path)
    (root / "lionagi/__init__.py").write_text(
        """
_EXPORTS = {"A": (".adapter", "A"), "B": "contract"}
_SOURCE_MODULES = ("sibling",)
""",
        encoding="utf-8",
    )
    edges = checker.scan(modules)
    targets = {
        e["target"] for e in edges if e["source"] == "lionagi" and e["kind"] == "lazy_export"
    }
    assert targets == {"lionagi.adapter", "lionagi.contract", "lionagi.sibling"}


def test_lionhub_graph_targets_are_classified_without_ownership(tmp_path):
    root, modules, data, manifest = fixture_project(tmp_path)
    hub = root / "lionhub/src/lionhub"
    (hub / "serve").mkdir(parents=True, exist_ok=True)
    (hub / "__init__.py").write_text("", encoding="utf-8")
    (hub / "serve/config.py").write_text("", encoding="utf-8")
    (root / "lionagi/root.py").write_text("import lionhub.serve.config\n", encoding="utf-8")
    (root / "lionagi/foundation.py").write_text(
        "from lionhub.serve import config\n"
        "from typing import TYPE_CHECKING\n"
        "if not TYPE_CHECKING:\n    pass\nelse:\n    import lionhub.serve.config\n"
        'import importlib\nimportlib.import_module("lionhub.serve.config")\n'
        "from lionagi.ln import import_module as load\n"
        'load("lionhub.serve", "config")\n'
        'load("lionhub.serve", module_name="config")\n'
        "import importlib as imp\n"
        "def unrelated():\n    import math as imp\n"
        'def still_imports():\n    return imp.import_module("lionhub.serve.config")\n'
        'def f(x: "lionhub.serve.config.Value"):\n    pass\n'
        '_LAZY_MAP = {"x": ("lionhub.serve.config", "Value")}\n',
        encoding="utf-8",
    )
    targets = checker.lionhub_targets(root)
    assert {"lionhub", "lionhub.serve", "lionhub.serve.config"} <= targets
    graph = checker.scan(modules, targets)
    forms = {(e["target"], e["kind"]) for e in graph}
    for kind in ("import", "type_checking", "dynamic_literal", "string_annotation", "lazy_export"):
        assert ("lionhub.serve.config", kind) in forms
    assert (
        sum(e["kind"] == "dynamic_literal" and e["target"] == "lionhub.serve.config" for e in graph)
        == 4
    )
    checked = checker.load_manifest(
        manifest, modules, checker.requirements(root / "pyproject.toml"), targets
    )
    findings = checker.classify(graph, checked, targets)
    assert any(
        f["target"] == "lionhub.serve.config"
        and f["violation"] == "upward"
        and f["target_owner"] == "lionhub (downstream package)"
        for f in findings
    )
    assert any(
        f["source"] == "lionagi.root"
        and f["target"] == "lionhub.serve.config"
        and f["violation"] == "cross_root"
        for f in findings
    )
    assert not any(name.startswith("lionhub") for name in data["modules"])


def test_secondary_users_and_module_coverage_fail_closed(tmp_path):
    root, modules, data, manifest = fixture_project(tmp_path)
    declared = checker.requirements(root / "pyproject.toml")
    data["requirements"]["pytest"]["secondary_users"] = ["developer"]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    checker.load_manifest(manifest, modules, declared)
    for value in ([], [""], ["developer", "developer"], ["tooling"], "developer"):
        data["requirements"]["pytest"]["secondary_users"] = value
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(checker.ManifestError, match="secondary"):
            checker.load_manifest(manifest, modules, declared)
    data["requirements"]["pytest"].pop("secondary_users")
    (root / "lionagi/new_module.py").write_text("", encoding="utf-8")
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(checker.ManifestError, match="coverage"):
        checker.load_manifest(manifest, checker.module_files(root), declared)


def test_target_notes_use_recorded_destinations_only():
    fallback = "lionhub (destination not yet recorded)"
    assert builder.match(builder.TARGET_RULES, "lionagi.cli.agent") == (
        "pending the CLI location decision (ADR-0139 D1)"
    )
    assert builder.match(builder.TARGET_RULES, "lionagi.studio.scheduler.engine") == (
        "lionhub.orchestration.scheduler"
    )
    assert builder.match(builder.TARGET_RULES, "lionagi.providers.openai.codex") == fallback
    assert builder.match(builder.TARGET_RULES, "lionagi.tools.khive_injection") == fallback
    assert (
        builder.match(builder.TARGET_RULES, "lionagi.studio.services.sessions") == "lionhub.serve"
    )
    assert builder.ADDITIONAL_TARGETS["lionagi.cli.orchestrate"] == [
        "lionhub.orchestration.flow.parsing"
    ]


def test_committed_inventory_matches_generator_and_rejects_hand_edit(tmp_path):
    committed = Path(builder.ROOT) / "scripts/feature_boundaries.json"
    candidate = tmp_path / "candidate.json"
    generated = tmp_path / "generated.json"
    candidate.write_bytes(committed.read_bytes())

    def assert_matches():
        # Seed the output with the candidate so its exact exceptions are preserved.
        generated.write_bytes(candidate.read_bytes())
        builder.main(["--output", str(generated)])
        assert candidate.read_bytes() == generated.read_bytes()

    assert_matches()
    mutated = json.loads(candidate.read_text(encoding="utf-8"))
    mutated["exceptions"] = [
        {
            "source": "lionagi",
            "target": "lionagi.config",
            "issue": "#1",
            "owner": "configuration",
            "phase": "P1",
        }
    ]
    candidate.write_text(json.dumps(mutated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert_matches()
    mutated["modules"]["lionagi"]["owner"] = "unreviewed-owner"
    candidate.write_text(json.dumps(mutated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(AssertionError):
        assert_matches()


def test_quoted_extra_names_and_missing_target_tree(tmp_path):
    root, _, _, _ = fixture_project(tmp_path)
    project = root / "pyproject.toml"
    project.write_text(
        '[project]\ndependencies=[]\n["project"."optional-dependencies"]\n'
        '"special-group"=["one-dep"]\n\'other-group\'=["two-dep"]\n',
        encoding="utf-8",
    )
    assert checker.requirements(project) == {"one-dep", "two-dep"}
    with pytest.raises(checker.ManifestError, match="target tree"):
        checker.lionhub_targets(root / "missing")


def test_target_notes_require_public_evidence(tmp_path):
    root, _, data, _ = fixture_project(tmp_path)
    data["modules"]["lionagi.foundation"]["target_owner"] = "lionhub.serve"
    checker.validate_target_notes(root, data, checker.lionhub_targets(root))
    data["modules"]["lionagi.foundation"]["target_owner"] = "lionhub.serve.unrecorded"
    with pytest.raises(checker.ManifestError, match="unrecorded destination"):
        checker.validate_target_notes(root, data, checker.lionhub_targets(root))
    data["modules"]["lionagi.foundation"]["target_owner"] = "lionhub.cli"
    with pytest.raises(checker.ManifestError, match="pending CLI destination"):
        checker.validate_target_notes(root, data, checker.lionhub_targets(root))


@pytest.mark.parametrize(
    "case",
    ["stale_module", "stale_requirement", "missing_field", "extra_field", "phase", "nonfinding"],
)
def test_invalid_inventory_exits_nonzero(tmp_path, capsys, case):
    root, _, data, manifest = fixture_project(tmp_path)
    entry = {
        "source": "lionagi.foundation",
        "target": "lionagi.adapter",
        "issue": "#3278",
        "owner": "a",
        "phase": "P1",
    }
    if case == "stale_module":
        data["modules"]["lionagi.removed"] = {"layer": "sdk", "owner": "old"}
    elif case == "stale_requirement":
        data["requirements"]["removed-distribution"] = {"layer": "sdk", "owner": "old"}
    elif case == "missing_field":
        entry.pop("phase")
        data["exceptions"] = [entry]
    elif case == "extra_field":
        data["exceptions"] = [{**entry, "reason": "temporary"}]
    elif case == "phase":
        data["exceptions"] = [{**entry, "phase": "later"}]
    else:
        data["exceptions"] = [{**entry, "target": "lionagi"}]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    assert checker.main(["--root", str(root), "--manifest", str(manifest)]) == 1
    error = capsys.readouterr().err
    if case == "nonfinding":
        assert "lionagi.foundation -> lionagi" in error
    elif case == "phase":
        assert "phase" in error
    else:
        assert "coverage" in error or "exactly" in error
