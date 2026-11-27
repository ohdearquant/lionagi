"""Static inventory and manifest validation tests."""

import ast
import json
import re
import subprocess
from importlib import import_module
from pathlib import Path

import pytest

from lionagi import _moved
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
    (adr / "ADR-0139-fixture.md").write_text("lionhub.serve.cli\nlionhub.cli\n", encoding="utf-8")
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


def test_dynamic_imports_that_name_their_module_by_keyword(tmp_path):
    root, modules, _, _ = fixture_project(tmp_path)
    (root / "lionagi/foundation.py").write_text(
        """
import importlib
from lionagi.ln import import_module as load
importlib.import_module(name="lionagi.adapter")
importlib.import_module(name=".contract", package="lionagi")
__import__(name="lionagi.root")
load(package_name="lionagi", module_name="sibling")
importlib.import_module(name=computed)
load(package_name=computed)
""",
        encoding="utf-8",
    )
    graph = checker.scan(modules)
    targets = {
        e["target"]
        for e in graph
        if e["source"] == "lionagi.foundation" and e["kind"] == "dynamic_literal"
    }
    assert targets == {"lionagi.adapter", "lionagi.contract", "lionagi.root", "lionagi.sibling"}


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


def _tracked_boundary_paths(root):
    return set(
        subprocess.check_output(
            ["git", "ls-files", "-z", "--", "lionagi", "lionhub/src/lionhub"],
            cwd=root,
            encoding="utf-8",
        ).split("\0")
    )


def _tracked_lionhub_module(root, tracked, name):
    path = Path("lionhub/src", *name.split("."))
    return next(
        (
            candidate
            for candidate in (path.with_suffix(".py"), path / "__init__.py")
            if candidate.as_posix() in tracked and (root / candidate).is_file()
        ),
        None,
    )


def test_target_notes_use_recorded_destinations_only():
    names = checker.module_files(builder.ROOT)
    for label, rules in (
        ("MODULE_RULES", builder.MODULE_RULES),
        ("TARGET_RULES", builder.TARGET_RULES),
    ):
        unused = {
            key
            for key in rules
            if not any(name == key or name.startswith(key + ".") for name in names)
        }
        assert not unused, f"{label} do not match current core source: {sorted(unused)}"

    rules = {"lionagi.example": "lionhub.example", "lionagi.example.cli": "lionhub.serve.cli"}
    assert builder.match(rules, "lionagi.example.cli.main") == "lionhub.serve.cli"
    assert builder.match(rules, "lionagi.example.other") == "lionhub.example"
    assert builder.match(rules, "lionagi.examples") is None


def test_moved_feature_boundaries_use_tracked_lionhub_destinations():
    root = builder.ROOT
    tracked = _tracked_boundary_paths(root)
    expected = {
        "lionagi.agent": "lionhub.agent",
        "lionagi.tools": "lionhub.tools",
        "lionagi.tools.base": "lionhub.tools.base",
        "lionagi.casts": "lionhub.agent.casts",
        "lionagi.hooks.external": "lionhub.hooks.external",
    }
    for old, target in expected.items():
        assert _moved.resolve(old) == target, old
        assert _tracked_lionhub_module(root, tracked, target) is not None, target


def _adr_0139_step_3_section(root):
    adr = root / "docs/adr/ADR-0139-cli-package-location-and-console-script-ownership.md"
    _, heading, d2 = adr.read_text(encoding="utf-8").partition("\n### D2 — ")
    assert heading, "ADR-0139 D2 heading is missing"
    return d2.partition("\n### ")[0]


def _adr_0139_step_3_table(section, columns):
    header = "| " + " | ".join(columns) + " |"
    lines = section.splitlines()
    assert lines.count(header) == 1, f"ADR-0139 D2 requires exactly one {header}"
    boundary = lines.index(header) + 1
    separator = "| " + " | ".join("---" for _ in columns) + " |"
    assert lines[boundary : boundary + 1] == [separator], (
        f"ADR-0139 D2 invalid separator for {header}"
    )
    rows = []
    for row in lines[boundary + 1 :]:
        if not row.strip():
            break
        cells = [cell.strip() for cell in row.strip().split("|")]
        assert len(cells) == len(columns) + 2 and not (cells[0] or cells[-1]), (
            f"ADR-0139 D2 invalid table row: {row}"
        )
        rows.append(cells[1:-1])
    assert rows, f"ADR-0139 D2 table has no data rows: {header}"
    return rows


def _adr_0139_step_3_destinations(section):
    rows = _adr_0139_step_3_table(
        section,
        (
            "Source before relocation (line numbers are at `327665f3966cb66987c726a6059470e9791e9cce`)",
            "Step-3 destination",
            "Scope",
        ),
    )
    destinations_by_source = {}
    for source, destination, _ in rows:
        path = re.match(r"`(lionagi/[\w/]+\.py)(?::[^`]+)?`(?:\s|$)", source)
        target = re.fullmatch(r"`(lionhub(?:\.\w+)+)`", destination)
        assert path and target, (
            f"ADR-0139 D2 invalid source or destination: {source} | {destination}"
        )
        old = path[1].removesuffix(".py").replace("/", ".")
        assert old not in destinations_by_source, f"duplicate ADR-0139 D2 source: {old}"
        destinations_by_source[old] = target[1]
    return destinations_by_source


def test_adr_0139_step_3_destinations_match_moves_or_target_rules():
    root = builder.ROOT
    section = _adr_0139_step_3_section(root)
    destinations_by_source = _adr_0139_step_3_destinations(section)
    tracked = _tracked_boundary_paths(root)
    for old, expected in destinations_by_source.items():
        destinations = [move.new for move in _moved.MOVED if move.old == old]
        if destinations:
            assert destinations == [expected], f"{old}: ADR-0139 D2 requires {expected} in MOVED"
            assert _tracked_lionhub_module(root, tracked, expected) is not None, (
                f"{expected}: ADR-0139 D2 destination file is missing or untracked"
            )
        else:
            original = old.replace(".", "/") + ".py"
            assert original in tracked and (root / original).is_file(), (
                f"{old}: ADR-0139 D2 needs an exact MOVED entry or a tracked original file"
            )
            assert builder.TARGET_RULES.get(old) == expected, (
                f"{old}: ADR-0139 D2 requires an exact TARGET_RULES entry for {expected}"
            )

    # The step-6 package move is separate. D2 must record every exact CLI descendant route.
    cli_moves = {move.old for move in _moved.MOVED if move.old.startswith("lionagi.cli.")}
    unlisted = cli_moves - destinations_by_source.keys()
    assert not unlisted, f"Exact CLI routes missing from ADR-0139 D2: {sorted(unlisted)}"


def _adr_0139_step_3_shared_hook(section):
    rows = _adr_0139_step_3_table(
        section, ("Source at this HEAD", "Step-3 public owner", "Extraction; retained adapter")
    )
    assert len(rows) == 1, "ADR-0139 D2 specifies one shared definition"
    historical_source, owner, extraction = rows[0]
    historical = re.fullmatch(
        r"`(lionagi/cli/orchestrate/_orchestration\.py):([1-9]\d*)@([0-9a-f]{8})` \(at `\3`\)",
        historical_source,
    )
    assert historical, "ADR-0139 D2 must pin the historical orchestration source"
    extracted = re.search(
        r"`register_branch_hook` is defined in `(lionagi/cli/_runs\.py):([1-9]\d*)@([0-9a-f]{8})`",
        section,
    )
    assert extracted, "ADR-0139 D2 must pin the extracted hook definition"
    declared_owner = re.fullmatch(r"`(lionhub(?:\.\w+)+)`", owner)
    assert declared_owner, f"ADR-0139 D2 invalid hook owner: {owner}"
    assert extraction.startswith("`register_branch_hook`"), "ADR-0139 D2 must name the shared hook"
    return historical.groups(), extracted.groups(), declared_owner[1]


def test_adr_0139_historical_shared_hook_pins_name_the_definitions():
    root = builder.ROOT
    shallow = subprocess.check_output(
        ["git", "rev-parse", "--is-shallow-repository"], cwd=root, encoding="utf-8"
    ).strip()
    if shallow == "true":
        pytest.skip("historic source pins require full Git history")

    historical, extracted, _ = _adr_0139_step_3_shared_hook(_adr_0139_step_3_section(root))
    for path, line, revision in (historical, extracted):
        pin = f"{path}:{line}@{revision}"
        source = subprocess.check_output(
            ["git", "show", f"{revision}:{path}"], cwd=root, encoding="utf-8"
        )
        assert any(
            isinstance(node, ast.FunctionDef)
            and node.name == "register_branch_hook"
            and node.lineno == int(line)
            for node in ast.parse(source, filename=pin).body
        ), f"{pin} does not define register_branch_hook"


def test_adr_0139_retained_adapter_exports_the_declared_hook():
    root = builder.ROOT
    section = _adr_0139_step_3_section(root)
    historical, extracted, declared_owner = _adr_0139_step_3_shared_hook(section)
    old_adapter = historical[0].removesuffix(".py").replace("/", ".")
    adapter_name = _moved.resolve(old_adapter)
    assert adapter_name is not None, f"ADR-0139 D2 cannot resolve retained adapter {old_adapter}"
    adapter_path = _tracked_lionhub_module(root, _tracked_boundary_paths(root), adapter_name)
    assert adapter_path is not None, f"{adapter_name}: retained adapter is missing or untracked"

    # Import through the adapter so a duplicate definition there cannot masquerade as a re-export.
    adapter = import_module(adapter_name)
    assert Path(adapter.__file__).resolve() == (root / adapter_path).resolve()
    register_branch_hook = adapter.register_branch_hook
    actual_owner = register_branch_hook.__module__
    assert declared_owner == actual_owner, (
        f"ADR-0139 D2 must assign register_branch_hook to {actual_owner}"
    )
    assert register_branch_hook is import_module(declared_owner).register_branch_hook, (
        f"{adapter_name} must re-export the same register_branch_hook object from {declared_owner}"
    )
    source_owner = extracted[0].removesuffix(".py").replace("/", ".")
    assert actual_owner == _adr_0139_step_3_destinations(section)[source_owner], (
        "ADR-0139 D2 must assign register_branch_hook and its enclosing module the same owner"
    )


def test_step_3_table_validates_a_complete_header_and_uses_its_width():
    columns = ("Source", "Destination")
    table = "| Source | Destination |\n| --- | --- |\n| before | after |"
    assert _adr_0139_step_3_table(table, columns) == [["before", "after"]]
    with pytest.raises(AssertionError, match="requires exactly one"):
        _adr_0139_step_3_table(table.replace("Source", "Other"), columns)
    with pytest.raises(AssertionError, match="invalid separator"):
        _adr_0139_step_3_table(table.replace("| --- | --- |", "| --- | --- | --- |"), columns)
    with pytest.raises(AssertionError, match="invalid table row"):
        _adr_0139_step_3_table(table.replace("| before | after |", "| before |"), columns)


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
    # The adopted CLI destination is recorded; the one it superseded is refused even
    # though the record still names it.
    data["modules"]["lionagi.foundation"]["target_owner"] = "lionhub.serve.cli"
    checker.validate_target_notes(root, data, checker.lionhub_targets(root))
    data["modules"]["lionagi.foundation"]["target_owner"] = "lionhub.cli.catalog"
    with pytest.raises(checker.ManifestError, match="superseded CLI destination"):
        checker.validate_target_notes(root, data, checker.lionhub_targets(root))
    # A superseded name is refused even when a package of that name still exists.
    with pytest.raises(checker.ManifestError, match="superseded CLI destination"):
        checker.validate_target_notes(
            root, data, checker.lionhub_targets(root) | {"lionhub.cli.catalog"}
        )
    # The placeholder used before the decision is no longer accepted.
    data["modules"]["lionagi.foundation"]["target_owner"] = (
        "pending the CLI location decision (ADR-0139 D1)"
    )
    with pytest.raises(checker.ManifestError, match="unrecorded destination"):
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
