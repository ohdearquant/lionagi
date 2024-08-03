from __future__ import annotations

import pandas as pd

from lion_core.converter import Converter
from lion_core.session.branch import BranchConverterRegistry as BCR


from lionagi.os.primitives import Node, pile, Pile
from .branch import Branch


from lionagi.app.Pandas.convert import to_df


# convert a pile into a pd.DataFrame
class PandasDataFrameConverter(Converter):

    @staticmethod
    def from_obj(cls, obj: pd.DataFrame, **kwargs) -> Pile:
        p = pile()
        for _, row in obj.iterrows():
            item = Node.from_dict(row.to_dict())
            p += item
        return p

    @staticmethod
    def to_obj(self: Branch, **kwargs) -> pd.DataFrame:
        dict_list = [self.messages[i].to_dict for i in self.order]
        return to_df(dict_list)


class LangChainConverter(Converter): ...


# create an index for items in a pile using llamaindex
class LlamaIndexConverter(Converter): ...


# convert an index for items in a pile using neo4j
class Neo4jConverter(Converter): ...


class BranchConverterRegistry(BCR):
    pass


BranchConverterRegistry.register("DataFrame", PandasDataFrameConverter)
