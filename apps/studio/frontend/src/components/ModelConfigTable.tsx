"use client";

import { Input, Select } from "@/components/ui/Field";
import type { ModelConfig } from "@/lib/types";

const PROVIDER_OPTIONS = ["claude_code", "codex", "gemini_code"] as const;

type Provider = (typeof PROVIDER_OPTIONS)[number];

const MODEL_OPTIONS: Record<Provider, string[]> = {
  claude_code: ["sonnet", "opus", "haiku"],
  codex: ["gpt-5.4-mini", "gpt-5.4", "o3", "o4-mini"],
  gemini_code: ["gemini-2.5-pro", "gemini-2.5-flash"],
};

const EFFORT_OPTIONS = ["none", "low", "medium", "high", "xhigh"] as const;

const PERMISSION_OPTIONS = ["default", "bypassPermissions"] as const;

export interface ModelConfigTableProps {
  models: Record<string, ModelConfig>;
  onChange: (models: Record<string, ModelConfig>) => void;
}

interface RowEntry {
  role: string;
  config: ModelConfig;
}

function toRows(models: Record<string, ModelConfig>): RowEntry[] {
  return Object.entries(models).map(([role, config]) => ({ role, config }));
}

function fromRows(rows: RowEntry[]): Record<string, ModelConfig> {
  const result: Record<string, ModelConfig> = {};
  for (const { role, config } of rows) {
    if (role.trim()) {
      result[role] = config;
    }
  }
  return result;
}

function nextDefaultRole(rows: RowEntry[]): string {
  const existing = new Set(rows.map((r) => r.role));
  let n = 1;
  while (existing.has(`role_${n}`)) {
    n++;
  }
  return `role_${n}`;
}

function isProvider(value: string): value is Provider {
  return (PROVIDER_OPTIONS as readonly string[]).includes(value);
}

export default function ModelConfigTable({ models, onChange }: ModelConfigTableProps) {
  const rows = toRows(models);

  function updateRow(index: number, updated: RowEntry) {
    const next = rows.map((row, i) => (i === index ? updated : row));
    onChange(fromRows(next));
  }

  function handleRoleChange(index: number, newRole: string) {
    const row = rows[index];
    updateRow(index, { ...row, role: newRole });
  }

  function handleProviderChange(index: number, newProvider: string) {
    if (!isProvider(newProvider)) return;
    const row = rows[index];
    const firstModel = MODEL_OPTIONS[newProvider][0];

    const updatedConfig: ModelConfig = {
      provider: newProvider,
      model: firstModel,
    };

    // Carry over effort/permission only if the new provider supports them
    if (newProvider === "codex" && row.config.reasoning_effort) {
      updatedConfig.reasoning_effort = row.config.reasoning_effort;
    }
    if (newProvider === "claude_code" && row.config.permission_mode) {
      updatedConfig.permission_mode = row.config.permission_mode;
    }

    updateRow(index, { ...row, config: updatedConfig });
  }

  function handleModelChange(index: number, newModel: string) {
    const row = rows[index];
    updateRow(index, { ...row, config: { ...row.config, model: newModel } });
  }

  function handleEffortChange(index: number, newEffort: string) {
    const row = rows[index];
    updateRow(index, {
      ...row,
      config: {
        ...row.config,
        reasoning_effort: newEffort === "none" ? undefined : newEffort,
      },
    });
  }

  function handlePermissionChange(index: number, newPermission: string) {
    const row = rows[index];
    updateRow(index, {
      ...row,
      config: {
        ...row.config,
        permission_mode: newPermission === "default" ? undefined : newPermission,
      },
    });
  }

  function handleAddRow() {
    const newRole = nextDefaultRole(rows);
    const newConfig: ModelConfig = {
      provider: "claude_code",
      model: MODEL_OPTIONS.claude_code[0],
    };
    const next = [...rows, { role: newRole, config: newConfig }];
    onChange(fromRows(next));
  }

  function handleDeleteRow(index: number) {
    const next = rows.filter((_, i) => i !== index);
    onChange(fromRows(next));
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="overflow-x-auto border border-edge">
        <table className="min-w-full border-collapse text-body">
          <thead className="border-b border-edge bg-surface-raised text-meta uppercase text-content-muted">
            <tr>
              <th scope="col" className="px-3 py-2 text-left font-medium tracking-normal">
                Role Name
              </th>
              <th scope="col" className="px-3 py-2 text-left font-medium tracking-normal">
                Provider
              </th>
              <th scope="col" className="px-3 py-2 text-left font-medium tracking-normal">
                Model
              </th>
              <th scope="col" className="px-3 py-2 text-left font-medium tracking-normal">
                Effort
              </th>
              <th scope="col" className="px-3 py-2 text-left font-medium tracking-normal">
                Permission
              </th>
              <th scope="col" className="w-12 px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {rows.map(({ role, config }, index) => {
              const provider = isProvider(config.provider) ? config.provider : "claude_code";
              const availableModels = MODEL_OPTIONS[provider];
              const showEffort = provider === "codex";
              const showPermission = provider === "claude_code";

              const rowLabel = role || `row ${index + 1}`;
              return (
                <tr key={index} className="border-b border-edge text-content-secondary">
                  {/* Role Name */}
                  <td className="px-3 py-2 align-middle">
                    <Input
                      type="text"
                      aria-label={`Role name for row ${index + 1}`}
                      value={role}
                      onChange={(e) => handleRoleChange(index, e.target.value)}
                      placeholder="role_name"
                      mono
                    />
                  </td>

                  {/* Provider */}
                  <td className="px-3 py-2 align-middle">
                    <Select
                      aria-label={`Provider for ${rowLabel}`}
                      value={provider}
                      onChange={(e) => handleProviderChange(index, e.target.value)}
                    >
                      {PROVIDER_OPTIONS.map((p) => (
                        <option key={p} value={p}>
                          {p}
                        </option>
                      ))}
                    </Select>
                  </td>

                  {/* Model */}
                  <td className="px-3 py-2 align-middle">
                    <Select
                      aria-label={`Model for ${rowLabel}`}
                      value={config.model}
                      onChange={(e) => handleModelChange(index, e.target.value)}
                    >
                      {availableModels.map((m) => (
                        <option key={m} value={m}>
                          {m}
                        </option>
                      ))}
                    </Select>
                  </td>

                  {/* Effort (codex only) */}
                  <td className="px-3 py-2 align-middle">
                    {showEffort ? (
                      <Select
                        aria-label={`Reasoning effort for ${rowLabel}`}
                        value={config.reasoning_effort ?? "none"}
                        onChange={(e) => handleEffortChange(index, e.target.value)}
                      >
                        {EFFORT_OPTIONS.map((e) => (
                          <option key={e} value={e}>
                            {e}
                          </option>
                        ))}
                      </Select>
                    ) : (
                      <span className="px-1 text-content-muted">—</span>
                    )}
                  </td>

                  {/* Permission (claude_code only) */}
                  <td className="px-3 py-2 align-middle">
                    {showPermission ? (
                      <Select
                        aria-label={`Permission mode for ${rowLabel}`}
                        value={config.permission_mode ?? "default"}
                        onChange={(e) => handlePermissionChange(index, e.target.value)}
                      >
                        {PERMISSION_OPTIONS.map((p) => (
                          <option key={p} value={p}>
                            {p}
                          </option>
                        ))}
                      </Select>
                    ) : (
                      <span className="px-1 text-content-muted">—</span>
                    )}
                  </td>

                  {/* Delete */}
                  <td className="px-3 py-2 align-middle">
                    <button
                      type="button"
                      onClick={() => handleDeleteRow(index)}
                      className="rounded border border-edge bg-surface-base px-2 py-1 text-meta text-content-muted hover:border-status-error/40 hover:bg-status-error-bg hover:text-status-error"
                      aria-label={`Remove ${role || "row"}`}
                    >
                      remove
                    </button>
                  </td>
                </tr>
              );
            })}

            {rows.length === 0 ? (
              <tr>
                <td colSpan={6} className="px-3 py-8 text-center text-body text-content-muted">
                  No model configurations. Add a role to get started.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <div>
        <button
          type="button"
          onClick={handleAddRow}
          className="rounded border border-edge bg-surface-base px-3 py-1.5 text-body text-content-secondary hover:border-edge-strong hover:text-content-primary"
        >
          + add role
        </button>
      </div>
    </div>
  );
}
