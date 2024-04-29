
## BaseNode

^50e3bb

> Child class of [[Base Component]]

`BaseNode` extends `Component`, focusing on nodes in graph or tree structures. It includes content management capabilities alongside the features inherited from `Component`.

### Attributes

- `content`: Flexible attribute for storing the node's content. Can be of any type to accommodate diverse content structures. Supports aliasing for compatibility with different naming conventions.

### Methods

#### `content_str() -> str`

Attempts to serialize the node's content to a string, returning a default "null" string if serialization fails.

## BaseRelatableNode

^146ece

> Child class of [[Base Nodes#^50e3bb|Base Node]]

`BaseRelatableNode` further extends `BaseNode` by adding functionality to manage relationships with other nodes, making it suitable for representing interconnected data points in a network.

### Attributes

- `related_nodes`: A list of identifiers (strings) for nodes that are related to this node.
- `label`: An optional label for the node, providing additional context or classification.

### Methods

#### `add_related_node(node_id: str) -> bool`

Adds a node to the list of related nodes if it's not already present, returning `True` if added successfully.

#### `remove_related_node(node_id: str) -> bool`

Removes a node from the list of related nodes if present, returning `True` if the node was successfully removed.

## Tool

> Child class of [[Base Nodes#^146ece|BaseRelatableNode]]

The `Tool` class represents a specialized form of `BaseRelatableNode` tailored for defining tools or utilities within the framework. It includes additional attributes for defining the tool's functionality, schema, manual, and parser.

### Attributes

- `func`: The main function or capability of the tool.
- `schema_`: Optional. Defines the structure and constraints of data the tool is designed to work with.
- `manual`: Optional documentation or manual for using the tool.
- `parser`: An optional parser associated with the tool for data processing or interpretation.

### Methods

#### `serialize_func(func)`

Serializes the `func` attribute, typically used to store the name of the function or method as a string.
