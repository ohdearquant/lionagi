# from experiments.executor.executor import SafeEvaluator

# class DecisionTreeManual:
#     def __init__(self, root):
#         self.root = root
#         self.evaluator = SafeEvaluator()

#     def evaluate(self, context):
#         return self._traverse_tree(self.root, context)

#     def _traverse_tree(self, node, context):
#         if isinstance(node, CompositeActionNode) or isinstance(node, ActionNode):
#             return node.execute(context)
#         elif isinstance(node, DecisionNode):
#             condition_result = self.evaluator.evaluate(node.condition, context)
#             next_node = node.true_branch if condition_result else node.false_branch
#             return self._traverse_tree(next_node, context)
#         else:
#             raise ValueError("Invalid node type.")
