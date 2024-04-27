from .abc import BaseComponent


class Pile:
    
    """
    represents a buffer that record the ids lionagi items in it. 
    Item must be a BaseComponent hierarchy instance.
    """
    
    def __init__(
        self, 
        pile: dict[str, BaseComponent]=None
    ) -> None:
        
        self.pile = pile or {}
        
    def append(self, item: BaseComponent):
        if not isinstance(item, BaseComponent):
            raise ValueError("Item must be a BaseComponent instance.")
        self.pile[item.id_] = item

    def pop(self, id_: str) -> BaseComponent:
        return self.pile.pop(id_, None)

    def __len__(self):
        return len(self.pile)