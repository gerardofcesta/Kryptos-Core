import * as React from "react";

export type StatusKind = "online" | "offline" | "busy" | "warning" | "idle";

export interface StatusPillProps extends React.HTMLAttributes<HTMLSpanElement> {
  status: StatusKind;
  label: string;
  pulse?: boolean;
}

const STATUS_STYLE: Record<StatusKind, { pill: string; dot: string }> = {
  online: {
    pill: "bg-[var(--wdk-success-bg)] text-[var(--wdk-success)]",
    dot: "bg-[var(--wdk-success)]",
  },
  offline: {
    pill: "bg-[var(--wdk-danger-bg)] text-[var(--wdk-danger)]",
    dot: "bg-[var(--wdk-danger)]",
  },
  busy: {
    pill: "bg-[var(--wdk-info-bg)] text-[var(--wdk-info)]",
    dot: "bg-[var(--wdk-info)]",
  },
  warning: {
    pill: "bg-[var(--wdk-warning-bg)] text-[var(--wdk-warning)]",
    dot: "bg-[var(--wdk-warning)]",
  },
  idle: {
    pill: "bg-[var(--wdk-surface-muted)] text-[var(--wdk-fg-muted)]",
    dot: "bg-[var(--wdk-fg-subtle)]",
  },
};

export const StatusPill = React.forwardRef<HTMLSpanElement, StatusPillProps>(
  ({ status, label, pulse = false, className, ...props }, ref) => {
    const style = STATUS_STYLE[status];
    const classes = [
      "inline-flex items-center gap-1.5 rounded-[var(--wdk-radius-pill)] px-2.5 py-1",
      "text-xs font-medium whitespace-nowrap",
      style.pill,
      className,
    ]
      .filter(Boolean)
      .join(" ");

    return (
      <span ref={ref} role="status" className={classes} {...props}>
        <span
          aria-hidden="true"
          className={[
            "h-2 w-2 rounded-full",
            style.dot,
            pulse ? "animate-pulse" : "",
          ]
            .filter(Boolean)
            .join(" ")}
        />
        <span>{label}</span>
      </span>
    );
  }
);

StatusPill.displayName = "StatusPill";
