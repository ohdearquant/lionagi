import { beforeEach, describe, expect, it } from "vitest";
import {
  effectPlanRoute,
  effectAcknowledgementStorageAvailable,
  planOperatorEffect,
  readEffectAcknowledgements,
  rememberEffectAcknowledgement,
} from "./operatorEffects";

describe("Operator client effects", () => {
  beforeEach(() => {
    const values = new Map<string, string>();
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: (key: string) => values.get(key) ?? null,
        setItem: (key: string, value: string) => values.set(key, value),
        removeItem: (key: string) => values.delete(key),
        clear: () => values.clear(),
      },
    });
  });

  it("validates and canonicalizes navigation instead of accepting arbitrary routes", () => {
    const plan = planOperatorEffect({
      id: "effect-1",
      kind: "navigate",
      space: "mission",
      params: { view: "fleet", status: ["failed"], s: "run-1" },
    });
    expect(plan).toEqual({
      kind: "navigate",
      to: "/fleet",
      search: { status: ["failed"], s: "run-1" },
    });
    if (plan.kind === "navigate") {
      expect(effectPlanRoute(plan)).toBe("/fleet?status=failed&s=run-1");
    }

    expect(
      planOperatorEffect({
        id: "effect-2",
        kind: "navigate",
        space: "library",
        params: { tab: "root-shell" },
      }),
    ).toEqual({ kind: "reject", rejectionCode: "invalid_params" });
  });

  it("maps a schedule prefill to the reviewed create form and rejects hidden forms", () => {
    expect(
      planOperatorEffect({
        id: "effect-1",
        kind: "prefill",
        form: "schedule",
        values: { name: "Daily brief", cron_expr: "0 9 * * *", action_prompt: "Summarize" },
      }),
    ).toEqual({
      kind: "navigate",
      to: "/schedules",
      search: {
        create: "1",
        name: "Daily brief",
        cron: "0 9 * * *",
        prompt: "Summarize",
      },
    });
    expect(
      planOperatorEffect({
        id: "effect-2",
        kind: "prefill",
        form: "workflow",
        values: {},
      }),
    ).toEqual({ kind: "reject", rejectionCode: "not_visible" });
  });

  it("persists a bounded acknowledgement history to avoid replaying effects", () => {
    rememberEffectAcknowledgement("conversation-1", "effect-1", {
      status: "applied",
      clientRoute: "/fleet?s=run-1",
    });
    expect(readEffectAcknowledgements("conversation-1").get("effect-1")).toEqual({
      status: "applied",
      clientRoute: "/fleet?s=run-1",
    });
  });

  it("reports blocked acknowledgement storage without throwing", () => {
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: () => null,
        setItem: () => {
          throw new Error("blocked");
        },
        removeItem: () => undefined,
      },
    });
    expect(effectAcknowledgementStorageAvailable("conversation-1")).toBe(false);
    expect(
      rememberEffectAcknowledgement("conversation-1", "effect-1", {
        status: "applied",
        clientRoute: "/fleet",
      }),
    ).toBe(false);
  });
});
