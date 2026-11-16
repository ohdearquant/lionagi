import pytest

from scripts import check_feature_boundaries as checker


def test_internal_aliases_use_the_literal_move_table(tmp_path):
    move_table = tmp_path / "_moved.py"
    move_table.write_text(
        """
MOVED: tuple[Moved, ...] = (
    Moved("lionagi.moved", "lionhub.package", "1.0", "2.0"),
    Moved("lionagi.moved.shadowed", "lionhub.package.shadowed", "1.0", "2.0"),
    Moved("lionagi.moved.specific", "lionhub.replacement", "1.0", "2.0"),
    Moved("lionagi.moved.stale", "lionhub.absent", "1.0", "2.0"),
    Moved("lionagi.core_alias", "lionagi.physical", "1.0", "2.0"),
)
""",
        encoding="utf-8",
    )
    files = {
        "lionagi": tmp_path / "__init__.py",
        "lionagi._moved": move_table,
        "lionagi.moved.shadowed": tmp_path / "shadowed.py",
        "lionagi.physical": tmp_path / "physical.py",
    }
    targets = {
        "lionhub.package.child",
        "lionhub.package.shadowed",
        "lionhub.package.stale",
        "lionhub.replacement",
    }
    manifest = {
        "modules": {name: {"layer": "sdk", "owner": "Core"} for name in files},
        "external_targets": {"lionhub": {"layer": "root", "owner": "LionHub"}},
        "exceptions": [],
    }
    names = (
        "lionagi.moved.child",
        "lionagi.moved.specific",
        "lionagi.moved.shadowed",
        "lionagi.moved.stale",
        "lionagi.moved_lookalike",
        "lionagi.core_alias",
        "lionhub.package.child",
    )
    edges = [
        {"source": "lionagi", "target": name, "kind": "lazy_export", "line": n}
        for n, name in enumerate(names, 1)
    ]
    edges[-1]["kind"] = "import"

    unresolved, aliases = checker.report_internal_targets(edges, files, targets, manifest)

    assert unresolved == [edges[3], edges[4]]
    assert aliases == [
        {**edges[0], "canonical_target": "lionhub.package.child", "target_owner": "LionHub"},
        {**edges[1], "canonical_target": "lionhub.replacement", "target_owner": "LionHub"},
        {**edges[5], "canonical_target": "lionagi.physical", "target_owner": "Core"},
    ]
    findings = checker.classify([*edges, *aliases], manifest, targets)
    direct, old_path, moved_specific = findings
    assert old_path["legacy_target"] == "lionagi.moved.child"
    for field in ("source", "target", "violation", "target_owner", "exception"):
        assert old_path[field] == direct[field]
    assert moved_specific["target"] == "lionhub.replacement"


def test_alias_to_core_module_without_owner_refuses(tmp_path):
    move_table = tmp_path / "_moved.py"
    move_table.write_text(
        'MOVED = (Moved("lionagi.old", "lionagi.owner_missing", "1.0", "2.0"),)',
        encoding="utf-8",
    )
    files = {
        "lionagi._moved": move_table,
        "lionagi.owner_missing": tmp_path / "owner_missing.py",
    }
    edges = [{"source": "lionagi", "target": "lionagi.old", "kind": "import", "line": 1}]
    manifest = {"modules": {}, "external_targets": {"lionhub": {"owner": "LionHub"}}}

    with pytest.raises(checker.ManifestError, match=r"no manifest owner: lionagi\.owner_missing"):
        checker.report_internal_targets(edges, files, set(), manifest)


def test_only_the_named_root_exports_are_grandfathered(tmp_path):
    root = tmp_path / "__init__.py"
    root.write_text(
        """
import lionagi.lndl
from lionagi import lndl

_LAZY_MAP = {
    "LNDLOutput": ("lndl", "LNDLOutput"),
    "LNDLError": ("lndl", "LNDLError"), "new_export": ("lndl", "LNDLOutput"),
    "MissingLvarError": ("lndl", "MissingLvarError"),
    "MissingLvarError": ("lndl", "MissingLvarError"),
    "MissingFieldError": (".lndl", "MissingFieldError"),
}

class OrdinaryExports:
    _LAZY_MAP = {"TypeMismatchError": ("lndl", "TypeMismatchError")}
""",
        encoding="utf-8",
    )
    moved = tmp_path / "_moved.py"
    moved.write_text(
        'MOVED = (Moved("lionagi.lndl", "lionhub.lndl", "1.0", "2.0"),)',
        encoding="utf-8",
    )
    files = {"lionagi": root, "lionagi._moved": moved}
    targets = {"lionhub.lndl"}
    manifest = {
        "modules": {name: {"layer": "sdk", "owner": "Core"} for name in files},
        "external_targets": {"lionhub": {"layer": "root", "owner": "LionHub"}},
        "exceptions": [],
    }
    edges = checker.scan(files, targets)
    unresolved, aliases = checker.report_internal_targets(edges, files, targets, manifest)
    findings = checker.classify([*edges, *aliases], manifest, targets)

    assert not unresolved
    by_line = {edge["line"]: edge for edge in findings}
    assert len(findings) == len(by_line) == 8
    assert set(by_line) == {2, 3, 6, 7, 8, 9, 10, 14}
    assert all(edge["target"] == "lionhub.lndl" for edge in findings)
    assert [edge["line"] for edge in findings if "disposition" in edge] == [6]
    assert by_line[6]["disposition"] == "grandfathered"
    assert by_line[6]["basis"] == "ADR-0136 Amendment 1 A3"


def test_computed_move_table_stays_unresolved(tmp_path):
    move_table = tmp_path / "_moved.py"
    move_table.write_text(
        """
MOVED = (
    Moved("lionagi.moved", "lionhub.package", "1.0", "2.0"),
    *more_specific_moves(),
)
""",
        encoding="utf-8",
    )

    assert checker.read_moved_aliases(move_table) == {}
