import { createFileRoute, redirect } from "@tanstack/react-router";
import { preserveRetiredSearch, retiredRedirect } from "@/lib/retiredRoutes";

export const Route = createFileRoute("/routing")({
  validateSearch: preserveRetiredSearch,
  beforeLoad: ({ search }) => {
    throw redirect(retiredRedirect("/library", search, { tab: "workflow" }));
  },
  component: () => null,
});
