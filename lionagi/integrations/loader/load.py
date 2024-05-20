from typing import Callable

from lionagi.core.generic import Node
from ..bridge.langchain_.langchain_bridge import LangchainBridge
from ..bridge.llamaindex_.llama_index_bridge import LlamaIndexBridge

from .load_util import dir_to_nodes, ReaderType, _datanode_parser


def text_reader(args, kwargs):
    """
    Reads text files from a directory and converts them to Node instances.

    Args:
        args: Positional arguments for the dir_to_nodes function.
        kwargs: Keyword arguments for the dir_to_nodes function.

    Returns:
        A list of Node instances.

    Example usage:
        >>> args = ['path/to/text/files']
        >>> kwargs = {'file_extension': 'txt'}
        >>> nodes = text_reader(args, kwargs)
    """
    return dir_to_nodes(*args, **kwargs)


def load(
    reader: str | Callable = "SimpleDirectoryReader",
    input_dir=None,
    input_files=None,
    recursive: bool = False,
    required_exts: list[str] = None,
    reader_type=ReaderType.LLAMAINDEX,
    reader_args=None,
    reader_kwargs=None,
    load_args=None,
    load_kwargs=None,
    to_lion: bool | Callable = True,
):

    if reader_args is None:
        reader_args = []
    if reader_kwargs is None:
        reader_kwargs = {}
    if load_args is None:
        load_args = []
    if load_kwargs is None:
        load_kwargs = {}

    if reader_type == ReaderType.PLAIN:
        return read_funcs[ReaderType.PLAIN](reader, reader_args, reader_kwargs)

    if reader_type == ReaderType.LANGCHAIN:
        return read_funcs[ReaderType.LANGCHAIN](
            reader, reader_args, reader_kwargs, to_lion
        )

    elif reader_type == ReaderType.LLAMAINDEX:
        if input_dir is not None:
            reader_kwargs["input_dir"] = input_dir
        if input_files is not None:
            reader_kwargs["input_files"] = input_files
        if recursive:
            reader_kwargs["recursive"] = True
        if required_exts is not None:
            reader_kwargs["required_exts"] = required_exts

        return read_funcs[ReaderType.LLAMAINDEX](
            reader, reader_args, reader_kwargs, load_args, load_kwargs, to_lion
        )

    elif reader_type == ReaderType.SELFDEFINED:
        return read_funcs[ReaderType.SELFDEFINED](
            reader, reader_args, reader_kwargs, load_args, load_kwargs, to_lion
        )

    else:
        raise ValueError(
            f"{reader_type} is not supported. Please choose from {list(ReaderType)}"
        )


def _plain_reader(reader, reader_args, reader_kwargs):
    try:
        if reader == "text_reader":
            reader = text_reader
        return reader(reader_args, reader_kwargs)
    except Exception as e:
        raise ValueError(
            f"Reader {reader} is currently not supported. Error: {e}"
        ) from e


def _langchain_reader(reader, reader_args, reader_kwargs, to_lion: bool | Callable):
    nodes = LangchainBridge.langchain_loader(reader, reader_args, reader_kwargs)
    if isinstance(to_lion, bool) and to_lion is True:
        nodes = [Node.from_langchain(i) for i in nodes]

    elif isinstance(to_lion, Callable):
        nodes = _datanode_parser(nodes, to_lion)
    return nodes


def _llama_index_reader(
    reader,
    reader_args,
    reader_kwargs,
    load_args,
    load_kwargs,
    to_lion: bool | Callable,
):
    nodes = LlamaIndexBridge.llama_index_read_data(
        reader, reader_args, reader_kwargs, load_args, load_kwargs
    )
    if isinstance(to_lion, bool) and to_lion is True:
        nodes = [Node.from_llama_index(i) for i in nodes]
    elif isinstance(to_lion, Callable):
        nodes = _datanode_parser(nodes, to_lion)
    return nodes


def _self_defined_reader(
    reader,
    reader_args,
    reader_kwargs,
    load_args,
    load_kwargs,
    to_lion: bool | Callable,
):
    try:
        loader = reader(*reader_args, **reader_kwargs)
        nodes = loader.load(*load_args, **load_kwargs)
    except Exception as e:
        raise ValueError(
            f"Self defined reader {reader} is not valid. Error: {e}"
        ) from e

    if isinstance(to_lion, bool) and to_lion is True:
        raise ValueError("Please define a valid parser to Node.")
    elif isinstance(to_lion, Callable):
        nodes = _datanode_parser(nodes, to_lion)
    return nodes


read_funcs = {
    ReaderType.PLAIN: _plain_reader,
    ReaderType.LANGCHAIN: _langchain_reader,
    ReaderType.LLAMAINDEX: _llama_index_reader,
    ReaderType.SELFDEFINED: _self_defined_reader,
}
