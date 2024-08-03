from lion_core.record.report import Report as CoreReport
from lionagi.os.primitives.utils import core_to_lionagi_container


class Report(CoreReport):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.forms = core_to_lionagi_container(self.forms)

    def next_forms(self):
        return core_to_lionagi_container(super().next_forms())


__all__ = ["Report"]
