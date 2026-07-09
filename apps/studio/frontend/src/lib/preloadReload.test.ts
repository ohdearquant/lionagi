import { describe, it, expect } from "vitest";
import { handlePreloadError, PRELOAD_RELOAD_KEY } from "./preloadReload";

function fakeStorage(seed: Record<string, string> = {}) {
  const store = { ...seed };
  return {
    getItem: (key: string) => store[key] ?? null,
    setItem: (key: string, value: string) => {
      store[key] = value;
    },
  };
}

describe("handlePreloadError", () => {
  it("reloads and sets the guard flag on first occurrence", () => {
    const storage = fakeStorage();
    let reloaded = false;
    const did = handlePreloadError(storage, () => {
      reloaded = true;
    });
    expect(did).toBe(true);
    expect(reloaded).toBe(true);
    expect(storage.getItem(PRELOAD_RELOAD_KEY)).toBe("1");
  });

  it("does not reload again within the same guard window (repeat occurrence)", () => {
    const storage = fakeStorage({ [PRELOAD_RELOAD_KEY]: "1" });
    let reloaded = false;
    const did = handlePreloadError(storage, () => {
      reloaded = true;
    });
    expect(did).toBe(false);
    expect(reloaded).toBe(false);
  });
});
