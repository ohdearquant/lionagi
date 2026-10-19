# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A failed checkpoint write must preserve the last readable snapshot."""

from pathlib import Path

import pytest

from lionagi.cli.orchestrate import _checkpoint


@pytest.mark.parametrize("operation", ["record", "record_spawned", "flush"])
@pytest.mark.parametrize("failure_stage", ["write", "replace"])
async def test_failed_checkpoint_write_preserves_target_and_releases_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, failure_stage: str
):
    target = tmp_path / "checkpoint.json"
    writer = _checkpoint.CheckpointWriter(
        path=target, session_id="session", prompt="task", plan=[], config={}
    )
    await writer.record("seed", status="completed", response="durable")
    before = target.read_bytes()
    previous = _checkpoint.load_checkpoint(target)
    failure = OSError("checkpoint write unavailable")
    write_text = Path.write_text
    replace = _checkpoint.os.replace
    reached = []

    def failing_write(path, data, *args, **kwargs):
        if path.parent == tmp_path and path.name.startswith("checkpoint."):
            reached.append("write")
            # The scratch file can be incomplete without exposing it as the target.
            write_text(path, "{")
            raise failure
        return write_text(path, data, *args, **kwargs)

    def failing_replace(source, destination, *args, **kwargs):
        if Path(destination) == target:
            reached.append("replace")
            raise failure
        return replace(source, destination, *args, **kwargs)

    with monkeypatch.context() as patch:
        if failure_stage == "write":
            patch.setattr(Path, "write_text", failing_write)
        else:
            patch.setattr(_checkpoint.os, "replace", failing_replace)

        with pytest.raises(OSError) as raised:
            if operation == "record":
                await writer.record("pending", status="completed", response="new")
            elif operation == "record_spawned":
                await writer.record_spawned("child", status="completed", response="new")
            else:
                writer.flow_context = {"pending": "new"}
                await writer.flush()

        assert raised.value is failure
        assert reached == [failure_stage]
        assert target.read_bytes() == before
        assert _checkpoint.load_checkpoint(target) == previous

    # The same writer can make progress after the filesystem operation recovers.
    await writer.record(
        "after", status="completed", response="recovered", flow_context={"recovered": True}
    )
    recovered = _checkpoint.load_checkpoint(target)
    assert recovered["ops"]["seed"] == previous["ops"]["seed"]
    assert recovered["ops"]["after"]["response"] == "recovered"
    assert recovered["flow_context"] == {"recovered": True}
