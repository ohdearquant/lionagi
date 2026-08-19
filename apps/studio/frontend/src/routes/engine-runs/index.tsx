/**
 * Engine runs route boundary. Search validation stays eager and the page
 * implementation remains code-split by TanStack Router.
 */
import { createFileRoute } from "@tanstack/react-router";
import EngineRunsSpace from "@/components/engine-runs/EngineRunsSpace";
import type { EngineRunsRouteSearch } from "@/components/engine-runs/EngineRunsSpace";

function str(v: unknown): string | undefined {
  return typeof v === "string" && v ? v : undefined;
}

const STATUSES = ["running", "completed", "failed", "cancelled"] as const;

function engineStatus(v: unknown): (typeof STATUSES)[number] | undefined {
  const s = str(v);
  return s !== undefined && (STATUSES as readonly string[]).includes(s)
    ? (s as (typeof STATUSES)[number])
    : undefined;
}

export function validateEngineRunsSearch(search: Record<string, unknown>): EngineRunsRouteSearch {
  if (
    search.status !== undefined &&
    search.status !== "" &&
    engineStatus(search.status) === undefined
  ) {
    throw new Error(`invalid engine run status: ${JSON.stringify(search.status)}`);
  }
  return {
    ...(str(search.kind) ? { kind: str(search.kind) } : {}),
    ...(engineStatus(search.status) ? { status: engineStatus(search.status) } : {}),
    ...(str(search.session_id) ? { session_id: str(search.session_id) } : {}),
    ...(str(search.s) ? { s: str(search.s) } : {}),
  };
}

export const Route = createFileRoute("/engine-runs/")({
  validateSearch: validateEngineRunsSearch,
  component: EngineRunsRoute,
});

function EngineRunsRoute() {
  return <EngineRunsSpace search={Route.useSearch()} />;
}
