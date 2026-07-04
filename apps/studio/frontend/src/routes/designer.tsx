import { createFileRoute } from "@tanstack/react-router";
import { lazy, Suspense } from "react";

const DesignerCanvas = lazy(() => import("@/components/designer/DesignerCanvas"));

export const Route = createFileRoute("/designer")({
  validateSearch: (search: Record<string, unknown>): { id?: string; kind?: string } => ({
    id: typeof search.id === "string" ? search.id : undefined,
    kind: typeof search.kind === "string" ? search.kind : undefined,
  }),
  component: DesignerPage,
});

function DesignerPage() {
  const { id, kind } = Route.useSearch();
  return (
    <Suspense
      fallback={
        <div className="flex h-full items-center justify-center font-data text-[length:var(--t-xs)] text-content-muted">
          Loading canvas…
        </div>
      }
    >
      <DesignerCanvas editDefId={id ?? null} initialKind={kind ?? null} />
    </Suspense>
  );
}
