class Node:
    """Base class for all nodes in the abstract syntax tree (AST)."""

    pass


class IfNode(Node):
    """Represents an 'IF' statement in the AST."""

    def __init__(self, condition, true_block, false_block=None):
        self.condition = condition
        self.true_block = true_block
        self.false_block = false_block


class ForNode(Node):
    """Represents a 'FOR' loop in the AST."""

    def __init__(self, iterator, collection, block):
        self.iterator = iterator
        self.collection = collection
        self.block = block


class TryNode(Node):
    """Represents a 'TRY-EXCEPT' block in the AST."""

    def __init__(self, try_block, except_block):
        self.try_block = try_block
        self.except_block = except_block


class ActionNode(Node):

    def __init__(self, action) -> None:
        self.action = action
