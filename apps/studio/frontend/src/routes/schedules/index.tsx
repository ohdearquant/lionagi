/**
 * Schedules space — a table, one row per standing automation, with a
 * month-calendar alternate view. Run history lives on the schedule detail
 * page, not in this list.
 */
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "use-intl";
import Button from "@/components/ui/Button";
import CreateScheduleModal from "@/components/schedules/CreateScheduleModal";
import ScheduleCards from "@/components/schedules/ScheduleCards";
import SchedulesTable from "@/components/schedules/SchedulesTable";
import ScheduleDetailModal from "@/components/schedules/ScheduleDetailModal";
import SchedulesCalendar from "@/components/schedules/SchedulesCalendar";
import { useSchedulesData } from "@/components/schedules/data";

export interface ScheduleRouteSearch {
  create?: string;
  name?: string;
  cron?: string;
  prompt?: string;
  desc?: string;
  s?: string;
}

export function validateScheduleSearch(search: Record<string, unknown>): ScheduleRouteSearch {
  const pick = (key: string) =>
    typeof search[key] === "string" && search[key] ? { [key]: search[key] as string } : {};
  return {
    ...pick("create"),
    ...pick("name"),
    ...pick("cron"),
    ...pick("prompt"),
    ...pick("desc"),
    ...pick("s"),
  };
}

// ?create=1 (+ name/cron/prompt/desc) opens the create form pre-filled — a
// deep-link surface for proposing a new routine. The operator still reviews
// and submits; nothing is created from the URL alone. ?s=<id> opens that
// schedule's detail (deep link from attention rows).
export const Route = createFileRoute("/schedules/")({
  validateSearch: validateScheduleSearch,
  component: SchedulesSpace,
});

type View = "cards" | "table" | "calendar";

function ViewToggle({ view, onChange }: { view: View; onChange: (v: View) => void }) {
  const t = useTranslations("schedules");
  const seg = (v: View, label: string) => (
    <button
      type="button"
      onClick={() => onChange(v)}
      aria-pressed={view === v}
      className={[
        "h-7 rounded px-3 text-[length:var(--t-xs)] font-medium transition-colors duration-100",
        view === v
          ? "bg-surface-overlay text-content-primary"
          : "text-content-muted hover:text-content-primary",
      ].join(" ")}
    >
      {label}
    </button>
  );
  return (
    <div className="flex items-center gap-0.5 rounded-md border border-edge p-0.5">
      {seg("cards", t("viewCards"))}
      {seg("table", t("viewTable"))}
      {seg("calendar", t("calendar"))}
    </div>
  );
}

function EmptyState({ onNew }: { onNew: () => void }) {
  const t = useTranslations("schedules");
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-2 pb-16 text-center">
      <div className="mb-2 flex h-12 w-12 items-center justify-center rounded-full border border-edge bg-surface-raised text-xl text-content-muted">
        ◷
      </div>
      <p className="text-body font-medium text-content-secondary">{t("emptyTitle")}</p>
      <p className="max-w-sm text-meta text-content-muted">{t("emptyBody")}</p>
      <div className="mt-2">
        <Button variant="primary" size="sm" onClick={onNew}>
          + {t("emptyCta")}
        </Button>
      </div>
    </div>
  );
}

function TableSkeleton() {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2 px-6 pb-6">
      {Array.from({ length: 6 }, (_, i) => (
        <div key={i} className="skeleton h-10 rounded-md" />
      ))}
    </div>
  );
}

function SchedulesSpace() {
  const t = useTranslations("schedules");
  const { schedules, runs, nowMs, loading, error, refresh } = useSchedulesData();
  const [view, setView] = useState<View>("cards");
  const [showModal, setShowModal] = useState(false);
  const search = Route.useSearch();
  const navigate = useNavigate({ from: "/schedules/" });

  const prefill = useMemo(
    () =>
      search.create === "1"
        ? {
            name: search.name,
            cron_expr: search.cron,
            action_prompt: search.prompt,
            description: search.desc,
          }
        : undefined,
    [search.create, search.name, search.cron, search.prompt, search.desc],
  );

  useEffect(() => {
    if (search.create === "1") {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- deep-link param opens the modal; state is the modal flag itself
      setShowModal(true);
    }
  }, [search.create]);

  const closeModal = () => {
    setShowModal(false);
    if (search.create) {
      void navigate({ to: "/schedules", search: () => ({}), replace: true });
    }
  };

  // Detail selection lives in the URL (?s=<id>) so deep links, refresh, and
  // back/forward all agree with what is on screen.
  const openSchedule = (id: string) => {
    void navigate({ to: "/schedules", search: (prev) => ({ ...prev, s: id }) });
  };
  const closeSchedule = () => {
    void navigate({
      to: "/schedules",
      search: ({ s: _s, ...rest }) => rest,
      replace: true,
    });
  };

  return (
    <main className="flex h-full w-full flex-col animate-page-enter">
      <header className="flex shrink-0 items-end justify-between gap-4 px-6 pb-4 pt-5">
        <div className="flex flex-col gap-0.5">
          <h1 className="text-heading font-semibold text-content-primary">{t("title")}</h1>
          <p className="text-body text-content-muted">{t("subtitle")}</p>
        </div>
        <div className="flex items-center gap-2">
          <ViewToggle view={view} onChange={setView} />
          <Button variant="primary" size="sm" onClick={() => setShowModal(true)}>
            + {t("newSchedule")}
          </Button>
        </div>
      </header>

      {error && (
        <div className="mx-6 mb-3 rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {t("loadError")}
        </div>
      )}

      {loading ? (
        <TableSkeleton />
      ) : schedules.length === 0 ? (
        <EmptyState onNew={() => setShowModal(true)} />
      ) : view === "cards" ? (
        <ScheduleCards
          schedules={schedules}
          runs={runs}
          nowMs={nowMs}
          onChanged={refresh}
          onOpen={openSchedule}
        />
      ) : view === "table" ? (
        <SchedulesTable
          schedules={schedules}
          runs={runs}
          nowMs={nowMs}
          onChanged={refresh}
          onOpen={openSchedule}
        />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <SchedulesCalendar schedules={schedules} runs={runs} />
        </div>
      )}

      {showModal && (
        <CreateScheduleModal onClose={closeModal} onCreated={refresh} initial={prefill} />
      )}

      {search.s && (
        <ScheduleDetailModal scheduleId={search.s} onClose={closeSchedule} onChanged={refresh} />
      )}
    </main>
  );
}
