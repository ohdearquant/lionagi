"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";
import AgentProfileForm from "@/components/AgentProfileForm";
import type { AgentProfile } from "@/lib/types";
import { createAgent } from "@/lib/api";

export default function NewAgentPage() {
  const router = useRouter();
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);

  const handleSave = useCallback(
    async (data: AgentProfile) => {
      setSaving(true);
      setErrors([]);

      try {
        await createAgent(data.name, data);
        router.push(`/agents/${encodeURIComponent(data.name)}`);
      } catch (err) {
        setErrors([err instanceof Error ? err.message : "Failed to create agent"]);
        setSaving(false);
      }
    },
    [router],
  );

  return (
    <main className="mx-auto flex w-full max-w-4xl flex-col gap-4 px-4 py-6 text-content-primary">
      <header className="flex flex-col gap-2 border-b border-edge pb-4">
        <Link href="/agents" className="text-meta text-content-muted hover:text-content-primary">
          / agents
        </Link>
        <h1 className="text-xl font-semibold text-content-primary">New Agent</h1>
      </header>

      <AgentProfileForm mode="create" onSave={handleSave} saving={saving} errors={errors} />
    </main>
  );
}
