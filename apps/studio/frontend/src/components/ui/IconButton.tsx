import type { ButtonHTMLAttributes, ReactNode } from "react";

export interface IconButtonProps extends Omit<
  ButtonHTMLAttributes<HTMLButtonElement>,
  "className" | "children"
> {
  /** Icon buttons carry no text — the label is mandatory. */
  "aria-label": string;
  size?: "sm" | "md";
  /** Pressed/active visual state (e.g. a toggled panel). */
  active?: boolean;
  children: ReactNode;
  className?: string;
}

const SIZE_CLASS: Record<NonNullable<IconButtonProps["size"]>, string> = {
  sm: "h-6 w-6",
  md: "h-7 w-7",
};

export default function IconButton({
  size = "sm",
  active,
  children,
  className,
  type = "button",
  ...rest
}: IconButtonProps) {
  return (
    <button
      type={type}
      {...rest}
      className={[
        "focus-ring flex shrink-0 items-center justify-center rounded transition-colors duration-100 disabled:cursor-not-allowed disabled:opacity-50",
        SIZE_CLASS[size],
        active
          ? "bg-surface-overlay text-content-primary"
          : "text-content-muted hover:bg-surface-overlay hover:text-content-primary",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {children}
    </button>
  );
}
