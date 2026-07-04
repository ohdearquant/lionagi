import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/invocations/")({
  beforeLoad: () => {
    throw redirect({ to: "/history" });
  },
  component: () => null,
});
