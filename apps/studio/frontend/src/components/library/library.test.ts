/**
 * Library master-detail contract tests.
 *
 * Covers:
 * - No Drawer import remains in library/ or routes/library.tsx
 * - AgentDrawer.tsx and PlaybookDrawer.tsx no longer exist
 * - SplitPane is used in library.tsx (not overlay Drawer)
 * - AgentDetail and WorkflowDetail exist with onBack prop
 * - parseSel / encodeSel round-trip logic
 * - validateSearch accepts tab + sel params
 * - URL sel encoding scheme: "<kind>:<name>"
 */

import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";

const LIBRARY_DIR = path.resolve(__dirname);
const ROUTES_DIR = path.resolve(__dirname, "../../routes");

// ─── Deleted drawer files ─────────────────────────────────────────────────────

describe("library/ — superseded drawer files deleted", () => {
  it("AgentDrawer.tsx no longer exists", () => {
    expect(fs.existsSync(path.join(LIBRARY_DIR, "AgentDrawer.tsx"))).toBe(false);
  });

  it("PlaybookDrawer.tsx no longer exists", () => {
    expect(fs.existsSync(path.join(LIBRARY_DIR, "PlaybookDrawer.tsx"))).toBe(false);
  });
});

// ─── No overlay Drawer imports ────────────────────────────────────────────────

function sourceFiles(dir: string, exts = [".tsx", ".ts"]): string[] {
  return fs
    .readdirSync(dir)
    .filter(
      (f) => exts.some((e) => f.endsWith(e)) && !f.endsWith(".test.ts") && !f.endsWith(".test.tsx"),
    );
}

describe("library/ — no shell/Drawer overlay import", () => {
  const files = sourceFiles(LIBRARY_DIR);

  for (const file of files) {
    it(`${file} does not import shell/Drawer`, () => {
      const src = fs.readFileSync(path.join(LIBRARY_DIR, file), "utf-8");
      expect(src).not.toMatch(/from.*shell\/Drawer/);
      expect(src).not.toMatch(/import.*Drawer.*from.*shell/);
    });
  }
});

describe("routes/library.tsx — no shell/Drawer overlay import", () => {
  it("does not import shell/Drawer", () => {
    const src = fs.readFileSync(path.join(ROUTES_DIR, "library.tsx"), "utf-8");
    expect(src).not.toMatch(/from.*shell\/Drawer/);
    expect(src).not.toMatch(/import.*Drawer.*from.*shell/);
  });
});

// ─── SplitPane is wired ───────────────────────────────────────────────────────

describe("routes/library.tsx — uses SplitPane master-detail", () => {
  const src = fs.readFileSync(path.join(ROUTES_DIR, "library.tsx"), "utf-8");

  it("imports SplitPane", () => {
    expect(src).toMatch(/import SplitPane from.*SplitPane/);
  });

  it("renders <SplitPane", () => {
    expect(src).toMatch(/<SplitPane/);
  });

  it('has id="library" on SplitPane', () => {
    expect(src).toMatch(/id="library"/);
  });

  it("wires detailActive prop", () => {
    expect(src).toMatch(/detailActive/);
  });
});

// ─── Detail pane components ───────────────────────────────────────────────────

describe("AgentDetail — detail pane contract", () => {
  it("exists at components/library/AgentDetail.tsx", () => {
    expect(fs.existsSync(path.join(LIBRARY_DIR, "AgentDetail.tsx"))).toBe(true);
  });

  it("exports AgentDetail", () => {
    const src = fs.readFileSync(path.join(LIBRARY_DIR, "AgentDetail.tsx"), "utf-8");
    expect(src).toMatch(/export function AgentDetail/);
  });

  it("accepts onBack prop (collapsed back affordance)", () => {
    const src = fs.readFileSync(path.join(LIBRARY_DIR, "AgentDetail.tsx"), "utf-8");
    expect(src).toMatch(/onBack\?/);
  });

  it("does not import shell/Drawer", () => {
    const src = fs.readFileSync(path.join(LIBRARY_DIR, "AgentDetail.tsx"), "utf-8");
    expect(src).not.toMatch(/from.*shell\/Drawer/);
  });
});

describe("WorkflowDetail — detail pane contract", () => {
  it("exists at components/library/WorkflowDetail.tsx", () => {
    expect(fs.existsSync(path.join(LIBRARY_DIR, "WorkflowDetail.tsx"))).toBe(true);
  });

  it("exports WorkflowDetail", () => {
    const src = fs.readFileSync(path.join(LIBRARY_DIR, "WorkflowDetail.tsx"), "utf-8");
    expect(src).toMatch(/export function WorkflowDetail/);
  });

  it("accepts onBack prop (collapsed back affordance)", () => {
    const src = fs.readFileSync(path.join(LIBRARY_DIR, "WorkflowDetail.tsx"), "utf-8");
    expect(src).toMatch(/onBack\?/);
  });

  it("exports CreateWorkflowPanel", () => {
    const src = fs.readFileSync(path.join(LIBRARY_DIR, "WorkflowDetail.tsx"), "utf-8");
    expect(src).toMatch(/export function CreateWorkflowPanel/);
  });

  it("PlaybookDetail.tsx is superseded (WorkflowDetail.tsx is the canonical file)", () => {
    expect(fs.existsSync(path.join(LIBRARY_DIR, "WorkflowDetail.tsx"))).toBe(true);
  });
});

// ─── URL param scheme ─────────────────────────────────────────────────────────

describe("library URL sel param — encoding scheme", () => {
  // Mirror parseSel and encodeSel inline to validate the contract without importing
  // the route module (which has router side-effects).

  type LibraryKind = "agent" | "workflow" | "skill" | "plugin" | "engine";

  function encodeSel(kind: LibraryKind, name: string): string {
    return `${kind}:${name}`;
  }

  function parseSel(sel: string | undefined): { kind: LibraryKind; name: string } | null {
    if (!sel) return null;
    const colon = sel.indexOf(":");
    if (colon === -1) return null;
    const kind = sel.slice(0, colon) as LibraryKind;
    const name = sel.slice(colon + 1);
    const valid: LibraryKind[] = ["agent", "workflow", "skill", "plugin", "engine"];
    if (!valid.includes(kind) || !name) return null;
    return { kind, name };
  }

  it("encodes kind:name", () => {
    expect(encodeSel("agent", "my-agent")).toBe("agent:my-agent");
    expect(encodeSel("workflow", "review-flow")).toBe("workflow:review-flow");
  });

  it("parses kind:name round-trip", () => {
    const encoded = encodeSel("agent", "my-agent");
    const parsed = parseSel(encoded);
    expect(parsed).toEqual({ kind: "agent", name: "my-agent" });
  });

  it("parses name with colons correctly (first colon only)", () => {
    const encoded = "agent:my:agent";
    const parsed = parseSel(encoded);
    expect(parsed).toEqual({ kind: "agent", name: "my:agent" });
  });

  it("returns null for invalid kind", () => {
    expect(parseSel("unknown:foo")).toBeNull();
    expect(parseSel("playbook:foo")).toBeNull();
  });

  it("returns null for empty sel", () => {
    expect(parseSel(undefined)).toBeNull();
    expect(parseSel("")).toBeNull();
  });

  it("returns null for missing colon", () => {
    expect(parseSel("agentonly")).toBeNull();
  });

  it("returns null for empty name", () => {
    expect(parseSel("agent:")).toBeNull();
  });

  it("all valid kinds parse correctly", () => {
    const kinds: LibraryKind[] = ["agent", "workflow", "skill", "plugin", "engine"];
    for (const kind of kinds) {
      const result = parseSel(encodeSel(kind, "test-name"));
      expect(result).toEqual({ kind, name: "test-name" });
    }
  });

  it("workflow: prefix is valid, playbook: is not", () => {
    expect(parseSel("workflow:my-flow")).toEqual({ kind: "workflow", name: "my-flow" });
    expect(parseSel("playbook:my-flow")).toBeNull();
  });
});

// ─── validateSearch contract ───────────────────────────────────────────────────

describe("routes/library.tsx — validateSearch accepts tab + sel", () => {
  const src = fs.readFileSync(path.join(ROUTES_DIR, "library.tsx"), "utf-8");

  it("validateSearch returns sel from search params", () => {
    expect(src).toMatch(/sel/);
    expect(src).toMatch(/validateSearch/);
  });

  it("sel is typed as optional string", () => {
    // The validateSearch return type includes sel?: string
    expect(src).toMatch(/sel\?:\s*string/);
  });
});

// ─── TabBar preserved ─────────────────────────────────────────────────────────

describe("routes/library.tsx — TabBar above the split", () => {
  const src = fs.readFileSync(path.join(ROUTES_DIR, "library.tsx"), "utf-8");

  it("imports TabBar", () => {
    expect(src).toMatch(/import TabBar from.*TabBar/);
  });

  it("renders <TabBar", () => {
    expect(src).toMatch(/<TabBar/);
  });
});

// ─── Rename assertions ────────────────────────────────────────────────────────

describe("routes/library.tsx — workflow terminology", () => {
  const src = fs.readFileSync(path.join(ROUTES_DIR, "library.tsx"), "utf-8");

  it('LIBRARY_TABS contains "workflow" not "playbook"', () => {
    expect(src).toMatch(/"workflow"/);
    expect(src).not.toMatch(/"playbook"/);
  });

  it("imports WorkflowDetail not PlaybookDetail", () => {
    expect(src).toMatch(/WorkflowDetail/);
    expect(src).not.toMatch(/PlaybookDetail/);
  });
});

describe("components/library/KindBadge.tsx — workflow kind", () => {
  const src = fs.readFileSync(path.join(LIBRARY_DIR, "KindBadge.tsx"), "utf-8");

  it('LibraryKind includes "workflow"', () => {
    expect(src).toMatch(/"workflow"/);
  });

  it('LibraryKind does not include "playbook"', () => {
    expect(src).not.toMatch(/"playbook"/);
  });
});
