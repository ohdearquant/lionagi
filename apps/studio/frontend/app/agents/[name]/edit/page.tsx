"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useCallback, useEffect, useState } from "react";
import AgentProfileForm from "@/components/AgentProfileForm";
import type { AgentProfile } from "@/lib/types";
import { getAgent, updateAgent } from "@/lib/api";

export default function EditAgentPage({ params }: { params: Promise<{ name: string }> }) {
  const { name } = use(params);
  const agentName = decodeURIComponent(name);
  const router = useRouter();
  const [initial, setInitial] = useState<AgentProfile | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);

  useEffect(() => {
    let active = true;

    async function load() {
      try {
        const data = await getAgent(agentName);
        if (active) {
          setInitial(data);
        }
      } catch (err) {
        if (active) {
          setLoadError(err instanceof Error ? err.message : "Failed to load agent");
        }
      }
    }

    void load();
    return () => {
      active = false;
    };
  }, [agentName]);

  const handleSave = useCallback(
    async (data: AgentProfile) => {
      setSaving(true);
      setErrors([]);

      try {
        await updateAgent(agentName, data);
        router.push(`/agents/${encodeURIComponent(agentName)}`);
      } catch (err) {
        setErrors([err instanceof Error ? err.message : "Failed to update agent"]);
        setSaving(false);
      }
    },
    [router, agentName],
  );

  return (
    <main className="mx-auto flex w-full max-w-4xl flex-col gap-4 px-4 py-6 text-content-primary">
      <header className="flex flex-col gap-2 border-b border-edge pb-4">
        <Link
          href={`/agents/${encodeURIComponent(agentName)}`}
          className="text-meta text-content-muted hover:text-content-primary"
        >
          / agents / {agentName}
        </Link>
        <h1 className="text-xl font-semibold text-content-primary">Edit: {agentName}</h1>
      </header>

      {loadError ? (
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {loadError}
        </div>
      ) : !initial ? (
        <div className="py-10 text-center text-body text-content-muted">Loading...</div>
      ) : (
        <AgentProfileForm
          initial={initial}
          mode="edit"
          onSave={handleSave}
          saving={saving}
          errors={errors}
        />
      )}
    </main>
  );
}
