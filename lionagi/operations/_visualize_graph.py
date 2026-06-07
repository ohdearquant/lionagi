def _ensure_headless_backend(save_path) -> None:
    """Force Agg backend when saving to disk — works from any thread.

    Must run BEFORE ``import matplotlib.pyplot`` in the calling scope,
    since matplotlib binds its backend on the first pyplot import.
    macOS default backend (``macosx``) creates an ``NSWindow`` which
    Cocoa forbids off the main thread; Agg has no GUI at all.
    """
    if not save_path:
        return
    try:
        import matplotlib

        matplotlib.use("Agg", force=True)
    except Exception:  # noqa: S110 — backend already bound and can't switch; savefig still works
        pass


def visualize_graph(
    builder,
    title: str = "Operation Graph",
    figsize=(14, 10),
    save_path: str | None = None,
):
    """Visualize an executed or in-progress operation graph.

    Reads node status from ``builder._executed`` and metadata on each node.
    """
    from lionagi.utils import is_import_installed

    if not is_import_installed("matplotlib"):
        raise ImportError(
            "matplotlib is required for visualization. "
            "Please install it using `pip install matplotlib`."
        )
    if not is_import_installed("networkx"):
        raise ImportError(
            "networkx is required for visualization. "
            "Please install it using `pip install networkx`."
        )

    _ensure_headless_backend(save_path)

    import matplotlib.pyplot as plt
    import networkx as nx
    import numpy as np

    graph = builder.get_graph()

    # Convert to networkx
    G = nx.DiGraph()  # noqa: N806 — networkx convention for a graph object

    # Track node positions for hierarchical layout
    node_levels = {}
    node_labels = {}
    node_colors = []
    node_sizes = []

    # First pass: add nodes and determine levels
    for node in graph.internal_nodes.values():
        node_id = str(node.id)[:8]
        G.add_node(node_id)

        # Determine level based on dependencies
        in_edges = [e for e in graph.internal_edges.values() if str(e.tail)[:8] == node_id]
        if not in_edges:
            level = 0  # Root nodes
        else:
            # Get max level of predecessors + 1
            pred_levels = []
            for edge in in_edges:
                pred_id = str(edge.head)[:8]
                if pred_id in node_levels:
                    pred_levels.append(node_levels[pred_id])
            level = max(pred_levels, default=0) + 1

        node_levels[node_id] = level

        # Create label
        ref_id = node.metadata.get("reference_id", "")
        if ref_id:
            label = f"{node.operation}\n[{ref_id}]"
        else:
            label = f"{node.operation}\n{node_id}"
        node_labels[node_id] = label

        # Color and size based on status and type
        if node.id in builder._executed:
            node_colors.append("#90EE90")  # Light green
            node_sizes.append(4000)
        elif node.metadata.get("expansion_source"):
            node_colors.append("#87CEEB")  # Sky blue
            node_sizes.append(3500)
        elif node.metadata.get("aggregation"):
            node_colors.append("#FFD700")  # Gold
            node_sizes.append(4500)
        elif node.metadata.get("is_condition_check"):
            node_colors.append("#DDA0DD")  # Plum
            node_sizes.append(3500)
        else:
            node_colors.append("#E0E0E0")  # Light gray
            node_sizes.append(3000)

    # Add edges
    edge_colors = []
    edge_styles = []
    edge_widths = []
    edge_labels = {}

    for edge in graph.internal_edges.values():
        head_id = str(edge.head)[:8]
        tail_id = str(edge.tail)[:8]
        G.add_edge(head_id, tail_id)

        # Style edges based on type
        edge_label = edge.label[0] if edge.label else ""
        edge_labels[(head_id, tail_id)] = edge_label

        if "expansion" in edge_label:
            edge_colors.append("#4169E1")  # Royal blue
            edge_styles.append("dashed")
            edge_widths.append(2)
        elif "aggregate" in edge_label:
            edge_colors.append("#FF6347")  # Tomato
            edge_styles.append("dotted")
            edge_widths.append(2.5)
        else:
            edge_colors.append("#808080")  # Gray
            edge_styles.append("solid")
            edge_widths.append(1.5)

    # Create improved hierarchical layout
    pos = {}
    nodes_by_level = {}

    for node_id, level in node_levels.items():
        if level not in nodes_by_level:
            nodes_by_level[level] = []
        nodes_by_level[level].append(node_id)

    # Position nodes with better spacing algorithm
    y_spacing = 2.5

    for level, nodes in nodes_by_level.items():
        num_nodes = len(nodes)

        if num_nodes <= 6:
            # Normal spacing for small levels
            x_spacing = 2.5
            x_offset = -(num_nodes - 1) * x_spacing / 2
            for i, node_id in enumerate(nodes):
                pos[node_id] = (x_offset + i * x_spacing, -level * y_spacing)
        else:
            # Multi-row layout for large levels
            nodes_per_row = min(6, int(np.ceil(np.sqrt(num_nodes * 1.5))))

            for i, node_id in enumerate(nodes):
                row = i // nodes_per_row
                col = i % nodes_per_row

                # Calculate row width
                nodes_in_row = min(nodes_per_row, num_nodes - row * nodes_per_row)
                x_spacing = 2.5
                x_offset = -(nodes_in_row - 1) * x_spacing / 2

                # Add slight y offset for different rows
                y_offset = row * 0.8

                pos[node_id] = (
                    x_offset + col * x_spacing,
                    -level * y_spacing - y_offset,
                )

    # Create figure
    plt.figure(figsize=figsize)

    # Draw nodes
    nx.draw_networkx_nodes(
        G,
        pos,
        node_color=node_colors,
        node_size=node_sizes,
        alpha=0.9,
        linewidths=2,
        edgecolors="black",
    )

    # Draw edges with different styles - use curved edges for better visibility
    for i, (u, v) in enumerate(G.edges()):
        # Calculate curve based on node positions
        u_pos = pos[u]
        v_pos = pos[v]

        # Determine connection style based on relative positions
        if abs(u_pos[0] - v_pos[0]) > 5:  # Far apart horizontally
            connectionstyle = "arc3,rad=0.2"
        else:
            connectionstyle = "arc3,rad=0.1"

        nx.draw_networkx_edges(
            G,
            pos,
            [(u, v)],
            edge_color=[edge_colors[i]],
            style=edge_styles[i],
            width=edge_widths[i],
            alpha=0.7,
            arrows=True,
            arrowsize=20,
            arrowstyle="-|>",
            connectionstyle=connectionstyle,
        )

    # Draw labels
    nx.draw_networkx_labels(
        G,
        pos,
        node_labels,
        font_size=9,
        font_weight="bold",
        font_family="monospace",
    )

    # Draw edge labels (only for smaller graphs)
    if len(G.edges()) < 20:
        nx.draw_networkx_edge_labels(
            G,
            pos,
            edge_labels,
            font_size=7,
            font_color="darkblue",
            bbox=dict(
                boxstyle="round,pad=0.3",
                facecolor="white",
                edgecolor="none",
                alpha=0.7,
            ),
        )

    plt.title(title, fontsize=18, fontweight="bold", pad=20)
    plt.axis("off")

    # Enhanced legend
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    legend_elements = [
        Patch(facecolor="#90EE90", edgecolor="black", label="Executed"),
        Patch(facecolor="#87CEEB", edgecolor="black", label="Expanded"),
        Patch(facecolor="#FFD700", edgecolor="black", label="Aggregation"),
        Patch(facecolor="#DDA0DD", edgecolor="black", label="Condition"),
        Patch(facecolor="#E0E0E0", edgecolor="black", label="Pending"),
        Line2D([0], [0], color="#808080", linewidth=2, label="Sequential"),
        Line2D(
            [0],
            [0],
            color="#4169E1",
            linewidth=2,
            linestyle="dashed",
            label="Expansion",
        ),
        Line2D(
            [0],
            [0],
            color="#FF6347",
            linewidth=2,
            linestyle="dotted",
            label="Aggregate",
        ),
    ]

    plt.legend(
        handles=legend_elements,
        loc="upper left",
        bbox_to_anchor=(0, 1),
        frameon=True,
        fancybox=True,
        shadow=True,
        ncol=2,
    )

    # Add statistics box
    stats_text = (
        f"Nodes: {len(G.nodes())}\nEdges: {len(G.edges())}\nExecuted: {len(builder._executed)}"
    )
    if nodes_by_level:
        max_level = max(nodes_by_level.keys())
        stats_text += f"\nLevels: {max_level + 1}"

    plt.text(
        0.98,
        0.02,
        stats_text,
        transform=plt.gca().transAxes,
        bbox=dict(boxstyle="round,pad=0.5", facecolor="lightgray", alpha=0.8),
        verticalalignment="bottom",
        horizontalalignment="right",
        fontsize=10,
        fontfamily="monospace",
    )

    plt.tight_layout()

    if save_path:
        from pathlib import Path

        p = Path(save_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        plt.savefig(p, dpi=150, bbox_inches="tight")
        print(f"Graph saved: {p}")
        plt.close()
    else:
        plt.show()
