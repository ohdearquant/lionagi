import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

export interface ConfirmButtonProps {
  /** Label shown in the idle state. */
  idleLabel: ReactNode;
  /** Label shown once armed (awaiting the confirming click). */
  confirmLabel: ReactNode;
  /** Called only on the confirming second click. */
  onConfirm: () => void;
  /** How long (ms) to wait before auto-disarming. Default: 3000. */
  timeout?: number;
  className?: string;
  disabled?: boolean;
}

/**
 * Two-click destructive action — first click arms, second click executes.
 * Auto-disarms after `timeout` ms if the confirming click never arrives.
 */
export default function ConfirmButton({
  idleLabel,
  confirmLabel,
  onConfirm,
  timeout = 3000,
  className,
  disabled,
}: ConfirmButtonProps) {
  const [armed, setArmed] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  function disarm() {
    if (timerRef.current) clearTimeout(timerRef.current);
    setArmed(false);
  }

  function handleClick() {
    if (disabled) return;
    if (!armed) {
      setArmed(true);
      timerRef.current = setTimeout(disarm, timeout);
    } else {
      disarm();
      onConfirm();
    }
  }

  useEffect(
    () => () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    },
    [],
  );

  return (
    <button
      type="button"
      disabled={disabled}
      onClick={handleClick}
      className={[
        "w-full rounded border px-3 py-1.5 text-left text-meta transition-colors disabled:opacity-50",
        armed
          ? "border-status-error/40 bg-status-error-bg text-status-error"
          : "border-edge bg-surface-overlay text-content-secondary hover:border-status-error/40 hover:text-status-error",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {armed ? confirmLabel : idleLabel}
    </button>
  );
}
