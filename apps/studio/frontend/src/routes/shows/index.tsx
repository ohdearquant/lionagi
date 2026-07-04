import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/shows/")({
  beforeLoad: () => {
    // Shows are subsumed into the History space.
    throw redirect({ to: "/history" });
  },
  component: () => null,
});
