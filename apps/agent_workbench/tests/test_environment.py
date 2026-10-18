# SPDX-License-Identifier: Apache-2.0
"""Whole episodes against the environment class."""

import os
import time

import pytest

from agent_workbench.models import WorkbenchAction
from agent_workbench.server import agent_workbench_environment as workbench
from agent_workbench.server.agent_workbench_environment import MAX_STEPS, AgentWorkbenchEnvironment
from agent_workbench.server.tasks import TASKS, build_task

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


@pytest.mark.parametrize("task_id", [t for t in sorted(TASKS) if TASKS[t].domain == "software"])
def test_submitting_the_buggy_code_scores_below_a_fix(env, task_id):
    env.reset(task_id=task_id)
    final = act(env, "submit", answer="")
    assert final.done and final.reward < 0.7


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
    act(env, "list_files")
    act(env, "check", answer="1")
    final = act(env, "submit", answer="1")
    assert final.reward == pytest.approx(0.3)


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
