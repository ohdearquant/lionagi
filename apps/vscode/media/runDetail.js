// Lion Studio — Run Detail webview script
// Runs inside the restricted webview context (no module system, strict CSP).
// Receives postMessage from the extension host.

(function () {
  "use strict";

  const log = document.getElementById("log");
  const footer = document.getElementById("footer");
  const statusDot = document.getElementById("statusDot");
  const statusBadge = document.getElementById("statusBadge");
  const emptyState = document.getElementById("emptyState");

  let eventCount = 0;
  let autoScroll = true;

  // Track whether the user has scrolled up (disable auto-scroll).
  if (log) {
    log.addEventListener("scroll", function () {
      const atBottom =
        log.scrollHeight - log.scrollTop - log.clientHeight < 40;
      autoScroll = atBottom;
    });
  }

  function scrollToBottom() {
    if (autoScroll && log) {
      log.scrollTop = log.scrollHeight;
    }
  }

  function setFooter(text) {
    if (footer) {
      footer.textContent = text;
    }
  }

  function addRow(el) {
    if (emptyState) {
      emptyState.style.display = "none";
    }
    if (log) {
      log.appendChild(el);
      scrollToBottom();
    }
    eventCount++;
  }

  // Action output is collapsed (clamped to a scrollable box) by default when it
  // is long — by line count OR by character count, so a single very long line
  // (e.g. a tool call's arguments JSON) collapses too.
  const COLLAPSE_MIN_LINES = 5;
  const COLLAPSE_CHAR_LIMIT = 320;

  function isLong(text) {
    return (
      text.split("\n").length > COLLAPSE_MIN_LINES ||
      text.length > COLLAPSE_CHAR_LIMIT
    );
  }

  function renderEvent(event) {
    const row = document.createElement("div");
    row.className = "log-row";

    const type = (event.role || event.type || "event").toLowerCase();

    // Extract the most useful text from the event object.
    const content = extractContent(event);
    const contentClass = contentCssClass(type);

    const typeSpan = document.createElement("span");
    typeSpan.className = "event-type";
    typeSpan.textContent = type;

    if (typeof content !== "string" || content.length === 0) {
      // Fall back to a compact JSON block for structured events. These dumps
      // (e.g. tool results with a dict output) are often long, so clamp them too.
      const block = document.createElement("div");
      block.className = "event-block";
      const dump = JSON.stringify(event, null, 2);
      block.textContent = dump;
      row.appendChild(typeSpan);
      row.appendChild(block);
      if (isLong(dump)) {
        makeCollapsible(row, block);
      }
      addRow(row);
      return;
    }

    const contentSpan = document.createElement("span");
    contentSpan.className = "event-content " + contentClass;
    row.appendChild(typeSpan);
    row.appendChild(contentSpan);

    // Action output can be very long — clamp it to a scrollable box by default.
    // The full text always stays in the DOM; collapse only limits its height.
    const isAction = contentClass.indexOf("--tool") !== -1;
    contentSpan.textContent = content;
    if (isAction && isLong(content)) {
      makeCollapsible(row, contentSpan);
    }

    addRow(row);
  }

  // Clamp a long element to a scrollable box with an expand/collapse toggle.
  // "Collapsed" contains the content (max-height + scroll) — it is never removed
  // from the DOM, so nothing is hidden and expand always restores the full height.
  function makeCollapsible(row, el) {
    el.classList.add("collapsible", "collapsed");

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "event-toggle";

    function sync() {
      toggle.textContent = el.classList.contains("collapsed")
        ? "▾ Expand"
        : "▴ Collapse";
    }
    toggle.addEventListener("click", function () {
      el.classList.toggle("collapsed");
      sync();
    });
    sync();
    row.appendChild(toggle);
  }

  function extractContent(event) {
    // Walk common field names in priority order.
    if (typeof event.content === "string" && event.content) {
      return event.content;
    }
    // lionagi structured message content (dict keyed by message type).
    if (event.content && typeof event.content === "object") {
      const c = event.content;
      if (typeof c.assistant_response === "string") {
        return c.assistant_response;
      }
      if (typeof c.instruction === "string") {
        return c.instruction;
      }
      if (typeof c.output === "string") {
        return c.function ? c.function + " → " + c.output : c.output;
      }
      if (typeof c.function === "string") {
        const args =
          c.arguments && typeof c.arguments === "object"
            ? JSON.stringify(c.arguments)
            : c.arguments || "";
        return c.function + "(" + args + ")";
      }
      if (typeof c.system === "string") {
        return c.system;
      }
    }
    if (typeof event.text === "string" && event.text) {
      return event.text;
    }
    if (typeof event.message === "string" && event.message) {
      return event.message;
    }
    if (typeof event.output === "string" && event.output) {
      return event.output;
    }
    if (typeof event.delta === "string" && event.delta) {
      return event.delta;
    }
    if (event.delta && typeof event.delta.text === "string") {
      return event.delta.text;
    }
    // Nested message object.
    if (event.message && typeof event.message === "object") {
      const m = event.message;
      if (typeof m.content === "string") {
        return m.content;
      }
      if (Array.isArray(m.content)) {
        return m.content
          .map(function (c) {
            return typeof c === "string" ? c : c.text || "";
          })
          .join("");
      }
    }
    return null;
  }

  function contentCssClass(type) {
    if (type === "assistant" || type === "message" || type === "response") {
      return "event-content--assistant";
    }
    if (
      type === "action" ||
      type === "tool_call" ||
      type === "tool_result" ||
      type === "action_request" ||
      type === "action_response"
    ) {
      return "event-content--tool";
    }
    return "";
  }

  function updateMeta(run) {
    // Update status badge and dot.
    if (statusBadge && run.status) {
      statusBadge.textContent = run.status;
      statusBadge.className =
        "badge badge--status badge--" + statusCssClass(run.status);
    }
    if (statusDot && run.status) {
      statusDot.className =
        "status-dot status-dot--" + statusCssClass(run.status);
    }

    const countEl = document.getElementById("msgCount");
    if (countEl && run.message_count !== undefined) {
      countEl.textContent = "messages: " + run.message_count;
    }
  }

  function statusCssClass(status) {
    const s = (status || "").toLowerCase();
    if (s === "running" || s === "active" || s === "starting") {
      return "running";
    }
    if (s === "succeeded" || s === "completed") {
      return "success";
    }
    if (s === "failed" || s === "error") {
      return "error";
    }
    if (s === "cancelled") {
      return "cancelled";
    }
    if (s === "queued" || s === "pending") {
      return "pending";
    }
    return "unknown";
  }

  // Listen for messages from the extension host.
  window.addEventListener("message", function (e) {
    const msg = e.data;
    if (!msg || !msg.type) {
      return;
    }

    switch (msg.type) {
      case "event": {
        renderEvent(msg.event);
        setFooter(eventCount + " event" + (eventCount === 1 ? "" : "s"));
        break;
      }

      case "done": {
        const row = document.createElement("div");
        row.className = "log-row log-row--done";
        row.textContent = "Run complete.";
        addRow(row);
        setFooter("Done · " + eventCount + " event" + (eventCount === 1 ? "" : "s"));

        // Stop status dot animation.
        if (statusDot) {
          statusDot.className =
            statusDot.className.replace("running", "success");
        }
        break;
      }

      case "empty": {
        if (emptyState) {
          emptyState.style.display = "";
        }
        setFooter("No messages recorded.");
        break;
      }

      case "error": {
        const row = document.createElement("div");
        row.className = "log-row log-row--error";
        row.textContent = "Error: " + (msg.message || "unknown");
        addRow(row);
        setFooter("Error — check the output channel.");
        break;
      }

      case "meta": {
        if (msg.run) {
          updateMeta(msg.run);
        }
        break;
      }

      default:
        break;
    }
  });

  setFooter("Connecting…");
})();
