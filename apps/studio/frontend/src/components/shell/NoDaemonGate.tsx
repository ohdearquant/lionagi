import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "use-intl";
import Button from "@/components/ui/Button";
import { IconClose, IconPort, IconTerminal } from "@/components/ui/icons";
import { resolveApiBase } from "@/lib/api";
import { onConnectivityFailure } from "@/lib/connectivity";

const POLL_INTERVAL_MS = 5000;
// Floor between two connectivity-failure-triggered re-probes, so a burst of
// failed requests (a view retrying, several panels erroring at once) fires
// one /health check instead of one per request.
const FAILURE_REPROBE_THROTTLE_MS = 2000;

type ConnectivityStatus = "checking" | "connected" | "unreachable" | "wrongApp";

/**
 * Probe the configured API base's /health endpoint and classify what answered.
 * The lionagi daemon serves an unauthenticated `{ status: "ok" }` at /health
 * (lionagi/studio/app.py) with no auth gate — any other shape, a non-2xx, or
 * a body that isn't JSON at all means something other than the daemon is on
 * that port. A network-level failure (nothing listening, CORS) is a separate
 * bucket: there's no program to point at, just nothing running yet.
 */
async function probeDaemon(apiBase: string): Promise<"connected" | "unreachable" | "wrongApp"> {
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
  return response.ok && status === "ok" ? "connected" : "wrongApp";
}

/**
 * Wraps the app shell with connectivity awareness. The daemon is the
 * operator's own `li studio` process, not a backend this app controls — a
 * failed probe means "not started yet" or "something else is on this port",
 * not an app crash. The shell keeps rendering underneath a dismissible
 * banner rather than blanking the screen; views that don't need the daemon
 * stay usable.
 */
export default function NoDaemonGate({ children }: { children: ReactNode }) {
  const t = useTranslations("daemon");
  const apiBase = resolveApiBase();
  const [status, setStatus] = useState<ConnectivityStatus>("checking");
  const [dismissed, setDismissed] = useState(false);
  const activeRef = useRef(true);
  const lastReprobeRef = useRef(0);

  const check = useCallback(async () => {
    const result = await probeDaemon(apiBase);
    if (!activeRef.current) return;
    setStatus(result);
    // Recovering clears any earlier dismissal so the NEXT failure surfaces
    // the banner again instead of staying silently hidden forever.
    if (result === "connected") setDismissed(false);
  }, [apiBase]);

  useEffect(() => {
    activeRef.current = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial /health probe on mount; setState only lands after the async fetch resolves, guarded by activeRef against a torn-down effect
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
  const showBanner = (status === "unreachable" || status === "wrongApp") && !dismissed;

  return (
    <>
      {children}
      {showBanner && (
        <div
          role="alert"
          className="fixed inset-x-0 bottom-0 z-40 flex flex-col gap-2.5 border-t border-edge bg-surface-raised px-4 py-3 shadow-card-hover sm:flex-row sm:items-start sm:justify-between"
          style={{ boxShadow: "var(--shadow-card-hover)" }}
        >
          <div className="flex items-start gap-2.5 text-body text-content-primary">
            <span className="mt-0.5 shrink-0 text-content-muted">
              {status === "wrongApp" ? <IconPort size={16} /> : <IconTerminal size={16} />}
            </span>
            {status === "wrongApp" ? (
              <div>
                <p className="font-medium">{t("wrongApp.title")}</p>
                <p className="text-meta text-content-muted">
                  {t("wrongApp.body", { base: displayBase })}
                </p>
                <p className="mt-1 text-meta text-content-muted">
                  {t("wrongApp.fix")}{" "}
                  <span className="font-data text-content-secondary">{t("wrongApp.command")}</span>
                </p>
              </div>
            ) : (
              <div>
                <p className="font-medium">{t("unreachable.title")}</p>
                <p className="text-meta text-content-muted">
                  {t("unreachable.body", { base: displayBase })}
                </p>
                <p className="mt-1 text-meta text-content-secondary">
                  <span className="font-data">{t("unreachable.install")}</span>
                  {" · "}
                  <span className="font-data">{t("unreachable.run")}</span>
                </p>
                <p className="text-meta text-content-muted">
                  {t("unreachable.runAltNote")}{" "}
                  <span className="font-data text-content-secondary">
                    {t("unreachable.runAlt")}
                  </span>
                </p>
              </div>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2 self-end sm:self-start">
            <Button variant="primary" size="sm" onClick={() => void check()}>
              {t("retry")}
            </Button>
            <button
              type="button"
              aria-label={t("dismiss")}
              onClick={() => setDismissed(true)}
              className="text-content-muted transition-colors duration-100 hover:text-content-primary"
            >
              <IconClose size={12} strokeWidth={2.25} />
            </button>
          </div>
        </div>
      )}
    </>
  );
}
