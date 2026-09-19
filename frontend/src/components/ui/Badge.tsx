import { forwardRef } from "react";
import type { HTMLAttributes } from "react";
import { cn } from "@/lib/cn";

export type BadgeTone = "neutral" | "amber" | "teal" | "danger";

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone;
}

const toneClasses: Record<BadgeTone, string> = {
  neutral: "bg-canvas-raised text-ink-soft border-border",
  amber: "bg-accent-amber-soft text-accent-amber-hover border-border-accent",
  teal: "bg-accent-teal-soft text-accent-teal border-accent-teal/30",
  danger: "bg-danger-soft text-danger border-danger/30",
};

/** A small mono-type label, used for identifiers, statuses and metadata. */
export const Badge = forwardRef<HTMLSpanElement, BadgeProps>(
  ({ tone = "neutral", className, ...props }, ref) => {
    return (
      <span
        ref={ref}
        className={cn(
          "inline-flex items-center rounded-xs border px-1.5 py-0.5 font-mono text-[0.7rem] leading-none tracking-wide uppercase",
          toneClasses[tone],
          className,
        )}
        {...props}
      />
    );
  },
);

Badge.displayName = "Badge";
