from lionagi.testing import LionAGIMockFactory


class _RecordingProvider:
    name = "recorder"

    def __init__(self):
        self.writebacks = []

    async def provide(self, branch, instruction):
        return None

    async def writeback(self, branch, action_responses):
        self.writebacks.append((branch, action_responses))


async def test_operate_runs_context_provider_writeback_after_actions():
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    branch = LionAGIMockFactory.create_mocked_branch(
        response=(
            '{"action_required": true, "action_requests": '
            '[{"function": "add", "arguments": {"a": 1, "b": 2}}]}'
        ),
        tools=[add],
    )
    provider = _RecordingProvider()
    branch.providers.register(provider)

    result = await branch.operate(instruction="add", actions=True)

    assert len(provider.writebacks) == 1
    writeback_branch, action_responses = provider.writebacks[0]
    assert writeback_branch is branch
    assert len(action_responses) == 1
    assert action_responses[0].function == "add"
    assert action_responses[0].output == 3
    assert result.action_responses == action_responses
