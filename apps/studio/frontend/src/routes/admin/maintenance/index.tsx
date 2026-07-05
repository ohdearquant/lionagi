import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/admin/maintenance/")({
  beforeLoad: () => {
    // Tab IA: spaces are the only pages; this list now lives as a space tab.
    throw redirect({ to: "/system", search: { tab: "maintenance" } });
  },
  component: () => null,
});
