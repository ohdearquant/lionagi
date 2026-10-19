/**
 * Pairing with the local daemon, for a page on another origin.
 *
 * A daemon with no auth token configured refuses API requests from a page that
 * is not on this machine unless the page sends the bearer of the daemon's
 * launch. `li studio` opens the page with a one-time code in the URL fragment
 * (`#pair=<code>`), which a browser never sends to a server. The code is read
 * once at startup and removed from the address bar, then exchanged with the
 * daemon for the bearer and a decision capability. The bearer is held in memory
 * only: nothing is written to storage, so a reload loses it and `li studio`
 * pairs the page again. The decision capability is kept in `sessionStorage` for
 * that tab: it survives a reload of the tab, is gone with the tab, and is not
 * readable over HTTP. It is sent in `X-Studio-Decision-Token` on the two
 * Operator decision requests, and to no other request.
 */

const DECISION_STORAGE_KEY = "studio.decisionToken";

type Paired = { base: string; token: string };

let pendingCode: string | undefined;
let redeeming: Promise<PairingResult> | undefined;
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
 * reach the daemon is kept for the next attempt. A caller that asks while an
 * exchange is in flight shares its result: the code works once, so a second
 * request would be refused after the first had already paired the page.
 */
export function redeemPairingCode(apiBase: string): Promise<PairingResult> {
  if (redeeming === undefined) {
    redeeming = exchangePairingCode(apiBase).finally(() => {
      redeeming = undefined;
    });
  }
  return redeeming;
}

async function exchangePairingCode(apiBase: string): Promise<PairingResult> {
  const code = pendingCode;
  if (code === undefined) return "refused";
  let response: Response;
  try {
    response = await fetch(`${apiBase}/api/studio/pair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
  } catch {
    return "unreachable";
  }
  if (response.status >= 500) return "unreachable";
  pendingCode = undefined;
  if (!response.ok) return "refused";
  let token: unknown;
  let decisionToken: unknown;
  try {
    const body = (await response.json()) as { token?: unknown; decision_token?: unknown } | null;
    token = body?.token;
    decisionToken = body?.decision_token;
  } catch {
    return "refused";
  }
  if (typeof token !== "string" || !token) return "refused";
  paired = { base: apiBase, token };
  writeDecisionCapability(
    typeof decisionToken === "string" && decisionToken
      ? { base: apiBase, token: decisionToken }
      : undefined,
  );
  return "paired";
}

/** The bearer this page was paired with, for requests to the same daemon only. */
export function pairedBearer(apiBase: string): string | undefined {
  return paired !== undefined && paired.base === apiBase ? paired.token : undefined;
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
  writeDecisionCapability(undefined);
}
