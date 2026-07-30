import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "use-intl";
import Button from "@/components/ui/Button";
import { IconPort, IconShield, IconTerminal } from "@/components/ui/icons";
import { resolveApiBase, resolveAuthToken } from "@/lib/api";
import { onConnectivityFailure } from "@/lib/connectivity";

const POLL_INTERVAL_MS = 5000;
// Floor between two connectivity-failure-triggered re-probes, so a burst of
// failed requests (a view retrying, several panels erroring at once) fires
// one /health check instead of one per request.
const FAILURE_REPROBE_THROTTLE_MS = 2000;

type ConnectivityStatus = "checking" | "connected" | "unreachable" | "wrongApp" | "needsPairing";

/**
 * Probe both the daemon's public liveness response and its authenticated
 * OpenAPI identity. `/health` alone cannot establish a usable connection:
 * a `li studio --no-open` process answers there while every application API
 * correctly rejects an unpaired browser. Keeping that tab behind a designed
 * pairing state prevents the shell from opening into a wall of 401s.
 */
async function probeDaemon(
  apiBase: string,
): Promise<"connected" | "unreachable" | "wrongApp" | "needsPairing"> {
  let response: Response;
  try {
    response = await fetch(`${apiBase}/health`);
  } catch {
    return "unreachable";
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return "wrongApp";
  }
  const status = (body as { status?: unknown } | null)?.status;
  if (!response.ok || status !== "ok") return "wrongApp";

  const token = resolveAuthToken();
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  try {
    response = await fetch(`${apiBase}/openapi.json`, { headers });
  } catch {
    return "unreachable";
  }
  if (response.status === 401 || response.status === 403) return "needsPairing";
  try {
    body = await response.json();
  } catch {
    return "wrongApp";
  }
  const title = (
    body as {
      info?: { title?: unknown };
    } | null
  )?.info?.title;
  return response.ok && title === "Lion Studio Server" ? "connected" : "wrongApp";
}

/**
 * Wraps the app shell with connectivity awareness. The daemon is the
 * operator's own `li studio` process, not a backend this app controls — a
 * failed probe means "not started yet" or "something else is on this port",
 * not an app crash. A failed probe replaces the data-dependent application
 * with a composed recovery surface so stale or malformed content never sits
 * behind a dismissible warning.
 */
export default function NoDaemonGate({ children }: { children: ReactNode }) {
  const t = useTranslations("daemon");
  const apiBase = resolveApiBase();
  const [status, setStatus] = useState<ConnectivityStatus>("checking");
  const activeRef = useRef(true);
  const lastReprobeRef = useRef(0);

  const check = useCallback(async () => {
    const result = await probeDaemon(apiBase);
    if (!activeRef.current) return;
    setStatus(result);
  }, [apiBase]);

  useEffect(() => {
    activeRef.current = true;
    void check();
    return () => {
      activeRef.current = false;
    };
  }, [check]);

  // Poll while in a bad state (catches recovery even with no other API
  // traffic). Once connected, stay quiet — a live daemon later failing a
  // request is caught by the connectivity-failure subscription below instead
  // of a background poll that never stops.
  useEffect(() => {
    if (status === "connected" || status === "checking") return;
    const id = setInterval(() => void check(), POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [status, check]);

  // Re-probe immediately when any API call anywhere in the app hits a
  // network-level failure — this is what catches a daemon going away while
  // the gate itself was last reporting "connected" and had stopped polling.
  useEffect(() => {
    return onConnectivityFailure(() => {
      const now = Date.now();
      if (now - lastReprobeRef.current < FAILURE_REPROBE_THROTTLE_MS) return;
      lastReprobeRef.current = now;
      void check();
    });
  }, [check]);

  const displayBase = apiBase || "http://127.0.0.1:8765";

  if (status === "connected") return <>{children}</>;

  const isChecking = status === "checking";
  const isWrongApp = status === "wrongApp";
  const needsPairing = status === "needsPairing";

  return (
    <div className="flex h-dvh min-h-[32rem] overflow-hidden bg-surface-base text-content-primary">
      <aside
        aria-hidden="true"
        className="hidden w-14 shrink-0 flex-col items-center border-r border-edge bg-surface-raised py-4 sm:flex"
      >
        <div className="grid size-8 place-items-center rounded-md border border-edge-strong bg-surface-raised font-data text-xs font-semibold text-content-primary">
          LI
        </div>
        <div className="mt-8 flex flex-col gap-4">
          {Array.from({ length: 5 }, (_, index) => (
            <span
              key={index}
              className="block size-5 rounded bg-surface-overlay"
              style={{ opacity: 1 - index * 0.12 }}
            />
          ))}
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 shrink-0 items-center border-b border-edge bg-surface-raised px-4">
          <span className="font-data text-xs font-semibold tracking-wide text-content-secondary">
            LION STUDIO
          </span>
          <span className="ml-auto inline-flex items-center gap-1.5 text-meta text-content-muted">
            <span className="size-1.5 rounded-full bg-status-pending" />
            {isChecking
              ? null
              : isWrongApp
                ? t("wrongApp.title")
                : needsPairing
                  ? t("pairing.title")
                  : t("unreachable.title")}
          </span>
        </header>

        <main
          role={isChecking ? "status" : "alert"}
          aria-live="polite"
          className="grid min-h-0 flex-1 place-items-center overflow-y-auto px-5 py-10 sm:px-10"
        >
          {isChecking ? (
            <div
              aria-label="Lion Studio"
              className="w-full max-w-xl rounded-xl border border-edge bg-surface-raised p-6 shadow-card"
            >
              <div className="flex items-center gap-3">
                <span className="grid size-10 place-items-center rounded-lg bg-surface-overlay text-content-muted">
                  <IconTerminal size={20} />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="h-3 w-40 animate-pulse rounded bg-surface-overlay" />
                  <div className="mt-2 h-2.5 w-64 max-w-full animate-pulse rounded bg-surface-overlay" />
                </div>
              </div>
              <div className="mt-6 space-y-2.5">
                <div className="h-9 animate-pulse rounded-md bg-surface-overlay" />
                <div className="h-9 animate-pulse rounded-md bg-surface-overlay" />
              </div>
            </div>
          ) : (
            <section className="w-full max-w-xl rounded-xl border border-edge bg-surface-raised p-6 shadow-card sm:p-8">
              <div className="flex items-start gap-4">
                <span
                  className={`grid size-11 shrink-0 place-items-center rounded-lg ${
                    isWrongApp || needsPairing
                      ? "bg-status-warning-bg text-status-warning"
                      : "bg-surface-overlay text-content-secondary"
                  }`}
                >
                  {isWrongApp ? (
                    <IconPort size={22} />
                  ) : needsPairing ? (
                    <IconShield size={22} />
                  ) : (
                    <IconTerminal size={22} />
                  )}
                </span>
                <div className="min-w-0">
                  <h1 className="text-xl font-semibold tracking-tight text-content-primary">
                    {isWrongApp
                      ? t("wrongApp.title")
                      : needsPairing
                        ? t("pairing.title")
                        : t("unreachable.title")}
                  </h1>
                  <p className="mt-2 text-body leading-relaxed text-content-secondary">
                    {isWrongApp
                      ? t("wrongApp.body", { base: displayBase })
                      : needsPairing
                        ? t("pairing.body", { base: displayBase })
                        : t("unreachable.body", { base: displayBase })}
                  </p>
                </div>
              </div>

              <div className="mt-6 border-t border-edge pt-5">
                {isWrongApp ? (
                  <>
                    <p className="text-body text-content-secondary">{t("wrongApp.fix")}</p>
                    <code className="mt-2 block overflow-x-auto rounded-md border border-edge bg-surface-base px-3 py-2.5 font-data text-xs text-content-primary">
                      {t("wrongApp.command")}
                    </code>
                  </>
                ) : needsPairing ? (
                  <>
                    <p className="text-body text-content-secondary">{t("pairing.fix")}</p>
                    <code className="mt-2 block overflow-x-auto rounded-md border border-edge bg-surface-base px-3 py-2.5 font-data text-xs text-content-primary">
                      {t("pairing.command")}
                    </code>
                  </>
                ) : (
                  <div className="space-y-3">
                    <div>
                      <code className="block overflow-x-auto rounded-md border border-edge bg-surface-base px-3 py-2.5 font-data text-xs text-content-primary">
                        {t("unreachable.install")}
                      </code>
                    </div>
                    <div>
                      <code className="block overflow-x-auto rounded-md border border-edge bg-surface-base px-3 py-2.5 font-data text-xs text-content-primary">
                        {t("unreachable.run")}
                      </code>
                    </div>
                  </div>
                )}
              </div>

              <div className="mt-6 flex flex-col-reverse gap-3 border-t border-edge pt-5 sm:flex-row sm:items-center sm:justify-between">
                <p className="min-w-0 truncate font-data text-[11px] text-content-muted">
                  {displayBase}
                </p>
                <Button variant="primary" size="sm" onClick={() => void check()}>
                  {t("retry")}
                </Button>
              </div>
            </section>
          )}
        </main>

        <footer className="flex h-7 shrink-0 items-center border-t border-edge bg-surface-raised px-3 text-[11px] text-content-muted">
          <span className="font-data">lionagi</span>
        </footer>
      </div>
    </div>
  );
}
