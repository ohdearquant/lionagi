import { useLayoutEffect, useRef } from "react";
import { useTranslations } from "use-intl";
import { Link } from "@tanstack/react-router";
import RunDetail from "@/components/history/RunDetail";

interface Props {
  runId: string | null;
  onBack?: () => void;
  showBack?: boolean;
}

export function resetDetailScrollPosition(element: HTMLElement | null): void {
  if (element) element.scrollTop = 0;
}

export default function SessionDetail({ runId, onBack, showBack = false }: Props) {
  const t = useTranslations("fleet");
  const scrollRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    resetDetailScrollPosition(scrollRef.current);
  }, [runId]);

  if (!runId) {
    return (
      <div className="flex h-full items-center justify-center px-8 py-12 text-center">
        <div className="max-w-xs">
          <div className="mx-auto grid size-11 place-items-center rounded-lg border border-edge bg-surface-raised text-content-muted">
            <svg
              width="22"
              height="22"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <circle cx="12" cy="12" r="8" />
              <path d="M12 8v4l3 2" />
            </svg>
          </div>
          <h2 className="mt-4 text-label font-semibold text-content-primary">
            {t("detail.title")}
          </h2>
          <p className="mt-1.5 text-body leading-relaxed text-content-muted">{t("detail.hint")}</p>
          <p className="mt-3 font-data text-meta text-content-muted">{t("empty.hint")}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Back affordance — narrow screens only (hidden once the split is side-by-side) */}
      {showBack && onBack && (
        <button
          type="button"
          onClick={onBack}
          className="flex items-center gap-2 border-b border-edge px-4 py-2 text-left transition-colors duration-100 hover:opacity-70 min-[960px]:hidden"
          aria-label={t("detail.back")}
        >
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
            className="text-content-muted"
          >
            <path d="M15 18l-6-6 6-6" />
          </svg>
          <span className="font-ui text-[length:var(--t-xs)] text-content-muted">
            {t("detail.back")}
          </span>
        </button>
      )}

      <div className="flex shrink-0 items-center justify-end border-b border-edge px-4 py-1.5">
        <Link
          to="/engine-runs"
          search={{ session_id: runId }}
          className="font-ui text-[length:var(--t-xs)] text-content-muted transition-colors duration-100 hover:text-content-primary"
        >
          {t("detail.engineRuns")} →
        </Link>
      </div>

      {/* Full run detail — same pane History renders, so Fleet selection shows
          the conversation, DAG, files, and signal events instead of bare meta. */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto">
        <RunDetail id={runId} />
      </div>
    </div>
  );
}
