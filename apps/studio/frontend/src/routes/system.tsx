import { createFileRoute, redirect } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { useTranslations } from "use-intl";
import Timestamp from "@/components/ui/Timestamp";
import { API_BASE, getAdminDoctor, runMaintenance } from "@/lib/api";
import type { AdminDoctorResponse, MaintenanceAction } from "@/lib/api";
import { IconHealth, IconTool, IconSettings } from "@/components/ui/icons";
import { LOCALES } from "@/i18n/locales";

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

function applyTheme(theme: "dark" | "light") {
  document.documentElement.setAttribute("data-theme", theme);
  if (theme === "dark") {
    document.documentElement.classList.add("dark");
  } else {
    document.documentElement.classList.remove("dark");
  }
  localStorage.setItem("theme", theme);
}

function getTheme(): "dark" | "light" {
  if (typeof document === "undefined") return "dark";
  return (document.documentElement.getAttribute("data-theme") as "dark" | "light") ?? "dark";
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

function HealthSection({ doctor }: { doctor: AdminDoctorResponse | null; loading: boolean }) {
  const t = useTranslations("system");
  if (!doctor) return null;
  const h = doctor.db_health;
  const phantoms = doctor.phantom_sessions.length;
  return (
    <section className="flex flex-col gap-3">
      <SectionHead icon={<IconHealth size={18} />} label={t("sections.health")} />
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-body text-content-secondary">
        <span>
          <span className="font-mono text-content-primary">{formatBytes(h.size_bytes)}</span>{" "}
          {t("health.stateDbSuffix")}
        </span>
        <span>
          <span className="font-mono text-content-primary">{formatBytes(h.wal_bytes)}</span>{" "}
          {t("health.walSuffix")}
        </span>
        <span>
          <span className="font-mono text-content-primary">{h.wal_pending}</span>{" "}
          {t("health.walPendingSuffix")}
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
          className={btnSecondary}
          disabled={running !== null}
          onClick={() => void run("checkpoint")}
        >
          {running === "checkpoint" ? t("maintenance.running") : t("maintenance.checkpointWal")}
        </button>
        <button
          className={btnSecondary}
          disabled={running !== null}
          onClick={() => void run("prune")}
        >
          {running === "prune" ? t("maintenance.running") : t("maintenance.pruneOldData")}
        </button>
        <button
          className={btnSecondary}
          disabled={running !== null}
          onClick={() => void run("vacuum")}
        >
          {running === "vacuum" ? t("maintenance.running") : t("maintenance.vacuumDb")}
        </button>
        {phantoms > 0 && (
          <button className={btnDanger} disabled={running !== null} onClick={() => void pruneAll()}>
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

// ─── Settings section ─────────────────────────────────────────────────────────

function SettingsSection() {
  const t = useTranslations("system");
  const [theme, setTheme] = useState<"dark" | "light">(() => getTheme());
  const [locale, setLocale] = useState<string>(() => {
    const m = document.cookie.match(/NEXT_LOCALE=([^;]+)/);
    return m ? m[1] : "en";
  });

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
            <button className={btnBase} onClick={toggleTheme}>
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
  const [doctor, setDoctor] = useState<AdminDoctorResponse | null>(null);
  const [healthLoading, setHealthLoading] = useState(true);
  const fetchedRef = useRef(false);

  useEffect(() => {
    if (fetchedRef.current) return;
    fetchedRef.current = true;
    getAdminDoctor()
      .then(setDoctor)
      .catch(() => setDoctor(null))
      .finally(() => setHealthLoading(false));
  }, []);

  return (
    <main className="flex w-full flex-col gap-8 px-6 py-6 animate-page-enter">
      <header className="flex flex-col gap-0.5">
        <h1 className="text-heading font-semibold text-content-primary">{t("title")}</h1>
        <p className="text-body text-content-muted">{t("subtitle")}</p>
      </header>

      <div className="flex w-full max-w-3xl flex-col gap-8">
        {!healthLoading && <HealthSection doctor={doctor} loading={healthLoading} />}
        <MaintenanceSection doctor={doctor} />
        <SettingsSection />
      </div>
    </main>
  );
}
