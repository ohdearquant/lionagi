/**
 * Pairing a page `li studio` opened with the daemon that launched it.
 *
 * The code arrives in the URL fragment, must leave the address bar before
 * anything else reads the location, and is exchanged once for a bearer that
 * the page keeps for that daemon, across reloads, until the daemon refuses it.
 */

import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from "vitest";
import {
  PAIRING_TIMEOUT_MS,
  clearDecisionToken,
  forgetPairedBearer,
  hasPendingPairingCode,
  pairedBearer,
  pairedDecisionToken,
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

  it("exchanges the code for a bearer that only that daemon's requests receive", async () => {
    fetchMock.mockResolvedValue(respond(200, { token: "launch-bearer", token_source: "launch" }));

    expect(await redeemPairingCode(BASE)).toBe("paired");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${BASE}/api/studio/pair`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ code: "one-time-code" });
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
    expect(pairedBearer(BASE)).toBe("launch-bearer");
    expect(pairedBearer("http://127.0.0.1:8766")).toBeUndefined();
    // The bearer is written once to localStorage, under its own key and bound to
    // this daemon; no other response field is stored, and this daemon issued no
    // decision capability to store either.
    expect(sessionWrites).not.toHaveBeenCalled();
    expect(hasPendingPairingCode()).toBe(false);
    expect(localWrites).toHaveBeenCalledTimes(1);
    expect(localWrites).toHaveBeenCalledWith(
      "studio.pairedBearer",
      JSON.stringify({ base: BASE, token: "launch-bearer" }),
    );
    expect(document.cookie).not.toContain("launch-bearer");
  });

  it("shares one exchange between callers that ask while it is in flight", async () => {
    let answer: (response: Response) => void = () => {};
    fetchMock
      .mockReturnValueOnce(new Promise<Response>((resolve) => (answer = resolve)))
      .mockResolvedValue(respond(401, { detail: "used" }));

    const first = redeemPairingCode(BASE);
    expect(hasPendingPairingCode()).toBe(true);
    const second = redeemPairingCode(BASE);
    answer(respond(200, { token: "launch-bearer", token_source: "launch" }));

    expect(await Promise.all([first, second])).toEqual(["paired", "paired"]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(pairedBearer(BASE)).toBe("launch-bearer");
    expect(hasPendingPairingCode()).toBe(false);
  });

  it("reports a code as pending until its exchange settles", async () => {
    let finish: (body: unknown) => void = () => {};
    let reading = false;
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      json: () => {
        reading = true;
        return new Promise((resolve) => (finish = resolve));
      },
    } as unknown as Response);

    const result = redeemPairingCode(BASE);
    await vi.waitFor(() => expect(reading).toBe(true));
    expect(hasPendingPairingCode()).toBe(true);
    finish({ token: "launch-bearer" });

    expect(await result).toBe("paired");
    expect(hasPendingPairingCode()).toBe(false);
  });

  it("gives up on an exchange that gets no answer, keeps the code and lets a retry pair", async () => {
    vi.useFakeTimers();
    try {
      fetchMock
        .mockImplementationOnce(
          (_url: string, init: RequestInit) =>
            new Promise<Response>((_resolve, reject) => {
              init.signal?.addEventListener("abort", () =>
                reject(new DOMException("aborted", "AbortError")),
              );
            }),
        )
        .mockResolvedValue(respond(200, { token: "launch-bearer", token_source: "launch" }));

      let settled = false;
      const first = redeemPairingCode(BASE).finally(() => {
        settled = true;
      });
      await vi.advanceTimersByTimeAsync(PAIRING_TIMEOUT_MS - 1);
      expect(settled).toBe(false);
      await vi.advanceTimersByTimeAsync(1);

      expect(await first).toBe("unreachable");
      expect(hasPendingPairingCode()).toBe(true);
      expect(await redeemPairingCode(BASE)).toBe("paired");
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps a code read from the address bar while an exchange was in flight", async () => {
    let answer: (response: Response) => void = () => {};
    fetchMock
      .mockReturnValueOnce(new Promise<Response>((resolve) => (answer = resolve)))
      .mockResolvedValue(respond(200, { token: "launch-bearer", token_source: "launch" }));

    const first = redeemPairingCode(BASE);
    pendingPage("#pair=second-code");
    answer(respond(401, { detail: "used" }));

    expect(await first).toBe("refused");
    expect(hasPendingPairingCode()).toBe(true);
    expect(await redeemPairingCode(BASE)).toBe("paired");
    const [, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ code: "second-code" });
  });

  it("does not hand one daemon's exchange to a caller asking another", async () => {
    const other = "http://127.0.0.1:8766";
    let answer: (response: Response) => void = () => {};
    fetchMock
      .mockReturnValueOnce(new Promise<Response>((resolve) => (answer = resolve)))
      .mockResolvedValueOnce(respond(401, { detail: "not issued here" }));

    const first = redeemPairingCode(BASE);
    const second = redeemPairingCode(other);
    answer(respond(200, { token: "launch-bearer", token_source: "launch" }));

    expect(await second).toBe("refused");
    expect(await first).toBe("paired");
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      `${BASE}/api/studio/pair`,
      `${other}/api/studio/pair`,
    ]);
    expect(pairedBearer(other)).toBeUndefined();
    expect(pairedBearer(BASE)).toBe("launch-bearer");
  });

  it("keeps the decision capability in sessionStorage for that daemon only", async () => {
    fetchMock.mockResolvedValue(
      respond(200, {
        token: "launch-bearer",
        token_source: "launch",
        decision_token: "decision-capability",
      }),
    );
    let stored: string | null = null;
    vi.stubGlobal("sessionStorage", {
      getItem: (key: string) => (key === "studio.decisionToken" ? stored : null),
      setItem: (key: string, value: string) => {
        if (key === "studio.decisionToken") stored = value;
      },
      removeItem: (key: string) => {
        if (key === "studio.decisionToken") stored = null;
      },
      clear: () => {
        stored = null;
      },
      key: () => null,
      length: 0,
    } as Storage);

    expect(await redeemPairingCode(BASE)).toBe("paired");

    expect(JSON.parse(String(stored))).toEqual({
      base: BASE,
      token: "decision-capability",
    });
    expect(pairedDecisionToken(BASE)).toBe("decision-capability");
    expect(pairedDecisionToken("http://127.0.0.1:8766")).toBeUndefined();
    expect(pairedBearer(BASE)).toBe("launch-bearer");
    // The capability stays out of localStorage: only the bearer is written there.
    expect(localWrites.mock.calls.map(([key]) => key)).toEqual(["studio.pairedBearer"]);
    expect(JSON.stringify(localWrites.mock.calls)).not.toContain("decision-capability");
  });

  it("survives a reload: the capability is read back from sessionStorage", async () => {
    vi.stubGlobal("sessionStorage", {
      getItem: () => JSON.stringify({ base: BASE, token: "decision-capability" }),
      setItem: () => {},
      removeItem: () => {},
      clear: () => {},
      key: () => null,
      length: 1,
    } as Storage);

    expect(pairedDecisionToken(BASE)).toBe("decision-capability");
  });

  it("forgets the capability on disconnect", async () => {
    fetchMock.mockResolvedValue(
      respond(200, {
        token: "launch-bearer",
        token_source: "launch",
        decision_token: "decision-capability",
      }),
    );
    await redeemPairingCode(BASE);
    expect(pairedDecisionToken(BASE)).toBe("decision-capability");

    clearDecisionToken();

    expect(pairedDecisionToken(BASE)).toBeUndefined();
  });

  it("pairs without a decision capability when the daemon issues none", async () => {
    fetchMock.mockResolvedValue(respond(200, { token: "launch-bearer", token_source: "launch" }));

    expect(await redeemPairingCode(BASE)).toBe("paired");
    expect(pairedBearer(BASE)).toBe("launch-bearer");
    expect(pairedDecisionToken(BASE)).toBeUndefined();
  });

  it("spends the code once: a second redeem sends nothing", async () => {
    fetchMock.mockResolvedValue(respond(200, { token: "launch-bearer", token_source: "launch" }));
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

    fetchMock.mockResolvedValueOnce(
      respond(200, { token: "launch-bearer", token_source: "launch" }),
    );
    expect(await redeemPairingCode(BASE)).toBe("paired");
    expect(pairedBearer(BASE)).toBe("launch-bearer");
  });
});

const BEARER_KEY = "studio.pairedBearer";

/** A storage that keeps what it is given, as a browser's does across a reload. */
function keepingStorage(): Storage {
  const data = new Map<string, string>();
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
    removeItem: (key: string) => {
      data.delete(key);
    },
    clear: () => data.clear(),
    key: () => null,
    get length() {
      return data.size;
    },
  } as Storage;
}

/** A storage that refuses every call, as one does when the browser blocks it. */
function refusingStorage(): Storage {
  const refuse = (): never => {
    throw new DOMException("storage is not available", "SecurityError");
  };
  return {
    getItem: refuse,
    setItem: refuse,
    removeItem: refuse,
    clear: refuse,
    key: refuse,
    length: 0,
  } as Storage;
}

describe("a bearer kept across reloads", () => {
  const OTHER = "http://127.0.0.1:8766";
  let fetchMock: ReturnType<typeof vi.fn>;
  let storage: Storage;

  beforeEach(() => {
    resetPairingForTests();
    storage = keepingStorage();
    vi.stubGlobal("localStorage", storage);
    vi.stubGlobal("sessionStorage", keepingStorage());
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    pendingPage("#pair=one-time-code");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/");
  });

  async function pair(token = "launch-bearer"): Promise<void> {
    fetchMock.mockResolvedValueOnce(respond(200, { token, token_source: "launch" }));
    expect(await redeemPairingCode(BASE)).toBe("paired");
  }

  /** A fresh copy of the module, as after a reload: only the storage is kept. */
  async function reload(): Promise<typeof import("./pairing")> {
    vi.resetModules();
    return import("./pairing");
  }

  it("keeps the base and the token it paired with", async () => {
    await pair();

    expect(JSON.parse(String(storage.getItem(BEARER_KEY)))).toEqual({
      base: BASE,
      token: "launch-bearer",
    });
    expect(pairedBearer(BASE)).toBe("launch-bearer");
  });

  it("answers for that daemon after a reload, and for no other", async () => {
    await pair();

    const reloaded = await reload();

    expect(reloaded.pairedBearer(BASE)).toBe("launch-bearer");
    expect(reloaded.pairedBearer(OTHER)).toBeUndefined();
    expect(reloaded.pairedBearer(`${BASE}/`)).toBeUndefined();
  });

  it.each([
    ["text that is not JSON", "not json {"],
    ["JSON null", "null"],
    ["a JSON string", JSON.stringify("launch-bearer")],
    ["an array", JSON.stringify([BASE, "stored-token"])],
    ["a record without a token", JSON.stringify({ base: BASE })],
    ["a record without a base", JSON.stringify({ token: "stored-token" })],
    ["a token that is not text", JSON.stringify({ base: BASE, token: 7 })],
    ["an empty token", JSON.stringify({ base: BASE, token: "" })],
  ])("gives nothing for a stored value that is %s", async (_label, raw) => {
    const reloaded = await reload();
    storage.setItem(BEARER_KEY, JSON.stringify({ base: BASE, token: "stored-token" }));
    expect(reloaded.pairedBearer(BASE)).toBe("stored-token");

    storage.setItem(BEARER_KEY, raw);

    expect(() => reloaded.pairedBearer(BASE)).not.toThrow();
    expect(reloaded.pairedBearer(BASE)).toBeUndefined();
  });

  it("pairs and answers from memory when the storage refuses every call", async () => {
    vi.stubGlobal("localStorage", refusingStorage());

    await pair();

    expect(pairedBearer(BASE)).toBe("launch-bearer");
    expect(pairedBearer(OTHER)).toBeUndefined();
    expect(() => forgetPairedBearer("launch-bearer")).not.toThrow();
    expect(pairedBearer(BASE)).toBeUndefined();
  });

  it("reads nothing, without throwing, from a storage that refuses reads", async () => {
    await pair();
    const reloaded = await reload();
    expect(reloaded.pairedBearer(BASE)).toBe("launch-bearer");

    vi.stubGlobal("localStorage", refusingStorage());

    expect(() => reloaded.pairedBearer(BASE)).not.toThrow();
    expect(reloaded.pairedBearer(BASE)).toBeUndefined();
  });

  it("replaces the stored bearer when the page pairs again", async () => {
    await pair("first-bearer");
    pendingPage("#pair=second-code");
    await pair("second-bearer");

    expect(JSON.parse(String(storage.getItem(BEARER_KEY)))).toEqual({
      base: BASE,
      token: "second-bearer",
    });
    expect((await reload()).pairedBearer(BASE)).toBe("second-bearer");
  });

  it("forgets the bearer from memory and from storage", async () => {
    await pair();

    forgetPairedBearer("launch-bearer");

    expect(storage.getItem(BEARER_KEY)).toBeNull();
    expect(pairedBearer(BASE)).toBeUndefined();
    expect((await reload()).pairedBearer(BASE)).toBeUndefined();
  });

  it("forgets only the refused bearer, keeping one another tab stored since", async () => {
    await pair("first-bearer");
    storage.setItem(BEARER_KEY, JSON.stringify({ base: BASE, token: "second-bearer" }));

    forgetPairedBearer("first-bearer");

    expect(pairedBearer(BASE)).toBe("second-bearer");
    expect((await reload()).pairedBearer(BASE)).toBe("second-bearer");
  });

  it("keeps the bearer this load paired with when an older one is refused", async () => {
    await pair("current-bearer");

    forgetPairedBearer("older-bearer");

    expect(pairedBearer(BASE)).toBe("current-bearer");
    expect(JSON.parse(String(storage.getItem(BEARER_KEY)))).toEqual({
      base: BASE,
      token: "current-bearer",
    });
  });

  it("leaves the decision capability alone when it forgets the bearer", async () => {
    fetchMock.mockResolvedValueOnce(
      respond(200, {
        token: "launch-bearer",
        token_source: "launch",
        decision_token: "decision-capability",
      }),
    );
    await redeemPairingCode(BASE);

    forgetPairedBearer("launch-bearer");

    expect(pairedBearer(BASE)).toBeUndefined();
    expect(pairedDecisionToken(BASE)).toBe("decision-capability");
  });

  it("clears the stored bearer when the pairing state is reset", async () => {
    await pair();

    resetPairingForTests();

    expect(storage.getItem(BEARER_KEY)).toBeNull();
  });

  async function pairAnswering(body: Record<string, unknown>): Promise<void> {
    fetchMock.mockResolvedValueOnce(respond(200, body));
    expect(await redeemPairingCode(BASE)).toBe("paired");
  }

  it("never stores a configured token, and holds it in memory for this load only", async () => {
    await pairAnswering({ token: "configured-token", token_source: "configured" });

    expect(storage.getItem(BEARER_KEY)).toBeNull();
    expect(pairedBearer(BASE)).toBe("configured-token");
    expect((await reload()).pairedBearer(BASE)).toBeUndefined();
  });

  it("stores nothing when the daemon does not say which bearer it sent", async () => {
    await pairAnswering({ token: "unlabelled-token" });

    expect(storage.getItem(BEARER_KEY)).toBeNull();
    expect(pairedBearer(BASE)).toBe("unlabelled-token");
  });

  it("stores nothing for a token_source other than launch", async () => {
    await pairAnswering({ token: "odd-token", token_source: "Launch" });

    expect(storage.getItem(BEARER_KEY)).toBeNull();
  });

  it("removes a stored launch bearer when the page pairs with a configured token", async () => {
    await pair("launch-bearer");
    expect(storage.getItem(BEARER_KEY)).not.toBeNull();
    pendingPage("#pair=second-code");

    await pairAnswering({ token: "configured-token", token_source: "configured" });

    expect(storage.getItem(BEARER_KEY)).toBeNull();
    expect(pairedBearer(BASE)).toBe("configured-token");
  });

  it("offers a stored bearer for the gate's check only when this load has not paired", async () => {
    storage.setItem(BEARER_KEY, JSON.stringify({ base: BASE, token: "stored-token" }));
    const fresh = await reload();

    expect(fresh.storedBearerToCheck(BASE)).toBe("stored-token");
    expect(fresh.storedBearerToCheck(OTHER)).toBeUndefined();

    fetchMock.mockResolvedValueOnce(respond(200, { token: "new-bearer", token_source: "launch" }));
    window.history.replaceState(null, "", "/#pair=next-code");
    fresh.takePairingCodeFromLocation();
    expect(await fresh.redeemPairingCode(BASE)).toBe("paired");
    expect(fresh.storedBearerToCheck(BASE)).toBeUndefined();
  });
});
