"use client";

import { useCallback, useState } from "react";
import AgentProfileForm from "@/components/AgentProfileForm";
import { createAgent } from "@/lib/api";
import type { AgentProfile } from "@/lib/types";

interface CreateAgentPanelProps {
  onCreated: (name: string) => void;
  onCancel: () => void;
}

export function CreateAgentPanel({ onCreated, onCancel }: CreateAgentPanelProps) {
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);

  const handleSave = useCallback(
    async (data: AgentProfile) => {
      const name = data.name.trim();
      if (!name) {
        setErrors(["Name is required"]);
        return;
      }
      setSaving(true);
      setErrors([]);
      try {
        await createAgent(name, data);
        onCreated(name);
      } catch (e) {
        setErrors([e instanceof Error ? e.message : "Failed to create agent"]);
      } finally {
        setSaving(false);
      }
    },
    [onCreated],
  );

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex shrink-0 items-center justify-between border-b border-edge px-4 py-3">
        <span className="font-medium text-[length:var(--t-md)] text-content-primary">
          New agent
        </span>
        <button
          type="button"
          onClick={onCancel}
          className="text-[length:var(--t-xs)] text-content-muted"
        >
          Cancel
        </button>
      </div>
      <div className="flex-1 overflow-auto p-4">
        <AgentProfileForm mode="create" onSave={handleSave} saving={saving} errors={errors} />
      </div>
    </div>
  );
}
