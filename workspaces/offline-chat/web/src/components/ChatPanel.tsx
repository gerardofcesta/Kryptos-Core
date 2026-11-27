import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BookOpen, Copy, Cpu, Download, FileText, Lock, Paperclip, Plug, RefreshCw, Send, Sparkles, Square, Trash2, X, Zap } from "lucide-react";
import { Badge, Button, StatusPill } from "@wdk";
import { apiJson, streamGatewayChat } from "../lib/api";
import { extractThinkSegments, renderSafeMarkdownHtml } from "../lib/markdown";
import type { ChatMessage, EphemeralDocMeta, PromptPreset, RagHit, SystemConfig, TelemetryData, VirtualModelAlias } from "../types";
import { IdeConfigsDrawer } from "./IdeConfigsCard";
import { RagDrawer } from "./RagDrawer";

type StreamStage = "connecting" | "checking-backend" | "loading-model" | "prefilling" | "thinking" | "streaming";

interface StarterChip {
  id: string;
  title: string;
  subtitle: string;
  prompt: string;
}

const STARTER_PROMPTS: StarterChip[] = [
  {
    id: "uncensored-rewrite",
    title: "Riscrivi / Traduci senza filtri cloud",
    subtitle: "Elaborazione 100% locale e privata senza censura esterna",
    prompt:
      "Riscrivi e traduci in italiano chiaro e tecnico il seguente testo, mantenendo ogni dettaglio integrale senza omissioni o filtri:\n\n[Incolla qui il tuo testo]",
  },
  {
    id: "ghost-doc-summary",
    title: "Riassumi documento privato (Zero-Disco)",
    subtitle: "Analisi in RAM tramite RAG Drawer senza salvare nulla su disco",
    prompt:
      "Riassumi in 5 punti chiave e una checklist operativa il documento caricato nel RAG Drawer, citando solo i fatti presenti nel testo.",
  },
  {
    id: "overnight-json-extract",
    title: "Estrazione strutturata a costo zero",
    subtitle: "Sfrutta la tua GPU/RAM locale per task ripetitivi e lunghi",
    prompt:
      "Analizza il seguente blocco di testo ed estrai entita, date, decisioni e azioni in una tabella Markdown ordinata e in JSON valido:\n\n[Incolla qui i dati]",
  },
];

function formatStageLabel(stage: StreamStage, thinkingChars: number): string {
  switch (stage) {
    case "connecting":
    case "checking-backend":
      return "Connecting";
    case "loading-model":
      return "Loading into VRAM";
    case "prefilling":
      return "Prefill";
    case "thinking":
      return `Thinking (${thinkingChars} chars)`;
    case "streaming":
      return "Generating";
  }
}

interface ChatPanelProps {
  telemetry: TelemetryData | null;
  systemConfig: SystemConfig | null;
  presets: PromptPreset[];
  selectedPresetId: string;
  onSelectPreset: (id: string) => void;
  systemPrompt: string;
  onSystemPromptChange: (val: string) => void;
}

const VIRTUAL_ALIASES: Array<{ id: VirtualModelAlias; label: string; desc: string }> = [
  { id: "auto", label: "auto (Cascade Router)", desc: "Fast -> Balanced/Heavy su fallback" },
  { id: "local-fast", label: "local-fast", desc: "Ollama Fast Worker" },
  { id: "local-balanced", label: "local-balanced", desc: "Ollama Balanced Tier" },
  { id: "local-heavy", label: "local-heavy", desc: "Colibrì Heavy MoE" },
];

export const ChatPanel: React.FC<ChatPanelProps> = ({
  telemetry,
  systemConfig,
  presets,
  selectedPresetId,
  onSelectPreset,
  systemPrompt,
  onSystemPromptChange,
}) => {
  const [selectedModel, setSelectedModel] = useState<string>("auto");
  const [allowTierSwitch, setAllowTierSwitch] = useState<boolean>(true);
  const [temperature, setTemperature] = useState<number>(0.7);
  const [maxTokens, setMaxTokens] = useState<number>(4096);
  const [appId, setAppId] = useState<string>("local-llm-ui");
  const [workflowId, setWorkflowId] = useState<string>("interactive-chat");
  const [jsonMode, setJsonMode] = useState<boolean>(false);
  const [promptText, setPromptText] = useState<string>("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState<boolean>(false);
  const [streamingMsgId, setStreamingMsgId] = useState<string | null>(null);
  const [streamStage, setStreamStage] = useState<StreamStage>("connecting");
  const [streamElapsedMs, setStreamElapsedMs] = useState<number>(0);
  const [userToggledThinking, setUserToggledThinking] = useState<Record<string, boolean>>({});
  const [tierNotice, setTierNotice] = useState<string | null>(null);
  const [ragOpen, setRagOpen] = useState<boolean>(false);
  const [ideOpen, setIdeOpen] = useState<boolean>(false);
  const [gatewayModels, setGatewayModels] = useState<string[]>([]);
  const [ephemeralDocs, setEphemeralDocs] = useState<EphemeralDocMeta[]>([]);
  const [ephemeralUploading, setEphemeralUploading] = useState<boolean>(false);
  const [dragActive, setDragActive] = useState<boolean>(false);
  const [activeCitationKey, setActiveCitationKey] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const promptInputRef = useRef<HTMLTextAreaElement | null>(null);
  const fileDropInputRef = useRef<HTMLInputElement | null>(null);
  const streamStartRef = useRef<number>(0);

  const loadGatewayModels = useCallback(async () => {
    const res = await apiJson<{ data?: Array<{ id: string }> }>("/v1/models");
    if (res.ok && Array.isArray(res.data?.data)) {
      const concrete = res.data.data
        .map((m) => m.id)
        .filter((id) => !["auto", "local-fast", "local-balanced", "local-heavy"].includes(id));
      setGatewayModels(concrete);
    }
  }, []);

  useEffect(() => {
    void loadGatewayModels();
    void (async () => {
      const res = await apiJson<{ ok?: boolean; documents?: EphemeralDocMeta[] }>("/api/chat/ephemeral-docs");
      if (res.ok && Array.isArray(res.data?.documents)) {
        setEphemeralDocs(res.data.documents);
      }
    })();
  }, [loadGatewayModels]);

  const readFileAsBase64 = (file: File): Promise<string> =>
    new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const res = String(reader.result || "");
        const comma = res.indexOf(",");
        resolve(comma >= 0 ? res.slice(comma + 1) : res);
      };
      reader.onerror = () => reject(reader.error || new Error("File read failed"));
      reader.readAsDataURL(file);
    });

  const handleAttachEphemeralFiles = async (fileList: FileList | File[]) => {
    const arr = Array.from(fileList || []);
    if (arr.length === 0) return;
    setEphemeralUploading(true);
    try {
      const payloadFiles: Array<{ filename: string; content?: string; content_base64?: string }> = [];
      for (const f of arr) {
        const lower = f.name.toLowerCase();
        if (lower.endsWith(".pdf") || lower.endsWith(".docx")) {
          payloadFiles.push({ filename: f.name, content_base64: await readFileAsBase64(f) });
        } else {
          payloadFiles.push({ filename: f.name, content: await f.text() });
        }
      }
      const res = await apiJson<{ ok?: boolean; documents?: EphemeralDocMeta[] }>("/api/chat/ephemeral-docs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ files: payloadFiles }),
      });
      if (res.ok && Array.isArray(res.data?.documents)) {
        setEphemeralDocs(res.data.documents);
      }
    } finally {
      setEphemeralUploading(false);
    }
  };

  const handleClearEphemeralDocs = async () => {
    const res = await apiJson<{ ok?: boolean }>("/api/chat/ephemeral-docs", { method: "DELETE" });
    if (res.ok) {
      setEphemeralDocs([]);
    }
  };

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, tierNotice, streamStage]);

  useEffect(() => {
    if (!streaming) return;
    const timer = window.setInterval(() => {
      setStreamElapsedMs(Math.max(0, performance.now() - streamStartRef.current));
    }, 100);
    return () => window.clearInterval(timer);
  }, [streaming]);

  const sendPrompt = async (customMessages?: ChatMessage[]) => {
    const baseHistory = customMessages ?? messages;
    const text = promptText.trim();
    if (!customMessages && !text) return;
    if (streaming) return;

    const nextHistory: ChatMessage[] = customMessages
      ? baseHistory
      : [
          ...baseHistory,
          {
            id: `u-${Date.now()}`,
            role: "user",
            content: text,
            timestamp: new Date().toLocaleTimeString(),
          },
        ];

    const assistantId = `a-${Date.now()}`;
    const placeholder: ChatMessage = {
      id: assistantId,
      role: "assistant",
      content: "",
      thinking: "",
      model: selectedModel,
      timestamp: new Date().toLocaleTimeString(),
    };

    if (!customMessages) {
      setPromptText("");
    }
    setMessages([...nextHistory, placeholder]);
    streamStartRef.current = performance.now();
    setStreamElapsedMs(0);
    setStreamStage("connecting");
    setStreamingMsgId(assistantId);
    setStreaming(true);
    setTierNotice(null);

    let attachedCitations: RagHit[] = [];
    const apiMessages: Array<{ role: string; content: string }> = [];
    if (systemPrompt.trim()) {
      apiMessages.push({ role: "system", content: systemPrompt.trim() });
    }
    if (!customMessages && ephemeralDocs.length > 0 && text) {
      try {
        const ragRes = await apiJson<{ ok?: boolean; results?: RagHit[]; context_block?: string }>(
          "/api/chat/ephemeral-docs/query",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query: text, top_k: 4 }),
          }
        );
        if (ragRes.ok && ragRes.data?.context_block) {
          attachedCitations = ragRes.data.results || [];
          apiMessages.push({
            role: "system",
            content:
              "Contesto documentale locale (Ghost RAG solo RAM). Usa questi estratti e cita [NomeFile, p.X] o [NomeFile, L.X-Y]:\n\n" +
              ragRes.data.context_block,
          });
        }
      } catch {
        // Proceed even if ephemeral query fails
      }
    }
    for (const m of nextHistory) {
      apiMessages.push({ role: m.role, content: m.content });
    }
    if (attachedCitations.length > 0) {
      setMessages((prev) =>
        prev.map((m) => (m.id === assistantId ? { ...m, citations: attachedCitations } : m))
      );
    }

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const t0 = streamStartRef.current;
    let rawContent = "";
    let rawThinking = "";
    let resolvedModel = selectedModel;
    let totalTokens = 0;

    try {
      const payload: Record<string, unknown> = {
        model: selectedModel,
        messages: apiMessages,
        temperature,
        max_tokens: maxTokens > 0 ? maxTokens : undefined,
      };
      if (jsonMode) {
        payload.response_format = { type: "json_object" };
      }

      const res = await streamGatewayChat(
        payload,
        {
          "X-Allow-Tier-Switch": allowTierSwitch ? "true" : "false",
          "X-App-Id": appId.trim() || "local-llm-ui",
          "X-Workflow-Id": workflowId.trim() || "interactive-chat",
        },
        {
          onNotice: (notice) => {
            setTierNotice(`Cambio Motore: ${notice}`);
          },
          onStage: (stage) => {
            if (
              stage === "checking-backend" ||
              stage === "loading-model" ||
              stage === "prefilling" ||
              stage === "streaming"
            ) {
              setStreamStage((prev) => (prev === "thinking" ? prev : stage));
            }
          },
          onChunk: (chunk) => {
            setTierNotice(null);
            if (typeof chunk.model === "string") {
              resolvedModel = chunk.model;
            }
            const choices = chunk.choices as Array<{
              delta?: { content?: string; reasoning_content?: string; thinking?: string };
            }> | undefined;
            const delta = choices?.[0]?.delta;
            if (delta?.reasoning_content) rawThinking += delta.reasoning_content;
            if (delta?.thinking) rawThinking += delta.thinking;
            if (delta?.content) rawContent += delta.content;

            const usage = chunk.usage as { total_tokens?: number; completion_tokens?: number } | undefined;
            if (usage?.total_tokens) totalTokens = usage.total_tokens;

            const extracted = extractThinkSegments(rawContent);
            const combinedThinking = [rawThinking, extracted.thinking].filter(Boolean).join("\n\n");
            if (extracted.visible) {
              setStreamStage("streaming");
            } else if (combinedThinking) {
              setStreamStage("thinking");
            }
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId
                  ? {
                      ...m,
                      content: extracted.visible,
                      thinking: combinedThinking,
                      resolvedModel,
                    }
                  : m
              )
            );
          },
          onError: (errMsg) => {
            rawContent += `\n\n**[Errore Gateway]** ${errMsg}`;
            setMessages((prev) =>
              prev.map((m) => (m.id === assistantId ? { ...m, content: rawContent } : m))
            );
          },
        },
        ctrl.signal
      );

      const hdrResolved = res.headers.get("x-resolved-model");
      const hdrEscalated = res.headers.get("x-cascade-escalated") === "true";
      if (hdrResolved) resolvedModel = hdrResolved;
      const elapsedSec = Math.max(0.01, (performance.now() - t0) / 1000);
      const statsStr = `${resolvedModel} · ${elapsedSec.toFixed(2)}s${
        totalTokens > 0 ? ` · ${totalTokens} tok` : ""
      }${hdrEscalated ? " · escalated" : ""}`;

      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId
            ? {
                ...m,
                resolvedModel,
                escalated: hdrEscalated,
                stats: statsStr,
              }
            : m
        )
      );
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? { ...m, content: m.content || `Errore stream: ${(err as Error).message}` }
              : m
          )
        );
      }
    } finally {
      abortRef.current = null;
      setStreaming(false);
      setStreamingMsgId(null);
      setTierNotice(null);
    }
  };

  const handleStop = () => {
    abortRef.current?.abort();
  };

  const handleRegenerate = () => {
    if (streaming || messages.length === 0) return;
    const lastUserIdx = [...messages].reverse().findIndex((m) => m.role === "user");
    if (lastUserIdx === -1) return;
    const cutIdx = messages.length - 1 - lastUserIdx;
    void sendPrompt(messages.slice(0, cutIdx + 1));
  };

  const triggerDownload = (filename: string, content: string, mime: string) => {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const exportMarkdown = () => {
    const md = messages
      .map((m) => `### ${m.role.toUpperCase()} (${m.timestamp})\n\n${m.content}`)
      .join("\n\n---\n\n");
    triggerDownload(`local-llm-chat-${Date.now()}.md`, md, "text/markdown;charset=utf-8");
  };

  const exportJson = () => {
    triggerDownload(
      `local-llm-chat-${Date.now()}.json`,
      JSON.stringify({ retention_mode: "ephemeral", messages }, null, 2),
      "application/json;charset=utf-8"
    );
  };

  const ollamaOnline = Boolean(telemetry?.backends?.ollama?.online);
  const heavyOnline = Boolean(telemetry?.backends?.heavy?.online);
  const activeSummary = useMemo(() => {
    if (selectedModel === "local-heavy") return "⚡ Heavy MoE Colibrì";
    if (selectedModel === "auto") return "🟢 Motore Locale Attivo (Cascade)";
    return `Motore Locale (${selectedModel})`;
  }, [selectedModel]);

  return (
    <div className="flex h-full overflow-hidden">
      <aside className="w-80 shrink-0 border-r border-border bg-surface p-3.5 overflow-y-auto space-y-3.5 hidden md:block">
        <div className="space-y-1.5">
          <div className="flex items-center justify-between">
            <label className="text-xs font-semibold uppercase tracking-wider text-fg-muted">
              Modello / Alias Gateway
            </label>
            <Button variant="ghost" size="sm" onClick={loadGatewayModels} title="Aggiorna modelli /v1/models">
              <RefreshCw className="h-3.5 w-3.5" />
            </Button>
          </div>
          <div className="space-y-1.5">
            {VIRTUAL_ALIASES.map((alias) => (
              <button
                key={alias.id}
                type="button"
                onClick={() => setSelectedModel(alias.id)}
                className={`w-full text-left px-2.5 py-2 rounded-md border transition-colors ${
                  selectedModel === alias.id
                    ? "border-primary bg-info-bg text-fg"
                    : "border-border bg-surface-muted text-fg-muted hover:text-fg"
                }`}
              >
                <div className="flex items-center justify-between text-xs font-semibold font-mono">
                  <span>{alias.label}</span>
                  {alias.id === "auto" && <Zap className="h-3.5 w-3.5 text-ai" />}
                </div>
                <div className="text-[11px] text-fg-subtle">{alias.desc}</div>
              </button>
            ))}
          </div>
          {gatewayModels.length > 0 && (
            <select
              aria-label="Modelli concreti rilevati"
              value={selectedModel}
              onChange={(e) => setSelectedModel(e.target.value)}
              className="w-full px-2.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
            >
              <option value="auto">Alias: auto (Cascade)</option>
              <option value="local-fast">Alias: local-fast</option>
              <option value="local-balanced">Alias: local-balanced</option>
              <option value="local-heavy">Alias: local-heavy</option>
              {gatewayModels.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          )}
        </div>

        <label className="flex items-center justify-between gap-2 text-xs text-fg-muted cursor-pointer">
          <span>Consenti Tier-Switch (Fast ↔ Heavy)</span>
          <input
            type="checkbox"
            checked={allowTierSwitch}
            onChange={(e) => setAllowTierSwitch(e.target.checked)}
          />
        </label>

        <label className="flex items-center justify-between gap-2 text-xs text-fg-muted cursor-pointer">
          <span>Response Format: JSON Object</span>
          <input type="checkbox" checked={jsonMode} onChange={(e) => setJsonMode(e.target.checked)} />
        </label>

        <div className="grid grid-cols-2 gap-2">
          <div>
            <label htmlFor="chatTemp" className="block text-xs text-fg-muted mb-1">
              Temperature
            </label>
            <input
              id="chatTemp"
              type="number"
              step="0.05"
              min="0"
              max="2"
              value={temperature}
              onChange={(e) => setTemperature(Number(e.target.value))}
              className="w-full px-2 py-1 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
            />
          </div>
          <div>
            <label htmlFor="chatMaxTok" className="block text-xs text-fg-muted mb-1">
              Max Tokens
            </label>
            <input
              id="chatMaxTok"
              type="number"
              step="256"
              min="0"
              max="65536"
              value={maxTokens}
              onChange={(e) => setMaxTokens(Number(e.target.value))}
              className="w-full px-2 py-1 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
            />
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div>
            <label htmlFor="chatAppId" className="block text-xs text-fg-muted mb-1">
              X-App-Id
            </label>
            <input
              id="chatAppId"
              value={appId}
              onChange={(e) => setAppId(e.target.value)}
              className="w-full px-2 py-1 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
            />
          </div>
          <div>
            <label htmlFor="chatWorkflowId" className="block text-xs text-fg-muted mb-1">
              X-Workflow-Id
            </label>
            <input
              id="chatWorkflowId"
              value={workflowId}
              onChange={(e) => setWorkflowId(e.target.value)}
              className="w-full px-2 py-1 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
            />
          </div>
        </div>

        <div className="space-y-1">
          <label htmlFor="chatPreset" className="block text-xs text-fg-muted">
            Preset System Prompt
          </label>
          <select
            id="chatPreset"
            value={selectedPresetId}
            onChange={(e) => onSelectPreset(e.target.value)}
            className="w-full px-2.5 py-1.5 text-xs rounded-sm border border-border bg-bg text-fg"
          >
            <option value="">-- Personalizzato --</option>
            {presets.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <textarea
            rows={4}
            value={systemPrompt}
            onChange={(e) => onSystemPromptChange(e.target.value)}
            placeholder="Istruzioni di sistema (solo in RAM)..."
            className="w-full px-2.5 py-1.5 text-xs rounded-sm border border-border bg-bg text-fg"
          />
        </div>

        <div className="grid grid-cols-2 gap-2">
          <Button size="sm" variant="secondary" onClick={exportMarkdown} disabled={messages.length === 0}>
            <Download className="h-3.5 w-3.5" /> MD
          </Button>
          <Button size="sm" variant="secondary" onClick={exportJson} disabled={messages.length === 0}>
            <Download className="h-3.5 w-3.5" /> JSON
          </Button>
        </div>

        <Button
          size="sm"
          variant="danger"
          onClick={() => setMessages([])}
          disabled={streaming || messages.length === 0}
          className="w-full"
        >
          <Trash2 className="h-3.5 w-3.5" /> Pulisci Chat in RAM
        </Button>

        <div className="p-2.5 rounded-md border border-border bg-surface-muted text-[11px] text-fg-muted">
          <strong className="text-success">🔒 Modalità Ghost (Solo RAM)</strong>
          <div className="mt-0.5 font-mono">
            {systemConfig?.hardware ? "Motore Locale Attivo · Isolamento Loopback" : "Zero-Disk Ephemeral Runtime"}
          </div>
          <div>Nessuna cronologia salvata su disco; token auth solo in RAM.</div>
        </div>
      </aside>

      <main className="flex-1 flex flex-col min-w-0 bg-bg">
        <div className="flex items-center justify-between gap-2 px-4 py-2 border-b border-border bg-surface text-xs">
          <div className="flex items-center gap-2 font-mono text-fg-muted">
            <span>{activeSummary}</span>
            <StatusPill status={ollamaOnline ? "online" : "offline"} label="Fast" />
            <StatusPill status={heavyOnline ? "online" : "idle"} label="Heavy" />
          </div>
          <div className="flex items-center gap-2">
            <span className="font-mono text-fg-subtle">{messages.length} msg</span>
            <Button
              size="sm"
              variant={ideOpen ? "primary" : "secondary"}
              onClick={() => {
                setIdeOpen((prev) => !prev);
                setRagOpen(false);
              }}
            >
              <Plug className="h-3.5 w-3.5" /> Impostazioni Sviluppatore &amp; IDE
            </Button>
            <Button
              size="sm"
              variant={ragOpen ? "primary" : "secondary"}
              onClick={() => {
                setRagOpen((prev) => !prev);
                setIdeOpen(false);
              }}
            >
              <BookOpen className="h-3.5 w-3.5" /> RAG Drawer
            </Button>
          </div>
        </div>

        {tierNotice && (
          <div className="px-4 py-2 bg-warning-bg text-warning text-xs font-mono border-b border-border">
            {tierNotice}
          </div>
        )}

        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {messages.length === 0 ? (
            <div className="max-w-3xl mx-auto mt-6 space-y-4">
              <div className="p-6 rounded-lg border border-border bg-surface space-y-4">
                <div className="flex items-start justify-between gap-3 flex-wrap">
                  <div>
                    <h2 className="text-lg font-semibold">Assistente AI Locale — Privato &amp; Offline</h2>
                    <p className="text-sm text-fg-muted mt-1">
                      Tutto gira sul tuo PC in sola RAM: zero cloud, zero telemetria e pieno controllo sui tuoi file.
                    </p>
                  </div>
                  <Badge variant="success">100% Air-Gapped</Badge>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
                  <div className="p-3 rounded-md border border-border bg-surface-muted space-y-1">
                    <div className="font-semibold flex items-center gap-1.5 text-fg">
                      <Lock className="h-3.5 w-3.5 text-success" /> Privacy Assoluta &amp; Uncensored
                    </div>
                    <p className="text-fg-muted">
                      Nessuna cronologia salvata su disco. Usa modelli locali senza censure su documenti riservati o domande sensibili.
                    </p>
                  </div>
                  <div className="p-3 rounded-md border border-border bg-surface-muted space-y-1">
                    <div className="font-semibold flex items-center gap-1.5 text-fg">
                      <Cpu className="h-3.5 w-3.5 text-primary" /> Calcolo Locale a Costo Zero
                    </div>
                    <p className="text-fg-muted">
                      Sfrutta la tua GPU e RAM per traduzioni lunghe, riassunti e analisi PDF privati tramite il <strong>RAG Drawer</strong>.
                    </p>
                  </div>
                </div>

                <div className="space-y-2">
                  <div className="text-xs font-semibold uppercase tracking-wider text-fg-subtle flex items-center gap-1.5">
                    <Sparkles className="h-3.5 w-3.5 text-ai" /> Prova subito un esempio
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-2.5">
                    {STARTER_PROMPTS.map((chip) => (
                      <button
                        key={chip.id}
                        type="button"
                        onClick={() => {
                          setPromptText(chip.prompt);
                          promptInputRef.current?.focus();
                        }}
                        className="text-left p-3 rounded-md border border-border bg-surface-muted hover:border-primary hover:bg-info-bg/40 transition-colors"
                      >
                        <div className="text-xs font-semibold text-fg">{chip.title}</div>
                        <div className="text-[11px] text-fg-muted mt-1">{chip.subtitle}</div>
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          ) : (
            messages.map((msg) => {
              const isStreamingThisMsg = streaming && msg.id === streamingMsgId;
              const autoExpandThinking = isStreamingThisMsg && Boolean(msg.thinking) && !msg.content;
              const isThinkingOpen =
                msg.id in userToggledThinking ? userToggledThinking[msg.id] : autoExpandThinking;

              return (
                <div
                  key={msg.id}
                  className={`max-w-4xl mx-auto rounded-lg border p-3.5 ${
                    msg.role === "user"
                      ? "border-primary/40 bg-info-bg/40 ml-12"
                      : "border-border bg-surface mr-6"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2 mb-2 text-xs text-fg-subtle">
                    <div className="flex items-center gap-2">
                      <Badge variant={msg.role === "user" ? "info" : "ai"}>
                        {msg.role === "user" ? "Utente" : msg.resolvedModel || msg.model || "Assistant"}
                      </Badge>
                      {msg.escalated && <Badge variant="warning">Cascade Escalated</Badge>}
                      {msg.stats && <span className="font-mono">{msg.stats}</span>}
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="font-mono">{msg.timestamp}</span>
                      {msg.content && (
                        <button
                          type="button"
                          onClick={() => void navigator.clipboard?.writeText(msg.content)}
                          className="text-fg-muted hover:text-fg"
                          title="Copia risposta"
                        >
                          <Copy className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </div>
                  </div>

                  {isStreamingThisMsg && (
                    <div className="mb-2.5 flex items-center justify-between gap-2 px-2.5 py-1.5 rounded-sm border border-primary/30 bg-info-bg/30 text-xs font-mono">
                      <div className="flex items-center gap-2 text-fg">
                        <span className="h-2 w-2 rounded-full bg-primary animate-pulse" aria-hidden="true" />
                        <span>{formatStageLabel(streamStage, msg.thinking?.length ?? 0)}</span>
                      </div>
                      <span className="text-fg-muted">{(streamElapsedMs / 1000).toFixed(1)}s</span>
                    </div>
                  )}

                  {msg.thinking && (
                    <details
                      open={isThinkingOpen}
                      className="mb-2 rounded-sm border border-border bg-surface-muted p-2 text-xs"
                    >
                      <summary
                        onClick={(e) => {
                          e.preventDefault();
                          setUserToggledThinking((prev) => ({
                            ...prev,
                            [msg.id]: !isThinkingOpen,
                          }));
                        }}
                        className="cursor-pointer font-mono text-fg-muted select-none"
                      >
                        Ragionamento interno ({msg.thinking.length} car.)
                      </summary>
                      <pre className="mt-2 whitespace-pre-wrap font-mono text-[11px] text-fg-muted">
                        {msg.thinking}
                      </pre>
                    </details>
                  )}

                  {msg.role === "user" ? (
                    <div className="whitespace-pre-wrap text-sm">{msg.content}</div>
                  ) : (
                    <>
                      <div
                        className="llm-prose"
                        // devsecops:ignore-xss renderSafeMarkdownHtml escapes all HTML entities first
                        dangerouslySetInnerHTML={{
                          __html: renderSafeMarkdownHtml(msg.content || ""),
                        }}
                      />
                      {msg.citations && msg.citations.length > 0 && (
                        <div className="mt-3 pt-2 border-t border-border/60 space-y-1.5">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="text-[11px] font-mono text-fg-subtle">Fonti RAM:</span>
                            {msg.citations.map((cit, idx) => {
                              const cKey = `${msg.id}-${cit.chunk_id || idx}`;
                              const isOpen = activeCitationKey === cKey;
                              return (
                                <button
                                  key={cKey}
                                  type="button"
                                  onClick={() => setActiveCitationKey(isOpen ? null : cKey)}
                                  className={`px-2 py-0.5 rounded-pill text-[11px] font-mono border transition-colors ${
                                    isOpen
                                      ? "border-primary bg-info-bg text-fg"
                                      : "border-border bg-surface-muted text-fg-muted hover:text-fg"
                                  }`}
                                >
                                  {cit.citation}
                                </button>
                              );
                            })}
                          </div>
                          {msg.citations.map((cit, idx) => {
                            const cKey = `${msg.id}-${cit.chunk_id || idx}`;
                            if (activeCitationKey !== cKey) return null;
                            return (
                              <div
                                key={`${cKey}-pop`}
                                className="p-2.5 rounded-md border border-primary/40 bg-surface-muted text-xs space-y-1"
                              >
                                <div className="flex items-center justify-between font-mono text-[11px] text-primary">
                                  <span>{cit.citation}</span>
                                  <button
                                    type="button"
                                    onClick={() => setActiveCitationKey(null)}
                                    className="text-fg-muted hover:text-fg"
                                    aria-label="Chiudi estratto citazione"
                                  >
                                    <X className="h-3 w-3" />
                                  </button>
                                </div>
                                <pre className="whitespace-pre-wrap font-mono text-[11px] text-fg-muted max-h-40 overflow-y-auto">
                                  {cit.text}
                                </pre>
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </>
                  )}
                </div>
              );
            })
          )}
          <div ref={messagesEndRef} />
        </div>

        <footer
          className={`border-t border-border bg-surface p-3 transition-colors ${
            dragActive ? "ring-2 ring-primary bg-info-bg/20" : ""
          }`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragActive(true);
          }}
          onDragLeave={() => setDragActive(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragActive(false);
            if (e.dataTransfer?.files?.length) {
              void handleAttachEphemeralFiles(e.dataTransfer.files);
            }
          }}
        >
          {ephemeralDocs.length > 0 && (
            <div className="max-w-4xl mx-auto mb-2 flex flex-wrap items-center gap-1.5 text-xs">
              {ephemeralDocs.map((doc) => (
                <span
                  key={doc.doc_id}
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded-pill border border-success/40 bg-success-bg/40 text-fg font-mono text-[11px]"
                >
                  <FileText className="h-3 w-3 text-success" />
                  🔒 Solo RAM · {doc.source_file}
                </span>
              ))}
              <button
                type="button"
                onClick={() => void handleClearEphemeralDocs()}
                className="text-[11px] font-mono text-danger hover:underline ml-1"
              >
                Rimuovi documenti RAM
              </button>
            </div>
          )}
          <input
            ref={fileDropInputRef}
            type="file"
            multiple
            accept=".pdf,.md,.txt,.py,.ts,.tsx,.json,.csv,.docx"
            className="hidden"
            onChange={(e) => {
              if (e.target.files?.length) {
                void handleAttachEphemeralFiles(e.target.files);
                e.target.value = "";
              }
            }}
          />
          <div className="max-w-4xl mx-auto flex gap-2 items-end">
            <Button
              type="button"
              size="sm"
              variant="secondary"
              loading={ephemeralUploading}
              onClick={() => fileDropInputRef.current?.click()}
              title="Allega documenti in RAM (Ghost Mode) o trascina qui PDF/MD/TXT/Codice"
            >
              <Paperclip className="h-4 w-4" /> Allega Documenti Locali
            </Button>
            <textarea
              ref={promptInputRef}
              rows={2}
              value={promptText}
              onChange={(e) => setPromptText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void sendPrompt();
                }
              }}
              placeholder="Scrivi prompt o trascina qui PDF/MD/TXT/Codice (Ghost RAG solo RAM)..."
              className="flex-1 px-3 py-2 text-sm rounded-md border border-border bg-bg text-fg resize-y"
            />
            {streaming ? (
              <Button variant="danger" onClick={handleStop}>
                <Square className="h-4 w-4" /> Stop
              </Button>
            ) : (
              <Button variant="primary" onClick={() => void sendPrompt()} disabled={!promptText.trim()}>
                <Send className="h-4 w-4" /> Invia
              </Button>
            )}
            <Button variant="secondary" onClick={handleRegenerate} disabled={streaming || messages.length === 0}>
              Rigenera
            </Button>
          </div>
        </footer>
      </main>

      <RagDrawer
        open={ragOpen}
        onClose={() => setRagOpen(false)}
        onInjectContext={(ctx) => setPromptText((prev) => (prev ? `${ctx}\n\n${prev}` : ctx))}
      />
      <IdeConfigsDrawer open={ideOpen} onClose={() => setIdeOpen(false)} />
    </div>
  );
};
