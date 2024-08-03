from lion_core.rule import Rule as CoreRule


class Rule(CoreRule):

    def __init__(
        self,
        fix=None,
        apply_types=None,
        exclude_types=None,
        apply_fields=None,
        exclude_fields=None,
        **kwargs,
    ) -> None:

        super().__init__(
            fix=fix,
            apply_types=apply_types,
            exclude_types=exclude_types,
            apply_fields=apply_fields,
            exclude_fields=exclude_fields,
            **kwargs,
        )


__all__ = ["Rule"]
