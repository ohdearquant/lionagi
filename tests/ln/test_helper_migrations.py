"""Representative callers retain their old serialized bytes and type results."""

import hashlib
import importlib
import json
from typing import Annotated, Optional

import pytest
from pydantic import BaseModel


def test_string_digest_families_keep_serialization_and_chain_format():
    from lionagi.hooks.external import compute_command_hash
    from lionagi.state._mirror_common import canonical_json, content_sha256
    from lionagi.studio.operator.store import OperatorStore
    from lionagi.studio.services.approvals import (
        _compute_chain_hash,
        _compute_content_hash,
        compute_params_hash,
    )

    value = {"z": "café 🦁", "a": [1, True, None]}
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    expected = hashlib.sha256(canonical.encode()).hexdigest()
    assert compute_params_hash(value) == expected
    assert _compute_content_hash(value) == expected
    unicode_json = '{"a":[1,true,null],"z":"café 🦁"}'
    unicode_digest = hashlib.sha256(unicode_json.encode("utf-8")).hexdigest()
    assert canonical_json(value) == unicode_json
    assert OperatorStore._json(value) == unicode_json
    assert content_sha256(value) == unicode_digest
    assert OperatorStore.canonical_hash(value) == unicode_digest
    assert unicode_digest != expected
    assert (
        _compute_chain_hash(expected, "0" * 64)
        == hashlib.sha256((expected + "0" * 64).encode()).hexdigest()
    )
    command = ["echo", "café"]
    assert compute_command_hash(command) == hashlib.sha256(json.dumps(command).encode()).hexdigest()


def test_digest_refs_keep_prefix_and_truncation():
    from lionhub.orchestration.engines.review import _clean_ref

    dimensions = ("security", "correctness")
    key = "verify-clean:" + ",".join(sorted(dimensions))
    assert _clean_ref(dimensions) == "V-" + hashlib.sha256(key.encode()).hexdigest()[:8]


def test_uncapped_callers_keep_large_string_and_byte_inputs():
    from lionagi.hooks.external import compute_command_hash
    from lionagi.ln import MAX_HASH_INPUT_BYTES
    from lionagi.plugins.trust import _sha256_bytes

    payload = b"x" * (MAX_HASH_INPUT_BYTES + 1)
    assert _sha256_bytes(payload) == hashlib.sha256(payload).hexdigest()
    command = ["echo", payload.decode()]
    assert compute_command_hash(command) == hashlib.sha256(json.dumps(command).encode()).hexdigest()


@pytest.mark.parametrize("payload", [b"", b"binary\x00\xff\n"])
def test_file_and_byte_digest_families_match_old_hashlib(payload, tmp_path):
    from lionagi.hooks.external import _hash_fd, compute_executable_digest
    from lionagi.plugins.discovery import DiscoveredPlugin
    from lionagi.plugins.manifest import PluginManifest
    from lionagi.plugins.trust import _sha256_bytes, _sha256_file, compute_manifest_hash

    path = tmp_path / "content.bin"
    path.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    assert _sha256_bytes(payload) == expected
    assert _sha256_file(path) == expected
    assert compute_executable_digest(path) == expected
    with path.open("rb") as stream:
        assert _hash_fd(stream.fileno()) == expected
        assert stream.read() == payload
    assert _sha256_file(tmp_path / "missing") is None
    manifest = PluginManifest(name="example", description="café", version="1", lionagi=">=0")
    discovered = DiscoveredPlugin("example", tmp_path, tmp_path / "plugin.yaml", manifest)
    assert (
        compute_manifest_hash(discovered)
        == hashlib.sha256(
            json.dumps(
                manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )


def test_provider_digest_family_keeps_blake2b_for_manifest_and_files(tmp_path, monkeypatch):
    from lionagi.plugins.discovery import DiscoveredPlugin
    from lionagi.plugins.manifest import PluginManifest
    from lionagi.plugins.registry import PluginRegistry
    from lionagi.service.connections.registry import EndpointRegistry

    manifest_bytes = b"name: example\nversion: '1'\n"
    source_bytes = b"# provider content\n"
    manifest_path = tmp_path / "plugin.yaml"
    manifest_path.write_bytes(manifest_bytes)
    (tmp_path / "provider.py").write_bytes(source_bytes)
    manifest = PluginManifest(
        name="example",
        version="1",
        lionagi=">=0",
        capabilities={"providers": [{"module": "provider.py"}]},
    )
    record = DiscoveredPlugin("example", tmp_path, manifest_path, manifest)
    monkeypatch.setattr(PluginRegistry, "get", classmethod(lambda cls, name: record))
    assert EndpointRegistry._plugin_entry_digest("example", "provider.py:Endpoint") == (
        hashlib.blake2b(manifest_bytes).hexdigest(),
        (("provider.py", hashlib.blake2b(source_bytes).hexdigest()),),
    )


def test_brief_digest_family_keeps_blake2b_snapshot(tmp_path):
    from lionagi.cli.orchestrate._manifest import _BriefCache, _validate_brief

    payload = "A café brief\n".encode()
    path = tmp_path / "brief.md"
    path.write_bytes(payload)
    assert _validate_brief(str(path), "task", _BriefCache()) == (
        path.resolve(),
        payload,
        hashlib.blake2b(payload).hexdigest(),
    )


def test_pydantic_class_callers_keep_classes_instances_and_rejections():
    from lionagi.libs.validate.common_field_validators import validate_model_to_type
    from lionagi.models._build_model import build_model_type
    from lionagi.operations.select.utils import parse_selection

    class Choice(BaseModel):
        answer: int = 3

    assert validate_model_to_type(None, Choice) is Choice
    assert validate_model_to_type(None, Choice()) is Choice
    with pytest.raises(ValueError, match="BaseModel subclass or instance"):
        validate_model_to_type(None, {"answer": 3})
    assert issubclass(build_model_type(base_type=Choice), Choice)
    assert parse_selection("Choice", [Choice]) == "Choice"


@pytest.mark.parametrize(
    "annotation,expected",
    [
        (int, int),
        (Optional[int], int),
        (str | None, str),
        (int | str | None, None),
        (list[int], None),
        (Annotated[Optional[int], "metadata"], None),
    ],
)
def test_optional_callers_keep_scalar_domain(annotation, expected):
    from lionhub.lndl.types import _unwrap_scalar

    assert _unwrap_scalar(annotation) is expected


def test_optional_rendering_keeps_duck_typed_model_classes():
    middle = importlib.import_module("lionhub.lndl.middle.lndl_middle")

    class ModelFieldsOnly:
        model_fields = {"answer": object()}

    assert middle._render_type(ModelFieldsOnly | None) == "ModelFieldsOnly: answer"
    assert middle._render_type(list[int | None]) == "list[int]"


@pytest.mark.parametrize(
    "name,module",
    [
        ("CompatibilityProfile", "compat"),
        ("StateCompatibilityRefused", "compat"),
        ("ForeignLineageError", "db"),
        ("SchemaTooNewError", "db"),
        ("StateDB", "db"),
    ],
)
def test_state_lazy_exports_keep_identity_and_cache(name, module, monkeypatch):
    package = importlib.import_module("lionagi.state")
    expected = getattr(importlib.import_module("lionagi.state." + module), name)
    monkeypatch.setattr(package, name, None, raising=False)
    monkeypatch.delattr(package, name)
    assert package.__getattr__(name) is expected
    assert vars(package)[name] is expected


def test_cli_lazy_export_keeps_callable_over_submodule(monkeypatch):
    package = importlib.import_module("lionagi.cli")
    expected = importlib.import_module("lionagi.cli.main").main
    monkeypatch.setattr(package, "main", None, raising=False)
    monkeypatch.delattr(package, "main")
    assert package.__getattr__("main") is expected
    assert callable(package.main)


@pytest.mark.parametrize("module", ["lionagi.cli", "lionagi.state"])
@pytest.mark.parametrize("name", ["missing", "missing'attribute\n"])
def test_lazy_unknown_attributes_keep_original_exception_text(module, name):
    package = importlib.import_module(module)
    with pytest.raises(AttributeError) as caught:
        package.__getattr__(name)
    assert str(caught.value) == f"module {module!r} has no attribute {name!r}"


def test_optional_dependency_probe_matches_find_spec():
    from lionagi.ln import is_import_installed

    for name in ("json", "lionagi", "lionagi_nonexistent_dependency_3583"):
        assert is_import_installed(name) == (importlib.util.find_spec(name) is not None)
