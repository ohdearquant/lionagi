import { useTranslations } from "use-intl";
import RunDetail from "@/components/history/RunDetail";

interface Props {
  runId: string | null;
  onBack?: () => void;
  showBack?: boolean;
}

export default function SessionDetail({ runId, onBack, showBack = false }: Props) {
  const t = useTranslations("fleet");

  if (!runId) {
    return (
      <div className="flex h-full items-center justify-center">
        <span className="font-data text-[length:var(--t-sm)] text-content-muted">
          {t("detail.hint")}
        </span>
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

      {/* Full run detail — same pane History renders, so Fleet selection shows
          the conversation, DAG, files, and signal events instead of bare meta. */}
      <div className="flex-1 overflow-y-auto">
        <RunDetail id={runId} fullPage />
      </div>
    </div>
  );
}
