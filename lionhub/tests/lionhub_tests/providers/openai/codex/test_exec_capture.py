# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The codex exec capture: which command lines it runs, under which codex home, what it
refuses before running anything, and what it writes."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from lionhub.providers.openai import _codex_hook_trust as trust
from lionhub.providers.openai import _codex_project_layers
from lionhub.providers.openai import codex_exec_capture as capture_module
from lionhub.providers.openai.codex_exec_capture import (
    CASES,
    HOOK_REASON,
    UNAVAILABLE_MODEL,
    CaptureRefusedError,
    capture,
    case_argv,
    main,
    stream_shape,
)

_SECRET = "sk-capture-test-0123456789abcdefghijklmnop"


class _Recorder:
    """An injected runner that records each call and answers with fixed output."""

    def __init__(self, answer=(0, "", ""), events=None):
        self.calls: list[tuple[list[str], dict[str, str], Path]] = []
        self.answer = answer
        self.events = events if events is not None else []

    def __call__(self, argv, env, cwd):
        self.events.append(("run", argv))
        self.calls.append((argv, env, cwd))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(_codex_project_layers, "_SYSTEM_CONFIG", tmp_path / "system.toml")
    monkeypatch.setattr(capture_module, "codex_binary_version", lambda binary: "0.162.1")
    operator = tmp_path / "operator-home"
    operator.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return {"HOME": str(home), "CODEX_HOME": str(operator), "PATH": "/usr/bin:/bin"}


def _run(tmp_path, env, **kwargs):
    recorder = kwargs.pop("runner", None) or _Recorder()
    said: list[str] = []
    recorder.events = said_events = []

    def say(line):
        said.append(line)
        said_events.append(("say", line))

    manifest = capture(tmp_path / "out", env=env, runner=recorder, say=say, **kwargs)
    return manifest, recorder, said, said_events


def test_the_prompts_and_cases_are_fixed_in_the_source():
    assert [case.name for case in CASES] == [
        "no_output", "unknown_model", "hook_blocked", "scratch_no_hook",
    ]  # fmt: skip
    assert all(case.prompt for case in CASES)
    assert [case.scratch_home for case in CASES] == [False, False, True, True]
    assert [case.hook for case in CASES] == [False, False, True, False]
    # The control asks what the hook case asks, so the hook is the one difference.
    assert CASES[2].prompt == CASES[3].prompt and CASES[2].extra_args == CASES[3].extra_args


def test_the_default_capture_runs_two_cases_read_only_without_user_config(tmp_path, env):
    manifest, recorder, said, events = _run(tmp_path, env)
    out = (tmp_path / "out").resolve()
    assert [argv for argv, _e, _c in recorder.calls] == [
        [
            "codex", "exec", "--json", "--ephemeral", "--ignore-user-config",
            "--sandbox", "read-only", "--skip-git-repo-check", "-C", str(out / "no_output" / "cwd"),
            "--", CASES[0].prompt,
        ],
        [
            "codex", "exec", "--json", "--ephemeral", "--ignore-user-config",
            "--sandbox", "read-only", "--skip-git-repo-check", "-C", str(out / "unknown_model" / "cwd"),
            "-m", UNAVAILABLE_MODEL, "--", CASES[1].prompt,
        ],
    ]  # fmt: skip
    for argv, child_env, cwd in recorder.calls:
        assert child_env["CODEX_HOME"] == str(Path(env["CODEX_HOME"]).resolve())
        assert cwd == Path(argv[argv.index("-C") + 1])
        assert cwd.is_dir() and not any(cwd.iterdir())
    # Each command line is printed before it runs.
    assert [kind for kind, _ in events] == ["say", "run", "say", "run"]
    for line, (argv, child_env, _cwd) in zip(said, recorder.calls):
        assert shlex.split(line)[1:] == [f"CODEX_HOME={child_env['CODEX_HOME']}", *argv]
    assert sorted(manifest["cases"]) == ["no_output", "unknown_model"]
    assert manifest["codex_version"] == "0.162.1"
    assert json.loads((out / "manifest.json").read_text()) == manifest
    for name in manifest["cases"]:
        assert sorted(p.name for p in (out / name).iterdir()) == [
            "cwd", "exit_status.txt", "stderr.txt", "stdout.jsonl",
        ]  # fmt: skip


def test_the_operator_home_is_passed_by_path_and_never_read(tmp_path, env):
    secret_config = Path(env["CODEX_HOME"]) / "config.toml"
    secret_config.write_text('[mcp_servers.x]\ncommand = "x"\n')
    secret_config.chmod(0)
    try:
        manifest, recorder, _said, _events = _run(tmp_path, env)
    finally:
        secret_config.chmod(0o600)
    assert len(recorder.calls) == 2
    assert all(case["codex_home"] == "operator" for case in manifest["cases"].values())


def test_the_hook_case_runs_in_a_scratch_home_whose_hook_reads_as_trusted(tmp_path, env):
    manifest, recorder, _said, _events = _run(tmp_path, env, hook_case=True)
    out = (tmp_path / "out").resolve()
    argv, child_env, _cwd = recorder.calls[2]
    home = out / "hook_blocked" / "codex-home"
    assert child_env["CODEX_HOME"] == str(home)
    assert "--ignore-user-config" not in argv and "--ephemeral" not in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert manifest["cases"]["hook_blocked"]["codex_home"] == "scratch"
    report = trust.hook_trust_report(home)
    assert report.problems == ()
    assert [(h.hook.event, h.status) for h in report.hooks] == [("UserPromptSubmit", trust.TRUSTED)]
    assert sorted(p.name for p in home.iterdir()) == [
        "block-prompt.sh", "config.toml", "hooks.json",
    ]  # fmt: skip
    blocked = subprocess.run(
        [str(home / "block-prompt.sh")], capture_output=True, text=True, check=False
    )
    assert (blocked.returncode, blocked.stderr.strip()) == (2, HOOK_REASON)


def test_the_control_runs_in_a_scratch_home_written_the_same_way_with_no_hook(tmp_path, env):
    manifest, recorder, _said, _events = _run(tmp_path, env, hook_case=True)
    out = (tmp_path / "out").resolve()
    hook_argv, hook_env, _ = recorder.calls[2]
    argv, child_env, cwd = recorder.calls[3]
    home = out / "scratch_no_hook" / "codex-home"
    assert child_env["CODEX_HOME"] == str(home)
    assert manifest["cases"]["scratch_no_hook"]["codex_home"] == "scratch"
    # The same command line as the hook case but for the working directory.
    assert argv == [str(cwd) if a == hook_argv[hook_argv.index("-C") + 1] else a for a in hook_argv]
    assert sorted(p.name for p in home.iterdir()) == ["config.toml"]
    assert (home / "config.toml").read_text() == ""
    report = trust.hook_trust_report(home)
    assert (report.hooks, report.problems) == ((), ())
    assert hook_env["CODEX_HOME"] != child_env["CODEX_HOME"]


def test_a_trust_record_with_the_wrong_hash_would_not_read_as_trusted(tmp_path, env):
    # Control for the trust check above: the same home with its hash changed reads otherwise.
    _run(tmp_path, env, hook_case=True)
    home = (tmp_path / "out" / "hook_blocked" / "codex-home").resolve()
    config = home / "config.toml"
    config.write_text(config.read_text().replace("sha256:", "sha256:0"))
    statuses = [h.status for h in trust.hook_trust_report(home).hooks]
    assert statuses != [trust.TRUSTED]


@pytest.mark.parametrize("kind", ["non_empty_dir", "file"])
def test_an_existing_out_that_is_not_an_empty_directory_is_refused(tmp_path, env, kind):
    out = tmp_path / "out"
    if kind == "file":
        out.write_text("x")
    else:
        out.mkdir()
        (out / "keep").write_text("x")
    recorder = _Recorder()
    with pytest.raises(CaptureRefusedError, match="is not an empty directory"):
        capture(out, env=env, runner=recorder, say=lambda line: None)
    assert recorder.calls == []


def test_an_empty_existing_out_is_used(tmp_path, env):
    (tmp_path / "out").mkdir()
    manifest, recorder, _said, _events = _run(tmp_path, env)
    assert len(recorder.calls) == 2 and manifest["cases"]


def test_a_project_config_above_out_is_refused_before_any_run(tmp_path, env):
    project = tmp_path / "project"
    (project / ".codex").mkdir(parents=True)
    (project / ".codex" / "config.toml").write_text('model = "x"\n')
    recorder = _Recorder()
    with pytest.raises(CaptureRefusedError, match="project config"):
        capture(project / "out", env=env, runner=recorder, say=lambda line: None)
    assert recorder.calls == []


def test_system_mcp_servers_are_refused_before_any_run(tmp_path, env):
    (tmp_path / "system.toml").write_text('[mcp_servers.ambient]\ncommand = "x"\n')
    recorder = _Recorder()
    with pytest.raises(CaptureRefusedError, match="ambient"):
        capture(tmp_path / "out", env=env, runner=recorder, say=lambda line: None)
    assert recorder.calls == []


@pytest.mark.parametrize(
    "system_text",
    [
        '[[hooks.UserPromptSubmit]]\n[[hooks.UserPromptSubmit.hooks]]\ntype = "command"\ncommand = "x"\n',
        'hooks = "not a table"\n',
    ],
)
def test_hooks_in_the_system_config_are_refused_before_any_run(tmp_path, env, system_text):
    (tmp_path / "system.toml").write_text(system_text)
    recorder = _Recorder()
    with pytest.raises(CaptureRefusedError, match="declares hooks"):
        capture(tmp_path / "out", env=env, runner=recorder, say=lambda line: None)
    assert recorder.calls == []
    assert not (tmp_path / "out").exists()


def test_a_system_hooks_file_is_refused_before_any_run(tmp_path, env):
    (tmp_path / "hooks.json").write_text('{"hooks": {}}\n')
    recorder = _Recorder()
    with pytest.raises(CaptureRefusedError, match="system hooks file"):
        capture(tmp_path / "out", env=env, runner=recorder, say=lambda line: None)
    assert recorder.calls == []


def test_an_unreadable_system_config_is_refused(tmp_path, env):
    (tmp_path / "system.toml").write_text("not = [toml\n")
    recorder = _Recorder()
    with pytest.raises(CaptureRefusedError, match="cannot be read"):
        capture(tmp_path / "out", env=env, runner=recorder, say=lambda line: None)
    assert recorder.calls == []


def test_a_system_config_with_only_hook_state_and_settings_is_not_refused(tmp_path, env):
    (tmp_path / "system.toml").write_text('model = "x"\n[hooks.state."a:b:0:0"]\nenabled = true\n')
    manifest, recorder, _said, _events = _run(tmp_path, env)
    assert len(recorder.calls) == 2 and manifest["cases"]


@pytest.mark.parametrize(
    ("cases", "hook_case", "ran"),
    [
        (None, False, ["no_output", "unknown_model"]),
        (["hook_blocked"], False, ["hook_blocked"]),
        (["scratch_no_hook"], False, ["scratch_no_hook"]),
        (None, True, ["no_output", "unknown_model", "hook_blocked", "scratch_no_hook"]),
        (["unknown_model", "no_output", "unknown_model"], False, ["no_output", "unknown_model"]),
        (["unknown_model"], True, ["unknown_model", "hook_blocked", "scratch_no_hook"]),
    ],
)
def test_cases_runs_the_named_cases_in_their_fixed_order(tmp_path, env, cases, hook_case, ran):
    manifest, recorder, _said, _events = _run(tmp_path, env, cases=cases, hook_case=hook_case)
    assert list(manifest["cases"]) == ran
    assert len(recorder.calls) == len(ran)
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted([*ran, "manifest.json"])


def test_an_unknown_case_is_refused_before_anything_is_written(tmp_path, env):
    recorder = _Recorder()
    with pytest.raises(CaptureRefusedError, match="no such case"):
        capture(tmp_path / "out", env=env, cases=["nope"], runner=recorder, say=lambda line: None)
    assert recorder.calls == [] and not (tmp_path / "out").exists()


def test_main_runs_only_the_cases_named_on_the_command_line(tmp_path, env, monkeypatch):
    seen = {}

    def fake_capture(out, **kwargs):
        seen.update(kwargs)
        return {"cases": {}}

    monkeypatch.setattr(capture_module, "capture", fake_capture)
    assert main(["--out", str(tmp_path / "o"), "--case", "hook_blocked"]) == 0
    assert seen["cases"] == ["hook_blocked"] and seen["hook_case"] is False
    assert main(["--out", str(tmp_path / "o")]) == 0
    assert seen["cases"] is None
    with pytest.raises(SystemExit):
        main(["--out", str(tmp_path / "o"), "--case", "nope"])


def test_a_relative_codex_home_is_refused(tmp_path, env):
    env["CODEX_HOME"] = "relative/home"
    recorder = _Recorder()
    with pytest.raises(CaptureRefusedError, match="codex home"):
        capture(tmp_path / "out", env=env, runner=recorder, say=lambda line: None)
    assert recorder.calls == []


def test_secret_values_in_the_environment_are_removed_from_what_is_written(tmp_path, env):
    env["OPENAI_API_KEY"] = _SECRET
    recorder = _Recorder(answer=(1, f'{{"key": "{_SECRET}"}}\n', f"bad key {_SECRET}\n"))
    manifest, _recorder, said, _events = _run(tmp_path, env, runner=recorder)
    out = (tmp_path / "out").resolve()
    written = [p.read_text() for p in out.rglob("*") if p.is_file()]
    assert written and not any(_SECRET in text for text in written)
    assert "[redacted]" in (out / "no_output" / "stdout.jsonl").read_text()
    assert not any(_SECRET in line for line in said)
    assert manifest["cases"]["no_output"]["exit_status"] == 1


def test_the_written_stream_keeps_one_line_per_event_and_every_line_parses(tmp_path, env):
    env["OPENAI_API_KEY"] = _SECRET
    events = [
        {"type": "thread.started", "thread_id": "t"},
        # A JSON string holding a newline is the two characters backslash and n on the line.
        {"type": "error", "message": f"line one\nline two, key {_SECRET}"},
        {"type": "turn.failed", "error": {"message": "x"}},
    ]
    stdout = "".join(json.dumps(event) + "\n" for event in events)
    stderr = f"first\nsecond {_SECRET}\n"
    _run(tmp_path, env, runner=_Recorder((1, stdout, stderr)))
    out = (tmp_path / "out").resolve()
    written = (out / "no_output" / "stdout.jsonl").read_text()
    assert written.endswith("\n")
    lines = written.splitlines()
    assert len(lines) == len(events)
    parsed = [json.loads(line) for line in lines]
    assert [event["type"] for event in parsed] == [event["type"] for event in events]
    assert parsed[1]["message"] == "line one\nline two, key [redacted]"
    assert _SECRET not in written
    assert (out / "no_output" / "stderr.txt").read_text() == "first\nsecond [redacted]\n"


def test_a_runner_that_cannot_start_is_recorded_not_raised(tmp_path, env):
    manifest, _recorder, _said, _events = _run(
        tmp_path, env, runner=_Recorder(answer=FileNotFoundError("no codex"))
    )
    out = (tmp_path / "out").resolve()
    assert manifest["cases"]["no_output"]["exit_status"] == -1
    assert "could not start" in (out / "no_output" / "stderr.txt").read_text()


def _lines(*events):
    return "".join(json.dumps(event) + "\n" for event in events)


@pytest.mark.parametrize(
    ("stdout", "started", "after"),
    [
        (
            _lines({"type": "thread.started"}, {"type": "turn.started"}, {"type": "error"}),
            True,
            True,
        ),
        (_lines({"type": "thread.started"}, {"type": "turn.failed"}), True, True),
        (_lines({"type": "error"}, {"type": "thread.started"}), True, False),
        (_lines({"type": "error"}), False, False),
        (_lines({"type": "thread.started"}, {"type": "turn.completed"}), True, None),
        ("", False, None),
    ],
)
def test_stream_shape_says_whether_the_error_came_after_the_thread_started(stdout, started, after):
    shape = stream_shape(stdout)
    assert shape["thread_started"] is started
    assert shape["error_after_thread_started"] is after


def test_stream_shape_keeps_types_and_never_event_text():
    stdout = _lines({"type": "error", "message": "model not found"}) + "not json\n[1]\n\n"
    shape = stream_shape(stdout)
    assert shape["event_types"] == ["error", "<other>", "<other>"]
    assert "model not found" not in json.dumps(shape)


def test_the_manifest_records_the_stream_shape_per_case(tmp_path, env):
    stdout = _lines({"type": "thread.started"}, {"type": "error", "message": "x"})
    manifest, _recorder, _said, _events = _run(tmp_path, env, runner=_Recorder((1, stdout, "")))
    case = manifest["cases"]["unknown_model"]
    assert case["event_types"] == ["thread.started", "error"]
    assert case["error_after_thread_started"] is True
    assert "home_entries_added" not in case


def test_the_hook_case_lists_what_the_run_added_to_the_scratch_home(tmp_path, env):
    class _Installs(_Recorder):
        def __call__(self, argv, env, cwd):
            if "--ignore-user-config" not in argv:
                (Path(env["CODEX_HOME"]) / "packages").mkdir()
            return super().__call__(argv, env, cwd)

    manifest, _recorder, _said, _events = _run(tmp_path, env, hook_case=True, runner=_Installs())
    assert manifest["cases"]["hook_blocked"]["home_entries_added"] == ["packages"]


def test_case_argv_keeps_the_prompt_after_the_option_terminator(tmp_path):
    argv = case_argv("/bin/codex", CASES[0], tmp_path)
    assert argv[-2:] == ["--", CASES[0].prompt]


def test_main_reports_a_refusal_on_stderr_and_exits_2(tmp_path, env, monkeypatch, capsys):
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep").write_text("x")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert main(["--out", str(out)]) == 2
    assert "is not an empty directory" in capsys.readouterr().err
