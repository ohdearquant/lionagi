from __future__ import annotations

from importlib.resources import files

from lionhub.agent.casts.catalog import _load_packaged_pack


def test_packaged_default_pack_resource_is_valid():
    resource = files("lionhub.agent.casts").joinpath("packs", "default.yaml")
    assert resource.is_file()

    pack = _load_packaged_pack(raise_on_error=True)
    assert pack is not None
    critic = pack.config("critic")
    assert critic is not None
    assert "adversarial" in critic.default_modes


def test_catalog_uses_the_canonical_resource_root(monkeypatch):
    from importlib import resources

    requested = []
    original_files = resources.files

    def record_root(package):
        requested.append(package)
        return original_files(package)

    monkeypatch.setattr(resources, "files", record_root)
    assert _load_packaged_pack(raise_on_error=True) is not None
    assert requested == ["lionhub.agent.casts"]
