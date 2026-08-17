import { createFileRoute, redirect, Link } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "use-intl";
import Timestamp from "@/components/ui/Timestamp";
import Button from "@/components/ui/Button";
import { API_BASE, getAdminDoctor, getAdminEvents, runMaintenance } from "@/lib/api";
import type { AdminDoctorResponse, AdminEvent, MaintenanceAction } from "@/lib/api";
import { IconHealth, IconTool, IconSettings, IconLog, IconEngine } from "@/components/ui/icons";
import { LOCALES } from "@/i18n/locales";
import { applyTheme, getTheme, THEME_CHANGE_EVENT } from "@/lib/theme";

// Old tab values are accepted so deep links keep working; the page itself
// renders every section in one column.
const SYSTEM_TABS = ["health", "maintenance", "settings"] as const;
type SystemTab = (typeof SYSTEM_TABS)[number];

export const Route = createFileRoute("/system")({
  validateSearch: (search: Record<string, unknown>): { tab?: SystemTab | "schedules" } => {
    const tab = search.tab;
    // "schedules" is kept so beforeLoad can redirect old links to the space.
    if (tab === "schedules") return { tab: "schedules" };
    return SYSTEM_TABS.includes(tab as SystemTab) ? { tab: tab as SystemTab } : {};
  },
  beforeLoad: ({ search }) => {
    if (search.tab === "schedules") throw redirect({ to: "/schedules" });
  },
  component: SystemPage,
});

// ─── Helpers ─────────────────────────────────────────────────────────────────

function formatBytes(value: number): string {
  if (value === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(value) / Math.log(1024));
  return `${(value / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
}

// ─── Section header ───────────────────────────────────────────────────────────

function SectionHead({
  icon,
  label,
  children,
}: {
  icon: React.ReactNode;
  label: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between border-b border-edge pb-2">
      <h2 className="flex items-center gap-2 text-label font-semibold text-content-primary">
        <span className="h-4 w-4 text-content-muted">{icon}</span>
        {label}
      </h2>
      {children}
    </div>
  );
}

// ─── Health section ───────────────────────────────────────────────────────────

function HealthSection({ doctor }: { doctor: AdminDoctorResponse }) {
  const t = useTranslations("system");
  const tShell = useTranslations("shell");
  const h = doctor.db_health;
  const phantoms = doctor.phantom_sessions.length;
  return (
    <section className="flex flex-col gap-3">
      <SectionHead icon={<IconHealth size={18} />} label={t("sections.health")}>
        <span className="inline-flex items-center gap-1.5 font-data text-meta text-status-success">
          <span className="size-1.5 rounded-full bg-status-success" />
          {tShell("footer.healthy")}
        </span>
      </SectionHead>
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-body text-content-secondary">
        <span>
          <span
            className={
              h.size_alert === true
                ? "font-mono text-status-warning"
                : "font-mono text-content-primary"
            }
            title={
              h.size_alert === true && h.size_threshold_bytes !== undefined
                ? `${formatBytes(h.size_bytes)} / ${formatBytes(h.size_threshold_bytes)}`
                : undefined
            }
          >
            {formatBytes(h.size_bytes)}
          </span>{" "}
          {t("health.stateDbSuffix")}
        </span>
        <span>
          <span className="font-mono text-content-primary">{formatBytes(h.wal_bytes)}</span>{" "}
          {t("health.walSuffix")}
        </span>
        <span className="text-content-muted">
          {t("health.checked")} <Timestamp value={doctor.diagnostic_run_at} exact />
        </span>
      </div>
      <div className="flex items-center gap-2 text-body text-content-secondary">
        <span
          className={
            phantoms === 0
              ? "text-[var(--status-success)] font-mono"
              : "text-[var(--status-error)] font-mono"
          }
        >
          {phantoms}
        </span>
        <span>{phantoms !== 1 ? t("health.phantomSessions") : t("health.phantomSession")}</span>
        {phantoms > 0 && (
          <a
            href="#maintenance"
            className="ml-1 text-meta text-[var(--accent)] underline-offset-2 hover:underline"
          >
            {t("health.manageBelow")}
          </a>
        )}
      </div>
    </section>
  );
}

// ─── Maintenance section ──────────────────────────────────────────────────────

function MaintenanceSection({ doctor }: { doctor: AdminDoctorResponse | null }) {
  const t = useTranslations("system");
  const [running, setRunning] = useState<MaintenanceAction | null>(null);
  const [result, setResult] = useState<{ ok: boolean; msg: string } | null>(null);

  async function run(action: MaintenanceAction) {
    setRunning(action);
    setResult(null);
    try {
      const res = await runMaintenance(action);
      let msg: string;
      if (res.action === "vacuum") {
        msg =
          res.status === "skipped"
            ? t("maintenance.vacuumSkipped")
            : t("maintenance.vacuumDone", { status: res.status ?? "ok" });
      } else if (res.action === "checkpoint") {
        msg =
          res.busy == null
            ? t("maintenance.checkpointSkipped")
            : t("maintenance.checkpointDone", {
                busy: res.busy,
                logPages: res.log_pages ?? 0,
                checkpointed: res.checkpointed ?? 0,
              });
      } else {
        msg = t("maintenance.pruneResult", {
          sessions: res.sessions_pruned ?? 0,
          runs: res.runs_pruned ?? 0,
        });
      }
      setResult({ ok: true, msg });
    } catch (err) {
      setResult({
        ok: false,
        msg: err instanceof Error ? err.message : t("maintenance.operationFailed"),
      });
    } finally {
      setRunning(null);
    }
  }

  async function pruneAll() {
    if (!doctor) return;
    const count = doctor.phantom_sessions.length;
    if (count === 0) return;
    if (!window.confirm(t("maintenance.confirmPrune", { count }))) return;
    setRunning("prune");
    setResult(null);
    try {
      const { pruneAdmin } = await import("@/lib/api");
      const res = await pruneAdmin({ all_phantom: true });
      setResult({ ok: true, msg: t("maintenance.prunedCount", { count: res.pruned }) });
    } catch (err) {
      setResult({
        ok: false,
        msg: err instanceof Error ? err.message : t("maintenance.pruneFailed"),
      });
    } finally {
      setRunning(null);
    }
  }

  const btnBase =
    "rounded px-3 py-1.5 text-meta font-medium transition-colors duration-100 disabled:opacity-40";
  const btnSecondary = `${btnBase} border border-edge bg-surface-overlay text-content-secondary hover:border-edge-strong hover:text-content-primary`;
  const btnDanger = `${btnBase} border border-[var(--status-error)]/40 bg-[var(--status-error-bg)] text-content-primary hover:bg-[var(--status-error)]/20`;

  const phantoms = doctor?.phantom_sessions.length ?? 0;

  return (
    <section id="maintenance" className="flex flex-col gap-3">
      <SectionHead icon={<IconTool size={18} />} label={t("sections.maintenance")} />

      {result && (
        <div
          className={`rounded px-3 py-2 text-body ${
            result.ok
              ? "border border-[var(--status-success)]/25 bg-[var(--status-success-bg)] text-content-primary"
              : "border border-[var(--status-error)]/30 bg-[var(--status-error-bg)] text-content-primary"
          }`}
        >
          {result.msg}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className={btnSecondary}
          disabled={running !== null}
          onClick={() => void run("checkpoint")}
        >
          {running === "checkpoint" ? t("maintenance.running") : t("maintenance.checkpointWal")}
        </button>
        <button
          type="button"
          className={btnSecondary}
          disabled={running !== null}
          onClick={() => void run("prune")}
        >
          {running === "prune" ? t("maintenance.running") : t("maintenance.pruneOldData")}
        </button>
        <button
          type="button"
          className={btnSecondary}
          disabled={running !== null}
          onClick={() => void run("vacuum")}
        >
          {running === "vacuum" ? t("maintenance.running") : t("maintenance.vacuumDb")}
        </button>
        {phantoms > 0 && (
          <button
            type="button"
            className={btnDanger}
            disabled={running !== null}
            onClick={() => void pruneAll()}
          >
            {t("maintenance.prunePhantoms", {
              count: phantoms,
              plural: phantoms !== 1 ? "s" : "",
            })}
          </button>
        )}
      </div>
      <p className="text-meta text-content-muted">{t("maintenance.hint")}</p>
    </section>
  );
}

// ─── Admin events section ─────────────────────────────────────────────────────

const ADMIN_EVENTS_LIMIT = 100;

function AdminEventsSection() {
  const t = useTranslations("system");
  const tDaemon = useTranslations("daemon");
  const [events, setEvents] = useState<AdminEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [actionFilter, setActionFilter] = useState("");
  const [targetFilter, setTargetFilter] = useState("");

  const load = useCallback((action: string, targetId: string) => {
    let alive = true;
    setLoading(true);
    setError(false);
    getAdminEvents({
      action: action.trim() || undefined,
      target_id: targetId.trim() || undefined,
      limit: ADMIN_EVENTS_LIMIT,
    })
      .then((res) => {
        if (alive) setEvents(res);
      })
      .catch(() => {
        if (alive) setError(true);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load() calls setState inside async callbacks; synchronous reset clears stale events before the fetch resolves
    return load(actionFilter, targetFilter);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- explicit "Apply" submit below re-triggers with the current filter values; typing alone shouldn't refetch on every keystroke
  }, []);

  function applyFilters(e: React.FormEvent) {
    e.preventDefault();
    load(actionFilter, targetFilter);
  }

  return (
    <section className="flex flex-col gap-3">
      <SectionHead icon={<IconLog size={18} />} label={t("sections.adminEvents")} />

      <form onSubmit={applyFilters} className="flex flex-wrap items-center gap-2">
        <input
          type="text"
          value={actionFilter}
          onChange={(e) => setActionFilter(e.target.value)}
          placeholder={t("adminEvents.actionPlaceholder")}
          aria-label={t("adminEvents.actionPlaceholder")}
          className="min-w-0 flex-1 rounded border border-edge bg-surface-overlay px-2 py-1 text-body text-content-primary focus:outline-none"
        />
        <input
          type="text"
          value={targetFilter}
          onChange={(e) => setTargetFilter(e.target.value)}
          placeholder={t("adminEvents.targetPlaceholder")}
          aria-label={t("adminEvents.targetPlaceholder")}
          className="min-w-0 flex-1 rounded border border-edge bg-surface-overlay px-2 py-1 text-body text-content-primary focus:outline-none"
        />
        <Button type="submit" variant="secondary" size="sm">
          {t("adminEvents.filter")}
        </Button>
      </form>

      {error ? (
        <div
          role="alert"
          className="flex flex-wrap items-center justify-between gap-3 rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-content-secondary"
        >
          <span>{t("adminEvents.loadError")}</span>
          <Button variant="secondary" size="sm" onClick={() => load(actionFilter, targetFilter)}>
            {tDaemon("retry")}
          </Button>
        </div>
      ) : loading ? (
        <div className="space-y-2">
          <div className="h-4 w-2/3 animate-pulse rounded bg-surface-overlay" />
          <div className="h-4 w-1/2 animate-pulse rounded bg-surface-overlay" />
        </div>
      ) : events.length === 0 ? (
        <p className="text-body text-content-muted">{t("adminEvents.empty")}</p>
      ) : (
        <div className="max-h-80 overflow-y-auto rounded border border-edge">
          <table className="w-full text-left" style={{ borderCollapse: "collapse" }}>
            <thead>
              <tr
                className="text-[length:var(--t-xs)] uppercase tracking-[0.08em] text-content-muted"
                style={{ position: "sticky", top: 0 }}
              >
                <th className="bg-surface-raised px-2.5 py-2 font-medium">
                  {t("adminEvents.when")}
                </th>
                <th className="bg-surface-raised px-2.5 py-2 font-medium">
                  {t("adminEvents.action")}
                </th>
                <th className="bg-surface-raised px-2.5 py-2 font-medium">
                  {t("adminEvents.target")}
                </th>
                <th className="bg-surface-raised px-2.5 py-2 font-medium">
                  {t("adminEvents.actor")}
                </th>
              </tr>
            </thead>
            <tbody>
              {events.map((ev) => (
                <tr key={ev.id} className="border-t border-edge-subtle">
                  <td className="whitespace-nowrap px-2.5 py-2 text-body text-content-muted">
                    <Timestamp value={ev.created_at} />
                  </td>
                  <td className="px-2.5 py-2 font-data text-body text-content-primary">
                    {ev.action}
                  </td>
                  <td className="px-2.5 py-2 font-mono text-meta text-content-secondary">
                    {ev.target_id ?? "—"}
                  </td>
                  <td className="px-2.5 py-2 text-body text-content-secondary">{ev.actor}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// ─── Data section — links to browsing surfaces that don't fit inline ─────────

function DataSection() {
  const t = useTranslations("system");
  const linkCls =
    "flex items-center justify-between gap-2 rounded border border-edge bg-surface-raised px-3 py-2 text-body text-content-secondary transition-colors duration-100 hover:border-edge-strong hover:text-content-primary";
  return (
    <section className="flex flex-col gap-3">
      <SectionHead icon={<IconEngine size={18} />} label={t("sections.data")} />
      <div className="flex flex-col gap-2">
        <Link to="/engine-runs" className={linkCls}>
          <span>{t("data.engineRuns")}</span>
          <span aria-hidden="true">→</span>
        </Link>
        <Link to="/definitions" className={linkCls}>
          <span>{t("data.definitions")}</span>
          <span aria-hidden="true">→</span>
        </Link>
      </div>
    </section>
  );
}

// ─── Settings section ─────────────────────────────────────────────────────────

function SettingsSection() {
  const t = useTranslations("system");
  const [theme, setTheme] = useState<"dark" | "light">(() => getTheme());
  const [locale, setLocale] = useState<string>(() => {
    const m = document.cookie.match(/NEXT_LOCALE=([^;]+)/);
    return m ? m[1] : "en";
  });

  useEffect(() => {
    const syncTheme = () => setTheme(getTheme());
    window.addEventListener(THEME_CHANGE_EVENT, syncTheme);
    window.addEventListener("storage", syncTheme);
    return () => {
      window.removeEventListener(THEME_CHANGE_EVENT, syncTheme);
      window.removeEventListener("storage", syncTheme);
    };
  }, []);

  function toggleTheme() {
    const next: "dark" | "light" = theme === "dark" ? "light" : "dark";
    setTheme(next);
    applyTheme(next);
  }

  function selectLocale(next: string) {
    document.cookie = `NEXT_LOCALE=${next};path=/;max-age=31536000;SameSite=Lax`;
    setLocale(next);
    // Reload to pick up new message bundle
    window.location.reload();
  }

  const rowCls = "flex items-center justify-between gap-4 py-2 text-body";
  const labelCls = "text-content-secondary";
  const valueCls = "font-mono text-content-primary";

  const btnBase =
    "rounded px-3 py-1 text-meta font-medium border border-edge bg-surface-overlay text-content-secondary hover:border-edge-strong hover:text-content-primary transition-colors duration-100";

  return (
    <section className="flex flex-col gap-1">
      <SectionHead icon={<IconSettings size={18} />} label={t("sections.settings")} />

      <div className="flex flex-col divide-y divide-edge-subtle">
        <div className={rowCls}>
          <span className={labelCls}>{t("settings.theme")}</span>
          <div className="flex items-center gap-3">
            <span className={valueCls}>
              {theme === "dark" ? t("settings.themeDark") : t("settings.themeLight")}
            </span>
            <button type="button" className={btnBase} onClick={toggleTheme}>
              {theme === "dark" ? t("settings.switchToLight") : t("settings.switchToDark")}
            </button>
          </div>
        </div>

        <div className={rowCls}>
          <span className={labelCls}>{t("settings.language")}</span>
          <select
            className={btnBase}
            value={locale}
            onChange={(e) => selectLocale(e.target.value)}
            aria-label={t("settings.language")}
          >
            {LOCALES.map((l) => (
              <option key={l.code} value={l.code}>
                {l.native}
              </option>
            ))}
          </select>
        </div>

        <div className={rowCls}>
          <span className={labelCls}>{t("settings.apiBase")}</span>
          <span className="font-mono text-meta text-content-muted">
            {API_BASE || window.location.origin}
          </span>
        </div>

        <div className={rowCls}>
          <span className={labelCls}>{t("settings.studioVersion")}</span>
          <span className="font-mono text-meta text-content-muted">
            {typeof import.meta.env.VITE_APP_VERSION === "string"
              ? import.meta.env.VITE_APP_VERSION
              : "dev"}
          </span>
        </div>
      </div>
    </section>
  );
}

// ─── Root ─────────────────────────────────────────────────────────────────────

function SystemPage() {
  const t = useTranslations("system");
  const tDaemon = useTranslations("daemon");
  const [doctor, setDoctor] = useState<AdminDoctorResponse | null>(null);
  const [healthLoading, setHealthLoading] = useState(true);
  const [healthError, setHealthError] = useState(false);

  const loadHealth = useCallback(async () => {
    setHealthLoading(true);
    setHealthError(false);
    try {
      setDoctor(await getAdminDoctor());
    } catch {
      setDoctor(null);
      setHealthError(true);
    } finally {
      setHealthLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial async health request owns the loading/error lifecycle
    void loadHealth();
  }, [loadHealth]);

  return (
    <main className="mx-auto flex w-full max-w-5xl flex-col gap-8 px-4 py-6 animate-page-enter sm:px-6 lg:py-8">
      <header className="flex flex-col gap-0.5">
        <h1 className="text-page-title font-semibold text-content-primary">{t("title")}</h1>
        <p className="text-body text-content-muted">{t("subtitle")}</p>
      </header>

      <div className="grid w-full gap-8 lg:grid-cols-[minmax(0,1.15fr)_minmax(20rem,0.85fr)] lg:items-start">
        <div className="flex min-w-0 flex-col gap-8">
          {healthLoading ? (
            <section className="flex flex-col gap-3" aria-busy="true">
              <SectionHead icon={<IconHealth size={18} />} label={t("sections.health")} />
              <div className="space-y-2">
                <div className="h-4 w-2/3 animate-pulse rounded bg-surface-overlay" />
                <div className="h-4 w-1/2 animate-pulse rounded bg-surface-overlay" />
              </div>
            </section>
          ) : healthError || !doctor ? (
            <section className="flex flex-col gap-3">
              <SectionHead icon={<IconHealth size={18} />} label={t("sections.health")} />
              <div
                role="alert"
                className="flex flex-wrap items-center justify-between gap-3 rounded border border-status-failure/30 bg-status-error-bg px-3 py-3"
              >
                <p className="text-body text-content-secondary">
                  {t("maintenance.operationFailed")}
                </p>
                <button
                  type="button"
                  onClick={() => void loadHealth()}
                  className="rounded border border-edge-strong bg-surface-raised px-3 py-1.5 text-meta font-medium text-content-primary transition-colors hover:bg-surface-overlay"
                >
                  {tDaemon("retry")}
                </button>
              </div>
            </section>
          ) : (
            <HealthSection doctor={doctor} />
          )}
          <MaintenanceSection doctor={doctor} />
          <AdminEventsSection />
          <DataSection />
        </div>
        <SettingsSection />
      </div>
    </main>
  );
}
