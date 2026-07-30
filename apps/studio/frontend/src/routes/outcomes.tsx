import { createFileRoute, redirect } from "@tanstack/react-router";
import { preserveRetiredSearch, retiredRedirect } from "@/lib/retiredRoutes";

export const Route = createFileRoute("/outcomes")({
  validateSearch: preserveRetiredSearch,
  beforeLoad: ({ search }) => {
    throw redirect(retiredRedirect("/fleet", search));
  },
  component: () => null,
});
