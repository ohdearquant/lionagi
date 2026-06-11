import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import DeclarativePlaybookForm from "@/components/DeclarativePlaybookForm";
import GraphPlaybookEditor from "@/components/GraphPlaybookEditor";
import {
  declarativeToPayload,
  detectPlaybookFormat,
  getWorkerRaw,
  rawToDeclarative,
  updatePlaybook,
} from "@/lib/api";
import type { DeclarativePlaybookData, PlaybookFormat } from "@/lib/types";

export const Route = createFileRoute("/playbooks/$name/edit/")({
  component: EditPlaybookPage,
});

interface LoadState {
  format: PlaybookFormat;
  declarative?: DeclarativePlaybookData;
  rawText?: string;
}

function EditPlaybookPage() {
  const { name } = Route.useParams();
  const workerName = name;

  const [loaded, setLoaded] = useState<LoadState | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getWorkerRaw(workerName)
      .then((raw) => {
        if (!active) return;
        const data = (raw.data as Record<string, unknown>) ?? {};
        const format = detectPlaybookFormat(data);
        if (format === "declarative") {
          setLoaded({
            format,
            declarative: rawToDeclarative(workerName, data),
            rawText: raw.raw,
          });
        } else {
          setLoaded({ format, rawText: raw.raw });
        }
      })
      .catch((err) => {
        if (active) setLoadError(err instanceof Error ? err.message : "Failed to load");
      });
    return () => {
      active = false;
    };
  }, [workerName]);

  if (loadError) {
    return (
      <main className="mx-auto max-w-7xl px-4 py-6">
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {loadError}
        </div>
      </main>
    );
  }

  if (!loaded) {
    return (
      <main className="mx-auto max-w-7xl px-4 py-6">
        <p className="text-body text-content-muted">Loading...</p>
      </main>
    );
  }

  if (loaded.format === "graph") {
    return <GraphPlaybookEditor workerName={workerName} />;
  }

  return (
    <DeclarativeEditPage
      workerName={workerName}
      initial={loaded.declarative!}
      rawText={loaded.rawText}
    />
  );
}

function DeclarativeEditPage({
  workerName,
  initial,
  rawText,
}: {
  workerName: string;
  initial: DeclarativePlaybookData;
  rawText?: string;
}) {
  const navigate = useNavigate();
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [showRaw, setShowRaw] = useState(false);

  const handleSave = useCallback(
    async (data: DeclarativePlaybookData) => {
      setSaving(true);
      setErrors([]);
      try {
        await updatePlaybook(workerName, declarativeToPayload(data));
        await navigate({
          to: "/playbooks/$name",
          params: { name: workerName },
        });
      } catch (err) {
        setErrors([err instanceof Error ? err.message : "Save failed"]);
        setSaving(false);
      }
    },
    [navigate, workerName],
  );

  return (
    <main className="mx-auto flex w-full max-w-4xl flex-col gap-4 px-4 py-6 text-content-primary">
      <header className="flex flex-col gap-2 border-b border-edge pb-4">
        <Link
          to="/playbooks/$name"
          params={{ name: workerName }}
          className="text-meta text-content-muted hover:text-content-primary"
        >
          / playbooks / {workerName}
        </Link>
        <div className="flex items-end justify-between">
          <h1 className="text-xl font-semibold text-content-primary">Edit: {workerName}</h1>
          <span className="rounded border border-edge bg-surface-raised px-2 py-0.5 text-meta uppercase tracking-[0.06em] text-content-muted">
            declarative
          </span>
        </div>
        <p className="text-meta text-content-muted">
          Single-agent playbook with a prompt template and CLI args. To convert this into a
          multi-step DAG, open the file directly and add a{" "}
          <code className="rounded bg-surface-overlay px-1 font-mono">steps:</code> key.
        </p>
      </header>

      <DeclarativePlaybookForm
        initial={initial}
        onSave={handleSave}
        saving={saving}
        errors={errors}
      />

      {rawText ? (
        <section className="mt-2 flex flex-col gap-2">
          <button
            type="button"
            onClick={() => setShowRaw((v) => !v)}
            className="self-start text-meta text-content-muted hover:text-content-primary"
          >
            {showRaw ? "▾" : "▸"} View raw YAML on disk
          </button>
          {showRaw ? (
            <pre className="overflow-x-auto rounded border border-edge bg-surface-base px-3 py-2 font-mono text-meta text-content-secondary">
              {rawText}
            </pre>
          ) : null}
        </section>
      ) : null}
    </main>
  );
}
