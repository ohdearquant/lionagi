# SPDX-License-Identifier: Apache-2.0
"""Whole episodes against the environment class."""

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from agent_workbench.models import WorkbenchAction
from agent_workbench.server import agent_workbench_environment as workbench
from agent_workbench.server.agent_workbench_environment import MAX_STEPS, AgentWorkbenchEnvironment
from agent_workbench.server.tasks import TASKS, _case_results, build_task

from .test_tasks import solve

FIXED = {
    "merge_intervals": """def merge_intervals(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result
""",
    "parse_duration": """def parse_duration(text):
    import re
    return sum(int(v) * {"h": 3600, "m": 60, "s": 1}[u] for v, u in re.findall(r"(\\d+)([hms])", text))
""",
    "rolling_mean": """def rolling_mean(values, window):
    return [sum(values[i:i + window]) / window for i in range(len(values) - window + 1)]
""",
    "slugify": """def slugify(title):
    import re
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
""",
    "top_words": """def top_words(text, k):
    import re
    from collections import Counter
    counts = Counter(re.findall(r"[a-z]+", text.lower()))
    return [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]
""",
}


def act(env, tool, **args):
    return env.step(WorkbenchAction(tool=tool, args=args))


def answer_text(task, want):
    if isinstance(want, list):
        return ", ".join(want)
    if isinstance(want, str):
        return want
    return f"{want:.{task.decimals}f}" if task.decimals else str(int(want))


@pytest.fixture()
def env():
    e = AgentWorkbenchEnvironment()
    yield e
    e.close()


@pytest.mark.parametrize("task_id", sorted(TASKS))
def test_a_careful_episode_earns_full_reward(env, task_id):
    obs = env.reset(task_id=task_id)
    assert obs.task and obs.tools and not obs.done and obs.steps_left == MAX_STEPS
    task = build_task(task_id)
    first = sorted(task.files)[0]
    assert "1 " in act(env, "read_file", path=first).output or task.files[first] == ""
    if task.answer_kind == "code":
        assert "passed" in act(env, "run_tests").output
        act(env, "write_file", path="solution.py", content=FIXED[task.expected])
        assert "FAIL" not in act(env, "run_tests").output
        final = act(env, "submit", answer="done")
    elif task.answer_kind == "constraints":
        import re

        words = re.findall(r"'(\w+)'", task.files["brief.txt"])
        text = " ".join(words).capitalize()
        assert "correct" in act(env, "check", answer=text).output
        final = act(env, "submit", answer=text)
    else:
        text = answer_text(task, solve(task))
        assert act(env, "check", answer=text).output.startswith("check: correct")
        final = act(env, "submit", answer=text)
    assert final.done and final.reward == 1.0
    assert act(env, "list_files").done


SOFTWARE = [t for t in sorted(TASKS) if TASKS[t].domain == "software"]


@pytest.mark.parametrize("task_id", SOFTWARE)
def test_the_unfixed_code_earns_no_correctness(env, task_id):
    # The unfixed code already passes some grader cases; only cases it failed earn credit.
    task = build_task(task_id)
    assert task.code_failing and len(task.code_failing) < len(task.code_cases)
    # The grader cases are not the visible tests.
    assert not {repr(c) for c in task.code_visible} & {repr(c) for c in task.code_cases}
    source, name = task.files["solution.py"], task.expected
    visible_pass = all(_case_results(source, name, [(a, w) for _, a, w in task.code_visible]))
    env.reset(task_id=task_id)
    act(env, "run_tests")
    final = act(env, "submit", answer="done")
    assert final.done and final.metadata["correctness"] == 0.0
    # No refused calls; verified only where the unfixed code already passes the visible tests.
    assert final.reward == pytest.approx(0.1 + 0.1 * visible_pass)


def _wrapped(task, body: str, table: list) -> str:
    """*body* defining the task's function, then a wrapper answering the (args, want) inputs in
    *table* itself."""
    name = task.expected
    import json

    entries = {json.dumps(list(args)): want for args, want in table}
    return (
        body
        + f"\nimport json as _json\n_inner = {name}\n_TABLE = {entries!r}\n"
        + f"def {name}(*args):\n"
        + "    key = _json.dumps(list(args))\n"
        + "    return _TABLE[key] if key in _TABLE else _inner(*args)\n"
    )


@pytest.mark.parametrize("task_id", SOFTWARE)
def test_code_that_special_cases_the_visible_tests_earns_nothing(env, task_id):
    task = build_task(task_id)
    lookup = [(args, want) for _, args, want in task.code_visible]
    env.reset(task_id=task_id)
    act(
        env,
        "write_file",
        path="solution.py",
        content=_wrapped(task, task.files["solution.py"], lookup),
    )
    act(env, "run_tests")
    final = act(env, "submit", answer="done")
    assert final.metadata["correctness"] == 0.0


@pytest.mark.parametrize("task_id", SOFTWARE)
def test_a_fix_that_breaks_a_case_the_original_passed_earns_nothing(env, task_id):
    task = build_task(task_id)
    kept = next(i for i in range(len(task.code_cases)) if i not in task.code_failing)
    _, args, _ = task.code_cases[kept]
    fixed = FIXED[task.expected]
    env.reset(task_id=task_id)
    act(env, "write_file", path="solution.py", content=_wrapped(task, fixed, [(args, "broken")]))
    final = act(env, "submit", answer="done")
    assert final.metadata["correctness"] == 0.0
    # Control: the same fix without the break earns full credit.
    env.reset(task_id=task_id)
    act(env, "write_file", path="solution.py", content=fixed)
    assert act(env, "submit", answer="done").metadata["correctness"] == 1.0


@pytest.mark.parametrize("task_id", SOFTWARE)
def test_a_fix_that_fails_a_visible_test_earns_nothing(env, task_id):
    task = build_task(task_id)
    _, args, _ = task.code_visible[0]
    env.reset(task_id=task_id)
    act(
        env,
        "write_file",
        path="solution.py",
        content=_wrapped(task, FIXED[task.expected], [(args, "broken")]),
    )
    assert act(env, "submit", answer="done").metadata["correctness"] == 0.0


def test_run_tests_runs_the_examples_as_the_task_gave_them(env):
    env.reset(task_id="software-merge_intervals-1")
    act(env, "write_file", path="test_solution.py", content="print('passed 2 of 2')\n")
    out = act(env, "run_tests").output
    assert "FAIL" in out
    final = act(env, "submit", answer="done")
    assert final.reward == pytest.approx(0.1)  # the rewritten test file verified nothing


def test_code_that_changes_its_workspace_when_called_earns_nothing(env):
    fixed = FIXED["slugify"]
    self_editing = (
        fixed
        + "\nimport pathlib\npathlib.Path(__file__).write_text(pathlib.Path(__file__).read_text() + '#')\n"
    )
    env.reset(task_id="software-slugify-4")
    act(env, "write_file", path="solution.py", content=self_editing)
    act(env, "run_tests")
    final = act(env, "submit", answer="done")
    assert final.metadata["correctness"] == 0.0
    assert final.reward == pytest.approx(0.1)
    # Control: the same fix, tested without changing itself, is verified.
    env.reset(task_id="software-slugify-4")
    act(env, "write_file", path="solution.py", content=fixed)
    act(env, "run_tests")
    assert act(env, "submit", answer="done").reward == pytest.approx(0.9)


def test_a_count_kept_between_graded_calls_fails_the_grading(env):
    """Code that counts its calls in a file could answer a case by its position, not its input."""
    counting = FIXED["slugify"].replace(
        "def slugify(title):\n",
        "def slugify(title):\n"
        "    import pathlib\n"
        "    p = pathlib.Path('.calls')\n"
        "    p.write_text(str(int(p.read_text()) + 1 if p.exists() else 1))\n",
    )
    env.reset(task_id="software-slugify-4")
    act(env, "write_file", path="solution.py", content=counting)
    assert act(env, "submit", answer="done").metadata["correctness"] == 0.0


def test_a_count_kept_in_a_file_time_fails_the_grading(env):
    counting = FIXED["slugify"].replace(
        "def slugify(title):\n",
        "def slugify(title):\n"
        "    import os\n"
        "    st = os.stat(__file__)\n"
        "    os.utime(__file__, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))\n",
    )
    env.reset(task_id="software-slugify-4")
    act(env, "write_file", path="solution.py", content=counting)
    assert act(env, "submit", answer="done").metadata["correctness"] == 0.0


def test_files_that_changed_after_a_passing_test_run_are_not_verified(env):
    env.reset(task_id="software-slugify-4")
    act(env, "write_file", path="solution.py", content=FIXED["slugify"])
    act(env, "run_tests")
    # Changed by no tool call, as a process that outlived its call could have.
    (env._ws / "notes.txt").write_text("changed")
    assert act(env, "submit", answer="done").reward == pytest.approx(0.8)


_GRADED_UNDER = "import pathlib, sys\nrunner = pathlib.Path(sys.argv[0]).name\n"


def test_a_helper_the_test_run_changes_verifies_nothing(env):
    helper = _GRADED_UNDER + (
        "if runner == '.grade_tests.py':\n"
        "    me = pathlib.Path(__file__)\n"
        "    me.write_text(me.read_text() + '#')\n"
    )
    env.reset(task_id="software-slugify-4")
    act(env, "write_file", path="helper.py", content=helper)
    act(env, "write_file", path="solution.py", content="import helper\n" + FIXED["slugify"])
    act(env, "run_tests")
    final = act(env, "submit", answer="done")
    assert final.metadata["correctness"] == 1.0
    assert final.reward == pytest.approx(0.8)


def test_a_file_changed_and_changed_back_during_a_test_run_verifies_nothing(env):
    """The tests ran on one version, the examples on another, and the file ends as it began."""
    toggling = (
        FIXED["slugify"]
        + _GRADED_UNDER
        + (
            "me = pathlib.Path(__file__)\n"
            "if runner == '.grade_tests.py':\n"
            "    me.write_text(me.read_text() + '#B\\n')\n"
            "elif runner == '.grade.py' and me.read_text().endswith('#B\\n'):\n"
            "    me.write_text(me.read_text()[:-3])\n"
        )
    )
    env.reset(task_id="software-slugify-4")
    act(env, "write_file", path="solution.py", content=toggling)
    act(env, "run_tests")
    assert (env._ws / "solution.py").read_text() == toggling
    final = act(env, "submit", answer="done")
    assert final.metadata["correctness"] == 1.0
    assert final.reward == pytest.approx(0.8)


def test_every_graded_case_starts_from_a_cleared_state(env, monkeypatch):
    events = []
    real_run, real_clear = (
        AgentWorkbenchEnvironment._run,
        AgentWorkbenchEnvironment._clear_agent_state,
    )

    def run(self, argv, raw=False):
        events.append(argv[4] if raw else "run")
        return real_run(self, argv, raw)

    def clear(self):
        events.append("clear")
        real_clear(self)

    monkeypatch.setattr(AgentWorkbenchEnvironment, "_run", run)
    monkeypatch.setattr(AgentWorkbenchEnvironment, "_clear_agent_state", clear)
    env.reset(task_id="software-slugify-4")
    act(env, "write_file", path="solution.py", content=FIXED["slugify"])
    assert act(env, "submit", answer="done").metadata["correctness"] == 1.0
    graded = [i for i, event in enumerate(events) if event == ".grade.py"]
    assert len(graded) > 3
    assert all(events[i - 1] == "clear" for i in graded)


def test_clearing_the_agent_state_kills_sweeps_and_removes_the_server_scripts(env, monkeypatch):
    env.reset(task_id="software-slugify-4")
    (env._ws / ".run.py").write_text("x")
    (env._ws / ".grade_tests.py" / "sub").mkdir(parents=True)
    os.chmod(env._ws / ".grade_tests.py", 0)
    calls = []
    found = iter([1, 0])
    monkeypatch.setattr(
        AgentWorkbenchEnvironment,
        "_kill_sandbox_uid",
        lambda self: calls.append("kill") or next(found),
    )
    monkeypatch.setattr(
        AgentWorkbenchEnvironment, "_remove_shared_tmp_files", lambda self: calls.append("sweep")
    )
    env._ids = (os.getuid(), os.getgid())
    try:
        env._clear_agent_state()
    finally:
        env._ids = None
    assert calls == ["kill", "kill", "sweep"]
    assert not os.path.lexists(env._ws / ".run.py")
    assert not os.path.lexists(env._ws / ".grade_tests.py")


@pytest.mark.parametrize("task_id", SOFTWARE)
def test_an_edit_that_fixes_nothing_earns_no_correctness(env, task_id):
    env.reset(task_id=task_id)
    source = build_task(task_id).files["solution.py"]
    act(env, "write_file", path="solution.py", content=source + "\n# reviewed\n")
    final = act(env, "submit", answer="done")
    assert final.metadata["correctness"] == 0.0


def test_tests_run_before_the_last_change_do_not_count_as_verifying_it(env):
    env.reset(task_id="software-slugify-4")
    act(env, "read_file", path="solution.py")
    act(env, "run_tests")
    act(env, "write_file", path="solution.py", content=FIXED["slugify"])
    final = act(env, "submit", answer="done")
    assert final.metadata["correctness"] == 1.0
    assert final.reward == pytest.approx(0.9)


def test_listing_the_files_is_not_reading_them(env):
    task = build_task("math-divisible-1")
    right = answer_text(task, solve(task))
    env.reset(task_id=task.spec.task_id)
    act(env, "list_files")
    act(env, "check", answer=right)
    assert act(env, "submit", answer=right).reward == pytest.approx(0.9)


def test_check_says_only_whether_the_answer_is_correct(env):
    # A reason such as "some items are missing" against a single candidate would say whether that
    # candidate belongs, and two checks would score items without solving the task.
    task = build_task("security-ioc-3")
    one = solve(task)[0]
    env.reset(task_id=task.spec.task_id)
    assert act(env, "check", answer=one).output == "check: not correct (1 checks left)"
    assert act(env, "check", answer="not-an-item").output == "check: not correct (0 checks left)"
    final = act(env, "submit", answer=one)
    assert final.reward == pytest.approx(0.7 * final.metadata["correctness"] + 0.1)


def test_checking_one_answer_does_not_verify_a_different_one(env):
    task = build_task("math-divisible-1")
    env.reset(task_id=task.spec.task_id)
    act(env, "read_file", path=sorted(task.files)[0])
    act(env, "check", answer="x")
    final = act(env, "submit", answer=answer_text(task, solve(task)))
    assert final.metadata["correctness"] == 1.0
    assert final.reward == pytest.approx(0.9)


def test_example_action_ends_every_task_with_a_reward_in_range(env):
    for task_id in sorted(TASKS):
        env.reset(task_id=task_id)
        final = act(env, "submit", answer="example")
        assert final.done and 0.0 <= final.reward <= 1.0


@pytest.mark.parametrize("task_id", sorted(TASKS))
def test_the_trainer_finish_action_earns_nothing_after_good_process(env, task_id):
    env.reset(task_id=task_id)
    act(env, "list_files")
    if build_task(task_id).answer_kind == "code":
        act(env, "run_tests")
    else:
        act(env, "check", answer="1")
    final = act(env, "finish")
    assert final.done and final.reward == 0.0


def test_a_blank_answer_is_refused_and_the_episode_goes_on(env):
    env.reset(task_id="math-divisible-1")
    for blank in ("", "   "):
        obs = act(env, "submit", answer=blank)
        assert obs.error and not obs.done
    act(env, "list_files")
    assert act(env, "check", answer="").error


def test_wrong_answer_with_good_process_is_capped(env):
    task_id = "math-divisible-1"
    env.reset(task_id=task_id)
    act(env, "read_file", path=sorted(build_task(task_id).files)[0])
    act(env, "check", answer="1")
    final = act(env, "submit", answer="1")
    # Read the input and made no refused call; an answer check called wrong verifies nothing.
    assert final.reward == pytest.approx(0.2)


def test_invalid_calls_are_refused_and_three_in_a_row_end_the_episode(env):
    env.reset(task_id="office-expenses-1")
    obs = act(env, "delete_everything")
    assert obs.error and not obs.done
    obs = act(env, "read_file", path="../../etc/passwd")
    assert "inside the workspace" in obs.error and not obs.done
    obs = act(env, "read_file", path="expenses.csv", bogus=1)
    assert obs.done and obs.reward == 0.0


def test_a_valid_call_resets_the_invalid_run(env):
    env.reset(task_id="office-expenses-1")
    act(env, "nope")
    act(env, "nope")
    assert not act(env, "list_files").done
    assert not act(env, "nope").done


def test_tool_budget_ends_the_episode(env):
    env.reset(task_id="math-paths-2")
    obs = None
    for _ in range(MAX_STEPS):
        obs = act(env, "list_files")
    assert obs.done and obs.reward == 0.0


def test_check_is_limited(env):
    env.reset(task_id="math-paths-2")
    act(env, "check", answer="1")
    act(env, "check", answer="2")
    assert "at most" in act(env, "check", answer="3").error


def test_run_python_sees_the_workspace(env):
    env.reset(task_id="science-gas-1")
    out = act(env, "run_python", code="print(open('lab_notes.txt').read().splitlines()[0])").output
    assert "Lab notebook" in out and "exit code 0" in out


def test_a_symlink_planted_at_the_script_name_is_not_followed(env, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    env.reset(task_id="science-gas-1")
    act(
        env,
        "run_python",
        code=f"import os; os.remove('.run.py'); os.symlink({str(victim)!r}, '.run.py')",
    )
    act(env, "run_python", code="print('hello')")
    assert victim.read_text() == "keep"


def test_symlink_escape_is_refused(env, tmp_path):
    env.reset(task_id="science-gas-1")
    act(env, "run_python", code="import os; os.symlink('/etc', 'etc_link')")
    assert "inside the workspace" in act(env, "read_file", path="etc_link/hosts").error


def test_reset_rejects_an_unknown_task(env):
    with pytest.raises(ValueError):
        env.reset(task_id="nope")


def test_a_refused_reset_leaves_the_running_episode_intact(env):
    env.reset(task_id="science-gas-1")
    ws = env._ws
    with pytest.raises(ValueError):
        env.reset(task_id="nope")
    assert env._ws == ws and ws.exists()
    assert "lab_notes.txt" in act(env, "list_files").output


@pytest.mark.parametrize("path", [".", "./", "", "/etc/hosts", "a/../lab_notes.txt"])
def test_a_write_that_names_no_workspace_file_is_refused(env, path):
    env.reset(task_id="science-gas-1")
    obs = act(env, "write_file", path=path, content="x")
    assert obs.error and not obs.done


def test_writing_over_a_directory_is_refused(env):
    env.reset(task_id="science-gas-1")
    act(env, "run_python", code="import os; os.mkdir('notes')")
    assert "directory" in act(env, "write_file", path="notes", content="x").error


def test_a_symlink_inside_the_workspace_is_neither_followed_nor_listed(env, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret" * 1000)
    env.reset(task_id="science-gas-1")
    before = (env._ws / "lab_notes.txt").read_text()
    act(
        env,
        "run_python",
        code=f"import os; os.symlink('lab_notes.txt', 'alias.txt'); os.symlink({str(outside)!r}, 'leak.txt')",
    )
    listing = act(env, "list_files").output
    assert "alias.txt" not in listing and "leak.txt" not in listing and "6000" not in listing
    assert "symlink" in act(env, "write_file", path="alias.txt", content="overwritten").error
    assert "symlink" in act(env, "read_file", path="alias.txt").error
    assert (env._ws / "lab_notes.txt").read_text() == before
    assert outside.read_text() == "secret" * 1000


def test_each_live_episode_gets_its_own_sandbox_uid(monkeypatch):
    monkeypatch.setattr(workbench.os, "geteuid", lambda: 0)
    a, b = workbench._claim_sandbox_uid(), workbench._claim_sandbox_uid()
    try:
        assert a is not None and b is not None and a != b
        workbench._release_sandbox_uid(a)
        c = workbench._claim_sandbox_uid()
        # The uid just released goes to the back of the rotation.
        assert c not in (a, b)
        workbench._release_sandbox_uid(c)
    finally:
        workbench._release_sandbox_uid(a)
        workbench._release_sandbox_uid(b)


def _no_root_side_effects(monkeypatch):
    # Pretend to be root for the uid bookkeeping only; nothing here may chown or scan /proc.
    monkeypatch.setattr(workbench.os, "geteuid", lambda: 0)
    monkeypatch.setattr(AgentWorkbenchEnvironment, "_kill_sandbox_uid", lambda self: 0)
    monkeypatch.setattr(AgentWorkbenchEnvironment, "_remove_shared_tmp_files", lambda self: None)


def test_a_finished_episode_releases_its_uid_and_workspace(monkeypatch):
    _no_root_side_effects(monkeypatch)
    monkeypatch.setattr(AgentWorkbenchEnvironment, "_own_workspace", lambda self: None)
    held = set(workbench._uids_in_use)
    env = AgentWorkbenchEnvironment()
    env.reset(task_id="math-divisible-1")
    ws, uid = env._ws, env._ids[0]
    assert uid in workbench._uids_in_use
    assert act(env, "submit", answer="1").done
    assert not ws.exists() and env._ids is None
    assert workbench._uids_in_use == held
    # A stateless caller builds a fresh instance per step and may never close it.
    for _ in range(3):
        assert (
            AgentWorkbenchEnvironment()
            .step(WorkbenchAction(tool="submit", args={"answer": "1"}))
            .done
        )
    assert workbench._uids_in_use == held


def test_a_reset_that_fails_part_way_releases_what_it_took(monkeypatch, tmp_path):
    _no_root_side_effects(monkeypatch)
    monkeypatch.setattr(workbench.tempfile, "tempdir", str(tmp_path))

    def refuse(self):
        raise PermissionError("chown refused")

    monkeypatch.setattr(AgentWorkbenchEnvironment, "_own_workspace", refuse)
    held = set(workbench._uids_in_use)
    env = AgentWorkbenchEnvironment()
    with pytest.raises(PermissionError):
        env.reset(task_id="math-divisible-1")
    assert workbench._uids_in_use == held
    assert list(tmp_path.iterdir()) == []
    assert env._task is None and env._ws is None and env._ids is None


def test_files_the_episode_uid_left_in_shared_tmp_are_removed(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    (shared / "cache").mkdir(parents=True)
    (shared / "cache" / "answer.txt").write_text("42")
    (shared / "note.txt").write_text("42")
    monkeypatch.setattr(workbench, "_SHARED_TMP_DIRS", (str(shared),))
    env = AgentWorkbenchEnvironment()
    # Control: another uid's files stay.
    env._ids = (os.getuid() + 1, os.getgid())
    env._remove_shared_tmp_files()
    assert sorted(p.name for p in shared.iterdir()) == ["cache", "note.txt"]
    env._ids = (os.getuid(), os.getgid())
    env._remove_shared_tmp_files()
    assert list(shared.iterdir()) == []
    env._ids = None


def test_a_workspace_replaced_by_a_symlink_is_never_followed(env, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    (outside / "secret.txt").write_text("secret")
    env.reset(task_id="science-gas-1")
    act(
        env,
        "run_python",
        code="import os, shutil; ws = os.getcwd(); os.chdir('/'); shutil.rmtree(ws); "
        f"os.symlink({str(outside)!r}, ws)",
    )
    assert os.path.islink(env._ws)  # the replacement happened
    home = env._home
    answers = [
        act(env, "list_files"),
        act(env, "read_file", path="secret.txt"),
        act(env, "write_file", path="secret.txt", content="overwritten"),
    ]
    assert all("no longer a directory" in (obs.error or obs.output) for obs in answers)
    assert answers[-1].done and not home.exists()
    assert (outside / "secret.txt").read_text() == "secret"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755


@pytest.mark.parametrize("tool", ["run_python", "run_tests", "submit"])
def test_running_code_in_a_workspace_replaced_by_a_symlink_is_refused(env, tmp_path, tool):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "solution.py").write_text("raise SystemExit('ran outside')\n")
    env.reset(task_id="software-slugify-4")
    act(
        env,
        "run_python",
        code="import os, shutil; ws = os.getcwd(); os.chdir('/'); shutil.rmtree(ws); "
        f"os.symlink({str(outside)!r}, ws)",
    )
    args = (
        {"code": "print(1)"}
        if tool == "run_python"
        else {"answer": "x"}
        if tool == "submit"
        else {}
    )
    obs = act(env, tool, **args)
    assert "no longer a directory" in obs.error and not obs.done
    assert sorted(p.name for p in outside.iterdir()) == ["solution.py"]


def test_a_script_the_server_cannot_write_is_refused(env, monkeypatch):
    env.reset(task_id="science-gas-1")
    real_open = workbench.os.open

    def refuse_scripts(path, *args, **kwargs):
        if str(path).endswith(".run.py"):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(workbench.os, "open", refuse_scripts)
    obs = act(env, "run_python", code="print(1)")
    assert "cannot prepare .run.py" in obs.error and not obs.done


def test_a_command_that_cannot_start_is_refused(env, monkeypatch):
    env.reset(task_id="science-gas-1")

    def no_start(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(workbench.subprocess, "Popen", no_start)
    obs = act(env, "run_python", code="print(1)")
    assert "cannot run in the workspace" in obs.error and not obs.done


def test_a_run_whose_workspace_went_away_is_refused_before_it_starts(env, monkeypatch):
    env.reset(task_id="science-gas-1")
    started = []
    monkeypatch.setattr(workbench.subprocess, "Popen", lambda *a, **k: started.append(a))
    os.rename(env._ws, env._ws.with_name("moved"))
    with pytest.raises(workbench._RefusalError, match="workspace directory is gone"):
        env._run(["true"])
    assert started == []


def test_removing_a_workspace_home_replaced_by_a_symlink_leaves_the_target_alone(env, tmp_path):
    # Outside the image the agent runs as the server's own user and can replace even the home.
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True, mode=0o755)
    (outside / "keep.txt").write_text("keep")
    env.reset(task_id="science-gas-1")
    home = env._home
    act(
        env,
        "run_python",
        code="import os, shutil; home = os.path.dirname(os.getcwd()); os.chdir('/'); "
        f"shutil.rmtree(home); os.symlink({str(outside)!r}, home)",
    )
    assert os.path.islink(home)  # the replacement happened
    env.close()
    assert not os.path.lexists(home)
    assert (outside / "keep.txt").read_text() == "keep"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert stat.S_IMODE((outside / "sub").stat().st_mode) == 0o755


def test_the_episode_uid_cannot_write_the_directory_holding_its_workspace(monkeypatch):
    _no_root_side_effects(monkeypatch)
    chowned = []
    monkeypatch.setattr(
        workbench.os, "chown", lambda path, uid, gid, **kw: chowned.append(Path(path))
    )
    env = AgentWorkbenchEnvironment()
    try:
        env.reset(task_id="science-gas-1")
        home, ws = env._home, env._ws
        assert ws.parent == home
        assert stat.S_IMODE(home.stat().st_mode) == 0o711
        assert stat.S_IMODE(ws.stat().st_mode) == 0o700
        assert ws in chowned and home not in chowned
    finally:
        env.close()
    assert not home.exists()


def test_a_workspace_the_agent_locked_is_given_back(env):
    env.reset(task_id="science-gas-1")
    act(env, "run_python", code="import os; os.chmod('.', 0)")
    out = act(env, "run_python", code="print('still here')").output
    assert "still here" in out and "exit code 0" in out


def test_a_workspace_the_agent_removed_is_refused_rather_than_an_error(env):
    env.reset(task_id="software-slugify-4")
    act(
        env,
        "run_python",
        code="import os, shutil; ws = os.getcwd(); os.chdir('/'); shutil.rmtree(ws)",
    )
    for tool in ["run_tests", "run_python"]:
        args = {"code": "print(1)"} if tool == "run_python" else {}
        obs = act(env, tool, **args)
        assert "workspace directory is gone" in obs.error and not obs.done


def test_a_locked_directory_at_the_script_name_is_cleared(env):
    env.reset(task_id="science-gas-1")
    act(
        env,
        "run_python",
        code="import os; os.remove('.run.py'); os.makedirs('.run.py/sub'); "
        "open('.run.py/sub/f', 'w').write('x'); os.chmod('.run.py/sub', 0); os.chmod('.run.py', 0)",
    )
    out = act(env, "run_python", code="print('ran')").output
    assert "ran" in out and "exit code 0" in out


@pytest.mark.parametrize("path", [".grade.py/sub", ".run.py/x", "notes/.grade_tests.py"])
def test_a_reserved_name_is_refused_anywhere_in_a_path(env, path):
    env.reset(task_id="science-gas-1")
    assert "reserved" in act(env, "write_file", path=path, content="x").error
    assert not (env._ws / Path(path).parts[0]).exists()


def test_cleanup_kills_until_none_is_left_then_sweeps_tmp_before_removing_the_workspace(
    monkeypatch,
):
    _no_root_side_effects(monkeypatch)
    monkeypatch.setattr(AgentWorkbenchEnvironment, "_own_workspace", lambda self: None)
    env = AgentWorkbenchEnvironment()
    env.reset(task_id="math-divisible-1")
    ws = env._ws
    calls = []
    found = iter([2, 1, 0, 5])
    monkeypatch.setattr(
        AgentWorkbenchEnvironment,
        "_kill_sandbox_uid",
        lambda self: calls.append("kill") or next(found),
    )
    monkeypatch.setattr(
        AgentWorkbenchEnvironment,
        "_remove_shared_tmp_files",
        lambda self: calls.append(("sweep", ws.exists())),
    )
    env.close()
    assert calls == ["kill", "kill", "kill", ("sweep", True)]
    assert not ws.exists()


def test_the_shared_tmp_sweep_reaches_every_depth_and_removes_only_the_uids_entries(
    tmp_path, monkeypatch
):
    shared = tmp_path / "shared"
    (shared / "theirs_dir").mkdir(parents=True)
    (shared / "theirs_dir" / "mine.txt").write_text("42")
    (shared / "theirs_dir" / "theirs.txt").write_text("keep")
    # World-writable, so the uid could have written in it.
    os.chmod(shared / "theirs_dir", 0o777)  # noqa: S103
    # Another episode's home: the uid cannot write in it, so the walk does not enter it.
    (shared / "their_home").mkdir()
    (shared / "their_home" / "mine.txt").write_text("keep")
    # The uid's own directory, locked: its files still go.
    (shared / "locked_mine" / "sub").mkdir(parents=True)
    (shared / "locked_mine" / "sub" / "secret.txt").write_text("42")
    os.chmod(shared / "locked_mine" / "sub", 0o500)
    os.chmod(shared / "locked_mine", 0)
    (shared / "my_dir" / "deep").mkdir(parents=True)
    (shared / "my_dir" / "deep" / "mine.txt").write_text("42")
    (shared / "my_shared_dir").mkdir()
    (shared / "my_shared_dir" / "theirs.txt").write_text("keep")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "mine.txt").write_text("keep")
    os.symlink(outside, shared / "link")
    monkeypatch.setattr(workbench, "_SHARED_TMP_DIRS", (str(shared),))
    # Files here all belong to the test's uid; these names stand for another episode's.
    real_stat = os.stat

    def as_another_uid(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if isinstance(path, str) and os.path.basename(path) in {
            "theirs_dir",
            "theirs.txt",
            "their_home",
        }:
            fields = list(st[:10])
            fields[4] = os.getuid() + 2
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(workbench.os, "stat", as_another_uid)
    chowned = []
    monkeypatch.setattr(
        workbench.os, "chown", lambda path, uid, gid, **kw: chowned.append((path, uid, gid))
    )

    def tree():
        return sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))

    before = tree()
    env = AgentWorkbenchEnvironment()
    env._ids = (os.getuid() + 3, os.getgid())  # control: a uid that owns nothing here
    env._remove_shared_tmp_files()
    assert tree() == before
    env._ids = (os.getuid(), os.getgid())
    env._remove_shared_tmp_files()
    env._ids = None
    assert tree() == [
        "outside",
        "outside/mine.txt",
        "shared",
        "shared/my_shared_dir",
        "shared/my_shared_dir/theirs.txt",
        "shared/their_home",
        "shared/their_home/mine.txt",
        "shared/theirs_dir",
        "shared/theirs_dir/theirs.txt",
    ]
    assert chowned == [("my_shared_dir", 0, 0)]
    # Handed on as a sticky directory anyone can write in, so the other uid keeps its access.
    assert stat.S_IMODE((shared / "my_shared_dir").stat().st_mode) == 0o1777


@pytest.mark.parametrize("path", ["a\x00b", "a" * 300, "locked/f"])
@pytest.mark.parametrize("tool", ["read_file", "write_file"])
def test_a_path_the_filesystem_rejects_is_refused(env, tool, path):
    env.reset(task_id="science-gas-1")
    act(env, "run_python", code="import os; os.mkdir('locked', 0)")
    args = {"path": path} if tool == "read_file" else {"path": path, "content": "x"}
    obs = act(env, tool, **args)
    assert obs.error and not obs.done


def test_a_workspace_with_a_locked_directory_is_still_removed(env):
    env.reset(task_id="science-gas-1")
    act(
        env,
        "run_python",
        code="import os; os.makedirs('a/b'); os.chmod('a/b', 0); os.chmod('a', 0)",
    )
    ws = env._ws
    env.reset(task_id="science-gas-1")
    assert not ws.exists()


@pytest.mark.parametrize("tool", ["read_file", "write_file"])
def test_a_fifo_is_refused_without_blocking(env, tool):
    env.reset(task_id="science-gas-1")
    act(env, "run_python", code="import os; os.mkfifo('pipe.txt')")
    args = {"path": "pipe.txt"} if tool == "read_file" else {"path": "pipe.txt", "content": "x"}
    started = time.monotonic()
    obs = act(env, tool, **args)
    assert obs.error and time.monotonic() - started < 5


def test_a_tree_past_path_max_or_behind_a_locked_directory_lists_and_is_removed(env):
    env.reset(task_id="science-gas-1")
    act(
        env,
        "run_python",
        code="import os\nfor _ in range(25):\n"
        "    open('f' * 200, 'w').write('x'); os.mkdir('d' * 200); os.chdir('d' * 200)\n",
    )
    act(
        env,
        "run_python",
        code="import os; os.mkdir('rdir'); open('rdir/f.txt', 'w').write('x'); os.chmod('rdir', 0o400)",
    )
    obs = act(env, "list_files")
    assert obs.error is None and not obs.done
    # Sorted first: the deepest file, some 5,000 characters below the workspace.
    assert obs.output.startswith("/".join(["d" * 200] * 7))
    home = env._home
    env.close()
    assert not home.exists()


def test_a_tree_deeper_than_the_walk_is_listed_and_removed_without_recursion(env):
    env.reset(task_id="software-slugify-4")
    act(
        env,
        "run_python",
        code="import os\nfor _ in range(600):\n    os.mkdir('d'); os.chdir('d')\n"
        "open('leaf.txt', 'w').write('x')\n",
    )
    home = env._home
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(300)
    try:
        obs = act(env, "list_files")
        # Too deep to hash, so nothing can be verified or graded on it.
        digest = env._workspace_digest()
        env.close()
    finally:
        sys.setrecursionlimit(limit)
    assert obs.error is None and "test_solution.py" in obs.output
    assert digest is None
    assert not home.exists()


@pytest.mark.parametrize("tool", ["write_file", "run_python"])
def test_text_utf8_cannot_encode_is_refused(env, tool):
    env.reset(task_id="science-gas-1")
    args = (
        {"path": "a.txt", "content": "\ud800"} if tool == "write_file" else {"code": "x = '\ud800'"}
    )
    obs = act(env, tool, **args)
    assert "UTF-8" in obs.error and not obs.done
    assert not (env._ws / "a.txt").exists()


def test_bytes_that_are_not_utf8_come_back_as_sendable_text(env):
    env.reset(task_id="science-gas-1")
    obs = act(env, "run_python", code="import sys; sys.stdout.buffer.write(b'\\xff\\xfe ok\\n')")
    assert obs.error is None and "ok" in obs.output
    obs.model_dump_json()  # a lone surrogate would fail here


def test_a_file_name_that_is_not_utf8_comes_back_as_sendable_text(env):
    env.reset(task_id="science-gas-1")
    try:
        os.close(os.open(os.fsencode(env._ws) + b"/\xff.txt", os.O_WRONLY | os.O_CREAT, 0o644))
    except OSError:
        pytest.skip("this filesystem refuses a file name that is not UTF-8")
    for obs in (
        act(env, "list_files"),
        act(env, "write_file", path="\udcff.txt", content="x"),
        act(env, "read_file", path="\udcff.txt"),
    ):
        assert obs.error is None
        obs.model_dump_json()  # a lone surrogate would fail here


def test_reset_without_a_task_id_picks_one(env):
    obs = env.reset()
    assert obs.metadata["task_id"] in TASKS


def test_workspaces_are_removed(env):
    env.reset(task_id="science-gas-1")
    ws = env._ws
    env.reset(task_id="science-gas-4")
    assert not os.path.exists(ws)


def test_a_process_the_agent_starts_does_not_outlive_its_call(env):
    env.reset(task_id="science-gas-1")
    code = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', "
        '\'import time; time.sleep(1.5); open("late.txt", "w").write("x")\'],'
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print('started')\n"
    )
    assert "started" in act(env, "run_python", code=code).output
    time.sleep(2.5)
    assert not (env._ws / "late.txt").exists()


def test_a_leftover_process_holding_the_output_cannot_hang_the_call(env, monkeypatch):
    monkeypatch.setattr(workbench, "PY_TIMEOUT_S", 2)
    env.reset(task_id="science-gas-1")
    code = "import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    started = time.monotonic()
    out = act(env, "run_python", code=code).output
    assert time.monotonic() - started < 10
    assert "timed out" in out


def test_a_value_json_would_change_fails_the_grader_as_it_fails_the_tests(env):
    task_id = "software-merge_intervals-1"
    n = len(build_task(task_id).code_visible)
    as_tuple = FIXED["merge_intervals"].replace("    return result\n", "    return tuple(result)\n")
    assert as_tuple != FIXED["merge_intervals"]
    env.reset(task_id=task_id)
    act(env, "write_file", path="solution.py", content=as_tuple)
    out = act(env, "run_tests").output
    assert "FAIL" in out and f"graded: 0 of {n} examples pass" in out
    assert act(env, "submit", answer="done").metadata["correctness"] == 0.0
    # Control: the same fix returning a list passes the tests and the grader alike.
    env.reset(task_id=task_id)
    act(env, "write_file", path="solution.py", content=FIXED["merge_intervals"])
    out = act(env, "run_tests").output
    assert "FAIL" not in out and f"graded: {n} of {n} examples pass" in out
    assert act(env, "submit", answer="done").metadata["correctness"] == 1.0


def test_a_json_module_in_the_workspace_does_not_stand_in_for_the_graders(env):
    import json

    task_id = "software-merge_intervals-1"
    task = build_task(task_id)
    n = len(task.code_visible)
    _, args, _ = task.code_visible[0]
    broken = _wrapped(task, FIXED[task.expected], [(args, "broken")])
    # Prints each visible example's expected value for the grader's call and exits before the
    # function runs.
    table = {json.dumps(list(a)): json.dumps(w) for _, a, w in task.code_visible}
    shim = (
        "import os, sys\n"
        f"_TABLE = {table!r}\n"
        "if len(sys.argv) > 1 and sys.argv[1] in _TABLE:\n"
        "    print(_TABLE[sys.argv[1]], flush=True)\n"
        "    os._exit(0)\n"
    )
    env.reset(task_id=task_id)
    act(env, "write_file", path="solution.py", content=broken)
    act(env, "write_file", path="json.py", content=shim)
    out = act(env, "run_tests").output
    assert f"graded: {n - 1} of {n} examples pass" in out
    assert act(env, "submit", answer="done").metadata["correctness"] == 0.0


def test_a_case_that_prints_no_value_never_matches_an_expected_null(env):
    env.reset(task_id="software-merge_intervals-1")
    act(env, "write_file", path="solution.py", content="def quiet():\n    raise SystemExit(0)\n")
    assert env._grade_cases([("quiet", (), None)]) == [False]
    # Control: a function that does return None matches it.
    act(env, "write_file", path="solution.py", content="def quiet():\n    return None\n")
    assert env._grade_cases([("quiet", (), None)]) == [True]


def test_the_unfixed_codes_cases_are_judged_by_the_graders_rule():
    from agent_workbench.server.tasks import _json_equal

    assert _json_equal([1, 2], [1, 2]) and _json_equal(1.0, 1) and _json_equal("a", "a")
    assert not _json_equal((1, 2), [1, 2])
    assert not _json_equal({1: "a"}, {"1": "a"})


def test_a_uid_another_server_process_holds_is_not_handed_out(monkeypatch):
    monkeypatch.setattr(workbench.os, "geteuid", lambda: 0)
    a = workbench._claim_sandbox_uid()
    saved = (workbench._uids_in_use, workbench._uid_lock_fds, workbench._next_uid_slot)
    try:
        # A second worker process starts with an empty pool at the same place in the rotation; only
        # the lock file tells it that a is taken.
        workbench._uids_in_use, workbench._uid_lock_fds = set(), {}
        workbench._next_uid_slot = a - workbench._SANDBOX_UID_BASE
        b = workbench._claim_sandbox_uid()
        assert b is not None and b != a
        workbench._release_sandbox_uid(b)
        # Released, b's lock is free for the next claim.
        workbench._next_uid_slot = b - workbench._SANDBOX_UID_BASE
        assert workbench._claim_sandbox_uid() == b
        workbench._release_sandbox_uid(b)
    finally:
        workbench._uids_in_use, workbench._uid_lock_fds, workbench._next_uid_slot = saved
        workbench._release_sandbox_uid(a)


def test_a_uid_lock_directory_others_can_write_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(workbench.os, "geteuid", lambda: 0)
    shared = tmp_path / "uids"
    shared.mkdir()
    shared.chmod(0o777)
    monkeypatch.setattr(workbench, "_UID_LOCK_DIR", shared)
    held = set(workbench._uids_in_use)
    with pytest.raises(RuntimeError, match="only the server's user"):
        workbench._claim_sandbox_uid()
    assert workbench._uids_in_use == held and list(shared.iterdir()) == []
