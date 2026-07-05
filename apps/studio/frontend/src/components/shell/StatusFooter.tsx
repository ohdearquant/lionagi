import { useEffect, useState } from "react";
import { useTranslations } from "use-intl";
import { getStats, resolveApiBase, type StudioStats } from "@/lib/api";

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

    async function poll() {
      try {
        const [s] = await Promise.all([
          getStats(),
          fetch(`${apiBase}/health`)
            .then((r) => {
              if (active) setHealthy(r.ok);
            })
            .catch(() => {
              if (active) setHealthy(false);
            }),
        ]);
        if (active) setStats(s);
      } catch {
        if (active) setHealthy(false);
      }
    }

    void poll();
    const id = setInterval(poll, 30_000);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, [apiBase]);

  const dbSize = stats?.db?.size_bytes;
  const version = import.meta.env.VITE_APP_VERSION as string | undefined;

  return (
    <footer className="flex h-6 shrink-0 items-center gap-3 border-t border-edge px-3 font-data text-[length:var(--t-xs)] text-content-muted">
      {/* Health dot + backend base */}
      <span className="flex items-center gap-1.5">
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
        <span className="tabular-nums text-[length:var(--t-xs)]">{apiBase || "localhost"}</span>
      </span>

      {/* DB size */}
      {dbSize !== undefined ? (
        <>
          <span className="text-edge-strong">·</span>
          <span className="tabular-nums">
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
      <span className="ml-auto truncate">
        {t("footer.ecosystemPrefix")}{" "}
        <a
          href="https://khive.ai"
          target="_blank"
          rel="noopener noreferrer"
          title={t("footer.ecosystemLink")}
          className="text-content-muted underline decoration-edge-strong underline-offset-2 transition-colors duration-100 hover:text-content-primary"
        >
          {t("footer.ecosystemLink")}
        </a>{" "}
        {t("footer.ecosystemSuffix")}
        <span className="sr-only"> ({t("footer.ecosystemNewTab")})</span>
      </span>
    </footer>
  );
}
