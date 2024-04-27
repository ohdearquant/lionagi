from collections import deque

from lionagi.libs import AsyncUtil, convert

from ..generic import BaseNode, Node, Edge
from ..action.tool import Tool
from ..action.node import ActionNode, ActionSelection
from ..graph.graph import Graph

from .execution_mails import BaseMail
from .base_executor import BaseExecutor


class StructureExecutor(BaseExecutor, Graph):
    """
    Executes tasks within a graph structure, handling dynamic node flows and conditional edge logic.

    Attributes:
        condition_check_result (bool | None): Result of the last condition check performed during execution,
            used to control flow based on dynamic conditions.
    """

    condition_check_result: bool | None = None

    async def check_edge_condition(self, edge: Edge, executable_id, request_source):
        """
        Evaluates the condition associated with an edge, determining if execution should proceed along that edge based
        on the condition's source type.

        Args:
            edge (Edge): The edge whose condition needs to be checked.
            executable_id (str): ID of the executor handling this edge's condition.
            request_source (str): Origin of the request prompting this condition check.

        Returns:
            bool: Result of the condition evaluation.

        Raises:
            ValueError: If the source_type of the condition is invalid.
        """
        if edge.condition.source_type == "structure":
            return edge.condition(self)

        elif edge.condition.source_type == "executable":
            return await self._check_executable_condition(
                edge, executable_id, request_source
            )

        else:
            raise ValueError("Invalid source_type.")

    def _process_edge_condition(self, edge_id):
        """
        Process the condition of a edge.

        Args:
            edge_id (str): The ID of the edge.
        """
        for key in list(self.pending_ins.keys()):
            skipped_requests = deque()
            while self.pending_ins[key]:
                mail: BaseMail = self.pending_ins[key].popleft()
                if (
                    mail.category == "condition"
                    and mail.package["package"]["edge_id"] == edge_id
                ):
                    self.condition_check_result = mail.package["package"][
                        "check_result"
                    ]
                else:
                    skipped_requests.append(mail)
            self.pending_ins[key] = skipped_requests

    async def _check_executable_condition(
        self, edge: Edge, executable_id, request_source
    ):
        """
        Sends the edge's condition to an external executable for evaluation and waits for the result.

        Args:
            edge (Edge): The edge containing the condition to be checked.
            executable_id (str): ID of the executable that will evaluate the condition.
            request_source (str): Source of the request for condition evaluation.

        Returns:
            bool: The result of the condition check.
        """
        self.send(
            recipient_id=executable_id,
            category="condition",
            package={"request_source": request_source, "package": edge},
        )
        while self.condition_check_result is None:
            await AsyncUtil.sleep(0.1)
            self._process_edge_condition(edge.id_)
            continue
        check_result = self.condition_check_result
        self.condition_check_result = None
        return check_result

    async def _handle_node_id(self, mail: BaseMail):
        """
        Processes the node identified by its ID in the mail's package, ensuring it exists and retrieving the next set of
        nodes based on the current node.

        Args:
            mail (BaseMail): The mail containing the node ID and related execution details.

        Raises:
            ValueError: If the node does not exist within the structure.
        """
        if mail.package["package"] not in self.internal_nodes:
            raise ValueError(
                f"Node {mail.package} does not exist in the structure {self.id_}"
            )
        return await self._next_node(
            self.internal_nodes[mail.package["package"]],
            mail.sender_id,
            mail.package["request_source"],
        )

    async def _handle_node(self, mail: BaseMail):
        """
        Processes the node specified in the mail's package, ensuring it exists within the structure.

        Args:
            mail (BaseMail): The mail containing the node details to be processed.

        Raises:
            ValueError: If the node does not exist within the structure.
        """
        if not self.node_exist(mail.package["package"]):
            raise ValueError(
                f"Node {mail.package} does not exist in the structure {self.id_}"
            )
        return await self._next_node(
            mail.package["package"], mail.sender_id, mail.package["request_source"]
        )

    async def _handle_mail(self, mail: BaseMail):
        """
        Processes incoming mail based on its category, initiating node execution or structure operations accordingly.

        Args:
            mail (BaseMail): The mail to be processed, containing category and package information.

        Raises:
            ValueError: If the mail type is invalid for the current structure or an error occurs in handling the node ID.
        """
        if mail.category == "start":
            return self.get_heads()

        elif mail.category == "end":
            self.execute_stop = True
            return None

        elif mail.category == "node_id":
            try:
                return await self._handle_node_id(mail)
            except Exception as e:
                raise ValueError(f"Error handling node id: {e}") from e

        elif mail.category == "node" and isinstance(mail.package["package"], BaseNode):
            try:
                return await self._handle_node(mail)
            except Exception as e:
                raise ValueError(f"Error handling node: {e}") from e

        else:
            raise ValueError(f"Invalid mail type for structure")

    async def _next_node(self, current_node: Node, executable_id, request_source):
        """
        Get the next step nodes based on the current node.

        Args:
            current_node (Node): The current node.
            executable_id (str): The ID of the executable.

        Returns:
            list[Node]: The next step nodes.
        """
        next_nodes = []
        next_edges = self.get_node_edges(current_node, node_as="out")
        for edge in convert.to_list(list(next_edges.values())):
            if edge.bundle:
                continue
            if edge.condition:
                check = await self.check_edge_condition(
                    edge, executable_id, request_source
                )
                if not check:
                    continue
            node = self.internal_nodes[edge.tail]
            further_edges = self.get_node_edges(node, node_as="out")
            bundled_nodes = deque()
            for f_edge in convert.to_list(list(further_edges.values())):
                if f_edge.bundle:
                    bundled_nodes.append(self.internal_nodes[f_edge.tail])
            if bundled_nodes:
                node = self.parse_bundled_to_action(node, bundled_nodes)
            next_nodes.append(node)
        return next_nodes

    def _send_mail(self, next_nodes: list | None, mail: BaseMail):
        """
        Sends mails to the next nodes or signals the end of execution if no next nodes exist.

        Args:
            next_nodes (list | None): List of next nodes to process or None if no further nodes are available.
            mail (BaseMail): The base mail used for sending follow-up actions.
        """
        if not next_nodes:  # tail
            self.send(
                recipient_id=mail.sender_id,
                category="end",
                package={
                    "request_source": mail.package["request_source"],
                    "package": "end",
                },
            )
        else:
            if len(next_nodes) == 1:
                self.send(
                    recipient_id=mail.sender_id,
                    category="node",
                    package={
                        "request_source": mail.package["request_source"],
                        "package": next_nodes[0],
                    },
                )
            else:
                self.send(
                    recipient_id=mail.sender_id,
                    category="node_list",
                    package={
                        "request_source": mail.package["request_source"],
                        "package": next_nodes,
                    },
                )

    @staticmethod
    def parse_bundled_to_action(instruction: Node, bundled_nodes: deque):
        """
        Constructs an action node from a bundle of nodes, combining various types of nodes like ActionSelection or Tool
        into a single actionable unit.

        This method takes a bundle of nodes and systematically integrates their functionalities into a single `ActionNode`.
        This is crucial in scenarios where multiple actions or tools need to be executed sequentially or in a coordinated
        manner as part of a larger instruction flow.

        Args:
            instruction (Node): The initial instruction node leading to this action.
            bundled_nodes (deque): A deque containing nodes to be bundled into the action. These nodes typically represent
                either actions to be taken or tools to be utilized.

        Returns:
            ActionNode: An `ActionNode` that encapsulates the combined functionality of the bundled nodes, ready for execution.

        Raises:
            ValueError: If an unrecognized node type is encountered within the bundled nodes. Only `ActionSelection` and
                `Tool` nodes are valid for bundling into an `ActionNode`.
        """
        action_node = ActionNode(instruction=instruction)
        while bundled_nodes:
            node = bundled_nodes.popleft()
            if isinstance(node, ActionSelection):
                action_node.action = node.action
                action_node.action_kwargs = node.action_kwargs
            elif isinstance(node, Tool):
                action_node.tools.append(node)
            else:
                raise ValueError("Invalid bundles nodes")
        return action_node

    async def forward(self) -> None:
        """
        Process the pending incoming mails and perform the corresponding actions.
        """
        for key in list(self.pending_ins.keys()):
            while self.pending_ins[key]:
                mail: BaseMail = self.pending_ins[key].popleft()
                try:
                    if mail == "end":
                        self.execute_stop = True
                        return
                    next_nodes = await self._handle_mail(mail)
                    self._send_mail(next_nodes, mail)
                except Exception as e:
                    raise ValueError(f"Error handling mail: {e}") from e

    async def execute(self, refresh_time=1):
        """
        Executes the forward processing loop, checking conditions and processing nodes at defined intervals.

        Args:
            refresh_time (int): The delay between execution cycles, allowing for asynchronous operations to complete.

        Raises:
            ValueError: If the graph structure is found to be cyclic, which is unsupported.
        """
        if not self.acyclic:
            raise ValueError("Structure is not acyclic")

        while not self.execute_stop:
            await self.forward()
            await AsyncUtil.sleep(refresh_time)
