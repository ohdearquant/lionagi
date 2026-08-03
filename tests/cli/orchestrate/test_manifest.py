# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Manifest schema v1: closed-schema validation and snapshot semantics."""

from __future__ import annotations

import json
from hashlib import blake2b

import pytest
import yaml

from lionagi.cli.orchestrate._manifest import (
    MAX_LEGS,
    MAX_TIMEOUT_SECONDS,
    ManifestError,
    load_manifest,
)


def _write(path, content: str):
    path.write_text(content)
    return path


def _brief(tmp_path, name="brief.md", content="Review module A.\n"):
    return _write(tmp_path / name, content)


def _minimal_manifest_dict(tmp_path, *, brief=None, cwd=None, label="leg-a"):
    brief = brief or _brief(tmp_path)
    cwd = cwd or (tmp_path / "work")
    cwd.mkdir(exist_ok=True)
    return {
        "manifest_version": 1,
        "legs": [
            {"brief": str(brief), "cwd": str(cwd), "label": label},
        ],
    }


def _dump_yaml(tmp_path, data, name="manifest.yaml"):
    return _write(tmp_path / name, yaml.safe_dump(data))


def _dump_json(tmp_path, data, name="manifest.json"):
    return _write(tmp_path / name, json.dumps(data))


# ── success cases ───────────────────────────────────────────────────────


def test_minimal_manifest_loads(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    manifest_path = _dump_yaml(tmp_path, data)

    manifest = load_manifest(manifest_path)

    assert manifest.manifest_version == 1
    assert len(manifest.legs) == 1
    leg = manifest.legs[0]
    assert leg.label == "leg-a"
    assert leg.model is None
    assert leg.agent is None
    assert leg.timeout is None
    assert leg.brief_bytes == (tmp_path / "brief.md").read_bytes()
    assert leg.brief_hash == blake2b(leg.brief_bytes).hexdigest()


def test_full_manifest_with_per_leg_overrides(tmp_path):
    (tmp_path / "work-a").mkdir()
    (tmp_path / "work-b").mkdir()
    brief_a = _brief(tmp_path, "a.md", "Brief A.\n")
    brief_b = _brief(tmp_path, "b.md", "Brief B.\n")
    data = {
        "manifest_version": 1,
        "defaults": {"agent": "reviewer", "timeout": 1200},
        "legs": [
            {
                "brief": str(brief_a),
                "cwd": str(tmp_path / "work-a"),
                "label": "leg-a",
            },
            {
                "brief": str(brief_b),
                "cwd": str(tmp_path / "work-b"),
                "label": "leg-b",
                "model": "codex/gpt-5",
                "timeout": 300,
            },
        ],
    }
    manifest_path = _dump_yaml(tmp_path, data)

    manifest = load_manifest(manifest_path)

    assert manifest.default_agent == "reviewer"
    assert manifest.default_timeout == 1200
    leg_a, leg_b = manifest.legs
    assert leg_a.agent == "reviewer" and leg_a.model is None
    assert leg_a.timeout == 1200
    assert leg_b.model == "codex/gpt-5" and leg_b.agent is None
    assert leg_b.timeout == 300


def test_yaml_and_json_parity(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    yaml_path = _dump_yaml(tmp_path, data, "manifest.yaml")
    json_path = _dump_json(tmp_path, data, "manifest.json")

    from_yaml = load_manifest(yaml_path)
    from_json = load_manifest(json_path)

    assert from_yaml == from_json


# ── snapshot semantics ──────────────────────────────────────────────────


def test_brief_edit_after_load_has_no_effect(tmp_path):
    brief = _brief(tmp_path, content="original content\n")
    data = _minimal_manifest_dict(tmp_path, brief=brief)
    manifest_path = _dump_yaml(tmp_path, data)

    manifest = load_manifest(manifest_path)
    original_bytes = manifest.legs[0].brief_bytes
    original_hash = manifest.legs[0].brief_hash

    brief.write_text("mutated after load\n")

    assert manifest.legs[0].brief_bytes == original_bytes
    assert manifest.legs[0].brief_hash == original_hash
    assert manifest.legs[0].brief_bytes != brief.read_bytes()


def test_manifest_edit_after_load_has_no_effect(tmp_path):
    data = _minimal_manifest_dict(tmp_path, label="leg-a")
    manifest_path = _dump_yaml(tmp_path, data)

    manifest = load_manifest(manifest_path)

    other_brief = _brief(tmp_path, "other.md", "other\n")
    (tmp_path / "work2").mkdir()
    mutated = _minimal_manifest_dict(
        tmp_path, brief=other_brief, cwd=tmp_path / "work2", label="leg-b"
    )
    _dump_yaml(tmp_path, mutated, manifest_path.name)

    assert manifest.legs[0].label == "leg-a"


# ── top-level schema ────────────────────────────────────────────────────


def test_manifest_path_must_be_absolute(tmp_path):
    with pytest.raises(ManifestError, match="must be absolute"):
        load_manifest("relative/manifest.yaml")


def test_manifest_file_must_exist(tmp_path):
    with pytest.raises(ManifestError, match="does not exist"):
        load_manifest(tmp_path / "missing.yaml")


def test_manifest_must_be_a_mapping(tmp_path):
    manifest_path = _write(tmp_path / "manifest.yaml", "- just\n- a\n- list\n")
    with pytest.raises(ManifestError, match="must be a mapping"):
        load_manifest(manifest_path)


def test_manifest_rejects_invalid_yaml(tmp_path):
    manifest_path = _write(tmp_path / "manifest.yaml", "legs: [unclosed\n")
    with pytest.raises(ManifestError, match="not valid YAML"):
        load_manifest(manifest_path)


def test_manifest_rejects_invalid_json(tmp_path):
    manifest_path = _write(tmp_path / "manifest.json", "{not json")
    with pytest.raises(ManifestError, match="not valid JSON"):
        load_manifest(manifest_path)


def test_manifest_rejects_unknown_top_level_key(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["extra_knob"] = True
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="unknown key.*extra_knob"):
        load_manifest(manifest_path)


def test_manifest_version_must_be_exactly_one(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["manifest_version"] = 2
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="manifest_version must be exactly 1"):
        load_manifest(manifest_path)


def test_manifest_version_true_is_not_one(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["manifest_version"] = True
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="manifest_version must be exactly 1"):
        load_manifest(manifest_path)


def test_manifest_version_missing(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    del data["manifest_version"]
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="manifest_version must be exactly 1"):
        load_manifest(manifest_path)


def test_legs_must_be_a_list(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["legs"] = "not-a-list"
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="legs must be a list"):
        load_manifest(manifest_path)


def test_legs_below_floor_refused(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["legs"] = []
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match=r"legs must contain 1\.\.64 entries, got 0"):
        load_manifest(manifest_path)


def test_legs_above_ceiling_refused(tmp_path):
    brief = _brief(tmp_path)
    (tmp_path / "work").mkdir()
    leg_template = {
        "brief": str(brief),
        "cwd": str(tmp_path / "work"),
    }
    legs = [{**leg_template, "label": f"leg-{i}"} for i in range(MAX_LEGS + 1)]
    data = {"manifest_version": 1, "legs": legs}
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match=rf"got {MAX_LEGS + 1}"):
        load_manifest(manifest_path)


def test_legs_at_ceiling_accepted(tmp_path):
    brief = _brief(tmp_path)
    (tmp_path / "work").mkdir()
    legs = [
        {"brief": str(brief), "cwd": str(tmp_path / "work"), "label": f"leg-{i}"}
        for i in range(MAX_LEGS)
    ]
    data = {"manifest_version": 1, "legs": legs}
    manifest_path = _dump_yaml(tmp_path, data)
    manifest = load_manifest(manifest_path)
    assert len(manifest.legs) == MAX_LEGS


# ── defaults ─────────────────────────────────────────────────────────────


def test_defaults_rejects_unknown_key(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["defaults"] = {"nonsense": 1}
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="defaults has unknown key.*nonsense"):
        load_manifest(manifest_path)


def test_defaults_rejects_model_and_agent_together(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["defaults"] = {"model": "openai/gpt-5", "agent": "reviewer"}
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="defaults names both model and agent"):
        load_manifest(manifest_path)


# ── leg schema ──────────────────────────────────────────────────────────


def test_leg_rejects_unknown_key(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["legs"][0]["nonsense"] = 1
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match=r"legs\[0\] has unknown key.*nonsense"):
        load_manifest(manifest_path)


@pytest.mark.parametrize("missing", ["brief", "cwd", "label"])
def test_leg_missing_required_key(tmp_path, missing):
    data = _minimal_manifest_dict(tmp_path)
    del data["legs"][0][missing]
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match=f"missing required key {missing!r}"):
        load_manifest(manifest_path)


def test_leg_rejects_model_and_agent_together(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["legs"][0]["model"] = "openai/gpt-5"
    data["legs"][0]["agent"] = "reviewer"
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="names both model and agent"):
        load_manifest(manifest_path)


def test_leg_model_ignores_default_agent_entirely(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["defaults"] = {"agent": "reviewer"}
    data["legs"][0]["model"] = "openai/gpt-5"
    manifest_path = _dump_yaml(tmp_path, data)
    manifest = load_manifest(manifest_path)
    leg = manifest.legs[0]
    assert leg.model == "openai/gpt-5"
    assert leg.agent is None


def test_leg_agent_ignores_default_model_entirely(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["defaults"] = {"model": "openai/gpt-5"}
    data["legs"][0]["agent"] = "reviewer"
    manifest_path = _dump_yaml(tmp_path, data)
    manifest = load_manifest(manifest_path)
    leg = manifest.legs[0]
    assert leg.agent == "reviewer"
    assert leg.model is None


def test_leg_with_neither_inherits_full_default_pair(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["defaults"] = {"model": "openai/gpt-5"}
    manifest_path = _dump_yaml(tmp_path, data)
    manifest = load_manifest(manifest_path)
    leg = manifest.legs[0]
    assert leg.model == "openai/gpt-5"
    assert leg.agent is None


def test_leg_timeout_overrides_default_timeout(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["defaults"] = {"timeout": 1200}
    data["legs"][0]["timeout"] = 60
    manifest_path = _dump_yaml(tmp_path, data)
    manifest = load_manifest(manifest_path)
    assert manifest.legs[0].timeout == 60


@pytest.mark.parametrize("bad_timeout", [0, -1, 86401, 900.5, "900", True])
def test_timeout_rejects_out_of_range_or_wrong_type(tmp_path, bad_timeout):
    data = _minimal_manifest_dict(tmp_path)
    data["legs"][0]["timeout"] = bad_timeout
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="timeout"):
        load_manifest(manifest_path)


def test_timeout_at_ceiling_accepted(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["legs"][0]["timeout"] = MAX_TIMEOUT_SECONDS
    manifest_path = _dump_yaml(tmp_path, data)
    manifest = load_manifest(manifest_path)
    assert manifest.legs[0].timeout == MAX_TIMEOUT_SECONDS


# ── label ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "label",
    [
        "../escape",
        "a/b",
        "/abs/path",
        "..",
        ".",
        "",
        "-leading-dash",
        "a" * 65,
    ],
)
def test_hostile_labels_refused(tmp_path, label):
    data = _minimal_manifest_dict(tmp_path, label=label)
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="label"):
        load_manifest(manifest_path)


def test_label_at_max_length_accepted(tmp_path):
    label = "a" * 64
    data = _minimal_manifest_dict(tmp_path, label=label)
    manifest_path = _dump_yaml(tmp_path, data)
    manifest = load_manifest(manifest_path)
    assert manifest.legs[0].label == label


def test_label_is_lowercased(tmp_path):
    data = _minimal_manifest_dict(tmp_path, label="LEG-A")
    manifest_path = _dump_yaml(tmp_path, data)
    manifest = load_manifest(manifest_path)
    assert manifest.legs[0].label == "leg-a"


def test_label_collision_after_lowercasing_refused(tmp_path):
    brief = _brief(tmp_path)
    (tmp_path / "work-a").mkdir()
    (tmp_path / "work-b").mkdir()
    data = {
        "manifest_version": 1,
        "legs": [
            {"brief": str(brief), "cwd": str(tmp_path / "work-a"), "label": "A-x"},
            {"brief": str(brief), "cwd": str(tmp_path / "work-b"), "label": "a-x"},
        ],
    }
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="collides with legs\\[0\\]"):
        load_manifest(manifest_path)


# ── brief ───────────────────────────────────────────────────────────────


def test_brief_must_be_absolute(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["legs"][0]["brief"] = "relative/brief.md"
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="brief must be an absolute path"):
        load_manifest(manifest_path)


def test_brief_must_exist(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["legs"][0]["brief"] = str(tmp_path / "does-not-exist.md")
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="does not exist or is not a regular file"):
        load_manifest(manifest_path)


def test_brief_must_be_a_regular_file_not_a_directory(tmp_path):
    brief_dir = tmp_path / "brief-as-dir"
    brief_dir.mkdir()
    data = _minimal_manifest_dict(tmp_path)
    data["legs"][0]["brief"] = str(brief_dir)
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="does not exist or is not a regular file"):
        load_manifest(manifest_path)


def test_brief_empty_after_strip_refused(tmp_path):
    brief = _brief(tmp_path, content="   \n\t\n")
    data = _minimal_manifest_dict(tmp_path, brief=brief)
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="brief is empty"):
        load_manifest(manifest_path)


def test_brief_symlink_is_resolved(tmp_path):
    real = _brief(tmp_path, "real.md", "real content\n")
    link = tmp_path / "link.md"
    link.symlink_to(real)
    data = _minimal_manifest_dict(tmp_path, brief=link)
    manifest_path = _dump_yaml(tmp_path, data)
    manifest = load_manifest(manifest_path)
    assert manifest.legs[0].brief == real.resolve()
    assert manifest.legs[0].brief_bytes == b"real content\n"


# ── cwd ─────────────────────────────────────────────────────────────────


def test_cwd_must_be_absolute(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["legs"][0]["cwd"] = "relative/dir"
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="cwd must be an absolute path"):
        load_manifest(manifest_path)


def test_cwd_must_exist(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["legs"][0]["cwd"] = str(tmp_path / "no-such-dir")
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="does not exist or is not a directory"):
        load_manifest(manifest_path)


def test_cwd_must_be_a_directory_not_a_file(tmp_path):
    cwd_file = tmp_path / "cwd-as-file"
    cwd_file.write_text("not a directory\n")
    data = _minimal_manifest_dict(tmp_path)
    data["legs"][0]["cwd"] = str(cwd_file)
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="does not exist or is not a directory"):
        load_manifest(manifest_path)


# ── env ─────────────────────────────────────────────────────────────────


def test_env_map_loads_as_sorted_pairs_with_verbatim_values(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["legs"][0]["env"] = {
        "ZED_VAR": "z value",
        "CARGO_TARGET_DIR": "/abs/targets/module-a",
    }
    manifest_path = _dump_yaml(tmp_path, data)

    leg = load_manifest(manifest_path).legs[0]

    assert leg.env == (
        ("CARGO_TARGET_DIR", "/abs/targets/module-a"),
        ("ZED_VAR", "z value"),
    )
    assert leg.env_keys == ("CARGO_TARGET_DIR", "ZED_VAR")


def test_env_omitted_is_empty_tuple(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    manifest_path = _dump_yaml(tmp_path, data)

    leg = load_manifest(manifest_path).legs[0]

    assert leg.env == ()
    assert leg.env_keys == ()


def test_env_must_be_a_mapping(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["legs"][0]["env"] = ["CARGO_TARGET_DIR=/abs"]
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="env must be a mapping"):
        load_manifest(manifest_path)


@pytest.mark.parametrize(
    "bad_key",
    ["lower_case", "1LEADING_DIGIT", "_LEADING_UNDERSCORE", "HAS-DASH", "X" * 65, ""],
)
def test_env_key_pattern_refused_by_name(tmp_path, bad_key):
    data = _minimal_manifest_dict(tmp_path)
    data["legs"][0]["env"] = {bad_key: "value"}
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="env key"):
        load_manifest(manifest_path)


def test_env_key_at_max_length_is_accepted(tmp_path):
    key = "K" + "X" * 63
    data = _minimal_manifest_dict(tmp_path)
    data["legs"][0]["env"] = {key: "value"}
    manifest_path = _dump_yaml(tmp_path, data)

    assert load_manifest(manifest_path).legs[0].env == ((key, "value"),)


def test_env_reserved_leg_artifacts_key_is_refused(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["legs"][0]["env"] = {"LIONAGI_LEG_ARTIFACTS": "/abs/elsewhere"}
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="reserved key 'LIONAGI_LEG_ARTIFACTS'"):
        load_manifest(manifest_path)


@pytest.mark.parametrize("bad_value", [8080, True, None, ["a"], {"nested": "no"}])
def test_env_values_must_be_strings(tmp_path, bad_value):
    data = _minimal_manifest_dict(tmp_path)
    data["legs"][0]["env"] = {"PORT_HINT": bad_value}
    manifest_path = _dump_json(tmp_path, data)
    with pytest.raises(ManifestError, match="must be a string value"):
        load_manifest(manifest_path)


def test_env_in_defaults_is_refused_as_unknown_key(tmp_path):
    data = _minimal_manifest_dict(tmp_path)
    data["defaults"] = {"env": {"CARGO_TARGET_DIR": "/abs"}}
    manifest_path = _dump_yaml(tmp_path, data)
    with pytest.raises(ManifestError, match="defaults has unknown key"):
        load_manifest(manifest_path)
