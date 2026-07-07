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
  project?: RetiredSearchValue;
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
    return preserved;
  }
  return { ...preserved, s };
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
          ]}
        />
      </div>
      <Suspense fallback={null}>
        <FleetView />
      </Suspense>
    </div>
  );
}
