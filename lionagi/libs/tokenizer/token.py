class BaseToken:
    def __init__(self, type_, value):
        self.type = type_
        self.value = value

    def __repr__(self):
        return f"BaseDirectiveToken({self.type}, {self.value})"