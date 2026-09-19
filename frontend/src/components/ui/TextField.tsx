import { forwardRef, useId } from "react";
import type { InputHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

export interface TextFieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
  hint?: string;
  errorMessage?: string;
}

export const TextField = forwardRef<HTMLInputElement, TextFieldProps>(
  ({ label, hint, errorMessage, id, className, ...props }, ref) => {
    const generatedId = useId();
    const inputId = id ?? generatedId;
    const hintId = hint ? `${inputId}-hint` : undefined;
    const errorId = errorMessage ? `${inputId}-error` : undefined;

    return (
      <div className="flex flex-col gap-1.5">
        <label htmlFor={inputId} className="text-sm font-medium text-ink-soft">
          {label}
        </label>
        <input
          ref={ref}
          id={inputId}
          aria-describedby={cn(hintId, errorId) || undefined}
          aria-invalid={Boolean(errorMessage) || undefined}
          className={cn(
            "h-10 rounded-md border border-border bg-surface px-3 text-[0.95rem] text-ink placeholder:text-ink-faint",
            "transition-[border-color,box-shadow] duration-[var(--duration-fast)] ease-[var(--ease-physical)]",
            "hover:border-border-strong",
            "focus-visible:border-accent-teal focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring/40",
            errorMessage && "border-danger focus-visible:border-danger focus-visible:ring-danger/30",
            className,
          )}
          {...props}
        />
        {hint && !errorMessage && (
          <p id={hintId} className="text-xs text-ink-faint">
            {hint}
          </p>
        )}
        {errorMessage && (
          <p id={errorId} className="text-xs text-danger">
            {errorMessage}
          </p>
        )}
      </div>
    );
  },
);

TextField.displayName = "TextField";
