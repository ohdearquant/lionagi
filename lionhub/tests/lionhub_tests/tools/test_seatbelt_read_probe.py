# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The Seatbelt policy's read roots and the probe that measures them on a volume."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from lionagi import _paths
from lionagi._errors import ConfigurationError
from lionhub.tools import _seatbelt, seatbelt_link_probe, seatbelt_read_probe
from lionhub.tools._seatbelt import SeatbeltPolicy, seatbelt_available
from lionhub.tools.seatbelt_read_probe import REQUIRED_ARMS, policy_digest, write_record

needs_seatbelt = pytest.mark.skipif(
    not seatbelt_available(), reason="needs macOS with /usr/bin/sandbox-exec"
)

_UUID = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    """A lionagi home and a temporary directory of the test's own, both resolved."""
    base = tmp_path.resolve()
    (base / "home").mkdir()
    (base / "tmp").mkdir()
    monkeypatch.setattr(_paths, "LIONAGI_HOME", base / "home")
    monkeypatch.setattr(tempfile, "tempdir", str(base / "tmp"))
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setattr(
        _seatbelt, "_uv_cache_dir", lambda *, _command_env_names=None: base / "uv-cache"
    )
    # Where the host's temp directory holds the base (/tmp on Linux), that root would refuse
    # every scratch directory under the base; it is dropped, and the base's own tmp stays the
    # temp directory the probe refuses.
    temp_roots = seatbelt_link_probe._temp_roots
    monkeypatch.setattr(
        seatbelt_link_probe,
        "_temp_roots",
        lambda: [root for root in temp_roots() if root != base and root not in base.parents],
    )
    return base


@pytest.fixture
def one_volume(monkeypatch):
    """Every path on one volume, named without df or diskutil."""
    monkeypatch.setattr(
        seatbelt_read_probe, "volume_identity", lambda path: ("/Volumes/Test", _UUID)
    )


@pytest.fixture
def planted(monkeypatch, home) -> Path:
    """The probe plants its files in directories under the test's base, two of them reached
    through a link, in place of /tmp, /var/tmp and /Users/Shared."""
    (home / "planted-real").mkdir()
    (home / "planted-link").symlink_to(home / "planted-real")
    (home / "shared").mkdir()
    monkeypatch.setattr(
        seatbelt_read_probe,
        "_PLANTED",
        (
            ("read_via_tmp_link_refused", str(home / "planted-link")),
            ("read_via_var_link_refused", str(home / "planted-link")),
            ("read_users_shared_refused", str(home / "shared")),
        ),
    )
    return home


def _params(argv: list[str]) -> list[tuple[str, str]]:
    """The ``-D NAME=value`` parameters of a wrapped command, in order."""
    pairs = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-D"]
    return [tuple(pair.split("=", 1)) for pair in pairs]


def _readable(policy: SeatbeltPolicy, path: Path) -> bool:
    real = Path(os.path.realpath(path))
    return any(
        real == Path(root) or Path(root) in real.parents
        for root in (*policy.writable_roots, *(policy.read_roots or ()))
    )


def _seatbelt_model(*, refuses_outside: bool = True, refuses_unrestricted: bool = False):
    """A stand-in for running under Seatbelt: with read roots, an operation on a path outside
    every readable root prints EPERM, as the kernel's refusal does; the shell lists its
    directory and the interpreter starts."""

    def run(policy, cwd, argv):
        if argv[0] == "/bin/sh":
            return 0, "\n".join(sorted(p.name for p in Path(cwd).iterdir())), ""
        if "-S" not in argv:
            return 0, "python=ok", ""
        op, path = argv[-2], Path(argv[-1])
        restricted = policy.read_roots is not None
        if (restricted and refuses_outside and not _readable(policy, path)) or (
            not restricted and refuses_unrestricted
        ):
            return 0, f"{op}=EPERM", ""
        n = {"read": lambda: len(path.read_bytes()), "stat": lambda: path.stat().st_size}.get(
            op, lambda: len(os.listdir(path))
        )()
        return 0, f"{op}=ok:{n}", ""

    return run


# ---------------------------------------------------------------------------- policy text
def test_without_read_roots_the_policy_reads_everywhere_as_before():
    policy = SeatbeltPolicy(writable_roots=("/w/ws",), read_only=("/w/ws/.git",), deny_read=("/d",))
    text = policy.profile()

    assert policy.read_roots is None
    assert text.startswith(_seatbelt._BASE_POLICY)
    assert "(allow file-read*)\n" in text
    assert "READ_ROOT" not in text and "READ_PARENT" not in text
    names = [name for name, _ in _params(policy.wrap(["/usr/bin/true"]))]
    assert not [name for name in names if name.startswith(("READ_ROOT_", "READ_PARENT_"))]
    assert "READ_ONLY_0" in names


def test_with_read_roots_reads_are_allowed_only_under_the_roots():
    policy = SeatbeltPolicy(
        writable_roots=("/w/ws",),
        read_only=("/w/ws/.git",),
        deny_read=("/d",),
        read_roots=("/r",),
    )
    text = policy.profile()
    grant = (
        '(allow file-read*\n  (subpath (param "WRITABLE_ROOT_0"))\n'
        '  (subpath (param "READ_ROOT_0")))\n'
    )

    assert "(allow file-read*)\n" not in text
    assert text.startswith("(version 1)\n(deny default)\n")
    assert grant in text
    assert _seatbelt._SYSTEM_READ_LITERALS in text
    # The parents are granted their metadata only, never a subtree.
    assert '(allow file-read-metadata\n  (literal (param "READ_PARENT_0"))' in text
    assert '(subpath (param "READ_PARENT_' not in text
    # The unreadable paths are denied after the read grants, so they win over them.
    assert text.index('(deny file-read* file-write* (subpath (param "DENY_READ_0")))') > (
        text.index(grant)
    )


def test_an_empty_set_of_read_roots_still_denies_reads_by_default():
    text = SeatbeltPolicy(writable_roots=("/w",), read_roots=()).profile()

    assert "(allow file-read*)\n" not in text
    assert '(allow file-read*\n  (subpath (param "WRITABLE_ROOT_0")))\n' in text


def test_the_parents_of_every_root_are_passed_once():
    policy = SeatbeltPolicy(writable_roots=("/w/ws", "/t"), read_roots=("/r/x",))
    params = _params(policy.wrap(["/usr/bin/true"]))
    parents = [value for name, value in params if name.startswith("READ_PARENT_")]

    assert parents == ["/w", "/", "/r"]
    assert ("READ_ROOT_0", "/r/x") in params
    assert policy.profile().count('(literal (param "READ_PARENT_') == len(parents)


def test_the_file_link_denies_stay_the_last_rules_with_read_roots():
    text = SeatbeltPolicy(
        writable_roots=("/w", "/t"),
        read_only=("/w/.git",),
        network=True,
        pty=True,
        deny_read=("/d",),
        read_roots=("/r",),
    ).profile()
    tail = text[text.index("(deny file-link") :]

    assert "(allow" not in tail
    assert [line for line in tail.splitlines() if line] == [
        line for line in tail.splitlines() if line.startswith("(deny file-link")
    ]


def test_the_digest_follows_the_read_rules(monkeypatch):
    policy = SeatbeltPolicy(writable_roots=("/w",), read_only=("/w/.git",), read_roots=("/r",))
    before = policy_digest(policy)
    monkeypatch.setattr(
        _seatbelt, "_SYSTEM_READ_LITERALS", _seatbelt._SYSTEM_READ_LITERALS + "(allow file-read*)\n"
    )

    assert policy_digest(policy) != before


def test_the_digest_follows_the_metadata_only_paths():
    policy = SeatbeltPolicy(writable_roots=("/w",), read_roots=("/r",))
    linked = SeatbeltPolicy(writable_roots=("/w",), read_roots=("/r",), read_metadata=("/l/x",))

    assert policy_digest(linked) != policy_digest(policy)


def test_the_root_directory_is_readable_only_under_read_roots():
    # Without the root directory itself, no process starts under a policy with read roots.
    root = '  (literal "/")\n'

    assert root in _seatbelt._SYSTEM_READ_LITERALS
    assert root in SeatbeltPolicy(writable_roots=("/w",), read_roots=()).profile()
    assert root not in SeatbeltPolicy(writable_roots=("/w",)).profile()


def test_metadata_only_paths_are_passed_as_parents_and_never_as_subtrees():
    policy = SeatbeltPolicy(
        writable_roots=("/w/ws",), read_roots=("/r/x",), read_metadata=("/l", "/l/py", "/w")
    )
    params = _params(policy.wrap(["/usr/bin/true"]))
    parents = [value for name, value in params if name.startswith("READ_PARENT_")]

    assert parents == ["/w", "/", "/r", "/l", "/l/py"]
    assert [value for name, value in params if name.startswith("READ_ROOT_")] == ["/r/x"]
    assert policy.profile().count('(literal (param "READ_PARENT_') == len(parents)
    # Without read roots nothing about them is rendered.
    open_policy = SeatbeltPolicy(writable_roots=("/w/ws",), read_metadata=("/l",))
    assert "READ_PARENT" not in open_policy.profile()


@pytest.mark.parametrize(
    "base",
    [
        pytest.param(
            lambda b: b.replace("(allow file-read*)\n", "(allow file-read* )\n"), id="reworded"
        ),
        pytest.param(lambda b: b + "(allow file-read*)\n", id="twice"),
    ],
)
def test_a_base_without_its_read_line_exactly_once_is_refused_under_read_roots(monkeypatch, base):
    monkeypatch.setattr(_seatbelt, "_BASE_POLICY", base(_seatbelt._BASE_POLICY))

    with pytest.raises(RuntimeError, match="not once"):
        SeatbeltPolicy(writable_roots=("/w",), read_roots=("/r",)).profile()
    # Without read roots the base is used as it stands.
    assert SeatbeltPolicy(writable_roots=("/w",)).profile().startswith(_seatbelt._BASE_POLICY)


def test_the_shipped_base_holds_its_read_line_exactly_once():
    assert _seatbelt._BASE_POLICY.count("(allow file-read*)\n") == 1


# ---------------------------------------------------------------------------- the interpreter's path
@pytest.fixture
def linked_python(tmp_path) -> dict[str, Path]:
    """An interpreter reached as a uv-installed one is: ``venv/bin/python`` links to
    ``python3.12`` beside it, which links to ``local/bin/python3.12``, which links into
    ``share/cp-link``, itself a link to the directory ``share/cp-real``."""
    base = tmp_path.resolve() / "py"
    (base / "share" / "cp-real" / "bin").mkdir(parents=True)
    real = base / "share" / "cp-real" / "bin" / "python3.12"
    real.write_text("")
    (base / "share" / "cp-link").symlink_to("cp-real")
    (base / "local" / "bin").mkdir(parents=True)
    (base / "local" / "bin" / "python3.12").symlink_to(
        base / "share" / "cp-link" / "bin" / "python3.12"
    )
    (base / "venv" / "bin").mkdir(parents=True)
    (base / "venv" / "bin" / "python3.12").symlink_to(base / "local" / "bin" / "python3.12")
    (base / "venv" / "bin" / "python").symlink_to("python3.12")
    return {
        "python": base / "venv" / "bin" / "python",
        "venv_link": base / "venv" / "bin" / "python3.12",
        "local_link": base / "local" / "bin" / "python3.12",
        "local_bin": base / "local" / "bin",
        "linked_dir": base / "share" / "cp-link",
        "real_dir": base / "share" / "cp-real",
        "real": real,
    }


def test_the_lookup_walk_names_every_link_and_linked_directory(linked_python):
    paths = _seatbelt._lookup_paths(str(linked_python["python"]))

    for name in ("python", "venv_link", "local_link", "linked_dir", "real_dir", "real"):
        assert str(linked_python[name]) in paths, name
    assert paths[-1] == str(linked_python["real"])
    assert (
        paths.index(str(linked_python["python"]))
        < paths.index(str(linked_python["local_link"]))
        < paths.index(str(linked_python["linked_dir"]))
        < paths.index(str(linked_python["real"]))
    )
    assert len(paths) == len(set(paths))
    # The control: realpath alone reaches only the last of these.
    assert os.path.realpath(linked_python["python"]) == str(linked_python["real"])


def test_a_link_loop_ends_the_lookup_walk(tmp_path):
    base = tmp_path.resolve()
    (base / "a").symlink_to(base / "b")
    (base / "b").symlink_to(base / "a")

    paths = _seatbelt._lookup_paths(str(base / "a"))

    assert str(base / "a") in paths and str(base / "b") in paths


def test_read_roots_grant_metadata_on_the_interpreter_path_only(home, linked_python, monkeypatch):
    (home / "workspace").mkdir()
    monkeypatch.setattr(sys, "executable", str(linked_python["python"]))

    policy = SeatbeltPolicy.for_workspace(home / "workspace", per_leg_uv_cache=True, read_roots=[])
    readable = policy.read_roots or ()
    parents = [
        value
        for name, value in _params(policy.wrap(["/usr/bin/true"]))
        if name.startswith("READ_PARENT_")
    ]

    for name in ("python", "venv_link", "local_link", "linked_dir", "real"):
        assert str(linked_python[name]) in policy.read_metadata, name
        assert str(linked_python[name]) in parents, name
    # A directory a link sits in gets its metadata, never its contents.
    assert str(linked_python["local_bin"]) not in readable
    assert not _readable(policy, linked_python["local_bin"] / "other")
    assert len(parents) == len(set(parents))


def test_without_read_roots_no_metadata_paths_are_named(home, linked_python, monkeypatch):
    (home / "workspace").mkdir()
    monkeypatch.setattr(sys, "executable", str(linked_python["python"]))

    assert (
        SeatbeltPolicy.for_workspace(home / "workspace", per_leg_uv_cache=True).read_metadata == ()
    )


# ---------------------------------------------------------------------------- building a policy
def test_a_workspace_policy_without_read_roots_leaves_reads_open(home):
    (home / "workspace").mkdir()
    policy = SeatbeltPolicy.for_workspace(home / "workspace", per_leg_uv_cache=True)

    assert policy.read_roots is None


def test_read_roots_add_the_system_set_and_the_interpreter(home):
    (home / "workspace").mkdir()
    (home / "docs").mkdir()
    policy = SeatbeltPolicy.for_workspace(
        home / "workspace", per_leg_uv_cache=True, read_roots=[home / "docs"]
    )
    readable = policy.read_roots or ()

    assert readable[0] == str(home / "docs")
    for path in (*_seatbelt._SYSTEM_READ_ROOTS, sys.prefix, sys.base_prefix):
        assert os.path.realpath(path) in readable
    assert len(readable) == len(set(readable))
    assert not set(readable) & set(policy.writable_roots)


@pytest.mark.parametrize("which", ["home", "above", "/"])
def test_a_read_root_that_is_or_holds_the_home_directory_is_refused(home, monkeypatch, which):
    (home / "workspace").mkdir()
    (home / "user" / "me").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home / "user" / "me"))
    root = {"home": home / "user" / "me", "above": home / "user", "/": Path("/")}[which]

    with pytest.raises(ConfigurationError, match="home directory or holds it"):
        SeatbeltPolicy.for_workspace(home / "workspace", per_leg_uv_cache=True, read_roots=[root])


# ---------------------------------------------------------------------------- the probe's arms
def test_the_probe_passes_under_a_policy_that_holds(home, one_volume, planted, monkeypatch):
    monkeypatch.setattr(seatbelt_read_probe, "_run", _seatbelt_model())
    mount, uuid, arms, digest, read_roots = seatbelt_read_probe.probe(home / "scratch")

    assert (mount, uuid) == ("/Volumes/Test", _UUID)
    assert set(arms) == set(REQUIRED_ARMS)
    assert all(arm["passed"] for arm in arms.values()), arms
    assert "read=EPERM" in arms["read_outside_refused"]["evidence"]
    assert os.path.realpath(sys.prefix) in read_roots
    assert list((home / "scratch").iterdir()) == []  # the working directory is removed


def test_the_refusal_arms_fail_when_reads_outside_are_not_refused(
    home, one_volume, planted, monkeypatch
):
    monkeypatch.setattr(seatbelt_read_probe, "_run", _seatbelt_model(refuses_outside=False))
    _, _, arms, _, _ = seatbelt_read_probe.probe(home / "scratch")

    for name in (
        "read_outside_refused",
        "read_through_symlink_refused",
        "stat_outside_refused",
        "list_parent_refused",
        "read_via_tmp_link_refused",
        "read_via_var_link_refused",
        "read_users_shared_refused",
    ):
        assert arms[name]["passed"] is False, name
    assert arms["read_inside_root_works"]["passed"] is True
    assert arms["control_reads_outside_without_read_roots"]["passed"] is True


def test_a_child_that_does_not_start_passes_no_refusal_arm(home, one_volume, planted, monkeypatch):
    healthy = _seatbelt_model()

    def run(policy, cwd, argv):
        if policy.read_roots is not None and argv[0] == sys.executable:
            return 1, "", "dyld: Library not loaded"
        return healthy(policy, cwd, argv)

    monkeypatch.setattr(seatbelt_read_probe, "_run", run)
    _, _, arms, _, _ = seatbelt_read_probe.probe(home / "scratch")

    assert [name for name, arm in arms.items() if arm["passed"]] == [
        "control_reads_outside_without_read_roots",
        "shell_runs_in_root",
    ]
    assert "Library not loaded" in arms["read_outside_refused"]["evidence"]


def test_a_refusal_with_another_errno_fails_its_arm(home, one_volume, planted, monkeypatch):
    healthy = _seatbelt_model()

    def run(policy, cwd, argv):
        rc, out, err = healthy(policy, cwd, argv)
        return rc, out.replace("=EPERM", "=ENOENT"), err

    monkeypatch.setattr(seatbelt_read_probe, "_run", run)
    _, _, arms, _, _ = seatbelt_read_probe.probe(home / "scratch")

    assert arms["read_outside_refused"]["passed"] is False
    assert arms["stat_outside_refused"]["passed"] is False


def test_the_control_arm_fails_when_reads_are_refused_without_read_roots(
    home, one_volume, planted, monkeypatch
):
    monkeypatch.setattr(seatbelt_read_probe, "_run", _seatbelt_model(refuses_unrestricted=True))
    _, _, arms, _, _ = seatbelt_read_probe.probe(home / "scratch")

    assert arms["control_reads_outside_without_read_roots"]["passed"] is False
    assert arms["read_outside_refused"]["passed"] is True


def test_a_shell_that_cannot_list_the_root_fails_its_arm(home, one_volume, planted, monkeypatch):
    healthy = _seatbelt_model()

    def run(policy, cwd, argv):
        if argv[0] == "/bin/sh":
            return 1, "", "ls: .: Operation not permitted"
        return healthy(policy, cwd, argv)

    monkeypatch.setattr(seatbelt_read_probe, "_run", run)
    _, _, arms, _, _ = seatbelt_read_probe.probe(home / "scratch")

    assert arms["shell_runs_in_root"]["passed"] is False


def test_a_planted_file_is_read_as_spelled_and_removed_afterwards(
    home, one_volume, planted, monkeypatch
):
    healthy = _seatbelt_model()
    read: list[str] = []

    def run(policy, cwd, argv):
        if argv[0] == sys.executable and "-S" in argv:
            read.append(argv[-1])
        return healthy(policy, cwd, argv)

    monkeypatch.setattr(seatbelt_read_probe, "_run", run)
    _, _, arms, _, _ = seatbelt_read_probe.probe(home / "scratch")

    assert arms["read_via_tmp_link_refused"]["passed"] is True
    # The child is handed the path through the link, as a command would spell it.
    assert any(path.startswith(str(home / "planted-link") + os.sep) for path in read)
    assert list((home / "planted-real").iterdir()) == []
    assert list((home / "shared").iterdir()) == []


def test_a_directory_where_no_file_can_be_planted_fails_its_arm(
    home, one_volume, planted, monkeypatch
):
    monkeypatch.setattr(
        seatbelt_read_probe, "_PLANTED", (("read_users_shared_refused", str(home / "absent")),)
    )
    monkeypatch.setattr(seatbelt_read_probe, "_run", _seatbelt_model())
    _, _, arms, _, _ = seatbelt_read_probe.probe(home / "scratch")

    assert arms["read_users_shared_refused"]["passed"] is False
    assert "no file could be planted" in arms["read_users_shared_refused"]["evidence"]


def test_a_planted_directory_inside_a_readable_root_is_refused(
    home, one_volume, planted, monkeypatch
):
    # The test's temporary directory is a writable root, as /tmp is when TMPDIR names it.
    monkeypatch.setattr(
        seatbelt_read_probe, "_PLANTED", (("read_via_tmp_link_refused", str(home / "tmp")),)
    )
    monkeypatch.setattr(seatbelt_read_probe, "_run", _seatbelt_model())

    with pytest.raises(ConfigurationError, match="set TMPDIR"):
        seatbelt_read_probe.probe(home / "scratch")
    assert not list((home / "tmp").glob("lionagi-read-probe-*"))


def test_the_probe_refuses_a_scratch_directory_inside_the_temp_directory(home):
    with pytest.raises(ConfigurationError, match="inside the temp directory"):
        seatbelt_read_probe.probe(home / "tmp" / "scratch")


# ---------------------------------------------------------------------------- the record and the command
def test_the_record_holds_the_kernel_digest_read_roots_and_arms(home):
    arms = {name: {"passed": True, "evidence": "test"} for name in REQUIRED_ARMS}
    path = write_record("/Volumes/Test", _UUID, arms, "ab" * 32, ("/usr", "/bin"))
    record = json.loads(path.read_text(encoding="utf-8"))

    assert path == home / "home" / "seatbelt-read-probes" / f"volume-{_UUID}.json"
    assert record["format"] == 1
    assert record["volume_uuid"] == _UUID
    assert record["policy_sha256"] == "ab" * 32
    assert record["read_roots"] == ["/usr", "/bin"]
    assert record["kernel"] == seatbelt_read_probe._kernel()
    assert record["arms"] == arms


def test_the_command_exits_2_on_a_host_without_seatbelt(monkeypatch, capsys):
    monkeypatch.setattr(_seatbelt, "seatbelt_available", lambda: False)

    assert seatbelt_read_probe.main([]) == 2
    assert "no Seatbelt sandbox" in capsys.readouterr().err


def test_the_command_prints_each_arm_and_exits_1_when_one_fails(
    home, one_volume, planted, monkeypatch, capsys
):
    monkeypatch.setattr(_seatbelt, "seatbelt_available", lambda: True)
    monkeypatch.setattr(seatbelt_read_probe, "_run", _seatbelt_model(refuses_outside=False))

    assert seatbelt_read_probe.main(["--scratch", str(home / "scratch")]) == 1
    out = capsys.readouterr().out
    assert "FAIL  read_outside_refused" in out
    assert "pass  read_inside_root_works" in out
    assert (home / "home" / "seatbelt-read-probes" / f"volume-{_UUID}.json").is_file()


# ---------------------------------------------------------------------------- on a real host
@needs_seatbelt
def test_the_probe_on_this_volume_writes_a_record_for_every_arm(home):
    mount, uuid, arms, digest, read_roots = seatbelt_read_probe.probe(home / "scratch")

    assert set(arms) == set(REQUIRED_ARMS)
    # The control must hold on any host: without read roots the outside file reads.
    assert arms["control_reads_outside_without_read_roots"]["passed"], arms
    # And the policy with read roots must hold on this host: every arm passes.
    failed = sorted(name for name, arm in arms.items() if not arm["passed"])
    assert not failed, arms
    path = write_record(mount, uuid, arms, digest, read_roots)
    assert path.parent == home / "home" / "seatbelt-read-probes"


@needs_seatbelt
def test_the_refusal_arms_fail_under_a_policy_that_allows_every_read(home, monkeypatch):
    # A policy that allows everything cannot refuse a read; the arms must say so, not pass.
    monkeypatch.setattr(SeatbeltPolicy, "profile", lambda self: "(version 1)\n(allow default)\n")
    _, _, arms, _, _ = seatbelt_read_probe.probe(home / "scratch")

    assert arms["read_outside_refused"]["passed"] is False
    assert arms["read_through_symlink_refused"]["passed"] is False
    assert arms["stat_outside_refused"]["passed"] is False
    assert arms["list_parent_refused"]["passed"] is False
    assert arms["read_via_tmp_link_refused"]["passed"] is False
    assert arms["read_via_var_link_refused"]["passed"] is False
    assert arms["read_users_shared_refused"]["passed"] is False
    assert arms["read_inside_root_works"]["passed"] is True
