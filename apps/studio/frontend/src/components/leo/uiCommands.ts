/**
 * Leo UI commands — declarative view changes Leo streams over SSE, executed
 * client-side. Strictly read-only on server state: navigation and form
 * prefill only. Anything that mutates still rides the proposed-action
 * confirm flow.
 */
import type { LeoUiCommand } from "@/lib/api";
import type { RetiredSearch } from "@/lib/retiredRoutes";

type NavigateFn = (opts: {
  to: string;
  search?: RetiredSearch;
  replace?: boolean;
}) => void | Promise<void>;

const SPACE_PATHS: Record<string, string> = {
  mission: "/",
  fleet: "/fleet",
  designer: "/designer",
  library: "/library",
  schedules: "/schedules",
  system: "/system",
};

/**
 * Non-empty string params carry over as the target route's search — this is
 * what lets a "navigate to schedules" command re-open a specific schedule
 * (?s=<id>) instead of only the handful of keys a space happened to need
 * historically.
 */
export function uiCommandSearch(params: Record<string, string> | undefined): RetiredSearch {
  const out: RetiredSearch = {};
  if (!params) return out;
  for (const [key, value] of Object.entries(params)) {
    if (typeof value === "string" && value.length > 0) out[key] = value;
  }
  return out;
}

/**
 * Execute one command. Returns a display label for the transcript chip when
 * applied, null when the command isn't recognized — unknown kinds render as
 * a quiet "couldn't apply" chip, never crash the stream.
 */
export function applyUiCommand(cmd: LeoUiCommand, navigate: NavigateFn): string | null {
  if (cmd.kind === "navigate") {
    const path = SPACE_PATHS[cmd.space ?? ""];
    if (!path) return null;
    const search = uiCommandSearch(cmd.params);
    // navigate() is TanStack's imperative navigate, so a repeat call with a
    // new search value re-renders the already-mounted route's search state
    // instead of requiring a fresh mount.
    void navigate({ to: path, search });
    const filters = Object.entries(search)
      .map(([k, v]) => `${k}=${v}`)
      .join(" · ");
    return filters ? `${cmd.space} · ${filters}` : `${cmd.space}`;
  }

  if (cmd.kind === "prefill_schedule") {
    const p = cmd.params ?? {};
    const search: Record<string, string> = { create: "1" };
    if (p.name) search.name = p.name;
    if (p.cron) search.cron = p.cron;
    if (p.prompt) search.prompt = p.prompt;
    if (p.desc) search.desc = p.desc;
    void navigate({ to: "/schedules", search });
    return p.name ? `schedules · new "${p.name}"` : "schedules · new";
  }

  return null;
}

/** Fallback label for commands that couldn't be applied. */
export function describeUiCommand(cmd: LeoUiCommand): string {
  return cmd.space ? `${cmd.kind} → ${cmd.space}` : cmd.kind;
}
