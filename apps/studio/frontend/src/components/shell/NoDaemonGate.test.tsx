/**
 * NoDaemonGate — daemon connectivity recovery surface.
 *
 * No @testing-library/react in this project (see history/InvocationDetail.test.tsx);
 * mounts via react-dom/client + act and drives the real /health probe through
 * a stubbed global fetch, same pattern as mission/usePulse.test.tsx.
 *
 * Covers:
 * - a composed shell skeleton renders while the first probe is pending
 * - data-dependent children render only after a real daemon answers
 * - unreachable (fetch throws — no daemon, CORS, or nothing listening)
 * - wrongApp (fetch resolves but the body isn't the lionagi `{ status: "ok" }` shape)
 * - connected (fetch resolves with the exact lionagi health shape)
 * - retry re-probes and restores the app on success
 * - a connectivity failure reported from elsewhere in the app (any other
 *   fetchJson call hitting a network-level error) triggers an immediate
 *   re-probe instead of waiting for the poll interval
 * - a page `li studio` opened redeems its pairing code once the daemon answers
 * - accessRequired (the daemon refused the code, or refused a request later),
 *   which names `li studio` as the remedy and does not poll
 * - a bearer an earlier load stored is checked with one request before the
 *   app mounts: only a 2xx mounts the app, a 401 forgets it and asks to pair
 *   again, any other answer or none in time shows accessRequired and keeps it,
 *   a network failure keeps it and shows unreachable
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import NoDaemonGate from "./NoDaemonGate";
import enMessages from "@/messages/en.json";
import { reportAccessRefused, reportConnectivityFailure } from "@/lib/connectivity";
import {
  PAIRING_TIMEOUT_MS,
  pairedBearer,
  resetPairingForTests,
  takePairingCodeFromLocation,
} from "@/lib/pairing";

const apiConfig = vi.hoisted(() => ({ base: "http://127.0.0.1:8765" }));

vi.mock("@/lib/api", () => ({
  get API_BASE() {
    return apiConfig.base;
  },
}));

function jsonResponse(body: unknown, ok = true, status = ok ? 200 : 404): Response {
  return {
    ok,
    status,
    json: () => Promise.resolve(body),
  } as Response;
}

describe("NoDaemonGate", () => {
  let container: HTMLDivElement;
  let root: Root;
  let unmounted: boolean;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    apiConfig.base = "http://127.0.0.1:8765";
    container = document.createElement("div");
    document.body.appendChild(container);
    unmounted = false;
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    // jsdom's `window.localStorage` has no Storage methods in this vitest
    // environment, so the page's storage is a working in-memory one.
    const data = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => data.get(key) ?? null,
      setItem: (key: string, value: string) => {
        data.set(key, String(value));
      },
      removeItem: (key: string) => {
        data.delete(key);
      },
      clear: () => data.clear(),
      key: () => null,
      get length() {
        return data.size;
      },
    } as Storage);
    vi.useFakeTimers();
    resetPairingForTests();
    window.history.replaceState(null, "", "/");
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

  async function mount({ strict = false } = {}) {
    const gate = (
      <IntlProvider locale="en" messages={enMessages}>
        <NoDaemonGate>
          <div data-testid="child">app content</div>
        </NoDaemonGate>
      </IntlProvider>
    );
    await act(async () => {
      root = createRoot(container);
      root.render(strict ? <StrictMode>{gate}</StrictMode> : gate);
      await Promise.resolve();
    });
  }

  it("renders a composed shell skeleton while the first probe is pending", async () => {
    fetchMock.mockReturnValue(new Promise(() => {}));
    await mount();
    expect(container.querySelector('[data-testid="child"]')).toBeNull();
    expect(container.querySelector('[role="status"]')).not.toBeNull();
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(container.textContent).toContain("LION STUDIO");
  });

  it("renders children once a real daemon answers", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ status: "ok" }));
    await mount();
    expect(container.querySelector('[data-testid="child"]')).not.toBeNull();
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("shows the unreachable recovery state on a network failure", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await mount();
    expect(container.querySelector('[data-testid="child"]')).toBeNull();
    const recovery = container.querySelector('[role="alert"]');
    expect(recovery).not.toBeNull();
    expect(recovery?.textContent).toContain("Studio needs a local lionagi daemon");
    expect(recovery?.textContent).toContain('pip install "lionagi[studio]"');
    expect(recovery?.textContent).toContain("li studio");
  });

  it("shows the page origin when health requests use the same-origin base", async () => {
    apiConfig.base = "";
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await mount();
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/health");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      window.location.origin,
    );
  });

  it("shows a distinct wrong-app recovery state when the health shape is invalid", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ hello: "world" }));
    await mount();
    expect(container.querySelector('[data-testid="child"]')).toBeNull();
    const recovery = container.querySelector('[role="alert"]');
    expect(recovery).not.toBeNull();
    expect(recovery?.textContent).toContain("Another program is using this port");
    expect(recovery?.textContent).toContain("li studio --port 8766");
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

  it("retry re-probes and restores the app once the daemon comes up", async () => {
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
    expect(container.querySelector('[data-testid="child"]')).not.toBeNull();
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

  function daemonAnswering(pair: Response) {
    fetchMock.mockImplementation((url: string) => {
      if (url.endsWith("/health")) return Promise.resolve(jsonResponse({ status: "ok" }));
      if (url.endsWith("/api/studio/pair")) return Promise.resolve(pair);
      return Promise.reject(new Error(`unexpected request: ${url}`));
    });
  }

  async function settle() {
    await act(async () => {
      for (let i = 0; i < 10; i++) await Promise.resolve();
    });
  }

  const BEARER_KEY = "studio.pairedBearer";

  function storeBearer(token: string) {
    window.localStorage.setItem(
      BEARER_KEY,
      JSON.stringify({ base: "http://127.0.0.1:8765", token }),
    );
  }

  function daemonCheckingBearer(check: (init?: RequestInit) => Promise<Response>) {
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url.endsWith("/health")) return Promise.resolve(jsonResponse({ status: "ok" }));
      if (url.endsWith("/api/casts/")) return check(init);
      return Promise.reject(new Error(`unexpected request: ${url}`));
    });
  }

  function checkCalls() {
    return fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/api/casts/"));
  }

  it("checks a stored bearer before mounting and, on a 401, forgets it and asks to pair again", async () => {
    storeBearer("stale-bearer");
    daemonCheckingBearer(() => Promise.resolve(jsonResponse({ detail: "refused" }, false, 401)));

    await mount();
    await settle();

    expect(checkCalls()).toHaveLength(1);
    expect(checkCalls()[0][1]).toMatchObject({
      headers: { Authorization: "Bearer stale-bearer" },
    });
    expect(checkCalls()[0][1].signal).toBeInstanceOf(AbortSignal);
    expect(container.querySelector('[data-testid="child"]')).toBeNull();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "This daemon needs an access token",
    );
    expect(window.localStorage.getItem(BEARER_KEY)).toBeNull();
    expect(pairedBearer("http://127.0.0.1:8765")).toBeUndefined();
  });

  it("does not mount the app while the check is pending, and reads no answer in time as unvalidated", async () => {
    storeBearer("stored-bearer");
    daemonCheckingBearer(
      (init) =>
        new Promise((_, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("The operation was aborted.", "AbortError")),
          );
        }),
    );

    await mount();
    await settle();

    expect(checkCalls()).toHaveLength(1);
    expect(container.querySelector('[data-testid="child"]')).toBeNull();
    expect(container.querySelector('[role="status"]')).not.toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PAIRING_TIMEOUT_MS);
    });
    await settle();

    expect(container.querySelector('[data-testid="child"]')).toBeNull();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "This daemon needs an access token",
    );
    expect(window.localStorage.getItem(BEARER_KEY)).not.toBeNull();
  });

  for (const status of [404, 500]) {
    it(`does not mount the app on a ${status} to the check, and keeps the stored bearer`, async () => {
      storeBearer("stored-bearer");
      daemonCheckingBearer(() => Promise.resolve(jsonResponse({ detail: "no" }, false, status)));

      await mount();
      await settle();

      expect(container.querySelector('[data-testid="child"]')).toBeNull();
      expect(container.querySelector('[role="alert"]')?.textContent).toContain(
        "This daemon needs an access token",
      );
      expect(window.localStorage.getItem(BEARER_KEY)).not.toBeNull();
      expect(pairedBearer("http://127.0.0.1:8765")).toBe("stored-bearer");
    });
  }

  it("mounts the app once the daemon accepts a stored bearer", async () => {
    storeBearer("stored-bearer");
    daemonCheckingBearer(() => Promise.resolve(jsonResponse({ roles: [] })));

    await mount();
    await settle();

    expect(container.querySelector('[data-testid="child"]')).not.toBeNull();
    expect(window.localStorage.getItem(BEARER_KEY)).not.toBeNull();
    expect(pairedBearer("http://127.0.0.1:8765")).toBe("stored-bearer");
  });

  it("keeps a stored bearer and shows unreachable when its check does not reach the daemon", async () => {
    storeBearer("stored-bearer");
    daemonCheckingBearer(() => Promise.reject(new TypeError("Failed to fetch")));

    await mount();
    await settle();

    expect(container.querySelector('[data-testid="child"]')).toBeNull();
    expect(container.querySelector('[role="alert"]')).not.toBeNull();
    expect(container.querySelector('[role="alert"]')?.textContent).not.toContain(
      "This daemon needs an access token",
    );
    expect(window.localStorage.getItem(BEARER_KEY)).not.toBeNull();
  });

  it("sends no check when nothing is stored", async () => {
    daemonCheckingBearer(() => Promise.resolve(jsonResponse({ roles: [] })));

    await mount();
    await settle();

    expect(checkCalls()).toHaveLength(0);
    expect(container.querySelector('[data-testid="child"]')).not.toBeNull();
  });

  function pairCalls() {
    return fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/api/studio/pair"));
  }

  it("redeems the pairing code of a page `li studio` opened, then shows the app", async () => {
    window.history.replaceState(null, "", "/#pair=one-time-code");
    takePairingCodeFromLocation();
    daemonAnswering(jsonResponse({ token: "launch-bearer" }));

    await mount();
    await settle();

    expect(pairCalls()).toHaveLength(1);
    expect(pairedBearer("http://127.0.0.1:8765")).toBe("launch-bearer");
    expect(container.querySelector('[data-testid="child"]')).not.toBeNull();
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("pairs once under StrictMode, whose doubled effect connects twice", async () => {
    window.history.replaceState(null, "", "/#pair=one-time-code");
    takePairingCodeFromLocation();
    let spent = false;
    fetchMock.mockImplementation((url: string) => {
      if (url.endsWith("/health")) return Promise.resolve(jsonResponse({ status: "ok" }));
      if (url.endsWith("/api/studio/pair")) {
        const first = !spent;
        spent = true;
        return Promise.resolve(
          first
            ? jsonResponse({ token: "launch-bearer" })
            : jsonResponse({ detail: "used" }, false, 401),
        );
      }
      return Promise.reject(new Error(`unexpected request: ${url}`));
    });

    await mount({ strict: true });
    await settle();

    expect(pairCalls()).toHaveLength(1);
    expect(pairedBearer("http://127.0.0.1:8765")).toBe("launch-bearer");
    expect(container.querySelector('[data-testid="child"]')).not.toBeNull();
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("shows the access-required state with the `li studio` remedy when the code is refused", async () => {
    window.history.replaceState(null, "", "/#pair=spent-code");
    takePairingCodeFromLocation();
    daemonAnswering(jsonResponse({ detail: "expired" }, false, 401));

    await mount();
    await settle();

    expect(container.querySelector('[data-testid="child"]')).toBeNull();
    const recovery = container.querySelector('[role="alert"]');
    expect(recovery?.textContent).toContain("This daemon needs an access token");
    expect(recovery?.textContent).toContain("li studio");
    expect(recovery?.textContent).not.toContain("Another program is using this port");
  });

  it("does not poll while access is refused, so it never flaps back to the app", async () => {
    window.history.replaceState(null, "", "/#pair=spent-code");
    takePairingCodeFromLocation();
    daemonAnswering(jsonResponse({ detail: "expired" }, false, 401));
    await mount();
    await settle();
    const calls = fetchMock.mock.calls.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15000);
    });

    expect(fetchMock.mock.calls.length).toBe(calls);
    expect(container.querySelector('[data-testid="child"]')).toBeNull();
  });

  it("keeps the code and stays unreachable when the pairing request fails on the network", async () => {
    window.history.replaceState(null, "", "/#pair=one-time-code");
    takePairingCodeFromLocation();
    fetchMock.mockImplementation((url: string) =>
      url.endsWith("/health")
        ? Promise.resolve(jsonResponse({ status: "ok" }))
        : Promise.reject(new TypeError("Failed to fetch")),
    );
    await mount();
    await settle();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "Studio needs a local lionagi daemon",
    );

    daemonAnswering(jsonResponse({ token: "launch-bearer" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    await settle();

    expect(pairCalls()).toHaveLength(2);
    expect(container.querySelector('[data-testid="child"]')).not.toBeNull();
  });

  it("switches to the access-required state when a request is refused while connected", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ status: "ok" }));
    await mount();
    expect(container.querySelector('[data-testid="child"]')).not.toBeNull();

    await act(async () => {
      reportAccessRefused();
      await Promise.resolve();
    });

    expect(container.querySelector('[data-testid="child"]')).toBeNull();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "This daemon needs an access token",
    );
    expect(pairCalls()).toHaveLength(0);
  });
});
