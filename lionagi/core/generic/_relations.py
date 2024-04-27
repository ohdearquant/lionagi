from pydantic import Field
from lionagi.libs import convert
from .abc import BaseComponent
from .edge import Edge


class Relations(BaseComponent):
    
    out: dict[str, Edge] = Field(
        title="Outgoing edges",
        default_factory=dict,
        description="The Outgoing edges of the node, {edge_id: Edge}",
    )
    
    in_: dict[str, Edge] = Field(
        title="Incoming edges",
        default_factory=dict,
        description="The Incoming edges of the node, {edge_id: Edge}",
    )

    def append(self, out: Edge=None, in_: Edge=None):
        if sum([1 if i else 0 for i in [in_, out]]) != 1:
            raise ValueError("One and only one of in_ or out must be True.")
        
        if out:
            self.out[out.id_] = out
        else:
            self.in_[in_.id_] = in_

    def pop(self, edge: Edge | str):
        edge_id = edge.id_ if isinstance(edge, Edge) else edge
        if edge_id not in self.edges:
            raise ValueError(f"Edge with id {edge_id} not found in the relations.")
        if edge_id in self.out:
            return self.out.pop(edge_id)
        return self.in_.pop(edge_id)
        


    @property
    def edges(self) -> dict[str, Edge]:
        return {**self.out, **self.in_}

    @property
    def relevant_nodes(self) -> set[str]:
        return set(convert.to_list(
            [[i.head, i.tail] for i in self.all_edges.values()], flatten=True
        ))
    