# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Studio Docker symlink mount guard: symlinks outside allowed roots must never appear in docker run argv."""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Unit tests for the pure helper functions
# ---------------------------------------------------------------------------


class TestIsMountAllowed:
    """Direct tests for _is_mount_allowed."""

    def test_path_inside_allowed_root_is_permitted(self, tmp_path):
        from lionagi.cli.studio import _is_mount_allowed

        root = tmp_path / "safe"
        root.mkdir()
        target = root / "subdir" / "file.yaml"
        assert _is_mount_allowed(target, [root]) is True

    def test_path_at_allowed_root_itself_is_permitted(self, tmp_path):
        from lionagi.cli.studio import _is_mount_allowed

        root = tmp_path / "safe"
        root.mkdir()
        assert _is_mount_allowed(root, [root]) is True

    def test_path_outside_allowed_root_is_rejected(self, tmp_path):
        from lionagi.cli.studio import _is_mount_allowed

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "other" / "secret"
        assert _is_mount_allowed(outside, [allowed]) is False

    def test_root_prefix_does_not_match_sibling_dir(self, tmp_path):
        """Allowlist /home/user must not match /home/username."""
        from lionagi.cli.studio import _is_mount_allowed

        user_root = tmp_path / "user"
        user_root.mkdir()
        username_root = tmp_path / "username"
        username_root.mkdir()
        target = username_root / "secrets"
        # /tmp/.../user should not grant access to /tmp/.../username/secrets
        assert _is_mount_allowed(target, [user_root]) is False

    def test_empty_allowed_roots_rejects_everything(self, tmp_path):
        from lionagi.cli.studio import _is_mount_allowed

        assert _is_mount_allowed(tmp_path / "anything", []) is False

    def test_second_allowed_root_permits_path(self, tmp_path):
        """A path may be permitted by any root in the list."""
        from lionagi.cli.studio import _is_mount_allowed

        root_a = tmp_path / "a"
        root_a.mkdir()
        root_b = tmp_path / "b"
        root_b.mkdir()
        target = root_b / "data"
        assert _is_mount_allowed(target, [root_a, root_b]) is True

    def test_system_path_outside_home_is_rejected(self):
        """/etc/passwd resolves outside a user home — must be rejected."""
        from lionagi.cli.studio import _is_mount_allowed

        home = Path.home().resolve()
        assert _is_mount_allowed(Path("/etc/passwd"), [home]) is False

    def test_proc_path_is_rejected(self):
        """/proc is outside the user home — must be rejected."""
        from lionagi.cli.studio import _is_mount_allowed

        home = Path.home().resolve()
        assert _is_mount_allowed(Path("/proc/1/mem"), [home]) is False


# ---------------------------------------------------------------------------
# Attack scenario: symlink escape is rejected before docker argv is built
# ---------------------------------------------------------------------------


class TestSymlinkEscapeIsRejected:
    def _build_docker_cmd(self, tmp_path, monkeypatch, symlink_target: Path) -> list[str]:
        """Set up a fake ~/.lionagi with a single evil symlink and capture docker run argv."""
        from lionagi.cli._logging import configure_cli_logging

        configure_cli_logging(verbose=False)

        import lionagi.cli.studio as studio_mod

        lionagi_home = tmp_path / ".lionagi"
        agents_dir = lionagi_home / "agents"
        agents_dir.mkdir(parents=True)

        # Create a symlink named "evil" pointing at the supplied target.
        link = agents_dir / "evil"
        link.symlink_to(symlink_target)

        captured: list[list[str]] = []

        def fake_subprocess_run(cmd, **kwargs):
            captured.append(list(cmd))

            class R:
                returncode = 0
                stderr = b""

            return R()

        monkeypatch.setattr(studio_mod.subprocess, "run", fake_subprocess_run)

        # Patch Path.home() so lionagi_home resolves inside tmp_path.
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        # Invoke _start_docker directly; it will call our fake subprocess.run.
        studio_mod._start_docker(host="127.0.0.1", api_port=8765, frontend_port=3000)

        # We expect two subprocess.run calls: pull + run. Return the run argv.
        assert len(captured) == 2, f"Expected 2 subprocess.run calls, got {len(captured)}"
        return captured[1]  # docker run argv

    def test_symlink_to_etc_is_not_mounted(self, tmp_path, monkeypatch, capsys):
        """Attack: symlink → /etc must be excluded from docker run argv."""
        etc = Path("/etc")
        if not etc.exists():
            import pytest

            pytest.skip("/etc does not exist on this platform")

        argv = self._build_docker_cmd(tmp_path, monkeypatch, etc)
        argv_str = " ".join(str(a) for a in argv)
        assert "/etc" not in argv_str, (
            "Escape succeeded: /etc appeared in docker run argv: " + argv_str
        )
        # The warning must have been printed.
        captured = capsys.readouterr()
        assert "outside the allowed mount roots" in captured.err

    def test_symlink_to_tmp_subdir_outside_home_is_not_mounted(self, tmp_path, monkeypatch, capsys):
        """Attack: symlink → a real directory outside allowed roots is blocked."""
        import tempfile

        # Create a real directory completely outside tmp_path (different mkdtemp).
        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir)
            argv = self._build_docker_cmd(tmp_path, monkeypatch, outside)
            argv_str = " ".join(str(a) for a in argv)
            assert str(outside) not in argv_str, (
                "Escape succeeded: outside path appeared in docker run argv: " + argv_str
            )
        captured = capsys.readouterr()
        assert "outside the allowed mount roots" in captured.err

    def test_symlink_inside_home_is_mounted(self, tmp_path, monkeypatch):
        """Normal case: symlink → a directory inside ~/.lionagi is permitted."""
        # Target lives inside tmp_path (which is the mocked home).
        safe_dir = tmp_path / "projects" / "firm" / "agents"
        safe_dir.mkdir(parents=True)

        argv = self._build_docker_cmd(tmp_path, monkeypatch, safe_dir)
        argv_str = " ".join(str(a) for a in argv)
        # The safe directory should appear as a -v mount.
        assert str(safe_dir) in argv_str, (
            "In-root symlink was incorrectly blocked. argv: " + argv_str
        )

    def test_broken_symlink_is_silently_skipped(self, tmp_path, monkeypatch):
        """A symlink pointing at a non-existent path is skipped without error."""
        nonexistent = tmp_path / "does_not_exist" / "file.yaml"
        # nonexistent was never created — resolve(strict=True) will raise OSError.
        argv = self._build_docker_cmd(tmp_path, monkeypatch, nonexistent)
        argv_str = " ".join(str(a) for a in argv)
        assert "does_not_exist" not in argv_str


# ---------------------------------------------------------------------------
# Unit tests for _mount_allowed_roots
# ---------------------------------------------------------------------------


class TestMountAllowedRoots:
    """_mount_allowed_roots returns expected safe roots."""

    def test_home_is_always_in_roots(self):
        from lionagi.cli.studio import _mount_allowed_roots

        roots = _mount_allowed_roots()
        home = Path.home().resolve()
        assert home in roots

    def test_xdg_config_home_is_included_when_set(self, tmp_path, monkeypatch):
        from lionagi.cli.studio import _mount_allowed_roots

        xdg = tmp_path / "xdg-config"
        xdg.mkdir()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        roots = _mount_allowed_roots()
        assert xdg.resolve() in roots

    def test_xdg_config_home_not_duplicated_if_equals_home(self, monkeypatch):
        """When XDG_CONFIG_HOME == home, it must not appear twice."""
        from lionagi.cli.studio import _mount_allowed_roots

        home = Path.home().resolve()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
        roots = _mount_allowed_roots()
        assert roots.count(home) == 1

    def test_no_xdg_env_gives_only_home(self, monkeypatch):
        from lionagi.cli.studio import _mount_allowed_roots

        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        roots = _mount_allowed_roots()
        assert len(roots) == 1
        assert Path.home().resolve() in roots
