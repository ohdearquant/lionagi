import type { ButtonHTMLAttributes, ReactNode } from "react";

export type ButtonVariant =
  | "primary"
  | "secondary"
  | "ghost"
  | "danger"
  | "toggle";

export interface ButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "className"> {
  variant?: ButtonVariant;
  size?: "sm" | "md";
  // For toggle variants, indicates the "on" state.
  active?: boolean;
  // Optional leading and trailing slots — small text or glyph nodes
  leading?: ReactNode;
  trailing?: ReactNode;
  className?: string;
}

const SIZE_CLASS: Record<NonNullable<ButtonProps["size"]>, string> = {
  sm: "h-7 px-2.5 text-meta",
  md: "h-8 px-3 text-body",
};

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  primary:
    "border border-interactive-primary bg-interactive-primary text-content-inverse hover:bg-interactive-primary-hover disabled:opacity-50 disabled:cursor-not-allowed",
  secondary:
    "border border-edge bg-surface-raised text-content-primary hover:border-edge-strong hover:bg-surface-overlay disabled:opacity-50 disabled:cursor-not-allowed",
  ghost:
    "border border-transparent bg-transparent text-content-secondary hover:text-content-primary hover:bg-surface-overlay disabled:opacity-50 disabled:cursor-not-allowed",
  danger:
    "border border-status-error/40 bg-status-error-bg text-status-error hover:bg-status-error/15 disabled:opacity-50 disabled:cursor-not-allowed",
  toggle: "", // resolved at runtime
};

const TOGGLE_ON =
  "border border-status-success/40 bg-status-success-bg text-status-success hover:bg-status-success/15";
const TOGGLE_OFF =
  "border border-edge bg-surface-raised text-content-muted hover:text-content-primary hover:border-edge-strong";

export default function Button({
  variant = "secondary",
  size = "md",
  active,
  leading,
  trailing,
  children,
  className,
  type = "button",
  ...rest
}: ButtonProps) {
  const variantCls =
    variant === "toggle" ? (active ? TOGGLE_ON : TOGGLE_OFF) : VARIANT_CLASS[variant];

  return (
    <button
      type={type}
      {...rest}
      className={[
        "inline-flex items-center justify-center gap-1.5 rounded-md font-medium transition-colors focus:outline-none focus:ring-1 focus:ring-interactive-primary",
        SIZE_CLASS[size],
        variantCls,
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {leading ? <span className="shrink-0">{leading}</span> : null}
      <span className="truncate">{children}</span>
      {trailing ? <span className="shrink-0">{trailing}</span> : null}
    </button>
  );
}
