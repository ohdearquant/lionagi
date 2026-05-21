"use client";

import Link from "next/link";
import { useCallback, useState } from "react";
import AgentProfileForm from "@/components/AgentProfileForm";
import type { AgentProfile } from "@/lib/types";

export default function NewAgentPage() {
  const [errors, setErrors] = useState<string[]>([]);

  const handleSave = useCallback(
    async (_data: AgentProfile) => {
      setErrors(["Not yet available"]);
    },
    [],
  );

  return (
    <main className="mx-auto flex w-full max-w-4xl flex-col gap-4 px-4 py-6 text-content-primary">
      <header className="flex flex-col gap-2 border-b border-edge pb-4">
        <Link href="/agents" className="text-meta text-content-muted hover:text-content-primary">
          / agents
        </Link>
        <h1 className="text-xl font-semibold text-content-primary">New Agent</h1>
      </header>

      <AgentProfileForm mode="create" onSave={handleSave} saving={false} errors={errors} />
    </main>
  );
}
