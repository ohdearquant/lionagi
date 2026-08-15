import type { OperatorEffectRejectionCode } from "@/lib/api";
import type { OperatorUiEffect } from "@/lib/types";

type SearchValue = string | number | boolean | string[];

export type OperatorEffectPlan =
  | {
      kind: "navigate";
      to: "/" | "/fleet" | "/library" | "/schedules" | "/system";
      search: Record<string, SearchValue>;
    }
  | { kind: "theme"; theme: "dark" | "light" }
  | { kind: "reject"; rejectionCode: OperatorEffectRejectionCode };

export type StoredEffectAcknowledgement =
  | { status: "applied"; clientRoute: string }
  | {
      status: "rejected";
      clientRoute?: string;
      rejectionCode: OperatorEffectRejectionCode;
    };

const FLEET_KEYS = new Set([
  "s",
  "status",
  "kind",
  "playbook",
  "project",
  "page",
  "skill",
  "sessions",
  "invocation",
]);
const LIBRARY_TABS = new Set(["all", "agent", "workflow", "playbook", "skill", "plugin", "engine"]);

function record(value: unknown): Record<string, unknown> | null {
  return value != null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function searchValue(value: unknown): value is SearchValue {
  return (
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean" ||
    (Array.isArray(value) && value.every((item) => typeof item === "string"))
  );
}

function pickSearch(value: unknown, allowed: Set<string>): Record<string, SearchValue> | null {
  const source = record(value);
  if (!source) return null;
  const result: Record<string, SearchValue> = {};
  for (const [key, item] of Object.entries(source)) {
    if (!allowed.has(key) || !searchValue(item)) return null;
    result[key] = item;
  }
  return result;
}

function stringSelection(value: unknown): Record<string, string> | null {
  const source = record(value);
  if (!source) return null;
  const result: Record<string, string> = {};
  for (const [key, item] of Object.entries(source)) {
    if (typeof item !== "string" || !item) return null;
    result[key] = item;
  }
  return Object.keys(result).length ? result : null;
}

export function planOperatorEffect(effect: unknown): OperatorEffectPlan {
  const raw = record(effect);
  if (!raw || typeof raw.kind !== "string") {
    return { kind: "reject", rejectionCode: "unsupported" };
  }

  if (raw.kind === "theme") {
    return raw.theme === "dark" || raw.theme === "light"
      ? { kind: "theme", theme: raw.theme }
      : { kind: "reject", rejectionCode: "invalid_params" };
  }

  if (raw.kind === "navigate") {
    const params = record(raw.params);
    if (!params || typeof raw.space !== "string") {
      return { kind: "reject", rejectionCode: "invalid_params" };
    }
    if (raw.space === "mission" || raw.space === "history") {
      const view = params.view;
      const fleet =
        raw.space === "history" || view === "fleet" || [...FLEET_KEYS].some((k) => k in params);
      if (!fleet) {
        return Object.keys(params).length === 0
          ? { kind: "navigate", to: "/", search: {} }
          : { kind: "reject", rejectionCode: "invalid_params" };
      }
      const fleetParams = { ...params };
      delete fleetParams.view;
      const search = pickSearch(fleetParams, FLEET_KEYS);
      return search
        ? { kind: "navigate", to: "/fleet", search }
        : { kind: "reject", rejectionCode: "invalid_params" };
    }
    if (raw.space === "library") {
      const search = pickSearch(params, new Set(["tab", "sel"]));
      if (!search || ("tab" in search && !LIBRARY_TABS.has(String(search.tab)))) {
        return { kind: "reject", rejectionCode: "invalid_params" };
      }
      return { kind: "navigate", to: "/library", search };
    }
    if (raw.space === "schedules") {
      const search = pickSearch(params, new Set(["create", "name", "cron", "prompt", "desc", "s"]));
      return search
        ? { kind: "navigate", to: "/schedules", search }
        : { kind: "reject", rejectionCode: "invalid_params" };
    }
    if (raw.space === "system") {
      const search = pickSearch(params, new Set(["tab"]));
      const tab = search?.tab;
      if (
        !search ||
        (tab != null && tab !== "health" && tab !== "maintenance" && tab !== "settings")
      ) {
        return { kind: "reject", rejectionCode: "invalid_params" };
      }
      return { kind: "navigate", to: "/system", search };
    }
    if (raw.space === "designer") {
      const search = pickSearch(params, new Set(["sel"]));
      if (!search) return { kind: "reject", rejectionCode: "invalid_params" };
      return {
        kind: "navigate",
        to: "/library",
        search: { tab: "workflow", ...search },
      };
    }
    return { kind: "reject", rejectionCode: "unsupported" };
  }

  if (raw.kind === "select") {
    const selection = stringSelection(raw.selection);
    if (!selection || typeof raw.space !== "string") {
      return { kind: "reject", rejectionCode: "invalid_params" };
    }
    if (raw.space === "mission" || raw.space === "history") {
      const id = selection.s ?? selection.runId ?? selection.run_id ?? selection.sessionId;
      return id
        ? { kind: "navigate", to: "/fleet", search: { s: id } }
        : { kind: "reject", rejectionCode: "invalid_params" };
    }
    if (raw.space === "library") {
      const selected = selection.sel;
      return selected
        ? { kind: "navigate", to: "/library", search: { sel: selected } }
        : { kind: "reject", rejectionCode: "invalid_params" };
    }
    if (raw.space === "schedules") {
      const id = selection.s ?? selection.id;
      return id
        ? { kind: "navigate", to: "/schedules", search: { s: id } }
        : { kind: "reject", rejectionCode: "invalid_params" };
    }
    if (raw.space === "designer") {
      const selected = selection.sel ?? selection.id;
      return selected
        ? {
            kind: "navigate",
            to: "/library",
            search: {
              tab: "workflow",
              sel: selected.startsWith("workflow:") ? selected : `workflow:${selected}`,
            },
          }
        : { kind: "reject", rejectionCode: "invalid_params" };
    }
    return { kind: "reject", rejectionCode: "unsupported" };
  }

  if (raw.kind === "prefill") {
    if (raw.form !== "schedule") {
      return { kind: "reject", rejectionCode: "not_visible" };
    }
    const values = record(raw.values);
    if (!values) return { kind: "reject", rejectionCode: "invalid_params" };
    const aliases: Record<string, string> = {
      name: "name",
      cron: "cron",
      cron_expr: "cron",
      prompt: "prompt",
      action_prompt: "prompt",
      desc: "desc",
      description: "desc",
    };
    const search: Record<string, SearchValue> = { create: "1" };
    for (const [key, value] of Object.entries(values)) {
      const target = aliases[key];
      if (!target || typeof value !== "string") {
        return { kind: "reject", rejectionCode: "invalid_params" };
      }
      search[target] = value;
    }
    return { kind: "navigate", to: "/schedules", search };
  }

  return { kind: "reject", rejectionCode: "unsupported" };
}

export function effectPlanRoute(plan: Extract<OperatorEffectPlan, { kind: "navigate" }>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(plan.search)) {
    if (Array.isArray(value)) {
      for (const item of value) query.append(key, item);
    } else {
      query.set(key, String(value));
    }
  }
  const suffix = query.toString();
  return `${plan.to}${suffix ? `?${suffix}` : ""}`;
}

function storageKey(conversationId: string): string {
  return `studio:operator-effects:${conversationId}`;
}

export function readEffectAcknowledgements(
  conversationId: string,
): Map<string, StoredEffectAcknowledgement> {
  if (typeof window === "undefined") return new Map();
  try {
    const raw = JSON.parse(window.localStorage.getItem(storageKey(conversationId)) ?? "[]");
    if (!Array.isArray(raw)) return new Map();
    const entries = raw.filter(
      (item): item is [string, StoredEffectAcknowledgement] =>
        Array.isArray(item) &&
        typeof item[0] === "string" &&
        item[1] != null &&
        typeof item[1] === "object",
    );
    return new Map(entries);
  } catch {
    return new Map();
  }
}

export function rememberEffectAcknowledgement(
  conversationId: string,
  effectId: string,
  acknowledgement: StoredEffectAcknowledgement,
): boolean {
  try {
    const acknowledgements = readEffectAcknowledgements(conversationId);
    acknowledgements.delete(effectId);
    acknowledgements.set(effectId, acknowledgement);
    const entries = [...acknowledgements.entries()].slice(-256);
    window.localStorage.setItem(storageKey(conversationId), JSON.stringify(entries));
    return true;
  } catch {
    return false;
  }
}

export function effectAcknowledgementStorageAvailable(conversationId: string): boolean {
  if (typeof window === "undefined") return false;
  const probeKey = `${storageKey(conversationId)}:probe`;
  try {
    window.localStorage.setItem(probeKey, "1");
    window.localStorage.removeItem(probeKey);
    return true;
  } catch {
    return false;
  }
}

export function operatorEffectId(effect: OperatorUiEffect | unknown): string | null {
  const raw = record(effect);
  return typeof raw?.id === "string" && raw.id ? raw.id : null;
}
