import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle2,
  Copy,
  Cpu,
  Download,
  Gauge,
  Globe,
  HardDrive,
  Pin,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle } from "@wdk";
import { apiJson, streamSsePost } from "../lib/api";
import type {
  BenchmarkRunResponse,
  DiscoveredLocalModel,
  HardwareWizardResponse,
  ModelCatalogItem,
  ModelCatalogResponse,
  ModelInventoryResponse,
  ModelPinShaResponse,
  ModelPullProgressEvent,
  ModelVerifyResponse,
  PreparedDownloadInfo,
} from "../types";

function buildBenchmarkMarkdownCard(res: BenchmarkRunResponse): string {
  const hw = res.hardware_summary;
  const gpuText = hw?.gpu_name
    ? `${hw.gpu_name}${hw.vram_total_gb ? ` (${hw.vram_total_gb} GB VRAM)` : ""}`
    : "CPU / N/A";
  const ramText = hw?.ram_total_gb ? `${hw.ram_total_gb} GB` : "N/A";
  return [
    `### Local LLM Benchmark Card — \`${res.model}\``,
    "",
    "| Metric | Value |",
    "| :--- | :--- |",
    `| Model / Tier | \`${res.model}\` (${res.tier || "--"} via ${res.backend || "--"}) |`,
    `| Run Type | ${res.cold_start ? "Cold Start" : "Warm"} |`,
    `| TTFT | ${res.ttft_ms != null ? `${res.ttft_ms} ms` : "--"} |`,
    `| Throughput (Total) | **${res.tok_per_sec ?? 0} tok/s** (${res.total_tokens ?? 0} tok in ${res.duration_s ?? 0} s) |`,
    `| Thinking Tokens | ${res.thinking_tokens ?? 0} tok (${res.thinking_tok_s ?? 0} tok/s) |`,
    `| Visible Tokens | ${res.visible_tokens ?? 0} tok (${res.visible_tok_s ?? 0} tok/s) |`,
    `| Hardware | ${gpuText} · RAM ${ramText} |`,
    `| Timestamp (UTC) | ${res.timestamp || "--"} |`,
  ].join("\n");
}

function triggerFileDownload(filename: string, content: string, mime: string): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export const ModelsWizardPanel: React.FC = () => {
  const [inventory, setInventory] = useState<ModelInventoryResponse | null>(null);
  const [verifyPath, setVerifyPath] = useState<string>("");
  const [expectedSha, setExpectedSha] = useState<string>("");
  const [verifyError, setVerifyError] = useState<string | null>(null);
  const [verifyResult, setVerifyResult] = useState<ModelVerifyResponse | null>(null);
  const [verifyBusy, setVerifyBusy] = useState<boolean>(false);
  const [pinBusy, setPinBusy] = useState<boolean>(false);
  const [catalog, setCatalog] = useState<ModelCatalogResponse | null>(null);
  const [tierFilter, setTierFilter] = useState<string>("all");
  const [roleFilter, setRoleFilter] = useState<string>("all");
  const [pendingPullModel, setPendingPullModel] = useState<ModelCatalogItem | null>(null);
  const [preparedDownload, setPreparedDownload] = useState<PreparedDownloadInfo | null>(null);
  const [prepareBusy, setPrepareBusy] = useState<boolean>(false);
  const [prepareError, setPrepareError] = useState<string | null>(null);
  const [confirmOnlinePull, setConfirmOnlinePull] = useState<boolean>(false);
  const [pullBusy, setPullBusy] = useState<boolean>(false);
  const [pullProgress, setPullProgress] = useState<ModelPullProgressEvent | null>(null);
  const [pullError, setPullError] = useState<string | null>(null);
  const [discoveredModels, setDiscoveredModels] = useState<DiscoveredLocalModel[] | null>(null);
  const [discoverBusy, setDiscoverBusy] = useState<boolean>(false);
  const pullAbortRef = useRef<AbortController | null>(null);
  const [wizard, setWizard] = useState<HardwareWizardResponse | null>(null);
  const [simVram, setSimVram] = useState<string>("");
  const [simRam, setSimRam] = useState<string>("");
  const [registryData, setRegistryData] = useState<{
    ollama?: { online?: boolean; models?: Array<{ name?: string; size_gb?: number; sizeGb?: number }> };
    heavy?: { files?: Array<{ name?: string; size_gb?: number; sizeGb?: number }> };
    heavyMoe?: { files?: Array<{ name?: string; size_gb?: number; sizeGb?: number }> };
  } | null>(null);
  const [benchModel, setBenchModel] = useState<string>("local-fast");
  const [benchWarmup, setBenchWarmup] = useState<boolean>(false);
  const [benchBusy, setBenchBusy] = useState<boolean>(false);
  const [benchError, setBenchError] = useState<string | null>(null);
  const [benchResult, setBenchResult] = useState<BenchmarkRunResponse | null>(null);
  const [benchCopied, setBenchCopied] = useState<boolean>(false);

  const loadAll = useCallback(async () => {
    const [invRes, wizRes, regRes, catRes] = await Promise.all([
      apiJson<ModelInventoryResponse>("/api/models/inventory"),
      apiJson<HardwareWizardResponse>("/api/system/hardware-wizard"),
      apiJson<{
        ollama?: { online?: boolean; models?: Array<{ name?: string; size_gb?: number; sizeGb?: number }> };
        heavy?: { files?: Array<{ name?: string; size_gb?: number; sizeGb?: number }> };
        heavyMoe?: { files?: Array<{ name?: string; size_gb?: number; sizeGb?: number }> };
      }>("/api/registry/local"),
      apiJson<ModelCatalogResponse>("/api/models/catalog"),
    ]);
    if (invRes.ok) {
      setInventory(invRes.data);
      const firstRel = invRes.data.models?.[0]?.rel_path || invRes.data.models?.[0]?.relative_path || "";
      if (firstRel) {
        setVerifyPath((prev) => prev || firstRel);
      }
    }
    if (wizRes.ok) {
      setWizard(wizRes.data);
      setSimVram(String(wizRes.data.vram_gb));
      setSimRam(String(wizRes.data.ram_gb));
    }
    if (regRes.ok) {
      setRegistryData(regRes.data);
    }
    if (catRes.ok && catRes.data?.ok) {
      setCatalog(catRes.data);
    }
  }, []);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  const handleVerifySha = async (relPath?: string) => {
    const firstModelRel =
      inventory?.models?.[0]?.rel_path || inventory?.models?.[0]?.relative_path || "";
    const target = (relPath ?? (verifyPath || firstModelRel)).trim();
    if (!target) {
      setVerifyResult(null);
      setVerifyError(
        "Nessun file modello selezionato o trovato in LOCAL_LLM_MODELS_DIR. Inserisci il percorso relativo (es. qwen3-14b.gguf) prima di verificare."
      );
      return;
    }
    setVerifyError(null);
    setVerifyPath(target);
    setVerifyBusy(true);
    try {
      const res = await apiJson<ModelVerifyResponse>("/api/models/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: target,
          expected_sha256: expectedSha.trim() || null,
        }),
      });
      if (!res.ok && res.data?.error) {
        setVerifyError(res.data.error);
      }
      setVerifyResult(res.data);
    } finally {
      setVerifyBusy(false);
    }
  };

  const handlePinSha = async (relPath?: string) => {
    const target = (relPath ?? verifyResult?.rel_path ?? verifyResult?.path ?? verifyPath).trim();
    if (!target) return;
    setVerifyError(null);
    setPinBusy(true);
    try {
      const res = await apiJson<ModelPinShaResponse>("/api/models/pin-sha256", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: target,
          expected_sha256: expectedSha.trim() || null,
        }),
      });
      if (!res.ok || !res.data?.ok) {
        setVerifyError(res.data?.error || `Errore pin SHA-256 (HTTP ${res.status})`);
        return;
      }
      setVerifyPath(target);
      setVerifyResult({
        ok: true,
        verified: true,
        status: "VERIFIED",
        path: res.data.path || target,
        rel_path: res.data.rel_path || target,
        computed_sha256: res.data.computed_sha256 || res.data.sha256,
        actual_sha256: res.data.actual_sha256 || res.data.sha256,
        expected_sha256: res.data.expected_sha256 || res.data.sha256,
      });
      await loadAll();
    } finally {
      setPinBusy(false);
    }
  };

  const filteredCatalog = useMemo(() => {
    const items = catalog?.models ?? [];
    return items.filter((item) => {
      if (tierFilter !== "all" && item.tier !== tierFilter) return false;
      if (roleFilter !== "all" && item.role !== roleFilter) return false;
      return true;
    });
  }, [catalog, tierFilter, roleFilter]);

  const openPullConfirmation = async (item: ModelCatalogItem) => {
    setPendingPullModel(item);
    setPreparedDownload(null);
    setPrepareError(null);
    setConfirmOnlinePull(false);
    setPullError(null);
    setPrepareBusy(true);
    try {
      const res = await apiJson<PreparedDownloadInfo & { error?: string }>("/api/models/download/prepare", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: item.id }),
      });
      if (res.ok && res.data?.ok) {
        setPreparedDownload(res.data);
      } else {
        setPrepareError(res.data?.error || `Pre-flight fallito (HTTP ${res.status})`);
      }
    } catch (err) {
      setPrepareError((err as Error).message);
    } finally {
      setPrepareBusy(false);
    }
  };

  const handleDiscoverLocal = async () => {
    setDiscoverBusy(true);
    try {
      const res = await apiJson<{ ok?: boolean; models?: DiscoveredLocalModel[] }>("/api/models/discover-local");
      if (res.ok && res.data?.ok) {
        setDiscoveredModels(res.data.models || []);
      }
    } finally {
      setDiscoverBusy(false);
    }
  };

  const startCatalogPull = async () => {
    if (!pendingPullModel || !confirmOnlinePull) return;
    const model = pendingPullModel;
    const prepared = preparedDownload;
    setPendingPullModel(null);
    setPreparedDownload(null);
    setPullBusy(true);
    setPullError(null);
    setPullProgress({
      model_id: model.id,
      backend: model.backend,
      stage: "starting",
      status: prepared && prepared.resume_offset_bytes > 0 ? "resuming" : "connecting",
      pct: 0,
    });

    const ctrl = new AbortController();
    pullAbortRef.current = ctrl;
    try {
      const endpoint = prepared?.download_token ? "/api/models/download/start" : "/api/models/pull";
      const bodyPayload = prepared?.download_token
        ? { download_token: prepared.download_token, confirm_authorize: true }
        : { model_id: model.id, confirm_online_pull: true };
      await streamSsePost(
        endpoint,
        bodyPayload,
        (evt) => {
          const parsed = evt as unknown as ModelPullProgressEvent;
          setPullProgress(parsed);
          if (parsed.stage === "error") {
            setPullError(parsed.error || "Errore durante il download.");
          }
        },
        (errMsg) => {
          setPullError(errMsg);
        },
        ctrl.signal
      );
      await loadAll();
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        setPullError((err as Error).message);
      }
    } finally {
      pullAbortRef.current = null;
      setPullBusy(false);
    }
  };

  const cancelCatalogPull = () => {
    pullAbortRef.current?.abort();
    setPullBusy(false);
  };

  const handleSimulateWizard = async () => {
    const v = Number(simVram);
    const r = Number(simRam);
    if (!Number.isFinite(v) || !Number.isFinite(r) || r < 1) return;
    const res = await apiJson<HardwareWizardResponse>(
      `/api/system/hardware-wizard?vram_gb=${encodeURIComponent(v)}&ram_gb=${encodeURIComponent(r)}`
    );
    if (res.ok) {
      setWizard(res.data);
    }
  };

  const runQuickBenchmark = async () => {
    setBenchBusy(true);
    setBenchError(null);
    setBenchCopied(false);
    try {
      const res = await apiJson<BenchmarkRunResponse>("/api/benchmark/run", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Allow-Tier-Switch": "true",
        },
        body: JSON.stringify({
          model: benchModel,
          max_tokens: 128,
          warmup: benchWarmup,
          allow_tier_switch: true,
        }),
      });
      if (!res.ok || !res.data?.ok || res.data?.status !== "OK") {
        setBenchResult(null);
        setBenchError(
          res.data?.error || `Benchmark fallito (HTTP ${res.status}): verifica che il motore selezionato sia attivo.`
        );
        return;
      }
      setBenchResult(res.data);
    } catch (err) {
      setBenchResult(null);
      setBenchError(`Errore rete benchmark: ${(err as Error).message}`);
    } finally {
      setBenchBusy(false);
    }
  };

  const handleDownloadBenchJson = () => {
    if (!benchResult) return;
    const safeModel = benchResult.model.replace(/[^a-zA-Z0-9._-]+/g, "_");
    triggerFileDownload(
      `local-llm-benchmark-${safeModel}-${Date.now()}.json`,
      JSON.stringify(benchResult, null, 2),
      "application/json;charset=utf-8"
    );
  };

  const handleDownloadBenchMd = () => {
    if (!benchResult) return;
    const safeModel = benchResult.model.replace(/[^a-zA-Z0-9._-]+/g, "_");
    triggerFileDownload(
      `local-llm-benchmark-${safeModel}-${Date.now()}.md`,
      buildBenchmarkMarkdownCard(benchResult),
      "text/markdown;charset=utf-8"
    );
  };

  const handleCopyBenchMd = async () => {
    if (!benchResult) return;
    const md = buildBenchmarkMarkdownCard(benchResult);
    try {
      await navigator.clipboard?.writeText(md);
      setBenchCopied(true);
      window.setTimeout(() => setBenchCopied(false), 2000);
    } catch {
      // Fallback: ignore if clipboard API unavailable
    }
  };

  return (
    <div className="h-full overflow-y-auto p-4 md:p-6">
      <div className="max-w-6xl mx-auto space-y-5">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <h2 className="text-lg font-semibold">Air-Gapped ModelManager SHA-256 & Hardware Setup Wizard</h2>
            <p className="text-xs font-mono text-fg-muted">
              100% Offline · Pesi isolati su LOCAL_LLM_MODELS_DIR · Verifica integrità SHA-256 locale
            </p>
          </div>
          <Button variant="secondary" size="sm" onClick={loadAll}>
            <RefreshCw className="h-3.5 w-3.5" /> Aggiorna Inventario
          </Button>
        </div>

        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between flex-wrap gap-2">
              <CardTitle className="text-base flex items-center gap-2">
                <ShieldCheck className="h-4 w-4 text-success" /> Air-Gapped Model Inventory & SHA-256 Verifier
              </CardTitle>
              <div className="flex items-center gap-2">
                <Badge variant={inventory?.air_gapped ? "success" : "warning"}>
                  {inventory?.air_gapped ? "Air-Gapped Active" : "Standard"}
                </Badge>
                <Badge variant="info">{inventory?.models_dir_source || "--"}</Badge>
              </div>
            </div>
            <div className="text-xs font-mono text-fg-muted">
              Directory: {inventory?.models_dir || "--"} · File rilevati: {inventory?.count ?? 0}
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-2 items-end">
              <div>
                <label htmlFor="verifyModelPath" className="block text-xs text-fg-muted mb-1">
                  Percorso relativo artefatto (es. qwen3-14b.gguf)
                </label>
                <input
                  id="verifyModelPath"
                  value={verifyPath}
                  onChange={(e) => setVerifyPath(e.target.value)}
                  className="w-full px-2.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
                />
              </div>
              <div>
                <label htmlFor="verifyExpectedSha" className="block text-xs text-fg-muted mb-1">
                  SHA-256 Atteso (opzionale se presente manifest.sha256.json)
                </label>
                <input
                  id="verifyExpectedSha"
                  value={expectedSha}
                  onChange={(e) => setExpectedSha(e.target.value)}
                  placeholder="64 hex chars..."
                  className="w-full px-2.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
                />
              </div>
              <Button variant="primary" size="sm" loading={verifyBusy} onClick={() => void handleVerifySha()}>
                <CheckCircle2 className="h-3.5 w-3.5" /> Verifica SHA-256 Offline
              </Button>
            </div>

            {verifyError && (
              <div className="p-2.5 rounded-md border border-danger/40 bg-danger-bg text-xs text-danger font-mono">
                {verifyError}
              </div>
            )}

            {verifyResult && (
              <div className="p-3 rounded-md border border-border bg-surface-muted text-xs font-mono space-y-1">
                <div className="flex items-center gap-2">
                  <Badge
                    variant={
                      verifyResult.status === "VERIFIED"
                        ? "success"
                        : verifyResult.status === "MISMATCH"
                          ? "danger"
                          : "warning"
                    }
                  >
                    {verifyResult.status}
                  </Badge>
                  <span>{verifyResult.path || verifyResult.rel_path}</span>
                </div>
                {(verifyResult.computed_sha256 || verifyResult.actual_sha256) && (
                  <div className="break-all">
                    Computed: {verifyResult.computed_sha256 || verifyResult.actual_sha256}
                  </div>
                )}
                {verifyResult.expected_sha256 && (
                  <div className="break-all">Expected: {verifyResult.expected_sha256}</div>
                )}
                {verifyResult.status === "UNVERIFIED_NO_MANIFEST" && (
                  <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
                    <span className="text-warning">
                      Digest calcolato offline con successo, ma nessuna voce trovata in manifest.sha256.json o campo Atteso.
                    </span>
                    <Button
                      size="sm"
                      variant="primary"
                      loading={pinBusy}
                      onClick={() => void handlePinSha(verifyResult.rel_path || verifyResult.path)}
                    >
                      <Pin className="h-3.5 w-3.5" /> Calcola &amp; Pin SHA-256 in manifest
                    </Button>
                  </div>
                )}
                {verifyResult.error && <div className="text-danger">{verifyResult.error}</div>}
              </div>
            )}

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <h3 className="text-xs font-semibold uppercase text-fg-muted mb-2">Pesi Locali (GGUF / Colibrì)</h3>
                {inventory?.models && inventory.models.length > 0 ? (
                  <div className="space-y-1.5">
                    {inventory.models.map((m) => {
                      const itemRel = m.rel_path || m.relative_path || m.name;
                      return (
                        <div
                          key={itemRel}
                          className="flex items-center justify-between gap-2 p-2 rounded-sm border border-border bg-surface-muted text-xs"
                        >
                          <div className="min-w-0">
                            <div className="font-mono font-medium truncate">{itemRel}</div>
                            <div className="text-[11px] font-mono text-fg-subtle">
                              {m.size_gb} GiB · {m.manifest_sha256 ? "Manifest SHA-256 presente" : "Nessun manifest"}
                            </div>
                          </div>
                          <div className="flex items-center gap-1.5 shrink-0">
                            <Button size="sm" variant="secondary" onClick={() => void handleVerifySha(itemRel)}>
                              Verifica
                            </Button>
                            {!m.manifest_sha256 && (
                              <Button
                                size="sm"
                                variant="primary"
                                loading={pinBusy}
                                onClick={() => void handlePinSha(itemRel)}
                              >
                                <Pin className="h-3 w-3" /> Pin
                              </Button>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <div className="text-xs text-fg-subtle font-mono">
                    Nessun peso GGUF/Colibrì trovato in LOCAL_LLM_MODELS_DIR.
                  </div>
                )}
              </div>

              <div>
                <h3 className="text-xs font-semibold uppercase text-fg-muted mb-2">
                  Modelfile Locali & Ollama ({registryData?.ollama?.models?.length ?? 0})
                </h3>
                <div className="space-y-1.5 max-h-48 overflow-y-auto">
                  {inventory?.modelfiles?.map((mf) => (
                    <div
                      key={mf.path}
                      className="p-2 rounded-sm border border-border bg-surface-muted text-xs font-mono"
                    >
                      <div className="font-semibold">{mf.filename}</div>
                      <div className="text-[11px] text-fg-muted">
                        FROM {mf.from || "--"} · num_ctx={mf.num_ctx ?? "--"}
                      </div>
                    </div>
                  ))}
                  {registryData?.ollama?.models?.map((om, idx) => (
                    <div
                      key={`${om.name}-${idx}`}
                      className="p-2 rounded-sm border border-border bg-surface-muted text-xs font-mono flex justify-between"
                    >
                      <span>{om.name}</span>
                      <span className="text-fg-muted">{om.size_gb ?? om.sizeGb ?? "--"} GB</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between flex-wrap gap-2">
              <CardTitle className="text-base flex items-center gap-2">
                <Cpu className="h-4 w-4 text-primary" /> Hardware Setup Wizard (Portabile)
              </CardTitle>
              <Badge variant="ai">{wizard?.profile_id || "--"}</Badge>
            </div>
            <div className="text-xs text-fg-muted">{wizard?.label || "Rilevamento profilo hardware..."}</div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 items-end">
              <div>
                <label htmlFor="wizVram" className="block text-xs text-fg-muted mb-1">
                  Simula VRAM (GB)
                </label>
                <input
                  id="wizVram"
                  type="number"
                  min={0}
                  max={512}
                  value={simVram}
                  onChange={(e) => setSimVram(e.target.value)}
                  className="w-full px-2.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
                />
              </div>
              <div>
                <label htmlFor="wizRam" className="block text-xs text-fg-muted mb-1">
                  Simula RAM Sistema (GB)
                </label>
                <input
                  id="wizRam"
                  type="number"
                  min={4}
                  max={2048}
                  value={simRam}
                  onChange={(e) => setSimRam(e.target.value)}
                  className="w-full px-2.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
                />
              </div>
              <Button variant="primary" size="sm" onClick={() => void handleSimulateWizard()}>
                <HardDrive className="h-3.5 w-3.5" /> Calcola Profilo Ottimale
              </Button>
            </div>

            {wizard && (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div className="p-3 rounded-md border border-border bg-surface-muted text-xs font-mono space-y-1.5">
                  <div>GPU: {wizard.gpu_name} ({wizard.vram_gb} GB VRAM)</div>
                  <div>RAM: {wizard.ram_gb} GB · Tier: {wizard.recommended_tier}</div>
                  <div>num_ctx: {wizard.num_ctx} · Heavy MoE: {wizard.heavy_moe_enabled ? "ABILITATO" : "OFF"}</div>
                  <div>
                    Anti-Swap: HEAVY_MOE_RAM_GB={wizard.heavy_moe_ram_gb} · MIN_FREE={wizard.heavy_moe_min_free_ram_gb} GB
                  </div>
                  <div>WSL2 Memory: {wizard.wsl_memory_gb} GB</div>
                  <div className="truncate">MODELS_DIR: {wizard.local_llm_models_dir}</div>
                </div>
                <pre className="p-3 rounded-md border border-border bg-surface-muted text-xs font-mono overflow-x-auto">
                  {Object.entries(wizard.env_overrides)
                    .map(([k, v]) => `${k}=${v}`)
                    .join("\n")}
                </pre>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between flex-wrap gap-2">
              <CardTitle className="text-base flex items-center gap-2">
                <Gauge className="h-4 w-4 text-primary" /> Benchmark Rapido &amp; Shareable Card
              </CardTitle>
              {benchResult && (
                <div className="flex flex-wrap gap-1.5">
                  <Button size="sm" variant="secondary" onClick={handleDownloadBenchJson}>
                    <Download className="h-3.5 w-3.5" /> Download JSON
                  </Button>
                  <Button size="sm" variant="secondary" onClick={handleDownloadBenchMd}>
                    <Download className="h-3.5 w-3.5" /> Download Markdown
                  </Button>
                  <Button size="sm" variant="secondary" onClick={() => void handleCopyBenchMd()}>
                    <Copy className="h-3.5 w-3.5" /> {benchCopied ? "Copiato!" : "Copy Markdown"}
                  </Button>
                </div>
              )}
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex flex-wrap gap-3 items-center">
              <select
                aria-label="Modello benchmark"
                value={benchModel}
                onChange={(e) => setBenchModel(e.target.value)}
                className="px-2.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
              >
                <option value="local-fast">local-fast</option>
                <option value="local-balanced">local-balanced</option>
                <option value="local-heavy">local-heavy</option>
                {registryData?.ollama?.models?.map((om) =>
                  om.name ? (
                    <option key={om.name} value={om.name}>
                      {om.name}
                    </option>
                  ) : null
                )}
              </select>
              <label className="flex items-center gap-1.5 text-xs text-fg-muted cursor-pointer">
                <input
                  type="checkbox"
                  checked={benchWarmup}
                  onChange={(e) => setBenchWarmup(e.target.checked)}
                />
                Warmup prima della misura
              </label>
              <Button size="sm" variant="primary" loading={benchBusy} onClick={() => void runQuickBenchmark()}>
                Esegui Benchmark Rapido
              </Button>
            </div>

            {benchError && (
              <div className="p-3 rounded-md border border-danger/40 bg-danger-bg text-xs font-mono text-danger">
                Benchmark fallito: {benchError}
              </div>
            )}

            {benchResult ? (
              <div className="space-y-3">
                <div className="flex items-center gap-2 flex-wrap text-xs font-mono">
                  <Badge variant="success">{benchResult.model}</Badge>
                  <Badge variant={benchResult.cold_start ? "warning" : "info"}>
                    {benchResult.cold_start ? "Cold Start" : "Warm"}
                  </Badge>
                  <span className="text-fg-muted">
                    Tier: {benchResult.tier || "--"} ({benchResult.backend || "--"})
                  </span>
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-5 gap-2.5 text-xs font-mono">
                  <div className="p-2.5 rounded-md border border-border bg-surface-muted">
                    <div className="text-[11px] text-fg-subtle">TTFT</div>
                    <div className="text-base font-semibold">
                      {benchResult.ttft_ms != null ? `${benchResult.ttft_ms} ms` : "--"}
                    </div>
                  </div>
                  <div className="p-2.5 rounded-md border border-border bg-surface-muted">
                    <div className="text-[11px] text-fg-subtle">Totale tok/s</div>
                    <div className="text-base font-semibold text-success">
                      {benchResult.tok_per_sec ?? 0} tok/s
                    </div>
                    <div className="text-[11px] text-fg-muted">{benchResult.total_tokens ?? 0} tok</div>
                  </div>
                  <div className="p-2.5 rounded-md border border-border bg-surface-muted">
                    <div className="text-[11px] text-fg-subtle">Thinking tok/s</div>
                    <div className="text-base font-semibold">
                      {benchResult.thinking_tok_s ?? 0} tok/s
                    </div>
                    <div className="text-[11px] text-fg-muted">{benchResult.thinking_tokens ?? 0} tok</div>
                  </div>
                  <div className="p-2.5 rounded-md border border-border bg-surface-muted">
                    <div className="text-[11px] text-fg-subtle">Visible tok/s</div>
                    <div className="text-base font-semibold">
                      {benchResult.visible_tok_s ?? 0} tok/s
                    </div>
                    <div className="text-[11px] text-fg-muted">{benchResult.visible_tokens ?? 0} tok</div>
                  </div>
                  <div className="p-2.5 rounded-md border border-border bg-surface-muted">
                    <div className="text-[11px] text-fg-subtle">Durata</div>
                    <div className="text-base font-semibold">{benchResult.duration_s ?? 0} s</div>
                    <div className="text-[11px] text-fg-muted truncate">
                      {benchResult.hardware_summary?.gpu_name || "CPU"}
                    </div>
                  </div>
                </div>
              </div>
            ) : (
              !benchError && (
                <pre className="p-3 rounded-md border border-border bg-surface-muted text-xs font-mono text-fg-muted">
                  Premi &apos;Esegui Benchmark Rapido&apos; per misurare TTFT, Thinking vs Visible tok/s e generare la Benchmark Card.
                </pre>
              )
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between flex-wrap gap-2">
              <CardTitle className="text-base flex items-center gap-2">
                <Download className="h-4 w-4 text-primary" /> Catalogo Modelli Curato &amp; Downloader Guidato (Opt-In)
              </CardTitle>
              <div className="flex flex-wrap gap-2">
                <select
                  aria-label="Filtra per Tier Hardware"
                  value={tierFilter}
                  onChange={(e) => setTierFilter(e.target.value)}
                  className="px-2 py-1 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
                >
                  <option value="all">Tutti i Tier</option>
                  <option value="cpu">CPU / 4 GB</option>
                  <option value="vram_8gb">8 GB VRAM</option>
                  <option value="vram_16gb">16 GB VRAM</option>
                  <option value="vram_24gb+">24 GB+ VRAM</option>
                </select>
                <select
                  aria-label="Filtra per Ruolo"
                  value={roleFilter}
                  onChange={(e) => setRoleFilter(e.target.value)}
                  className="px-2 py-1 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
                >
                  <option value="all">Tutti i Ruoli</option>
                  <option value="fast_tools">Fast Chat &amp; Tools</option>
                  <option value="uncensored">Uncensored Local</option>
                  <option value="heavy_moe">Heavy MoE Overnight</option>
                  <option value="embeddings">Embeddings</option>
                </select>
              </div>
            </div>
            <div className="text-xs font-mono text-fg-muted">
              Default 100% Air-Gapped · Il download richiede conferma esplicita di connessione temporanea per singolo modello
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            {(pullBusy || pullProgress || pullError) && (
              <div className="p-3 rounded-md border border-border bg-surface-muted space-y-2 text-xs font-mono">
                <div className="flex items-center justify-between gap-2">
                  <span>
                    Download: <strong>{pullProgress?.model_id || "--"}</strong> ({pullProgress?.status || pullProgress?.stage || "--"})
                  </span>
                  <div className="flex items-center gap-2">
                    <span>{pullProgress?.pct ?? 0}%</span>
                    {pullBusy && (
                      <Button size="sm" variant="danger" onClick={cancelCatalogPull}>
                        <X className="h-3 w-3" /> Annulla
                      </Button>
                    )}
                  </div>
                </div>
                <progress
                  className={`wdk-progress ${
                    pullError || pullProgress?.stage === "error"
                      ? "is-danger"
                      : pullProgress?.stage === "completed"
                        ? "is-success"
                        : ""
                  }`}
                  value={pullProgress?.pct ?? 0}
                  max={100}
                />
                {pullProgress?.stage === "completed" && pullProgress.path && (
                  <div className="flex items-center justify-between flex-wrap gap-2">
                    <span className={pullProgress.verified ? "text-success" : "text-warning"}>
                      {pullProgress.verified
                        ? `Verifica SHA-256 completata: ${pullProgress.path}`
                        : `Scaricato in ${pullProgress.path} (${pullProgress.status})`}
                    </span>
                    {!pullProgress.verified && (
                      <Button
                        size="sm"
                        variant="primary"
                        loading={pinBusy}
                        onClick={() => void handlePinSha(pullProgress.path)}
                      >
                        <Pin className="h-3 w-3" /> Pin SHA-256
                      </Button>
                    )}
                  </div>
                )}
                {pullError && <div className="text-danger">{pullError}</div>}
              </div>
            )}

            <div className="p-3 rounded-md border border-border bg-surface-muted space-y-2">
              <div className="flex items-center justify-between flex-wrap gap-2">
                <div className="text-xs">
                  <div className="font-semibold text-fg">Zero-Copy Local Discovery (Sola Lettura)</div>
                  <div className="text-[11px] text-fg-muted">
                    Scansiona cache locali esistenti (Ollama / HuggingFace / LM Studio) consumando 0 GB extra.
                  </div>
                </div>
                <Button size="sm" variant="secondary" loading={discoverBusy} onClick={() => void handleDiscoverLocal()}>
                  <HardDrive className="h-3.5 w-3.5" /> Scansiona Modelli Locali
                </Button>
              </div>
              {discoveredModels !== null && (
                discoveredModels.length > 0 ? (
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                    {discoveredModels.map((dm) => (
                      <div
                        key={dm.path}
                        className="p-2 rounded-sm border border-border bg-bg text-[11px] font-mono space-y-0.5"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-semibold text-fg truncate">{dm.name}</span>
                          <Badge variant="success">Zero-Copy · 0 GB extra</Badge>
                        </div>
                        <div className="text-fg-muted">
                          {dm.source} · {dm.size_gb} GB · {dm.hardware_fit.emoji} {dm.hardware_fit.label} (~{dm.hardware_fit.est_tok_s} tok/s)
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="text-[11px] font-mono text-fg-subtle">
                    Nessun modello esterno rilevato nelle cache locali standard.
                  </div>
                )
              )}
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {filteredCatalog.map((item) => {
                const fitVariant =
                  item.hardware_fit?.level === "green" || item.fits_hardware === "optimal"
                    ? "success"
                    : item.hardware_fit?.level === "yellow" || item.fits_hardware === "tight"
                      ? "warning"
                      : "danger";
                const fitLabel = item.hardware_fit
                  ? `${item.hardware_fit.emoji} ${item.hardware_fit.label} (~${item.hardware_fit.est_tok_s} tok/s)`
                  : item.fits_hardware === "optimal"
                    ? "🟢 Fits Hardware · Optimal"
                    : item.fits_hardware === "tight"
                      ? "🟡 Fits Hardware · Tight"
                      : "🔴 Too Large for Host";
                return (
                  <div
                    key={item.id}
                    className="p-3 rounded-md border border-border bg-surface-muted flex flex-col justify-between gap-2.5 text-xs"
                  >
                    <div className="space-y-1.5">
                      <div className="flex items-start justify-between gap-2">
                        <div className="font-semibold text-fg">{item.name}</div>
                        <Badge variant={fitVariant}>{fitLabel}</Badge>
                      </div>
                      <div className="flex flex-wrap gap-1.5 font-mono text-[11px]">
                        <Badge variant="info">{item.tier}</Badge>
                        <Badge variant="neutral">{item.role}</Badge>
                        <Badge variant="neutral">{item.backend.toUpperCase()}</Badge>
                        {item.installed && <Badge variant="success">Installato</Badge>}
                      </div>
                      {item.summary && <p className="text-fg-muted leading-snug">{item.summary}</p>}
                      <div className="font-mono text-[11px] text-fg-subtle">
                        Req: {item.vram_required_gb}G VRAM · {item.ram_required_gb}G RAM · {item.disk_required_gb}G Disk
                      </div>
                      <div className="font-mono text-[11px] text-fg-muted">{item.fits_reason}</div>
                    </div>
                    <div className="flex items-center justify-between gap-2 pt-1 border-t border-border/60">
                      <span className="font-mono text-[11px] text-fg-subtle truncate">
                        {item.ollama_tag || item.filename || item.id}
                      </span>
                      <Button
                        size="sm"
                        variant={item.installed ? "secondary" : "primary"}
                        disabled={pullBusy || !item.has_download_source}
                        onClick={() => void openPullConfirmation(item)}
                      >
                        <Download className="h-3.5 w-3.5" /> {item.installed ? "Re-Scarica" : "Scarica"}
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>

        {pendingPullModel && (
          <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4">
            <div className="max-w-lg w-full rounded-lg border border-border bg-surface p-5 space-y-4 shadow-xl">
              <div className="flex items-center justify-between gap-2">
                <h3 className="text-base font-semibold flex items-center gap-2">
                  <Globe className="h-4 w-4 text-warning" /> Autorizzazione Download Modello
                </h3>
                <Button size="sm" variant="secondary" onClick={() => setPendingPullModel(null)}>
                  <X className="h-3.5 w-3.5" />
                </Button>
              </div>
              <div className="text-xs space-y-2 text-fg-muted">
                <p>
                  <strong>Local LLM è 100% air-gapped di default.</strong> Nessun download parte senza tua autorizzazione
                  esplicita verso i soli domini HTTPS consentiti.
                </p>
                {prepareBusy && (
                  <div className="p-2.5 rounded-md border border-border bg-surface-muted font-mono">
                    Verifica pre-flight (dimensione esatta, spazio disco, Hardware-Fit)...
                  </div>
                )}
                {prepareError && (
                  <div className="p-2.5 rounded-md border border-danger/40 bg-danger-bg text-danger font-mono">
                    {prepareError}
                  </div>
                )}
                <div className="p-2.5 rounded-md border border-border bg-surface-muted font-mono space-y-1 text-fg">
                  <div>Modello: {pendingPullModel.name}</div>
                  <div>Backend: {pendingPullModel.backend.toUpperCase()}</div>
                  <div>
                    Dimensione: {preparedDownload ? `${preparedDownload.exact_gb} GB` : `${pendingPullModel.disk_required_gb} GB`}
                    {preparedDownload ? ` · Spazio libero: ${preparedDownload.free_disk_gb} GB` : ""}
                  </div>
                  {preparedDownload?.hardware_fit && (
                    <div>
                      Hardware-Fit: {preparedDownload.hardware_fit.emoji} {preparedDownload.hardware_fit.label} (~{preparedDownload.hardware_fit.est_tok_s} tok/s)
                    </div>
                  )}
                  {preparedDownload && (
                    <>
                      <div>Licenza SPDX: {preparedDownload.license_spdx}</div>
                      {preparedDownload.expected_sha256 && (
                        <div className="break-all">SHA-256: {preparedDownload.expected_sha256}</div>
                      )}
                      {preparedDownload.resume_offset_bytes > 0 && (
                        <div className="text-info">
                          Ripresa automatica HTTP Range da {(preparedDownload.resume_offset_bytes / (1024 * 1024)).toFixed(1)} MB
                        </div>
                      )}
                      {!preparedDownload.sufficient_disk && (
                        <div className="text-danger">
                          Spazio disco insufficiente (richiesto almeno 10% di margine libero).
                        </div>
                      )}
                    </>
                  )}
                </div>
                <label className="flex items-start gap-2 cursor-pointer pt-1 text-fg">
                  <input
                    type="checkbox"
                    checked={confirmOnlinePull}
                    onChange={(e) => setConfirmOnlinePull(e.target.checked)}
                  />
                  <span>
                    Confermo di voler autorizzare il download e la verifica integrità SHA-256 locale.
                  </span>
                </label>
              </div>
              <div className="flex justify-end gap-2">
                <Button size="sm" variant="secondary" onClick={() => setPendingPullModel(null)}>
                  Annulla (Resta Air-Gapped)
                </Button>
                <Button
                  size="sm"
                  variant="primary"
                  disabled={
                    !confirmOnlinePull ||
                    prepareBusy ||
                    (preparedDownload !== null && !preparedDownload.sufficient_disk)
                  }
                  onClick={() => void startCatalogPull()}
                >
                  <Download className="h-3.5 w-3.5" /> Autorizza Download (
                  {preparedDownload ? `${preparedDownload.exact_gb} GB` : `${pendingPullModel.disk_required_gb} GB`})
                </Button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
