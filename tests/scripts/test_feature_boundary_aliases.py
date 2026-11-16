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
    assert [finding["target"] for finding in checker.classify(edges, manifest, targets)] == [
        "lionhub.package.child"
    ]


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
