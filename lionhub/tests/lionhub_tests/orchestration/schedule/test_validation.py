"""Tests for argument injection hardening in build_argv (CWE-88): model, extra-args, identifiers, sentinel."""

from __future__ import annotations

import pytest


class TestGithubRepoValidatorUnit:
    """_validate_github_repo rejects values that would manipulate the API path."""

    def test_path_traversal_raises(self):
        """'../../other-endpoint' must raise ValueError."""
        from lionhub.orchestration.schedule.validation import _validate_github_repo

        with pytest.raises(ValueError, match="github_repo"):
            _validate_github_repo("../../other-endpoint")

    def test_extra_slash_segment_raises(self):
        """'owner/name/extra' (more than one slash) must raise ValueError."""
        from lionhub.orchestration.schedule.validation import _validate_github_repo

        with pytest.raises(ValueError, match="github_repo"):
            _validate_github_repo("owner/name/extra")

    def test_no_slash_raises(self):
        """'owner' (missing slash) must raise ValueError."""
        from lionhub.orchestration.schedule.validation import _validate_github_repo

        with pytest.raises(ValueError, match="github_repo"):
            _validate_github_repo("owner")

    def test_leading_dash_in_owner_raises(self):
        """'-owner/repo' (leading dash in owner segment) must raise ValueError."""
        from lionhub.orchestration.schedule.validation import _validate_github_repo

        with pytest.raises(ValueError, match="github_repo"):
            _validate_github_repo("-owner/repo")

    def test_percent_encoded_traversal_raises(self):
        """'%2e%2e/repo' (URL-encoded dots) must raise ValueError."""
        from lionhub.orchestration.schedule.validation import _validate_github_repo

        with pytest.raises(ValueError, match="github_repo"):
            _validate_github_repo("%2e%2e/repo")

    def test_empty_string_raises(self):
        """Empty string must raise ValueError (no owner/name structure)."""
        from lionhub.orchestration.schedule.validation import _validate_github_repo

        with pytest.raises(ValueError, match="github_repo"):
            _validate_github_repo("")

    def test_valid_repo_accepted(self):
        """'owner/repo.name-x_1' is a legitimate GitHub repo and must pass."""
        from lionhub.orchestration.schedule.validation import _validate_github_repo

        _validate_github_repo("owner/repo.name-x_1")  # must not raise

    def test_valid_simple_repo_accepted(self):
        """'octocat/hello-world' is a common GitHub repo and must pass."""
        from lionhub.orchestration.schedule.validation import _validate_github_repo

        _validate_github_repo("octocat/hello-world")  # must not raise

    def test_dot_prefix_repo_accepted(self):
        """'github/.github' must be accepted -- repos starting with '.' are valid
        (verified: https://api.github.com/repos/github/.github returns 200)."""
        from lionhub.orchestration.schedule.validation import _validate_github_repo

        _validate_github_repo("github/.github")  # must not raise

    def test_single_char_owner_accepted(self):
        """'a/repo' — a single-char owner is valid GitHub grammar and must pass."""
        from lionhub.orchestration.schedule.validation import _validate_github_repo

        _validate_github_repo("a/repo")  # must not raise

    def test_dotted_repo_names_accepted(self):
        """Repo names that are dotted but not traversal singletons are accepted.

        'owner/...', 'owner/a..b', 'owner/.git' remain a single URL path segment
        (not '.'/'..'), so they are path-safe and must not be over-rejected.
        """
        from lionhub.orchestration.schedule.validation import _validate_github_repo

        _validate_github_repo("owner/...")  # must not raise
        _validate_github_repo("owner/a..b")  # must not raise
        _validate_github_repo("owner/.git")  # must not raise

    def test_dot_singleton_repo_raises(self):
        """'owner/.' is a path-traversal singleton and must raise ValueError."""
        from lionhub.orchestration.schedule.validation import _validate_github_repo

        with pytest.raises(ValueError, match="traversal"):
            _validate_github_repo("owner/.")

    def test_dotdot_singleton_repo_raises(self):
        """'owner/..' is a path-traversal singleton and must raise ValueError."""
        from lionhub.orchestration.schedule.validation import _validate_github_repo

        with pytest.raises(ValueError, match="traversal"):
            _validate_github_repo("owner/..")

    def test_owner_too_long_raises(self):
        """Owner segment > 39 chars must raise ValueError."""
        from lionhub.orchestration.schedule.validation import _validate_github_repo

        long_owner = "a" * 40
        with pytest.raises(ValueError, match="owner segment"):
            _validate_github_repo(f"{long_owner}/repo")

    def test_repo_name_too_long_raises(self):
        """Repo name segment > 100 chars must raise ValueError."""
        from lionhub.orchestration.schedule.validation import _validate_github_repo

        long_name = "a" * 101
        with pytest.raises(ValueError, match="repo name segment"):
            _validate_github_repo(f"owner/{long_name}")

    def test_owner_exactly_max_length_accepted(self):
        """Owner segment of exactly 39 chars must be accepted."""
        from lionhub.orchestration.schedule.validation import _validate_github_repo

        owner = "a" * 39
        _validate_github_repo(f"{owner}/repo")  # must not raise

    def test_repo_name_exactly_max_length_accepted(self):
        """Repo name of exactly 100 chars must be accepted."""
        from lionhub.orchestration.schedule.validation import _validate_github_repo

        name = "a" * 100
        _validate_github_repo(f"owner/{name}")  # must not raise


@pytest.mark.parametrize(
    "bad_value",
    [
        {},
        {"max_fires": 1},
        {"window_sec": 60},
        {"max_fires": 0, "window_sec": 60},
        {"max_fires": True, "window_sec": 60},
        {"max_fires": 1, "window_sec": 0},
        {"max_fires": 1, "window_sec": 60, "burst": 2},
    ],
)
def test_validate_rate_limit_rejects_malformed_config(bad_value):
    from lionhub.orchestration.schedule.validation import validate_rate_limit

    with pytest.raises(ValueError, match="rate_limit"):
        validate_rate_limit(bad_value)


def test_validate_rate_limit_accepts_positive_window_cap():
    from lionhub.orchestration.schedule.validation import validate_rate_limit

    assert validate_rate_limit({"max_fires": 3, "window_sec": 120}) == (3, 120)
