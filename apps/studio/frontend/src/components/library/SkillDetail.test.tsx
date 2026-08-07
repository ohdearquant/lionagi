/**
 * SkillDetail — history-unavailable fallback payload.
 *
 * The backend deliberately answers `versions: null, version: null,
 * history_available: false` when the version-history store can't be read
 * (see lionagi/studio/services/definitions.py's `_get_skill_definition`).
 * That's the right backend behavior, but a frontend that assumes `versions`
 * is always an array crashes exactly during the outage an operator most
 * needs to read the pane. This asserts the pane renders the disk-backed
 * skill content instead of throwing.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { DefinitionDetail, SkillDetail as SkillSummaryDetail } from "@/lib/api";

const api = vi.hoisted(() => ({
  getSkill: vi.fn(),
  getDefinition: vi.fn(),
  getDefinitionVersion: vi.fn(),
  getPluginSkill: vi.fn(),
  rollbackDefinition: vi.fn(),
  saveDefinition: vi.fn(),
  validateSkill: vi.fn(),
  listInvocations: vi.fn(),
}));

vi.mock("@/lib/api", () => api);

const { SkillDetail } = await import("./SkillDetail");

function skillSummary(overrides: Partial<SkillSummaryDetail> = {}): SkillSummaryDetail {
  return {
    name: "my-skill",
    description: "Does a thing.",
    path: "skills/my-skill/SKILL.md",
    content: "Body content that must still render during a history outage.",
    allowed_tools: [],
    ...overrides,
  };
}

function historyUnavailableDef(overrides: Partial<DefinitionDetail> = {}): DefinitionDetail {
  return {
    kind: "skill",
    name: "my-skill",
    path: "skills/my-skill/SKILL.md",
    content: "Body content that must still render during a history outage.",
    version: null,
    versions: null,
    history_available: false,
    ...overrides,
  };
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("SkillDetail — history-unavailable fallback", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    Object.values(api).forEach((fn) => fn.mockReset());
    api.listInvocations.mockResolvedValue({ invocations: [], total: 0, completed_total: 0 });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it("renders the disk-backed content without throwing when versions/version are null", async () => {
    api.getSkill.mockResolvedValue(skillSummary());
    api.getDefinition.mockResolvedValue(historyUnavailableDef());

    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <SkillDetail name="my-skill" />
        </IntlProvider>,
      );
    });
    await flush();

    expect(container.textContent).toContain(
      "Body content that must still render during a history outage.",
    );
    // No version badge and no version-history strip when history is
    // unavailable -- but the pane itself must not have crashed rendering.
    expect(container.textContent).not.toContain("vnull");
    expect(api.getDefinition).toHaveBeenCalledTimes(1);
  });

  it("still shows the version-history strip and badge when history is available", async () => {
    api.getSkill.mockResolvedValue(skillSummary());
    api.getDefinition.mockResolvedValue(
      historyUnavailableDef({
        version: 2,
        history_available: true,
        versions: [
          { id: "v2", version: 2, created_at: 200, message: "second" },
          { id: "v1", version: 1, created_at: 100, message: "first" },
        ],
      }),
    );

    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <SkillDetail name="my-skill" />
        </IntlProvider>,
      );
    });
    await flush();

    expect(container.textContent).toContain("v2");
    const versionButtons = [...container.querySelectorAll("button")].filter((b) =>
      /^v\d/.test(b.textContent ?? ""),
    );
    expect(versionButtons.length).toBe(2);
  });
});
