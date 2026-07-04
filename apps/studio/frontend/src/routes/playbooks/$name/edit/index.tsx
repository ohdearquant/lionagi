import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/playbooks/$name/edit/")({
  beforeLoad: ({ params }) => {
    throw redirect({ to: "/library", search: { tab: "workflow", sel: `workflow:${params.name}` } });
  },
  component: () => null,
});
