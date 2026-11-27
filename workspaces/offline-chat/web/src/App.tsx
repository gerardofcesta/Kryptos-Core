import React, { useCallback, useEffect, useRef, useState } from "react";
import { Moon, Power, Sun } from "lucide-react";
import { Badge, Button, StatusPill } from "@wdk";
import { ChatPanel } from "./components/ChatPanel";
import { BatchRunnerPanel } from "./components/BatchRunnerPanel";
import { EnginesPanel } from "./components/EnginesPanel";
import { GoModePanel } from "./components/GoModePanel";
import { ModelsWizardPanel } from "./components/ModelsWizardPanel";
import { OrchestratorPanel } from "./components/OrchestratorPanel";
import { PromptsPanel } from "./components/PromptsPanel";
import { apiFetch, apiJson, fetchSessionToken, startAuthenticatedSseLoop } from "./lib/api";
import type {
  AppTab,
  OperatorMode,
  OrchestratorStatusPayload,
  PromptPreset,
  SystemConfig,
  TelemetryData,
} from "./types";

const TABS: Array<{ id: AppTab; label: string }> = [
  { id: "chat", label: "Chat & RAG" },
  { id: "engines", label: "Motori & Ledger" },
  { id: "models", label: "ModelManager & Wizard" },
  { id: "prompts", label: "Libreria Prompt" },
  { id: "orchestrator", label: "Orchestrator" },
  { id: "gomode", label: "Go Mode" },
  { id: "batch", label: "Batch Overnight" },
];

export const App: React.FC = () => {
  const [activeTab, setActiveTab] = useState<AppTab>("chat");
  const [operatorMode, setOperatorMode] = useState<OperatorMode>("ghost");
  const [darkTheme, setDarkTheme] = useState<boolean>(true);
  const [telemetry, setTelemetry] = useState<TelemetryData | null>(null);
  const [systemConfig, setSystemConfig] = useState<SystemConfig | null>(null);
  const [presets, setPresets] = useState<PromptPreset[]>([]);
  const [selectedPresetId, setSelectedPresetId] = useState<string>("");
  const [systemPrompt, setSystemPrompt] = useState<string>("");
  const [orchStatus, setOrchStatus] = useState<OrchestratorStatusPayload | null>(null);
  const [shutdownDone, setShutdownDone] = useState<boolean>(false);

  const activeTabRef = useRef<AppTab>(activeTab);
  const orchActiveRef = useRef<boolean>(false);
  const selectedPresetIdRef = useRef<string>(selectedPresetId);

  useEffect(() => {
    activeTabRef.current = activeTab;
  }, [activeTab]);

  useEffect(() => {
    orchActiveRef.current = Boolean(orchStatus?.active);
  }, [orchStatus?.active]);

  useEffect(() => {
    selectedPresetIdRef.current = selectedPresetId;
  }, [selectedPresetId]);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", darkTheme ? "dark" : "light");
  }, [darkTheme]);

  const fetchTelemetry = useCallback(async () => {
    const res = await apiJson<TelemetryData>("/api/system/telemetry");
    if (res.ok) setTelemetry(res.data);
  }, []);

  const fetchOrchestratorStatus = useCallback(async () => {
    const res = await apiJson<OrchestratorStatusPayload>("/api/orchestrator/status");
    if (res.ok) setOrchStatus(res.data);
  }, []);

  const loadPresets = useCallback(async () => {
    const res = await apiJson<{ prompts?: PromptPreset[] }>("/api/prompts");
    if (res.ok && Array.isArray(res.data?.prompts)) {
      const loaded = res.data.prompts;
      setPresets(loaded);
      if (!selectedPresetIdRef.current && loaded[0]) {
        selectedPresetIdRef.current = loaded[0].id;
        setSelectedPresetId(loaded[0].id);
        setSystemPrompt(loaded[0].content || "");
      }
    }
  }, []);

  useEffect(() => {
    let stopTel: (() => void) | undefined;
    let stopOrch: (() => void) | undefined;
    let cancelled = false;

    const boot = async () => {
      await fetchSessionToken();
      if (cancelled) return;
      const [cfgRes] = await Promise.all([
        apiJson<SystemConfig>("/api/system/config"),
        fetchTelemetry(),
        loadPresets(),
        fetchOrchestratorStatus(),
      ]);
      if (cancelled) return;
      if (cfgRes.ok) setSystemConfig(cfgRes.data);

      stopTel = startAuthenticatedSseLoop("/api/system/telemetry/stream", (payload) => {
        setTelemetry(payload as TelemetryData);
      });
      stopOrch = startAuthenticatedSseLoop(
        "/api/orchestrator/stream",
        (payload) => {
          setOrchStatus(payload as OrchestratorStatusPayload);
        },
        () => activeTabRef.current === "orchestrator" || orchActiveRef.current
      );
    };

    void boot();
    return () => {
      cancelled = true;
      stopTel?.();
      stopOrch?.();
    };
  }, [fetchOrchestratorStatus, fetchTelemetry, loadPresets]);

  const handleSelectPreset = (id: string) => {
    setSelectedPresetId(id);
    const found = presets.find((p) => p.id === id);
    if (found) {
      setSystemPrompt(found.content || "");
    }
  };

  const handleShutdown = async () => {
    if (!window.confirm("Spegnere Orchestrator (wipe RAM), tutti i motori LLM e chiudere la sessione?")) {
      return;
    }
    try {
      await apiFetch("/api/control/engine", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "shutdown_app" }),
      });
    } catch {
      // Ignore connection close
    }
    setShutdownDone(true);
    setTimeout(() => {
      try {
        window.close();
      } catch {
        // Ignore
      }
    }, 400);
  };

  if (shutdownDone) {
    return (
      <div className="h-screen w-screen flex items-center justify-center bg-bg text-fg p-6 text-center">
        <div className="space-y-2">
          <h1 className="text-xl font-semibold">Kryptos Core arrestato in sicurezza</h1>
          <p className="text-sm text-fg-muted">
            Orchestrator e Ghost RAM azzerati, motori spenti e VRAM liberata.
          </p>
        </div>
      </div>
    );
  }

  const olOnline = Boolean(telemetry?.backends?.ollama?.online);
  const hvOnline = Boolean(telemetry?.backends?.heavy?.online ?? telemetry?.backends?.heavyMoe?.online);
  const gpuUsedGb = telemetry?.gpu?.vramUsedGb ?? telemetry?.gpu?.memoryUsedGb;
  const gpuTotalGb = telemetry?.gpu?.vramTotalGb ?? telemetry?.gpu?.memoryTotalGb;
  const vramLabel =
    telemetry?.gpu?.available && gpuUsedGb != null && gpuTotalGb != null
      ? `VRAM ${gpuUsedGb}/${gpuTotalGb}G`
      : "VRAM --";
  const ramLabel =
    telemetry?.ram?.freeGb != null ? `RAM ${telemetry.ram.freeGb}G free` : "RAM --";

  return (
    <div className="h-screen w-screen flex flex-col bg-bg text-fg overflow-hidden">
      <header className="shrink-0 flex items-center justify-between gap-3 px-4 py-2.5 border-b border-border bg-surface flex-wrap">
        <div className="flex items-center gap-2.5">
          <span className="h-2.5 w-2.5 rounded-full bg-success" aria-hidden="true" />
          <div>
            <div className="text-sm font-semibold leading-tight">Kryptos Core</div>
            <div className="text-[11px] font-mono text-fg-subtle">
              🔒 Modalità Ghost (Solo RAM) · Zero CDN · Zero Disk Log
            </div>
          </div>
        </div>

        <nav className="flex items-center gap-1 flex-wrap" role="tablist">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={activeTab === t.id}
              onClick={() => setActiveTab(t.id)}
              className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
                activeTab === t.id
                  ? "bg-primary text-primary-fg"
                  : "text-fg-muted hover:bg-surface-muted hover:text-fg"
              }`}
            >
              {t.label}
            </button>
          ))}
        </nav>

        <div className="flex items-center gap-1.5 flex-wrap">
          <button
            type="button"
            onClick={() => {
              setOperatorMode("ghost");
              setActiveTab("chat");
            }}
            className={`px-2 py-1 rounded-pill text-[11px] font-mono border ${
              operatorMode === "ghost"
                ? "border-success bg-success-bg text-success"
                : "border-border text-fg-muted"
            }`}
          >
            [GHOST · RAM]
          </button>
          <button
            type="button"
            onClick={() => {
              setOperatorMode("orchestrator");
              setActiveTab("orchestrator");
            }}
            className={`px-2 py-1 rounded-pill text-[11px] font-mono border ${
              operatorMode === "orchestrator"
                ? "border-primary bg-info-bg text-primary"
                : "border-border text-fg-muted"
            }`}
          >
            [ORCH · {String(orchStatus?.state || "IDLE").toUpperCase()}]
          </button>
        </div>

        <div className="flex items-center gap-1.5 flex-wrap">
          <StatusPill status={olOnline ? "online" : "offline"} label={olOnline ? "🟢 Motore Fast Attivo" : "Motore Fast Offline"} />
          <StatusPill status={hvOnline ? "online" : "idle"} label={hvOnline ? "⚡ Heavy Ready" : "Motore Heavy Standby"} />
          <Badge variant="neutral">{vramLabel}</Badge>
          <Badge variant="neutral">{ramLabel}</Badge>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setDarkTheme((d) => !d)}
            aria-label="Cambia tema"
          >
            {darkTheme ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
          </Button>
          <Button size="sm" variant="danger" onClick={() => void handleShutdown()}>
            <Power className="h-3.5 w-3.5" /> Spegni
          </Button>
        </div>
      </header>

      <div className="flex-1 min-h-0 overflow-hidden">
        {activeTab === "chat" && (
          <ChatPanel
            telemetry={telemetry}
            systemConfig={systemConfig}
            presets={presets}
            selectedPresetId={selectedPresetId}
            onSelectPreset={handleSelectPreset}
            systemPrompt={systemPrompt}
            onSystemPromptChange={setSystemPrompt}
          />
        )}
        {activeTab === "engines" && (
          <EnginesPanel telemetry={telemetry} onRefreshTelemetry={() => void fetchTelemetry()} />
        )}
        {activeTab === "models" && <ModelsWizardPanel />}
        {activeTab === "prompts" && (
          <PromptsPanel
            presets={presets}
            onReloadPresets={loadPresets}
            onUseInChat={(preset) => {
              setSelectedPresetId(preset.id);
              setSystemPrompt(preset.content);
              setActiveTab("chat");
            }}
          />
        )}
        {activeTab === "orchestrator" && (
          <OrchestratorPanel
            liveStatus={orchStatus}
            onRefreshStatus={() => void fetchOrchestratorStatus()}
          />
        )}
        {activeTab === "gomode" && <GoModePanel />}
        {activeTab === "batch" && <BatchRunnerPanel />}
      </div>
    </div>
  );
};

export default App;
