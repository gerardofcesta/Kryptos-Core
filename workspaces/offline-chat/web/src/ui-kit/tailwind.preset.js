/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: ["class", '[data-theme="dark"]'],
  theme: {
    extend: {
      fontFamily: {
        sans: ["var(--wdk-font-sans)"],
        mono: ["var(--wdk-font-mono)"],
      },
      colors: {
        bg: "var(--wdk-bg)",
        surface: {
          DEFAULT: "var(--wdk-surface)",
          muted: "var(--wdk-surface-muted)",
          elevated: "var(--wdk-surface-elevated)",
        },
        fg: {
          DEFAULT: "var(--wdk-fg)",
          muted: "var(--wdk-fg-muted)",
          subtle: "var(--wdk-fg-subtle)",
        },
        border: {
          DEFAULT: "var(--wdk-border)",
          strong: "var(--wdk-border-strong)",
        },
        primary: {
          DEFAULT: "var(--wdk-primary)",
          hover: "var(--wdk-primary-hover)",
          fg: "var(--wdk-primary-fg)",
        },
        success: {
          DEFAULT: "var(--wdk-success)",
          bg: "var(--wdk-success-bg)",
        },
        warning: {
          DEFAULT: "var(--wdk-warning)",
          bg: "var(--wdk-warning-bg)",
        },
        danger: {
          DEFAULT: "var(--wdk-danger)",
          hover: "var(--wdk-danger-hover)",
          bg: "var(--wdk-danger-bg)",
        },
        info: {
          DEFAULT: "var(--wdk-info)",
          bg: "var(--wdk-info-bg)",
        },
        ai: {
          DEFAULT: "var(--wdk-ai)",
          bg: "var(--wdk-ai-bg)",
        },
      },
      borderRadius: {
        sm: "var(--wdk-radius-sm)",
        md: "var(--wdk-radius-md)",
        lg: "var(--wdk-radius-lg)",
        pill: "var(--wdk-radius-pill)",
      },
      boxShadow: {
        "wdk-sm": "var(--wdk-shadow-sm)",
        "wdk-md": "var(--wdk-shadow-md)",
      },
    },
  },
};
