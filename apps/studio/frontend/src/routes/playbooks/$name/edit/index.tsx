import { createFileRoute, redirect } from "@tanstack/react-router";
import { preserveRetiredSearch, retiredRedirect } from "@/lib/retiredRoutes";

export const Route = createFileRoute("/playbooks/$name/edit/")({
  validateSearch: preserveRetiredSearch,
  beforeLoad: ({ params, search }) => {
    throw redirect(
      retiredRedirect("/library", search, {
        tab: "playbook",
        sel: `playbook:custom:${params.name}`,
      }),
    );
  },
  component: () => null,
});
