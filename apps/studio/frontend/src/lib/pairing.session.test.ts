import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const BASE = "http://127.0.0.1:8765";
const OTHER = "http://127.0.0.1:8766";
const KEY = "studio.pairedBearer";

function storage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => {
      values.delete(key);
    },
    setItem: (key, value) => {
      values.set(key, String(value));
    },
  };
}

describe("pairing bearer storage", () => {
  let local: Storage;
  let session: Storage;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.resetModules();
    local = storage();
    session = storage();
    vi.stubGlobal("localStorage", local);
    vi.stubGlobal("sessionStorage", session);
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/");
  });

  async function reload() {
    vi.resetModules();
    return import("./pairing");
  }

  async function pair(body: Record<string, unknown>, base = BASE) {
    const pairing = await import("./pairing");
    window.history.replaceState(null, "", "/#pair=code");
    pairing.takePairingCodeFromLocation();
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(body), { status: 200 }));
    expect(await pairing.redeemPairingCode(base)).toBe("paired");
    return pairing;
  }

  it.each(["configured", undefined, "Launch", "other"])(
    "keeps source %s in the current tab only, including after reload",
    async (token_source) => {
      const localWrite = vi.spyOn(local, "setItem");
      const pairing = await pair({ token: "configured-secret", token_source });
      expect(localWrite).not.toHaveBeenCalled();
      expect(local.getItem(KEY)).toBeNull();
      expect(pairing.pairedBearer(BASE)).toBe("configured-secret");
      const fresh = await reload();
      expect(fresh.storedBearerToCheck(BASE)).toBe("configured-secret");
      expect(fresh.pairedBearer(BASE)).toBe("configured-secret");
      expect(fresh.pairedBearer(OTHER)).toBeUndefined();
      vi.stubGlobal("sessionStorage", storage());
      expect((await reload()).pairedBearer(BASE)).toBeUndefined();
    },
  );

  it("stores only an explicitly marked launch bearer across reload", async () => {
    await pair({ token: "launch", token_source: "launch" });
    expect(JSON.parse(local.getItem(KEY)!)).toEqual({
      base: BASE,
      token: "launch",
      token_source: "launch",
    });
    expect((await reload()).storedBearerToCheck(BASE)).toBe("launch");
  });

  it.each(["configured", undefined, "other"])(
    "refuses to write source %s to local storage even if asked directly",
    async (token_source) => {
      const pairing = await reload();
      const localWrite = vi.spyOn(local, "setItem");
      pairing.writeStoredBearerForTests("localStorage", {
        base: BASE,
        token: "configured-secret",
        token_source,
      });
      expect(localWrite).not.toHaveBeenCalled();
      expect(local.getItem(KEY)).toBeNull();
    },
  );

  it("removes legacy local records at module load and never offers their bearer", async () => {
    local.setItem(KEY, JSON.stringify({ base: BASE, token: "old-credential" }));
    const pairing = await reload();
    expect(local.getItem(KEY)).toBeNull();
    expect(pairing.storedBearerToCheck(BASE)).toBeUndefined();
    expect(pairing.pairedBearer(BASE)).toBeUndefined();
  });

  it("replaces a matching stored launch with session-only pairing", async () => {
    await pair({ token: "launch", token_source: "launch" });
    await pair({ token: "configured", token_source: "configured" });
    expect(local.getItem(KEY)).toBeNull();
    expect((await reload()).pairedBearer(BASE)).toBe("configured");
  });

  it("retains a launch bearer for a different base", async () => {
    local.setItem(KEY, JSON.stringify({ base: OTHER, token: "other", token_source: "launch" }));
    await pair({ token: "configured", token_source: "configured" });
    const fresh = await reload();
    expect(fresh.pairedBearer(BASE)).toBe("configured");
    expect(fresh.pairedBearer(OTHER)).toBe("other");
  });

  it("does not let a late refusal remove a newer session token", async () => {
    const pairing = await pair({ token: "old", token_source: "configured" });
    await pair({ token: "new", token_source: "configured" });
    pairing.forgetPairedBearer("old", BASE);
    expect(pairing.pairedBearer(BASE)).toBe("new");
    expect((await reload()).pairedBearer(BASE)).toBe("new");
  });

  it("keeps a configured bearer in memory if session storage is unavailable", async () => {
    const unavailable = () => {
      throw new DOMException("unavailable", "SecurityError");
    };
    vi.stubGlobal("sessionStorage", {
      getItem: unavailable,
      setItem: unavailable,
      removeItem: unavailable,
    });
    const pairing = await pair({ token: "configured", token_source: "configured" });
    expect(local.getItem(KEY)).toBeNull();
    expect(pairing.pairedBearer(BASE)).toBe("configured");
    expect((await reload()).pairedBearer(BASE)).toBeUndefined();
  });

  it("never returns an unmarked legacy bearer when removal fails", async () => {
    local.setItem(KEY, JSON.stringify({ base: BASE, token: "old-credential" }));
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => local.getItem(key),
      removeItem: () => {
        throw new DOMException("unavailable", "SecurityError");
      },
      setItem: () => {
        throw new DOMException("unavailable", "SecurityError");
      },
    });
    const pairing = await reload();
    expect(pairing.pairedBearer(BASE)).toBeUndefined();
    expect(pairing.storedBearerToCheck(BASE)).toBeUndefined();
  });

  it("does not forget an identical bearer for a different base", async () => {
    local.setItem(KEY, JSON.stringify({ base: OTHER, token: "same", token_source: "launch" }));
    const pairing = await pair({ token: "same", token_source: "configured" });
    pairing.forgetPairedBearer("same", BASE);
    expect(pairing.pairedBearer(BASE)).toBeUndefined();
    expect(pairing.pairedBearer(OTHER)).toBe("same");
  });

  it("removes a same-base session bearer when pairing with a launch bearer", async () => {
    await pair({ token: "configured", token_source: "configured" });
    await pair({ token: "launch", token_source: "launch" });
    expect(session.getItem(KEY)).toBeNull();
    expect((await reload()).pairedBearer(BASE)).toBe("launch");
  });
});
