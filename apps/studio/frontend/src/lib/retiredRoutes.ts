/**
 * Shared redirect helpers for retired routes. The old standalone pages under
 * /playfield, /runs, and /invocations were folded into the unified Fleet
 * surface; every retired route shim funnels through here so search-param
 * preservation and invocation-fetch error handling live in one place instead
 * of being reimplemented (and forgotten) per route.
 */
import { getInvocation as apiGetInvocation, type InvocationDetail } from "@/lib/api";

export type RetiredSearchPrimitive = string | number | boolean;
export type RetiredSearchValue = RetiredSearchPrimitive | RetiredSearchPrimitive[];
export type RetiredSearch = Record<string, RetiredSearchValue>;

export type RetiredRedirectTo = "/fleet" | "/library" | "/schedules" | "/system";

export interface RetiredRedirectTarget<TTo extends RetiredRedirectTo = RetiredRedirectTo> {
  to: TTo;
  search: RetiredSearch;
}

export interface InvocationRedirectDeps {
  getInvocation: (id: string) => Promise<InvocationDetail>;
}

function isRetiredPrimitive(value: unknown): value is RetiredSearchPrimitive {
  if (typeof value === "number" || typeof value === "boolean") return true;
  return typeof value === "string" && value.length > 0;
}

/** First non-empty string from a scalar search value or an array of them. */
export function firstSearchString(value: unknown): string | undefined {
  if (typeof value === "string" && value.length > 0) return value;
  if (Array.isArray(value)) {
    for (const item of value) {
      if (typeof item === "string" && item.length > 0) return item;
    }
  }
  return undefined;
}

/**
 * Keeps non-empty strings, numbers, booleans, and arrays of those primitives.
 * Drops null/undefined, empty strings, empty arrays, objects, and functions
 * so a retired URL's filters carry over without smuggling unexpected shapes
 * into the target route's search.
 */
export function preserveRetiredSearch(search: Record<string, unknown>): RetiredSearch {
  const out: RetiredSearch = {};
  for (const [key, value] of Object.entries(search)) {
    if (Array.isArray(value)) {
      const items = value.filter(isRetiredPrimitive);
      if (items.length > 0) out[key] = items;
      continue;
    }
    if (isRetiredPrimitive(value)) {
      out[key] = value;
    }
  }
  return out;
}

/** Preserved incoming search with sanitized overrides applied on top. */
export function mergeRetiredSearch(
  search: Record<string, unknown>,
  overrides?: Record<string, unknown>,
): RetiredSearch {
  const preserved = preserveRetiredSearch(search);
  const overridden = overrides ? preserveRetiredSearch(overrides) : {};
  return { ...preserved, ...overridden };
}

export function retiredRedirect<TTo extends RetiredRedirectTo>(
  to: TTo,
  search?: Record<string, unknown>,
  overrides?: Record<string, unknown>,
): RetiredRedirectTarget<TTo> {
  return { to, search: mergeRetiredSearch(search ?? {}, overrides) };
}

/**
 * Resolves the retired /invocations/$id redirect target. Selects the
 * incoming ?s= session when it's one of the invocation's own sessions,
 * otherwise falls back to the first returned session. Fetch failures are
 * left to reject — the route's errorComponent must render the real detail,
 * not a silently swallowed fallback.
 */
export async function retiredInvocationRedirect(
  invocationId: string,
  search: Record<string, unknown>,
  deps?: InvocationRedirectDeps,
): Promise<RetiredRedirectTarget<"/fleet">> {
  const fetchInvocation = deps?.getInvocation ?? apiGetInvocation;
  const invocation = await fetchInvocation(invocationId);
  const sessionIds = invocation.sessions.map((session) => session.id);

  if (sessionIds.length === 0) {
    return retiredRedirect("/fleet", search, { invocation: invocationId });
  }

  const requested = firstSearchString(search.s);
  const selected = requested && sessionIds.includes(requested) ? requested : sessionIds[0];

  const overrides: Record<string, unknown> = { s: selected };
  if (sessionIds.length > 1) overrides.sessions = sessionIds;

  return retiredRedirect("/fleet", search, overrides);
}

/** Renders whatever detail a rejected invocation fetch carried, plain text. */
export function formatRetiredRouteError(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string" && error.length > 0) return error;
  return "This link could not be resolved.";
}
