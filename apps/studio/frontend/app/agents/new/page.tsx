"use client";

import Link from "next/link";
import { notImplemented } from "@/lib/copy";

// H-FE-983: POST /api/agents/{name} returns 501. This page previously
// rendered an AgentProfileForm whose Create Agent button called that route.
// The form is replaced with a hold-message until the backend is implemented.
// Option A (implement the route) is tracked in the issue.

export default function NewAgentPage() {
  return (
    <main className="mx-auto flex w-full max-w-2xl flex-col gap-6 px-4 py-12">
      <header className="flex flex-col gap-2 border-b border-edge pb-4">
        <Link href="/agents" className="text-meta text-content-muted hover:text-content-primary">
          &larr; agents
        </Link>
        <h1 className="text-xl font-semibold text-content-primary">New Agent</h1>
      </header>

      <div className="rounded-lg border border-edge bg-surface-raised p-6 text-center">
        <p className="text-body text-content-secondary">{notImplemented.newAgent}</p>
        <p className="mt-3 font-mono text-meta text-content-muted">li agent --help</p>
      </div>
    </main>
  );
}
