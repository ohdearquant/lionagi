/**
 * StatusFooter — the DB size reading.
 *
 * No @testing-library/react in this project; mounts via react-dom/client + act
 * and stubs the api module, same pattern as NoDaemonGate.test.tsx.
 *
 * The backend decides whether the store is over its threshold and says so in
 * `size_alert`. The footer rendered the size in the same muted grey either
 * way, so a store many times over its limit was indistinguishable from a
 * healthy one. Both arms are here: without the under-threshold arm, an
 * implementation that always paints the warning passes.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import StatusFooter from "./StatusFooter";
import enMessages from "@/messages/en.json";

const getStats = vi.fn();

vi.mock("@/lib/api", () => ({
  resolveApiBase: () => "http://127.0.0.1:8765",
  getStats: (...args: unknown[]) => getStats(...args),
}));

const GB = 1024 * 1024 * 1024;
const MB = 1024 * 1024;

function statsWith(db: Record<string, unknown>) {
  return {
    playbooks: 0,
    agents: 0,
    runs: 0,
    shows: 0,
    skills: 0,
    plugins: 0,
    db: {
      path: ".lionagi/state.db",
      wal_bytes: 0,
      connections_active: 0,
      last_checkpoint_at: null,
      ...db,
    },
  };
}

async function mountFooter(container: HTMLElement): Promise<Root> {
  let root!: Root;
  await act(async () => {
    root = createRoot(container);
    root.render(
      <IntlProvider locale="en" messages={enMessages}>
        <StatusFooter />
      </IntlProvider>,
    );
  });
  // let the health probe and the stats read settle
  await act(async () => {
    await Promise.resolve();
  });
  return root;
}

/** The span carrying the DB reading, found by its rendered text. */
function dbSpan(container: HTMLElement): HTMLElement {
  const match = Array.from(container.querySelectorAll("span")).find((el) =>
    /^DB\s/.test(el.textContent ?? ""),
  );
  if (!match) throw new Error("no DB reading rendered");
  return match as HTMLElement;
}

describe("StatusFooter DB reading", () => {
  let container: HTMLElement;
  let root: Root | null = null;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve({ ok: true, status: 200 } as Response)),
    );
  });

  afterEach(async () => {
    if (root) await act(async () => root!.unmount());
    root = null;
    container.remove();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("marks the reading when the backend says the store is over its threshold", async () => {
    getStats.mockResolvedValue(
      statsWith({
        size_bytes: 8.47 * GB,
        size_alert: true,
        size_threshold_bytes: 500 * MB,
      }),
    );
    root = await mountFooter(container);

    const span = dbSpan(container);
    expect(span.className).toContain("text-status-warning");
    // Numbers only, so the reason survives in every locale.
    expect(span.getAttribute("title")).toBe("8.5 GB / 500.0 MB");
  });

  it("leaves the reading unmarked when the store is under its threshold", async () => {
    getStats.mockResolvedValue(
      statsWith({
        size_bytes: 120 * MB,
        size_alert: false,
        size_threshold_bytes: 500 * MB,
      }),
    );
    root = await mountFooter(container);

    const span = dbSpan(container);
    expect(span.textContent).toContain("120.0 MB");
    expect(span.className).not.toContain("text-status-warning");
    expect(span.getAttribute("title")).toBeNull();
  });

  it("leaves the reading unmarked when the backend sends no verdict at all", async () => {
    // An older daemon, or any response without the field. The footer must not
    // invent a verdict by re-deriving the threshold on its own.
    getStats.mockResolvedValue(statsWith({ size_bytes: 8.47 * GB }));
    root = await mountFooter(container);

    const span = dbSpan(container);
    expect(span.className).not.toContain("text-status-warning");
    expect(span.getAttribute("title")).toBeNull();
  });
});
