import { createFileRoute } from "@tanstack/react-router";
import { lazy, Suspense } from "react";
import { useTranslations } from "use-intl";
import TabBar from "@/components/shell/TabBar";
import { firstSearchString, preserveRetiredSearch } from "@/lib/retiredRoutes";
import type { RetiredSearch, RetiredSearchValue } from "@/lib/retiredRoutes";

const FleetView = lazy(() => import("@/components/fleet/FleetView"));

// Fleet is the redirect target for every retired route (/playfield, /runs,
// /invocations, and their siblings), so its search contract must keep
// whatever filters those old URLs carried instead of only understanding `s`.
export type FleetSearch = RetiredSearch & {
  s?: string;
  status?: RetiredSearchValue;
  playbook?: RetiredSearchValue;
  // `project`/`project_null`/`q` are actively read by FleetView to scope and
  // search the session list — unlike the other retired-route leftovers below,
  // these are load-bearing, not just preserved for old bookmarks.
  project?: string;
  project_null?: boolean;
  q?: string;
  page?: RetiredSearchValue;
  skill?: RetiredSearchValue;
  sessions?: RetiredSearchValue;
  invocation?: RetiredSearchValue;
};

export function validateFleetSearch(search: Record<string, unknown>): FleetSearch {
  const preserved = preserveRetiredSearch(search);
  const s = firstSearchString(search.s);
  if (!s) {
    delete preserved.s;
  } else {
    preserved.s = s;
  }

  // project/project_null/q are parsed to clean scalar types (not the raw
  // RetiredSearchValue passthrough) since the view reads them directly.
  delete preserved.project;
  delete preserved.project_null;
  delete preserved.q;
  const out: FleetSearch = preserved;
  const project = firstSearchString(search.project);
  if (project) out.project = project;
  if (search.project_null === true || search.project_null === "true") out.project_null = true;
  const q = firstSearchString(search.q);
  if (q) out.q = q;

  return out;
}

export const Route = createFileRoute("/fleet")({
  validateSearch: validateFleetSearch,
  component: FleetPage,
});

function FleetPage() {
  const t = useTranslations("shell");
  return (
    <div className="flex h-full w-full flex-col">
      <div className="px-6 pt-4">
        <TabBar
          ariaLabel={t("tabs.homeAria")}
          tabs={[
            { id: "overview", label: t("tabs.overview"), to: "/", active: false },
            { id: "fleet", label: t("tabs.fleet"), to: "/fleet", active: true },
            { id: "attention", label: t("tabs.attention"), to: "/attention", active: false },
          ]}
        />
      </div>
      <Suspense
        fallback={
          <div className="flex min-h-0 flex-1">
            <div className="w-[25rem] shrink-0 space-y-2 border-e border-edge p-4">
              <div className="skeleton h-6 w-28 rounded" />
              {Array.from({ length: 7 }, (_, index) => (
                <div key={index} className="skeleton h-10 rounded" />
              ))}
            </div>
            <div className="hidden min-w-0 flex-1 place-items-center p-8 sm:grid">
              <div className="skeleton h-44 w-full max-w-xl rounded-lg" />
            </div>
          </div>
        }
      >
        <FleetView />
      </Suspense>
    </div>
  );
}
