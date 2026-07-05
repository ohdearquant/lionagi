import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/kanban/")({
  beforeLoad: () => {
    // Tab IA: spaces are the only pages; this list now lives as a space tab.
    throw redirect({ to: "/fleet" });
  },
  component: () => null,
});
