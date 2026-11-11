from __future__ import annotations

from lionhub.agent.casts import catalog


def test_catalog_uses_the_canonical_resource_root(monkeypatch):
    requested = []
    original_files = catalog.files

    def record_root(package):
        requested.append(package)
        return original_files(package)

    monkeypatch.setattr(catalog, "files", record_root)
    assert catalog._load_packaged_pack(raise_on_error=True) is not None
    assert requested == ["lionhub.agent.casts"]
