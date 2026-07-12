"use client";

import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { getRunFile } from "@/lib/api";
import { looksLikeFilename, resolveFileRef } from "@/lib/fileRefs";
import { IconFile, IconWarning } from "./icons";
import Modal from "./Modal";

export interface FileResolutionContext {
  /** Run this message belongs to — used to fetch content on click. */
  runId: string;
  /** Absolute paths known to belong to this run (the step's own "top files",
   * derived from tool-call args) — the ONLY source of truth for what a
   * reference is allowed to resolve to. Never fabricated from text. */
  knownFiles: string[];
  /** The emitting agent's own artifact subdir, checked first on ambiguity. */
  agentDir?: string | null;
}

export interface MarkdownProps {
  children: string;
  className?: string;
  /** Enables file-reference resolution (markdown links + bare filenames)
   * into clickable, in-Studio file-viewer links. Omit to render plain
   * markdown with no file-link behavior (existing callers unaffected). */
  fileContext?: FileResolutionContext;
}

function FileRefLink({
  label,
  path,
  candidates,
  onOpen,
}: {
  label: string;
  path?: string;
  candidates?: string[];
  onOpen: (path: string) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);

  if (path) {
    return (
      <button
        type="button"
        onClick={() => onOpen(path)}
        className="inline-flex items-center gap-0.5 rounded bg-surface-overlay px-1 font-mono text-[0.9em] text-status-running underline decoration-dotted hover:text-status-running/80"
        title={path}
      >
        <IconFile size={11} strokeWidth={2} />
        {label}
      </button>
    );
  }

  return (
    <span className="relative inline-block">
      <button
        type="button"
        onClick={() => setMenuOpen((v) => !v)}
        className="inline-flex items-center gap-0.5 rounded bg-surface-overlay px-1 font-mono text-[0.9em] text-status-running underline decoration-dotted decoration-wavy hover:text-status-running/80"
        title="Multiple files match — choose one"
      >
        <IconFile size={11} strokeWidth={2} />
        {label}
      </button>
      {menuOpen && (
        <div className="absolute left-0 top-full z-20 mt-1 min-w-max rounded border border-edge bg-surface-raised p-1 shadow-card">
          {(candidates ?? []).map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => {
                setMenuOpen(false);
                onOpen(c);
              }}
              className="block w-full truncate rounded px-2 py-1 text-left font-mono text-meta text-content-secondary hover:bg-surface-overlay"
              title={c}
            >
              {c}
            </button>
          ))}
        </div>
      )}
    </span>
  );
}

function FileRef({
  raw,
  label,
  fallback,
  fileContext,
  onOpen,
}: {
  raw: string;
  label: string;
  fallback: ReactNode;
  fileContext: FileResolutionContext;
  onOpen: (path: string) => void;
}) {
  const match = resolveFileRef(raw, {
    knownFiles: fileContext.knownFiles,
    agentDir: fileContext.agentDir,
  });
  if (match.type === "single")
    return <FileRefLink label={label} path={match.path} onOpen={onOpen} />;
  if (match.type === "ambiguous")
    return <FileRefLink label={label} candidates={match.candidates} onOpen={onOpen} />;
  return <>{fallback}</>;
}

function FileViewerModal({
  runId,
  path,
  onClose,
}: {
  runId: string;
  path: string;
  onClose: () => void;
}) {
  const [state, setState] = useState<
    | { status: "loading" }
    | { status: "ready"; content: string; truncated: boolean }
    | { status: "missing" }
    | { status: "error"; detail?: string }
  >({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    getRunFile(runId, path)
      .then((result) => {
        if (cancelled) return;
        if (!result.ok) {
          if (result.status === 404) setState({ status: "missing" });
          else setState({ status: "error", detail: result.detail });
          return;
        }
        setState({
          status: "ready",
          content: result.data.content,
          truncated: result.data.truncated,
        });
      })
      .catch((err) => {
        // getRunFile rethrows on network failure (fetch reject) rather than
        // resolving an { ok: false } shape — without this the modal is stuck
        // in "loading" forever on a dropped connection.
        if (cancelled) return;
        setState({ status: "error", detail: err instanceof Error ? err.message : undefined });
      });
    return () => {
      cancelled = true;
    };
  }, [runId, path]);

  return (
    <Modal
      title={path.split("/").pop() ?? path}
      closeLabel="Close file viewer"
      onClose={onClose}
      maxWidth="max-w-2xl"
    >
      <div className="max-h-[70vh] overflow-auto p-4">
        {state.status === "loading" && <p className="text-body text-content-muted">Loading…</p>}
        {state.status === "missing" && (
          <p className="flex items-center gap-1.5 text-body text-content-muted">
            <IconWarning size={12} strokeWidth={2} />
            File not found — it may have been moved or deleted since this message was written.
          </p>
        )}
        {state.status === "error" && (
          <p className="flex items-center gap-1.5 text-body text-status-error">
            <IconWarning size={12} strokeWidth={2} />
            {state.detail ?? "Could not load this file."}
          </p>
        )}
        {state.status === "ready" && (
          <>
            <pre className="whitespace-pre-wrap break-words font-mono text-[length:var(--t-xs)] leading-relaxed text-content-secondary">
              {state.content}
            </pre>
            {state.truncated && (
              <p className="mt-2 text-meta text-content-muted">
                File truncated — showing the first portion only.
              </p>
            )}
          </>
        )}
      </div>
    </Modal>
  );
}

export default function Markdown({ children, className, fileContext }: MarkdownProps) {
  const [openPath, setOpenPath] = useState<string | null>(null);

  const components: Components | undefined = fileContext
    ? {
        a: (props) => {
          const { href, children: linkChildren } = props;
          const label = typeof linkChildren === "string" ? linkChildren : (href ?? "");
          if (!href || /^(https?:|mailto:)/i.test(href)) {
            return (
              <a href={href} target="_blank" rel="noreferrer noopener">
                {linkChildren}
              </a>
            );
          }
          return (
            <FileRef
              raw={href}
              label={label}
              fallback={
                <a href={href} target="_blank" rel="noreferrer noopener">
                  {linkChildren}
                </a>
              }
              fileContext={fileContext}
              onOpen={setOpenPath}
            />
          );
        },
        code: (props) => {
          const { className: codeClassName, children: codeChildren, ...rest } = props;
          const text = typeof codeChildren === "string" ? codeChildren : String(codeChildren ?? "");
          // Fenced code blocks carry a `language-*` className; only bare
          // inline spans (no className) are candidate filenames.
          if (!codeClassName && looksLikeFilename(text)) {
            return (
              <FileRef
                raw={text}
                label={text}
                fallback={
                  <code className={codeClassName} {...rest}>
                    {codeChildren}
                  </code>
                }
                fileContext={fileContext}
                onOpen={setOpenPath}
              />
            );
          }
          return (
            <code className={codeClassName} {...rest}>
              {codeChildren}
            </code>
          );
        },
      }
    : undefined;

  return (
    <div className={["markdown-body", className].filter(Boolean).join(" ")}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children}
      </ReactMarkdown>
      {fileContext && openPath && (
        <FileViewerModal
          runId={fileContext.runId}
          path={openPath}
          onClose={() => setOpenPath(null)}
        />
      )}
    </div>
  );
}
