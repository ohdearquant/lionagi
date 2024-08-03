async def select(
    instruction=None,
    context=None,
    system=None,
    sender=None,
    recipient=None,
    choices=None,
    branch=None,
    form=None,
    confidence=None,
    reason=False,
    reflect=None,
    image: str | list[str] | None = None,
    verbose=True,
    **kwargs,
):
    from lionagi.os.operator.processor.unit.unit import UnitProcessor

    unit = UnitProcessor(branch)

    return await unit.direct(
        instruction=instruction,
        context=context,
        system=system,
        sender=sender,
        select=True,
        select_choices=choices,
        recipient=recipient,
        form=form,
        confidence=confidence,
        reason=reason,
        image=image,
        verbose_direct=verbose,
        reflect=reflect,
        **kwargs,
    )
