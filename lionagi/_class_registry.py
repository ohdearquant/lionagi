# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import ast
import importlib
import os
from typing import TypeVar

T = TypeVar("T")
LION_CLASS_REGISTRY: dict[str, type[T]] = {}
LION_CLASS_FILE_REGISTRY: dict[str, str] = {}

pattern_list = [
    "lionagi/protocols/generic",
    "lionagi/protocols/graph",
    "lionagi/protocols/messages",
]

__all__ = (
    "get_class",
    "LION_CLASS_REGISTRY",
)


def get_file_classes(file_path):
    with open(file_path) as file:
        file_content = file.read()

    tree = ast.parse(file_content)

    class_file_dict = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            class_file_dict[node.name] = file_path

    return class_file_dict


def get_class_file_registry(folder_path, pattern_list):
    class_file_registry = {}
    for root, _, files in os.walk(folder_path):
        for file in files:
            if file.endswith(".py"):
                if any(pattern in root for pattern in pattern_list):
                    class_file_dict = get_file_classes(os.path.join(root, file))
                    class_file_registry.update(class_file_dict)
    return class_file_registry


if not LION_CLASS_FILE_REGISTRY:
    script_path = os.path.abspath(__file__)
    script_dir = os.path.dirname(script_path)

    LION_CLASS_FILE_REGISTRY = get_class_file_registry(script_dir, pattern_list)

# The `lionagi/` package directory itself.  A valid *file_path* must live
# under this directory — not merely under the project root — so that a stale
# or polluted LION_CLASS_FILE_REGISTRY entry cannot import arbitrary top-level
# modules from the checkout (e.g. test files outside the package).
_PACKAGE_DIR: str = os.path.dirname(os.path.abspath(__file__))

# The parent directory of the lionagi package (i.e. the project root that
# contains the `lionagi/` directory).  Used to derive the dotted module name,
# which must include the `lionagi.` prefix.  Stored at module level so it can
# be used by get_class_objects without re-computing on every call.
_PACKAGE_PARENT: str = os.path.dirname(_PACKAGE_DIR)


def get_class_objects(file_path):
    """Return {class_name: class} for all classes in *file_path* via canonical import."""
    abs_path = os.path.abspath(file_path)

    # Guard: the file must live inside the `lionagi/` package directory itself,
    # not merely under the project root.  Otherwise any importable top-level
    # module in the checkout (e.g. a test file) would be treated as a valid
    # dotted module, broadening the fallback import surface.  os.path.commonpath
    # raises ValueError for paths on different drives (Windows); treat that the
    # same as "outside the package".
    try:
        within_package = os.path.commonpath([_PACKAGE_DIR, abs_path]) == _PACKAGE_DIR
    except ValueError:
        within_package = False

    if not within_package:
        raise ValueError(
            f"Cannot derive a dotted module name for {file_path!r}: "
            f"it is not located under the package root {_PACKAGE_DIR!r}."
        )

    rel = os.path.relpath(abs_path, _PACKAGE_PARENT)

    # Convert filesystem path to dotted module name.
    # Example: "lionagi/protocols/graph/node.py" -> "lionagi.protocols.graph.node"
    dotted_name = rel.replace(os.sep, ".").removesuffix(".py")

    module = importlib.import_module(dotted_name)

    class_objects = {}
    for attr_name in dir(module):
        obj = getattr(module, attr_name)
        if isinstance(obj, type):
            class_objects[attr_name] = obj

    return class_objects


def get_class(class_name: str) -> type:
    """Retrieve a class by name from the registry or by dynamic import; raises ValueError if not found."""
    if class_name in LION_CLASS_REGISTRY:
        return LION_CLASS_REGISTRY[class_name]

    try:
        found_class_filepath = LION_CLASS_FILE_REGISTRY[class_name]
        found_class_dict = get_class_objects(found_class_filepath)
        return found_class_dict[class_name]
    except Exception as e:
        raise ValueError(f"Unable to find class {class_name}: {e}") from e
