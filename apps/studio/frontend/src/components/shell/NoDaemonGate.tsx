import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "use-intl";
import Button from "@/components/ui/Button";
import EmptyState from "@/components/ui/EmptyState";
import { IconTerminal } from "@/components/ui/icons";
import { resolveApiBase } from "@/lib/api";

const POLL_INTERVAL_MS = 5000;

type ConnectivityStatus = "checking" | "connected" | "unreachable";

async function probeDaemon(apiBase: string): Promise<boolean> {
  try {
    const response = await fetch(`${apiBase}/health`);
    return response.ok;
  } catch {
    return false;
  }
}

/**
 * Gates the app shell on the local daemon being reachable. A static hosted
 * deploy has no backend of its own — the daemon is the operator's own
 * `li studio` process — so a network failure here means "not started yet",
 * not a server error worth showing broken panels for.
 */
export default function NoDaemonGate({ children }: { children: ReactNode }) {
  const t = useTranslations("daemon");
  const apiBase = resolveApiBase();
  const [status, setStatus] = useState<ConnectivityStatus>("checking");
  const activeRef = useRef(true);

  const check = useCallback(async () => {
    const ok = await probeDaemon(apiBase);
    if (!activeRef.current) return;
    setStatus(ok ? "connected" : "unreachable");
  }, [apiBase]);

  useEffect(() => {
    activeRef.current = true;
    void check();
    return () => {
      activeRef.current = false;
    };
  }, [check]);

  useEffect(() => {
    if (status !== "unreachable") return;
    const id = setInterval(() => void check(), POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [status, check]);

  if (status === "unreachable") {
    return (
      <div className="flex h-dvh items-center justify-center bg-surface-base px-6">
        <EmptyState
          glyph={<IconTerminal className="h-5 w-5" />}
          title={t("title", { base: apiBase || "http://127.0.0.1:8765" })}
          body={
            <>
              <span className="block font-data">{t("bootstrapInstall")}</span>
              <span className="block font-data">{t("bootstrapRun")}</span>
              <span className="mt-2 block">{t("authNote")}</span>
            </>
          }
          action={
            <Button variant="primary" onClick={() => void check()}>
              {t("retry")}
            </Button>
          }
        />
      </div>
    );
  }

  // "checking" renders children immediately — the gate must never flash on
  // slow-but-live responses, only on an actual network failure / non-2xx.
  return <>{children}</>;
}
