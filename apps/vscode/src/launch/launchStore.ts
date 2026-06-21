import type { LaunchRequest } from "../api/types.js";

const _store = new Map<string, LaunchRequest>();

export function rememberLaunch(invocationId: string, req: LaunchRequest): void {
  _store.set(invocationId, req);
}

export function recallLaunch(invocationId: string): LaunchRequest | undefined {
  return _store.get(invocationId);
}
