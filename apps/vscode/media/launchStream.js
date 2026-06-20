// @ts-check
(function () {
  const logEl = /** @type {HTMLElement} */ (document.getElementById("log"));
  const badgeEl = /** @type {HTMLElement} */ (
    document.getElementById("status-badge")
  );

  let autoScroll = true;

  logEl.addEventListener("scroll", () => {
    const atBottom =
      logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
    autoScroll = atBottom;
  });

  function appendRow(text, cls) {
    const row = document.createElement("div");
    row.className = "log-row " + (cls || "content");
    row.textContent = text;
    logEl.appendChild(row);
    if (autoScroll) {
      logEl.scrollTop = logEl.scrollHeight;
    }
  }

  function setBadge(label, cls) {
    badgeEl.textContent = label;
    badgeEl.className = cls || "";
  }

  function rowClassForEvent(ev) {
    if (!ev || !ev.type) {
      return "content";
    }
    const t = String(ev.type).toLowerCase();
    if (t === "assistant" || t === "assistant_response") {
      return "assistant";
    }
    if (
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
