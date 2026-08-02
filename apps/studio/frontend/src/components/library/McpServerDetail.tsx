import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "use-intl";
import {
  checkMcpServer,
  deleteMcpServer,
  getMcpServer,
  registerMcpServer,
  setMcpServerEnabled,
  updateMcpServer,
  validateMcpServer,
} from "@/lib/api";
import type { McpServerConfigInput, McpServerSummary } from "@/lib/api";
import Button from "@/components/ui/Button";
import SectionLabel from "@/components/ui/SectionLabel";
import DrawerBackButton from "@/components/ui/DrawerBackButton";
import DrawerHeader from "@/components/ui/DrawerHeader";

type Transport = "stdio" | "http";

interface FormState {
  transport: Transport;
  command: string;
  args: string;
  envText: string;
  url: string;
  timeout: string;
}

function emptyForm(): FormState {
  return { transport: "stdio", command: "", args: "", envText: "", url: "", timeout: "" };
}

/** A server's env values never reach the client (see the backend's secret
 * handling), so editing pre-fills only key names as `KEY=` placeholders —
 * leaving a line as `KEY=` means "no change"; a value replaces it; deleting
 * the line entirely removes that key. */
function formFromServer(server: McpServerSummary): FormState {
  return {
    transport: server.transport,
    command: server.command ?? "",
    args: (server.args ?? []).join("\n"),
    envText: server.env_keys.map((k) => `${k}=`).join("\n"),
    url: server.url ?? "",
    timeout: server.timeout != null ? String(server.timeout) : "",
  };
}

/** Parse `KEY=value` lines into an env patch. A bare `KEY=` (no value)
 * means "leave this key alone" and is omitted. A key from `originalKeys`
 * that has no line at all in `text` was deleted by the operator — it comes
 * back as an explicit `null`, the only way the wire format can express
 * removing a key (see McpServerConfigInput). */
function parseEnvText(
  text: string,
  originalKeys: readonly string[] = [],
): Record<string, string | null> {
  const patch: Record<string, string | null> = {};
  const linesFor = new Set<string>();
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    const value = trimmed.slice(eq + 1);
    if (!key) continue;
    linesFor.add(key);
    if (value === "") continue;
    patch[key] = value;
  }
  for (const key of originalKeys) {
    if (!linesFor.has(key)) patch[key] = null;
  }
  return patch;
}

function formToConfig(
  form: FormState,
  originalEnvKeys: readonly string[] = [],
  forUpdate = false,
): McpServerConfigInput {
  const config: McpServerConfigInput = {};
  if (form.transport === "stdio") {
    config.command = form.command.trim();
    const args = form.args
      .split("\n")
      .map((a) => a.trim())
      .filter(Boolean);
    // On update, an emptied editor has to be sent as an explicit empty list.
    // The server merges a patch onto what it stored and never infers a removal
    // from an absent key, so omitting args here would silently restore the old
    // ones and the operator would watch their deletion undo itself on reload.
    // On create there is nothing to clear, so an empty list is just noise.
    if (args.length || forUpdate) config.args = args;
    const env = parseEnvText(form.envText, originalEnvKeys);
    if (Object.keys(env).length) config.env = env;
  } else {
    config.url = form.url.trim();
  }
  if (form.timeout.trim()) {
    const n = Number(form.timeout);
    if (!Number.isNaN(n)) config.timeout = n;
  } else if (forUpdate) {
    // Same reasoning as args: null is the server's removal signal.
    config.timeout = null;
  }
  return config;
}

function formatCheckedAt(epochSec: number): string {
  const diffSec = Math.max(0, Math.floor(Date.now() / 1000 - epochSec));
  if (diffSec < 60) return `${diffSec}s ago`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
  return `${Math.floor(diffSec / 86400)}d ago`;
}

function LastCheckBadge({ server }: { server: McpServerSummary }) {
  const t = useTranslations("library.mcp");
  if (!server.last_check) {
    return <span className="text-content-muted">{t("neverChecked")}</span>;
  }
  const { ok, error, checked_at } = server.last_check;
  return (
    <span className={ok ? "text-status-success" : "text-status-failure"} title={error ?? undefined}>
      {ok ? t("connectionOk") : t("connectionFailed")} · {formatCheckedAt(checked_at)}
    </span>
  );
}

// ── Shared form fields ──────────────────────────────────────────────────────

interface FormFieldsProps {
  form: FormState;
  setForm: (updater: (prev: FormState) => FormState) => void;
}

function FormFields({ form, setForm }: FormFieldsProps) {
  const t = useTranslations("library.mcp");
  return (
    <div className="flex flex-col gap-4">
      <label className="flex flex-col gap-1.5">
        <SectionLabel>{t("transport")}</SectionLabel>
        <div className="flex gap-2">
          {(["stdio", "http"] as const).map((tr) => (
            <button
              key={tr}
              type="button"
              onClick={() => setForm((prev) => ({ ...prev, transport: tr }))}
              className="rounded border px-3 py-1.5 text-[length:var(--t-sm)]"
              style={{
                borderColor: form.transport === tr ? "var(--accent)" : "var(--edge)",
                color: form.transport === tr ? "var(--accent)" : "var(--content-muted)",
              }}
            >
              {tr === "stdio" ? t("transportStdio") : t("transportHttp")}
            </button>
          ))}
        </div>
      </label>

      {form.transport === "stdio" ? (
        <>
          <label className="flex flex-col gap-1.5">
            <SectionLabel>{t("command")}</SectionLabel>
            <input
              type="text"
              value={form.command}
              onChange={(e) => setForm((prev) => ({ ...prev, command: e.target.value }))}
              placeholder={t("commandPlaceholder")}
              className="rounded border border-edge bg-surface-overlay px-3 py-2 font-data text-[length:var(--t-base)] text-content-primary focus:outline-none focus:ring-1 focus:ring-accent"
            />
          </label>
          <label className="flex flex-col gap-1.5">
            <SectionLabel>{t("args")}</SectionLabel>
            <textarea
              value={form.args}
              onChange={(e) => setForm((prev) => ({ ...prev, args: e.target.value }))}
              placeholder={t("argsPlaceholder")}
              rows={3}
              className="resize-none rounded border border-edge bg-surface-overlay px-3 py-2 font-data text-[length:var(--t-sm)] text-content-primary focus:outline-none focus:ring-1 focus:ring-accent"
            />
          </label>
          <label className="flex flex-col gap-1.5">
            <SectionLabel>{t("env")}</SectionLabel>
            <textarea
              value={form.envText}
              onChange={(e) => setForm((prev) => ({ ...prev, envText: e.target.value }))}
              placeholder={t("envPlaceholder")}
              rows={3}
              className="resize-none rounded border border-edge bg-surface-overlay px-3 py-2 font-data text-[length:var(--t-sm)] text-content-primary focus:outline-none focus:ring-1 focus:ring-accent"
            />
            <span className="text-[length:var(--t-xs)] text-content-muted">{t("envHint")}</span>
          </label>
        </>
      ) : (
        <label className="flex flex-col gap-1.5">
          <SectionLabel>{t("url")}</SectionLabel>
          <input
            type="text"
            value={form.url}
            onChange={(e) => setForm((prev) => ({ ...prev, url: e.target.value }))}
            placeholder="https://example.com/mcp"
            className="rounded border border-edge bg-surface-overlay px-3 py-2 font-data text-[length:var(--t-base)] text-content-primary focus:outline-none focus:ring-1 focus:ring-accent"
          />
        </label>
      )}

      <label className="flex flex-col gap-1.5">
        <SectionLabel>{t("timeout")}</SectionLabel>
        <input
          type="number"
          value={form.timeout}
          onChange={(e) => setForm((prev) => ({ ...prev, timeout: e.target.value }))}
          placeholder="300"
          className="w-32 rounded border border-edge bg-surface-overlay px-3 py-2 font-data text-[length:var(--t-base)] text-content-primary focus:outline-none focus:ring-1 focus:ring-accent"
        />
      </label>
    </div>
  );
}

// ── Detail (view + edit existing) ───────────────────────────────────────────

interface DetailProps {
  name: string;
  onBack?: () => void;
  onDeleted?: () => void;
}

export function McpServerDetail({ name, onBack, onDeleted }: DetailProps) {
  const t = useTranslations("library.mcp");
  const td = useTranslations("library.drawer");
  const [server, setServer] = useState<McpServerSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<FormState>(emptyForm());
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [validation, setValidation] = useState<{ ok: boolean; errors?: string[] | null } | null>(
    null,
  );

  const reload = useCallback(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    getMcpServer(name)
      .then((s) => {
        if (!alive) return;
        setServer(s);
        setForm(formFromServer(s));
        setEditing(false);
        setValidation(null);
        setSaveError(null);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "Failed to load");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [name]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reload() calls setState inside async callbacks; synchronous reset is needed to clear stale state before the fetch resolves
    return reload();
  }, [reload]);

  const handleValidate = useCallback(async () => {
    setValidation(null);
    try {
      const result = await validateMcpServer(
        name,
        formToConfig(form, server?.env_keys ?? [], true),
      );
      setValidation(result);
    } catch (e) {
      setValidation({ ok: false, errors: [e instanceof Error ? e.message : "Validation failed"] });
    }
  }, [name, form, server]);

  const handleSave = useCallback(async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const updated = await updateMcpServer(name, formToConfig(form, server?.env_keys ?? [], true));
      setServer(updated);
      setForm(formFromServer(updated));
      setEditing(false);
      setValidation(null);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }, [name, form, server]);

  const handleToggleEnabled = useCallback(async () => {
    if (!server) return;
    const updated = await setMcpServerEnabled(name, !server.enabled);
    setServer(updated);
  }, [name, server]);

  const handleCheck = useCallback(async () => {
    setChecking(true);
    try {
      const updated = await checkMcpServer(name);
      setServer(updated);
    } finally {
      setChecking(false);
    }
  }, [name]);

  const handleDelete = useCallback(async () => {
    if (!window.confirm(t("confirmDelete", { name }))) return;
    await deleteMcpServer(name);
    onDeleted?.();
  }, [name, onDeleted, t]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center text-meta text-content-muted">
        {td("loading")}
      </div>
    );
  }

  if (error || !server) {
    return <div className="p-4 text-meta text-status-failure">{error ?? td("notFound")}</div>;
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {onBack && <DrawerBackButton onClick={onBack}>{td("back")}</DrawerBackButton>}

      <DrawerHeader
        name={server.name}
        badge={server.transport}
        trailing={
          editing ? (
            <>
              <Button size="sm" variant="secondary" onClick={handleValidate}>
                {td("validate")}
              </Button>
              <Button
                size="sm"
                variant="primary"
                onClick={() => void handleSave()}
                disabled={saving}
              >
                {saving ? td("saving") : td("save")}
              </Button>
              <Button
                size="sm"
                variant="secondary"
                onClick={() => {
                  setEditing(false);
                  setForm(formFromServer(server));
                  setValidation(null);
                }}
              >
                {td("cancel")}
              </Button>
            </>
          ) : (
            <>
              <Button
                size="sm"
                variant="toggle"
                active={server.enabled}
                onClick={() => void handleToggleEnabled()}
              >
                {server.enabled ? t("enabled") : t("disabled")}
              </Button>
              <Button
                size="sm"
                variant="secondary"
                onClick={() => void handleCheck()}
                disabled={checking}
              >
                {checking ? t("checking") : t("checkConnection")}
              </Button>
              <Button size="sm" variant="secondary" onClick={() => setEditing(true)}>
                {td("edit")}
              </Button>
              <Button size="sm" variant="danger" onClick={() => void handleDelete()}>
                {t("remove")}
              </Button>
            </>
          )
        }
      />

      {saveError && (
        <div className="shrink-0 border-b border-edge px-4 py-2 text-[length:var(--t-xs)] text-status-failure">
          {saveError}
        </div>
      )}

      {validation && (
        <div
          className="shrink-0 border-b border-edge px-4 py-2 text-[length:var(--t-xs)]"
          style={{ color: validation.ok ? "var(--status-success)" : "var(--status-failure)" }}
        >
          {validation.ok ? t("validationOk") : (validation.errors ?? []).join("; ")}
        </div>
      )}

      <div className="flex-1 overflow-auto p-4">
        {editing ? (
          <FormFields form={form} setForm={setForm} />
        ) : (
          <div className="flex flex-col gap-4">
            <div className="flex flex-wrap gap-x-5 gap-y-2 text-[length:var(--t-xs)]">
              {server.command && (
                <div className="flex items-center gap-1.5">
                  <span className="text-content-muted">{t("command")}</span>
                  <span className="font-data text-content-primary">
                    {server.command} {(server.args ?? []).join(" ")}
                  </span>
                </div>
              )}
              {server.url && (
                <div className="flex items-center gap-1.5">
                  <span className="text-content-muted">{t("url")}</span>
                  <span className="font-data text-content-primary">{server.url}</span>
                </div>
              )}
              {server.timeout != null && (
                <div className="flex items-center gap-1.5">
                  <span className="text-content-muted">{t("timeout")}</span>
                  <span className="font-data text-content-primary">{server.timeout}</span>
                </div>
              )}
            </div>

            <div>
              <SectionLabel className="mb-1.5">{t("env")}</SectionLabel>
              {server.env_keys.length === 0 ? (
                <p className="text-[length:var(--t-sm)] text-content-muted">{t("noEnv")}</p>
              ) : (
                <div className="flex flex-wrap gap-1.5">
                  {server.env_keys.map((k) => (
                    <span
                      key={k}
                      className="rounded border border-edge bg-surface-overlay px-1.5 py-0.5 font-data text-[length:var(--t-xs)] text-content-secondary"
                    >
                      {k}
                    </span>
                  ))}
                </div>
              )}
              <p className="mt-1 text-[length:var(--t-xs)] italic text-content-muted">
                {t("envValuesHidden")}
              </p>
            </div>

            <div>
              <SectionLabel className="mb-1.5">{t("lastCheck")}</SectionLabel>
              <p className="text-[length:var(--t-sm)]">
                <LastCheckBadge server={server} />
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Create ───────────────────────────────────────────────────────────────────

interface CreateProps {
  onCreated: (name: string) => void;
  onCancel: () => void;
}

export function CreateMcpServerPanel({ onCreated, onCancel }: CreateProps) {
  const t = useTranslations("library.mcp");
  const td = useTranslations("library.drawer");
  const [name, setName] = useState("");
  const [form, setForm] = useState<FormState>(emptyForm());
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [validation, setValidation] = useState<{ ok: boolean; errors?: string[] | null } | null>(
    null,
  );

  const handleValidate = useCallback(async () => {
    setValidation(null);
    try {
      const result = await validateMcpServer(name.trim(), formToConfig(form));
      setValidation(result);
    } catch (e) {
      setValidation({ ok: false, errors: [e instanceof Error ? e.message : "Validation failed"] });
    }
  }, [name, form]);

  const handleCreate = useCallback(async () => {
    const trimmed = name.trim();
    if (!trimmed || creating) return;
    setCreating(true);
    setCreateError(null);
    try {
      await registerMcpServer(trimmed, formToConfig(form));
      onCreated(trimmed);
    } catch (e) {
      setCreateError(e instanceof Error ? e.message : t("createError"));
    } finally {
      setCreating(false);
    }
  }, [name, form, creating, onCreated, t]);

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex shrink-0 items-center justify-between border-b border-edge px-4 py-3">
        <span className="font-medium text-[length:var(--t-md)] text-content-primary">
          {t("createTitle")}
        </span>
        <button
          type="button"
          onClick={onCancel}
          className="text-[length:var(--t-xs)] text-content-muted"
        >
          {td("cancel")}
        </button>
      </div>

      <div className="flex flex-1 flex-col gap-4 overflow-auto p-4">
        <label className="flex flex-col gap-1.5">
          <SectionLabel>{t("createName")}</SectionLabel>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t("createNamePlaceholder")}
            className="rounded border border-edge bg-surface-overlay px-3 py-2 font-data text-[length:var(--t-base)] text-content-primary focus:outline-none focus:ring-1 focus:ring-accent"
          />
        </label>

        <FormFields form={form} setForm={setForm} />

        {validation && (
          <div
            className="text-[length:var(--t-xs)]"
            style={{ color: validation.ok ? "var(--status-success)" : "var(--status-failure)" }}
          >
            {validation.ok ? t("validationOk") : (validation.errors ?? []).join("; ")}
          </div>
        )}

        {createError && (
          <div className="text-[length:var(--t-xs)] text-status-failure">{createError}</div>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-2 border-t border-edge px-4 py-3">
        <div className="flex-1" />
        <Button size="sm" variant="secondary" onClick={() => void handleValidate()}>
          {td("validate")}
        </Button>
        <Button
          size="sm"
          variant="primary"
          onClick={() => void handleCreate()}
          disabled={!name.trim() || creating}
        >
          {creating ? t("creating") : t("create")}
        </Button>
      </div>
    </div>
  );
}
