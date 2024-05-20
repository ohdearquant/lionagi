from typing import Union, Callable

from lionagi.libs import func_call
from lionagi.core.generic import Node
from ..bridge.langchain_.langchain_bridge import LangchainBridge
from ..bridge.llamaindex_.llama_index_bridge import LlamaIndexBridge


from ..loader.load_util import ChunkerType, file_to_chunks, _datanode_parser


def datanodes_convert(documents, chunker_type):

    for i in range(len(documents)):
        if type(documents[i]) == Node:
            if chunker_type == ChunkerType.LLAMAINDEX:
                documents[i] = documents[i].to_llama_index()
            elif chunker_type == ChunkerType.LANGCHAIN:
                documents[i] = documents[i].to_langchain()
    return documents


def text_chunker(documents, args, kwargs):

    def chunk_node(node):
        chunks = file_to_chunks(node.to_dict(), *args, **kwargs)
        func_call.lcall(chunks, lambda chunk: chunk.pop("node_id"))
        return [Node.from_obj({**chunk}) for chunk in chunks]

    return [chunk_node(doc) for doc in documents]


def chunk(
    documents,
    chunker,
    chunker_type=ChunkerType.PLAIN,
    chunker_args=None,
    chunker_kwargs=None,
    chunking_kwargs=None,
    documents_convert_func=None,
    to_lion: bool | Callable = True,
):

    if chunker_args is None:
        chunker_args = []
    if chunker_kwargs is None:
        chunker_kwargs = {}
    if chunking_kwargs is None:
        chunking_kwargs = {}

    if chunker_type == ChunkerType.PLAIN:
        return chunk_funcs[ChunkerType.PLAIN](
            documents, chunker, chunker_args, chunker_kwargs
        )

    elif chunker_type == ChunkerType.LANGCHAIN:
        return chunk_funcs[ChunkerType.LANGCHAIN](
            documents,
            documents_convert_func,
            chunker,
            chunker_args,
            chunker_kwargs,
            to_lion,
        )

    elif chunker_type == ChunkerType.LLAMAINDEX:
        return chunk_funcs[ChunkerType.LLAMAINDEX](
            documents,
            documents_convert_func,
            chunker,
            chunker_args,
            chunker_kwargs,
            to_lion,
        )

    elif chunker_type == ChunkerType.SELFDEFINED:
        return chunk_funcs[ChunkerType.SELFDEFINED](
            documents,
            chunker,
            chunker_args,
            chunker_kwargs,
            chunking_kwargs,
            to_lion,
        )

    else:
        raise ValueError(
            f"{chunker_type} is not supported. Please choose from {list(ChunkerType)}"
        )


def _self_defined_chunker(
    documents,
    chunker,
    chunker_args,
    chunker_kwargs,
    chunking_kwargs,
    to_lion: bool | Callable,
):
    try:
        splitter = chunker(*chunker_args, **chunker_kwargs)
        nodes = splitter.split(documents, **chunking_kwargs)
    except Exception as e:
        raise ValueError(
            f"Self defined chunker {chunker} is not valid. Error: {e}"
        ) from e

    if isinstance(to_lion, bool) and to_lion is True:
        raise ValueError("Please define a valid parser to Node.")
    elif isinstance(to_lion, Callable):
        nodes = _datanode_parser(nodes, to_lion)
    return nodes


def _llama_index_chunker(
    documents,
    documents_convert_func,
    chunker,
    chunker_args,
    chunker_kwargs,
    to_lion: bool | Callable,
):
    if documents_convert_func:
        documents = documents_convert_func(documents, "llama_index")
    nodes = LlamaIndexBridge.llama_index_parse_node(
        documents, chunker, chunker_args, chunker_kwargs
    )

    if isinstance(to_lion, bool) and to_lion is True:
        nodes = [Node.from_llama_index(i) for i in nodes]
    elif isinstance(to_lion, Callable):
        nodes = _datanode_parser(nodes, to_lion)
    return nodes


def _langchain_chunker(
    documents,
    documents_convert_func,
    chunker,
    chunker_args,
    chunker_kwargs,
    to_lion: bool | Callable,
):
    if documents_convert_func:
        documents = documents_convert_func(documents, "langchain")
    nodes = LangchainBridge.langchain_text_splitter(
        documents, chunker, chunker_args, chunker_kwargs
    )
    if isinstance(to_lion, bool) and to_lion is True:
        if isinstance(documents, str):
            nodes = [Node(content=i) for i in nodes]
        else:
            nodes = [Node.from_langchain(i) for i in nodes]
    elif isinstance(to_lion, Callable):
        nodes = _datanode_parser(nodes, to_lion)
    return nodes


def _plain_chunker(documents, chunker, chunker_args, chunker_kwargs):
    try:
        if chunker == "text_chunker":
            chunker = text_chunker
        return chunker(documents, chunker_args, chunker_kwargs)
    except Exception as e:
        raise ValueError(
            f"Reader {chunker} is currently not supported. Error: {e}"
        ) from e


chunk_funcs = {
    ChunkerType.PLAIN: _plain_chunker,
    ChunkerType.LANGCHAIN: _langchain_chunker,
    ChunkerType.LLAMAINDEX: _llama_index_chunker,
    ChunkerType.SELFDEFINED: _self_defined_chunker,
}
