import { createFileRoute, redirect, isRedirect } from "@tanstack/react-router";
import { getInvocation } from "@/lib/api";

export const Route = createFileRoute("/invocations/$id")({
  beforeLoad: async ({ params }) => {
    try {
      const inv = await getInvocation(params.id);
      const firstSession = inv.sessions[0];
      if (firstSession) {
        throw redirect({
          to: "/history",
          search: { tab: "run", sel: `run:${firstSession.id}` },
        });
      }
    } catch (err) {
      if (isRedirect(err)) throw err;
      // Fetch failed — degrade gracefully.
    }
    throw redirect({ to: "/history", search: { tab: "run" } });
  },
  component: () => null,
});
