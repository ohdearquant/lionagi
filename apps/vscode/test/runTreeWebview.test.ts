// @vitest-environment jsdom
/**
 * Rendering tests for the run-tree webview script (media/runTree.js), loaded as
 * its real IIFE into jsdom. Covers the happy path (a snapshot renders the forest)
 * and the malformed-message guards: a non-array forest must not throw inside
 * renderSnapshot/countNodes and wedge the listener. jsdom surfaces a listener
 * throw as a synchronous, catchable window "error" event, so the malformed cases
 * assert that no such error fires.
 */
import { describe, it, expect, beforeAll } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));

const TREE_HTML = `
  <div class="header">
    <span id="statusDot"></span>
    <h1 id="runTitle">t</h1>
    <span id="statusBadge"></span>
  </div>
  <div id="usageLine" style="display:none"></div>
  <div id="tree"><div id="emptyState" style="display:none"></div></div>
  <div id="footer"><span id="footerStatus"></span></div>
`;

beforeAll(() => {
  // The IIFE captures element refs (treeEl, footer, …) at load time, so the DOM
  // must exist before it runs — hence no per-test body reset below.
  document.body.innerHTML = TREE_HTML;
  const src = readFileSync(join(here, "..", "media", "runTree.js"), "utf8");
  new Function(src)();
});

function dispatchSnapshot(payload: Record<string, unknown>): string[] {
  const errors: string[] = [];
  const onErr = (ev: Event): void => {
    const e = ev as ErrorEvent;
    errors.push(String(e.error?.message ?? e.message ?? e));
  };
  window.addEventListener("error", onErr);
  try {
    window.dispatchEvent(
      new MessageEvent("message", { data: { type: "snapshot", ...payload } })
    );
  } finally {
    window.removeEventListener("error", onErr);
  }
  return errors;
}

describe("runTree.js snapshot rendering", () => {
  it("renders a forest of nodes", () => {
    const errors = dispatchSnapshot({
      forest: [
        { state: "running", name: "root", children: [] },
        { state: "succeeded", name: "child", children: [] },
      ],
      runState: "running",
    });
    expect(errors).toEqual([]);
    expect(document.querySelectorAll("#tree ul.tree-list > li").length).toBe(2);
  });

  it("does not throw on a non-array forest object", () => {
    expect(dispatchSnapshot({ forest: {}, runState: "running" })).toEqual([]);
  });

  it("does not throw on a string forest", () => {
    expect(dispatchSnapshot({ forest: "xyz", runState: "running" })).toEqual([]);
  });
});
