/**
 * Pairing with the local daemon, for a page on another origin.
 *
 * A daemon with no auth token configured refuses API requests from a page that
 * is not on this machine unless the page sends the bearer of the daemon's
 * launch. `li studio` opens the page with a one-time code in the URL fragment
 * (`#pair=<code>`), which a browser never sends to a server. The code is read
 * once at startup and removed from the address bar, then exchanged with the
 * daemon for the bearer. The bearer is held in memory only: nothing is written
 * to storage, so a reload loses it and `li studio` pairs the page again.
 */

let pendingCode: string | undefined;
let paired: { base: string; token: string } | undefined;

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
  return pendingCode !== undefined;
}

export type PairingResult = "paired" | "refused" | "unreachable";

/**
 * Exchange the pending code with the daemon at `apiBase`. A refused code is
 * dropped, since it was used, expired or never issued; a code that did not
 * reach the daemon is kept for the next attempt.
 */
export async function redeemPairingCode(apiBase: string): Promise<PairingResult> {
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
  try {
    token = ((await response.json()) as { token?: unknown } | null)?.token;
  } catch {
    return "refused";
  }
  if (typeof token !== "string" || !token) return "refused";
  paired = { base: apiBase, token };
  return "paired";
}

/** The bearer this page was paired with, for requests to the same daemon only. */
export function pairedBearer(apiBase: string): string | undefined {
  return paired !== undefined && paired.base === apiBase ? paired.token : undefined;
}

export function resetPairingForTests(): void {
  pendingCode = undefined;
  paired = undefined;
}
