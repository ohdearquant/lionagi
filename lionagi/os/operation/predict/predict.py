async def predict(
    instruction=None,
    context=None,
    system=None,
    sender=None,
    recipient=None,
    branch=None,
    form=None,
    confidence=None,
    reason=False,
    num_sentences=1,
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
        predict=True,
        predict_num_sentences=num_sentences,
        confidence=confidence,
        reason=reason,
        image=image,
        verbose_direct=verbose,
        reflect=reflect,
        **kwargs,
    )
