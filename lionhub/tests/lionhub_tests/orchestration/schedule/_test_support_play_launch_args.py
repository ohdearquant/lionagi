from __future__ import annotations


def _play_schedule(**overrides):
    schedule = {
        "action_kind": "play",
        "action_model": "",
        "action_prompt": "",
        "action_agent": None,
        "action_playbook": "audit",
        "action_project": None,
        "action_extra_args": [],
        "action_flow_yaml": None,
    }
    schedule.update(overrides)
    return schedule
