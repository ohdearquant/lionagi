# one step reason action
async def react(
    instruction=None,
    context=None,
    system=None,
    sender=None,
    recipient=None,
    branch=None,
    form=None,
    tools=None,
    tool_schema=None,
    image: str | list[str] | None = None,
    verbose=True,
    reflect=None,
    **kwargs,
):
    from lionagi.os.operator.processor.unit.unit import UnitProcessor

    unit = UnitProcessor(branch)
    return await unit.direct(
        instruction=instruction,
        context=context,
        system=system,
        form=form,
        sender=sender,
        recipient=recipient,
        reason=True,
        confidence=True,
        tools=tools or True,
        allow_action=True,
        tool_schema=tool_schema,
        image=image,
        verbose_direct=verbose,
        reflect=reflect,
        **kwargs,
    )
