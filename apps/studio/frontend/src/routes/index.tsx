import { createFileRoute } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import MissionControl from "@/components/mission/MissionControl";
import TabBar from "@/components/shell/TabBar";

export const Route = createFileRoute("/")({
  component: HomeOverview,
});

function HomeOverview() {
  const t = useTranslations("shell");
  return (
    <div className="flex w-full flex-col">
      <div className="px-6 pt-4">
        <TabBar
          ariaLabel={t("tabs.homeAria")}
          tabs={[
            { id: "overview", label: t("tabs.overview"), to: "/", active: true },
            { id: "fleet", label: t("tabs.fleet"), to: "/fleet", active: false },
          ]}
        />
      </div>
      <MissionControl />
    </div>
  );
}
