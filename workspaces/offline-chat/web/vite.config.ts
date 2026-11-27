import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const wdkRoot = path.resolve(__dirname, "src/ui-kit");

const stripExternalDocUrlsPlugin = () => ({
  name: "strip-external-doc-urls",
  generateBundle(_options: unknown, bundle: Record<string, { type: string; code?: string }>) {
    for (const chunk of Object.values(bundle)) {
      if (chunk.type === "chunk" && typeof chunk.code === "string") {
        chunk.code = chunk.code.replaceAll(
          "https://reactjs.org/docs/error-decoder.html",
          "urn:react:error-decoder"
        );
      }
    }
  },
});

export default defineConfig({
  plugins: [react(), stripExternalDocUrlsPlugin()],
  base: "/",
  resolve: {
    alias: {
      "@wdk": wdkRoot,
      react: path.resolve(__dirname, "node_modules/react"),
      "react-dom": path.resolve(__dirname, "node_modules/react-dom"),
    },
    dedupe: ["react", "react-dom"],
  },
  server: {
    port: 5173,
    fs: {
      allow: [__dirname, wdkRoot],
    },
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8787",
        changeOrigin: true,
      },
      "/v1": {
        target: "http://127.0.0.1:8787",
        changeOrigin: true,
      },
      "/mcp": {
        target: "http://127.0.0.1:8787",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        entryFileNames: "static/app.js",
        chunkFileNames: "static/[name]-[hash].js",
        assetFileNames: (assetInfo) => {
          if (assetInfo.name && assetInfo.name.endsWith(".css")) {
            return "static/styles.css";
          }
          return "static/[name]-[hash][extname]";
        },
      },
    },
  },
});
