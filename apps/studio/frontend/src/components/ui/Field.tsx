/**
 * Form field primitives — one input dialect for every form surface.
 * FieldLabel wraps a control with the label/hint column; Input, TextArea,
 * and Select share the same field chrome.
 */
import type {
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";

const FIELD_CLASS =
  "rounded border border-edge bg-surface-base px-2.5 text-body text-content-primary placeholder:text-content-muted focus:border-interactive-primary focus:outline-none disabled:cursor-not-allowed disabled:opacity-50";

function cls(...parts: Array<string | false | undefined>) {
  return parts.filter(Boolean).join(" ");
}

export interface FieldLabelProps {
  label: ReactNode;
  required?: boolean;
  /** Muted helper line under the control. */
  hint?: ReactNode;
  children: ReactNode;
  className?: string;
}

export function FieldLabel({ label, required, hint, children, className }: FieldLabelProps) {
  return (
    <label className={cls("flex flex-col gap-1", className)}>
      <span className="text-meta font-medium text-content-secondary">
        {label}
        {required && <span className="text-status-error"> *</span>}
      </span>
      {children}
      {hint != null && <span className="text-meta text-content-muted">{hint}</span>}
    </label>
  );
}

export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "className"> {
  /** Mono field — code identities (names, cron, models, paths). */
  mono?: boolean;
  className?: string;
}

export function Input({ mono, className, ...rest }: InputProps) {
  return <input {...rest} className={cls("h-8", FIELD_CLASS, mono && "font-data", className)} />;
}

export interface TextAreaProps extends Omit<
  TextareaHTMLAttributes<HTMLTextAreaElement>,
  "className"
> {
  mono?: boolean;
  className?: string;
}

export function TextArea({ mono, rows = 3, className, ...rest }: TextAreaProps) {
  return (
    <textarea
      rows={rows}
      {...rest}
      className={cls("resize-y py-1.5", FIELD_CLASS, mono && "font-data", className)}
    />
  );
}

export interface SelectProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, "className"> {
  className?: string;
}

export function Select({ className, children, ...rest }: SelectProps) {
  return (
    <select {...rest} className={cls("h-8", FIELD_CLASS, className)}>
      {children}
    </select>
  );
}
