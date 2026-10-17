import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import { API_BASE, listInvocations } from "@/lib/api";
import { pairedBearer, resetPairingForTests, takePairingCodeFromLocation } from "@/lib/pairing";
import NoDaemonGate from "./NoDaemonGate";
import StatusFooter from "./StatusFooter";
import enMessages from "@/messages/en.json";

// Select the real API module's destination before its first import, then navigate
// without reloading it, as the client router does.
const initial = vi.hoisted(() => {
  const snapshot = {
    url: window.location.href,
    base: window.__STUDIO_API_BASE__,
    token: window.__STUDIO_AUTH_TOKEN__,
    storage: Object.getOwnPropertyDescriptor(window, "sessionStorage"),
  };
  const storage = new Map<string, string>();
  Object.defineProperty(window, "sessionStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => void storage.set(key, value),
    },
  });
  window.history.replaceState(null, "", "?api=http://127.0.0.2:8766");
  window.__STUDIO_API_BASE__ = "http://127.0.0.1:8765";
  window.__STUDIO_AUTH_TOKEN__ = "shell-token";
  return snapshot;
});

describe("the loaded API destination", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.useFakeTimers();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    window.history.replaceState(null, "", "?api=http://127.0.0.1:8765");
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/");
    resetPairingForTests();
  });

  afterAll(() => {
    window.history.replaceState(null, "", initial.url);
    window.__STUDIO_API_BASE__ = initial.base;
    window.__STUDIO_AUTH_TOKEN__ = initial.token;
    if (initial.storage) Object.defineProperty(window, "sessionStorage", initial.storage);
    else Reflect.deleteProperty(window, "sessionStorage");
  });

  it.each(["gate", "footer"] as const)(
    "keeps the %s health probes and endpoint display aligned with data after navigation",
    async (surface) => {
      expect(API_BASE).toBe("http://127.0.0.2:8766");
      const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>((url) =>
        Promise.resolve(
          new Response(url.endsWith("/health") ? '{"status":"unavailable"}' : "{}", {
            status: url.endsWith("/health") ? 503 : 200,
            headers: { "Content-Type": "application/json" },
          }),
        ),
      );
      vi.stubGlobal("fetch", fetchMock);
      const render = () => (
        <IntlProvider locale="en" messages={enMessages}>
          {surface === "gate" ? <NoDaemonGate>app content</NoDaemonGate> : <StatusFooter />}
        </IntlProvider>
      );
      await act(async () => root.render(render()));
      expect(container.textContent).toContain(API_BASE);
      expect(container.textContent).not.toContain("http://127.0.0.1:8765");

      window.history.replaceState(null, "", "?tab=agent");
      // The injected base the shell token was issued for: a destination read again here
      // would match it and send the token.
      window.sessionStorage.setItem("studio.apiBaseOverride", "http://127.0.0.1:8765");
      await act(async () => {
        root.render(render());
        await vi.advanceTimersByTimeAsync(30_000);
      });
      await listInvocations();

      expect(container.textContent).toContain(API_BASE);
      expect(container.textContent).not.toContain("http://127.0.0.1:8765");
      const calls = fetchMock.mock.calls;
      expect(calls.filter(([url]) => url.endsWith("/health")).length).toBeGreaterThan(1);
      expect(calls.some(([url]) => url.endsWith("/api/invocations/"))).toBe(true);
      for (const [url, init] of calls) {
        expect(new URL(url).origin).toBe(API_BASE);
        expect(new Headers(init?.headers).has("Authorization")).toBe(false);
      }
    },
  );

  it("redeems the pairing code, and reads the bearer, under the override's base", async () => {
    expect(API_BASE).toBe("http://127.0.0.2:8766");
    window.history.replaceState(null, "", `?api=${API_BASE}#pair=one-time-code`);
    takePairingCodeFromLocation();
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>((url) =>
      Promise.resolve(
        new Response(url.endsWith("/health") ? '{"status":"ok"}' : '{"token":"launch-bearer"}', {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await act(async () =>
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <NoDaemonGate>app content</NoDaemonGate>
        </IntlProvider>,
      ),
    );
    expect(container.textContent).toContain("app content");

    const redeemed = fetchMock.mock.calls.filter(([url]) => url.endsWith("/api/studio/pair"));
    expect(redeemed).toHaveLength(1);
    expect(new URL(redeemed[0]![0]).origin).toBe(API_BASE);
    expect(pairedBearer(API_BASE)).toBe("launch-bearer");
  });
});
