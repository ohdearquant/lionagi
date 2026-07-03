"""Two-op steering fixture graph builder: op1 (plan) -> op2 (implement)."""

from __future__ import annotations

from lionagi.operations.node import Operation, create_operation
from lionagi.protocols.graph.edge import Edge
from lionagi.protocols.graph.graph import Graph
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch
from lionagi.session.session import Session

from .fixture import IMPLEMENT_INSTRUCTION, PLAN_INSTRUCTION


def build_two_op_flow(imodel: iModel) -> tuple[Session, Graph, Operation, Operation]:
    """Wire the strictly-sequential 2-op fixture; pure, does not execute."""
    session = Session()
    plan_branch = Branch(chat_model=imodel)
    implement_branch = Branch(chat_model=imodel)
    session.include_branches(plan_branch)
    session.include_branches(implement_branch)

    op1 = create_operation("operate", parameters={"instruction": PLAN_INSTRUCTION})
    op1.branch_id = plan_branch.id
    op2 = create_operation("operate", parameters={"instruction": IMPLEMENT_INSTRUCTION})
    op2.branch_id = implement_branch.id

    graph = Graph()
    graph.add_node(op1)
    graph.add_node(op2)
    graph.add_edge(Edge(head=op1.id, tail=op2.id, label=["depends_on"]))

    return session, graph, op1, op2
