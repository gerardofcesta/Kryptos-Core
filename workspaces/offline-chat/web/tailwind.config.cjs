/** @type {import('tailwindcss').Config} */
const path = require("node:path");
const wdkPreset = require(path.resolve(__dirname, "src/ui-kit/tailwind.preset.js"));

module.exports = {
  presets: [wdkPreset],
  content: [
    "./index.html",
    "./src/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {},
  },
  plugins: [],
};
