# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li agent --image <path>`: file -> base64 data-URI content part.

Covers the pure reader (`_load_image_data_uris`) and its CLI wiring in
`run_agent` — must fail fast (before any LLM call) on a missing path, a
non-file path, or an unrecognized extension, and must forward the resulting
data URIs to `_run_agent` as the `images=` kwarg unchanged.
"""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import patch

import pytest

from lionagi.cli.agent import _load_image_data_uris

# A minimal-but-valid 1x1 PNG (the actual bytes don't matter — only that they
# round-trip through base64 unchanged).
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "0005579f550000000049454e44ae426082"
)


class TestLoadImageDataUris:
    def test_valid_png_becomes_data_uri(self, tmp_path):
        f = tmp_path / "shot.png"
        f.write_bytes(_PNG_BYTES)

        uris = _load_image_data_uris([str(f)])

        assert len(uris) == 1
        expected_b64 = base64.b64encode(_PNG_BYTES).decode("ascii")
        assert uris[0] == f"data:image/png;base64,{expected_b64}"

    def test_jpeg_and_jpg_both_map_to_image_jpeg(self, tmp_path):
        f1 = tmp_path / "a.jpg"
        f2 = tmp_path / "b.jpeg"
        f1.write_bytes(b"fake-jpeg-bytes")
        f2.write_bytes(b"fake-jpeg-bytes")

        uris = _load_image_data_uris([str(f1), str(f2)])

        assert uris[0].startswith("data:image/jpeg;base64,")
        assert uris[1].startswith("data:image/jpeg;base64,")

    def test_webp_and_gif_supported(self, tmp_path):
        f1 = tmp_path / "a.webp"
        f2 = tmp_path / "b.gif"
        f1.write_bytes(b"x")
        f2.write_bytes(b"x")

        uris = _load_image_data_uris([str(f1), str(f2)])

        assert uris[0].startswith("data:image/webp;base64,")
        assert uris[1].startswith("data:image/gif;base64,")

    def test_multiple_paths_preserve_order(self, tmp_path):
        f1 = tmp_path / "one.png"
        f2 = tmp_path / "two.png"
        f1.write_bytes(b"one")
        f2.write_bytes(b"two")

        uris = _load_image_data_uris([str(f1), str(f2)])

        assert uris[0] == f"data:image/png;base64,{base64.b64encode(b'one').decode('ascii')}"
        assert uris[1] == f"data:image/png;base64,{base64.b64encode(b'two').decode('ascii')}"

    def test_missing_file_raises_naming_path(self, tmp_path):
        bad = str(tmp_path / "does-not-exist.png")
        with pytest.raises(FileNotFoundError) as exc_info:
            _load_image_data_uris([bad])
        assert bad in str(exc_info.value)

    def test_directory_path_raises_not_a_file(self, tmp_path):
        d = tmp_path / "a-directory.png"
        d.mkdir()
        with pytest.raises(ValueError) as exc_info:
            _load_image_data_uris([str(d)])
        assert str(d) in str(exc_info.value)
        assert "not a regular file" in str(exc_info.value)

    def test_unknown_extension_raises_loud_not_silent(self, tmp_path):
        f = tmp_path / "screenshot.bmp"
        f.write_bytes(b"x")
        with pytest.raises(ValueError) as exc_info:
            _load_image_data_uris([str(f)])
        msg = str(exc_info.value)
        assert str(f) in msg
        assert ".bmp" in msg
        # Names the supported extensions rather than guessing a media type.
        assert "png" in msg and "jpeg" in msg

    def test_no_extension_raises(self, tmp_path):
        f = tmp_path / "screenshot"
        f.write_bytes(b"x")
        with pytest.raises(ValueError):
            _load_image_data_uris([str(f)])


# ---------------------------------------------------------------------------
# CLI wiring: --image -> run_agent -> _run_agent(images=[...])
# ---------------------------------------------------------------------------

_CAPTURED: dict[str, Any] = {}


async def _fake_run_agent(
    model_str: str | None,
    prompt: str,
    **kwargs: Any,
) -> tuple[str, str, str, str, str | None]:
    _CAPTURED["agent"] = {"model_str": model_str, "prompt": prompt, **kwargs}
    return "output", "provider", "branch-id", "completed", "sess-001"


def _run(argv: list[str]) -> int:
    import lionagi.cli.agent as agent_mod
    from lionagi.cli.main import main

    _CAPTURED.clear()
    with patch.object(agent_mod, "_run_agent", _fake_run_agent):
        return main(argv)


class TestImageCliWiring:
    def test_single_image_forwarded_as_data_uri(self, tmp_path):
        f = tmp_path / "shot.png"
        f.write_bytes(_PNG_BYTES)

        rc = _run(["agent", "codex/gpt-5.5", "--image", str(f), "what is in this image?"])

        assert rc == 0
        c = _CAPTURED["agent"]
        assert c["prompt"] == "what is in this image?"
        assert c["images"] == [f"data:image/png;base64,{base64.b64encode(_PNG_BYTES).decode()}"]

    def test_repeated_image_flag_collects_all_paths_in_order(self, tmp_path):
        f1 = tmp_path / "a.png"
        f2 = tmp_path / "b.jpg"
        f1.write_bytes(b"one")
        f2.write_bytes(b"two")

        rc = _run(
            ["agent", "codex/gpt-5.5", "--image", str(f1), "--image", str(f2), "compare these"]
        )

        assert rc == 0
        c = _CAPTURED["agent"]
        assert len(c["images"]) == 2
        assert c["images"][0].startswith("data:image/png;base64,")
        assert c["images"][1].startswith("data:image/jpeg;base64,")

    def test_no_image_flag_forwards_none(self):
        rc = _run(["agent", "codex/gpt-5.5", "no images here"])
        assert rc == 0
        assert _CAPTURED["agent"]["images"] is None

    def test_missing_image_path_fails_before_run_agent(self, tmp_path, monkeypatch):
        import lionagi.cli.agent as agent_mod

        errors: list[str] = []
        monkeypatch.setattr(agent_mod, "log_error", lambda msg: errors.append(msg))

        def _boom(*a, **kw):
            raise AssertionError(
                "_run_agent must not be reached — --image validation must fire first"
            )

        monkeypatch.setattr(agent_mod, "_run_agent", _boom)

        bad = str(tmp_path / "nope.png")
        from lionagi.cli.main import main

        rc = main(["agent", "codex/gpt-5.5", "--image", bad, "prompt"])

        assert rc == 1
        assert errors
        assert bad in errors[0]

    def test_unknown_extension_fails_before_run_agent(self, tmp_path, monkeypatch):
        import lionagi.cli.agent as agent_mod

        errors: list[str] = []
        monkeypatch.setattr(agent_mod, "log_error", lambda msg: errors.append(msg))

        def _boom(*a, **kw):
            raise AssertionError(
                "_run_agent must not be reached — --image validation must fire first"
            )

        monkeypatch.setattr(agent_mod, "_run_agent", _boom)

        f = tmp_path / "screenshot.tiff"
        f.write_bytes(b"x")
        from lionagi.cli.main import main

        rc = main(["agent", "codex/gpt-5.5", "--image", str(f), "prompt"])

        assert rc == 1
        assert errors
        assert str(f) in errors[0]
