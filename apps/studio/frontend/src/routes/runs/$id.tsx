import { createFileRoute, redirect } from "@tanstack/react-router";
import { preserveRetiredSearch, retiredRedirect } from "@/lib/retiredRoutes";

export const Route = createFileRoute("/runs/$id")({
  validateSearch: preserveRetiredSearch,
  beforeLoad: ({ params, search }) => {
    // The standalone run detail page is retired; Fleet's split-pane detail
    // is the only surface now, selected via ?s=<run id>.
    throw redirect(retiredRedirect("/fleet", search, { s: params.id }));
  },
  component: () => null,
});
