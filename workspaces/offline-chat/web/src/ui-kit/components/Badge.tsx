import * as React from "react";

export type BadgeVariant = "neutral" | "info" | "success" | "warning" | "danger" | "ai";

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  variant?: BadgeVariant;
}

const VARIANT_CLASSES: Record<BadgeVariant, string> = {
  neutral: "bg-[var(--wdk-surface-muted)] text-[var(--wdk-fg-muted)] border-[var(--wdk-border)]",
  info: "bg-[var(--wdk-info-bg)] text-[var(--wdk-info)] border-transparent",
  success: "bg-[var(--wdk-success-bg)] text-[var(--wdk-success)] border-transparent",
  warning: "bg-[var(--wdk-warning-bg)] text-[var(--wdk-warning)] border-transparent",
  danger: "bg-[var(--wdk-danger-bg)] text-[var(--wdk-danger)] border-transparent",
  ai: "bg-[var(--wdk-ai-bg)] text-[var(--wdk-ai)] border-transparent",
};

export const Badge = React.forwardRef<HTMLSpanElement, BadgeProps>(
  ({ variant = "neutral", className, ...props }, ref) => {
    const classes = [
      "inline-flex items-center gap-1 rounded-[var(--wdk-radius-pill)] border px-2.5 py-0.5",
      "text-xs font-medium leading-tight whitespace-nowrap",
      VARIANT_CLASSES[variant],
      className,
    ]
      .filter(Boolean)
      .join(" ");

    return <span ref={ref} className={classes} {...props} />;
  }
);

Badge.displayName = "Badge";
