from abc import ABC


class BaseFlow(ABC):

    @classmethod
    def class_name(cls) -> str:
        """
        Returns the class name of the flow.
        """
        return cls.__name__


class BaseMonoFlow(BaseFlow):

    def __init__(self, branch) -> None:
        self.branch = branch


class BasePolyFlow(BaseFlow):

    def __init__(self, session) -> None:
        self.session = session
