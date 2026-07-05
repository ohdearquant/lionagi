import type { ButtonHTMLAttributes } from "react";
import { IconArrowLeft } from "./icons";

export interface DrawerBackButtonProps extends Omit<
  ButtonHTMLAttributes<HTMLButtonElement>,
  "className" | "type"
> {
  /** Visible label text. */
  children: string;
}

/** Back affordance rendered at the top of a detail drawer in narrow (collapsed) mode. */
export default function DrawerBackButton({ children, ...rest }: DrawerBackButtonProps) {
  return (
    <button
      type="button"
      {...rest}
      className="flex shrink-0 items-center gap-1.5 border-b border-edge px-4 py-2 text-[length:var(--t-xs)] text-content-muted"
    >
      <IconArrowLeft size={11} strokeWidth={2} /> {children}
    </button>
  );
}
