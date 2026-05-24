"use client";

import Link from "next/link";
import { notImplemented } from "@/lib/copy";

// H-FE-983: POST /api/playbooks/{name} returns 501. This page previously
// rendered a full canvas form whose Create button called that route. The
// form is replaced with a hold-message until the backend is implemented.
// Option A (implement the route) is tracked in the issue.

export default function NewWorkerPage() {
  return (
    <main className="mx-auto flex w-full max-w-2xl flex-col gap-6 px-4 py-12">
      <header className="flex flex-col gap-2 border-b border-edge pb-4">
        <Link href="/playbooks" className="text-meta text-content-muted hover:text-content-primary">
          &larr; playbooks
        </Link>
        <h1 className="text-xl font-semibold text-content-primary">New Playbook</h1>
      </header>

      <div className="rounded-lg border border-edge bg-surface-raised p-6 text-center">
        <p className="text-body text-content-secondary">{notImplemented.newPlaybook}</p>
        <p className="mt-3 font-mono text-meta text-content-muted">li play --help</p>
      </div>
    </main>
  );
}
