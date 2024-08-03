async def plan(
    instruction=None,
    context=None,
    system=None,
    sender=None,
    recipient=None,
    branch=None,
    form=None,
    image: str | list[str] | None = None,
    allow_extension=False,
    num_step=3,
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
        sender=sender,
        recipient=recipient,
        form=form,
        plan=True,
        image=image,
        verbose_direct=verbose,
        reflect=reflect,
        allow_extension=allow_extension,
        plan_num_steps=num_step,
        **kwargs,
    )
