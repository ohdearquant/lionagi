from lionagi.libs import convert
from .edge import Edge
from .pile import BiDirectionalPile

class Relations(BiDirectionalPile):
    
    def __init__(self):
        super().__init__(Edge)

    def append(self, edge: Edge, direction="out"):
        if direction == "out": 
            self._append(edge, left=False) # out is right
        else:          
            self._append(edge, left=True) # in is left    

    @property
    def edges(self) -> dict[str, Edge]:
        return {**self.left.pile, **self.right.pile}

    @property
    def relevant_nodes(self) -> set[str]:
        return set(convert.to_list(
            [[i.head, i.tail] for i in self.edges.values()], flatten=True, dropna=True
        ))
    
    @property
    def node_edges(self):
        dict_ = {i: [] for i in self.relevant_nodes}
        for edge in self.edges.values():
            dict_[edge.head].append(edge)
            dict_[edge.tail].append(edge)
        
        return {
            k: list(set(v)) for k, v in dict_.items() 
        }