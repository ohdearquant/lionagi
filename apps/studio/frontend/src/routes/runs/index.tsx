import { createFileRoute, redirect } from "@tanstack/react-router";
import { preserveRetiredSearch, retiredRedirect } from "@/lib/retiredRoutes";

export const Route = createFileRoute("/runs/")({
  validateSearch: preserveRetiredSearch,
  beforeLoad: ({ search }) => {
    // Tab IA: spaces are the only pages; this list now lives as a space tab.
    throw redirect(retiredRedirect("/fleet", search));
  },
  component: () => null,
});
