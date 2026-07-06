import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/playbooks/$name/")({
  beforeLoad: ({ params }) => {
    throw redirect({
      to: "/library",
      search: { tab: "playbook", sel: `playbook:custom:${params.name}` },
    });
  },
  component: () => null,
});
