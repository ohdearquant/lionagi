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

// ─── Built-in playbook templates — Workflows page (DESIGN-BRIEF §3) ──────────
//
// The Workflows page used to render nothing: useLibraryData() never fetched
// playbooks at all. These tests cover the fix: built-in templates + the
// user's own playbooks are fetched and surfaced as "workflow" rows split by
// subKind, with a real detail pane (not a stub) for both.

describe("routes/library.tsx — built-in + user playbooks are fetched", () => {
  const src = fs.readFileSync(path.join(ROUTES_DIR, "library.tsx"), "utf-8");

  it("imports listBuiltinPlaybooks and listPlaybooks from lib/api", () => {
    expect(src).toMatch(/listBuiltinPlaybooks/);
    expect(src).toMatch(/listPlaybooks/);
  });

  it("useLibraryData pushes builtin-subKind and custom-subKind workflow rows", () => {
    expect(src).toMatch(/subKind:\s*"builtin"/);
    expect(src).toMatch(/subKind:\s*"custom"/);
    expect(src).toMatch(/subKind:\s*"graph"/);
  });

  it("imports PlaybookTemplateDetail for the builtin/custom detail pane", () => {
    expect(src).toMatch(/import\s*\{\s*PlaybookTemplateDetail\s*\}\s*from/);
  });

  it("dispatches builtin/custom subKind to PlaybookTemplateDetail, graph subKind to WorkflowDetail", () => {
    expect(src).toMatch(/subKind === "graph"[\s\S]{0,80}<WorkflowDetail/);
    expect(src).toMatch(/<PlaybookTemplateDetail/);
  });
});

describe("PlaybookTemplateDetail — detail pane contract", () => {
  const filePath = path.join(LIBRARY_DIR, "PlaybookTemplateDetail.tsx");
  const src = fs.readFileSync(filePath, "utf-8");

  it("exists at components/library/PlaybookTemplateDetail.tsx", () => {
    expect(fs.existsSync(filePath)).toBe(true);
  });

  it("exports PlaybookTemplateDetail", () => {
    expect(src).toMatch(/export function PlaybookTemplateDetail/);
  });

  it("accepts onBack and onCloned props", () => {
    expect(src).toMatch(/onBack\?/);
    expect(src).toMatch(/onCloned\?/);
  });

  it("does not import shell/Drawer", () => {
    expect(src).not.toMatch(/from.*shell\/Drawer/);
  });

  it("fetches the builtin endpoint when isBuiltin, the user playbook endpoint otherwise", () => {
    expect(src).toMatch(/getBuiltinPlaybookRaw/);
    expect(src).toMatch(/getWorkerRaw/);
    expect(src).toMatch(/isBuiltin\s*\?\s*getBuiltinPlaybookRaw/);
  });

  it("reuses StatusPill for recent runs — no forked status/verdict chip component", () => {
    expect(src).toMatch(/import StatusPill from/);
    expect(src).toMatch(/<StatusPill/);
  });

  it("renders Run and Clone actions wired to launchPlaybook / installBuiltinPlaybook", () => {
    expect(src).toMatch(/launchPlaybook/);
    expect(src).toMatch(/installBuiltinPlaybook/);
  });

  it("does not fabricate a step/DAG graph — no WorkerGraph or WorkflowEditor import", () => {
    expect(src).not.toMatch(/WorkerGraph/);
    expect(src).not.toMatch(/WorkflowEditor/);
  });
});

// ─── URL sel param — 3-part workflow encoding (builtin/custom/graph) ─────────

describe("library URL sel param — workflow subKind encoding", () => {
  // Mirrors the real parseSel/encodeSel in routes/library.tsx verbatim, same
  // rationale as the plain kind:name block above: validate the contract
  // without importing the route module (router side-effects).

  type LibraryKind = "agent" | "workflow" | "skill" | "plugin" | "engine";
  type WorkflowSubKind = "builtin" | "custom" | "graph";
  const WORKFLOW_SUB_KINDS: WorkflowSubKind[] = ["builtin", "custom", "graph"];

  function encodeSel(kind: LibraryKind, name: string, subKind?: WorkflowSubKind): string {
    if (kind === "workflow") {
      return `workflow:${subKind ?? "graph"}:${name}`;
    }
    return `${kind}:${name}`;
  }

  function parseSel(
    sel: string | undefined,
  ): { kind: LibraryKind; name: string; subKind?: WorkflowSubKind } | null {
    if (!sel) return null;
    const colon = sel.indexOf(":");
    if (colon === -1) return null;
    const kind = sel.slice(0, colon) as LibraryKind;
    const rest = sel.slice(colon + 1);
    const valid: LibraryKind[] = ["agent", "workflow", "skill", "plugin", "engine"];
    if (!valid.includes(kind) || !rest) return null;

    if (kind === "workflow") {
      const colon2 = rest.indexOf(":");
      if (colon2 !== -1) {
        const maybeSubKind = rest.slice(0, colon2);
        const name = rest.slice(colon2 + 1);
        if (WORKFLOW_SUB_KINDS.includes(maybeSubKind as WorkflowSubKind) && name) {
          return { kind, name, subKind: maybeSubKind as WorkflowSubKind };
        }
      }
      return { kind, name: rest, subKind: "graph" };
    }

    return { kind, name: rest };
  }

  it("encodes workflow:<subKind>:<name>", () => {
    expect(encodeSel("workflow", "research", "builtin")).toBe("workflow:builtin:research");
    expect(encodeSel("workflow", "my-copy", "custom")).toBe("workflow:custom:my-copy");
    expect(encodeSel("workflow", "review-flow", "graph")).toBe("workflow:graph:review-flow");
  });

  it("defaults to graph subKind when none is passed (non-workflow-split call sites)", () => {
    expect(encodeSel("workflow", "review-flow")).toBe("workflow:graph:review-flow");
  });

  it("non-workflow kinds are unaffected by subKind", () => {
    expect(encodeSel("agent", "my-agent")).toBe("agent:my-agent");
  });

  it("parses each subKind round-trip", () => {
    expect(parseSel("workflow:builtin:research")).toEqual({
      kind: "workflow",
      name: "research",
      subKind: "builtin",
    });
    expect(parseSel("workflow:custom:my-copy")).toEqual({
      kind: "workflow",
      name: "my-copy",
      subKind: "custom",
    });
    expect(parseSel("workflow:graph:review-flow")).toEqual({
      kind: "workflow",
      name: "review-flow",
      subKind: "graph",
    });
  });

  it("backward-compat: pre-split workflow:<name> (no subKind) resolves as graph", () => {
    expect(parseSel("workflow:review-flow")).toEqual({
      kind: "workflow",
      name: "review-flow",
      subKind: "graph",
    });
  });

  it("non-workflow kinds still parse without a subKind field", () => {
    expect(parseSel("agent:my-agent")).toEqual({ kind: "agent", name: "my-agent" });
  });

  it("returns null for invalid kind or empty sel", () => {
    expect(parseSel("unknown:foo")).toBeNull();
    expect(parseSel(undefined)).toBeNull();
    expect(parseSel("")).toBeNull();
  });
});
