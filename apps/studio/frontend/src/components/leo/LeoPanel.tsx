/**
 * LeoPanel — Leo, the resident Studio operator, as a persistent right-side
 * panel. Stickable: it expands and minimizes but never unmounts, so the
 * transcript and session survive collapse and route changes. Leo streams
 * text, surfaces proposed actions the operator confirms, and drives the UI
 * through ui_command events executed client-side.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import Button from "@/components/ui/Button";
import IconButton from "@/components/ui/IconButton";
import { IconArrowRight, IconCheck } from "@/components/ui/icons";
import Markdown from "@/components/ui/Markdown";
import {
  createLeoSession,
  streamLeoMessage,
  confirmLeoAction,
  type LeoEvent,
  type LeoProposedAction,
} from "@/lib/api";
import { applyUiCommand, describeUiCommand } from "./uiCommands";

const DEFAULT_W = 380;
const MIN_EXPANDED_W = 280;
const MAX_EXPANDED_W = 700;
const WIDTH_KEY = "studio:leo-width";
const SESSION_KEY = "studio:leo-session";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface TextBubble {
  kind: "text";
  role: "user" | "assistant";
  content: string;
  streaming?: boolean;
}

interface ProposedActionBlock {
  kind: "proposed_action";
  action: LeoProposedAction;
  dismissed: boolean;
  confirming: boolean;
  confirmed: boolean;
  error?: string;
}

interface UiActionBlock {
  kind: "ui_action";
  label: string;
  applied: boolean;
}

interface ToolActivityBlock {
  kind: "tool_activity";
  tool: string;
  detail: string;
  done: boolean;
}

type Message = TextBubble | ProposedActionBlock | UiActionBlock | ToolActivityBlock;

// ---------------------------------------------------------------------------
// Marks + icons
// ---------------------------------------------------------------------------

function LeoMark({ size = 24 }: { size?: number }) {
  return (
    <span
      aria-hidden="true"
      className="flex shrink-0 items-center justify-center rounded font-data font-semibold text-accent"
      style={{
        width: size,
        height: size,
        fontSize: Math.round(size * 0.55),
        background: "color-mix(in srgb, var(--accent) 12%, transparent)",
      }}
    >
      λ
    </span>
  );
}

function IconCollapse({ size = 13 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points="6 5 13 12 6 19" />
      <polyline points="13 5 20 12 13 19" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Transcript blocks
// ---------------------------------------------------------------------------

function UserBubble({ content }: { content: string }) {
  return (
    <div className="max-w-[85%] self-end whitespace-pre-wrap break-words rounded-lg bg-surface-overlay px-3 py-2 font-ui text-[length:var(--t-base)] leading-relaxed text-content-primary">
      {content}
    </div>
  );
}

function AssistantBubble({ content, streaming }: { content: string; streaming?: boolean }) {
  return (
    <div className="leo-md max-w-[95%] self-start break-words font-ui text-[length:var(--t-base)] leading-relaxed text-content-primary">
      <Markdown>{content}</Markdown>
      {streaming && (
        <span
          aria-hidden="true"
          className="ml-0.5 inline-block h-3 w-2 animate-pulse align-text-bottom bg-content-muted"
        />
      )}
    </div>
  );
}

function UiActionChip({ block, failedText }: { block: UiActionBlock; failedText: string }) {
  return (
    <div className="flex items-center gap-1.5 self-start rounded border border-edge bg-surface-overlay px-2 py-1">
      <span aria-hidden="true" className="flex items-center text-accent">
        <IconArrowRight size={10} strokeWidth={2.25} />
      </span>
      <span className="font-data text-[length:var(--t-xs)] text-content-secondary">
        {block.label}
      </span>
      {!block.applied && (
        <span className="font-data text-[length:var(--t-xs)] text-content-muted">
          · {failedText}
        </span>
      )}
    </div>
  );
}

function ProposedActionView({
  block,
  t,
  onConfirm,
  onDismiss,
}: {
  block: ProposedActionBlock;
  t: ReturnType<typeof useTranslations<"leo">>;
  onConfirm: () => void;
  onDismiss: () => void;
}) {
  if (block.dismissed) return null;

  return (
    <div
      className="max-w-[95%] self-start rounded border px-3 py-2"
      style={{
        borderColor: "var(--accent)",
        borderLeftWidth: 3,
        background: "color-mix(in srgb, var(--accent) 8%, var(--surface-raised))",
      }}
    >
      <div className="mb-1 font-data text-[length:var(--t-xs)] uppercase tracking-[0.06em] text-accent">
        {t("proposedAction")}
      </div>
      <div className="mb-1.5 font-ui text-[length:var(--t-base)] text-content-primary">
        {block.action.description}
      </div>
      <div className="mb-2 font-data text-[length:var(--t-xs)] text-content-muted">
        {block.action.endpoint}
      </div>
      {block.error && (
        <div className="mb-2 font-ui text-[length:var(--t-sm)] text-status-failure">
          {block.error}
        </div>
      )}
      {block.confirmed ? (
        <div className="flex items-center text-status-success">
          <IconCheck size={12} strokeWidth={2.25} />
        </div>
      ) : (
        <div className="flex gap-2">
          <Button variant="primary" size="sm" onClick={onConfirm} disabled={block.confirming}>
            {block.confirming ? "…" : t("confirm")}
          </Button>
          <Button variant="ghost" size="sm" onClick={onDismiss}>
            {t("dismiss")}
          </Button>
        </div>
      )}
    </div>
  );
}

function ToolActivityView({ block }: { block: ToolActivityBlock }) {
  return (
    <div className="flex items-start gap-2 self-start">
      {!block.done ? (
        <span className="relative mt-0.5 flex h-2 w-2 shrink-0">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-75" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
        </span>
      ) : (
        <span className="mt-0.5 flex h-2 w-2 shrink-0 items-center justify-center text-status-success">
          <IconCheck size={8} strokeWidth={3} />
        </span>
      )}
      <div className="flex flex-col gap-0.5">
        <span className="font-data text-[length:var(--t-xs)] text-content-secondary">
          {block.tool}
        </span>
        {block.detail && (
          <span className="max-w-[240px] truncate font-data text-[length:var(--t-xs)] text-content-muted">
            {block.detail}
          </span>
        )}
      </div>
    </div>
  );
}

function ZeroState({
  t,
  onExample,
}: {
  t: ReturnType<typeof useTranslations<"leo">>;
  onExample: (text: string) => void;
}) {
  const examples = [t("example1"), t("example2"), t("example3")];
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-4 px-6 text-center">
      <LeoMark size={36} />
      <p className="max-w-[260px] font-ui text-[length:var(--t-base)] leading-relaxed text-content-secondary">
        {t("zeroStateTitle")}
      </p>
      <div className="flex w-full flex-col gap-1.5">
        {examples.map((ex) => (
          <button
            key={ex}
            type="button"
            onClick={() => onExample(ex)}
            className="rounded border border-edge bg-surface-base px-3 py-1.5 text-left font-ui text-[length:var(--t-sm)] text-content-secondary transition-colors duration-100 hover:border-edge-strong hover:text-content-primary"
          >
            {ex}
          </button>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

export interface LeoPanelProps {
  expanded: boolean;
  onMinimize: () => void;
}

export default function LeoPanel({ expanded, onMinimize }: LeoPanelProps) {
  const t = useTranslations("leo");
  const navigate = useNavigate();
  const [sessionId, setSessionId] = useState<string | null>(() =>
    typeof window !== "undefined" ? localStorage.getItem(SESSION_KEY) : null,
  );
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const closeStreamRef = useRef<(() => void) | null>(null);

  // Resizable width
  const [panelW, setPanelW] = useState(() => {
    const stored = typeof window !== "undefined" ? localStorage.getItem(WIDTH_KEY) : null;
    return stored ? Math.max(MIN_EXPANDED_W, Math.min(MAX_EXPANDED_W, Number(stored))) : DEFAULT_W;
  });
  const dragRef = useRef<{ startX: number; startW: number } | null>(null);

  useEffect(() => {
    function onMove(e: MouseEvent) {
      if (!dragRef.current) return;
      e.preventDefault();
      const delta = dragRef.current.startX - e.clientX;
      const next = Math.max(
        MIN_EXPANDED_W,
        Math.min(MAX_EXPANDED_W, dragRef.current.startW + delta),
      );
      setPanelW(next);
    }
    function onUp() {
      if (!dragRef.current) return;
      dragRef.current = null;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      localStorage.setItem(WIDTH_KEY, String(panelW));
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [panelW]);

  const handleDragStart = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      dragRef.current = { startX: e.clientX, startW: panelW };
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [panelW],
  );

  // Create a session on first expand
  useEffect(() => {
    if (expanded && !sessionId) {
      createLeoSession()
        .then((s) => {
          setSessionId(s.id);
          localStorage.setItem(SESSION_KEY, s.id);
        })
        .catch(() => {
          // session creation failed — will retry on next send
        });
    }
  }, [expanded, sessionId]);

  useEffect(() => {
    if (expanded) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, expanded]);

  useEffect(() => {
    if (expanded) inputRef.current?.focus();
  }, [expanded]);

  const send = useCallback(
    async (content: string) => {
      if (!content.trim() || busy) return;
      setBusy(true);

      let sid = sessionId;
      if (!sid) {
        try {
          const s = await createLeoSession();
          sid = s.id;
          setSessionId(sid);
          localStorage.setItem(SESSION_KEY, sid);
        } catch {
          setBusy(false);
          return;
        }
      }

      setMessages((prev) => [
        ...prev,
        { kind: "text", role: "user", content },
        { kind: "text", role: "assistant", content: "", streaming: true },
      ]);

      closeStreamRef.current?.();

      const close = streamLeoMessage(sid, content, (event: LeoEvent) => {
        switch (event.type) {
          case "text":
            setMessages((prev) => {
              const updated = [...prev];
              for (let i = updated.length - 1; i >= 0; i--) {
                const m = updated[i];
                if (m.kind === "text" && m.role === "assistant" && m.streaming) {
                  const line = event.content ?? "";
                  const sep = m.content ? "\n" : "";
                  updated[i] = { ...m, content: m.content + sep + line };
                  break;
                }
              }
              return updated;
            });
            break;

          case "ui_command": {
            const cmd = event.command;
            if (cmd) {
              const label = applyUiCommand(cmd, navigate);
              setMessages((prev) => [
                ...prev,
                {
                  kind: "ui_action",
                  label: label ?? describeUiCommand(cmd),
                  applied: label != null,
                },
              ]);
            }
            break;
          }

          case "tool_start":
            setMessages((prev) => [
              ...prev,
              {
                kind: "tool_activity",
                tool: event.tool ?? "tool",
                detail: event.args_summary ?? "",
                done: false,
              },
            ]);
            break;

          case "tool_done":
            setMessages((prev) => {
              const updated = [...prev];
              for (let i = updated.length - 1; i >= 0; i--) {
                const m = updated[i];
                if (m.kind === "tool_activity" && !m.done && m.tool === (event.tool ?? "tool")) {
                  updated[i] = { ...m, done: true, detail: event.preview ?? m.detail };
                  break;
                }
              }
              return updated;
            });
            break;

          case "heartbeat":
            break;

          case "proposed_action":
            if (event.action) {
              setMessages((prev) => [
                ...prev,
                {
                  kind: "proposed_action",
                  action: event.action!,
                  dismissed: false,
                  confirming: false,
                  confirmed: false,
                },
              ]);
            }
            break;

          case "error":
            setMessages((prev) => {
              const updated = [...prev];
              for (let i = updated.length - 1; i >= 0; i--) {
                const m = updated[i];
                if (m.kind === "text" && m.role === "assistant" && m.streaming) {
                  updated[i] = { ...m, content: event.detail ?? t("error"), streaming: false };
                  break;
                }
              }
              return updated;
            });
            break;

          case "done":
            setMessages((prev) => {
              const updated = [...prev];
              for (let i = updated.length - 1; i >= 0; i--) {
                const m = updated[i];
                if (m.kind === "text" && m.role === "assistant" && m.streaming) {
                  // Drop the placeholder if the turn produced no text at all
                  if (!m.content) {
                    updated.splice(i, 1);
                  } else {
                    updated[i] = { ...m, streaming: false };
                  }
                  break;
                }
              }
              return updated;
            });
            setBusy(false);
            break;
        }
      });

      closeStreamRef.current = close;
    },
    [busy, sessionId, navigate, t],
  );

  const handleSubmit = useCallback(() => {
    const content = input.trim();
    if (!content) return;
    setInput("");
    void send(content);
  }, [input, send]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.nativeEvent.isComposing) return;
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSubmit();
      }
    },
    [handleSubmit],
  );

  const handleConfirm = useCallback(async (idx: number, block: ProposedActionBlock) => {
    setMessages((prev) => {
      const updated = [...prev];
      const m = updated[idx];
      if (m.kind === "proposed_action") updated[idx] = { ...m, confirming: true };
      return updated;
    });
    try {
      await confirmLeoAction(block.action);
      setMessages((prev) => {
        const updated = [...prev];
        const m = updated[idx];
        if (m.kind === "proposed_action") {
          updated[idx] = { ...m, confirming: false, confirmed: true };
        }
        return updated;
      });
    } catch (err) {
      setMessages((prev) => {
        const updated = [...prev];
        const m = updated[idx];
        if (m.kind === "proposed_action") {
          updated[idx] = {
            ...m,
            confirming: false,
            error: err instanceof Error ? err.message : "Failed",
          };
        }
        return updated;
      });
    }
  }, []);

  const handleDismiss = useCallback((idx: number) => {
    setMessages((prev) => {
      const updated = [...prev];
      const m = updated[idx];
      if (m.kind === "proposed_action") updated[idx] = { ...m, dismissed: true };
      return updated;
    });
  }, []);

  return (
    <aside
      aria-label={t("ariaLabel")}
      className={`relative flex h-full shrink-0 flex-col overflow-hidden bg-surface-raised transition-[width] duration-150 ease-out ${
        expanded ? "border-l border-edge" : ""
      }`}
      style={{ width: expanded ? panelW : 0 }}
    >
      {expanded ? (
        <div className="flex h-full flex-col" style={{ width: panelW }}>
          {/* Resize handle */}
          <button
            type="button"
            aria-label="Resize Leo panel"
            onMouseDown={handleDragStart}
            className="absolute left-0 top-0 z-10 h-full w-1 cursor-col-resize border-none bg-transparent p-0 hover:bg-interactive-primary/30 active:bg-interactive-primary/50"
          />
          {/* Header */}
          <div className="flex shrink-0 items-center gap-2 border-b border-edge px-3 py-2.5">
            <LeoMark size={22} />
            <span className="font-ui text-[length:var(--t-base)] font-semibold text-content-primary">
              {t("name")}
            </span>
            <span className="min-w-0 flex-1 truncate font-ui text-[length:var(--t-xs)] text-content-muted">
              {busy ? (
                <span className="flex items-center gap-1.5">
                  <span className="relative flex h-1.5 w-1.5">
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-75" />
                    <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-accent" />
                  </span>
                  <span className="text-accent">{t("thinking")}</span>
                </span>
              ) : (
                t("caption")
              )}
            </span>
            <IconButton
              aria-label={t("minimize")}
              title={`${t("minimize")} — ⌘J`}
              onClick={onMinimize}
              size="sm"
            >
              <IconCollapse />
            </IconButton>
          </div>

          {/* Transcript */}
          <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-3 py-3">
            {messages.length === 0 ? (
              <ZeroState
                t={t}
                onExample={(ex) => {
                  setInput(ex);
                  inputRef.current?.focus();
                }}
              />
            ) : (
              messages.map((msg, i) => {
                if (msg.kind === "text") {
                  return msg.role === "user" ? (
                    <UserBubble key={i} content={msg.content} />
                  ) : (
                    <AssistantBubble key={i} content={msg.content} streaming={msg.streaming} />
                  );
                }
                if (msg.kind === "tool_activity") {
                  return <ToolActivityView key={i} block={msg} />;
                }
                if (msg.kind === "ui_action") {
                  return <UiActionChip key={i} block={msg} failedText={t("uiActionFailed")} />;
                }
                return (
                  <ProposedActionView
                    key={i}
                    block={msg}
                    t={t}
                    onConfirm={() => void handleConfirm(i, msg)}
                    onDismiss={() => handleDismiss(i)}
                  />
                );
              })
            )}
            <div ref={messagesEndRef} />
          </div>

          {/* Composer */}
          <div className="flex shrink-0 gap-2 border-t border-edge px-3 py-2.5">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={t("inputPlaceholder")}
              rows={2}
              disabled={busy}
              className="min-h-[34px] flex-1 resize-none rounded border border-edge bg-surface-base px-2.5 py-1.5 font-ui text-[length:var(--t-base)] leading-snug text-content-primary placeholder:text-content-muted focus:border-interactive-primary focus:outline-none"
            />
            <div className="self-end">
              <Button
                variant="primary"
                size="sm"
                onClick={handleSubmit}
                disabled={busy || !input.trim()}
              >
                {t("send")}
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </aside>
  );
}
