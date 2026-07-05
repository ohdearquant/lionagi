import { createFileRoute } from "@tanstack/react-router";
import { lazy, Suspense } from "react";
import { useTranslations } from "use-intl";
import TabBar from "@/components/shell/TabBar";

const FleetView = lazy(() => import("@/components/fleet/FleetView"));

export const Route = createFileRoute("/fleet")({
  validateSearch: (search: Record<string, unknown>): { s?: string } => {
    const s = search.s;
    return typeof s === "string" && s.length > 0 ? { s } : {};
  },
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
