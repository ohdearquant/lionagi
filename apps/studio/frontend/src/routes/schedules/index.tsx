/**
 * Schedules space — standing automations as a time-flow board (upcoming →
 * today → running → done) with a month-calendar alternate view.
 */
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "use-intl";
import Button from "@/components/ui/Button";
import CreateScheduleModal from "@/components/schedules/CreateScheduleModal";
import SchedulesBoard from "@/components/schedules/SchedulesBoard";
import SchedulesCalendar from "@/components/schedules/SchedulesCalendar";
import { useSchedulesData } from "@/components/schedules/data";

// ?create=1 (+ name/cron/prompt/desc) opens the create form pre-filled — a
// deep-link surface for proposing a new routine. The operator still reviews
// and submits; nothing is created from the URL alone. ?s=<id> opens that
// schedule's detail (deep link from attention rows).
export const Route = createFileRoute("/schedules/")({
  validateSearch: (
    search: Record<string, unknown>,
  ): {
    create?: string;
    name?: string;
    cron?: string;
    prompt?: string;
    desc?: string;
    s?: string;
  } => {
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
  },
  component: SchedulesSpace,
});

type View = "board" | "calendar";

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
      {seg("board", t("board"))}
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

function BoardSkeleton() {
  return (
    <div className="flex min-h-0 flex-1 gap-3 px-6 pb-6">
      {Array.from({ length: 4 }, (_, i) => (
        <div key={i} className="flex flex-1 flex-col gap-2 rounded-lg border border-edge p-2 pt-11">
          <div className="skeleton h-20 rounded-md" />
          <div className="skeleton h-20 rounded-md" />
        </div>
      ))}
    </div>
  );
}

function SchedulesSpace() {
  const t = useTranslations("schedules");
  const { schedules, runs, nowMs, loading, error, refresh } = useSchedulesData();
  const [view, setView] = useState<View>("board");
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
        <BoardSkeleton />
      ) : schedules.length === 0 ? (
        <EmptyState onNew={() => setShowModal(true)} />
      ) : view === "board" ? (
        <SchedulesBoard
          schedules={schedules}
          runs={runs}
          nowMs={nowMs}
          onChanged={refresh}
          initialSelectedId={search.s}
        />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <SchedulesCalendar schedules={schedules} runs={runs} />
        </div>
      )}

      {showModal && (
        <CreateScheduleModal onClose={closeModal} onCreated={refresh} initial={prefill} />
      )}
    </main>
  );
}
