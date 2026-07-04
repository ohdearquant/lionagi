import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/playbooks/new/")({
  beforeLoad: () => {
    // Workflow creation moved to Library space (Wave 1C).
    // The Library page hosts a "New Workflow" flow with inline name + YAML editor
    // and calls POST /api/playbooks/{name} once the backend is wired.
    throw redirect({ to: "/library", search: { tab: "workflow" } });
  },
  component: () => null,
});
