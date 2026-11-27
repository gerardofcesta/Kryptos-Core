import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  Cpu,
  DollarSign,
  HardDrive,
  MemoryStick,
  RefreshCw,
  RotateCw,
  ShieldAlert,
  Sparkles,
  X,
} from "lucide-react";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, StatusPill } from "@wdk";
import { IdeConfigsCard } from "./IdeConfigsCard";
import { apiJson } from "../lib/api";
import type {
  LedgerSnapshot,
  TelemetryData,
  TuningAdvisorResponse,
  TuningBoundsResponse,
} from "../types";

interface EnginesPanelProps {
  telemetry: TelemetryData | null;
  onRefreshTelemetry: () => void;
}

const DEFAULT_CTX_STEPS = [2048, 4096, 8192, 16384, 24576, 32768, 49152, 65536, 131072];

type ZoneLevel = "green" | "amber" | "red";

function classifyZone(value: number, greenMax: number, amberMax: number): ZoneLevel {
  if (value <= greenMax) return "green";
  if (value <= amberMax) return "amber";
  return "red";
}

function zoneBadgeVariant(zone: ZoneLevel): "success" | "warning" | "danger" {
  if (zone === "green") return "success";
  if (zone === "amber") return "warning";
  return "danger";
}

function zoneProgressClass(zone: ZoneLevel): string {
  if (zone === "green") return "wdk-progress is-success";
  if (zone === "amber") return "wdk-progress is-warning";
  return "wdk-progress is-danger";
}

function zoneLabel(zone: ZoneLevel): string {
  if (zone === "green") return "Safe (Verde)";
  if (zone === "amber") return "Stretto (Ambra)";
  return "Bloccato OOM (Rosso)";
}

export const EnginesPanel: React.FC<EnginesPanelProps> = ({ telemetry, onRefreshTelemetry }) => {
  const [ramLimitGb, setRamLimitGb] = useState<number>(12);
  const [ctxSize, setCtxSize] = useState<number>(16384);
  const [kvQuant, setKvQuant] = useState<"q8_0" | "f16">("q8_0");
  const [bounds, setBounds] = useState<TuningBoundsResponse | null>(null);
  const [boundsLoading, setBoundsLoading] = useState<boolean>(false);
  const [actionBusy, setActionBusy] = useState<boolean>(false);
  const [consoleLog, setConsoleLog] = useState<string>("In attesa di comandi operativi...");
  const [ledger, setLedger] = useState<LedgerSnapshot | null>(null);

  // Layer 2 On-Demand Hardware Advisor drawer state
  const [advisorOpen, setAdvisorOpen] = useState<boolean>(false);
  const [advisorQuestion, setAdvisorQuestion] = useState<string>(
    "Fin dove posso spingere Context e Heavy MoE RAM Limit senza andare in OOM o swap?"
  );
  const [advisorBusy, setAdvisorBusy] = useState<boolean>(false);
  const [advisorError, setAdvisorError] = useState<string | null>(null);
  const [advisorResult, setAdvisorResult] = useState<TuningAdvisorResponse | null>(null);

  const loadLedger = useCallback(async () => {
    const res = await apiJson<LedgerSnapshot>("/api/ledger");
    if (res.ok) {
      setLedger(res.data);
    }
  }, []);

  const loadBounds = useCallback(async (quant: "q8_0" | "f16", applyDefaults = false) => {
    setBoundsLoading(true);
    try {
      const res = await apiJson<TuningBoundsResponse>(
        `/api/system/tuning-bounds?kv_quant=${encodeURIComponent(quant)}`
      );
      if (res.ok && res.data?.ok) {
        setBounds(res.data);
        if (applyDefaults) {
          setCtxSize(res.data.fast_ctx.active || res.data.fast_ctx.recommended || 16384);
          setRamLimitGb(res.data.heavy_ram_limit_gb.active || res.data.heavy_ram_limit_gb.recommended || 12);
        }
      }
    } finally {
      setBoundsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadLedger();
    void loadBounds("q8_0", true);
  }, [loadLedger, loadBounds]);

  const ctxSteps = useMemo(
    () => (bounds?.fast_ctx?.steps && bounds.fast_ctx.steps.length > 0 ? bounds.fast_ctx.steps : DEFAULT_CTX_STEPS),
    [bounds]
  );

  const ctxStepIdx = useMemo(() => {
    let bestIdx = 0;
    let bestDiff = Infinity;
    ctxSteps.forEach((step, idx) => {
      const diff = Math.abs(step - ctxSize);
      if (diff < bestDiff) {
        bestDiff = diff;
        bestIdx = idx;
      }
    });
    return bestIdx;
  }, [ctxSteps, ctxSize]);

  const fastCtxBounds = bounds?.fast_ctx ?? {
    min: 2048,
    recommended: 16384,
    max_safe: 32768,
    zones: { green_max: 16384, amber_max: 32768, hard_max: 131072 },
  };
  const heavyRamBounds = bounds?.heavy_ram_limit_gb ?? {
    min: 4,
    recommended: 12,
    max_safe: 18,
    zones: { green_max: 12, amber_max: 18, hard_max: 64 },
  };
  const heavyCtxBounds = bounds?.heavy_ctx ?? {
    min: 2048,
    recommended: 16384,
    max_safe: 32768,
    zones: { green_max: 16384, amber_max: 32768, hard_max: 131072 },
  };

  const fastCtxZone = classifyZone(ctxSize, fastCtxBounds.zones.green_max, fastCtxBounds.zones.amber_max);
  const heavyRamZone = classifyZone(ramLimitGb, heavyRamBounds.zones.green_max, heavyRamBounds.zones.amber_max);
  const heavyCtxZone = classifyZone(ctxSize, heavyCtxBounds.zones.green_max, heavyCtxBounds.zones.amber_max);

  const estKvGb = useMemo(() => {
    const per1k = bounds?.budget?.kv_gb_per_1k ?? 0.078;
    return Number(((ctxSize / 1024) * per1k).toFixed(2));
  }, [bounds, ctxSize]);

  const estVramPct = useMemo(() => {
    const usable = bounds?.budget?.usable_vram_gb || 0;
    const weights = bounds?.budget?.model_weights_gb || 0;
    if (usable <= 0) return 0;
    return Math.min(100, Math.round(((weights + estKvGb) / usable) * 100));
  }, [bounds, estKvGb]);

  const estRamPct = useMemo(() => {
    const total = bounds?.budget?.ram_total_gb || 0;
    const osRes = bounds?.budget?.os_ram_reserve_gb || 0;
    const vkPool = bounds?.budget?.vk_host_pool_gb || 1.5;
    const ceiling = Math.max(1, total - osRes);
    return Math.min(100, Math.round(((ramLimitGb + vkPool) / ceiling) * 100));
  }, [bounds, ramLimitGb]);

  const runEngineAction = async (action: string, restart = false) => {
    setActionBusy(true);
    setConsoleLog(`${restart ? "Riavvio" : "Esecuzione"} '${action}' (ramLimitGb=${ramLimitGb}, ctx=${ctxSize}, restart=${restart})...`);
    try {
      const res = await apiJson<{ ok?: boolean; error?: string; output?: string; message?: string }>(
        "/api/control/engine",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            action,
            ramLimitGb,
            ram_limit_gb: ramLimitGb,
            ctx: ctxSize,
            restart,
          }),
        }
      );
      if (res.status === 409) {
        setConsoleLog(`409 Conflict: ${res.data.error || "Run Orchestrator attiva"}`);
      } else if (res.ok && res.data.ok !== false) {
        setConsoleLog(res.data.output || res.data.message || `Azione '${action}' completata.`);
      } else {
        setConsoleLog(`Errore (${res.status}): ${res.data.error || "azione fallita"}`);
      }
      onRefreshTelemetry();
      void loadBounds(kvQuant, false);
    } finally {
      setActionBusy(false);
    }
  };

  const askHardwareAdvisor = async (overrideQuestion?: string) => {
    const q = (overrideQuestion ?? advisorQuestion).trim();
    if (!q) return;
    setAdvisorBusy(true);
    setAdvisorError(null);
    try {
      const res = await apiJson<TuningAdvisorResponse>("/api/system/tuning-advisor", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: q,
          kv_quant: kvQuant,
          use_llm: true,
        }),
      });
      if (!res.ok || !res.data?.ok) {
        setAdvisorError(`Errore Hardware Advisor (HTTP ${res.status})`);
        return;
      }
      setAdvisorResult(res.data);
    } catch (err) {
      setAdvisorError(`Errore rete advisor: ${(err as Error).message}`);
    } finally {
      setAdvisorBusy(false);
    }
  };

  const applyAdvisorRecommendations = () => {
    if (!advisorResult?.recommended) return;
    setCtxSize(advisorResult.recommended.fast_ctx);
    setRamLimitGb(advisorResult.recommended.heavy_ram_limit_gb);
    setConsoleLog(
      `[Advisor] Applicati valori Layer 1 clamped agli slider: num_ctx=${advisorResult.recommended.fast_ctx}, ramLimitGb=${advisorResult.recommended.heavy_ram_limit_gb} GB.`
    );
  };

  const gpu = telemetry?.gpu;
  const ram = telemetry?.ram;
  const disk = telemetry?.disk;
  const ol = telemetry?.backends?.ollama;
  const hv = telemetry?.backends?.heavy ?? telemetry?.backends?.heavyMoe;
  const lms = telemetry?.backends?.lmstudio;
  const gpuAvailable = Boolean(gpu?.available);
  const vramUsedGb = gpuAvailable ? (gpu?.vramUsedGb ?? gpu?.memoryUsedGb) : null;
  const vramFreeGb = gpuAvailable ? (gpu?.vramFreeGb ?? gpu?.memoryFreeGb) : null;
  const vramTotalGb = gpuAvailable ? (gpu?.vramTotalGb ?? gpu?.memoryTotalGb) : null;
  const vramUsedPct = gpuAvailable ? (gpu?.vramUsedPct ?? gpu?.memoryUsedPct ?? 0) : 0;
  const gpuTempC = gpu?.tempC ?? gpu?.temperatureC;
  const gpuPowerW = gpu?.powerW ?? gpu?.powerDrawW;
  const gpuUtilPct = gpu?.utilPct ?? gpu?.utilizationPct;
  const heavySafe = ram?.heavyMoeSafe ?? ram?.heavyReady ?? false;
  const ledgerEntries = ledger?.entries ?? ledger?.workflows ?? [];

  return (
    <div className="h-full overflow-y-auto p-4 md:p-6">
      <div className="max-w-6xl mx-auto space-y-5">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <h2 className="text-lg font-semibold">Centro Controllo Motori, Risorse & Cost Ledger</h2>
            <p className="text-xs font-mono text-fg-muted">
              Stream SSE in tempo reale · Mutua esclusione Fast ↔ Heavy MoE · Zero Disk Retention
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" size="sm" onClick={onRefreshTelemetry}>
              <RefreshCw className="h-3.5 w-3.5" /> Telemetria
            </Button>
            <Button variant="secondary" size="sm" loading={boundsLoading} onClick={() => void loadBounds(kvQuant, false)}>
              <RefreshCw className="h-3.5 w-3.5" /> Ricalcola Bounds
            </Button>
            <Button variant="secondary" size="sm" onClick={loadLedger}>
              <DollarSign className="h-3.5 w-3.5" /> Aggiorna Ledger
            </Button>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <Card>
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between">
                <CardTitle className="flex items-center gap-2 text-sm">
                  <Cpu className="h-4 w-4 text-primary" /> GPU VRAM
                </CardTitle>
                <Badge variant="neutral">{gpuTempC != null ? `${gpuTempC} °C` : "-- °C"}</Badge>
              </div>
              <div className="text-xs font-mono text-fg-muted truncate">
                {gpu?.name || (gpuAvailable ? "GPU Rilevata" : "Rilevamento GPU...")}
              </div>
            </CardHeader>
            <CardContent className="space-y-2">
              <progress className="wdk-progress" value={vramUsedPct} max={100} />
              <div className="grid grid-cols-2 gap-1 text-xs font-mono text-fg-muted">
                <span>Usata: {vramUsedGb ?? "--"} / {vramTotalGb ?? "--"} GB</span>
                <span>Libera: {vramFreeGb ?? "--"} GB</span>
                <span>Power: {gpuPowerW ?? "--"} W</span>
                <span>Util: {gpuUtilPct ?? "--"}%</span>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between">
                <CardTitle className="flex items-center gap-2 text-sm">
                  <MemoryStick className="h-4 w-4 text-primary" /> RAM Sistema
                </CardTitle>
                <Badge variant={heavySafe ? "success" : "warning"}>
                  {heavySafe ? "Anti-Swap OK" : "Sotto Soglia Heavy"}
                </Badge>
              </div>
              <div className="text-xs font-mono text-fg-muted">
                Soglia Heavy MoE: {ram?.heavyMoeMinFreeGb ?? "--"} GB liberi
              </div>
            </CardHeader>
            <CardContent className="space-y-2">
              <progress
                className={`wdk-progress ${heavySafe ? "is-success" : "is-warning"}`}
                value={ram?.usedPct ?? 0}
                max={100}
              />
              <div className="grid grid-cols-3 gap-1 text-xs font-mono text-fg-muted">
                <span>Usata: {ram?.usedGb ?? "--"}G</span>
                <span>Libera: {ram?.freeGb ?? "--"}G</span>
                <span>Tot: {ram?.totalGb ?? "--"}G</span>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between">
                <CardTitle className="flex items-center gap-2 text-sm">
                  <HardDrive className="h-4 w-4 text-primary" /> Storage Locale
                </CardTitle>
                <Badge variant="info">{disk?.drive || "C:\\"}</Badge>
              </div>
              <div className="text-xs font-mono text-fg-muted">Disco modelli / workspace</div>
            </CardHeader>
            <CardContent className="space-y-2">
              <progress className="wdk-progress" value={disk?.usedPct ?? 0} max={100} />
              <div className="grid grid-cols-3 gap-1 text-xs font-mono text-fg-muted">
                <span>Libero: {disk?.freeGb ?? "--"}G</span>
                <span>Usato: {disk?.usedGb ?? "--"}G</span>
                <span>Tot: {disk?.totalGb ?? "--"}G</span>
              </div>
            </CardContent>
          </Card>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <Card>
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between">
                <CardTitle className="text-sm">Fast Tier · Ollama</CardTitle>
                <StatusPill status={ol?.online ? "online" : "offline"} label={ol?.online ? "ONLINE" : "OFFLINE"} />
              </div>
            </CardHeader>
            <CardContent className="text-xs font-mono text-fg-muted space-y-1">
              <div>Stato: {ol?.online ? "🟢 Motore Locale Attivo" : "Inattivo"}</div>
              <div>Attivo: {ol?.activeModel || "--"}</div>
              <div>Modelli: {ol?.models?.length ?? 0} · num_ctx: {bounds?.fast_ctx?.active ?? ctxSize}</div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between">
                <CardTitle className="text-sm">Heavy MoE · Colibrì</CardTitle>
                <StatusPill status={hv?.online ? "online" : "idle"} label={hv?.online ? "ONLINE" : "OFFLINE"} />
              </div>
            </CardHeader>
            <CardContent className="text-xs font-mono text-fg-muted space-y-1">
              <div>Stato: {hv?.online ? "⚡ Motore Heavy Pronto" : "Standby"}</div>
              <div>Modello: {hv?.name || hv?.activeModel || "--"}</div>
              <div>Quant: {hv?.quant || "--"}</div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between">
                <CardTitle className="text-sm">LM Studio</CardTitle>
                <StatusPill status={lms?.online ? "online" : "idle"} label={lms?.online ? "ONLINE" : "OFFLINE"} />
              </div>
            </CardHeader>
            <CardContent className="text-xs font-mono text-fg-muted space-y-1">
              <div>Stato: {lms?.online ? "🟢 Connesso (Solo Locale)" : "Non rilevato"}</div>
              <div>Modelli: {lms?.models?.join(", ") || "--"}</div>
            </CardContent>
          </Card>
        </div>

        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between flex-wrap gap-2">
              <CardTitle className="text-base flex items-center gap-2">
                <Activity className="h-4 w-4 text-primary" /> Tuning Guidato (Layer 1 Math Rails) &amp; Controllo Motori
              </CardTitle>
              <div className="flex items-center gap-2">
                <select
                  aria-label="KV Cache Quantization"
                  value={kvQuant}
                  onChange={(e) => {
                    const nextQ = e.target.value === "f16" ? "f16" : "q8_0";
                    setKvQuant(nextQ);
                    void loadBounds(nextQ, false);
                  }}
                  className="px-2 py-1 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
                >
                  <option value="q8_0">KV Cache: q8_0 (1B/elem)</option>
                  <option value="f16">KV Cache: f16 (2B/elem)</option>
                </select>
                <Button variant="secondary" size="sm" onClick={() => setAdvisorOpen((v) => !v)}>
                  <Sparkles className="h-3.5 w-3.5" /> Chiedi all&apos;Hardware Advisor
                </Button>
              </div>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="p-3 rounded-md border border-border bg-surface-muted space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <label htmlFor="engRamSlider" className="text-xs font-semibold">
                    Heavy MoE RAM Limit: <span className="font-mono">{ramLimitGb} GB</span>
                  </label>
                  <Badge variant={zoneBadgeVariant(heavyRamZone)}>{zoneLabel(heavyRamZone)}</Badge>
                </div>
                <input
                  id="engRamSlider"
                  type="range"
                  min={heavyRamBounds.min}
                  max={heavyRamBounds.zones.hard_max}
                  step={1}
                  value={ramLimitGb}
                  onChange={(e) => setRamLimitGb(Number(e.target.value))}
                  className="w-full cursor-pointer accent-primary"
                />
                <progress
                  className={zoneProgressClass(heavyRamZone)}
                  value={Math.min(heavyRamBounds.zones.hard_max, ramLimitGb)}
                  max={heavyRamBounds.zones.hard_max}
                />
                <div className="flex items-center justify-between text-[11px] font-mono text-fg-muted">
                  <button type="button" className="underline" onClick={() => setRamLimitGb(heavyRamBounds.min)}>
                    Min: {heavyRamBounds.min} GB
                  </button>
                  <button type="button" className="underline text-success" onClick={() => setRamLimitGb(heavyRamBounds.recommended)}>
                    Rec: {heavyRamBounds.recommended} GB
                  </button>
                  <button type="button" className="underline text-warning" onClick={() => setRamLimitGb(heavyRamBounds.max_safe)}>
                    Max Safe: {heavyRamBounds.max_safe} GB
                  </button>
                </div>
                {heavyRamZone === "red" && (
                  <div className="text-[11px] font-mono text-danger flex items-center gap-1">
                    <ShieldAlert className="h-3.5 w-3.5 shrink-0" />
                    Supera il limite sicuro Layer 1 ({heavyRamBounds.max_safe} GB). Rischio swap/OOM.
                  </div>
                )}
              </div>

              <div className="p-3 rounded-md border border-border bg-surface-muted space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <label htmlFor="engCtxSlider" className="text-xs font-semibold">
                    Context (num_ctx): <span className="font-mono">{ctxSize.toLocaleString()}</span>
                  </label>
                  <Badge variant={zoneBadgeVariant(fastCtxZone)}>{zoneLabel(fastCtxZone)}</Badge>
                </div>
                <input
                  id="engCtxSlider"
                  type="range"
                  min={0}
                  max={ctxSteps.length - 1}
                  step={1}
                  value={ctxStepIdx}
                  onChange={(e) => setCtxSize(ctxSteps[Number(e.target.value)] ?? 16384)}
                  className="w-full cursor-pointer accent-primary"
                />
                <progress
                  className={zoneProgressClass(fastCtxZone)}
                  value={ctxStepIdx + 1}
                  max={ctxSteps.length}
                />
                <div className="flex items-center justify-between text-[11px] font-mono text-fg-muted">
                  <button type="button" className="underline" onClick={() => setCtxSize(fastCtxBounds.min)}>
                    Min: {fastCtxBounds.min}
                  </button>
                  <button type="button" className="underline text-success" onClick={() => setCtxSize(fastCtxBounds.recommended)}>
                    Rec: {fastCtxBounds.recommended}
                  </button>
                  <button type="button" className="underline text-warning" onClick={() => setCtxSize(fastCtxBounds.max_safe)}>
                    Max Safe: {fastCtxBounds.max_safe}
                  </button>
                </div>
                {fastCtxZone === "red" && (
                  <div className="text-[11px] font-mono text-danger flex items-center gap-1">
                    <ShieldAlert className="h-3.5 w-3.5 shrink-0" />
                    Supera il limite sicuro VRAM Layer 1 ({fastCtxBounds.max_safe}). Rischio spill PCIe/OOM.
                  </div>
                )}
              </div>
            </div>

            {bounds && (
              <div className="p-3 rounded-md border border-border bg-bg space-y-2 text-xs font-mono">
                <div className="flex items-center justify-between flex-wrap gap-2">
                  <span className="font-semibold text-fg">Anteprima Live Budget Memoria (Layer 1)</span>
                  <span className="text-fg-muted">
                    KV Cost ({bounds.budget.kv_quant}): {bounds.budget.kv_gb_per_1k} GB/1k tok · OS Reserves: {bounds.budget.os_vram_reserve_gb}G VRAM / {bounds.budget.os_ram_reserve_gb}G RAM
                  </span>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  <div className="space-y-1">
                    <div className="flex justify-between text-[11px] text-fg-muted">
                      <span>Fast VRAM: Pesi {bounds.budget.model_weights_gb}G + KV {estKvGb}G</span>
                      <span>Usabile: {bounds.budget.usable_vram_gb}G ({estVramPct}%)</span>
                    </div>
                    <progress className={zoneProgressClass(fastCtxZone)} value={estVramPct} max={100} />
                  </div>
                  <div className="space-y-1">
                    <div className="flex justify-between text-[11px] text-fg-muted">
                      <span>Heavy RAM: Pool {ramLimitGb}G + Host {bounds.budget.vk_host_pool_gb}G</span>
                      <span>Usabile: {bounds.budget.usable_ram_gb}G ({estRamPct}%)</span>
                    </div>
                    <progress className={zoneProgressClass(heavyRamZone)} value={estRamPct} max={100} />
                  </div>
                </div>
              </div>
            )}

            <div className="flex flex-wrap gap-2">
              <Button
                variant="primary"
                size="sm"
                loading={actionBusy}
                disabled={fastCtxZone === "red"}
                onClick={() => void runEngineAction("start_fast", true)}
              >
                <RotateCw className="h-3.5 w-3.5" /> Applica &amp; Riavvia Fast (Ollama)
              </Button>
              <Button
                variant="primary"
                size="sm"
                loading={actionBusy}
                disabled={heavyRamZone === "red" || heavyCtxZone === "red"}
                onClick={() => void runEngineAction("start_heavy", true)}
              >
                <RotateCw className="h-3.5 w-3.5" /> Applica &amp; Riavvia Heavy (Colibrì)
              </Button>
              <Button variant="secondary" size="sm" loading={actionBusy} onClick={() => void runEngineAction("start_fast", false)}>
                Avvia Fast Tier
              </Button>
              <Button variant="secondary" size="sm" loading={actionBusy} onClick={() => void runEngineAction("start_heavy", false)}>
                Avvia Heavy MoE
              </Button>
              <Button variant="secondary" size="sm" loading={actionBusy} onClick={() => void runEngineAction("unload_vram", false)}>
                Libera VRAM (Unload)
              </Button>
              <Button variant="danger" size="sm" loading={actionBusy} onClick={() => void runEngineAction("stop_all", false)}>
                Arresta Tutti i Motori
              </Button>
            </div>

            <pre className="p-3 rounded-md border border-border bg-surface-muted font-mono text-xs text-fg-muted overflow-x-auto max-h-40">
              {consoleLog}
            </pre>
          </CardContent>
        </Card>

        {advisorOpen && (
          <Card>
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between gap-2">
                <CardTitle className="text-base flex items-center gap-2">
                  <Sparkles className="h-4 w-4 text-primary" /> Hardware &amp; Tuning Advisor (Layer 2 · Local RAG + Fast-LLM)
                </CardTitle>
                <Button variant="secondary" size="sm" onClick={() => setAdvisorOpen(false)}>
                  <X className="h-3.5 w-3.5" /> Chiudi
                </Button>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="flex flex-wrap gap-1.5">
                {[
                  "Fin dove posso spingere Context e Heavy MoE RAM Limit senza andare in OOM?",
                  "Perché le prestazioni sono calate sotto 5 tok/s con context elevato?",
                  "Meglio KV cache q8_0 o f16 sulla mia GPU attuale?",
                ].map((preset) => (
                  <button
                    key={preset}
                    type="button"
                    onClick={() => {
                      setAdvisorQuestion(preset);
                      void askHardwareAdvisor(preset);
                    }}
                    className="px-2 py-1 text-[11px] font-mono rounded-sm border border-border bg-surface-muted hover:border-primary text-left"
                  >
                    {preset}
                  </button>
                ))}
              </div>
              <div className="flex gap-2">
                <input
                  aria-label="Domanda Hardware Advisor"
                  value={advisorQuestion}
                  onChange={(e) => setAdvisorQuestion(e.target.value)}
                  className="flex-1 px-2.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
                />
                <Button variant="primary" size="sm" loading={advisorBusy} onClick={() => void askHardwareAdvisor()}>
                  Analizza
                </Button>
              </div>
              {advisorError && (
                <div className="p-2.5 rounded-md border border-danger/40 bg-danger-bg text-xs font-mono text-danger">
                  {advisorError}
                </div>
              )}
              {advisorResult && (
                <div className="p-3 rounded-md border border-border bg-surface-muted space-y-2.5 text-xs">
                  <div className="flex items-center justify-between flex-wrap gap-2">
                    <div className="flex items-center gap-2">
                      <Badge variant={advisorResult.mode === "llm+kb" ? "success" : "info"}>
                        {advisorResult.mode === "llm+kb" ? "Fast-LLM + Local KB" : "KB Deterministico (Ollama offline)"}
                      </Badge>
                      {advisorResult.clamped && (
                        <Badge variant="warning">Clamped da Layer 1 Math Bounds</Badge>
                      )}
                    </div>
                    <Button size="sm" variant="primary" onClick={applyAdvisorRecommendations}>
                      Applica Consigli agli Slider
                    </Button>
                  </div>
                  <div className="grid grid-cols-3 gap-2 font-mono">
                    <div className="p-2 rounded-sm border border-border bg-bg">
                      Fast num_ctx: <strong>{advisorResult.recommended.fast_ctx}</strong>
                    </div>
                    <div className="p-2 rounded-sm border border-border bg-bg">
                      Heavy RAM Limit: <strong>{advisorResult.recommended.heavy_ram_limit_gb} GB</strong>
                    </div>
                    <div className="p-2 rounded-sm border border-border bg-bg">
                      Heavy ctx: <strong>{advisorResult.recommended.heavy_ctx}</strong>
                    </div>
                  </div>
                  <p className="text-fg leading-relaxed">{advisorResult.explanation}</p>
                  {advisorResult.warnings.length > 0 && (
                    <div className="text-warning font-mono space-y-0.5">
                      {advisorResult.warnings.map((w, idx) => (
                        <div key={idx}>• {w}</div>
                      ))}
                    </div>
                  )}
                  {advisorResult.kb_hits.length > 0 && (
                    <div className="text-[11px] font-mono text-fg-muted">
                      Fonti KB locali: {advisorResult.kb_hits.map((h) => h.citation).join(" · ")}
                    </div>
                  )}
                </div>
              )}
            </CardContent>
          </Card>
        )}

        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base flex items-center gap-2">
                <DollarSign className="h-4 w-4 text-success" /> Cost / Token Ledger & Budget Cap (RAM-Only)
              </CardTitle>
              <Badge variant="success">Retention: {ledger?.retention_mode || "ephemeral"}</Badge>
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
              <div className="p-2.5 rounded-md border border-border bg-surface-muted">
                <div className="text-[11px] text-fg-subtle">Richieste</div>
                <div className="text-base font-mono font-semibold">{ledger?.totals?.requests ?? 0}</div>
              </div>
              <div className="p-2.5 rounded-md border border-border bg-surface-muted">
                <div className="text-[11px] text-fg-subtle">Token Totali</div>
                <div className="text-base font-mono font-semibold">{ledger?.totals?.total_tokens ?? 0}</div>
              </div>
              <div className="p-2.5 rounded-md border border-border bg-surface-muted">
                <div className="text-[11px] text-fg-subtle">Retry / Escalation</div>
                <div className="text-base font-mono font-semibold">
                  {ledger?.totals?.retries ?? 0} / {ledger?.totals?.escalations ?? 0}
                </div>
              </div>
              <div className="p-2.5 rounded-md border border-border bg-surface-muted">
                <div className="text-[11px] text-fg-subtle">Stima Cloud Equiv.</div>
                <div className="text-base font-mono font-semibold text-success">
                  ${(ledger?.totals?.estimated_cost_usd ?? 0).toFixed(4)}
                </div>
              </div>
              <div className="p-2.5 rounded-md border border-border bg-surface-muted">
                <div className="text-[11px] text-fg-subtle">Workflow Attivi</div>
                <div className="text-base font-mono font-semibold">{ledgerEntries.length}</div>
              </div>
            </div>

            {ledgerEntries.length > 0 ? (
              <div className="overflow-x-auto">
                <table className="w-full text-xs border-collapse">
                  <thead>
                    <tr className="border-b border-border text-fg-muted text-left">
                      <th className="py-1.5 pr-3">App ID</th>
                      <th className="py-1.5 pr-3">Workflow ID</th>
                      <th className="py-1.5 pr-3">Req</th>
                      <th className="py-1.5 pr-3">Token (P+C)</th>
                      <th className="py-1.5 pr-3">Budget Cap</th>
                      <th className="py-1.5">Stima USD</th>
                    </tr>
                  </thead>
                  <tbody>
                    {ledgerEntries.map((entry) => (
                      <tr key={`${entry.app_id}-${entry.workflow_id}`} className="border-b border-border/60 font-mono">
                        <td className="py-1.5 pr-3">{entry.app_id}</td>
                        <td className="py-1.5 pr-3">{entry.workflow_id}</td>
                        <td className="py-1.5 pr-3">{entry.requests}</td>
                        <td className="py-1.5 pr-3">
                          {entry.total_tokens} ({entry.prompt_tokens}+{entry.completion_tokens})
                        </td>
                        <td className="py-1.5 pr-3">
                          <Badge variant={entry.budget.exceeded ? "danger" : "success"}>
                            {entry.budget.exceeded ? "EXCEEDED" : `${entry.budget.remaining_tokens} rim.`}
                          </Badge>
                        </td>
                        <td className="py-1.5">${entry.estimated_cost_usd.toFixed(5)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="text-xs text-fg-subtle font-mono">
                Nessuna richiesta contabilizzata nella sessione RAM corrente.
              </div>
            )}
          </CardContent>
        </Card>

        <IdeConfigsCard />
      </div>
    </div>
  );
};
