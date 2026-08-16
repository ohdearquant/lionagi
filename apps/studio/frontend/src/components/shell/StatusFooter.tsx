import { useEffect, useState } from "react";
import { useTranslations } from "use-intl";
import { getStats, resolveApiBase, type StudioStats } from "@/lib/api";

const HEALTH_POLL_MS = 30_000;
const STATS_POLL_MS = 5 * 60_000;
export const STATS_INITIAL_DELAY_MS = 2_000;

function formatBytes(b: number): string {
  if (b === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.min(Math.floor(Math.log(b) / Math.log(1024)), units.length - 1);
  return `${(b / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
}

export default function StatusFooter() {
  const t = useTranslations("shell");
  const [stats, setStats] = useState<StudioStats | null>(null);
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const apiBase = resolveApiBase();

  useEffect(() => {
    let active = true;
    let healthInFlight = false;
    let statsInFlight = false;
    let hasStats = false;

    async function pollHealth() {
      if (healthInFlight) return;
      healthInFlight = true;
      try {
        const response = await fetch(`${apiBase}/health`);
        if (active) setHealthy(response.ok);
      } catch {
        if (active) setHealthy(false);
      } finally {
        healthInFlight = false;
      }
    }

    async function pollStats() {
      if (statsInFlight || document.visibilityState === "hidden") return;
      statsInFlight = true;
      try {
        const next = await getStats();
        if (active) {
          hasStats = true;
          setStats(next);
        }
      } catch {
        // Diagnostics are optional footer context. Their failure must not
        // override the independent /health reading or make the daemon look
        // unavailable.
      } finally {
        statsInFlight = false;
      }
    }

    void pollHealth();
    const healthId = setInterval(() => void pollHealth(), HEALTH_POLL_MS);
    const initialStatsId = window.setTimeout(() => void pollStats(), STATS_INITIAL_DELAY_MS);
    const statsId = setInterval(() => void pollStats(), STATS_POLL_MS);
    const onVisibility = () => {
      if (document.visibilityState === "visible" && !hasStats) void pollStats();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      active = false;
      clearInterval(healthId);
      clearTimeout(initialStatsId);
      clearInterval(statsId);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [apiBase]);

  const dbSize = stats?.db?.size_bytes;
  // The backend already decides this against one threshold; re-deriving it
  // here would be a second opinion that can disagree with /api/stats.
  const dbOverThreshold = stats?.db?.size_alert === true;
  const dbThreshold = stats?.db?.size_threshold_bytes;
  const version = import.meta.env.VITE_APP_VERSION as string | undefined;

  return (
    <footer className="flex h-6 min-w-0 shrink-0 items-center gap-3 border-t border-edge px-3 font-data text-[length:var(--t-xs)] text-content-muted">
      {/* Health dot + backend base */}
      <span className="flex min-w-0 items-center gap-1.5">
        <span
          aria-label={healthy === false ? t("footer.unhealthy") : t("footer.healthy")}
          className="inline-block h-[5px] w-[5px] rounded-full"
          style={{
            background:
              healthy === null
                ? "var(--content-muted)"
                : healthy
                  ? "var(--status-success)"
                  : "var(--status-failure)",
          }}
        />
        <span className="truncate tabular-nums text-[length:var(--t-xs)]">
          {apiBase || "localhost"}
        </span>
      </span>

      {/* DB size */}
      {dbSize !== undefined ? (
        <>
          <span className="text-edge-strong">·</span>
          <span
            className={dbOverThreshold ? "tabular-nums text-status-warning" : "tabular-nums"}
            // Numbers only, so the reason for the colour survives without a
            // translated string in all sixteen locales.
            title={
              dbOverThreshold && dbThreshold !== undefined
                ? `${formatBytes(dbSize)} / ${formatBytes(dbThreshold)}`
                : undefined
            }
          >
            {t("footer.db")} {formatBytes(dbSize)}
          </span>
        </>
      ) : null}

      {/* Version */}
      {version ? (
        <>
          <span className="text-edge-strong">·</span>
          <span className="tabular-nums">
            {t("footer.version")} {version}
          </span>
        </>
      ) : null}

      {/* Ecosystem note */}
      <span className="ml-auto hidden truncate sm:inline">
        {t("footer.ecosystemPrefix")}{" "}
        <bdi>
          <a
            href="https://khive.ai"
            target="_blank"
            rel="noopener noreferrer"
            title={t("footer.ecosystemLink")}
            className="text-content-muted underline decoration-edge-strong underline-offset-2 transition-colors duration-100 hover:text-content-primary"
          >
            {t("footer.ecosystemLink")}
          </a>
        </bdi>{" "}
        {t("footer.ecosystemSuffix")}
        <span className="sr-only"> ({t("footer.ecosystemNewTab")})</span>
      </span>
    </footer>
  );
}
