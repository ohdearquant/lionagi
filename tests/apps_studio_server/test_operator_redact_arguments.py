# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Mapping-key behavior of redact_arguments: classification reads the raw key,
display shows the scrubbed key, and scrub collisions never drop entries."""

from __future__ import annotations

from lionagi.studio.operator.redact import redact_arguments


class TestRawKeyClassification:
    """Scrubbing a path-shaped key rewrites it to its leaf; the secrecy
    decision must happen before that rewrite, or a key whose path carries the
    credential marker serves its value in clear under the leaf name."""

    def test_posix_path_key_with_credential_marker_redacts_value(self):
        out = redact_arguments({"/etc/lionagi/api_key/value": "S3cr3tValue"})
        assert "S3cr3tValue" not in repr(out)

    def test_windows_path_key_with_credential_marker_redacts_value(self):
        out = redact_arguments({"C:\\conf\\api_key\\value": "S3cr3tValue"})
        assert "S3cr3tValue" not in repr(out)

    def test_token_path_key_redacts_value(self):
        out = redact_arguments({"/var/secrets/token/current": "S3cr3tValue"})
        assert "S3cr3tValue" not in repr(out)

    def test_plain_credential_key_control(self):
        assert redact_arguments({"api_key": "S3cr3tValue"}) == {"api_key": "[redacted]"}

    def test_container_under_path_shaped_credential_key_is_withheld(self):
        out = redact_arguments({"/etc/lionagi/api_key/value": {"inner": "S3cr3tValue"}})
        assert "S3cr3tValue" not in repr(out)


class TestScrubbedKeyCollision:
    """scrub_text is not injective: distinct absolute paths sharing a leaf
    collapse to one display key. The projection must keep every entry — a run
    manifest is path-keyed by construction, and a silently dropped artifact
    has no truncation marker to say it existed."""

    def test_colliding_leaves_keep_both_entries(self):
        out = redact_arguments({"/srv/a/config.json": "alpha", "/srv/b/config.json": "beta"})
        assert len(out) == 2
        assert sorted(str(v) for v in out.values()) == ["alpha", "beta"]

    def test_three_way_collision_keeps_all_entries(self):
        out = redact_arguments({"/a/x.json": "one", "/b/x.json": "two", "/c/x.json": "three"})
        assert len(out) == 3
