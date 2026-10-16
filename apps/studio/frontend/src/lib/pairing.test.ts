/**
 * Pairing a page `li studio` opened with the daemon that launched it.
 *
 * The code arrives in the URL fragment, must leave the address bar before
 * anything else reads the location, and is exchanged once for a bearer that
 * lives in memory only.
 */

import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from "vitest";
import {
  hasPendingPairingCode,
  pairedBearer,
  redeemPairingCode,
  resetPairingForTests,
  takePairingCodeFromLocation,
} from "./pairing";

const BASE = "http://127.0.0.1:8765";

function respond(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status });
}

function pendingPage(fragment: string, path = "/") {
  window.history.replaceState(null, "", `${path}${fragment}`);
  takePairingCodeFromLocation();
}

describe("takePairingCodeFromLocation", () => {
  beforeEach(() => {
    resetPairingForTests();
    window.history.replaceState(null, "", "/");
  });

  it("removes the code from the address bar and keeps it in memory", () => {
    pendingPage("#pair=one-time-code");
    expect(window.location.hash).toBe("");
    expect(window.location.href).not.toContain("one-time-code");
    expect(hasPendingPairingCode()).toBe(true);
  });

  it("keeps the path, the query and any other fragment parameter", () => {
    pendingPage("#tab=logs&pair=one-time-code", "/runs?api=http://127.0.0.1:8766");
    expect(window.location.pathname).toBe("/runs");
    expect(window.location.search).toBe("?api=http://127.0.0.1:8766");
    expect(window.location.hash).toBe("#tab=logs");
    expect(window.location.href).not.toContain("one-time-code");
  });

  it("leaves a page without a code alone", () => {
    pendingPage("#tab=logs");
    expect(window.location.hash).toBe("#tab=logs");
    expect(hasPendingPairingCode()).toBe(false);
  });

  it("drops an empty code without keeping one", () => {
    pendingPage("#pair=");
    expect(window.location.hash).toBe("");
    expect(hasPendingPairingCode()).toBe(false);
  });
});

function writeSpyStorage(write: (key: string, value: string) => void): Storage {
  return {
    getItem: () => null,
    setItem: write,
    removeItem: () => {},
    clear: () => {},
    key: () => null,
    length: 0,
  } as Storage;
}

describe("redeemPairingCode", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let localWrites: Mock<(key: string, value: string) => void>;
  let sessionWrites: Mock<(key: string, value: string) => void>;

  beforeEach(() => {
    resetPairingForTests();
    localWrites = vi.fn<(key: string, value: string) => void>();
    sessionWrites = vi.fn<(key: string, value: string) => void>();
    vi.stubGlobal("localStorage", writeSpyStorage(localWrites));
    vi.stubGlobal("sessionStorage", writeSpyStorage(sessionWrites));
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    pendingPage("#pair=one-time-code");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/");
  });

  it("exchanges the code for a bearer held in memory for that daemon only", async () => {
    fetchMock.mockResolvedValue(respond(200, { token: "launch-bearer" }));

    expect(await redeemPairingCode(BASE)).toBe("paired");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${BASE}/api/studio/pair`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ code: "one-time-code" });
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
    expect(pairedBearer(BASE)).toBe("launch-bearer");
    expect(pairedBearer("http://127.0.0.1:8766")).toBeUndefined();
    expect(hasPendingPairingCode()).toBe(false);
    expect(localWrites).not.toHaveBeenCalled();
    expect(sessionWrites).not.toHaveBeenCalled();
    expect(document.cookie).not.toContain("launch-bearer");
  });

  it("spends the code once: a second redeem sends nothing", async () => {
    fetchMock.mockResolvedValue(respond(200, { token: "launch-bearer" }));
    await redeemPairingCode(BASE);

    expect(await redeemPairingCode(BASE)).toBe("refused");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("drops a code the daemon refuses", async () => {
    fetchMock.mockResolvedValue(respond(401, { detail: "expired" }));

    expect(await redeemPairingCode(BASE)).toBe("refused");
    expect(hasPendingPairingCode()).toBe(false);
    expect(pairedBearer(BASE)).toBeUndefined();
  });

  it("refuses an answer without a token", async () => {
    fetchMock.mockResolvedValue(respond(200, { token: "" }));

    expect(await redeemPairingCode(BASE)).toBe("refused");
    expect(pairedBearer(BASE)).toBeUndefined();
  });

  it("keeps the code when it never reached the daemon, and pairs on the next try", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    expect(await redeemPairingCode(BASE)).toBe("unreachable");
    expect(hasPendingPairingCode()).toBe(true);

    fetchMock.mockResolvedValueOnce(respond(503, { detail: "starting" }));
    expect(await redeemPairingCode(BASE)).toBe("unreachable");
    expect(hasPendingPairingCode()).toBe(true);

    fetchMock.mockResolvedValueOnce(respond(200, { token: "launch-bearer" }));
    expect(await redeemPairingCode(BASE)).toBe("paired");
    expect(pairedBearer(BASE)).toBe("launch-bearer");
  });
});
