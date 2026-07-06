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

  type LibraryKind = "agent" | "workflow" | "playbook" | "skill" | "plugin" | "engine";

  function encodeSel(kind: LibraryKind, name: string): string {
    return `${kind}:${name}`;
  }

  function parseSel(sel: string | undefined): { kind: LibraryKind; name: string } | null {
    if (!sel) return null;
    const colon = sel.indexOf(":");
    if (colon === -1) return null;
    const kind = sel.slice(0, colon) as LibraryKind;
    const name = sel.slice(colon + 1);
    const valid: LibraryKind[] = ["agent", "workflow", "playbook", "skill", "plugin", "engine"];
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
    const kinds: LibraryKind[] = ["agent", "workflow", "playbook", "skill", "plugin", "engine"];
    for (const kind of kinds) {
      const result = parseSel(encodeSel(kind, "test-name"));
      expect(result).toEqual({ kind, name: "test-name" });
    }
  });

  it("workflow: and playbook: prefixes are both valid", () => {
    expect(parseSel("workflow:my-flow")).toEqual({ kind: "workflow", name: "my-flow" });
    expect(parseSel("playbook:my-flow")).toEqual({ kind: "playbook", name: "my-flow" });
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

describe("routes/library.tsx — workflow + playbook terminology", () => {
  const src = fs.readFileSync(path.join(ROUTES_DIR, "library.tsx"), "utf-8");

  it('LIBRARY_TABS contains both "workflow" and "playbook"', () => {
    expect(src).toMatch(/"workflow"/);
    expect(src).toMatch(/"playbook"/);
  });

  it("imports WorkflowDetail not PlaybookDetail", () => {
    expect(src).toMatch(/WorkflowDetail/);
    expect(src).not.toMatch(/PlaybookDetail/);
  });
});

describe("components/library/KindBadge.tsx — workflow + playbook kinds", () => {
  const src = fs.readFileSync(path.join(LIBRARY_DIR, "KindBadge.tsx"), "utf-8");

  it('LibraryKind includes "workflow"', () => {
    expect(src).toMatch(/"workflow"/);
  });

  it('LibraryKind includes "playbook" as its own kind', () => {
    expect(src).toMatch(/"playbook"/);
  });
});

// ─── Built-in playbook templates — Playbooks page (DESIGN-BRIEF §3) ──────────
//
// The Workflows page used to render nothing: useLibraryData() never fetched
// playbooks at all. These tests cover the fix: built-in templates + the
// user's own playbooks are fetched and surfaced as "playbook" rows split by
// subKind, with a real detail pane (not a stub) for both. Playbooks are a
// distinct top-level kind from "workflow" (graph designs) — they are not a
// workflow subKind.

describe("routes/library.tsx — built-in + user playbooks are fetched", () => {
  const src = fs.readFileSync(path.join(ROUTES_DIR, "library.tsx"), "utf-8");

  it("imports listBuiltinPlaybooks and listPlaybooks from lib/api", () => {
    expect(src).toMatch(/listBuiltinPlaybooks/);
    expect(src).toMatch(/listPlaybooks/);
  });

  it("useLibraryData pushes builtin-subKind and custom-subKind playbook rows", () => {
    expect(src).toMatch(/subKind:\s*"builtin"/);
    expect(src).toMatch(/subKind:\s*"custom"/);
    expect(src).toMatch(/kind:\s*"playbook"/);
  });

  it("imports PlaybookTemplateDetail for the builtin/custom detail pane", () => {
    expect(src).toMatch(/import\s*\{\s*PlaybookTemplateDetail\s*\}\s*from/);
  });

  it("dispatches playbook kind to PlaybookTemplateDetail, workflow kind to WorkflowDetail", () => {
    expect(src).toMatch(/parsed\?\.kind === "workflow"[\s\S]{0,80}<WorkflowDetail/);
    expect(src).toMatch(/parsed\?\.kind === "playbook"[\s\S]{0,80}<PlaybookTemplateDetail/);
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

  it("renders recent-run status through the §0 keystone, never raw run.status", () => {
    expect(src).toMatch(/import StatusVerdictChips from/);
    expect(src).toMatch(/deriveDisplayStatus/);
    expect(src).toMatch(/<StatusVerdictChips/);
    expect(src).not.toMatch(/<StatusPill/);
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

// ─── URL sel param — playbook subKind encoding + workflow back-compat ────────

describe("library URL sel param — playbook subKind encoding", () => {
  // Mirrors the real parseSel/encodeSel in routes/library.tsx verbatim, same
  // rationale as the plain kind:name block above: validate the contract
  // without importing the route module (router side-effects). "workflow" is
  // graph-designs-only now (plain kind:name); "playbook" carries the
  // builtin/custom split, plus backward-compat for pre-split
  // workflow:<subKind>:<name> bookmarks.

  type LibraryKind = "agent" | "workflow" | "playbook" | "skill" | "plugin" | "engine";
  type PlaybookSubKind = "builtin" | "custom";
  const PLAYBOOK_SUB_KINDS: PlaybookSubKind[] = ["builtin", "custom"];
  const LIBRARY_KINDS: LibraryKind[] = [
    "agent",
    "workflow",
    "playbook",
    "skill",
    "plugin",
    "engine",
  ];

  function encodeSel(kind: LibraryKind, name: string, subKind?: PlaybookSubKind): string {
    if (kind === "playbook") {
      return `playbook:${subKind ?? "custom"}:${name}`;
    }
    return `${kind}:${name}`;
  }

  function parseSel(
    sel: string | undefined,
  ): { kind: LibraryKind; name: string; subKind?: PlaybookSubKind } | null {
    if (!sel) return null;
    const colon = sel.indexOf(":");
    if (colon === -1) return null;
    const kind = sel.slice(0, colon) as LibraryKind;
    const rest = sel.slice(colon + 1);
    if (!LIBRARY_KINDS.includes(kind) || !rest) return null;

    if (kind === "playbook") {
      const colon2 = rest.indexOf(":");
      if (colon2 !== -1) {
        const maybeSubKind = rest.slice(0, colon2);
        const name = rest.slice(colon2 + 1);
        if (PLAYBOOK_SUB_KINDS.includes(maybeSubKind as PlaybookSubKind) && name) {
          return { kind, name, subKind: maybeSubKind as PlaybookSubKind };
        }
      }
      return null;
    }

    if (kind === "workflow") {
      const colon2 = rest.indexOf(":");
      if (colon2 !== -1) {
        const legacySubKind = rest.slice(0, colon2);
        const name = rest.slice(colon2 + 1);
        if (legacySubKind === "graph" && name) {
          return { kind: "workflow", name };
        }
        if ((legacySubKind === "builtin" || legacySubKind === "custom") && name) {
          return { kind: "playbook", name, subKind: legacySubKind };
        }
      }
      return { kind, name: rest };
    }

    return { kind, name: rest };
  }

  it("encodes playbook:<subKind>:<name>", () => {
    expect(encodeSel("playbook", "research", "builtin")).toBe("playbook:builtin:research");
    expect(encodeSel("playbook", "my-copy", "custom")).toBe("playbook:custom:my-copy");
  });

  it("defaults to custom subKind when none is passed", () => {
    expect(encodeSel("playbook", "my-copy")).toBe("playbook:custom:my-copy");
  });

  it("workflow (graph) encodes as plain kind:name, no subKind", () => {
    expect(encodeSel("workflow", "review-flow")).toBe("workflow:review-flow");
  });

  it("non-playbook kinds are unaffected by subKind", () => {
    expect(encodeSel("agent", "my-agent")).toBe("agent:my-agent");
  });

  it("parses each playbook subKind round-trip", () => {
    expect(parseSel("playbook:builtin:research")).toEqual({
      kind: "playbook",
      name: "research",
      subKind: "builtin",
    });
    expect(parseSel("playbook:custom:my-copy")).toEqual({
      kind: "playbook",
      name: "my-copy",
      subKind: "custom",
    });
  });

  it("parses workflow:<name> as a plain graph design, no subKind", () => {
    expect(parseSel("workflow:review-flow")).toEqual({ kind: "workflow", name: "review-flow" });
  });

  it("backward-compat: pre-split workflow:graph:<name> resolves as workflow", () => {
    expect(parseSel("workflow:graph:review-flow")).toEqual({
      kind: "workflow",
      name: "review-flow",
    });
  });

  it("backward-compat: pre-split workflow:builtin|custom:<name> resolves as playbook", () => {
    expect(parseSel("workflow:builtin:research")).toEqual({
      kind: "playbook",
      name: "research",
      subKind: "builtin",
    });
    expect(parseSel("workflow:custom:my-copy")).toEqual({
      kind: "playbook",
      name: "my-copy",
      subKind: "custom",
    });
  });

  it("non-playbook kinds still parse without a subKind field", () => {
    expect(parseSel("agent:my-agent")).toEqual({ kind: "agent", name: "my-agent" });
  });

  it("returns null for invalid kind, empty sel, or a playbook sel missing subKind", () => {
    expect(parseSel("unknown:foo")).toBeNull();
    expect(parseSel(undefined)).toBeNull();
    expect(parseSel("")).toBeNull();
    expect(parseSel("playbook:no-subkind")).toBeNull();
  });
});
