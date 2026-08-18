import type {
  AgentProfile,
  AgentProfileSummary,
  ArtifactContract,
  ArtifactVerification,
  DeclarativeArgSpec,
  DeclarativePlaybookData,
  PlaybookFormat,
  ProjectDetail,
  ProjectSummary,
  OperatorConversation,
  OperatorContextSnapshot,
  OperatorConversationSnapshot,
  OperatorFrame,
  OperatorFrameType,
  OperatorModelCatalog,
  OperatorProposalResult,
  OperatorTurnAccepted,
  OperatorTurnRequest,
  ResumeAvailability,
  RunResumeRequest,
  RunResumeResponse,
  RunSummary,
  ScheduleDetail,
  ScheduleRunSummary,
  ScheduleSummary,
  ShowDetail,
  ShowEvent,
  ShowSummary,
  WorkerFormData,
  WorkerGraph,
  WorkerRaw,
  WorkerStepNode,
  WorkerLinkEdge,
} from "./types";
import { reportConnectivityFailure } from "./connectivity";

declare global {
  interface Window {
    __STUDIO_API_BASE__?: string;
    __STUDIO_AUTH_TOKEN__?: string;
  }
}

/** Return the per-launch bearer token injected by the desktop shell, if any. */
export function resolveAuthToken(): string | undefined {
  if (typeof window !== "undefined" && window.__STUDIO_AUTH_TOKEN__) {
    return window.__STUDIO_AUTH_TOKEN__;
  }
  return undefined;
}

export function resolveApiBase(): string {
  // Priority: window.__STUDIO_API_BASE__ (runtime injection) >
  // VITE_STUDIO_API_BASE (build-time env) > origin logic.
  // Treat empty string as "not configured" — defense against baking an empty
  // env var that silently produced same-origin /api/* requests.
  if (typeof window !== "undefined" && window.__STUDIO_API_BASE__) {
    return window.__STUDIO_API_BASE__;
  }
  const viteEnv = import.meta.env.VITE_STUDIO_API_BASE as string | undefined;
  if (viteEnv) return viteEnv;
  if (typeof window !== "undefined") {
    const port = window.location.port;
    // Vite dev-server ports use the configured same-origin proxy. Keeping the
    // browser on /api makes STUDIO_API_URL and the isolated E2E daemon target
    // effective without requiring CORS on the backend.
    if (port === "3000" || port === "5173") {
      return "";
    }
    // Every other browser origin — including HTTPS on a non-local hostname —
    // is treated as a same-origin deployment (Docker/reverse-proxy serving
    // the SPA and API from one origin). Hosted-static deploys that need to
    // reach a separate local daemon must set window.__STUDIO_API_BASE__ or
    // VITE_STUDIO_API_BASE explicitly rather than relying on a hostname guess.
    return "";
  }
  // SSR / test environment without window: fall back to localhost for compat.
  return "http://localhost:8765";
}

export const API_BASE = resolveApiBase();

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  readonly code?: string;
  readonly retryable?: boolean;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    if (detail != null && typeof detail === "object" && !Array.isArray(detail)) {
      const record = detail as Record<string, unknown>;
      if (typeof record.code === "string") this.code = record.code;
      if (typeof record.retryable === "boolean") this.retryable = record.retryable;
    }
  }
}

// How many field errors to name before switching to a count. A request that
// fails validation on a dozen fields is still one mistake to the person reading
// it, and a dozen clauses is harder to act on than three plus a number.
const MAX_VALIDATION_ERRORS_SHOWN = 3;
const SAFE_HTTP_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const RETRYABLE_SSE_CLIENT_STATUSES = new Set([408, 425, 429]);

/**
 * Render a FastAPI/Pydantic validation body into one readable sentence.
 *
 * A 422 arrives with `detail` as an array of `{loc, msg}` entries. That is the
 * one shape the object branch cannot read, so without this the reader gets
 * "Request failed: 422" and no way to learn which field was rejected.
 *
 * Returns undefined when the array carries nothing readable, so the caller
 * falls back to the status code instead of showing a confidently empty message.
 */
function formatValidationErrors(detail: unknown[]): string | undefined {
  const parts: string[] = [];
  for (const entry of detail) {
    if (entry == null || typeof entry !== "object") continue;
    const record = entry as Record<string, unknown>;
    if (typeof record.msg !== "string" || !record.msg) continue;
    // Drop the leading request-part segment ("body", "query", ...): it repeats
    // what the caller already knows and pushes the useful field name rightward.
    const loc = Array.isArray(record.loc) ? record.loc.slice(1) : [];
    const path = loc.filter((seg) => typeof seg === "string" || typeof seg === "number").join(".");
    parts.push(path ? `${path}: ${record.msg}` : record.msg);
  }
  if (parts.length === 0) return undefined;
  const shown = parts.slice(0, MAX_VALIDATION_ERRORS_SHOWN);
  const hidden = parts.length - shown.length;
  return hidden > 0 ? `${shown.join("; ")} (+${hidden} more)` : shown.join("; ");
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${API_BASE}${path}`;

  // Preserve object-shaped headers for existing callers/tests, while making
  // Headers and tuple-list inputs mutable too.
  const initialHeaders = init?.headers;
  const headers: HeadersInit =
    initialHeaders instanceof Headers || Array.isArray(initialHeaders)
      ? new Headers(initialHeaders)
      : { ...(initialHeaders ?? {}) };
  const setHeader = (name: string, value: string) => {
    if (headers instanceof Headers) headers.set(name, value);
    else (headers as Record<string, string>)[name] = value;
  };

  // Every unsafe API call declares JSON, including bodyless POST/DELETE
  // actions. Besides matching the backend contract, this makes browser calls
  // non-simple requests so cross-site forms cannot invoke mutating routes.
  const method = (init?.method ?? "GET").toUpperCase();
  if (!SAFE_HTTP_METHODS.has(method) && !new Headers(headers).has("content-type")) {
    setHeader("Content-Type", "application/json");
  }

  // Attach the desktop-shell bearer token when present.
  const token = resolveAuthToken();
  if (token) {
    setHeader("Authorization", `Bearer ${token}`);
  }

  let response: Response;
  try {
    response = await fetch(url, { redirect: "follow", ...init, headers });
  } catch (err) {
    // fetch() itself only throws for network-level failures (daemon not
    // running, CORS, DNS) — never for HTTP error statuses. Let NoDaemonGate
    // re-probe /health right away instead of waiting for its own poll tick.
    reportConnectivityFailure();
    throw err;
  }
  if (!response.ok) {
    // Preserve the backend `detail` field (FastAPI/Pydantic validation errors,
    // our structured 409 body, etc.) so callers can surface it to the operator.
    // Falls back to the status code when the body is not JSON or has no detail.
    let detail: unknown;
    try {
      const body = (await response.json()) as { detail?: unknown };
      detail = body?.detail;
    } catch {
      // not JSON — ignore
    }
    const fallback = `Request failed: ${response.status}`;
    let message: string;
    if (typeof detail === "string") {
      message = detail;
    } else if (Array.isArray(detail)) {
      message = formatValidationErrors(detail) ?? fallback;
    } else if (detail != null && typeof detail === "object") {
      const record = detail as Record<string, unknown>;
      message = typeof record.message === "string" ? record.message : fallback;
    } else {
      message = fallback;
    }
    throw new ApiError(response.status, message, detail);
  }
  // 204/empty-body responses have nothing to parse — return as-is (matches
  // callers that type these as `unknown`/`{ ok: boolean }` but never actually
  // read a JSON body for a No Content response).
  if (response.status === 204) {
    return undefined as T;
  }
  // Static deploys with no backend (e.g. Vercel serving only the SPA dist/)
  // rewrite every unmatched path — including /api/* — to index.html with a
  // 200. response.ok is then true and response.json() throws a cryptic,
  // engine-specific parse error on the HTML document. Detect it here so
  // every caller gets one clear message instead of chasing this per view.
  const contentType = response.headers.get("content-type") ?? "";
  const looksJson = contentType.includes("json");
  if (!looksJson) {
    const text = await response.text();
    if (!text) {
      // Empty body without a JSON content-type — nothing to parse.
      return undefined as T;
    }
    if (/^\s*<(!doctype html|html)/i.test(text)) {
      throw new Error(
        `Studio API returned HTML instead of JSON for ${path} — the API base is likely unconfigured for this deployment (set VITE_STUDIO_API_BASE at build time or window.__STUDIO_API_BASE__ at runtime).`,
      );
    }
    return JSON.parse(text) as T;
  }
  return response.json() as Promise<T>;
}

// Fetch-based server-sent-events subscription. Native EventSource cannot
// attach the Authorization header the desktop shell's per-launch bearer token
// requires; fetch + ReadableStream can. Mirrors EventSource semantics for the
// studio endpoints: unnamed `data: <json>\n\n` frames, auto-reconnect after
// 2s unless closed. Callers parse the JSON and call the returned closer on
// their terminal "done" frame.
function sseSubscribe(
  path: string | (() => string),
  onData: (data: string, eventId?: string) => void,
): () => void {
  const controller = new AbortController();
  let closed = false;
  const close = () => {
    closed = true;
    controller.abort();
  };

  void (async () => {
    while (!closed) {
      try {
        const token = resolveAuthToken();
        const headers: Record<string, string> = { Accept: "text/event-stream" };
        if (token) headers["Authorization"] = `Bearer ${token}`;
        const requestPath = typeof path === "function" ? path() : path;
        const response = await fetch(`${API_BASE}${requestPath}`, {
          headers,
          signal: controller.signal,
        });
        if (!response.ok) {
          const permanentClientError =
            response.status >= 400 &&
            response.status < 500 &&
            !RETRYABLE_SSE_CLIENT_STATUSES.has(response.status);
          if (permanentClientError) {
            // Authentication, authorization, validation, and missing-resource
            // failures cannot heal on a timer. Stop instead of hammering the
            // daemon every two seconds for the lifetime of the page.
            closed = true;
            break;
          }
          throw new Error(`SSE request failed: ${response.status}`);
        }
        if (!response.body) {
          throw new Error(`SSE request failed: ${response.status}`);
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let sep: number;
          while ((sep = buffer.indexOf("\n\n")) !== -1) {
            const frame = buffer.slice(0, sep);
            buffer = buffer.slice(sep + 2);
            const data = frame
              .split("\n")
              .filter((line) => line.startsWith("data:"))
              .map((line) => line.slice(5).replace(/^ /, ""))
              .join("\n");
            const eventId = frame
              .split("\n")
              .filter((line) => line.startsWith("id:"))
              .map((line) => line.slice(3).replace(/^ /, ""))
              .at(-1);
            if (data && !closed) onData(data, eventId || undefined);
          }
        }
      } catch {
        // Aborted by close(), or a network error worth retrying.
      }
      if (!closed) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }
    }
  })();

  return close;
}

// ─── Operator conversations (ADR-0083 v1) ──────────────────────────────────

const OPERATOR_FRAME_TYPES = new Set<OperatorFrameType>([
  "text",
  "tool_call",
  "tool_result",
  "ui_command",
  "proposal",
  "confirmation",
  "error",
  "done",
]);

type RawRecord = Record<string, unknown>;

function asRecord(value: unknown): RawRecord {
  return value != null && typeof value === "object" && !Array.isArray(value)
    ? (value as RawRecord)
    : {};
}

function normalizeOperatorConversation(value: unknown): OperatorConversation {
  const raw = asRecord(value);
  const id = raw.id;
  if (typeof id !== "string" || !id) {
    throw new Error("Operator conversation response did not include an id.");
  }
  const status =
    raw.status === "archived" || raw.status === "deleted" ? raw.status : ("active" as const);
  const readNumber = (camel: string, snake: string): number | undefined => {
    const candidate = raw[camel] ?? raw[snake];
    return typeof candidate === "number" ? candidate : undefined;
  };
  const readString = (camel: string, snake: string): string | undefined => {
    const candidate = raw[camel] ?? raw[snake];
    return typeof candidate === "string" ? candidate : undefined;
  };
  return {
    id,
    status,
    pinned: raw.pinned === true,
    project: typeof raw.project === "string" ? raw.project : null,
    title: typeof raw.title === "string" ? raw.title : null,
    nextSequence: readNumber("nextSequence", "next_sequence"),
    activeRequestId: readString("activeRequestId", "active_request_id") ?? null,
    // The pinned selection must survive normalization: dropping it here is
    // what made the composer fall back to "Default" on every page refresh
    // even though the store kept the pin the whole time.
    provider: readString("provider", "provider") ?? null,
    providerModel: readString("providerModel", "provider_model") ?? null,
    createdAt: readNumber("createdAt", "created_at"),
    updatedAt: readNumber("updatedAt", "updated_at"),
  };
}

export function isOperatorFrame(value: unknown): value is OperatorFrame {
  const raw = asRecord(value);
  return (
    raw.version === 1 &&
    typeof raw.conversationId === "string" &&
    typeof raw.requestId === "string" &&
    typeof raw.sequence === "number" &&
    Number.isInteger(raw.sequence) &&
    raw.sequence >= 1 &&
    typeof raw.type === "string" &&
    OPERATOR_FRAME_TYPES.has(raw.type as OperatorFrameType) &&
    raw.payload != null &&
    typeof raw.payload === "object" &&
    typeof raw.createdAt === "number"
  );
}

export async function createOperatorConversation(input?: {
  project?: string | null;
  title?: string | null;
}): Promise<OperatorConversation> {
  const response = await fetchJson<unknown>("/api/operator/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input ?? {}),
  });
  const raw = asRecord(response);
  return normalizeOperatorConversation(raw.conversation ?? raw);
}

export async function listOperatorConversations(options?: {
  status?: "active" | "archived" | "all";
}): Promise<OperatorConversation[]> {
  const query = options?.status ? `?status=${encodeURIComponent(options.status)}` : "";
  const response = await fetchJson<unknown>(`/api/operator/conversations${query}`);
  const raw = asRecord(response);
  if (!Array.isArray(raw.conversations)) {
    throw new Error("Operator conversation list response was invalid.");
  }
  return raw.conversations.map(normalizeOperatorConversation);
}

export interface OperatorConversationPatch {
  title?: string | null;
  pinned?: boolean;
  status?: "active" | "archived";
}

export async function updateOperatorConversation(
  conversationId: string,
  patch: OperatorConversationPatch,
): Promise<OperatorConversation> {
  const body: Record<string, unknown> = {};
  if ("title" in patch) body.title = patch.title;
  if ("pinned" in patch) body.pinned = patch.pinned;
  if ("status" in patch) body.status = patch.status;
  const response = await fetchJson<unknown>(
    `/api/operator/conversations/${encodeURIComponent(conversationId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  const raw = asRecord(response);
  return normalizeOperatorConversation(raw.conversation ?? raw);
}

export async function forkOperatorConversation(
  conversationId: string,
  options?: { upToSequence?: number; title?: string },
): Promise<OperatorConversationSnapshot> {
  const body: Record<string, unknown> = {};
  if (options?.upToSequence != null) body.upToSequence = options.upToSequence;
  if (options?.title != null) body.title = options.title;
  const response = await fetchJson<unknown>(
    `/api/operator/conversations/${encodeURIComponent(conversationId)}/fork`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  const raw = asRecord(response);
  const conversation = normalizeOperatorConversation(raw.conversation ?? raw);
  const framesValue = raw.frames;
  const page = Array.isArray(framesValue) ? framesValue : [];
  for (const frame of page) {
    if (!isOperatorFrame(frame)) {
      throw new Error("Operator fork response contains an unsupported protocol frame.");
    }
  }
  return { conversation, frames: page };
}

export async function getOperatorConversation(
  conversationId: string,
): Promise<OperatorConversationSnapshot> {
  const pageSize = 1000;
  let afterSequence = 0;
  let conversation: OperatorConversation | null = null;
  const frames: OperatorFrame[] = [];

  for (;;) {
    const response = await fetchJson<unknown>(
      `/api/operator/conversations/${encodeURIComponent(conversationId)}?after_sequence=${afterSequence}&limit=${pageSize}`,
    );
    const raw = asRecord(response);
    conversation = normalizeOperatorConversation(raw.conversation ?? raw);
    const framesValue = raw.frames ?? raw.events;
    const page = Array.isArray(framesValue) ? framesValue : [];
    for (const frame of page) {
      if (!isOperatorFrame(frame)) {
        throw new Error("Operator history contains an unsupported protocol frame.");
      }
    }
    frames.push(...page);
    const hasMore = typeof raw.hasMore === "boolean" ? raw.hasMore : page.length >= pageSize;
    if (!hasMore) break;
    const reportedNext = raw.nextAfterSequence;
    const nextSequence =
      typeof reportedNext === "number"
        ? reportedNext
        : Math.max(...page.map((frame) => (frame as OperatorFrame).sequence));
    if (nextSequence <= afterSequence) {
      throw new Error("Operator history pagination did not advance.");
    }
    afterSequence = nextSequence;
  }

  if (!conversation) {
    throw new Error("Operator conversation response was empty.");
  }
  return { conversation, frames };
}

export async function submitOperatorTurn(
  conversationId: string,
  request: OperatorTurnRequest,
): Promise<OperatorTurnAccepted> {
  return fetchJson<OperatorTurnAccepted>(
    `/api/operator/conversations/${encodeURIComponent(conversationId)}/turns`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        instruction: request.instruction,
        context: request.context,
        expected_last_sequence: request.expectedLastSequence,
        ...(request.model ? { model: request.model } : {}),
        ...(request.provider ? { provider: request.provider } : {}),
        ...(request.effort ? { effort: request.effort } : {}),
        ...(request.clearSelection ? { clear_selection: true } : {}),
      }),
    },
  );
}

/**
 * Report where the human is now, outside of any turn.
 *
 * A turn's context is frozen when it is submitted, so without this the
 * Operator answers "where am I" with wherever they were when they hit send.
 * Best effort by design: a dropped report costs freshness, never correctness,
 * because the tool falls back to the turn's own snapshot and labels it.
 */
export async function reportOperatorView(
  conversationId: string,
  context: OperatorContextSnapshot & { observationSeq: number; observerId: string },
): Promise<void> {
  await fetchJson<unknown>(
    `/api/operator/conversations/${encodeURIComponent(conversationId)}/view`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(context),
    },
  );
}

export async function fetchOperatorModelCatalog(): Promise<OperatorModelCatalog> {
  return fetchJson<OperatorModelCatalog>("/api/operator/models");
}

export async function cancelOperatorRequest(
  conversationId: string,
  requestId: string,
): Promise<void> {
  await fetchJson<unknown>(
    `/api/operator/conversations/${encodeURIComponent(conversationId)}/requests/${encodeURIComponent(requestId)}/cancel`,
    { method: "POST" },
  );
}

export type OperatorEffectRejectionCode =
  | "unsupported"
  | "invalid_params"
  | "stale_context"
  | "not_visible"
  | "client_error";

export async function acknowledgeOperatorEffect(
  conversationId: string,
  effectId: string,
  acknowledgement:
    | { status: "applied"; clientRoute: string }
    | {
        status: "rejected";
        clientRoute?: string;
        rejectionCode: OperatorEffectRejectionCode;
      },
): Promise<{ effectId: string; status: "applied" | "rejected" }> {
  return fetchJson(
    `/api/operator/conversations/${encodeURIComponent(conversationId)}/effects/${encodeURIComponent(effectId)}/ack`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(acknowledgement),
    },
  );
}

export async function confirmOperatorProposal(
  conversationId: string,
  proposalId: string,
  expectedCommandHash: string,
  expectedTargetVersion?: string | null,
): Promise<OperatorProposalResult> {
  return fetchJson<OperatorProposalResult>(
    `/api/operator/conversations/${encodeURIComponent(conversationId)}/proposals/${encodeURIComponent(proposalId)}/confirm`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expectedCommandHash,
        expectedTargetVersion: expectedTargetVersion ?? null,
      }),
    },
  );
}

export async function decideOperatorProposal(
  conversationId: string,
  proposalId: string,
  decision: "allow" | "deny",
  expectedCommandHash?: string,
  expectedTargetVersion?: string | null,
): Promise<OperatorProposalResult> {
  return fetchJson<OperatorProposalResult>(
    `/api/operator/conversations/${encodeURIComponent(conversationId)}/proposals/${encodeURIComponent(proposalId)}/decision`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        decision,
        ...(expectedCommandHash ? { expectedCommandHash } : {}),
        expectedTargetVersion: expectedTargetVersion ?? null,
      }),
    },
  );
}

export interface OperatorSseChunk {
  data: string[];
  rest: string;
}

/**
 * Consume complete SSE records while retaining a possibly-fragmented tail.
 * Handles both LF and CRLF records and joins multi-line data fields.
 */
export function consumeOperatorSse(input: string): OperatorSseChunk {
  const data: string[] = [];
  let rest = input;
  for (;;) {
    const match = /\r?\n\r?\n/.exec(rest);
    if (!match || match.index == null) break;
    const record = rest.slice(0, match.index);
    rest = rest.slice(match.index + match[0].length);
    const payload = record
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, ""))
      .join("\n");
    if (payload) data.push(payload);
  }
  return { data, rest };
}

export type OperatorStreamConnection = "connecting" | "open" | "reconnecting";

export interface OperatorStreamHandlers {
  onFrame: (frame: OperatorFrame) => void;
  onConnection?: (state: OperatorStreamConnection) => void;
  onError?: (error: Error, fatal: boolean) => void;
}

/**
 * Authenticated, replayable SSE subscription. The cursor advances only after
 * a validated v1 frame and is sent on every reconnect, so delivery may repeat
 * but never silently skips a durable frame.
 */
export function streamOperatorConversation(
  conversationId: string,
  afterSequence: number,
  handlers: OperatorStreamHandlers,
): () => void {
  let closed = false;
  let cursor = Math.max(0, afterSequence);
  let controller: AbortController | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  const wait = (delay: number) =>
    new Promise<void>((resolve) => {
      retryTimer = setTimeout(() => {
        retryTimer = null;
        resolve();
      }, delay);
    });

  const close = () => {
    closed = true;
    controller?.abort();
    if (retryTimer) clearTimeout(retryTimer);
  };

  void (async () => {
    let retryMs = 750;
    let firstAttempt = true;
    while (!closed) {
      handlers.onConnection?.(firstAttempt ? "connecting" : "reconnecting");
      controller = new AbortController();
      try {
        const query = new URLSearchParams({ after_sequence: String(cursor) });
        const headers: Record<string, string> = { Accept: "text/event-stream" };
        const token = resolveAuthToken();
        if (token) headers.Authorization = `Bearer ${token}`;
        const response = await fetch(
          `${API_BASE}/api/operator/conversations/${encodeURIComponent(conversationId)}/stream?${query}`,
          { headers, signal: controller.signal },
        );
        if (!response.ok || !response.body) {
          const fatal =
            response.status < 500 &&
            response.status !== 408 &&
            response.status !== 425 &&
            response.status !== 429;
          const error = new Error(`Operator stream request failed: ${response.status}`);
          handlers.onError?.(error, fatal);
          if (fatal) {
            closed = true;
            break;
          }
          throw error;
        }

        handlers.onConnection?.("open");
        retryMs = 750;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!closed) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const chunk = consumeOperatorSse(buffer);
          buffer = chunk.rest;
          for (const payload of chunk.data) {
            let candidate: unknown;
            try {
              candidate = JSON.parse(payload);
            } catch {
              const error = new Error("Operator stream sent malformed JSON.");
              handlers.onError?.(error, true);
              closed = true;
              controller.abort();
              break;
            }
            if (!isOperatorFrame(candidate)) {
              const error = new Error("Operator stream sent an unsupported protocol frame.");
              handlers.onError?.(error, true);
              closed = true;
              controller.abort();
              break;
            }
            if (candidate.conversationId !== conversationId) {
              const error = new Error("Operator stream frame belongs to another conversation.");
              handlers.onError?.(error, true);
              closed = true;
              controller.abort();
              break;
            }
            handlers.onFrame(candidate);
            // Advanced after the handler, for the reason the signal stream
            // advances after its consumer: a throwing handler is caught below
            // and reconnects from this cursor, so advancing first drops the
            // frame it never handled.
            cursor = Math.max(cursor, candidate.sequence);
          }
        }
      } catch (error) {
        if (closed || controller.signal.aborted) break;
        reportConnectivityFailure();
        handlers.onError?.(
          error instanceof Error ? error : new Error("Operator stream disconnected."),
          false,
        );
      }
      if (!closed) {
        firstAttempt = false;
        await wait(retryMs);
        retryMs = Math.min(Math.round(retryMs * 1.8), 10_000);
      }
    }
  })();

  return close;
}

// ─── Runs ─────────────────────────────────────────────────────────────────────

export type RunSort = "recent" | "cost";

export interface RunListParams {
  page?: number;
  per_page?: number;
  status?: string[];
  /** Orchestration-kind facet: agent | play | flow | fanout | show. */
  kind?: string[];
  playbook?: string;
  project?: string;
  project_null?: boolean;
  search?: string;
  /** "recent" (default) or "cost" — highest reported spend first, server-side. */
  sort?: RunSort;
}

export interface RunListResponse {
  runs: RunSummary[];
  page: number;
  per_page: number;
  total: number;
  total_pages: number;
  has_next: boolean;
  has_prev: boolean;
}

export async function listRuns(params?: RunListParams): Promise<RunListResponse> {
  const query = new URLSearchParams();
  if (params?.page != null) query.set("page", String(params.page));
  if (params?.per_page != null) query.set("per_page", String(params.per_page));
  if (params?.playbook) query.set("playbook", params.playbook);
  if (params?.project_null) {
    query.set("project_null", "true");
  } else if (params?.project) {
    query.set("project", params.project);
  }
  if (params?.search) query.set("search", params.search);
  if (params?.sort) query.set("sort", params.sort);
  for (const value of params?.status ?? []) query.append("status", value);
  for (const value of params?.kind ?? []) query.append("kind", value);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  // The daemon registers this list route with a trailing slash (unlike
  // /api/runs/{run_id}); omitting it triggers Starlette's redirect-with-
  // absolute-Location, which the browser then blocks as cross-origin
  // whenever the frontend is served from a different origin than the daemon.
  return fetchJson<RunListResponse>(`/api/runs/${suffix}`);
}

export interface RunProjectCount {
  project: string | null;
  count: number;
  last_activity: number | null;
}

export interface RunProjectsResponse {
  projects: RunProjectCount[];
  total: number;
}

/** Per-project run counts, sorted by last activity — feeds the fleet project
 * filter's option list without requiring a full unfiltered run scan. */
export async function listRunProjects(): Promise<RunProjectsResponse> {
  return fetchJson<RunProjectsResponse>("/api/runs/projects");
}

export async function resumeRun(
  runId: string,
  request: RunResumeRequest,
): Promise<RunResumeResponse> {
  return fetchJson<RunResumeResponse>(`/api/runs/${encodeURIComponent(runId)}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
}

// Read-only precheck (services/run_resume.py resume_availability) so the UI
// can determine resumability BEFORE rendering the resume action — a run
// with no checkpoint reads as an explicit, explained state rather than a
// dead or guessed-at control.
export async function getResumeAvailability(runId: string): Promise<ResumeAvailability> {
  return fetchJson<ResumeAvailability>(`/api/runs/${encodeURIComponent(runId)}/resume`);
}

export interface RunFileContent {
  path: string;
  content: string;
  size: number;
  truncated: boolean;
}

export type RunFileResult =
  | { ok: true; data: RunFileContent }
  | { ok: false; status: number; detail?: string };

/** Read-only fetch of a run artifact's content, for the message-renderer's
 * file viewer. Reports status/detail on failure (rather than throwing) so
 * the click-time 404/403 path renders a graceful missing-file state instead
 * of an unhandled error. */
export async function getRunFile(runId: string, path: string): Promise<RunFileResult> {
  const query = new URLSearchParams({ path });
  const url = `${API_BASE}/api/runs/${encodeURIComponent(runId)}/file?${query.toString()}`;
  const token = resolveAuthToken();
  const headers: HeadersInit = {};
  if (token) (headers as Record<string, string>)["Authorization"] = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(url, { headers });
  } catch (err) {
    reportConnectivityFailure();
    throw err;
  }
  if (!response.ok) {
    let detail: string | undefined;
    try {
      const body = (await response.json()) as { detail?: string };
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // not JSON — ignore
    }
    return { ok: false, status: response.status, detail };
  }
  return { ok: true, data: (await response.json()) as RunFileContent };
}

// ─── Workers (playbooks) ──────────────────────────────────────────────────────

interface PlaybookDetail {
  name: string;
  path?: string;
  description?: string;
  data?: Record<string, unknown>;
  raw?: string;
}

function parseGraphFromPlaybook(pb: PlaybookDetail): WorkerGraph {
  const data = pb.data ?? {};
  const stepsRaw = (data.steps as Record<string, unknown>) ?? {};
  const linksRaw = (data.links as Array<Record<string, unknown>>) ?? [];

  const nodes: WorkerStepNode[] = Object.entries(stepsRaw).map(([id, raw]) => {
    const s = (raw as Record<string, unknown>) ?? {};
    return {
      id,
      label: id,
      role: String(s.role ?? ""),
      assignment: String(s.assignment ?? ""),
      prompt: String(s.prompt ?? ""),
      capacity: Number(s.capacity ?? 1),
      timeout: s.timeout != null ? Number(s.timeout) : null,
      inputs: (s.inputs as string[]) ?? [],
      outputs: (s.outputs as string[]) ?? [],
    };
  });

  const edges: WorkerLinkEdge[] = linksRaw.map((l, i) => {
    const rawMode = String(l.mode ?? "simple");
    const mode: "simple" | "code" = rawMode === "code" ? "code" : "simple";
    return {
      id: `e-${i}`,
      source: String(l.from ?? ""),
      target: String(l.to ?? ""),
      mode,
      condition: l.condition != null ? String(l.condition) : undefined,
      map: (l.map as Record<string, string>) ?? undefined,
      handler: l.handler != null ? String(l.handler) : undefined,
    };
  });

  return {
    name: pb.name,
    description: String(data.description ?? pb.description ?? ""),
    nodes,
    edges,
  };
}

export async function getWorkerGraph(name: string): Promise<WorkerGraph> {
  const data = await fetchJson<PlaybookDetail>(`/api/playbooks/${encodeURIComponent(name)}`);
  return parseGraphFromPlaybook(data);
}

export async function getWorkerRaw(name: string): Promise<WorkerRaw> {
  return fetchJson<WorkerRaw>(`/api/playbooks/${encodeURIComponent(name)}`);
}

export async function createWorker(name: string, data: WorkerFormData): Promise<unknown> {
  return fetchJson<unknown>(`/api/playbooks/${encodeURIComponent(name)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function updateWorker(name: string, data: WorkerFormData): Promise<unknown> {
  return fetchJson<unknown>(`/api/playbooks/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function validateWorker(
  name: string,
  data: WorkerFormData,
): Promise<{ ok: boolean; errors?: string[] }> {
  return fetchJson<{ ok: boolean; errors?: string[] }>(
    `/api/playbooks/${encodeURIComponent(name)}/validate`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    },
  );
}

// ─── Declarative playbook format helpers ──────────────────────────────────────

/**
 * Inspect a raw playbook payload and decide which editor to render.
 *
 * - ``graph``: has ``steps`` or ``links`` keys with content
 * - ``declarative``: has ``agent`` and/or ``prompt`` and no steps/links
 * - default for empty/new playbooks: ``declarative`` (fewer required fields)
 */
export function detectPlaybookFormat(data: Record<string, unknown>): PlaybookFormat {
  const steps = data?.steps;
  const links = data?.links;
  const hasSteps =
    steps != null && typeof steps === "object" && Object.keys(steps as object).length > 0;
  const hasLinks = Array.isArray(links) && links.length > 0;
  if (hasSteps || hasLinks) return "graph";
  return "declarative";
}

/**
 * Map raw YAML payload → DeclarativePlaybookData shape the form binds to.
 */
export function rawToDeclarative(
  name: string,
  data: Record<string, unknown>,
): DeclarativePlaybookData {
  const argsRaw = (data.args as Record<string, Record<string, unknown>>) ?? {};
  const args: DeclarativeArgSpec[] = Object.entries(argsRaw).map(([argName, spec]) => ({
    name: argName,
    type: String(spec?.type ?? "str"),
    default: spec?.default != null ? String(spec.default) : "",
    help: String(spec?.help ?? ""),
  }));

  return {
    name,
    description: String(data.description ?? ""),
    agent: String(data.agent ?? ""),
    effort: String(data.effort ?? ""),
    maxOps: data["max-ops"] != null ? Number(data["max-ops"]) : null,
    prompt: String(data.prompt ?? ""),
    args,
    yolo: Boolean(data.yolo ?? false),
    showGraph: Boolean(data["show-graph"] ?? false),
    argumentHint: String(data["argument-hint"] ?? ""),
  };
}

/**
 * Convert DeclarativePlaybookData → wire payload for PUT /api/playbooks/{name}.
 * Uses the YAML key names (with hyphens) the backend expects.
 */
export function declarativeToPayload(data: DeclarativePlaybookData): Record<string, unknown> {
  const argsOut: Record<string, Record<string, unknown>> = {};
  for (const a of data.args) {
    const trimmed = a.name.trim();
    if (!trimmed) continue;
    const spec: Record<string, unknown> = { type: a.type || "str" };
    if (a.default !== "") spec.default = a.default;
    if (a.help) spec.help = a.help;
    argsOut[trimmed] = spec;
  }

  return {
    description: data.description,
    agent: data.agent || null,
    effort: data.effort || null,
    "max-ops": data.maxOps != null && Number.isFinite(data.maxOps) ? data.maxOps : null,
    prompt: data.prompt || null,
    args: Object.keys(argsOut).length > 0 ? argsOut : null,
    yolo: data.yolo,
    "show-graph": data.showGraph,
    "argument-hint": data.argumentHint || null,
  };
}

/**
 * Generic playbook update — accepts any partial dict, lets the backend merge.
 * Use this for declarative-format saves; graph saves continue to use
 * ``updateWorker`` since the wire shape is fully typed.
 */
export async function updatePlaybook(
  name: string,
  payload: Record<string, unknown>,
): Promise<unknown> {
  return fetchJson<unknown>(`/api/playbooks/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// ADR-0014: Run button is defaults-only. No task/cwd payload — the backend
// runs the playbook with its default configuration. Input binding and
// worktree customisation belong in `li play`.
export async function startRun(workerName: string): Promise<{ run_id: string }> {
  return fetchJson<{ run_id: string }>(`/api/playbooks/${encodeURIComponent(workerName)}/run`, {
    method: "POST",
  });
}

// ─── User playbooks (list) + built-in playbook templates ─────────────────────
//
// "Playbooks" are YAML CLI templates read from ~/.lionagi/playbooks (the same
// files `li play <name>` resolves). Built-in templates are read-only package
// data shipped with lionagi (examples/playbooks/, bundled) — Studio's onboarding
// set for the Workflows page. installBuiltinPlaybook() idempotently copies a
// template into the user's own playbooks dir; it never overwrites a copy the
// user has since customized.

export interface PlaybookSummary {
  name: string;
  path?: string;
  description?: string;
}

export async function listPlaybooks(): Promise<{ playbooks: PlaybookSummary[] }> {
  return fetchJson<{ playbooks: PlaybookSummary[] }>("/api/playbooks/");
}

export interface PlaybookArgSpec {
  type?: string;
  default?: unknown;
  help?: string;
}

export interface BuiltinPlaybookSummary {
  name: string;
  description: string;
  args: Record<string, PlaybookArgSpec>;
  argument_hint: string;
  installed: boolean;
}

export async function listBuiltinPlaybooks(): Promise<{ playbooks: BuiltinPlaybookSummary[] }> {
  return fetchJson<{ playbooks: BuiltinPlaybookSummary[] }>("/api/playbook-templates/");
}

export async function getBuiltinPlaybookRaw(name: string): Promise<WorkerRaw> {
  return fetchJson<WorkerRaw>(`/api/playbook-templates/${encodeURIComponent(name)}`);
}

export interface InstallBuiltinPlaybookResult {
  installed: boolean;
  playbook: PlaybookSummary;
}

export async function installBuiltinPlaybook(name: string): Promise<InstallBuiltinPlaybookResult> {
  return fetchJson<InstallBuiltinPlaybookResult>(
    `/api/playbook-templates/${encodeURIComponent(name)}/install`,
    { method: "POST" },
  );
}

// Real, working launch path for playbooks. startRun() above (POST
// /api/playbooks/{name}/run) is a 501 stub (# TODO(lift-backend-writes) in
// services/playbooks.py) — this goes through /api/launches instead.
export async function launchPlaybook(name: string): Promise<LaunchResult> {
  return fetchJson<LaunchResult>(`/api/launches/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action_kind: "play", action_playbook: name }),
  });
}

// ─── Agents ───────────────────────────────────────────────────────────────────

export async function listAgents(): Promise<{ agents: AgentProfileSummary[] }> {
  return fetchJson<{ agents: AgentProfileSummary[] }>("/api/agents/");
}

export async function getAgent(name: string): Promise<AgentProfile> {
  return fetchJson<AgentProfile>(`/api/agents/${encodeURIComponent(name)}`);
}

export async function createAgent(name: string, data: AgentProfile): Promise<unknown> {
  return fetchJson<unknown>(`/api/agents/${encodeURIComponent(name)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function updateAgent(name: string, data: Partial<AgentProfile>): Promise<unknown> {
  return fetchJson<unknown>(`/api/agents/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function deleteAgent(name: string): Promise<{ deleted: boolean; name: string }> {
  return fetchJson<{ deleted: boolean; name: string }>(`/api/agents/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

// ─── Shows ────────────────────────────────────────────────────────────────────

export async function listShows(): Promise<ShowSummary[]> {
  return fetchJson<ShowSummary[]>("/api/shows/");
}

export async function getShow(topic: string): Promise<ShowDetail> {
  return fetchJson<ShowDetail>(`/api/shows/${encodeURIComponent(topic)}`);
}

// H-FE-5: terminal {"type":"done"} event from shows.py MUST close the
// stream. The closer runs BEFORE invoking the callback for done events so
// that close() always runs even if the callback throws.
export function streamShow(topic: string, onEvent: (event: ShowEvent) => void): () => void {
  const close = sseSubscribe(`/api/shows/${encodeURIComponent(topic)}/stream`, (data) => {
    const event = JSON.parse(data) as ShowEvent;
    if (event.type === "done") {
      close();
    }
    onEvent(event);
  });
  return close;
}

// ─── Sessions ────────────────────────────────────────────────────────────────

export interface SessionSummary {
  id: string;
  name: string;
  created_at: number;
  updated_at: number;
  branch_count: number;
  message_count: number;
  status: string;
}

export interface SessionMessage {
  id: string;
  role: string;
  /**
   * Null when the server refused to decode this payload, which it does above a
   * size ceiling. Nullable rather than optional on purpose: the withholding is
   * a state a renderer has to handle, and an optional field reads as one a
   * caller may ignore.
   */
  content: Record<string, unknown> | null;
  /**
   * True when `content` is null because it was refused, as opposed to a message
   * that carried none. This endpoint returns no per-session bounds beside the
   * rows, so this flag is the only place that distinction is available.
   */
  content_withheld?: boolean;
  /**
   * Present only on a withheld row, where the payload that normally carries the
   * pairing is gone. An action request and its response are one call, and
   * without these a withheld pair renders as two unrelated rows.
   */
  action_request_id?: string;
  action_response_id?: string;
  sender: string | null;
  timestamp: number;
  lion_class: string;
  branch_id?: string;
}

export interface SessionBranch {
  id: string;
  name: string;
  created_at: number;
  /** Persisted full-progression bounds, independent of the messages window. */
  first_message_at?: number | null;
  last_message_at?: number | null;
  started_at?: number | null;
  ended_at?: number | null;
  messages: SessionMessage[];
  /** Full progression length; messages is a tail window of it. */
  message_total?: number;
  message_offset?: number;
  /** Whether this branch has messages older than the current window — the
   * server's own signal, independent of the message_total/messages.length
   * diff the client also computes. */
  message_has_older?: boolean;
  model?: string | null;
  provider?: string | null;
  agent_name?: string | null;
}

export interface SessionDetail {
  id: string;
  name: string;
  invocation_kind?: string | null;
  created_at: number;
  updated_at: number;
  status?: string | null;
  started_at?: number | null;
  ended_at?: number | null;
  branches: SessionBranch[];
  // Opaque anchor cursor for the "load older" page, one page further back
  // than what `branches[].messages` currently carries. Absent/null once every
  // branch's progression has been fully paged.
  message_next_cursor?: string | null;
  // ADR-0022: provenance disclosure — mirrors what list_sessions() exposes.
  model?: string | null;
  provider?: string | null;
  effort?: string | null;
  agent_hash?: string | null;
  invocation_id?: string | null;
  /** Project scope used by Operator write tools to fail closed across runs. */
  project?: string | null;
  /** Whether a queued run control would ever reach a runner (services/
   * sessions.py get_session, computed by the admission path's own predicate).
   * False for a mirrored or imported agent session, which no lionagi run owns:
   * the server admits no control for one, so no control is offered either.
   * Absent is read as false by the control surface — a missing capability is
   * not evidence of a capability. */
  has_control_consumer?: boolean | null;
  /** Whether this run's pause gate is held, or queued to be (services/
   * sessions.py _pause_is_held). Server-derived, so it survives a reload: a
   * pause remembered only in component state comes back as "not paused" and
   * leaves Resume disabled on a run that is still stopped. Absent is read as
   * false, which is the pre-pause state and the one that offers Pause. */
  pause_is_held?: boolean | null;
  // ADR-0028: denormalized status reason (services/sessions.py get_session
  // already returns these; the type was just missing them).
  status_reason_code?: string | null;
  status_reason_summary?: string | null;
  // Structured refs backing the status reason; entries with kind
  // "failed_operation" carry the authored node id of an op the run's own
  // failure evidence names.
  status_evidence_refs?: Array<{ kind?: string; id?: string; label?: string }> | null;
  // ADR-0029: artifact contract and verification result.
  artifact_contract_json?: ArtifactContract | null;
  artifact_verification_json?: ArtifactVerification | null;
  // Absolute artifact-root path on disk (services/sessions.py get_session
  // returns this verbatim) — the run's save root for file-link resolution.
  artifacts_path?: string | null;
  // Cost-visibility contract: null means the provider never reported usage
  // (unknown), never coerced to 0. Token totals are summed across every
  // branch of the session, so an orchestration run carries its workers'
  // spend, not just the orchestrator's own turns.
  total_cost_usd?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  // Full-session aggregate (services/sessions.py get_session, computed over
  // every branch's full progression, not the display window) — `files` is
  // the run-wide known-file union, including files touched before the
  // 200-message tail window this response's `branches[].messages` covers.
  message_stats?: {
    message_count: number;
    roles: Record<string, number>;
    tool_call_count: number;
    error_count: number;
    files: string[];
    // The server hydrates the newest slice of a long session's action rows and
    // stops at a bound. When it stopped, every field above derived from those
    // rows is a floor rather than a total, and the reader has to say so —
    // a lower bound presented as a count is read as a count.
    bounded?: boolean;
    // `files` has its own ceilings (distinct names, total bytes, rows
    // scanned), separate from `bounded` above: the union is computed over the
    // whole run rather than the hydrated slice, so it can be cut when nothing
    // else was. A file union that was cut answers "is this name a file?" with
    // a no it has not earned, so it is never presented as complete.
    files_bounded?: boolean;
  };
}

export async function listSessions(): Promise<{ sessions: SessionSummary[] }> {
  return fetchJson<{ sessions: SessionSummary[] }>("/api/sessions/");
}

export const SESSION_MESSAGE_PAGE = 200;

// message_cursor pages backward from the tail (server: services/sessions.py
// _window_message_ids) — each older page's anchor is stable against new
// messages landing at the tail, unlike a fixed offset that shifts under a
// live session. The offset param still exists server-side for legacy callers
// but this client only ever asks for a page by cursor.
export async function getSession(
  id: string,
  params?: { messageLimit?: number; messageCursor?: string },
): Promise<SessionDetail> {
  const query = new URLSearchParams();
  if (params?.messageLimit != null) query.set("message_limit", String(params.messageLimit));
  if (params?.messageCursor != null) query.set("message_cursor", params.messageCursor);
  const qs = query.toString();
  return fetchJson<SessionDetail>(`/api/sessions/${encodeURIComponent(id)}${qs ? `?${qs}` : ""}`);
}

export function streamSession(
  id: string,
  onEvent: (event: Record<string, unknown>) => void,
): () => void {
  let cursor: string | undefined;
  const close = sseSubscribe(
    () => {
      const query = new URLSearchParams();
      if (cursor) query.set("cursor", cursor);
      const suffix = query.toString();
      return `/api/sessions/${encodeURIComponent(id)}/stream${suffix ? `?${suffix}` : ""}`;
    },
    (data, eventId) => {
      let event: Record<string, unknown>;
      try {
        event = JSON.parse(data) as Record<string, unknown>;
      } catch {
        /* malformed chunk */
        return;
      }
      if (event.type === "done") {
        close();
      }
      onEvent(event);
      // Advance only after the consumer accepted this frame. If the callback
      // throws, reconnecting from the prior cursor repeats rather than skips it.
      if (eventId && event.type !== "heartbeat" && event.type !== "done") {
        cursor = eventId;
      }
    },
  );
  return close;
}

// ─── Session lifecycle signals (Phase C Move 1) ───────────────────────────────

export interface SignalEvent {
  id: string;
  session_id: string;
  seq: number;
  kind: string;
  op_id: string;
  /** Epoch MILLISECONDS, so it can be compared against `Date.now()` directly.
   *  The backend stamps this in SECONDS (`time.time()`, in the session
   *  observer) and passes it through the signals service unchanged;
   *  `normalizeSignalEvent` does the conversion once, here at the wire, and
   *  it is the only place that knows the backend's unit. */
  ts: number;
  payload: Record<string, unknown>;
}

const SIGNAL_TS_SECONDS_TO_MS = 1000;

/** Converts one raw signal off the wire into the frontend's units. Exported so
 *  the unit can be asserted directly rather than inferred from a consumer. */
export function normalizeSignalEvent(raw: SignalEvent): SignalEvent {
  return { ...raw, ts: raw.ts * SIGNAL_TS_SECONDS_TO_MS };
}

export function streamSignals(
  id: string,
  onEvent: (event: SignalEvent | { type: string }) => void,
): () => void {
  let afterSeq = 0;
  const close = sseSubscribe(
    () => {
      const query = new URLSearchParams({ after_seq: String(afterSeq) });
      return `/api/sessions/${encodeURIComponent(id)}/signals?${query}`;
    },
    (data) => {
      let event: SignalEvent | { type: string };
      try {
        event = JSON.parse(data) as SignalEvent | { type: string };
      } catch {
        /* malformed chunk */
        return;
      }
      if ("type" in event) {
        if (event.type === "done") close();
        onEvent(event);
        return;
      }
      onEvent(normalizeSignalEvent(event));
      // Advanced only after the consumer has taken the event. Advancing first
      // left a throwing consumer with the cursor already past a signal it
      // never handled, and the reconnect resumes from that cursor, so the
      // signal was skipped for good. Redelivering one the consumer already
      // processed is the cheaper failure of the two.
      afterSeq = Math.max(afterSeq, event.seq);
    },
  );
  return close;
}

// ─── Invocations (ADR-0020) ───────────────────────────────────────────────────

export interface InvocationSummary {
  id: string;
  skill: string;
  plugin: string | null;
  prompt: string | null;
  started_at: number;
  ended_at: number | null;
  status: string;
  session_count: number;
  created_at: number;
  updated_at: number;
  node_metadata: Record<string, unknown> | null;
  status_reason_summary?: string | null;
  // ADR-0026: project provenance from the most-recently updated child session.
  project?: string | null;
  project_source?: string | null;
  // ADR-0057 health verdict (worst-of across child sessions) + the real
  // last-activity timestamp behind it, "unknown" when the invocation has
  // no child sessions yet — same vocabulary runs use, plus
  // "unknown" for a case runs never hit (a run always has itself).
  health?: "healthy" | "idle" | "unresponsive" | "stale" | "orphaned" | "zombie" | "unknown" | null;
  last_activity_at?: number | null;
}

export interface InvocationSession {
  id: string;
  name: string | null;
  agent_name: string | null;
  playbook_name: string | null;
  invocation_kind: string | null;
  status: string | null;
  last_message_at: number | null;
  started_at: number | null;
  ended_at: number | null;
  // ADR-0022: per-child-session model + effort disclosure.
  model?: string | null;
  effort?: string | null;
}

// ADR-0021: structured skill outputs. `kind` is the dispatch key for
// the frontend renderer; `content` shape depends on the kind.
export interface ArtifactSummary {
  id: string;
  invocation_id: string | null;
  session_id: string | null;
  kind: string;
  name: string;
  created_at: number;
  content: Record<string, unknown> | null;
  file_path: string | null;
}

export interface InvocationDetail extends InvocationSummary {
  sessions: InvocationSession[];
  artifacts: ArtifactSummary[];
}

export async function getArtifact(id: string): Promise<ArtifactSummary> {
  return fetchJson<ArtifactSummary>(`/api/artifacts/${encodeURIComponent(id)}`);
}

export async function listArtifactsForSession(
  sessionId: string,
): Promise<{ artifacts: ArtifactSummary[] }> {
  return fetchJson<{ artifacts: ArtifactSummary[] }>(
    `/api/artifacts/by-session/${encodeURIComponent(sessionId)}`,
  );
}

export interface InvocationListResponse {
  invocations: InvocationSummary[];
  limit: number;
  offset: number;
  has_next: boolean;
  /** Real total matching the filters, not just this page's row count —
   * `limit` caps at 200, so counting `invocations` instead plateaus there. */
  total: number;
  /** Total matching the filters with status == "completed" specifically,
   * ignoring `params.status` — always a meaningful success-rate numerator. */
  completed_total: number;
}

export interface InvocationListParams {
  skill?: string;
  plugin?: string;
  status?: string;
  limit?: number;
  offset?: number;
}

export async function listInvocations(
  params?: InvocationListParams,
): Promise<InvocationListResponse> {
  const query = new URLSearchParams();
  if (params?.skill) query.set("skill", params.skill);
  if (params?.plugin) query.set("plugin", params.plugin);
  if (params?.status) query.set("status", params.status);
  if (params?.limit !== undefined) query.set("limit", String(params.limit));
  if (params?.offset !== undefined) query.set("offset", String(params.offset));
  const qs = query.toString();
  return fetchJson<InvocationListResponse>(`/api/invocations/${qs ? `?${qs}` : ""}`);
}

export async function getInvocation(id: string): Promise<InvocationDetail> {
  return fetchJson<InvocationDetail>(`/api/invocations/${encodeURIComponent(id)}`);
}

// ─── Definitions (versioned md files via SQLite) ──────────────────────────────

export interface DefinitionSummary {
  kind: string;
  name: string;
  path: string;
  disk_path: string;
  // null when the version-history store could not be read for the listing
  // (distinct from false, which means "never saved a version").
  has_versions: boolean | null;
  version: number;
  updated_at: number;
}

export interface DefinitionVersion {
  id: string;
  version: number;
  created_at: number;
  message: string | null;
}

export interface DefinitionDetail {
  kind: string;
  name: string;
  path: string;
  content: string;
  // version/versions are null when the version-history store could not be
  // read; content/path are always disk-backed and always present.
  version: number | null;
  versions: DefinitionVersion[] | null;
  history_available: boolean;
}

export async function listDefinitions(
  kind?: string,
): Promise<{ definitions: DefinitionSummary[] }> {
  const query = kind ? `?kind=${encodeURIComponent(kind)}` : "";
  return fetchJson<{ definitions: DefinitionSummary[] }>(`/api/definitions/${query}`);
}

export async function getDefinition(kind: string, name: string): Promise<DefinitionDetail> {
  return fetchJson<DefinitionDetail>(
    `/api/definitions/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`,
  );
}

// A specific historical version's content -- distinct from DefinitionDetail
// (the current definition): the backend's single-version read has nothing
// to fall back on, so it either answers with a real version number and
// content or refuses outright; it never returns the versions/history_available
// fields DefinitionDetail carries.
export interface DefinitionVersionDetail {
  kind: string;
  name: string;
  version: number;
  content: string;
  created_at: number;
  message: string | null;
}

export async function getDefinitionVersion(
  kind: string,
  name: string,
  version: number,
): Promise<DefinitionVersionDetail> {
  return fetchJson<DefinitionVersionDetail>(
    `/api/definitions/${encodeURIComponent(kind)}/${encodeURIComponent(name)}/versions/${version}`,
  );
}

// F-A3-1 (ADR-0016): backend is POST /api/definitions/{kind}/{name} — no PUT route exists.
// Return type matches services/definitions.py save_definition() response shape:
//   { kind, name, version, saved_at, message? }
export async function saveDefinition(
  kind: string,
  name: string,
  content: string,
  message?: string,
): Promise<{
  kind: string;
  name: string;
  version: number;
  saved_at: number;
  message: string | null;
}> {
  return fetchJson<{
    kind: string;
    name: string;
    version: number;
    saved_at: number;
    message: string | null;
  }>(`/api/definitions/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, message }),
  });
}

// H-FE-4: version is a query param per ADR-0016 and definitions.py:58-63,
// not a path segment. Return type updated to include full rollback response.
export async function rollbackDefinition(
  kind: string,
  name: string,
  version: number,
): Promise<{
  version: number;
  saved_at: number;
  rolled_back_from: number;
  rolled_back_to: number;
  message: string | null;
}> {
  return fetchJson(
    `/api/definitions/${encodeURIComponent(kind)}/${encodeURIComponent(name)}/rollback?version=${version}`,
    { method: "POST" },
  );
}

export async function snapshotDefinitions(kind?: string): Promise<{ snapshots_created: number }> {
  const query = kind ? `?kind=${encodeURIComponent(kind)}` : "";
  return fetchJson<{ snapshots_created: number }>(`/api/definitions/snapshot${query}`, {
    method: "POST",
  });
}

// ─── Skills ─────────────────────────────────────────────────────────────────

export interface SkillSummary {
  name: string;
  description: string;
  path: string;
  allowed_tools: string[];
}

export interface SkillDetail {
  name: string;
  description: string;
  path: string;
  content: string;
  allowed_tools: string[];
}

export async function listSkills(): Promise<{ skills: SkillSummary[] }> {
  return fetchJson<{ skills: SkillSummary[] }>("/api/skills/");
}

export async function getSkill(name: string): Promise<SkillDetail> {
  return fetchJson<SkillDetail>(`/api/skills/${encodeURIComponent(name)}`);
}

export interface SkillValidationResult {
  ok: boolean;
  errors: string[] | null;
}

export async function validateSkill(name: string, content: string): Promise<SkillValidationResult> {
  return fetchJson<SkillValidationResult>(`/api/skills/${encodeURIComponent(name)}/validate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
}

// ─── Plugins ──────────────────────────────────────────────────────────────────

export interface PluginSummary {
  name: string;
  description: string;
  version: string;
  source: "marketplace" | "third-party";
  skill_count: number;
  agent_count: number;
  has_hooks: boolean;
  has_mcp: boolean;
  path: string;
}

export interface PluginSkillRef {
  name: string;
  description: string;
}

export interface PluginAgentRef {
  name: string;
  description: string;
}

export interface PluginDetail {
  name: string;
  description: string;
  version: string;
  source: "marketplace" | "third-party";
  skill_count: number;
  agent_count: number;
  has_hooks: boolean;
  has_mcp: boolean;
  path: string;
  skills: PluginSkillRef[];
  agents: PluginAgentRef[];
  hooks: Record<string, unknown> | null;
  mcp: Record<string, unknown> | null;
  readme: string | null;
}

export interface PluginSkillDetail {
  name: string;
  description: string;
  path: string;
  content: string;
  allowed_tools: string[];
}

export async function listPlugins(): Promise<{ plugins: PluginSummary[] }> {
  return fetchJson<{ plugins: PluginSummary[] }>("/api/plugins");
}

export async function getPlugin(name: string): Promise<PluginDetail> {
  return fetchJson<PluginDetail>(`/api/plugins/${encodeURIComponent(name)}`);
}

// ─── MCP servers ────────────────────────────────────────────────────────────

export type McpServerTransport = "stdio" | "http";

export interface McpServerLastCheck {
  ok: boolean;
  error: string | null;
  checked_at: number;
}

export interface McpServerSummary {
  name: string;
  transport: McpServerTransport;
  command?: string;
  args?: string[];
  url?: string;
  timeout?: number;
  env_keys: string[];
  enabled: boolean;
  created_at: number;
  updated_at: number;
  last_check: McpServerLastCheck | null;
}

/** Fields a client may submit for register/update. `env` values are only
 * ever sent up (to be stored), never returned by the server — see
 * McpServerSummary, which carries `env_keys` instead. A `null` value for a
 * key is the explicit way to remove it; the server never infers a deletion
 * from a key's mere absence, since env merges key-by-key onto what's
 * already stored. */
export interface McpServerConfigInput {
  command?: string;
  args?: string[];
  env?: Record<string, string | null>;
  url?: string;
  /** `null` clears a stored timeout. Absent leaves it as it was, the same
   * merge rule `env` follows. */
  timeout?: number | null;
  enabled?: boolean;
}

export interface McpServerValidationResult {
  ok: boolean;
  errors?: string[] | null;
  connection_checked: boolean;
  connection_ok: boolean | null;
  connection_error: string | null;
}

export async function listMcpServers(): Promise<{ servers: McpServerSummary[] }> {
  return fetchJson<{ servers: McpServerSummary[] }>("/api/mcp/servers/");
}

export async function getMcpServer(name: string): Promise<McpServerSummary> {
  return fetchJson<McpServerSummary>(`/api/mcp/servers/${encodeURIComponent(name)}`);
}

export async function registerMcpServer(
  name: string,
  data: McpServerConfigInput,
): Promise<McpServerSummary> {
  return fetchJson<McpServerSummary>("/api/mcp/servers/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, ...data }),
  });
}

export async function updateMcpServer(
  name: string,
  data: McpServerConfigInput,
): Promise<McpServerSummary> {
  return fetchJson<McpServerSummary>(`/api/mcp/servers/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function setMcpServerEnabled(
  name: string,
  enabled: boolean,
): Promise<McpServerSummary> {
  return fetchJson<McpServerSummary>(
    `/api/mcp/servers/${encodeURIComponent(name)}/${enabled ? "enable" : "disable"}`,
    { method: "POST" },
  );
}

export async function deleteMcpServer(name: string): Promise<{ ok: boolean }> {
  return fetchJson<{ ok: boolean }>(`/api/mcp/servers/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

/** Attempt a real connection to an already-registered server and persist
 * the result (surfaced afterwards via `last_check` on the summary). */
export async function checkMcpServer(name: string): Promise<McpServerSummary> {
  return fetchJson<McpServerSummary>(`/api/mcp/servers/${encodeURIComponent(name)}/check`, {
    method: "POST",
  });
}

/** Validate a config before saving. Shape is always checked; pass
 * `check_connection: true` to also attempt a real connection (the result
 * distinguishes "not checked" from "checked and failed" via
 * `connection_checked`). */
export async function validateMcpServer(
  name: string,
  data: McpServerConfigInput & { check_connection?: boolean },
): Promise<McpServerValidationResult> {
  return fetchJson<McpServerValidationResult>(
    `/api/mcp/servers/${encodeURIComponent(name || "new")}/validate`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, ...data }),
    },
  );
}

export async function getPluginSkill(
  pluginName: string,
  skillName: string,
): Promise<PluginSkillDetail> {
  return fetchJson<PluginSkillDetail>(
    `/api/plugins/${encodeURIComponent(pluginName)}/skills/${encodeURIComponent(skillName)}`,
  );
}

export interface PluginAgentDetail {
  name: string;
  description: string;
  path: string;
  content: string;
}

export async function getPluginAgent(
  pluginName: string,
  agentName: string,
): Promise<PluginAgentDetail> {
  return fetchJson<PluginAgentDetail>(
    `/api/plugins/${encodeURIComponent(pluginName)}/agents/${encodeURIComponent(agentName)}`,
  );
}

// ─── Admin ────────────────────────────────────────────────────────────────────

export type PhantomReason = "process_dead" | "missing_artifacts" | "stale_lock";

export interface PhantomSession {
  session_id: string;
  playbook: string | null;
  started_at: number | null;
  reason: PhantomReason;
}

export interface AdminDoctorResponse {
  phantom_sessions: PhantomSession[];
  db_health: {
    size_bytes: number;
    wal_bytes: number;
    size_alert?: boolean;
    size_threshold_bytes?: number;
  };
  diagnostic_run_at: string;
}

export interface AdminPruneRequest {
  session_ids?: string[];
  all_phantom?: boolean;
}

export async function getAdminDoctor(): Promise<AdminDoctorResponse> {
  return fetchJson<AdminDoctorResponse>("/api/admin/doctor");
}

export async function pruneAdmin(body: AdminPruneRequest): Promise<{ pruned: number }> {
  return fetchJson<{ pruned: number }>("/api/admin/prune", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export interface AdminEvent {
  id: string;
  created_at: number;
  action: string;
  target_id: string | null;
  details: Record<string, unknown> | null;
  actor: string;
}

export interface AdminEventListParams {
  action?: string;
  target_id?: string;
  limit?: number;
}

export async function getAdminEvents(params?: AdminEventListParams): Promise<AdminEvent[]> {
  const query = new URLSearchParams();
  if (params?.action) query.set("action", params.action);
  if (params?.target_id) query.set("target_id", params.target_id);
  if (params?.limit != null) query.set("limit", String(params.limit));
  const qs = query.toString();
  const res = await fetchJson<{ events: AdminEvent[] }>(`/api/admin/events${qs ? `?${qs}` : ""}`);
  return res.events;
}

// ─── Admin maintenance (Phase C Move 3) ──────────────────────────────────────

export type MaintenanceAction = "vacuum" | "checkpoint" | "prune";

export interface MaintenanceResult {
  action: MaintenanceAction;
  // vacuum
  status?: string;
  // checkpoint
  mode?: string;
  busy?: number | null;
  log_pages?: number | null;
  checkpointed?: number | null;
  // How much WAL the checkpoint was asked to drain and how long it took. The
  // three counters above read zero on every successful TRUNCATE regardless of
  // size, so they cannot separate a long drain from an idle one.
  wal_bytes_before?: number | null;
  elapsed_ms?: number | null;
  // prune
  sessions_pruned?: number;
  runs_pruned?: number;
}

export async function runMaintenance(action: MaintenanceAction): Promise<MaintenanceResult> {
  return fetchJson<MaintenanceResult>("/api/admin/maintenance", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
}

// ─── Teams (`li team` crews with a shared inbox) ──────────────────────────────

export interface TeamSummary {
  id: string;
  name: string;
  member_count: number;
  last_modified: number;
}

export interface TeamListResponse {
  teams: TeamSummary[];
  limit: number;
  offset: number;
  total: number;
  has_next: boolean;
}

export interface TeamMessage {
  id: string;
  from: string;
  to: string | string[];
  content: string;
  timestamp: string;
  read_by: Record<string, unknown>;
  kind: string;
  from_op?: string;
  artifacts?: string[];
}

export interface TeamDetail {
  id: string;
  name: string;
  members: string[];
  messages: TeamMessage[];
  created_at: string;
}

export async function listTeams(params?: {
  limit?: number;
  offset?: number;
}): Promise<TeamListResponse> {
  const query = new URLSearchParams();
  if (params?.limit != null) query.set("limit", String(params.limit));
  if (params?.offset != null) query.set("offset", String(params.offset));
  const qs = query.toString();
  return fetchJson<TeamListResponse>(`/api/teams/${qs ? `?${qs}` : ""}`);
}

export async function getTeam(teamId: string): Promise<TeamDetail> {
  return fetchJson<TeamDetail>(`/api/teams/${encodeURIComponent(teamId)}`);
}

// ─── Projects (ADR-0026) ──────────────────────────────────────────────────────

export interface ProjectListResponse {
  projects: ProjectSummary[];
  unassigned_count: number;
}

export async function listProjects(): Promise<ProjectListResponse> {
  return fetchJson<ProjectListResponse>("/api/projects/");
}

export async function getProject(name: string): Promise<ProjectDetail> {
  return fetchJson<ProjectDetail>(`/api/projects/${encodeURIComponent(name)}`);
}

export async function createProject(data: {
  name: string;
  github?: string;
  description?: string;
  path?: string;
}): Promise<unknown> {
  return fetchJson<unknown>("/api/projects/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function updateProject(
  name: string,
  data: { github?: string; description?: string; path?: string },
): Promise<unknown> {
  return fetchJson<unknown>(`/api/projects/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function deleteProject(name: string): Promise<unknown> {
  return fetchJson<unknown>(`/api/projects/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

// ─── Stats (extended) ─────────────────────────────────────────────────────────

export interface DbStats {
  path: string;
  size_bytes: number;
  wal_bytes: number;
  /** True once size_bytes reaches size_threshold_bytes. Computed by the
   * backend so every surface applies one threshold rather than its own. */
  size_alert?: boolean;
  size_threshold_bytes?: number;
  connections_active: number;
  last_checkpoint_at: string | null;
  tables?: Record<string, number>;
  sessions_by_status?: Record<string, number>;
  pragmas?: Record<string, string | number | boolean | null>;
  slow_queries?: null;
}

export interface StudioStats {
  playbooks: number;
  agents: number;
  runs: number;
  shows: number;
  skills: number;
  plugins: number;
  db?: DbStats;
}

export async function getStats(): Promise<StudioStats> {
  return fetchJson<StudioStats>("/api/stats");
}

export type ActivityWindow = "24h" | "7d";

export interface ActivityBucket {
  t: number;
  completed: number;
  failed: number;
  cancelled: number;
  running: number;
}

export interface ActivityStats {
  window: ActivityWindow;
  buckets: ActivityBucket[];
  completion_rate: number | null;
  total: number;
}

export async function getActivityStats(window: ActivityWindow): Promise<ActivityStats> {
  return fetchJson<ActivityStats>(`/api/stats/activity?window=${window}`);
}

// Cost-visibility contract: `reported_usd` is `null` whenever no session in
// the window reported a cost — never coerced to 0. `coverage` is the
// fraction of the window's sessions (including in-flight/non-terminal ones)
// that reported a cost at all.
export interface SpendStats {
  window: ActivityWindow;
  reported_usd: number | null;
  reported_count: number;
  unreported_count: number;
  total_count: number;
  coverage: number | null;
}

export async function getSpendStats(window: ActivityWindow): Promise<SpendStats> {
  return fetchJson<SpendStats>(`/api/stats/spend?window=${window}`);
}

export interface SpendRollupRow {
  key: string | null;
  reported_usd: number | null;
  reported_count: number;
  unreported_count: number;
}

export interface SpendRollup {
  window: ActivityWindow;
  by_project: SpendRollupRow[];
  by_agent: SpendRollupRow[];
  by_playbook: SpendRollupRow[];
}

export async function getSpendRollup(window: ActivityWindow): Promise<SpendRollup> {
  return fetchJson<SpendRollup>(`/api/stats/spend/rollup?window=${window}`);
}

// ─── Schedules (ADR-0027) ───────────────────────────────────────────────────

export type GitHubEventFilter = "pr_merged" | "pr_opened" | "pr_updated" | "pr_closed";

export interface GitHubFilter {
  event?: GitHubEventFilter;
  base?: string;
}

export interface ScheduleListResponse {
  schedules: ScheduleSummary[];
}

export async function listSchedules(params?: {
  enabled?: boolean;
  trigger_type?: string;
  project?: string;
}): Promise<ScheduleListResponse> {
  const query = new URLSearchParams();
  if (params?.enabled !== undefined) query.set("enabled", String(params.enabled));
  if (params?.trigger_type) query.set("trigger_type", params.trigger_type);
  if (params?.project) query.set("project", params.project);
  const qs = query.toString();
  return fetchJson<ScheduleListResponse>(`/api/schedules/${qs ? `?${qs}` : ""}`);
}

export async function getSchedule(id: string): Promise<ScheduleDetail> {
  return fetchJson<ScheduleDetail>(`/api/schedules/${encodeURIComponent(id)}`);
}

export async function createSchedule(
  data: Record<string, unknown>,
): Promise<{ id: string; name: string }> {
  return fetchJson<{ id: string; name: string }>("/api/schedules/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function updateSchedule(id: string, data: Record<string, unknown>): Promise<unknown> {
  return fetchJson<unknown>(`/api/schedules/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function deleteSchedule(id: string): Promise<unknown> {
  return fetchJson<unknown>(`/api/schedules/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export async function enableSchedule(id: string): Promise<unknown> {
  return fetchJson<unknown>(`/api/schedules/${encodeURIComponent(id)}/enable`, {
    method: "POST",
  });
}

export async function disableSchedule(id: string): Promise<unknown> {
  return fetchJson<unknown>(`/api/schedules/${encodeURIComponent(id)}/disable`, {
    method: "POST",
  });
}

export async function triggerSchedule(id: string): Promise<{ run_id: string }> {
  return fetchJson<{ run_id: string }>(`/api/schedules/${encodeURIComponent(id)}/trigger`, {
    method: "POST",
  });
}

export async function listScheduleRuns(
  scheduleId: string,
  params?: { status?: string; limit?: number; offset?: number },
): Promise<{ runs: ScheduleRunSummary[]; has_next: boolean }> {
  const query = new URLSearchParams();
  if (params?.status) query.set("status", params.status);
  if (params?.limit != null) query.set("limit", String(params.limit));
  if (params?.offset != null) query.set("offset", String(params.offset));
  const qs = query.toString();
  return fetchJson(`/api/schedules/${encodeURIComponent(scheduleId)}/runs${qs ? `?${qs}` : ""}`);
}

// ─── Attention dispositions (needs-attention discharge lifecycle) ────────────

export type AttentionDispositionState = "acknowledged" | "resolved" | "expected" | "snoozed";

export interface AttentionDisposition {
  item_id: string;
  state: AttentionDispositionState;
  note: string | null;
  created_at: number;
  updated_at: number;
  expires_at: number | null;
  actor: string;
  source_status: string;
  /** Server-owned, monotonic per item_id. Echo back on the next PUT — required
   * to recreate an item a DELETE has removed; a stale value is rejected (409). */
  revision: number;
}

export interface AttentionDispositionHistoryEntry {
  id: string;
  item_id: string;
  prior_state: AttentionDispositionState | "open" | null;
  new_state: AttentionDispositionState | "open";
  note: string | null;
  actor: string;
  source_status: string | null;
  created_at: number;
}

/** Batch-read current, non-lapsed dispositions keyed by item_id. */
export async function listAttentionDispositions(): Promise<Record<string, AttentionDisposition>> {
  const res = await fetchJson<{ dispositions: Record<string, AttentionDisposition> }>(
    "/api/attention/dispositions/",
  );
  return res.dispositions;
}

/**
 * Create-or-replace one item's disposition. Idempotent under retry while the
 * disposition stays active. `revision` should be the value last read for
 * this item_id (e.g. `item.disposition?.revision`) — required to recreate a
 * disposition a DELETE has removed; omitted or stale, the server rejects
 * with 409 rather than resurrecting stale data.
 */
export async function putAttentionDisposition(
  itemId: string,
  body: {
    state: AttentionDispositionState;
    sourceStatus: string;
    note?: string;
    expiresAt?: number;
    actor?: string;
    revision?: number;
  },
): Promise<AttentionDisposition> {
  return fetchJson<AttentionDisposition>(
    `/api/attention/dispositions/${encodeURIComponent(itemId)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        state: body.state,
        source_status: body.sourceStatus,
        note: body.note,
        expires_at: body.expiresAt,
        actor: body.actor,
        revision: body.revision,
      }),
    },
  );
}

/** Remove a disposition (undo — the item returns to open). */
export async function deleteAttentionDisposition(
  itemId: string,
): Promise<{ item_id: string; deleted: boolean }> {
  return fetchJson(`/api/attention/dispositions/${encodeURIComponent(itemId)}`, {
    method: "DELETE",
  });
}

export async function getAttentionDispositionHistory(
  itemId: string,
): Promise<AttentionDispositionHistoryEntry[]> {
  const res = await fetchJson<{ item_id: string; history: AttentionDispositionHistoryEntry[] }>(
    `/api/attention/dispositions/${encodeURIComponent(itemId)}/history`,
  );
  return res.history;
}

// ─── Engine runs (Phase C Move 2) ─────────────────────────────────────────────

export interface EngineRunSummary {
  id: string;
  kind: string;
  status: string;
  started_at: number;
  ended_at: number | null;
  session_id: string | null;
  invocation_id: string | null;
  signal_session_id: string | null;
  parent_session_id: string | null;
  outcome: Record<string, unknown> | null;
  has_output: boolean;
  error_code: string | null;
}

export interface EngineRunDetail extends Omit<EngineRunSummary, "outcome"> {
  spec_json: Record<string, unknown> | null;
  spec_preview: Record<string, unknown>;
  outcome_json: Record<string, unknown> | null;
  export_dir: string | null;
  error: string | null;
}

export interface EngineRunPage {
  version: 1;
  items: EngineRunSummary[];
  next_cursor: string | null;
}

export interface EngineRunListParams {
  kind?: string;
  status?: string;
  session_id?: string;
  limit?: number;
  cursor?: string;
}

export async function listEngineRuns(params?: EngineRunListParams): Promise<EngineRunPage> {
  const query = new URLSearchParams();
  if (params?.kind) query.set("kind", params.kind);
  if (params?.status) query.set("status", params.status);
  if (params?.session_id) query.set("session_id", params.session_id);
  if (params?.limit != null) query.set("limit", String(params.limit));
  if (params?.cursor) query.set("cursor", params.cursor);
  const qs = query.toString();
  return fetchJson<EngineRunPage>(`/api/engine-runs/${qs ? `?${qs}` : ""}`);
}

export async function getEngineRun(
  runId: string,
  options?: { includeSpec?: boolean },
): Promise<EngineRunDetail> {
  const query = options?.includeSpec ? "?include_spec=true" : "";
  return fetchJson<EngineRunDetail>(`/api/engine-runs/${encodeURIComponent(runId)}${query}`);
}

// ─── Shows / plays ──────────────────────────────────────────────────────────

/**
 * A play currently waiting on a real gate decision (gate_failed, escalated,
 * a failing verdict parked in `gated`, or explicit opt-in), read live.
 */
export interface GatedPlaySummary {
  id: string;
  topic: string;
  play_name: string;
  /** Play lifecycle status; null when the live state is unavailable. */
  status?: string | null;
  started_at: number | null;
  updated_at: number | null;
  feedback: string | null;
  session_id: string | null;
}

export async function listGatedPlays(): Promise<GatedPlaySummary[]> {
  return fetchJson<GatedPlaySummary[]>("/api/shows/gated-plays");
}

// ─── Engine definitions ───────────────────────────────────────────────────────

export interface EngineDef {
  id: string;
  name: string;
  kind: string;
  model: string | null;
  max_depth: number | null;
  max_agents: number | null;
  options: Record<string, string> | null;
  description: string | null;
  created_at: number;
  updated_at: number;
}

export interface CreateEngineDefRequest {
  name: string;
  kind: string;
  model?: string;
  max_depth?: number;
  max_agents?: number;
  options?: Record<string, string>;
  description?: string;
}

export interface UpdateEngineDefRequest {
  name?: string;
  kind?: string;
  model?: string;
  max_depth?: number;
  max_agents?: number;
  options?: Record<string, string>;
  description?: string;
}

export interface LaunchResult {
  invocation_id: string;
  action_kind: string;
}

export async function listEngineDefs(params?: { kind?: string }): Promise<EngineDef[]> {
  const query = new URLSearchParams();
  if (params?.kind) query.set("kind", params.kind);
  const qs = query.toString();
  return fetchJson<EngineDef[]>(`/api/engine-defs/${qs ? `?${qs}` : ""}`);
}

export async function getEngineDef(defId: string): Promise<EngineDef> {
  return fetchJson<EngineDef>(`/api/engine-defs/${encodeURIComponent(defId)}`);
}

export async function createEngineDef(
  body: CreateEngineDefRequest,
): Promise<{ id: string; name: string; created_at: number }> {
  return fetchJson(`/api/engine-defs/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function updateEngineDef(
  defId: string,
  body: UpdateEngineDefRequest,
): Promise<{ ok: boolean }> {
  return fetchJson(`/api/engine-defs/${encodeURIComponent(defId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function deleteEngineDef(defId: string): Promise<{ ok: boolean }> {
  return fetchJson(`/api/engine-defs/${encodeURIComponent(defId)}`, { method: "DELETE" });
}

// ─── Workflow definitions ─────────────────────────────────────────────────────

export type WorkflowNodeKind = "input" | "chat" | "parse" | "fanout" | "engine";

export interface WorkflowNodePos {
  x: number;
  y: number;
}

export interface WorkflowEngineConfig {
  engine_def_id: string;
  model?: string;
  max_depth?: number;
  max_agents?: number;
  options?: Record<string, string>;
}

export interface WorkflowNode {
  id: string;
  kind: WorkflowNodeKind;
  label: string;
  pos: WorkflowNodePos;
  config?: WorkflowEngineConfig | Record<string, unknown>;
}

export interface WorkflowEdge {
  id: string;
  from: string;
  to: string;
  label?: string;
  condition?: string;
}

export interface WorkflowSpec {
  version: 1;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  inputs: string[];
  outputs: string[];
}

export interface WorkflowDef {
  id: string;
  name: string;
  description: string | null;
  spec_json: WorkflowSpec | null;
  created_at: number;
  updated_at: number;
}

export interface CreateWorkflowDefRequest {
  name: string;
  description?: string;
  spec_json?: WorkflowSpec;
}

export interface CreatedWorkflowDef {
  id: string;
  name: string;
  created_at: number;
}

export interface UpdateWorkflowDefRequest {
  name?: string;
  description?: string;
  spec_json?: WorkflowSpec;
}

export async function listWorkflowDefs(): Promise<WorkflowDef[]> {
  return fetchJson<WorkflowDef[]>("/api/workflow-defs/");
}

export async function getWorkflowDef(defId: string): Promise<WorkflowDef> {
  return fetchJson<WorkflowDef>(`/api/workflow-defs/${encodeURIComponent(defId)}`);
}

export async function createWorkflowDef(
  body: CreateWorkflowDefRequest,
): Promise<CreatedWorkflowDef> {
  return fetchJson(`/api/workflow-defs/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function updateWorkflowDef(
  defId: string,
  body: UpdateWorkflowDefRequest,
): Promise<{ ok: boolean }> {
  return fetchJson(`/api/workflow-defs/${encodeURIComponent(defId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function deleteWorkflowDef(defId: string): Promise<{ ok: boolean }> {
  return fetchJson(`/api/workflow-defs/${encodeURIComponent(defId)}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Casts catalog — roles, modes, and their emission contracts (read-only)

export interface CastEmission {
  model: string;
  key: string;
}

export interface CastRoleConfig {
  active?: boolean;
  model?: string | null;
  effort?: string | null;
  default_modes?: string[];
  modes_allow?: string[];
  authority?: string[];
  boundaries?: string[];
  escalations?: string[];
}

export interface CastRole {
  name: string;
  description: string | null;
  emits: CastEmission[];
  body?: string | null;
  config?: CastRoleConfig | null;
}

export interface CastMode {
  name: string;
  description: string | null;
  behaviors?: string | null;
  conflicts_with?: string[];
}

export interface CastsCatalog {
  roles: CastRole[];
  modes: CastMode[];
}

export async function getCasts(): Promise<CastsCatalog> {
  return fetchJson<CastsCatalog>("/api/casts/");
}

export async function launchEngine(body: {
  action_kind: "engine";
  action_engine_def: string;
  action_prompt: string;
}): Promise<LaunchResult> {
  return fetchJson(`/api/launches/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** One named, reusable hook definition from the shared hook library. */
export interface HookDef {
  description: string;
  command: string;
  timeout?: number;
}

/** Provider-neutral hook events — materialized per provider at launch. */
export type HookEvent =
  | "pre_tool"
  | "post_tool"
  | "prompt_submit"
  | "post_response"
  | "session_start"
  | "session_end";

/** One assembly row binding a library hook to a neutral event. */
export interface HookAttachment {
  hook: string;
  event: HookEvent;
  matcher?: string;
}

export interface HookLibrary {
  path: string;
  hooks: Record<string, HookDef>;
  events?: string[];
  error?: string;
}

export async function getHookLibrary(): Promise<HookLibrary> {
  return fetchJson<HookLibrary>("/api/hooks/library");
}

export async function putHookDef(name: string, spec: HookDef): Promise<HookDef & { name: string }> {
  return fetchJson<HookDef & { name: string }>(`/api/hooks/library/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(spec),
  });
}

export async function deleteHookDef(name: string): Promise<{ ok: boolean }> {
  return fetchJson<{ ok: boolean }>(`/api/hooks/library/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

/** The Operator's hook assembly — attachments materialized into the provider
 * CLI's settings each turn (the Operator inherits nothing else). */
export interface OperatorHooksConfig {
  enabled: boolean;
  attachments: HookAttachment[];
  path?: string;
  error?: string;
}

export async function getOperatorHooks(): Promise<OperatorHooksConfig> {
  return fetchJson<OperatorHooksConfig>("/api/operator/hooks");
}

export async function putOperatorHooks(config: {
  enabled: boolean;
  attachments: HookAttachment[];
}): Promise<OperatorHooksConfig> {
  return fetchJson<OperatorHooksConfig>("/api/operator/hooks", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}
