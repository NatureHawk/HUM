import { forwardRef } from "react";
import type { HTMLAttributes } from "react";
import { cn } from "@/lib/cn";

export type SurfaceElevation = "flat" | "raised";
export type SurfacePadding = "none" | "sm" | "md" | "lg";

export interface SurfaceProps extends HTMLAttributes<HTMLDivElement> {
  elevation?: SurfaceElevation;
  padding?: SurfacePadding;
}

const paddingClasses: Record<SurfacePadding, string> = {
  none: "p-0",
  sm: "p-3",
  md: "p-4",
  lg: "p-6",
};

/**
 * The tactile "paper" container primitive used by cards and panels. A
 * hairline ink border stands in for a shadow; when elevation is "raised" a
 * small, warm-tinted shadow is added — never a big soft blur.
 */
export const Surface = forwardRef<HTMLDivElement, SurfaceProps>(
  ({ elevation = "flat", padding = "md", className, ...props }, ref) => {
    return (
      <div
        ref={ref}
        className={cn(
          "rounded-lg border border-border bg-surface",
          elevation === "raised" ? "shadow-raised" : "shadow-flat",
          paddingClasses[padding],
          className,
        )}
        {...props}
      />
    );
  },
);

Surface.displayName = "Surface";
