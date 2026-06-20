// @ts-check
(function () {
  const logEl = /** @type {HTMLElement} */ (document.getElementById("log"));
  const badgeEl = /** @type {HTMLElement} */ (
    document.getElementById("status-badge")
  );

  let autoScroll = true;

  // Action output is collapsed by default when it exceeds this many lines.
  const COLLAPSE_MIN_LINES = 5;
  const COLLAPSE_VISIBLE = 4;

  logEl.addEventListener("scroll", () => {
    const atBottom =
      logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
    autoScroll = atBottom;
  });

  function appendRow(text, cls) {
    const row = document.createElement("div");
    row.className = "log-row " + (cls || "content");
    const full = String(text).replace(/\n+$/, "");
    const lines = full.split("\n");
    if (cls === "tool" && lines.length > COLLAPSE_MIN_LINES) {
      attachCollapse(row, full, lines);
    } else {
      row.textContent = text;
    }
    logEl.appendChild(row);
    if (autoScroll) {
      logEl.scrollTop = logEl.scrollHeight;
    }
  }

  // Show the first COLLAPSE_VISIBLE lines with an expand/collapse toggle.
  function attachCollapse(row, full, lines) {
    const head = lines.slice(0, COLLAPSE_VISIBLE).join("\n");
    const hidden = lines.length - COLLAPSE_VISIBLE;
    let collapsed = true;

    const textEl = document.createElement("span");
    textEl.className = "log-text";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "log-toggle";

    function sync() {
      textEl.textContent = collapsed ? head : full;
      toggle.textContent = collapsed
        ? "▾ Show " + hidden + " more line" + (hidden === 1 ? "" : "s")
        : "▴ Show less";
    }
    toggle.addEventListener("click", function () {
      collapsed = !collapsed;
      sync();
    });
    sync();
    row.appendChild(textEl);
    row.appendChild(toggle);
  }

  function setBadge(label, cls) {
    badgeEl.textContent = label;
    badgeEl.className = cls || "";
  }

  function rowClassForEvent(ev) {
    const t = String((ev && (ev.role || ev.type)) || "").toLowerCase();
    if (t === "assistant" || t === "assistant_response") {
      return "assistant";
    }
    if (
      t === "action" ||
      t === "action_request" ||
      t === "action_response" ||
      t === "tool_call" ||
      t === "tool_result"
    ) {
      return "tool";
    }
    return "content";
  }

  function formatEvent(ev) {
    if (!ev || typeof ev !== "object") {
      return String(ev);
    }
    // lionagi structured message content (dict keyed by message type).
    if (ev.content && typeof ev.content === "object") {
      const c = ev.content;
      if (typeof c.assistant_response === "string") {
        return c.assistant_response.trim();
      }
      if (typeof c.instruction === "string") {
        return c.instruction.trim();
      }
      if (typeof c.output === "string") {
        return (c.function ? c.function + " → " + c.output : c.output).trim();
      }
      if (typeof c.function === "string") {
        const args =
          c.arguments && typeof c.arguments === "object"
            ? JSON.stringify(c.arguments)
            : c.arguments || "";
        return c.function + "(" + args + ")";
      }
      if (typeof c.system === "string") {
        return c.system.trim();
      }
    }
    // Try to surface a meaningful text payload
    const candidates = [
      ev["content"],
      ev["text"],
      ev["message"],
      ev["data"],
      ev["output"],
    ];
    for (const c of candidates) {
      if (typeof c === "string" && c.trim()) {
        return c.trim();
      }
      if (Array.isArray(c)) {
        const texts = c
          .map((item) =>
            typeof item === "string"
              ? item
              : typeof item === "object" && item !== null && "text" in item
                ? String(item["text"])
                : null
          )
          .filter(Boolean);
        if (texts.length > 0) {
          return texts.join(" ").trim();
        }
      }
    }
    // Fall back to JSON dump, omitting type field for brevity
    const { type: _type, ...rest } = ev;
    const keys = Object.keys(rest);
    if (keys.length === 0) {
      return ev.type ? `[${ev.type}]` : "[event]";
    }
    return JSON.stringify(rest);
  }

  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (!msg || !msg.kind) {
      return;
    }

    switch (msg.kind) {
      case "status":
        setBadge(msg.label, msg.cls);
        break;

      case "event": {
        const ev = msg.event;
        if (!ev) {
          break;
        }
        const evType = ev.type;
        // heartbeat and done are handled by host; skip rendering heartbeat
        if (evType === "heartbeat") {
          break;
        }
        if (evType === "done") {
          appendRow("— done —", "meta");
          setBadge("done", "done");
          break;
        }
        const text = formatEvent(ev);
        if (text) {
          appendRow(text, rowClassForEvent(ev));
        }
        break;
      }

      case "meta":
        appendRow(msg.text, "meta");
        break;

      case "error":
        appendRow("Error: " + msg.text, "meta");
        setBadge("error", "error");
        break;
    }
  });
})();
