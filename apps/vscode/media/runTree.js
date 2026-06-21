// Den — Run Tree webview script
// Runs inside the restricted webview context (no module system, strict CSP).
// Receives postMessage from the extension host.

(function () {
  "use strict";

  var treeEl = document.getElementById("tree");
  var footer = document.getElementById("footer");
  var statusDot = document.getElementById("statusDot");
  var statusBadge = document.getElementById("statusBadge");
  var emptyState = document.getElementById("emptyState");
  var usageLine = document.getElementById("usageLine");

  function setFooter(text) {
    if (footer) {
      footer.textContent = text;
    }
  }

  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function stateCssClass(state) {
    switch (state) {
      case "running": return "running";
      case "succeeded": return "succeeded";
      case "failed": return "failed";
      case "queued": return "queued";
      case "awaiting_approval": return "awaiting_approval";
      case "escalated": return "escalated";
      default: return "pending";
    }
  }

  function runStateCssClass(runState) {
    switch (runState) {
      case "running": return "running";
      case "succeeded": return "succeeded";
      case "failed": return "failed";
      default: return "pending";
    }
  }

  function formatElapsed(seconds) {
    if (!seconds || seconds <= 0) {
      return "";
    }
    return seconds.toFixed(1) + "s";
  }

  function buildNodeLi(node) {
    var li = document.createElement("li");

    var row = document.createElement("div");
    row.className = "tree-node";

    var statePill = document.createElement("span");
    var cls = stateCssClass(node.state);
    statePill.className = "node-state node-state--" + cls;
    statePill.textContent = node.state;

    var nameSpan = document.createElement("span");
    nameSpan.className = "node-name";
    nameSpan.title = node.name;
    nameSpan.textContent = node.name;

    row.appendChild(statePill);
    row.appendChild(nameSpan);

    if (node.elapsed && node.elapsed > 0) {
      var elapsedSpan = document.createElement("span");
      elapsedSpan.className = "node-elapsed";
      elapsedSpan.textContent = formatElapsed(node.elapsed);
      row.appendChild(elapsedSpan);
    }

    li.appendChild(row);

    if (node.children && node.children.length > 0) {
      var ul = document.createElement("ul");
      ul.className = "tree-list";
      for (var i = 0; i < node.children.length; i++) {
        ul.appendChild(buildNodeLi(node.children[i]));
      }
      li.appendChild(ul);
    }

    return li;
  }

  function renderSnapshot(msg) {
    var forest = msg.forest;
    var runState = msg.runState;
    var usage = msg.usage;

    // Update header status.
    var cls = runStateCssClass(runState);
    if (statusDot) {
      statusDot.className = "status-dot status-dot--" + cls;
    }
    if (statusBadge) {
      statusBadge.className = "badge badge--status badge--" + cls;
      statusBadge.textContent = runState;
    }

    // Update usage line.
    if (usage && usageLine) {
      usageLine.style.display = "";
      usageLine.textContent =
        "in " + usage.inputTokens +
        " · out " + usage.outputTokens +
        " tokens · $" + (usage.totalCostUsd || 0).toFixed(4) +
        " · " + usage.numTurns + " turn" + (usage.numTurns === 1 ? "" : "s") +
        " · " + ((usage.durationMs || 0) / 1000).toFixed(1) + "s";
    }

    // Clear and rebuild the tree.
    if (!treeEl) {
      return;
    }

    // Remove existing tree-list children (keep emptyState).
    var existing = treeEl.querySelector("ul.tree-list");
    if (existing) {
      treeEl.removeChild(existing);
    }

    // A malformed snapshot (non-array forest) is treated as empty rather than
    // crashing the later forest.slice()/index loop — the status/usage header
    // above still updates, so a corrupt forest never wedges the whole panel.
    if (!Array.isArray(forest) || forest.length === 0) {
      if (emptyState) {
        emptyState.style.display = "";
      }
      return;
    }

    if (emptyState) {
      emptyState.style.display = "none";
    }

    var ul = document.createElement("ul");
    ul.className = "tree-list";
    for (var i = 0; i < forest.length; i++) {
      ul.appendChild(buildNodeLi(forest[i]));
    }
    treeEl.appendChild(ul);
  }

  // Listen for messages from the extension host.
  window.addEventListener("message", function (e) {
    var msg = e.data;
    if (!msg || !msg.type) {
      return;
    }

    switch (msg.type) {
      case "snapshot": {
        renderSnapshot(msg);
        var nodeCount = countNodes(msg.forest);
        setFooter(
          "streaming… · " +
          nodeCount + " node" + (nodeCount === 1 ? "" : "s")
        );
        break;
      }

      case "done": {
        setFooter("done");
        // Stop running animation on the status dot.
        if (statusDot) {
          statusDot.className = statusDot.className.replace("running", "succeeded");
        }
        break;
      }

      case "error": {
        setFooter("Error: " + (msg.message || "unknown"));
        break;
      }

      default:
        break;
    }
  });

  function countNodes(forest) {
    if (!Array.isArray(forest) || forest.length === 0) {
      return 0;
    }
    var count = 0;
    var stack = forest.slice();
    while (stack.length > 0) {
      var node = stack.pop();
      count++;
      if (node.children && node.children.length > 0) {
        for (var i = 0; i < node.children.length; i++) {
          stack.push(node.children[i]);
        }
      }
    }
    return count;
  }

  setFooter("Connecting…");
})();
