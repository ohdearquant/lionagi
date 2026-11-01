/**
 * Pairing with the local daemon, for a page on another origin.
 *
 * A daemon with no auth token configured refuses API requests from a page that
 * is not on this machine unless the page sends the bearer of the daemon's
 * launch. `li studio` opens the page with a one-time code in the URL fragment
 * (`#pair=<code>`), which a browser never sends to a server. The code is read
 * once at startup and removed from the address bar, then exchanged with the
 * daemon for the bearer and a decision capability. When the daemon answers that
 * the bearer is its launch bearer (`token_source: "launch"`), it is kept in
 * `localStorage` for the page's own origin, bound to the daemon's API base: it
 * survives reloads, tabs and bookmarks until the daemon refuses it, and the page
 * then forgets it and `li studio` pairs it again. The gate checks a stored
 * bearer with one request before the app mounts. A configured
 * `LIONAGI_STUDIO_AUTH_TOKEN` (`token_source: "configured"`), or an answer that
 * does not say which bearer it is, is held in memory for that page load only
 * and never written to storage. The decision capability is
 * kept in `sessionStorage` for that tab: it survives a reload of the tab, is
 * gone with the tab, and is not readable over HTTP. It is sent in
 * `X-Studio-Decision-Token` on the two Operator decision requests, and to no
 * other request.
 */

const DECISION_STORAGE_KEY = "studio.decisionToken";
const BEARER_STORAGE_KEY = "studio.pairedBearer";
// An exchange that never settles would hold every later attempt on it, Retry
// included. Past this it counts as not having reached the daemon, so the code
// is kept for the next attempt.
export const PAIRING_TIMEOUT_MS = 15_000;

type Paired = { base: string; token: string };

let pendingCode: string | undefined;
let redeeming: { base: string; result: Promise<PairingResult> } | undefined;
let paired: Paired | undefined;
// A storage that refuses writes (private mode) still honours this page load.
let decisionMemory: Paired | undefined;

function readDecisionCapability(): Paired | undefined {
  if (typeof window === "undefined") return decisionMemory;
  try {
    const raw = window.sessionStorage.getItem(DECISION_STORAGE_KEY);
    if (raw === null) return decisionMemory;
    const parsed = JSON.parse(raw) as { base?: unknown; token?: unknown } | null;
    if (
      parsed == null ||
      typeof parsed.base !== "string" ||
      typeof parsed.token !== "string" ||
      !parsed.token
    ) {
      return undefined;
    }
    return { base: parsed.base, token: parsed.token };
  } catch {
    return decisionMemory;
  }
}

function writeDecisionCapability(value: Paired | undefined): void {
  decisionMemory = value;
  if (typeof window === "undefined") return;
  try {
    if (value === undefined) window.sessionStorage.removeItem(DECISION_STORAGE_KEY);
    else window.sessionStorage.setItem(DECISION_STORAGE_KEY, JSON.stringify(value));
  } catch {
    // Storage unavailable: the capability lives for this page load only.
  }
}

/**
 * The bearer an earlier load of this page stored, if it is well formed. A
 * storage that refuses the read, or holds anything else, yields nothing.
 */
function readStoredBearer(): Paired | undefined {
  if (typeof window === "undefined") return undefined;
  try {
    const raw = window.localStorage.getItem(BEARER_STORAGE_KEY);
    if (raw === null) return undefined;
    const parsed = JSON.parse(raw) as { base?: unknown; token?: unknown } | null;
    if (
      parsed == null ||
      typeof parsed.base !== "string" ||
      typeof parsed.token !== "string" ||
      !parsed.token
    ) {
      return undefined;
    }
    return { base: parsed.base, token: parsed.token };
  } catch {
    return undefined;
  }
}

function writeStoredBearer(value: Paired | undefined): void {
  if (typeof window === "undefined") return;
  try {
    if (value === undefined) window.localStorage.removeItem(BEARER_STORAGE_KEY);
    else window.localStorage.setItem(BEARER_STORAGE_KEY, JSON.stringify(value));
  } catch {
    // Storage unavailable: the bearer lives for this page load only.
  }
}

/** Read `pair` from the fragment once and remove it, keeping any other fragment. */
export function takePairingCodeFromLocation(): void {
  if (typeof window === "undefined") return;
  const hash = window.location.hash;
  if (hash.length < 2) return;
  const params = new URLSearchParams(hash.slice(1));
  const code = params.get("pair");
  if (code === null) return;
  params.delete("pair");
  const rest = params.toString();
  const url = new URL(window.location.href);
  url.hash = rest ? `#${rest}` : "";
  window.history.replaceState(window.history.state, "", url.toString());
  if (code) pendingCode = code;
}

export function hasPendingPairingCode(): boolean {
  return pendingCode !== undefined || redeeming !== undefined;
}

export type PairingResult = "paired" | "refused" | "unreachable";

/**
 * Exchange the pending code with the daemon at `apiBase`. A refused code is
 * dropped, since it was used, expired or never issued; a code that did not
 * reach the daemon, or got no answer within `PAIRING_TIMEOUT_MS`, is kept for
 * the next attempt. A caller that asks while an exchange with the same daemon
 * is in flight shares its result: the code works once, so a second request
 * would be refused after the first had already paired the page.
 */
export function redeemPairingCode(apiBase: string): Promise<PairingResult> {
  if (redeeming !== undefined && redeeming.base === apiBase) return redeeming.result;
  const result = exchangePairingCode(apiBase).finally(() => {
    if (redeeming?.result === result) redeeming = undefined;
  });
  redeeming = { base: apiBase, result };
  return result;
}

async function exchangePairingCode(apiBase: string): Promise<PairingResult> {
  const code = pendingCode;
  if (code === undefined) return "refused";
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), PAIRING_TIMEOUT_MS);
  try {
    return await sendPairingCode(apiBase, code, abort.signal);
  } finally {
    clearTimeout(timer);
  }
}

async function sendPairingCode(
  apiBase: string,
  code: string,
  signal: AbortSignal,
): Promise<PairingResult> {
  let response: Response;
  try {
    response = await fetch(`${apiBase}/api/studio/pair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
      signal,
    });
  } catch {
    return "unreachable";
  }
  if (response.status >= 500) return "unreachable";
  // Only the code this exchange sent is spent: one read from the address bar
  // while it was in flight is the next attempt's.
  if (pendingCode === code) pendingCode = undefined;
  if (!response.ok) return "refused";
  let token: unknown;
  let decisionToken: unknown;
  let tokenSource: unknown;
  try {
    const body = (await response.json()) as {
      token?: unknown;
      decision_token?: unknown;
      token_source?: unknown;
    } | null;
    token = body?.token;
    decisionToken = body?.decision_token;
    tokenSource = body?.token_source;
  } catch {
    return "refused";
  }
  if (typeof token !== "string" || !token) return "refused";
  paired = { base: apiBase, token };
  // Only the daemon's launch bearer is stored. A configured token, or an answer
  // from a daemon that does not say which bearer it sent, stays in memory for
  // this page load, and any bearer stored earlier is removed.
  writeStoredBearer(tokenSource === "launch" ? paired : undefined);
  writeDecisionCapability(
    typeof decisionToken === "string" && decisionToken
      ? { base: apiBase, token: decisionToken }
      : undefined,
  );
  return "paired";
}

/**
 * The bearer this page was paired with, for requests to the same daemon only:
 * the one paired in this load, or else the one an earlier load stored for
 * exactly this API base.
 */
export function pairedBearer(apiBase: string): string | undefined {
  if (paired !== undefined) return paired.base === apiBase ? paired.token : undefined;
  const stored = readStoredBearer();
  return stored !== undefined && stored.base === apiBase ? stored.token : undefined;
}

/**
 * The bearer an earlier load stored for exactly this API base, when this load
 * has not paired: the one the gate checks with the daemon before the app mounts.
 */
export function storedBearerToCheck(apiBase: string): string | undefined {
  if (paired !== undefined) return undefined;
  const stored = readStoredBearer();
  return stored !== undefined && stored.base === apiBase ? stored.token : undefined;
}

/**
 * Forget the bearer, in memory and in storage: the daemon refused it, so the
 * page asks to be paired again. The decision capability is left alone.
 */
export function forgetPairedBearer(): void {
  paired = undefined;
  writeStoredBearer(undefined);
}

/**
 * The decision capability this page holds, for the API origin it was paired
 * with only. Another origin receives nothing.
 */
export function pairedDecisionToken(apiBase: string): string | undefined {
  const capability = readDecisionCapability();
  return capability !== undefined && capability.base === apiBase ? capability.token : undefined;
}

/** Forget the decision capability: the tab closed, or the page disconnected. */
export function clearDecisionToken(): void {
  writeDecisionCapability(undefined);
}

export function resetPairingForTests(): void {
  pendingCode = undefined;
  redeeming = undefined;
  paired = undefined;
  writeStoredBearer(undefined);
  writeDecisionCapability(undefined);
}
