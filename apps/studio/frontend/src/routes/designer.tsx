import { createFileRoute } from "@tanstack/react-router";
import { lazy, Suspense } from "react";

const WorkflowDesigner = lazy(() => import("@/components/workflow/WorkflowDesigner"));

export const Route = createFileRoute("/designer")({
  validateSearch: (search: Record<string, unknown>): { id?: string } => ({
    id: typeof search.id === "string" ? search.id : undefined,
  }),
  component: DesignerPage,
});

function DesignerPage() {
  const { id } = Route.useSearch();
  return (
    <Suspense
      fallback={
        <div className="flex h-full items-center justify-center font-data text-[length:var(--t-xs)] text-content-muted">
          Loading canvas…
        </div>
      }
    >
      <WorkflowDesigner defId={id ?? null} />
    </Suspense>
  );
}
