/**
 * Guarded reload for a stale lazy-loaded route chunk (see main.tsx). A deploy
 * rotates hashed chunk filenames, so a page loaded before the deploy can fail
 * to lazy-load a route chunk it still references. Reload once to pick up the
 * new index; the sessionStorage flag prevents a reload loop if the failure
 * persists for another reason.
 */
export const PRELOAD_RELOAD_KEY = "studio-chunk-reload";

/** Reloads once per session on a preload error; returns whether it reloaded. */
export function handlePreloadError(
  storage: Pick<Storage, "getItem" | "setItem">,
  reload: () => void,
): boolean {
  if (storage.getItem(PRELOAD_RELOAD_KEY)) return false;
  storage.setItem(PRELOAD_RELOAD_KEY, "1");
  reload();
  return true;
}
