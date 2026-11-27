import * as React from "react";

export interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  elevated?: boolean;
}

export const Card = React.forwardRef<HTMLDivElement, CardProps>(
  ({ elevated = false, className, ...props }, ref) => {
    const classes = [
      "rounded-[var(--wdk-radius-lg)] border border-[var(--wdk-border)]",
      elevated
        ? "bg-[var(--wdk-surface-elevated)] shadow-[var(--wdk-shadow-md)]"
        : "bg-[var(--wdk-surface)] shadow-[var(--wdk-shadow-sm)]",
      "text-[var(--wdk-fg)]",
      className,
    ]
      .filter(Boolean)
      .join(" ");

    return <div ref={ref} className={classes} {...props} />;
  }
);
Card.displayName = "Card";

export const CardHeader = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={["flex flex-col gap-1.5 p-5 pb-3", className].filter(Boolean).join(" ")}
      {...props}
    />
  )
);
CardHeader.displayName = "CardHeader";

export const CardTitle = React.forwardRef<
  HTMLHeadingElement,
  React.HTMLAttributes<HTMLHeadingElement>
>(({ className, ...props }, ref) => (
  <h3
    ref={ref}
    className={["text-base font-semibold tracking-tight text-[var(--wdk-fg)]", className]
      .filter(Boolean)
      .join(" ")}
    {...props}
  />
));
CardTitle.displayName = "CardTitle";

export const CardDescription = React.forwardRef<
  HTMLParagraphElement,
  React.HTMLAttributes<HTMLParagraphElement>
>(({ className, ...props }, ref) => (
  <p
    ref={ref}
    className={["text-sm text-[var(--wdk-fg-muted)]", className].filter(Boolean).join(" ")}
    {...props}
  />
));
CardDescription.displayName = "CardDescription";

export const CardContent = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={["p-5 pt-0", className].filter(Boolean).join(" ")} {...props} />
  )
);
CardContent.displayName = "CardContent";

export const CardFooter = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={[
        "flex items-center justify-end gap-2 border-t border-[var(--wdk-border)] px-5 py-3",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
      {...props}
    />
  )
);
CardFooter.displayName = "CardFooter";
