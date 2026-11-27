import * as React from "react";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md" | "lg";

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
}

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary:
    "bg-[var(--wdk-primary)] text-[var(--wdk-primary-fg)] hover:bg-[var(--wdk-primary-hover)] shadow-[var(--wdk-shadow-sm)]",
  secondary:
    "bg-[var(--wdk-surface)] text-[var(--wdk-fg)] border border-[var(--wdk-border-strong)] hover:bg-[var(--wdk-surface-muted)]",
  ghost:
    "bg-transparent text-[var(--wdk-fg-muted)] hover:bg-[var(--wdk-surface-muted)] hover:text-[var(--wdk-fg)]",
  danger:
    "bg-[var(--wdk-danger)] text-white hover:bg-[var(--wdk-danger-hover)] shadow-[var(--wdk-shadow-sm)]",
};

const SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: "h-8 px-3 text-xs rounded-[var(--wdk-radius-sm)]",
  md: "h-9 px-4 text-sm rounded-[var(--wdk-radius-md)]",
  lg: "h-11 px-5 text-base rounded-[var(--wdk-radius-md)]",
};

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  (
    {
      variant = "primary",
      size = "md",
      loading = false,
      disabled,
      className,
      type = "button",
      children,
      ...props
    },
    ref
  ) => {
    const isDisabled = Boolean(disabled || loading);
    const classes = [
      "inline-flex items-center justify-center gap-2 font-medium transition-colors",
      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--wdk-ring)]",
      "disabled:pointer-events-none disabled:opacity-50 select-none cursor-pointer",
      VARIANT_CLASSES[variant],
      SIZE_CLASSES[size],
      className,
    ]
      .filter(Boolean)
      .join(" ");

    return (
      <button
        ref={ref}
        type={type}
        disabled={isDisabled}
        aria-busy={loading || undefined}
        className={classes}
        {...props}
      >
        {loading && (
          <span
            aria-hidden="true"
            className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent"
          />
        )}
        {children}
      </button>
    );
  }
);

Button.displayName = "Button";
