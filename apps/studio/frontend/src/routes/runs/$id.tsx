import { createFileRoute, Link } from "@tanstack/react-router";
import { IconArrowLeft } from "@/components/ui/icons";
import RunDetail from "@/components/history/RunDetail";

export const Route = createFileRoute("/runs/$id")({
  component: RunDetailPage,
});

function RunDetailPage() {
  const { id } = Route.useParams();
  return (
    <div className="flex min-h-screen w-full flex-col bg-surface-base text-content-primary animate-page-enter">
      <header className="sticky top-11 z-30 flex items-center gap-3 border-b border-edge bg-surface-base px-3 py-1.5 xl:px-4">
        <Link
          to="/history"
          search={{ tab: "run" }}
          className="inline-flex shrink-0 items-center gap-1 text-sm text-content-secondary hover:text-content-primary"
        >
          <IconArrowLeft size={11} strokeWidth={2} /> history
        </Link>
        <span className="text-content-muted">/</span>
        <span className="min-w-0 flex-1 truncate font-mono text-base font-semibold text-content-primary">
          {id}
        </span>
      </header>
      <div className="flex flex-1 flex-col px-3 py-3 xl:px-4">
        <RunDetail id={id} fullPage />
      </div>
    </div>
  );
}
