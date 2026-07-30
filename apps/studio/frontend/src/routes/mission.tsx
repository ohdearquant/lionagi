import { createFileRoute, redirect } from "@tanstack/react-router";
import { preserveRetiredSearch, retiredRedirect } from "@/lib/retiredRoutes";

export const Route = createFileRoute("/mission")({
  validateSearch: preserveRetiredSearch,
  beforeLoad: ({ search }) => {
    throw redirect(retiredRedirect("/", search));
  },
  component: () => null,
});
