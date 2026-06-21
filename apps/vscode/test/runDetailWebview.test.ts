// @vitest-environment jsdom
/**
 * Rendering tests for the run-detail webview script (media/runDetail.js), exercised
 * by loading the real IIFE into jsdom and dispatching the "reason" postMessage the
 * extension host sends. Asserts the evidence-ref count banner the host wires from
 * status_evidence_refs — closing the webview-has-no-test-harness coverage gap.
 */
import { describe, it, expect, beforeAll, beforeEach } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));

beforeAll(() => {
  // Execute the webview IIFE once so it registers its single window "message"
  // listener. The reason handler queries the DOM at message time, so resetting
  // document.body in beforeEach is enough to isolate tests.
  const src = readFileSync(join(here, "..", "media", "runDetail.js"), "utf8");
  new Function(src)();
});

beforeEach(() => {
  document.body.innerHTML = '<div id="header"></div>';
});

function sendReason(payload: Record<string, unknown>): void {
  window.dispatchEvent(
    new MessageEvent("message", { data: { type: "reason", ...payload } })
  );
}

describe("runDetail.js reason banner", () => {
  it("renders summary, code, and a plural evidence-ref count", () => {
    sendReason({
      code: "run.failed.exception",
      summary: "RuntimeError: boom",
      evidenceRefs: [{ kind: "session", id: "s-1" }, { id: "report" }],
    });

    const banner = document.getElementById("reasonBanner");
    expect(banner).not.toBeNull();
    expect(
      banner!.querySelector(".reason-banner__summary")!.textContent
    ).toBe("RuntimeError: boom");
    expect(banner!.querySelector(".reason-banner__code")!.textContent).toBe(
      "run.failed.exception"
    );
    expect(
      banner!.querySelector(".reason-banner__evidence")!.textContent
    ).toBe("· 2 evidence refs");
  });

  it("uses the singular noun for exactly one evidence ref", () => {
    sendReason({
      summary: "boom",
      evidenceRefs: [{ kind: "session", id: "s-1" }],
    });
    expect(
      document.querySelector(".reason-banner__evidence")!.textContent
    ).toBe("· 1 evidence ref");
  });

  it("renders no evidence span when there are no evidence refs", () => {
    sendReason({ summary: "boom", code: "run.failed.exception" });
    expect(document.querySelector(".reason-banner__evidence")).toBeNull();
  });

  it("clears a stale evidence count when re-targeted to a run with none", () => {
    sendReason({ summary: "first", evidenceRefs: [{ id: "a" }, { id: "b" }] });
    expect(
      document.querySelector(".reason-banner__evidence")!.textContent
    ).toBe("· 2 evidence refs");

    // Re-target the SAME banner (no DOM reset) to a run with no evidence.
    sendReason({ summary: "second", evidenceRefs: [] });
    expect(document.querySelector(".reason-banner__evidence")).toBeNull();
    expect(
      document.querySelector(".reason-banner__summary")!.textContent
    ).toBe("second");
  });
});

describe("runDetail.js malformed event messages", () => {
  // renderEvent dereferences event.role/event.type, so a null/undefined event
  // threw and the throw was swallowed by dispatchEvent. jsdom surfaces it as a
  // synchronous, catchable window "error" event — assert none fires.
  function dispatchAndCaptureErrors(data: Record<string, unknown>): string[] {
    const errors: string[] = [];
    const onErr = (ev: Event): void => {
      const e = ev as ErrorEvent;
      errors.push(String(e.error?.message ?? e.message ?? e));
    };
    window.addEventListener("error", onErr);
    try {
      window.dispatchEvent(new MessageEvent("message", { data }));
    } finally {
      window.removeEventListener("error", onErr);
    }
    return errors;
  }

  it("does not throw on an event message with a null event", () => {
    expect(dispatchAndCaptureErrors({ type: "event", event: null })).toEqual([]);
  });

  it("does not throw on an event message with no event field", () => {
    expect(dispatchAndCaptureErrors({ type: "event" })).toEqual([]);
  });
});
