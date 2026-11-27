import React, { useCallback, useEffect, useState } from "react";
import { Download, Pause, Play, RefreshCw, Square, Thermometer } from "lucide-react";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle } from "@wdk";
import { apiFetch, apiJson } from "../lib/api";
import type { BatchJobItem, BatchWatchdogStatus } from "../types";

export const BatchRunnerPanel: React.FC = () => {
  const [taskType, setTaskType] = useState<
    "translate_paragraphs" | "bulk_summarize" | "code_docstring_audit" | "jsonl_extract"
  >("translate_paragraphs");
  const [model, setModel] = useState<string>("local-fast");
  const [targetLanguage, setTargetLanguage] = useState<string>("Italian");
  const [instruction, setInstruction] = useState<string>("");
  const [inputText, setInputText] = useState<string>("");
  const [persistCheckpoint, setPersistCheckpoint] = useState<boolean>(false);
  const [sizing, setSizing] = useState<{
    max_safe_ctx: number;
    max_chunk_chars: number;
    effective_chunk_chars: number;
  } | null>(null);
  const [jobs, setJobs] = useState<BatchJobItem[]>([]);
  const [watchdog, setWatchdog] = useState<BatchWatchdogStatus | null>(null);
  const [maxGpuTemp, setMaxGpuTemp] = useState<number>(82);
  const [minFreeRam, setMinFreeRam] = useState<number>(6);
  const [msg, setMsg] = useState<string>("");

  const refreshData = useCallback(async () => {
    const [jobsRes, sizeRes] = await Promise.all([
      apiJson<{ ok: boolean; jobs?: BatchJobItem[]; watchdog?: BatchWatchdogStatus }>(
        "/api/batch/jobs"
      ),
      apiJson<{
        ok: boolean;
        max_safe_ctx: number;
        max_chunk_chars: number;
        effective_chunk_chars: number;
      }>(`/api/batch/sizing?model=${encodeURIComponent(model)}`),
    ]);
    if (jobsRes.ok) {
      setJobs(jobsRes.data.jobs || []);
      if (jobsRes.data.watchdog) {
        setWatchdog(jobsRes.data.watchdog);
        setMaxGpuTemp(jobsRes.data.watchdog.thresholds.max_gpu_temp_c);
        setMinFreeRam(jobsRes.data.watchdog.thresholds.min_free_ram_gb);
      }
    }
    if (sizeRes.ok) setSizing(sizeRes.data);
  }, [model]);

  useEffect(() => {
    void refreshData();
  }, [refreshData]);

  useEffect(() => {
    const hasActive = jobs.some((j) =>
      ["queued", "running", "paused", "paused_watchdog"].includes(j.status)
    );
    if (!hasActive) return;
    const timer = window.setInterval(() => {
      void refreshData();
    }, 1500);
    return () => window.clearInterval(timer);
  }, [jobs, refreshData]);

  const handleStartJob = async () => {
    if (!inputText.trim()) {
      setMsg("Inserisci un testo o elenco blocchi da elaborare.");
      return;
    }
    const res = await apiJson<{ ok: boolean; error?: string; job?: BatchJobItem }>(
      "/api/batch/jobs",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_type: taskType,
          model,
          target_language: targetLanguage,
          instruction,
          input_text: inputText,
          persist_checkpoint: persistCheckpoint,
        }),
      }
    );
    if (res.ok && res.data.job) {
      setMsg(`Batch job avviato: ${res.data.job.job_id} (${res.data.job.total_chunks} chunk).`);
      void refreshData();
    } else {
      setMsg(res.data.error || `Errore avvio job (${res.status})`);
    }
  };

  const handleJobAction = async (jobId: string, action: "pause" | "resume" | "cancel") => {
    const res = await apiJson<{ ok: boolean; error?: string }>(
      `/api/batch/jobs/${encodeURIComponent(jobId)}/${action}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      }
    );
    setMsg(res.ok ? `Job ${jobId}: ${action} eseguito.` : res.data.error || "Errore controllo job");
    void refreshData();
  };

  const handleExport = async (jobId: string, fmt: "md" | "json" | "csv") => {
    const res = await apiFetch(
      `/api/batch/jobs/${encodeURIComponent(jobId)}/export?format=${fmt}`
    );
    if (!res.ok) {
      setMsg(`Errore export (${res.status})`);
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${jobId}.${fmt}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const saveWatchdog = async () => {
    const res = await apiJson<{ ok: boolean; watchdog?: BatchWatchdogStatus }>(
      "/api/batch/watchdog",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          max_gpu_temp_c: maxGpuTemp,
          cooldown_gpu_temp_c: Math.max(40, maxGpuTemp - 8),
          min_free_ram_gb: minFreeRam,
          resume_free_ram_gb: minFreeRam + 1.5,
        }),
      }
    );
    if (res.ok && res.data.watchdog) {
      setWatchdog(res.data.watchdog);
      setMsg("Soglie Thermal & RAM Watchdog aggiornate.");
    }
  };

  return (
    <div className="h-full overflow-y-auto p-4 md:p-6">
      <div className="max-w-6xl mx-auto space-y-5">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <h2 className="text-lg font-semibold">
              Overnight Batch Job Runner · Zero-Token Compute Locale
            </h2>
            <p className="text-xs font-mono text-fg-muted">
              Chunking entro max_safe_ctx · Checkpoint per chunk · Thermal & RAM Watchdog notturno · Export MD/JSON/CSV
            </p>
          </div>
          <Button size="sm" variant="secondary" onClick={() => void refreshData()}>
            <RefreshCw className="h-3.5 w-3.5" /> Aggiorna
          </Button>
        </div>

        {/* Thermal & RAM Watchdog */}
        <Card>
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between flex-wrap gap-2">
              <CardTitle className="text-sm flex items-center gap-2">
                <Thermometer className="h-4 w-4" /> Thermal & RAM Watchdog Notturno
              </CardTitle>
              <Badge variant={watchdog?.triggered ? "danger" : "success"}>
                {watchdog?.triggered ? "THROTTLED · PAUSA TERMICA/RAM" : "SAFE · READY"}
              </Badge>
            </div>
          </CardHeader>
          <CardContent className="flex flex-wrap items-center gap-3 text-xs font-mono">
            <Badge variant="neutral">
              GPU Temp: {watchdog?.gpu_temp_c != null ? `${watchdog.gpu_temp_c}°C` : "N/A"}
            </Badge>
            <Badge variant="neutral">
              Free RAM: {watchdog?.free_ram_gb != null ? `${watchdog.free_ram_gb} GiB` : "N/A"}
            </Badge>
            <label className="flex items-center gap-1.5">
              Max GPU °C:
              <input
                type="number"
                min={50}
                max={95}
                value={maxGpuTemp}
                onChange={(e) => setMaxGpuTemp(Number(e.target.value) || 82)}
                className="w-16 px-1.5 py-1 rounded-sm border border-border bg-bg text-fg"
              />
            </label>
            <label className="flex items-center gap-1.5">
              Min Free RAM (GB):
              <input
                type="number"
                min={2}
                max={64}
                step={0.5}
                value={minFreeRam}
                onChange={(e) => setMinFreeRam(Number(e.target.value) || 6)}
                className="w-16 px-1.5 py-1 rounded-sm border border-border bg-bg text-fg"
              />
            </label>
            <Button size="sm" variant="secondary" onClick={() => void saveWatchdog()}>
              Applica Soglie Watchdog
            </Button>
            {watchdog?.reason && <span className="text-danger">{watchdog.reason}</span>}
          </CardContent>
        </Card>

        {/* New Batch Job Form */}
        <Card>
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between flex-wrap gap-2">
              <CardTitle className="text-sm">Nuovo Lavoro Batch Chunked</CardTitle>
              {sizing && (
                <Badge variant="info">
                  max_safe_ctx={sizing.max_safe_ctx} · chunk={sizing.effective_chunk_chars} char
                </Badge>
              )}
            </div>
          </CardHeader>
          <CardContent className="space-y-3 text-xs">
            <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
              <div>
                <label className="block text-fg-muted mb-1">Tipo Task</label>
                <select
                  value={taskType}
                  onChange={(e) =>
                    setTaskType(
                      e.target.value as
                        | "translate_paragraphs"
                        | "bulk_summarize"
                        | "code_docstring_audit"
                        | "jsonl_extract"
                    )
                  }
                  className="w-full px-2 py-1.5 rounded-sm border border-border bg-bg text-fg font-mono"
                >
                  <option value="translate_paragraphs">Traduzione Paragrafi</option>
                  <option value="bulk_summarize">Summarization Massiva</option>
                  <option value="code_docstring_audit">Audit / Docstring Codice</option>
                  <option value="jsonl_extract">Estrazione Strutturata JSONL</option>
                </select>
              </div>
              <div>
                <label className="block text-fg-muted mb-1">Modello Locale</label>
                <select
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  className="w-full px-2 py-1.5 rounded-sm border border-border bg-bg text-fg font-mono"
                >
                  <option value="local-fast">local-fast (Ollama)</option>
                  <option value="local-balanced">local-balanced (Ollama)</option>
                  <option value="local-heavy">local-heavy (Colibrì MoE)</option>
                </select>
              </div>
              <div>
                <label className="block text-fg-muted mb-1">Lingua Target (se traduzione)</label>
                <input
                  value={targetLanguage}
                  onChange={(e) => setTargetLanguage(e.target.value)}
                  className="w-full px-2 py-1.5 rounded-sm border border-border bg-bg text-fg"
                />
              </div>
              <div className="flex items-end">
                <label className="flex items-center gap-2 cursor-pointer pb-1.5">
                  <input
                    type="checkbox"
                    checked={persistCheckpoint}
                    onChange={(e) => setPersistCheckpoint(e.target.checked)}
                  />
                  <span>Salva checkpoint su disco</span>
                </label>
              </div>
            </div>

            <div>
              <label className="block text-fg-muted mb-1">Istruzioni extra (opzionale)</label>
              <input
                value={instruction}
                onChange={(e) => setInstruction(e.target.value)}
                placeholder="Es. Mantieni i termini tecnici inglesi tra parentesi..."
                className="w-full px-2.5 py-1.5 rounded-sm border border-border bg-bg text-fg"
              />
            </div>

            <div>
              <label className="block text-fg-muted mb-1">
                Testo / Paragrafi / Codice da processare a chunk
              </label>
              <textarea
                rows={6}
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
                placeholder="Incolla qui testo lungo, paragrafi separati da riga vuota, o codice sorgente..."
                className="w-full px-2.5 py-1.5 rounded-sm border border-border bg-bg text-fg font-mono"
              />
            </div>

            <div className="flex items-center gap-3">
              <Button size="sm" variant="primary" onClick={() => void handleStartJob()}>
                <Play className="h-3.5 w-3.5" /> Avvia Job Batch
              </Button>
              {msg && <span className="font-mono text-primary">{msg}</span>}
            </div>
          </CardContent>
        </Card>

        {/* Jobs Queue */}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Coda Batch & Checkpoint ({jobs.length})</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {jobs.length === 0 ? (
              <div className="text-xs font-mono text-fg-subtle">
                Nessun job batch creato in questa sessione.
              </div>
            ) : (
              jobs.map((j) => {
                const badgeVar =
                  j.status === "completed"
                    ? "success"
                    : j.status === "paused_watchdog" || j.status === "failed"
                    ? "danger"
                    : j.status === "paused"
                    ? "warning"
                    : "info";
                return (
                  <div
                    key={j.job_id}
                    className="p-3 rounded-md border border-border bg-surface-muted space-y-2 text-xs"
                  >
                    <div className="flex items-center justify-between gap-2 flex-wrap">
                      <div className="flex items-center gap-2 font-mono">
                        <span className="font-semibold">{j.job_id}</span>
                        <Badge variant={badgeVar}>{j.status.toUpperCase()}</Badge>
                        <span>
                          {j.task_type} · {j.completed_chunks}/{j.total_chunks} ({j.progress_pct}%)
                        </span>
                        {j.eta_seconds != null && <span>ETA: {j.eta_seconds}s</span>}
                      </div>
                      <div className="flex items-center gap-1.5 flex-wrap">
                        {j.status === "running" && (
                          <Button
                            size="sm"
                            variant="secondary"
                            onClick={() => void handleJobAction(j.job_id, "pause")}
                          >
                            <Pause className="h-3.5 w-3.5" /> Pausa
                          </Button>
                        )}
                        {(j.status === "paused" || j.status === "failed") && (
                          <Button
                            size="sm"
                            variant="primary"
                            onClick={() => void handleJobAction(j.job_id, "resume")}
                          >
                            <Play className="h-3.5 w-3.5" /> Riprendi
                          </Button>
                        )}
                        {["queued", "running", "paused", "paused_watchdog"].includes(j.status) && (
                          <Button
                            size="sm"
                            variant="danger"
                            onClick={() => void handleJobAction(j.job_id, "cancel")}
                          >
                            <Square className="h-3.5 w-3.5" /> Annulla
                          </Button>
                        )}
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => void handleExport(j.job_id, "md")}
                        >
                          <Download className="h-3.5 w-3.5" /> MD
                        </Button>
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => void handleExport(j.job_id, "json")}
                        >
                          JSON
                        </Button>
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => void handleExport(j.job_id, "csv")}
                        >
                          CSV
                        </Button>
                      </div>
                    </div>
                    <div className="w-full h-2 rounded-full bg-bg overflow-hidden border border-border">
                      <div
                        className="h-full bg-primary transition-all"
                        style={{ width: `${Math.min(100, Math.max(0, j.progress_pct))}%` }}
                      />
                    </div>
                    {j.watchdog_reason && (
                      <div className="text-[11px] font-mono text-danger">
                        Watchdog: {j.watchdog_reason} (pause count: {j.watchdog_pause_count})
                      </div>
                    )}
                    {j.error && (
                      <div className="text-[11px] font-mono text-danger">Errore: {j.error}</div>
                    )}
                  </div>
                );
              })
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
};
