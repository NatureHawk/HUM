import { forwardRef } from "react";
import type { HTMLAttributes } from "react";
import { cn } from "@/lib/cn";

export type SpinnerSize = "sm" | "md";

export interface SpinnerProps extends HTMLAttributes<HTMLSpanElement> {
  size?: SpinnerSize;
  label?: string;
}

const sizeClasses: Record<SpinnerSize, string> = {
  sm: "h-3.5 w-3.5 border-[1.5px]",
  md: "h-5 w-5 border-2",
};

/** A restrained loading indicator — a single rotating hairline arc, no glow. */
export const Spinner = forwardRef<HTMLSpanElement, SpinnerProps>(
  ({ size = "md", label = "Loading", className, ...props }, ref) => {
    return (
      <span
        ref={ref}
        role="status"
        aria-label={label}
        className={cn("inline-flex items-center justify-center", className)}
        {...props}
      >
        <span
          className={cn(
            "animate-spin rounded-full border-border-strong border-t-accent-teal motion-reduce:animate-none",
            sizeClasses[size],
          )}
        />
        <span className="sr-only">{label}</span>
      </span>
    );
  },
);

Spinner.displayName = "Spinner";
