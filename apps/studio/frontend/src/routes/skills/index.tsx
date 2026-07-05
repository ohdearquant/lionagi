import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/skills/")({
  beforeLoad: () => {
    // Tab IA: spaces are the only pages; this list now lives as a space tab.
    throw redirect({ to: "/library", search: { tab: "skill" } });
  },
  component: () => null,
});
