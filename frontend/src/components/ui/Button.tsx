import { forwardRef } from "react";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/cn";

export type ButtonVariant = "primary" | "quiet" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  children: ReactNode;
}

const variantClasses: Record<ButtonVariant, string> = {
  primary:
    "bg-accent-amber text-surface-raised border border-accent-amber hover:bg-accent-amber-hover hover:border-accent-amber-hover active:bg-accent-amber-active active:border-accent-amber-active",
  quiet:
    "bg-surface text-ink border border-border hover:border-border-strong hover:bg-canvas-raised active:bg-canvas-raised",
  ghost:
    "bg-transparent text-ink-soft border border-transparent hover:bg-canvas-raised hover:text-ink",
  danger:
    "bg-transparent text-danger border border-danger/40 hover:bg-danger-soft active:bg-danger-soft",
};

const sizeClasses: Record<ButtonSize, string> = {
  sm: "h-8 px-3 text-sm gap-1.5",
  md: "h-10 px-4 text-[0.95rem] gap-2",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ variant = "primary", size = "md", className, children, ...props }, ref) => {
    return (
      <button
        ref={ref}
        className={cn(
          "inline-flex items-center justify-center rounded-md font-sans font-medium",
          "transition-[background-color,border-color,color,transform] duration-[var(--duration-fast)] ease-[var(--ease-physical)]",
          "disabled:cursor-not-allowed disabled:opacity-50",
          "active:translate-y-px",
          variantClasses[variant],
          sizeClasses[size],
          className,
        )}
        {...props}
      >
        {children}
      </button>
    );
  },
);

Button.displayName = "Button";
