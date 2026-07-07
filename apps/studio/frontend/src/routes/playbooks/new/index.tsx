import { createFileRoute, redirect } from "@tanstack/react-router";
import { preserveRetiredSearch, retiredRedirect } from "@/lib/retiredRoutes";

export const Route = createFileRoute("/playbooks/new/")({
  validateSearch: preserveRetiredSearch,
  beforeLoad: ({ search }) => {
    // Workflow creation moved to Library space.
    // The Library page hosts a "New Workflow" flow with inline name + YAML editor
    // and calls POST /api/playbooks/{name} once the backend is wired.
    throw redirect(retiredRedirect("/library", search, { tab: "workflow" }));
  },
  component: () => null,
});
