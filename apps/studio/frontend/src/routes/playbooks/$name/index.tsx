import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/playbooks/$name/")({
  beforeLoad: ({ params }) => {
    throw redirect({
      to: "/library",
      search: { tab: "workflow", sel: `workflow:custom:${params.name}` },
    });
  },
  component: () => null,
});
