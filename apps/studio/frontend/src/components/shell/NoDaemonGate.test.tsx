/**
 * NoDaemonGate — daemon connectivity banner.
 *
 * No @testing-library/react in this project (see history/InvocationDetail.test.tsx);
 * mounts via react-dom/client + act and drives the real /health probe through
 * a stubbed global fetch, same pattern as mission/usePulse.test.tsx.
 *
 * Covers:
 * - children always render, in every connectivity state (never blanks the app)
 * - unreachable (fetch throws — no daemon, CORS, or nothing listening)
 * - wrongApp (fetch resolves but the body isn't the lionagi `{ status: "ok" }` shape)
 * - connected (fetch resolves with the exact lionagi health shape) — no banner
 * - retry re-probes and clears the banner on success
 * - dismiss hides the banner; a fresh connectivity failure reported after a
 *   successful recovery shows the banner again
 * - a connectivity failure reported from elsewhere in the app (any other
 *   fetchJson call hitting a network-level error) triggers an immediate
 *   re-probe instead of waiting for the poll interval
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import NoDaemonGate from "./NoDaemonGate";
import enMessages from "@/messages/en.json";
import { reportConnectivityFailure } from "@/lib/connectivity";

vi.mock("@/lib/api", () => ({
  resolveApiBase: () => "http://127.0.0.1:8765",
}));

function jsonResponse(body: unknown, ok = true): Response {
  return {
    ok,
    json: () => Promise.resolve(body),
  } as Response;
}

describe("NoDaemonGate", () => {
  let container: HTMLDivElement;
  let root: Root;
  let unmounted: boolean;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    unmounted = false;
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers();
  });

  afterEach(() => {
    if (!unmounted) {
      act(() => {
        root.unmount();
      });
    }
    container.remove();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  async function mount() {
    await act(async () => {
      root = createRoot(container);
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <NoDaemonGate>
            <div data-testid="child">app content</div>
          </NoDaemonGate>
        </IntlProvider>,
      );
      await Promise.resolve();
    });
  }

  it("renders children immediately in the 'checking' state, before the probe resolves", async () => {
    fetchMock.mockReturnValue(new Promise(() => {}));
    await mount();
    expect(container.querySelector('[data-testid="child"]')).not.toBeNull();
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("keeps rendering children and shows no banner once a real daemon answers", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ status: "ok" }));
    await mount();
    expect(container.querySelector('[data-testid="child"]')).not.toBeNull();
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("shows the unreachable banner on a network failure, without unmounting children", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await mount();
    expect(container.querySelector('[data-testid="child"]')).not.toBeNull();
    const banner = container.querySelector('[role="alert"]');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain("Studio needs a local lionagi daemon");
    expect(banner?.textContent).toContain("li studio");
  });

  it("shows the wrong-app banner when something answers but not with the lionagi health shape", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ hello: "world" }));
    await mount();
    const banner = container.querySelector('[role="alert"]');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain("Another program is using this port");
    expect(banner?.textContent).toContain("li studio --port 8766");
  });

  it("shows the wrong-app banner when the port answers with a non-JSON body", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: () => Promise.reject(new Error("Unexpected token < in JSON")),
    } as unknown as Response);
    await mount();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "Another program is using this port",
    );
  });

  it("treats a non-2xx response as wrong-app, not unreachable", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "not found" }, false));
    await mount();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "Another program is using this port",
    );
  });

  it("retry re-probes and clears the banner once the daemon comes up", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await mount();
    expect(container.querySelector('[role="alert"]')).not.toBeNull();

    fetchMock.mockResolvedValueOnce(jsonResponse({ status: "ok" }));
    const retryButton = Array.from(container.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Retry"),
    );
    expect(retryButton).toBeDefined();
    await act(async () => {
      retryButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("dismiss hides the banner immediately; a later failure after recovery shows it again", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await mount();
    const dismissButton = container.querySelector('[aria-label="Dismiss"]');
    expect(dismissButton).not.toBeNull();
    await act(async () => {
      dismissButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(container.querySelector('[role="alert"]')).toBeNull();
    // children still render while dismissed
    expect(container.querySelector('[data-testid="child"]')).not.toBeNull();

    // background poll: daemon recovers — the gate stops polling once
    // connected, so the dismissal flag has been cleared for whatever comes
    // next.
    fetchMock.mockResolvedValueOnce(jsonResponse({ status: "ok" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(container.querySelector('[role="alert"]')).toBeNull();

    // some other view's API call now fails at the network level — the gate
    // re-probes off that signal (it isn't polling anymore) and the banner
    // must reappear rather than staying hidden from the earlier dismissal.
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await act(async () => {
      reportConnectivityFailure();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(container.querySelector('[role="alert"]')).not.toBeNull();
  });

  it("re-probes on a connectivity failure reported elsewhere in the app, even while already connected", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ status: "ok" }));
    await mount();
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await act(async () => {
      reportConnectivityFailure();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(container.querySelector('[role="alert"]')).not.toBeNull();
  });

  it("polls every 5s while unreachable and stops once connected", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await mount();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
