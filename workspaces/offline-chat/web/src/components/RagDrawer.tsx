import React, { useCallback, useEffect, useState } from "react";
import { BookOpen, FileUp, Search, ShieldAlert, Trash2, X } from "lucide-react";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle } from "@wdk";
import { apiJson } from "../lib/api";
import type { RagHit, RagStatus } from "../types";

interface RagDrawerProps {
  open: boolean;
  onClose: () => void;
  onInjectContext: (contextText: string) => void;
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
  }
  return btoa(binary);
}

export const RagDrawer: React.FC<RagDrawerProps> = ({ open, onClose, onInjectContext }) => {
  const [mode, setMode] = useState<"ghost" | "vault">("ghost");
  const [collection, setCollection] = useState("default");
  const [ttlSeconds, setTtlSeconds] = useState("1800");
  const [enableOcr, setEnableOcr] = useState(true);
  const [filename, setFilename] = useState("notes.md");
  const [textContent, setTextContent] = useState("");
  const [queryText, setQueryText] = useState("");
  const [topK, setTopK] = useState(5);
  const [hits, setHits] = useState<RagHit[]>([]);
  const [ragStatus, setRagStatus] = useState<RagStatus | null>(null);
  const [statusMsg, setStatusMsg] = useState<string>("");
  const [busy, setBusy] = useState(false);

  const refreshStatus = useCallback(async () => {
    const res = await apiJson<RagStatus>("/api/rag/status");
    if (res.ok) {
      setRagStatus(res.data);
    }
  }, []);

  useEffect(() => {
    if (open) {
      void refreshStatus();
    }
  }, [open, refreshStatus]);

  if (!open) return null;

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    setStatusMsg(`Indicizzazione ${file.name} in modalità ${mode}...`);
    try {
      const buf = await file.arrayBuffer();
      const b64 = arrayBufferToBase64(buf);
      const payload: Record<string, unknown> = {
        filename: file.name,
        content_base64: b64,
        mode,
        collection: collection.trim() || "default",
        enable_ocr: enableOcr,
      };
      if (mode === "ghost" && Number(ttlSeconds) > 0) {
        payload.ttl_seconds = Number(ttlSeconds);
      }
      const res = await apiJson<{ ok: boolean; chunk_count?: number; duration_ms?: number; error?: string }>(
        "/api/rag/ingest",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        }
      );
      if (res.ok && res.data.ok) {
        setStatusMsg(`Ingerito ${file.name}: ${res.data.chunk_count ?? 0} chunk (${res.data.duration_ms ?? 0} ms)`);
        await refreshStatus();
      } else {
        setStatusMsg(res.data.error || `Errore ingest (${res.status})`);
      }
    } finally {
      setBusy(false);
      e.target.value = "";
    }
  };

  const handleTextIngest = async () => {
    if (!textContent.trim()) {
      setStatusMsg("Inserisci del testo da indicizzare.");
      return;
    }
    setBusy(true);
    setStatusMsg(`Indicizzazione testo in ${mode}...`);
    try {
      const payload: Record<string, unknown> = {
        filename: filename.trim() || "snippet.md",
        content: textContent,
        mode,
        collection: collection.trim() || "default",
        enable_ocr: false,
      };
      if (mode === "ghost" && Number(ttlSeconds) > 0) {
        payload.ttl_seconds = Number(ttlSeconds);
      }
      const res = await apiJson<{ ok: boolean; chunk_count?: number; duration_ms?: number; error?: string }>(
        "/api/rag/ingest",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        }
      );
      if (res.ok && res.data.ok) {
        setStatusMsg(`Indicizzati ${res.data.chunk_count ?? 0} chunk in ${res.data.duration_ms ?? 0} ms`);
        setTextContent("");
        await refreshStatus();
      } else {
        setStatusMsg(res.data.error || `Errore ingest (${res.status})`);
      }
    } finally {
      setBusy(false);
    }
  };

  const handleQuery = async () => {
    if (!queryText.trim()) return;
    setBusy(true);
    try {
      const res = await apiJson<{ ok: boolean; results?: RagHit[]; count?: number; duration_ms?: number; error?: string }>(
        "/api/rag/query",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            query: queryText.trim(),
            mode,
            collection: collection.trim() || "default",
            top_k: topK,
          }),
        }
      );
      if (res.ok && res.data.ok) {
        setHits(res.data.results || []);
        setStatusMsg(`Trovati ${res.data.count ?? 0} passaggi in ${res.data.duration_ms ?? 0} ms`);
      } else {
        setStatusMsg(res.data.error || `Errore query (${res.status})`);
      }
    } finally {
      setBusy(false);
    }
  };

  const handleClear = async (clearMode: "ghost" | "vault" | "all") => {
    setBusy(true);
    try {
      const res = await apiJson<{ ok: boolean; cleared_chunks?: number; error?: string }>("/api/rag/clear", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: clearMode, collection: collection.trim() || null }),
      });
      if (res.ok && res.data.ok) {
        setHits([]);
        setStatusMsg(`Puliti ${res.data.cleared_chunks ?? 0} chunk (${clearMode})`);
        await refreshStatus();
      } else {
        setStatusMsg(res.data.error || `Errore clear (${res.status})`);
      }
    } finally {
      setBusy(false);
    }
  };

  const injectAllHits = () => {
    if (hits.length === 0) return;
    const formatted = hits
      .map((h) => `${h.citation}\n${h.text}`)
      .join("\n\n---\n\n");
    onInjectContext(`Contesto RAG locale (${mode} · ${collection}):\n\n${formatted}`);
    setStatusMsg("Contesto RAG inserito nel compositore chat.");
  };

  const ghostTotal = ragStatus?.ghost?.total_chunks ?? 0;
  const vaultTotal = ragStatus?.vault?.total_chunks ?? 0;

  return (
    <aside className="w-full lg:w-96 shrink-0 border-l border-border bg-surface flex flex-col h-full overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-surface-muted">
        <div className="flex items-center gap-2">
          <BookOpen className="h-4 w-4 text-primary" />
          <span className="font-semibold text-sm">RAG Locale · Ghost vs Vault</span>
        </div>
        <Button variant="ghost" size="sm" onClick={onClose} aria-label="Chiudi drawer RAG">
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="flex-1 overflow-y-auto p-3.5 space-y-3.5">
        <div className="grid grid-cols-2 gap-2">
          <button
            type="button"
            onClick={() => setMode("ghost")}
            className={`p-2 rounded-md border text-left transition-colors ${
              mode === "ghost"
                ? "border-primary bg-info-bg text-fg"
                : "border-border bg-surface-muted text-fg-muted"
            }`}
          >
            <div className="flex items-center justify-between text-xs font-semibold">
              <span>Ghost RAG</span>
              <Badge variant="success">RAM-Only</Badge>
            </div>
            <div className="text-[11px] text-fg-muted mt-0.5">{ghostTotal} chunk in RAM</div>
          </button>
          <button
            type="button"
            onClick={() => setMode("vault")}
            className={`p-2 rounded-md border text-left transition-colors ${
              mode === "vault"
                ? "border-primary bg-warning-bg text-fg"
                : "border-border bg-surface-muted text-fg-muted"
            }`}
          >
            <div className="flex items-center justify-between text-xs font-semibold">
              <span>Vault RAG</span>
              <Badge variant="warning">Persistente</Badge>
            </div>
            <div className="text-[11px] text-fg-muted mt-0.5">{vaultTotal} chunk su disco</div>
          </button>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div>
            <label htmlFor="ragCollection" className="block text-xs text-fg-muted mb-1">
              Collezione
            </label>
            <input
              id="ragCollection"
              value={collection}
              onChange={(e) => setCollection(e.target.value)}
              className="w-full px-2.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
            />
          </div>
          {mode === "ghost" ? (
            <div>
              <label htmlFor="ragTtl" className="block text-xs text-fg-muted mb-1">
                TTL RAM (sec)
              </label>
              <input
                id="ragTtl"
                type="number"
                min={10}
                max={86400}
                value={ttlSeconds}
                onChange={(e) => setTtlSeconds(e.target.value)}
                className="w-full px-2.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
              />
            </div>
          ) : (
            <div className="flex items-end pb-1">
              <span className="text-[11px] text-warning flex items-center gap-1">
                <ShieldAlert className="h-3.5 w-3.5" /> Scrittura in rag-vault/
              </span>
            </div>
          )}
        </div>

        <Card>
          <CardHeader className="p-3 pb-2">
            <CardTitle className="text-xs uppercase tracking-wider text-fg-muted">
              1. Ingestione Documento (PDF/DOCX/MD/TXT)
            </CardTitle>
          </CardHeader>
          <CardContent className="p-3 pt-0 space-y-2">
            <div className="flex items-center justify-between gap-2">
              <label className="inline-flex items-center gap-1.5 text-xs text-fg-muted cursor-pointer">
                <input
                  type="checkbox"
                  checked={enableOcr}
                  onChange={(e) => setEnableOcr(e.target.checked)}
                />
                OCR RapidOCR (PDF scansionati)
              </label>
              <label className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-sm bg-primary text-primary-fg text-xs font-medium cursor-pointer hover:bg-primary-hover">
                <FileUp className="h-3.5 w-3.5" />
                Carica File
                <input
                  type="file"
                  accept=".pdf,.docx,.pptx,.csv,.md,.txt,.py,.ts,.json"
                  onChange={handleFileUpload}
                  className="hidden"
                />
              </label>
            </div>
            <input
              value={filename}
              onChange={(e) => setFilename(e.target.value)}
              placeholder="Nome documento (es. notes.md)"
              className="w-full px-2.5 py-1 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
            />
            <textarea
              rows={3}
              value={textContent}
              onChange={(e) => setTextContent(e.target.value)}
              placeholder="Oppure incolla testo/appunti da indicizzare..."
              className="w-full px-2.5 py-1.5 text-xs rounded-sm border border-border bg-bg text-fg"
            />
            <div className="flex gap-2">
              <Button size="sm" variant="secondary" loading={busy} onClick={handleTextIngest} className="flex-1">
                Indicizza Testo
              </Button>
              <Button size="sm" variant="danger" disabled={busy} onClick={() => handleClear(mode)}>
                <Trash2 className="h-3.5 w-3.5" /> Wipe {mode === "ghost" ? "RAM" : "Vault"}
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="p-3 pb-2">
            <CardTitle className="text-xs uppercase tracking-wider text-fg-muted">
              2. Ricerca Citazioni
            </CardTitle>
          </CardHeader>
          <CardContent className="p-3 pt-0 space-y-2">
            <div className="flex gap-1.5">
              <input
                value={queryText}
                onChange={(e) => setQueryText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void handleQuery();
                }}
                placeholder="Query semantica / keyword..."
                className="flex-1 px-2.5 py-1.5 text-xs rounded-sm border border-border bg-bg text-fg"
              />
              <input
                type="number"
                min={1}
                max={20}
                value={topK}
                onChange={(e) => setTopK(Number(e.target.value) || 5)}
                className="w-12 px-1.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg text-center"
                title="Top K"
              />
              <Button size="sm" loading={busy} onClick={handleQuery}>
                <Search className="h-3.5 w-3.5" />
              </Button>
            </div>

            {statusMsg && <div className="text-[11px] font-mono text-fg-muted">{statusMsg}</div>}

            {hits.length > 0 && (
              <div className="space-y-2 pt-1">
                <Button size="sm" variant="primary" onClick={injectAllHits} className="w-full">
                  Inserisci {hits.length} citazioni nel prompt
                </Button>
                {hits.map((hit, idx) => (
                  <div
                    key={`${hit.filename}-${idx}`}
                    className="p-2 rounded-sm border border-border bg-surface-muted space-y-1"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <button
                        type="button"
                        onClick={() => onInjectContext(`${hit.citation}\n${hit.text}`)}
                        className="text-[11px] font-mono font-semibold text-primary hover:underline text-left"
                        title="Clicca per inserire questa citazione nel prompt"
                      >
                        {hit.citation}
                      </button>
                      <Badge variant="neutral">{hit.score.toFixed(3)}</Badge>
                    </div>
                    <p className="text-xs text-fg-muted line-clamp-4">{hit.text}</p>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </aside>
  );
};
