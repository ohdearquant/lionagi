/* eslint-disable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex -- the WAI-ARIA separator is an interactive window splitter */
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";
import { Link, useLocation, useNavigate, useSearch } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import {
  ApiError,
  acknowledgeOperatorEffect,
  cancelOperatorRequest,
  reportOperatorView,
  createOperatorConversation,
  decideOperatorProposal,
  fetchOperatorModelCatalog,
  forkOperatorConversation,
  getOperatorConversation,
  listOperatorConversations,
  streamOperatorConversation,
  submitOperatorTurn,
  updateOperatorConversation,
} from "@/lib/api";
import type {
  OperatorContextSnapshot,
  OperatorConfirmationPayload,
  OperatorConversation,
  OperatorDonePayload,
  OperatorEffort,
  OperatorErrorPayload,
  OperatorFrame,
  OperatorModelCatalogEntry,
  OperatorProposalPayload,
  OperatorProvider,
  OperatorTextPayload,
  OperatorToolCallPayload,
  OperatorToolResultPayload,
  OperatorTruncationPayload,
  OperatorUiCommandPayload,
} from "@/lib/types";
import Button from "@/components/ui/Button";
import Markdown from "@/components/ui/Markdown";
import {
  IconArrowRight,
  IconBan,
  IconCheck,
  IconChevronDown,
  IconClose,
  IconCopy,
  IconError,
  IconLaunch,
  IconPause,
  IconShield,
  IconTool,
} from "@/components/ui/icons";
import { initialOperatorState, operatorReducer, pendingOperatorProposals } from "./operatorReducer";
import {
  effectAcknowledgementStorageAvailable,
  effectPlanRoute,
  operatorEffectId,
  planOperatorEffect,
  readEffectAcknowledgements,
  rememberEffectAcknowledgement,
  type StoredEffectAcknowledgement,
} from "./operatorEffects";
import { nextObservationSeq, observationObserver } from "./observationSequence";
import { useOperatorProject } from "./OperatorProjectContext";
import { applyTheme } from "@/lib/theme";

const STORAGE_KEY = "studio:operator-conversation";
const AUTO_ALLOW_KEY = "studio:operator-auto-allow";
const DEFAULT_WIDTH = 408;
const MIN_WIDTH = 320;
const MAX_WIDTH = 640;

function recordConversationMetadataCall(
  pending: Map<string, Set<string>>,
  frame: OperatorFrame,
): boolean {
  const payload = frame.payload as { tool?: unknown; callId?: unknown; ok?: unknown };
  if (
    frame.type === "tool_call" &&
    payload.tool === "mcp__studio_operator__update_conversation" &&
    typeof payload.callId === "string" &&
    payload.callId.length > 0
  ) {
    const calls = pending.get(frame.requestId) ?? new Set<string>();
    calls.add(payload.callId);
    pending.set(frame.requestId, calls);
  } else if (frame.type === "tool_result" && typeof payload.callId === "string") {
    const calls = pending.get(frame.requestId);
    const matched = calls?.delete(payload.callId) ?? false;
    if (calls?.size === 0) pending.delete(frame.requestId);
    return matched && payload.ok === true;
  } else if (frame.type === "done") {
    const missingResult = (pending.get(frame.requestId)?.size ?? 0) > 0;
    pending.delete(frame.requestId);
    return missingResult;
  }
  return false;
}

const OPERATOR_PROVIDER_ORDER: OperatorProvider[] = ["claude_code", "codex", "gemini_code"];

const OPERATOR_PROVIDER_LABELS: Record<OperatorProvider, string> = {
  claude_code: "Claude",
  codex: "Codex",
  gemini_code: "Gemini",
};

function groupModelsByProvider(
  catalog: OperatorModelCatalogEntry[],
): { provider: OperatorProvider; models: OperatorModelCatalogEntry[] }[] {
  return OPERATOR_PROVIDER_ORDER.map((provider) => ({
    provider,
    models: catalog.filter((entry) => entry.provider === provider),
  })).filter((group) => group.models.length > 0);
}

// What the alert shows. A decision's error names its proposal, so a later decision of
// that proposal that succeeds clears it and leaves any other error in place.
type ActionError = { message: string; proposalId?: string };

interface Props {
  open: boolean;
  onClose: () => void;
}

type DisplayItem =
  | {
      kind: "text";
      key: string;
      requestId: string;
      sequence: number;
      role: "user" | "assistant";
      format: "plain" | "markdown";
      content: string;
    }
  | { kind: "frame"; key: string; frame: OperatorFrame };

function readStoredConversation(): string | null {
  if (typeof window === "undefined") return null;
  const value = window.localStorage.getItem(STORAGE_KEY)?.trim();
  return value || null;
}

// A rename, pin or archive this panel wrote while a read was pending must not be
// undone by the read's older snapshot of that row. The server stamps updatedAt on
// every write, so a snapshot stamped later already holds the write. Otherwise the
// write's result is the newer row: its description, which the Operator can change,
// is taken with the fields this panel writes.
function keepWrite(row: OperatorConversation, written: OperatorConversation): OperatorConversation {
  const snapshotIsNewer =
    row.updatedAt !== undefined &&
    written.updatedAt !== undefined &&
    row.updatedAt > written.updatedAt;
  if (snapshotIsNewer) return row;
  return {
    ...row,
    title: written.title,
    description: written.description,
    pinned: written.pinned,
    status: written.status,
    updatedAt: written.updatedAt,
  };
}

function byPinThenRecency(a: OperatorConversation, b: OperatorConversation): number {
  return Number(b.pinned) - Number(a.pinned) || (b.updatedAt ?? 0) - (a.updatedAt ?? 0);
}

function conversationLabel(conversation: OperatorConversation): string {
  const title = conversation.title?.trim();
  if (title) return title;
  const timestamp = conversation.updatedAt ?? conversation.createdAt;
  if (typeof timestamp === "number" && Number.isFinite(timestamp)) {
    const milliseconds = timestamp < 10_000_000_000 ? timestamp * 1000 : timestamp;
    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    }).format(milliseconds);
  }
  return conversation.id.slice(0, 8);
}

function formatRetentionBytes(bytes: number): string {
  const amount = (value: number): string =>
    Number.isInteger(value) ? String(value) : value.toFixed(1);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${amount(bytes / 1024)} KB`;
  return `${amount(bytes / (1024 * 1024))} MB`;
}

function displayItems(frames: OperatorFrame[]): DisplayItem[] {
  const items: DisplayItem[] = [];
  for (const frame of frames) {
    if (frame.type !== "text") {
      items.push({ kind: "frame", key: `frame-${frame.sequence}`, frame });
      continue;
    }
    const payload = frame.payload as OperatorTextPayload;
    const role = payload.role === "user" ? "user" : "assistant";
    const previous = items.at(-1);
    if (
      previous?.kind === "text" &&
      previous.requestId === frame.requestId &&
      previous.role === role &&
      previous.format === payload.format
    ) {
      previous.content += payload.content;
      continue;
    }
    items.push({
      kind: "text",
      key: `text-${frame.sequence}`,
      requestId: frame.requestId,
      sequence: frame.sequence,
      role,
      format: payload.format,
      content: payload.content,
    });
  }
  return items;
}

function findRunId(value: unknown, depth = 0): string | null {
  if (depth > 4 || value == null || typeof value !== "object") return null;
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findRunId(item, depth + 1);
      if (found) return found;
    }
    return null;
  }
  const record = value as Record<string, unknown>;
  for (const key of ["sessionId", "session_id", "runId", "run_id"]) {
    if (typeof record[key] === "string" && record[key]) return record[key];
  }
  for (const nested of Object.values(record)) {
    const found = findRunId(nested, depth + 1);
    if (found) return found;
  }
  return null;
}

const SENSITIVE_COMMAND_KEY = /(?:authorization|credential|password|secret|token|api[_-]?key)/i;

const MAX_RENDERED_COMMAND_CHARS = 6_000;

export interface FormattedProposalCommand {
  /** The string drawn on screen — not necessarily the whole command. */
  text: string;
  /** True when the text is a lossy view: depth cut, array cut, length cut, or redaction. */
  elided: boolean;
  /** Characters dropped by the length cut. Zero when the length cut did not fire. */
  droppedCharacters: number;
}

function redactCommandValue(value: unknown, depth: number, elided: { hit: boolean }): unknown {
  if (depth >= 7) {
    elided.hit = true;
    return "[truncated]";
  }
  if (Array.isArray(value)) {
    const result = value.slice(0, 50).map((item) => redactCommandValue(item, depth + 1, elided));
    if (value.length > 50) {
      elided.hit = true;
      result.push(`[${value.length - 50} more items]`);
    }
    return result;
  }
  if (value == null || typeof value !== "object") return value;
  const redacted: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (SENSITIVE_COMMAND_KEY.test(key)) {
      elided.hit = true;
      redacted[key] = "[redacted]";
    } else {
      redacted[key] = redactCommandValue(item, depth + 1, elided);
    }
  }
  return redacted;
}

/**
 * Render a proposal command for review, reporting whether the rendering is lossy.
 *
 * The caller must surface `elided` outside any collapsed disclosure: the operator
 * approves the whole command, so "what you are reading is smaller than what runs"
 * has to be visible in the same glance as the Allow button.
 */
export function formatProposalCommand(command: Record<string, unknown>): FormattedProposalCommand {
  const elided = { hit: false };
  const formatted = JSON.stringify(redactCommandValue(command, 0, elided), null, 2);
  if (formatted.length > MAX_RENDERED_COMMAND_CHARS) {
    return {
      text: `${formatted.slice(0, MAX_RENDERED_COMMAND_CHARS)}\n…`,
      elided: true,
      droppedCharacters: formatted.length - MAX_RENDERED_COMMAND_CHARS,
    };
  }
  return { text: formatted, elided: elided.hit, droppedCharacters: 0 };
}

// Numbered rather than timed, and stamped with who did the observing. See
// ./observationSequence for why neither a clock nor another page's count can
// order these.
function operatorContext(
  pathname: string,
  search: Record<string, unknown>,
  project: string | null,
): OperatorContextSnapshot & { observationSeq: number; observerId: string } {
  let space: OperatorContextSnapshot["space"] = "mission";
  if (pathname.startsWith("/library")) space = "library";
  else if (pathname.startsWith("/schedules")) space = "schedules";
  else if (pathname.startsWith("/system")) space = "system";
  else if (pathname.startsWith("/designer")) space = "designer";
  // /fleet IS the history space (navigate's enum promises as much); without
  // this arm a navigate(space="history") lands on a snapshot that calls
  // itself "mission", two vocabularies for one surface.
  else if (pathname.startsWith("/history") || pathname.startsWith("/fleet")) space = "history";

  const filters: OperatorContextSnapshot["filters"] = {};
  const selection: Record<string, string> = {};
  for (const [key, value] of Object.entries(search)) {
    if (
      value == null ||
      (!Array.isArray(value) &&
        typeof value !== "string" &&
        typeof value !== "number" &&
        typeof value !== "boolean")
    ) {
      continue;
    }
    filters[key] = value as OperatorContextSnapshot["filters"][string];
    if ((key === "s" || key === "sel") && typeof value === "string") selection[key] = value;
  }
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (Array.isArray(value)) {
      for (const item of value) query.append(key, String(item));
    } else if (value != null && typeof value !== "object") {
      query.set(key, String(value));
    }
  }
  const queryString = query.toString();
  return {
    space,
    ...(project ? { project } : {}),
    route: `${pathname}${queryString ? `?${queryString}` : ""}`,
    selection: Object.keys(selection).length ? selection : null,
    filters,
    observationSeq: nextObservationSeq(),
    observerId: observationObserver(),
  };
}

function ConnectionBadge({
  state,
}: {
  state: "idle" | "connecting" | "open" | "reconnecting" | "error";
}) {
  const t = useTranslations("operator");
  const label = t(`connection.${state}` as Parameters<typeof t>[0]);
  const color =
    state === "open"
      ? "bg-status-success"
      : state === "error"
        ? "bg-status-failure"
        : state === "idle"
          ? "bg-content-muted"
          : "bg-status-pending";
  return (
    <span
      className="inline-flex min-w-0 items-center gap-1.5 font-data text-[length:var(--t-xs)] text-content-muted"
      title={label}
    >
      <span aria-hidden className={`h-1.5 w-1.5 shrink-0 rounded-full ${color}`} />
      <span className="truncate">{label}</span>
    </span>
  );
}

function RunLink({ runId }: { runId: string }) {
  const t = useTranslations("operator");
  return (
    <Link
      to="/fleet"
      search={{ s: runId }}
      className="focus-ring mt-2 inline-flex h-8 items-center gap-1.5 rounded border border-edge bg-surface-overlay px-2.5 text-body font-medium text-content-primary transition-colors hover:border-edge-strong"
    >
      <IconLaunch size={13} />
      <span>{t("run.open")}</span>
      <span className="max-w-32 truncate font-data text-meta text-content-muted">{runId}</span>
      <IconArrowRight size={12} />
    </Link>
  );
}

function CopyButton({ value }: { value: string }) {
  const t = useTranslations("operator");
  const [copied, setCopied] = useState(false);

  const copy = useCallback(() => {
    // Older/insecure contexts have no clipboard API; stay silent rather than
    // flashing a success the copy never performed.
    const clipboard = navigator.clipboard;
    if (!clipboard?.writeText) return;
    void clipboard.writeText(value).then(
      () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      },
      () => setCopied(false),
    );
  }, [value]);

  return (
    <button
      type="button"
      onClick={copy}
      aria-label={t("message.copy")}
      title={copied ? t("message.copied") : t("message.copy")}
      className="focus-ring flex h-6 w-6 shrink-0 items-center justify-center rounded text-content-muted opacity-0 transition-opacity hover:bg-surface-overlay hover:text-content-primary focus-visible:opacity-100 group-hover:opacity-100"
    >
      {copied ? <IconCheck size={12} className="text-status-success" /> : <IconCopy size={12} />}
    </button>
  );
}

function TextMessage({ item }: { item: Extract<DisplayItem, { kind: "text" }> }) {
  const t = useTranslations("operator");
  const user = item.role === "user";
  return (
    <article
      aria-label={user ? t("message.you") : t("message.operator")}
      className={`group flex items-start gap-1 ${user ? "justify-end" : "justify-start"}`}
    >
      {user && <CopyButton value={item.content} />}
      <div
        className={
          user
            ? "max-w-[88%] rounded-lg border border-edge bg-surface-overlay px-3 py-2 text-body text-content-primary"
            : "min-w-0 max-w-full text-body leading-relaxed text-content-secondary"
        }
      >
        {item.format === "markdown" && !user ? (
          <Markdown>{item.content}</Markdown>
        ) : (
          <p className="whitespace-pre-wrap break-words">{item.content}</p>
        )}
      </div>
      {!user && <CopyButton value={item.content} />}
    </article>
  );
}

/** `mcp__studio_operator__run_stats` reads as `run_stats`; the namespace is
 * constant across every row and spends the width that the name needs. */
function shortToolName(tool: string): string {
  const parts = tool.split("__").filter(Boolean);
  return parts.length > 1 ? parts[parts.length - 1] : tool;
}

/** One line of what the call actually asked for, so a collapsed row is not
 * just a name. Values only: the keys repeat and the width is scarce. A call
 * that describes itself (Bash carries `description`) leads with that
 * sentence — "what is this doing" reads better than raw argv on a narrow
 * row, and the argv would otherwise truncate the description clean away. */
function argumentSummary(args: unknown): string {
  if (!args || typeof args !== "object" || Array.isArray(args)) return "";
  const record = args as Record<string, unknown>;
  const parts: string[] = [];
  const description = record.description;
  if (typeof description === "string" && description) parts.push(description);
  for (const [key, value] of Object.entries(record)) {
    if (key === "description") continue;
    if (value === null || value === undefined || value === "") continue;
    if (typeof value === "object") continue;
    parts.push(String(value));
    if (parts.length === 3) break;
  }
  return parts.join(" · ").slice(0, 120);
}

function ToolCallCard({ payload }: { payload: OperatorToolCallPayload }) {
  const t = useTranslations("operator");
  const summary = argumentSummary(payload.arguments);
  const hasArgs = Boolean(payload.arguments && Object.keys(payload.arguments).length > 0);
  return (
    <details className="group rounded border border-edge/60 bg-surface-raised/50">
      <summary className="focus-ring flex cursor-pointer list-none items-center gap-2 rounded px-2 py-1 text-meta text-content-muted">
        <IconTool size={11} className="shrink-0" />
        <span className="shrink-0 font-data font-medium text-content-secondary">
          {shortToolName(payload.tool)}
        </span>
        {summary && <span className="min-w-0 flex-1 truncate font-data">{summary}</span>}
        <span className="ml-auto shrink-0 font-data opacity-60">{t(`tool.${payload.mode}`)}</span>
      </summary>
      {hasArgs && (
        <pre className="max-h-48 overflow-auto border-t border-edge/60 px-2 py-1.5 font-data text-meta leading-relaxed text-content-muted">
          {JSON.stringify(payload.arguments, null, 2)}
        </pre>
      )}
    </details>
  );
}

function ToolResultCard({ payload }: { payload: OperatorToolResultPayload }) {
  const t = useTranslations("operator");
  const runId = payload.ok ? findRunId(payload.result) : null;

  // A bare success carries no information a reader needs, and one full-width
  // box per call is what buries the conversation. Keep the box for the two
  // cases that do say something: a failure, and a run worth opening.
  if (payload.ok && !runId) {
    return (
      <div className="flex items-center gap-1.5 px-2 text-meta text-content-muted">
        <IconCheck size={11} className="shrink-0 text-status-success" />
        {t("tool.done")}
      </div>
    );
  }

  return (
    <div
      className={`rounded border px-2.5 py-2 text-body ${
        payload.ok
          ? "border-status-success/30 bg-status-success-bg text-content-secondary"
          : "border-status-failure/30 bg-status-error-bg text-status-failure"
      }`}
    >
      <div className="flex items-center gap-1.5 font-medium">
        {payload.ok ? <IconCheck size={13} /> : <IconError size={13} />}
        {payload.ok ? t("tool.done") : t("tool.failed")}
      </div>
      {!payload.ok && payload.error?.message && (
        <p className="mt-1 break-words text-meta">{payload.error.message}</p>
      )}
      {runId && <RunLink runId={runId} />}
    </div>
  );
}

function ProposalCard({
  frame,
  resolved,
  deciding,
  onDecision,
}: {
  frame: OperatorFrame<OperatorProposalPayload>;
  resolved: boolean;
  deciding: boolean;
  onDecision: (decision: "allow" | "deny") => void;
}) {
  const t = useTranslations("operator");
  const proposal = frame.payload.proposal;
  const command = formatProposalCommand(proposal.command);
  const target = proposal.target
    ? `${proposal.target.kind} · ${proposal.target.id}`
    : t("proposal.noTarget");
  return (
    <section
      aria-label={t("proposal.ariaLabel")}
      className="rounded-lg border border-status-pending/40 bg-status-warning-bg p-3"
    >
      <div className="flex items-start gap-2">
        <IconShield size={15} className="mt-0.5 shrink-0 text-status-pending" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-label font-semibold text-content-primary">{t("proposal.title")}</h3>
            <span className="rounded border border-status-pending/30 px-1.5 py-0.5 font-data text-meta uppercase tracking-[var(--tracking-meta)] text-status-pending">
              {t(`proposal.risk.${proposal.risk}`)}
            </span>
          </div>
          <p className="mt-1 text-body leading-relaxed text-content-secondary">
            {proposal.summary}
          </p>
          <p className="mt-2 truncate font-data text-meta text-content-muted" title={target}>
            {target}
          </p>
          {/*
            Open by default: every proposal that reaches this card needs a permission
            decision, and Allow must never be live over a command the operator has not
            been shown.
          */}
          <details
            open
            className="mt-3 overflow-hidden rounded border border-status-pending/30 bg-surface-base"
          >
            <summary className="focus-ring cursor-pointer px-2.5 py-2 text-meta font-medium text-content-secondary">
              {t("proposal.review")}
            </summary>
            <pre className="max-h-48 overflow-auto border-t border-status-pending/20 px-2.5 py-2 font-data text-meta leading-relaxed text-content-primary">
              {command.text}
            </pre>
          </details>
          {/*
            Outside the disclosure on purpose — a warning rendered inside a collapsible
            element is a warning the operator can approve without ever seeing.
          */}
          {command.elided && (
            <p
              data-testid="proposal-elided"
              className="mt-2 flex items-start gap-1.5 text-meta text-status-failure"
            >
              <IconError size={13} className="mt-0.5 shrink-0" />
              {command.droppedCharacters > 0
                ? t("proposal.elidedCharacters", { count: command.droppedCharacters })
                : t("proposal.elided")}
            </p>
          )}
          {resolved ? (
            <div className="mt-3 flex items-center gap-1.5 text-body text-status-success">
              <IconCheck size={13} />
              {t("proposal.resolved")}
            </div>
          ) : (
            <div className="mt-3 flex gap-2">
              <Button
                variant="primary"
                size="sm"
                disabled={deciding}
                onClick={() => onDecision("allow")}
              >
                {deciding ? t("proposal.deciding") : t("proposal.allow")}
              </Button>
              <Button
                variant="danger"
                size="sm"
                disabled={deciding}
                onClick={() => onDecision("deny")}
              >
                {t("proposal.deny")}
              </Button>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function FrameCard({
  frame,
  proposalResolved,
  deciding,
  onProposalDecision,
}: {
  frame: OperatorFrame;
  proposalResolved: boolean;
  deciding: boolean;
  onProposalDecision: (
    frame: OperatorFrame<OperatorProposalPayload>,
    decision: "allow" | "deny",
  ) => void;
}) {
  const t = useTranslations("operator");
  if (frame.type === "tool_call") {
    return <ToolCallCard payload={frame.payload as OperatorToolCallPayload} />;
  }
  if (frame.type === "tool_result") {
    return <ToolResultCard payload={frame.payload as OperatorToolResultPayload} />;
  }
  if (frame.type === "proposal") {
    const proposalFrame = frame as OperatorFrame<OperatorProposalPayload>;
    return (
      <ProposalCard
        frame={proposalFrame}
        resolved={proposalResolved}
        deciding={deciding}
        onDecision={(decision) => onProposalDecision(proposalFrame, decision)}
      />
    );
  }
  if (frame.type === "confirmation") {
    const payload = frame.payload as OperatorConfirmationPayload;
    const denied = payload.state === "denied" || payload.state === "cancelled";
    return (
      <div className="flex items-center gap-1.5 px-1 text-meta text-content-muted">
        {denied ? <IconBan size={12} /> : <IconShield size={12} />}
        {t(`confirmation.${payload.state}` as Parameters<typeof t>[0])}
      </div>
    );
  }
  if (frame.type === "ui_command") {
    const payload = frame.payload as OperatorUiCommandPayload;
    return (
      <div className="rounded border border-edge bg-surface-raised px-2.5 py-2 text-meta text-content-muted">
        {t("effect.requested", { kind: payload.effect.kind })}
      </div>
    );
  }
  if (frame.type === "error") {
    const payload = frame.payload as OperatorErrorPayload;
    return (
      <div className="rounded border border-status-failure/30 bg-status-error-bg px-2.5 py-2 text-body text-status-failure">
        <div className="flex items-start gap-1.5">
          <IconError size={13} className="mt-0.5 shrink-0" />
          <span className="break-words">{payload.error.message}</span>
        </div>
      </div>
    );
  }
  if (frame.type === "done") {
    const payload = frame.payload as OperatorDonePayload;
    const tone =
      payload.outcome === "completed"
        ? "text-status-success"
        : payload.outcome === "cancelled"
          ? "text-content-muted"
          : "text-status-failure";
    return (
      <div className={`flex items-center gap-2 py-1 text-meta ${tone}`}>
        <span aria-hidden className="h-px flex-1 bg-edge" />
        <span>{t(`outcome.${payload.outcome}`)}</span>
        <span aria-hidden className="h-px flex-1 bg-edge" />
      </div>
    );
  }
  if (frame.type === "truncation") {
    const payload = frame.payload as OperatorTruncationPayload;
    const reasonLimit =
      payload.reason === "frames_per_turn"
        ? payload.limits.maxFramesPerTurn
        : payload.limits.maxTurnPayloadBytes;
    return (
      <div className="rounded border border-status-warning/30 bg-status-warning-bg px-2.5 py-2 text-meta text-content-secondary">
        <div className="flex items-start gap-1.5">
          <IconError size={13} className="mt-0.5 shrink-0 text-status-warning" />
          <div className="min-w-0 break-words">
            <div className="font-medium text-content-primary">{t("truncation.title")}</div>
            <div>
              {t("truncation.summary", {
                frames: payload.elidedFrames,
                size: formatRetentionBytes(payload.elidedBytes),
              })}
            </div>
            <div className="font-data">
              {payload.reason}
              {t("truncation.limit", {
                limit:
                  payload.reason === "frames_per_turn"
                    ? String(reasonLimit)
                    : formatRetentionBytes(reasonLimit),
              })}
            </div>
          </div>
        </div>
      </div>
    );
  }
  return null;
}

export default function OperatorPanel({ open, onClose }: Props) {
  const t = useTranslations("operator");
  const location = useLocation();
  const search = useSearch({ from: "/fleet", shouldThrow: false });
  const project = useOperatorProject(location.pathname, search ?? {});
  const navigate = useNavigate();
  const [state, dispatch] = useReducer(operatorReducer, initialOperatorState);
  const [instruction, setInstruction] = useState("");
  const [modelCatalog, setModelCatalog] = useState<OperatorModelCatalogEntry[]>([]);
  const [model, setModel] = useState<string>("");
  const [effort, setEffort] = useState<OperatorEffort | "">("");
  const [sending, setSending] = useState(false);
  const [streamEpoch, setStreamEpoch] = useState(0);
  const [stopping, setStopping] = useState(false);
  const [actionError, setActionError] = useState<ActionError | null>(null);
  const [deciding, setDeciding] = useState<Set<string>>(() => new Set());
  const [decidedProposalIds, setDecidedProposalIds] = useState<Set<string>>(() => new Set());
  // Opt-in, per-browser: every pending proposal is allowed the moment it
  // arrives. The proposal cards still render and the executed confirmations
  // still land, so the audit trail is unchanged — only the click is skipped.
  const [autoAllow, setAutoAllow] = useState<boolean>(
    () => typeof window !== "undefined" && window.localStorage.getItem(AUTO_ALLOW_KEY) === "1",
  );
  const toggleAutoAllow = useCallback(() => {
    setAutoAllow((current) => {
      const next = !current;
      window.localStorage.setItem(AUTO_ALLOW_KEY, next ? "1" : "0");
      return next;
    });
  }, []);
  const [conversations, setConversations] = useState<OperatorConversation[]>([]);
  const [listFilter, setListFilter] = useState<"active" | "archived">("active");
  const [listPanelOpen, setListPanelOpen] = useState(false);
  const [listActionError, setListActionError] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [conversationBusyId, setConversationBusyId] = useState<string | null>(null);
  const [panelWidth, setPanelWidth] = useState(DEFAULT_WIDTH);
  const [showJump, setShowJump] = useState(false);
  const [visibleCount, setVisibleCount] = useState(200);
  const feedRef = useRef<HTMLDivElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const nearBottomRef = useRef(true);
  const previousItemCountRef = useRef(0);
  const anchorHeightRef = useRef<number | null>(null);
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const effectsInFlightRef = useRef<Set<string>>(new Set());
  const effectsAcknowledgedRef = useRef<Set<string>>(new Set());
  const effectOutcomesRef = useRef<Map<string, StoredEffectAcknowledgement>>(new Map());
  const renameInputRef = useRef<HTMLInputElement>(null);
  const renameButtonRefs = useRef<Map<string, HTMLButtonElement>>(new Map());
  const previousRenamingIdRef = useRef<string | null>(null);
  const conversationListButtonRef = useRef<HTMLButtonElement>(null);
  // The conversation the human last chose, set before its load starts so a late
  // result for an earlier choice is dropped. A failed load keeps it: the error view
  // disables the composer, and Retry or a pick from the list replaces it.
  const selectedConversationIdRef = useRef<string | null>(null);
  // How many times the operator has chosen a conversation (a row, a new or forked
  // conversation, a retry). The list's own first selection is not a choice.
  const operatorChoicesRef = useRef(0);
  // The filter of the newest list read, set when that read starts. A read for another
  // filter is dropped when it settles, and merged rows must match this one.
  const listFilterRef = useRef<"active" | "archived">("active");
  // Conversations this panel created, by a draft send or a fork. A list read started
  // before one was created may lack its row, so the merge keeps that row.
  const createdConversationIdsRef = useRef(new Set<string>());
  // A send that failed once its conversation was no longer shown: the error and the
  // text, kept until that conversation is selected again.
  const failedSendsRef = useRef(new Map<string, { message: string; instruction: string }>());
  // Decisions refused once their conversation was no longer shown: each one's error, by
  // proposal in the order refused, kept until that conversation is selected again or the
  // proposal is decided again.
  const refusedDecisionsRef = useRef(new Map<string, Map<string, ActionError>>());
  // Proposals auto-allow has decided once. A decision refused while its conversation is
  // shown is not retried by itself: its error shows, and the operator can decide again
  // by hand. One refused after its conversation stopped being shown leaves this set.
  const autoAllowTriedRef = useRef(new Set<string>());
  // The latest rename, pin or archive result this panel wrote, per conversation.
  const writeResultsRef = useRef(new Map<string, OperatorConversation>());
  // For the shown conversation: the last frame sequence read and its update_conversation
  // calls still waiting for a result, so a result or the turn's end refreshes metadata.
  const metadataRefreshRef = useRef<{
    conversationId: string;
    sequence: number;
    pending: Map<string, Set<string>>;
  } | null>(null);
  // Refresh reads can settle out of order. Each read takes the next number, and
  // a read applies only when no later read of that conversation has applied.
  const metadataReadCountRef = useRef(0);
  const metadataReadAppliedRef = useRef(new Map<string, number>());

  useEffect(
    () => () => {
      selectedConversationIdRef.current = null;
    },
    [],
  );

  const readConversationList = useCallback(async (status: "active" | "archived") => {
    const createdBefore = new Set(createdConversationIdsRef.current);
    const writtenBefore = new Map(writeResultsRef.current);
    const available = await listOperatorConversations({ status });
    return {
      available,
      merge(current: OperatorConversation[]) {
        if (listFilterRef.current !== status) return current;
        // Only local creations newer than this read may be absent from its result.
        const rows = new Map(
          current
            .filter(
              (item) =>
                createdConversationIdsRef.current.has(item.id) && !createdBefore.has(item.id),
            )
            .map((item) => [item.id, item]),
        );
        for (const item of available) rows.set(item.id, item);
        // A write completed during this read is kept over the read's snapshot of its row.
        let kept = false;
        for (const [id, written] of writeResultsRef.current) {
          const row = rows.get(id);
          if (writtenBefore.get(id) === written || row === undefined) continue;
          const next = keepWrite(row, written);
          kept ||= next !== row;
          rows.set(id, next);
        }
        const merged = [...rows.values()].filter((item) => item.status === listFilterRef.current);
        // The server lists pinned rows first, newest first; a kept pin is placed the same way.
        return kept ? merged.sort(byPinThenRecency) : merged;
      },
    };
  }, []);

  const refreshConversationMetadata = useCallback(
    async (conversationId: string) => {
      const read = ++metadataReadCountRef.current;
      const superseded = () => (metadataReadAppliedRef.current.get(conversationId) ?? 0) > read;
      const writtenBefore = writeResultsRef.current.get(conversationId);
      try {
        const [snapshot, list] = await Promise.all([
          getOperatorConversation(conversationId),
          readConversationList(listFilter),
        ]);
        if (selectedConversationIdRef.current !== conversationId || superseded()) return;
        metadataReadAppliedRef.current.set(conversationId, read);
        const written = writeResultsRef.current.get(conversationId);
        dispatch({
          type: "UPDATE_CONVERSATION",
          conversation:
            written === undefined || written === writtenBefore
              ? snapshot.conversation
              : keepWrite(snapshot.conversation, written),
        });
        setConversations(list.merge);
      } catch (error) {
        if (
          selectedConversationIdRef.current !== conversationId ||
          listFilterRef.current !== listFilter ||
          superseded()
        )
          return;
        setActionError({ message: error instanceof Error ? error.message : t("errors.load") });
      }
    },
    [listFilter, readConversationList, t],
  );

  useEffect(() => {
    const conversationId = state.conversation?.id;
    if (!conversationId || state.loadState !== "ready") {
      metadataRefreshRef.current = null;
      return;
    }
    const previous = metadataRefreshRef.current;
    const sameConversation = previous?.conversationId === conversationId;
    const tracking = sameConversation
      ? previous
      : {
          conversationId,
          sequence: 0,
          pending: new Map<string, Set<string>>(),
        };
    let refresh = false;
    for (const frame of state.frames) {
      if (frame.conversationId !== conversationId || frame.sequence <= tracking.sequence) continue;
      tracking.sequence = frame.sequence;
      refresh = recordConversationMetadataCall(tracking.pending, frame) || refresh;
    }
    metadataRefreshRef.current = tracking;
    // Loaded snapshots already contain current metadata. Track their pending
    // calls so later provider results or a terminal frame can refresh it.
    if (sameConversation && refresh) void refreshConversationMetadata(conversationId);
  }, [refreshConversationMetadata, state.conversation?.id, state.frames, state.loadState]);

  // Derived rather than synced through an effect: archiving the row being
  // renamed, or switching the filter away from it, removes the row and its
  // input while that input still holds focus. Removing a focused element moves
  // focus to the document body silently and fires no blur, so a stored id would
  // just go stale -- pointing at a row nobody can see, and reopening rename on
  // it if the row ever comes back. Reading the rename as "the row is here and
  // it is the one being renamed" makes the disappearance a state change the
  // focus effect below can act on.
  const activeRenamingId =
    renamingId && conversations.some((item) => item.id === renamingId) ? renamingId : null;

  useEffect(() => {
    if (activeRenamingId) {
      renameInputRef.current?.focus();
      previousRenamingIdRef.current = activeRenamingId;
      return;
    }
    if (previousRenamingIdRef.current) {
      // The row this focus was meant to return to may be the one that just
      // disappeared, and its ref entry goes with it. Without a fallback the
      // input is removed with focus on nothing, which strands a keyboard user
      // on document.body. The disclosure trigger outlives every row, so it is
      // the target that is always still there.
      const button = renameButtonRefs.current.get(previousRenamingIdRef.current);
      (button ?? conversationListButtonRef.current)?.focus();
    }
    previousRenamingIdRef.current = activeRenamingId;
  }, [activeRenamingId]);

  const loadConversation = useCallback(
    async (conversationId: string) => {
      selectedConversationIdRef.current = conversationId;
      dispatch({ type: "LOAD_START" });
      try {
        const snapshot = await getOperatorConversation(conversationId);
        if (selectedConversationIdRef.current !== conversationId) return;
        window.localStorage.setItem(STORAGE_KEY, snapshot.conversation.id);
        setConversations((current) => {
          if (snapshot.conversation.status !== listFilterRef.current) {
            return current.filter((item) => item.id !== snapshot.conversation.id);
          }
          const index = current.findIndex((item) => item.id === snapshot.conversation.id);
          if (index < 0) return [snapshot.conversation, ...current];
          return current.map((item, itemIndex) =>
            itemIndex === index ? snapshot.conversation : item,
          );
        });
        dispatch({
          type: "LOAD_SUCCESS",
          conversation: snapshot.conversation,
          frames: snapshot.frames,
        });
      } catch (error) {
        if (selectedConversationIdRef.current !== conversationId) return;
        dispatch({
          type: "LOAD_ERROR",
          error: error instanceof Error ? error.message : t("errors.load"),
        });
      }
    },
    [t],
  );

  const proposals = useMemo(() => pendingOperatorProposals(state.frames), [state.frames]);

  // A failed send or a refused decision kept for a conversation is reported, the send
  // first, once that conversation is shown and ready, no send, Stop or decision is in
  // flight or about to start under auto-allow, and no other error is showing. Refusals
  // are reported one at a time, oldest first, each once the alert before it has
  // cleared. A new decision of a proposal drops its kept refusal when it starts, so only
  // that decision's outcome is reported. A send in flight still owns the
  // composer and its outcome may clear it, a Stop or a decision made by hand clears
  // the alert when it starts, and each of them replaces it when it fails, as another
  // error showing would be replaced, so each defers the report rather than losing it.
  const shownConversationId = state.conversation?.id;
  useEffect(() => {
    if (
      !shownConversationId ||
      state.loadState !== "ready" ||
      sending ||
      stopping ||
      deciding.size > 0 ||
      actionError ||
      state.error
    )
      return;
    // A proposal auto-allow will decide on its next tick; if that decision fails, its
    // error would replace the alert.
    if (
      autoAllow &&
      proposals.some(({ frame, resolved }) => {
        const id = frame.payload.proposal.id;
        return !resolved && !decidedProposalIds.has(id) && !autoAllowTriedRef.current.has(id);
      })
    )
      return;
    const failed = failedSendsRef.current.get(shownConversationId);
    if (!failed) {
      const kept = refusedDecisionsRef.current.get(shownConversationId);
      if (!kept) return;
      const pending = new Set(
        proposals.filter(({ resolved }) => !resolved).map(({ frame }) => frame.payload.proposal.id),
      );
      // Each reported once, and only while its proposal is still pending here; the
      // rest wait for the next report.
      for (const [proposalId, refused] of kept) {
        kept.delete(proposalId);
        if (pending.has(proposalId)) {
          setActionError(refused);
          break;
        }
      }
      if (kept.size === 0) refusedDecisionsRef.current.delete(shownConversationId);
      return;
    }
    // Reported once: a later error replaces this one as it replaces any other, and the
    // text stays in the composer.
    failedSendsRef.current.delete(shownConversationId);
    setActionError({ message: failed.message });
    // Text typed here since keeps its place after the failed text, so neither is lost.
    setInstruction((current) =>
      current.trim() ? `${failed.instruction}\n${current}` : failed.instruction,
    );
  }, [
    actionError,
    autoAllow,
    decidedProposalIds,
    deciding,
    proposals,
    sending,
    shownConversationId,
    state.error,
    state.loadState,
    stopping,
  ]);

  useEffect(() => {
    let active = true;
    void fetchOperatorModelCatalog()
      .then((catalog) => {
        if (!active) return;
        setModelCatalog(catalog.models);
        // Never replace "no selection" with the catalog's first entry: the
        // composer still works with no model chosen -- the daemon falls back
        // to its own env-var default for a turn that omits one, and picking
        // one here on the caller's behalf would silently override that
        // default the moment the catalog loads, before the human ever
        // touched the menu.
        //
        // A selection the catalog does not offer is not cleared either. It is
        // usually the conversation's own stored pin, which the daemon will
        // keep using; clearing it would show "Default" for a turn that runs
        // on something else. The menu renders it as unavailable instead, so
        // the operator can see what is in force and change it.
      })
      .catch(() => {
        // The composer still works with no model selected -- the daemon
        // falls back to its own env-var default for a turn that omits one.
      });
    return () => {
      active = false;
    };
  }, []);

  // A conversation remembers the provider and model it was pinned to, and the
  // daemon keeps using that pin for a turn that names neither. Showing
  // "Default" while a pin is in force tells the operator the opposite of what
  // will happen, so the stored selection is hydrated whenever the conversation
  // changes. Effort is per turn rather than per conversation, so it starts
  // empty and the operator chooses it again.
  const hydratedConversationRef = useRef<string | null>(null);
  useEffect(() => {
    const conversation = state.conversation;
    if (!conversation) {
      hydratedConversationRef.current = null;
      return;
    }
    if (hydratedConversationRef.current === conversation.id) return;
    hydratedConversationRef.current = conversation.id;
    setModel(conversation.providerModel ?? "");
    setEffort("");
  }, [state.conversation]);

  const effortChoices = useMemo(
    () => modelCatalog.find((entry) => entry.id === model)?.efforts ?? [],
    [modelCatalog, model],
  );
  const modelGroups = useMemo(() => groupModelsByProvider(modelCatalog), [modelCatalog]);
  const selectedModelEntry = useMemo(
    () => modelCatalog.find((entry) => entry.id === model) ?? null,
    [modelCatalog, model],
  );
  // Derived, not synced via effect: a stale selection from a previous model
  // just stops being offered rather than needing a setState-on-effect sync.
  const effectiveEffort = effort && effortChoices.includes(effort) ? effort : "";

  useEffect(() => {
    let active = true;
    const stored = readStoredConversation();
    // A choice of conversation made while the list is read, New conversation included,
    // is kept: the read then selects nothing.
    const choicesAtStart = operatorChoicesRef.current;
    void readConversationList("active")
      .then(({ available, merge }) => {
        if (!active || listFilterRef.current !== "active") return;
        setConversations(merge);
        if (selectedConversationIdRef.current || operatorChoicesRef.current !== choicesAtStart)
          return;
        const selected =
          (stored ? available.find((item) => item.id === stored) : null) ??
          available.find((item) => item.status === "active") ??
          available[0] ??
          null;
        if (selected) {
          void loadConversation(selected.id);
        } else if (stored) {
          window.localStorage.removeItem(STORAGE_KEY);
        }
      })
      .catch(() => {
        if (
          !active ||
          selectedConversationIdRef.current ||
          operatorChoicesRef.current !== choicesAtStart ||
          listFilterRef.current !== "active"
        )
          return;
        if (stored) {
          void loadConversation(stored);
        } else {
          setActionError({ message: t("errors.load") });
        }
      });
    return () => {
      active = false;
    };
  }, [loadConversation, readConversationList, t]);

  useEffect(() => {
    const conversationId = state.conversation?.id;
    if (!conversationId || state.loadState !== "ready") return;
    let queued: OperatorFrame[] = [];
    let animationFrame: number | null = null;
    const flush = () => {
      animationFrame = null;
      if (queued.length === 0) return;
      const batch = queued;
      queued = [];
      dispatch({ type: "APPEND_FRAMES", frames: batch });
    };
    const close = streamOperatorConversation(conversationId, state.lastSequence, {
      onFrame: (frame) => {
        queued.push(frame);
        if (animationFrame == null) animationFrame = window.requestAnimationFrame(flush);
      },
      onConnection: (connection) => {
        dispatch({
          type: "CONNECTION",
          state:
            connection === "open"
              ? "open"
              : connection === "reconnecting"
                ? "reconnecting"
                : "connecting",
        });
      },
      onError: (error, fatal) => {
        if (fatal) {
          dispatch({ type: "CONNECTION", state: "error", error: error.message });
        } else {
          dispatch({ type: "CONNECTION", state: "reconnecting" });
        }
      },
    });
    return () => {
      close();
      if (animationFrame != null) window.cancelAnimationFrame(animationFrame);
      if (queued.length) dispatch({ type: "APPEND_FRAMES", frames: queued });
    };
    // Reconnect is normally owned by streamOperatorConversation; changing the
    // cursor must not tear down a healthy stream after every frame. A
    // successful submit deliberately advances streamEpoch, though: the POST
    // may have reached a replacement daemon after the old stream died, and a
    // fresh subscription replays that durable user frame immediately.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.conversation?.id, state.loadState, streamEpoch]);

  const applyClientEffect = useCallback(
    async (effect: unknown): Promise<StoredEffectAcknowledgement> => {
      const plan = planOperatorEffect(effect);
      const currentRoute = `${window.location.pathname}${window.location.search}`;
      if (plan.kind === "reject") {
        return {
          status: "rejected",
          clientRoute: currentRoute,
          rejectionCode: plan.rejectionCode,
        };
      }
      try {
        if (plan.kind === "theme") {
          applyTheme(plan.theme);
          if (document.documentElement.getAttribute("data-theme") !== plan.theme) {
            return {
              status: "rejected",
              clientRoute: currentRoute,
              rejectionCode: "client_error",
            };
          }
          return { status: "applied", clientRoute: currentRoute };
        }
        await navigate({
          to: plan.to as never,
          search: plan.search as never,
        });
        return { status: "applied", clientRoute: effectPlanRoute(plan) };
      } catch {
        return {
          status: "rejected",
          clientRoute: currentRoute,
          rejectionCode: "client_error",
        };
      }
    },
    [navigate],
  );

  useEffect(() => {
    const conversationId = state.conversation?.id;
    if (!conversationId || state.loadState !== "ready") return;
    const remembered = readEffectAcknowledgements(conversationId);
    for (const frame of state.frames) {
      if (frame.type !== "ui_command") continue;
      const effect = (frame.payload as OperatorUiCommandPayload).effect;
      const effectId = operatorEffectId(effect);
      const effectKey = `${conversationId}:${effectId ?? ""}`;
      if (
        !effectId ||
        effectsInFlightRef.current.has(effectKey) ||
        effectsAcknowledgedRef.current.has(effectKey)
      ) {
        continue;
      }
      effectsInFlightRef.current.add(effectKey);
      void (async () => {
        const previous = effectOutcomesRef.current.get(effectKey) ?? remembered.get(effectId);
        let acknowledgement = previous;
        if (!acknowledgement) {
          if (!effectAcknowledgementStorageAvailable(conversationId)) {
            acknowledgement = {
              status: "rejected",
              clientRoute: `${window.location.pathname}${window.location.search}`,
              rejectionCode: "client_error",
            };
          } else {
            acknowledgement = await applyClientEffect(effect);
          }
          // The in-memory outcome is written before any network await. If
          // acknowledgement fails, a later frame retries the same ACK without
          // replaying the visible navigation/theme operation.
          effectOutcomesRef.current.set(effectKey, acknowledgement);
          // Persist before the network acknowledgement. If the daemon drops
          // immediately after applying navigation/theme, replay sends the
          // same acknowledgement without repeating the visible side effect.
          rememberEffectAcknowledgement(conversationId, effectId, acknowledgement);
        }
        await acknowledgeOperatorEffect(conversationId, effectId, acknowledgement);
        effectsAcknowledgedRef.current.add(effectKey);
      })()
        .catch((error) => {
          setActionError({ message: error instanceof Error ? error.message : t("errors.effect") });
        })
        .finally(() => {
          effectsInFlightRef.current.delete(effectKey);
        });
    }
  }, [applyClientEffect, state.conversation?.id, state.frames, state.loadState, t]);

  const items = useMemo(() => displayItems(state.frames), [state.frames]);
  const visibleItems = useMemo(() => items.slice(-visibleCount), [items, visibleCount]);
  const resolvedProposalIds = useMemo(() => {
    const resolved = new Set(decidedProposalIds);
    for (const item of proposals) {
      if (item.resolved) resolved.add(item.frame.payload.proposal.id);
    }
    return resolved;
  }, [decidedProposalIds, proposals]);

  // A decision's error no longer applies once its proposal is decided or the stream
  // resolves it; any other error stays. Cleared while rendering, so it is never shown
  // beside its resolved proposal.
  if (actionError?.proposalId && resolvedProposalIds.has(actionError.proposalId))
    setActionError(null);

  useLayoutEffect(() => {
    if (!open || !nearBottomRef.current) {
      if (open && !nearBottomRef.current) setShowJump(true);
      return;
    }
    endRef.current?.scrollIntoView({ block: "end" });
    setShowJump(false);
  }, [items.length, open]);

  useEffect(() => {
    const added = Math.max(0, items.length - previousItemCountRef.current);
    if (added && !nearBottomRef.current) setVisibleCount((count) => count + added);
    previousItemCountRef.current = items.length;
  }, [items.length]);

  // Report where the human is whenever they move, not only when they send.
  // A turn's context is frozen at submit, so without this the Operator's answer
  // to "where am I" is wherever they were when they hit send. Best effort: a
  // failed report costs freshness, never correctness, because the read falls
  // back to the turn's own snapshot and says which one it used.
  const conversationId = state.conversation?.id;
  useEffect(() => {
    if (!conversationId) return;
    // Stamped here, when the view is SEEN, not below when the request fires:
    // the debounce and the network both reorder, so two navigations can reach
    // the server reversed and the server has to keep whichever the browser
    // observed later rather than whichever arrived later.
    const context = operatorContext(
      location.pathname,
      location.search as Record<string, unknown>,
      project,
    );
    const timer = window.setTimeout(() => {
      void reportOperatorView(conversationId, context).catch(() => {});
    }, 150);
    return () => window.clearTimeout(timer);
  }, [conversationId, location.pathname, location.search, project]);

  useLayoutEffect(() => {
    const oldHeight = anchorHeightRef.current;
    const feed = feedRef.current;
    if (oldHeight == null || !feed) return;
    feed.scrollTop += feed.scrollHeight - oldHeight;
    anchorHeightRef.current = null;
  }, [visibleCount]);

  const handleScroll = useCallback(() => {
    const feed = feedRef.current;
    if (!feed) return;
    const near = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 96;
    nearBottomRef.current = near;
    setShowJump(!near);
  }, []);

  const jumpToLatest = useCallback(() => {
    nearBottomRef.current = true;
    endRef.current?.scrollIntoView({ block: "end" });
    setShowJump(false);
  }, []);

  const handleSend = useCallback(async () => {
    const trimmed = instruction.trim();
    if (!trimmed || sending || state.activeRequestId) return;
    setSending(true);
    setActionError(null);
    // The conversation this send is for: the one shown when it started, or, for
    // a draft, the one created for it. A failure is reported there, at once while
    // it is selected, else when it is selected again. A draft whose conversation
    // could not be created has none, so its failure shows where its text still is.
    let sentTo = state.conversation?.id ?? null;
    // A draft's new conversation takes the panel only while the operator has made no
    // choice of conversation since this send started; otherwise it is listed and the
    // turn submitted there, and the chosen conversation stays selected.
    const choicesAtStart = operatorChoicesRef.current;
    try {
      let conversation = state.conversation;
      if (!conversation) {
        conversation = await createOperatorConversation();
        createdConversationIdsRef.current.add(conversation.id);
        sentTo = conversation.id;
        setConversations((current) => [
          conversation as OperatorConversation,
          ...current.filter((item) => item.id !== conversation?.id),
        ]);
        if (operatorChoicesRef.current === choicesAtStart) {
          selectedConversationIdRef.current = conversation.id;
          window.localStorage.setItem(STORAGE_KEY, conversation.id);
          dispatch({ type: "LOAD_SUCCESS", conversation, frames: [] });
        }
      }
      const context = operatorContext(
        location.pathname,
        location.search as Record<string, unknown>,
        project,
      );
      // The menu is hydrated from the conversation's pin, so an empty
      // selection here means the operator moved it back to Default. Sending
      // nothing would leave the pin in force, which is the opposite of what
      // the menu now says, so ask for it to be dropped.
      const clearing = !model && Boolean(conversation.provider || conversation.providerModel);
      const accepted = await submitOperatorTurn(conversation.id, {
        instruction: trimmed,
        context,
        expectedLastSequence: state.lastSequence,
        ...(model ? { model } : {}),
        ...(effectiveEffort ? { effort: effectiveEffort } : {}),
        ...(clearing ? { clearSelection: true } : {}),
      });
      // The turn runs on its own conversation; the panel shows it only when that
      // conversation is still the one selected.
      if (selectedConversationIdRef.current === conversation.id) {
        dispatch({ type: "TURN_ACCEPTED", requestId: accepted.requestId });
        setStreamEpoch((current) => current + 1);
        nearBottomRef.current = true;
      }
      setInstruction("");
    } catch (error) {
      const message = error instanceof Error ? error.message : t("errors.send");
      if (sentTo !== null && selectedConversationIdRef.current !== sentTo) {
        // The text leaves the composer now shown, unless it was edited, so it is not
        // sent to this conversation by mistake; both return with the one it was for.
        failedSendsRef.current.set(sentTo, { message, instruction: trimmed });
        setInstruction((current) => (current.trim() === trimmed ? "" : current));
        return;
      }
      setActionError({ message });
      // New conversation during the send clears the composer; the failed text comes back.
      setInstruction((current) => (current.trim() ? current : trimmed));
      if (
        error instanceof ApiError &&
        error.status === 409 &&
        error.code === "stale_context" &&
        state.conversation
      ) {
        await loadConversation(state.conversation.id);
      }
    } finally {
      setSending(false);
    }
  }, [
    instruction,
    location.pathname,
    location.search,
    project,
    model,
    effectiveEffort,
    sending,
    state.activeRequestId,
    state.conversation,
    state.lastSequence,
    t,
    loadConversation,
  ]);

  const handleStop = useCallback(async () => {
    if (!state.conversation || !state.activeRequestId || stopping) return;
    setStopping(true);
    setActionError(null);
    try {
      await cancelOperatorRequest(state.conversation.id, state.activeRequestId);
    } catch (error) {
      setActionError({ message: error instanceof Error ? error.message : t("errors.stop") });
    } finally {
      setStopping(false);
    }
  }, [state.activeRequestId, state.conversation, stopping, t]);

  const handleProposalDecision = useCallback(
    async (
      frame: OperatorFrame<OperatorProposalPayload>,
      decision: "allow" | "deny",
      auto = false,
    ) => {
      if (!state.conversation) return;
      const conversationId = state.conversation.id;
      const proposal = frame.payload.proposal;
      setDeciding((current) => new Set(current).add(proposal.id));
      // A click clears the alert; a decision that auto-allow makes leaves it showing.
      if (!auto) setActionError(null);
      refusedDecisionsRef.current.get(conversationId)?.delete(proposal.id);
      try {
        await decideOperatorProposal(
          conversationId,
          proposal.id,
          decision,
          proposal.commandHash,
          proposal.target?.version ?? null,
        );
        setDecidedProposalIds((current) => new Set(current).add(proposal.id));
      } catch (error) {
        const refusal = {
          message: error instanceof Error ? error.message : t("errors.decision"),
          proposalId: proposal.id,
        };
        if (selectedConversationIdRef.current !== conversationId) {
          // Refused once its conversation was no longer shown, so it is not reported in
          // another one: it is kept for that conversation and reported when it is shown
          // again. The proposal is still pending there and counts as untried, so with
          // auto-allow on it is decided again there instead.
          autoAllowTriedRef.current.delete(proposal.id);
          let kept = refusedDecisionsRef.current.get(conversationId);
          if (!kept) refusedDecisionsRef.current.set(conversationId, (kept = new Map()));
          kept.set(proposal.id, refusal);
          return;
        }
        setActionError(refusal);
      } finally {
        setDeciding((current) => {
          const next = new Set(current);
          next.delete(proposal.id);
          return next;
        });
      }
    },
    [state.conversation, t],
  );

  // Auto-allow: send one decision per pending proposal, as it arrives. The tried set
  // makes this idempotent across re-renders and SSE replays, and keeps a refused
  // decision from being sent again on every render; one refused after its
  // conversation stopped being shown leaves the set, so it is decided again there.
  // Deferred a tick so the decision (a network POST plus its own state
  // bookkeeping) never runs synchronously inside the effect body; the guard
  // sets keep a cancelled-then-rescheduled tick from double-deciding.
  // Nothing is tried while no conversation is shown: a proposal a closing stream
  // appends after New conversation is decided once its conversation is shown again.
  useEffect(() => {
    if (!autoAllow || !shownConversationId) return;
    const timer = window.setTimeout(() => {
      for (const item of proposals) {
        const id = item.frame.payload.proposal.id;
        if (
          item.resolved ||
          decidedProposalIds.has(id) ||
          deciding.has(id) ||
          autoAllowTriedRef.current.has(id)
        )
          continue;
        autoAllowTriedRef.current.add(id);
        void handleProposalDecision(item.frame, "allow", true);
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [
    autoAllow,
    proposals,
    decidedProposalIds,
    deciding,
    handleProposalDecision,
    shownConversationId,
  ]);

  const resetConversation = useCallback(() => {
    operatorChoicesRef.current += 1;
    selectedConversationIdRef.current = null;
    window.localStorage.removeItem(STORAGE_KEY);
    dispatch({ type: "RESET" });
    setActionError(null);
    setInstruction("");
    setDecidedProposalIds(new Set());
  }, []);

  const selectConversation = useCallback(
    (conversationId: string) => {
      if (!conversationId) {
        resetConversation();
        return;
      }
      operatorChoicesRef.current += 1;
      if (conversationId === state.conversation?.id) return;
      dispatch({ type: "RESET" });
      setVisibleCount(200);
      setActionError(null);
      setDecidedProposalIds(new Set());
      nearBottomRef.current = true;
      void loadConversation(conversationId);
    },
    [loadConversation, resetConversation, state.conversation?.id],
  );

  const refreshConversations = useCallback(
    (status: "active" | "archived") => {
      listFilterRef.current = status;
      return readConversationList(status)
        .then(({ merge }) => setConversations(merge))
        .catch((error) => {
          if (listFilterRef.current !== status) return;
          setListActionError(error instanceof Error ? error.message : t("errors.load"));
        });
    },
    [readConversationList, t],
  );

  const commitRename = useCallback(
    async (conversationId: string, title: string) => {
      const trimmed = title.trim();
      setRenamingId(null);
      const existing = conversations.find((item) => item.id === conversationId);
      if (existing && (existing.title ?? "") === trimmed) return;
      setListActionError(null);
      try {
        const updated = await updateOperatorConversation(conversationId, {
          title: trimmed.length > 0 ? trimmed : null,
        });
        writeResultsRef.current.set(updated.id, updated);
        setConversations((current) =>
          current.map((item) => (item.id === updated.id ? updated : item)),
        );
        if (selectedConversationIdRef.current === updated.id) {
          dispatch({ type: "UPDATE_CONVERSATION", conversation: updated });
        }
      } catch (error) {
        setListActionError(error instanceof Error ? error.message : t("errors.rename"));
      }
    },
    [conversations, t],
  );

  const togglePin = useCallback(
    async (conversation: OperatorConversation) => {
      setConversationBusyId(conversation.id);
      setListActionError(null);
      try {
        const updated = await updateOperatorConversation(conversation.id, {
          pinned: !conversation.pinned,
        });
        writeResultsRef.current.set(updated.id, updated);
        setConversations((current) =>
          current.map((item) => (item.id === updated.id ? updated : item)).sort(byPinThenRecency),
        );
        if (selectedConversationIdRef.current === updated.id) {
          dispatch({ type: "UPDATE_CONVERSATION", conversation: updated });
        }
      } catch (error) {
        setListActionError(error instanceof Error ? error.message : t("errors.pin"));
      } finally {
        setConversationBusyId(null);
      }
    },
    [t],
  );

  const toggleArchive = useCallback(
    async (conversation: OperatorConversation) => {
      const nextStatus = conversation.status === "archived" ? "active" : "archived";
      setConversationBusyId(conversation.id);
      setListActionError(null);
      try {
        const updated = await updateOperatorConversation(conversation.id, {
          status: nextStatus,
        });
        writeResultsRef.current.set(updated.id, updated);
        setConversations((current) =>
          updated.status === listFilterRef.current
            ? current.map((item) => (item.id === updated.id ? updated : item))
            : current.filter((item) => item.id !== updated.id),
        );
        if (selectedConversationIdRef.current === updated.id) {
          dispatch({ type: "UPDATE_CONVERSATION", conversation: updated });
        }
      } catch (error) {
        setListActionError(error instanceof Error ? error.message : t("errors.archive"));
      } finally {
        setConversationBusyId(null);
      }
    },
    [t],
  );

  const forkConversationById = useCallback(
    async (conversation: OperatorConversation) => {
      // The click is the choice, so a draft's conversation created during the fork
      // request does not take the panel, even when the fork then fails; it is listed.
      operatorChoicesRef.current += 1;
      setConversationBusyId(conversation.id);
      setListActionError(null);
      try {
        const snapshot = await forkOperatorConversation(conversation.id);
        createdConversationIdsRef.current.add(snapshot.conversation.id);
        setConversations((current) => [snapshot.conversation, ...current]);
        resetConversation();
        selectedConversationIdRef.current = snapshot.conversation.id;
        window.localStorage.setItem(STORAGE_KEY, snapshot.conversation.id);
        setVisibleCount(200);
        nearBottomRef.current = true;
        dispatch({
          type: "LOAD_SUCCESS",
          conversation: snapshot.conversation,
          frames: snapshot.frames,
        });
        setListPanelOpen(false);
      } catch (error) {
        setListActionError(error instanceof Error ? error.message : t("errors.fork"));
      } finally {
        setConversationBusyId(null);
      }
    },
    [resetConversation, t],
  );

  const clampWidth = useCallback(
    (value: number) => Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, value)),
    [],
  );

  if (!open) return null;

  const empty = state.frames.length === 0 && state.loadState !== "loading";
  const fatalError = state.loadState === "error";
  const conversationListLabel = state.conversation
    ? t("list.ariaLabelSelected", { title: conversationLabel(state.conversation) })
    : t("list.ariaLabel");

  return (
    <>
      <button
        type="button"
        aria-label={t("close")}
        className="fixed inset-0 z-30 bg-black/40 lg:hidden"
        onClick={onClose}
      />
      <aside
        aria-label={t("ariaLabel")}
        className="fixed inset-y-0 end-0 z-40 flex min-h-0 flex-col border-s border-edge bg-surface-base shadow-card lg:relative lg:inset-auto lg:z-auto lg:shrink-0 lg:shadow-none"
        style={{ width: `min(${panelWidth}px, calc(100vw - 3.5rem))` }}
      >
        {/* WAI-ARIA window-splitter pattern: the separator is intentionally
            focusable and handles pointer/keyboard resizing. */}
        <div
          role="separator"
          aria-label={t("resize")}
          aria-orientation="vertical"
          aria-valuemin={MIN_WIDTH}
          aria-valuemax={MAX_WIDTH}
          aria-valuenow={Math.round(panelWidth)}
          tabIndex={0}
          onPointerDown={(event) => {
            dragRef.current = { startX: event.clientX, startWidth: panelWidth };
            event.currentTarget.setPointerCapture(event.pointerId);
          }}
          onPointerMove={(event) => {
            const drag = dragRef.current;
            if (!drag) return;
            const rtl = document.documentElement.dir === "rtl";
            const delta = event.clientX - drag.startX;
            setPanelWidth(clampWidth(drag.startWidth + (rtl ? delta : -delta)));
          }}
          onPointerUp={() => {
            dragRef.current = null;
          }}
          onPointerCancel={() => {
            dragRef.current = null;
          }}
          onKeyDown={(event) => {
            if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
            event.preventDefault();
            const rtl = document.documentElement.dir === "rtl";
            const towardEnd = event.key === "ArrowRight";
            const grow = rtl ? towardEnd : !towardEnd;
            setPanelWidth((width) => clampWidth(width + (grow ? 16 : -16)));
          }}
          className="group absolute inset-y-0 start-0 z-10 w-2 -translate-x-1/2 cursor-col-resize focus:outline-none"
        >
          <span
            aria-hidden
            className="absolute inset-y-0 start-1/2 w-px bg-edge transition-colors group-hover:bg-accent group-focus-visible:bg-accent"
          />
        </div>

        <header className="flex h-12 shrink-0 items-center gap-3 border-b border-edge px-3">
          <div className="flex h-7 w-7 items-center justify-center rounded border border-edge bg-surface-raised text-accent">
            <IconShield size={15} />
          </div>
          <div className="min-w-0 flex-1">
            <h2 className="truncate text-label font-semibold text-content-primary">{t("title")}</h2>
            <div className="flex min-w-0 items-center gap-2">
              <ConnectionBadge state={state.connectionState} />
              <div className="relative min-w-0 max-w-36 flex-1">
                <button
                  type="button"
                  ref={conversationListButtonRef}
                  aria-label={conversationListLabel}
                  aria-expanded={listPanelOpen}
                  title={conversationListLabel}
                  onClick={() => setListPanelOpen((open) => !open)}
                  className="flex min-w-0 items-center gap-1 truncate border-0 bg-transparent py-0 font-data text-meta text-content-muted outline-none hover:text-content-primary focus:text-content-primary"
                >
                  <span className="min-w-0 truncate">
                    {state.conversation
                      ? conversationLabel(state.conversation)
                      : t("newConversation")}
                  </span>
                  <IconChevronDown size={11} />
                </button>
                {listPanelOpen && (
                  <div className="absolute start-0 top-full z-20 mt-1 w-80 rounded-lg border border-edge bg-surface-raised p-2 shadow-lg">
                    <div className="flex items-center justify-between gap-2 pb-2">
                      <div className="flex gap-1">
                        <Button
                          variant="toggle"
                          size="sm"
                          active={listFilter === "active"}
                          onClick={() => {
                            setListFilter("active");
                            void refreshConversations("active");
                          }}
                        >
                          {t("list.filter.active")}
                        </Button>
                        <Button
                          variant="toggle"
                          size="sm"
                          active={listFilter === "archived"}
                          onClick={() => {
                            setListFilter("archived");
                            void refreshConversations("archived");
                          }}
                        >
                          {t("list.filter.archived")}
                        </Button>
                      </div>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setListPanelOpen(false);
                          resetConversation();
                        }}
                      >
                        {t("newConversation")}
                      </Button>
                    </div>
                    {listActionError && (
                      <p className="pb-2 text-meta text-status-failure">{listActionError}</p>
                    )}
                    <ul className="max-h-72 space-y-1 overflow-y-auto">
                      {conversations.length === 0 && (
                        <li className="px-2 py-3 text-center text-meta text-content-muted">
                          {t("list.empty")}
                        </li>
                      )}
                      {conversations.map((conversation) => {
                        const busy = conversationBusyId === conversation.id;
                        const archived = conversation.status === "archived";
                        return (
                          <li
                            key={conversation.id}
                            className={[
                              "flex items-center gap-1 rounded px-1.5 py-1",
                              conversation.id === state.conversation?.id
                                ? "bg-surface-overlay"
                                : "hover:bg-surface-overlay",
                            ].join(" ")}
                          >
                            {activeRenamingId === conversation.id ? (
                              <input
                                ref={renameInputRef}
                                value={renameDraft}
                                maxLength={512}
                                placeholder={t("list.renamePlaceholder")}
                                onChange={(event) => setRenameDraft(event.target.value)}
                                onKeyDown={(event) => {
                                  if (event.key === "Enter") {
                                    // Deferred past this keydown/keyup pair: blurring
                                    // synchronously here can hand focus to the row's
                                    // Rename button before the browser delivers the
                                    // matching keyup, which then replays as a second
                                    // Enter activation on that button.
                                    event.preventDefault();
                                    const input = event.currentTarget;
                                    requestAnimationFrame(() => input.blur());
                                  } else if (event.key === "Escape") {
                                    setRenamingId(null);
                                  }
                                }}
                                onBlur={() => void commitRename(conversation.id, renameDraft)}
                                className="min-w-0 flex-1 rounded border border-edge bg-surface-base px-1 py-0.5 text-meta text-content-primary outline-none focus:border-accent"
                              />
                            ) : (
                              <button
                                type="button"
                                onDoubleClick={() => {
                                  setRenamingId(conversation.id);
                                  setRenameDraft(conversation.title ?? "");
                                }}
                                onClick={() => {
                                  selectConversation(conversation.id);
                                  setListPanelOpen(false);
                                }}
                                title={conversation.description || t("list.renamePlaceholder")}
                                className="min-w-0 flex-1 truncate text-start text-meta text-content-primary outline-none"
                              >
                                <span className="block truncate">
                                  {conversation.pinned ? "★ " : ""}
                                  {conversationLabel(conversation)}
                                </span>
                                {conversation.description && (
                                  <span className="block truncate text-content-muted">
                                    {conversation.description}
                                  </span>
                                )}
                              </button>
                            )}
                            {activeRenamingId !== conversation.id && (
                              <button
                                type="button"
                                ref={(node) => {
                                  if (node) renameButtonRefs.current.set(conversation.id, node);
                                  else renameButtonRefs.current.delete(conversation.id);
                                }}
                                disabled={busy}
                                aria-label={t("list.renameAriaLabel", {
                                  title: conversationLabel(conversation),
                                })}
                                title={t("list.renameAriaLabel", {
                                  title: conversationLabel(conversation),
                                })}
                                onClick={() => {
                                  setRenamingId(conversation.id);
                                  setRenameDraft(conversation.title ?? "");
                                }}
                                className="shrink-0 rounded px-1 py-0.5 text-meta text-content-muted hover:bg-surface-overlay disabled:opacity-50"
                              >
                                {t("list.rename")}
                              </button>
                            )}
                            <button
                              type="button"
                              disabled={busy}
                              aria-label={conversation.pinned ? t("list.unpin") : t("list.pin")}
                              title={conversation.pinned ? t("list.unpin") : t("list.pin")}
                              onClick={() => void togglePin(conversation)}
                              className={[
                                "shrink-0 rounded px-1 py-0.5 text-meta hover:bg-surface-overlay disabled:opacity-50",
                                conversation.pinned ? "text-accent" : "text-content-muted",
                              ].join(" ")}
                            >
                              {t(conversation.pinned ? "list.unpin" : "list.pin")}
                            </button>
                            <button
                              type="button"
                              disabled={busy}
                              aria-label={archived ? t("list.unarchive") : t("list.archive")}
                              title={archived ? t("list.unarchive") : t("list.archive")}
                              onClick={() => void toggleArchive(conversation)}
                              className="shrink-0 rounded px-1 py-0.5 text-meta text-content-muted hover:bg-surface-overlay disabled:opacity-50"
                            >
                              {t(archived ? "list.unarchive" : "list.archive")}
                            </button>
                            <button
                              type="button"
                              disabled={busy}
                              aria-label={t("list.fork")}
                              title={t("list.fork")}
                              onClick={() => void forkConversationById(conversation)}
                              className="shrink-0 rounded px-1 py-0.5 text-meta text-content-muted hover:bg-surface-overlay disabled:opacity-50"
                            >
                              {busy ? t("list.forking") : t("list.fork")}
                            </button>
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                )}
              </div>
              {/* Model and effort used to sit here. Four controls did not fit
                  the 48px header: both selects are native, so they take their
                  intrinsic width from the widest option and carried shrink-0,
                  which let them overrun the conversation picker beside them
                  instead of yielding — the connection state and the picker
                  painted over each other at panel width. They now live in the
                  composer row, which is also where they are changed. */}
            </div>
          </div>
          <button
            type="button"
            className="focus-ring flex h-8 w-8 items-center justify-center rounded text-content-muted transition-colors hover:bg-surface-overlay hover:text-content-primary"
            aria-label={t("close")}
            title={t("close")}
            onClick={onClose}
          >
            <IconClose size={15} />
          </button>
        </header>

        <div
          ref={feedRef}
          onScroll={handleScroll}
          className="relative min-h-0 flex-1 overflow-y-auto px-3 py-4"
          aria-live="polite"
          aria-busy={state.loadState === "loading" || sending}
        >
          {state.loadState === "loading" && state.frames.length === 0 && (
            <div className="space-y-3" aria-label={t("loading")}>
              <div className="skeleton h-3 w-28 rounded" />
              <div className="skeleton h-16 w-full rounded-lg" />
              <div className="skeleton h-3 w-40 rounded" />
            </div>
          )}

          {fatalError && (
            <div className="flex min-h-full flex-col items-center justify-center px-4 text-center">
              <IconError size={24} className="text-status-failure" />
              <h3 className="mt-3 text-label font-semibold text-content-primary">
                {t("error.title")}
              </h3>
              <p className="mt-1 max-w-64 text-body leading-relaxed text-content-muted">
                {state.error ?? t("errors.load")}
              </p>
              <div className="mt-4 flex gap-2">
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => {
                    // The conversation whose load failed, else the stored one.
                    const id = selectedConversationIdRef.current ?? readStoredConversation();
                    if (!id) return;
                    operatorChoicesRef.current += 1;
                    void loadConversation(id);
                  }}
                >
                  {t("retry")}
                </Button>
                <Button variant="secondary" size="sm" onClick={resetConversation}>
                  {t("newConversation")}
                </Button>
              </div>
            </div>
          )}

          {empty && !fatalError && (
            <div className="flex min-h-full flex-col items-center justify-center px-4 pb-8 text-center">
              <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-edge bg-surface-raised text-accent">
                <IconShield size={20} />
              </div>
              <h3 className="mt-4 text-[length:var(--t-lg)] font-semibold text-content-primary">
                {t("empty.title")}
              </h3>
              <p className="mt-2 max-w-72 text-body leading-relaxed text-content-muted">
                {t("empty.body")}
              </p>
              <p className="mt-3 font-data text-meta text-content-muted">{t("empty.example")}</p>
            </div>
          )}

          {!fatalError && items.length > 0 && (
            <div className="space-y-4">
              {items.length > visibleItems.length && (
                <div className="flex justify-center pb-1">
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                      anchorHeightRef.current = feedRef.current?.scrollHeight ?? null;
                      setVisibleCount((count) => Math.min(items.length, count + 200));
                    }}
                  >
                    {t("loadOlder")}
                  </Button>
                </div>
              )}
              {visibleItems.map((item) => {
                if (item.kind === "text") return <TextMessage key={item.key} item={item} />;
                const proposalId =
                  item.frame.type === "proposal"
                    ? (item.frame.payload as OperatorProposalPayload).proposal.id
                    : "";
                return (
                  <FrameCard
                    key={item.key}
                    frame={item.frame}
                    proposalResolved={resolvedProposalIds.has(proposalId)}
                    deciding={deciding.has(proposalId)}
                    onProposalDecision={handleProposalDecision}
                  />
                );
              })}
            </div>
          )}
          <div ref={endRef} />
        </div>

        {showJump && (
          <button
            type="button"
            className="focus-ring absolute bottom-28 end-4 rounded-full border border-edge bg-surface-overlay px-3 py-1.5 text-meta font-medium text-content-primary shadow-card"
            onClick={jumpToLatest}
          >
            {t("jumpToLatest")}
          </button>
        )}

        <footer className="shrink-0 border-t border-edge bg-surface-raised p-3">
          {(actionError || state.error) && (
            <div
              role="alert"
              className="mb-2 flex items-start gap-1.5 rounded border border-status-failure/30 bg-status-error-bg px-2 py-1.5 text-meta text-status-failure"
            >
              <IconError size={12} className="mt-0.5 shrink-0" />
              <span className="min-w-0 flex-1 break-words">
                {actionError?.message ?? state.error}
              </span>
            </div>
          )}
          <label htmlFor="operator-instruction" className="sr-only">
            {t("composer.label")}
          </label>
          <div className="rounded-lg border border-edge bg-surface-base transition-colors focus-within:border-edge-strong focus-within:shadow-[var(--focus-ring)]">
            <textarea
              id="operator-instruction"
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              onKeyDown={(event) => {
                if (event.nativeEvent.isComposing) return;
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void handleSend();
                }
              }}
              rows={2}
              maxLength={32_768}
              disabled={Boolean(state.activeRequestId) || sending || fatalError}
              placeholder={
                state.activeRequestId ? t("composer.busyPlaceholder") : t("composer.placeholder")
              }
              className="block max-h-40 min-h-14 w-full resize-none bg-transparent px-3 py-2 text-body leading-relaxed text-content-primary outline-none placeholder:text-content-muted disabled:cursor-not-allowed disabled:opacity-60"
            />
            <div className="flex items-center justify-between gap-2 px-2 pb-2">
              <span className="min-w-0 truncate font-data text-meta text-content-muted">
                {t("composer.hint")}
              </span>
              <div className="flex shrink-0 items-center gap-1.5">
                <select
                  aria-label={t("model.label")}
                  aria-describedby={selectedModelEntry ? "operator-model-consequence" : undefined}
                  title={t("model.label")}
                  value={model}
                  onChange={(event) => {
                    setModel(event.target.value);
                    // A previous model's effort selection may not exist on the
                    // new one; clear it explicitly rather than carrying a value
                    // effortChoices no longer offers.
                    setEffort("");
                  }}
                  className="max-w-32 border-0 bg-transparent py-0 font-data text-meta text-content-muted outline-none focus:text-content-primary"
                >
                  <option value="">{t("model.default")}</option>
                  {modelGroups.map((group) => (
                    <optgroup
                      key={group.provider}
                      label={t("model.recommendedGroup", {
                        provider: OPERATOR_PROVIDER_LABELS[group.provider],
                      })}
                    >
                      {group.models.map((entry) => (
                        <option key={entry.id} value={entry.id}>
                          {entry.efforts.length > 0
                            ? `${entry.label} · ${entry.efforts.join(" / ")}`
                            : entry.label}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                  {model && !selectedModelEntry && (
                    <optgroup label={t("model.legacyGroup")}>
                      <option value={model}>{t("model.unavailable", { model })}</option>
                    </optgroup>
                  )}
                </select>
                {effortChoices.length > 0 && (
                  // Effort is the first thing to go when the row gets tight. It
                  // is the narrower choice of the two, and which model answered
                  // is the question people actually ask of a reply, so model
                  // stays visible at every width.
                  <select
                    aria-label={t("effort.label")}
                    title={t("effort.label")}
                    value={effectiveEffort}
                    onChange={(event) => setEffort(event.target.value as OperatorEffort)}
                    className="hidden max-w-24 border-0 bg-transparent py-0 font-data text-meta text-content-muted outline-none focus:text-content-primary sm:block"
                  >
                    <option value="">{t("effort.default")}</option>
                    {effortChoices.map((choice) => (
                      <option key={choice} value={choice}>
                        {choice}
                      </option>
                    ))}
                  </select>
                )}
                <label
                  title={t("composer.autoAllow")}
                  className="hidden shrink-0 cursor-pointer items-center gap-1 font-data text-meta text-content-muted hover:text-content-primary sm:flex"
                >
                  <input
                    type="checkbox"
                    checked={autoAllow}
                    onChange={toggleAutoAllow}
                    className="h-3 w-3 accent-[var(--accent)]"
                  />
                  {t("composer.autoAllow")}
                </label>
              </div>
              {state.activeRequestId ? (
                // Send is disabled while a turn runs, so the same slot becomes
                // Stop. Keeping it here rather than in the header puts the
                // control where the eye already is, and leaves exactly one
                // way to stop a turn.
                <Button
                  variant="danger"
                  size="sm"
                  leading={<IconPause size={12} />}
                  disabled={stopping}
                  onClick={() => void handleStop()}
                >
                  {stopping ? t("stopping") : t("stop")}
                </Button>
              ) : (
                <Button
                  variant="primary"
                  size="sm"
                  trailing={<IconArrowRight size={12} />}
                  disabled={!instruction.trim() || sending || fatalError}
                  onClick={() => void handleSend()}
                >
                  {sending ? t("composer.sending") : t("composer.send")}
                </Button>
              )}
            </div>
            {selectedModelEntry && (
              <p
                id="operator-model-consequence"
                data-testid="operator-model-consequence"
                className="-mt-1 flex flex-wrap items-center gap-x-1.5 px-2 pb-2 font-data text-[length:var(--t-xs)] leading-tight text-content-muted"
              >
                <span>{OPERATOR_PROVIDER_LABELS[selectedModelEntry.provider]}</span>
                <span aria-hidden>·</span>
                <span>
                  {t("model.efforts", { efforts: selectedModelEntry.efforts.join(" / ") })}
                </span>
                <span aria-hidden>·</span>
                <span>
                  {selectedModelEntry.provider === "gemini_code"
                    ? t("model.effortInModel")
                    : t("model.effortAsSetting")}
                </span>
              </p>
            )}
          </div>
        </footer>
      </aside>
    </>
  );
}
