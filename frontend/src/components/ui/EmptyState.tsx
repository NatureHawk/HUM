import { forwardRef } from "react";
import type { HTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface EmptyStateProps extends HTMLAttributes<HTMLDivElement> {
  title: string;
  description?: string;
  action?: ReactNode;
}

/**
 * An expressive empty state: a large, editorial display headline paired with
 * a single quiet line of copy and an optional action. This is the one
 * primitive in the system that is allowed real typographic personality.
 */
export const EmptyState = forwardRef<HTMLDivElement, EmptyStateProps>(
  ({ title, description, action, className, ...props }, ref) => {
    return (
      <div
        ref={ref}
        className={cn(
          "flex flex-col items-start gap-3 rounded-lg border border-dashed border-border px-6 py-10",
          className,
        )}
        {...props}
      >
        <p className="max-w-md font-display text-3xl leading-[1.1] text-ink italic sm:text-4xl">
          {title}
        </p>
        {description && (
          <p className="max-w-sm text-sm text-ink-soft">{description}</p>
        )}
        {action && <div className="mt-1">{action}</div>}
      </div>
    );
  },
);

EmptyState.displayName = "EmptyState";
