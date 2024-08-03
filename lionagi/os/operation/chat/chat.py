from typing import Any
from lionagi.os.operator.processor.unit.unit import UnitProcessor


async def chat(
    instruction: Any = None,
    context: Any = None,
    system: Any = None,
    sender: Any = None,
    recipient: Any = None,
    branch=None,
    form=None,
    tools=None,
    image: str | list[str] | None = None,
    **kwargs,
):
    unit = UnitProcessor(branch)

    return await unit.chat(
        instruction=instruction,
        context=context,
        system=system,
        sender=sender,
        recipient=recipient,
        form=form,
        image=image,
        tools=tools,
        **kwargs,
    )
