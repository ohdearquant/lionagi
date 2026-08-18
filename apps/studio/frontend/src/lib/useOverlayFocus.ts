import { useCallback, useEffect, useRef, type RefObject } from "react";
import {
  isTopmostOverlay,
  type OverlayLayer,
  popOverlay,
  pushOverlay,
  subscribeOverlayChange,
} from "./overlayStack";

const FOCUSABLE = [
  "button:not([disabled])",
  "[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

interface Options {
  /** Names the registration; read only when debugging the stack. */
  description: string;
  layer?: OverlayLayer;
  dialogRef: RefObject<HTMLElement | null>;
  /** Runs when Escape arrives and this overlay owns the keyboard. */
  onEscape: () => void;
  /** Preferred landing spot for the caret; falls back to the first focusable child. */
  initialFocusRef?: RefObject<HTMLElement | null>;
}

export interface OverlayFocus {
  /** Re-offer the claim once content the shell was waiting on has rendered. */
  claimFocus: () => void;
  /** True while nothing is painted above this overlay. */
  ownsKeyboard: () => boolean;
}

/**
 * The one place overlay keyboard ownership is enforced: registration, the focus
 * claim, Tab containment, Escape routing and focus restore. Dialog shells carry
 * none of it, so there is a single implementation to keep correct rather than
 * copies that drift apart.
 */
export function useOverlayFocus({
  description,
  layer,
  dialogRef,
  onEscape,
  initialFocusRef,
}: Options): OverlayFocus {
  const onEscapeRef = useRef(onEscape);
  useEffect(() => {
    onEscapeRef.current = onEscape;
  }, [onEscape]);

  const initialRef = useRef(initialFocusRef);
  useEffect(() => {
    initialRef.current = initialFocusRef;
  }, [initialFocusRef]);

  // Captured during first render: child effects run before ours, so by mount the trigger is gone.
  const previouslyFocusedRef = useRef<HTMLElement | null | undefined>(undefined);
  if (previouslyFocusedRef.current === undefined) {
    previouslyFocusedRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
  }

  const overlayRef = useRef<symbol | null>(null);
  const claimRef = useRef<() => void>(() => {});

  useEffect(() => {
    const previouslyFocused = previouslyFocusedRef.current ?? null;
    const dialog = dialogRef.current;
    const overlay = pushOverlay(description, layer);
    overlayRef.current = overlay;

    const focusable = () =>
      Array.from(dialog?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []).filter(
        (element) =>
          element.tabIndex >= 0 &&
          !element.hidden &&
          element.getAttribute("aria-hidden") !== "true",
      );
    const holdsFocus = () => dialog?.contains(document.activeElement) ?? false;

    // Where this hook last put the caret. A shell whose fields arrive later
    // lands its first claim on a fallback, and the re-offer afterwards has to
    // be able to correct that without stomping focus a child or the operator
    // moved deliberately.
    let placed: HTMLElement | null = null;

    const claimFocus = () => {
      if (!isTopmostOverlay(overlay)) return;
      const preferred = initialRef.current?.current ?? null;
      if (holdsFocus() && (!preferred || preferred === placed || document.activeElement !== placed))
        return;
      const target = preferred ?? focusable()[0] ?? dialog ?? null;
      target?.focus();
      placed = target;
    };
    claimRef.current = claimFocus;

    const onKey = (e: KeyboardEvent) => {
      if (!isTopmostOverlay(overlay)) return;
      if (e.key === "Escape") {
        e.preventDefault();
        onEscapeRef.current();
        return;
      }
      if (e.key !== "Tab" || !dialog) return;
      const items = focusable();
      // A dialog still loading has nothing to land on; the key stays inside regardless.
      if (items.length === 0) {
        e.preventDefault();
        dialog.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (!dialog.contains(document.activeElement)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };

    claimFocus();
    // Read after the claim, so a child that focused itself counts as holding it.
    let tookFocus = holdsFocus();
    // Mounting under an overlay means the claim was declined; take it when that one closes.
    const unsubscribe = subscribeOverlayChange(() => {
      claimFocus();
      tookFocus = tookFocus || holdsFocus();
    });
    document.addEventListener("keydown", onKey);

    return () => {
      claimRef.current = () => {};
      unsubscribe();
      document.removeEventListener("keydown", onKey);
      // Ownership is read BEFORE the pop: afterwards the token is unregistered and
      // always reads false, which would silently retire the restore entirely.
      const restoresFocus = tookFocus && isTopmostOverlay(overlay);
      // Restore before the pop too, so that whatever is beneath gets the last
      // word: popping notifies it, and its claim should outrank this launcher.
      if (restoresFocus && previouslyFocused?.isConnected) previouslyFocused.focus();
      popOverlay(overlay);
      overlayRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return {
    claimFocus: useCallback(() => claimRef.current(), []),
    ownsKeyboard: useCallback(
      () => overlayRef.current !== null && isTopmostOverlay(overlayRef.current),
      [],
    ),
  };
}
