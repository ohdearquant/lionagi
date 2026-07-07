import { createFileRoute, redirect } from "@tanstack/react-router";
import RetiredRouteError from "@/components/routing/RetiredRouteError";
import { preserveRetiredSearch, retiredInvocationRedirect } from "@/lib/retiredRoutes";

export const Route = createFileRoute("/invocations/$id")({
  validateSearch: preserveRetiredSearch,
  beforeLoad: async ({ params, search }) => {
    // Resolves which child session to land on in Fleet; a failed fetch is
    // left to reject so errorComponent shows the real detail instead of a
    // silent bounce to /fleet.
    throw redirect(await retiredInvocationRedirect(params.id, search));
  },
  errorComponent: ({ error }) => <RetiredRouteError error={error} fallbackTo="/fleet" />,
  component: () => null,
});
