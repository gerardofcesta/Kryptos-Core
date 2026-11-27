import React, { useCallback, useEffect, useState } from "react";
import { Pause, Play, RefreshCw, ShieldAlert, ShieldCheck, Square, Trash2 } from "lucide-react";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, StatusPill } from "@wdk";
import { apiJson } from "../lib/api";
import { renderSafeMarkdownHtml } from "../lib/markdown";
import type { CodeAuditResult, OrchestratorStatusPayload } from "../types";

interface OrchestratorPanelProps {
  liveStatus: OrchestratorStatusPayload | null;
  onRefreshStatus: () => void;
}

const FSM_PHASES = ["SCOUT", "STRATEGIST", "INVESTIGATOR", "VALIDATOR", "ADJUDICATOR"];

const MISSION_PRESETS: Array<{
  id: string;
  label: string;
  profile: string;
  mission: string;
}> = [
  {
    id: "security_audit",
    label: "Security & Dependency Audit",
    profile: "code_audit",
    mission:
      "Analizza la cartella target in sola lettura, verifica Anti-Slopsquatting e Secret Scan, individua vulnerabilità critiche e produci un report verificato.",
  },
  {
    id: "codebase_review",
    label: "Private Codebase Review",
    profile: "general",
    mission:
      "Esamina architettura, entrypoints, race condition e bug di correttezza nella codebase locale in sola lettura senza inviare dati all'esterno.",
  },
  {
    id: "document_analysis",
    label: "Document Analysis",
    profile: "document_research",
    mission:
      "Analizza i documenti e i dati strutturati nella cartella target, estrai fatti chiave, contraddizioni e anomalie con citazioni esatte.",
  },
];

export const OrchestratorPanel: React.FC<OrchestratorPanelProps> = ({
  liveStatus,
  onRefreshStatus,
}) => {
  const [retentionMode, setRetentionMode] = useState<"ephemeral" | "persistent">("ephemeral");
  const [missionProfile, setMissionProfile] = useState<string>("code_audit");
  const [executionMode, setExecutionMode] = useState<"dual_tier" | "fast_only">("dual_tier");
  const [sandboxMode, setSandboxMode] = useState<"auto" | "host_readonly" | "docker">("auto");
  const [targetPath, setTargetPath] = useState<string>("");
  const [mission, setMission] = useState<string>(MISSION_PRESETS[0].mission);
  const [maxSteps, setMaxSteps] = useState<number>(30);
  const [maxEscalations, setMaxEscalations] = useState<number>(3);
  const [preflight, setPreflight] = useState<{
    dockerDaemon?: boolean;
    sandboxImageReady?: boolean;
    heavyReady?: boolean;
    readyForSandbox?: boolean;
    hostReadOnlyReady?: boolean;
    warnings?: string[];
  } | null>(null);
  const [auditResult, setAuditResult] = useState<CodeAuditResult | null>(null);
  const [auditBusy, setAuditBusy] = useState<boolean>(false);
  const [wizardMsg, setWizardMsg] = useState<string>("");
  const [reportMd, setReportMd] = useState<string>("");
  const [reportVerdict, setReportVerdict] = useState<string | null>(null);
  const [vaults, setVaults] = useState<Array<Record<string, unknown>>>([]);

  const loadPreflightAndVaults = useCallback(async () => {
    const [preRes, vaultRes] = await Promise.all([
      apiJson<{
        dockerDaemon?: boolean;
        sandboxImageReady?: boolean;
        heavyReady?: boolean;
        readyForSandbox?: boolean;
        hostReadOnlyReady?: boolean;
        warnings?: string[];
      }>("/api/orchestrator/preflight"),
      apiJson<{ vaults?: Array<Record<string, unknown>> }>("/api/orchestrator/vaults"),
    ]);
    if (preRes.ok) setPreflight(preRes.data);
    if (vaultRes.ok && Array.isArray(vaultRes.data?.vaults)) setVaults(vaultRes.data.vaults);
  }, []);

  useEffect(() => {
    void loadPreflightAndVaults();
  }, [loadPreflightAndVaults]);

  const activeCodeAudit =
    auditResult ||
    liveStatus?.code_audit ||
    liveStatus?.surface_map?.code_audit ||
    liveStatus?.surface_map?.codeAudit ||
    null;

  const runDeterministicCodeAudit = async () => {
    if (!targetPath.trim()) {
      setWizardMsg("Inserisci un percorso target valido per eseguire code_audit.");
      return;
    }
    setAuditBusy(true);
    setWizardMsg("Esecuzione scan code_audit (Anti-Slopsquatting + Secret Scan)...");
    try {
      const res = await apiJson<CodeAuditResult & { error?: string }>(
        "/api/orchestrator/code-audit",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            target_path: targetPath.trim(),
            check_secrets: true,
            check_slopsquatting: true,
          }),
        }
      );
      if (res.ok && res.data.ok) {
        setAuditResult(res.data);
        setWizardMsg(res.data.summary);
      } else {
        setWizardMsg(res.data.error || `Errore code_audit (${res.status})`);
      }
    } finally {
      setAuditBusy(false);
    }
  };

  const handleStart = async () => {
    const res = await apiJson<{ ok: boolean; error?: string; run_id?: string }>(
      "/api/orchestrator/start",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          operator: "orchestrator",
          retention_mode: retentionMode,
          mission_profile: missionProfile,
          execution_mode: executionMode,
          sandbox_mode: sandboxMode,
          target_path: targetPath.trim(),
          mission: mission.trim(),
          budget: {
            max_steps: maxSteps,
            max_heavy_escalations: maxEscalations,
          },
        }),
      }
    );
    if (res.ok && res.data.ok !== false) {
      setWizardMsg(`Run avviata: ${res.data.run_id || "attiva"}`);
      onRefreshStatus();
    } else {
      setWizardMsg(res.data.error || `Avvio rifiutato (${res.status})`);
    }
  };

  const handleControl = async (endpoint: "pause" | "resume" | "stop" | "purge-report") => {
    const res = await apiJson<{ ok: boolean; error?: string }>(`/api/orchestrator/${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (endpoint === "purge-report" && res.ok) {
      setReportMd("");
      setReportVerdict(null);
    }
    setWizardMsg(res.ok ? `Comando ${endpoint} eseguito.` : res.data.error || `Errore ${endpoint}`);
    onRefreshStatus();
    void loadPreflightAndVaults();
  };

  const fetchReport = async () => {
    const res = await apiJson<{ ok: boolean; report_md?: string; report?: string; verdict?: string }>(
      "/api/orchestrator/report"
    );
    if (res.ok) {
      setReportMd(res.data.report_md || res.data.report || "");
      setReportVerdict(res.data.verdict || null);
    }
  };

  const deleteVault = async (runId: string) => {
    const res = await apiJson<{ ok: boolean; error?: string }>(
      `/api/orchestrator/vaults/${encodeURIComponent(runId)}`,
      { method: "DELETE" }
    );
    if (res.ok) {
      void loadPreflightAndVaults();
    } else {
      setWizardMsg(res.data.error || "Errore eliminazione vault");
    }
  };

  const stateName = String(liveStatus?.state || liveStatus?.phase || "IDLE").toUpperCase();
  const isActive = Boolean(liveStatus?.active);
  const step = liveStatus?.step ?? liveStatus?.current_step ?? 0;
  const maxSt = liveStatus?.max_steps ?? liveStatus?.maxSteps ?? 0;
  const isDockerIsolated =
    liveStatus?.resolved_sandbox_mode === "docker" &&
    Boolean(preflight?.readyForSandbox ?? true);
  const modeBadgeLabel =
    liveStatus?.resolved_sandbox_mode === "host_readonly" || !preflight?.readyForSandbox
      ? "Host Read-Only Safe Mode"
      : isDockerIsolated
      ? "Docker Isolated"
      : "Host Read-Only Safe Mode";
  const eventsList = Array.isArray(liveStatus?.events) ? liveStatus.events : [];
  const hypothesesList = Array.isArray(liveStatus?.hypotheses) ? liveStatus.hypotheses : [];

  return (
    <div className="h-full overflow-y-auto p-4 md:p-6">
      <div className="max-w-6xl mx-auto space-y-5">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <h2 className="text-lg font-semibold">Orchestrator · Analisi Autonoma & Audit Locale</h2>
            <p className="text-xs font-mono text-fg-muted">
              100% Offline Read-Only · Auto-Fallback senza Docker · Anti-Slopsquatting & Secret Scan
            </p>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <Badge variant={modeBadgeLabel === "Docker Isolated" ? "success" : "info"}>
              {modeBadgeLabel}
            </Badge>
            <StatusPill
              status={isActive ? "busy" : "idle"}
              label={stateName}
              pulse={isActive}
            />
            <Button size="sm" variant="secondary" onClick={() => { onRefreshStatus(); void loadPreflightAndVaults(); }}>
              <RefreshCw className="h-3.5 w-3.5" /> Aggiorna
            </Button>
          </div>
        </div>

        {/* Visible Error Banner */}
        {liveStatus?.error && (
          <div
            role="alert"
            className="p-3 rounded-md border border-danger bg-danger-bg text-danger text-xs font-mono flex items-start gap-2"
          >
            <ShieldAlert className="h-4 w-4 shrink-0 mt-0.5" />
            <div>
              <div className="font-semibold">Errore Orchestrator ({stateName})</div>
              <div>{liveStatus.error}</div>
            </div>
          </div>
        )}

        {/* Preflight */}
        <div className="flex flex-wrap gap-2 items-center text-xs font-mono">
          <Badge variant={preflight?.readyForSandbox ? "success" : "info"}>
            {preflight?.readyForSandbox ? "Docker Isolated Ready" : "Host Read-Only Safe Mode Ready"}
          </Badge>
          <Badge variant={preflight?.dockerDaemon ? "success" : "warning"}>
            Docker: {preflight?.dockerDaemon ? "ATTIVO" : "OFFLINE (Safe Mode attivo)"}
          </Badge>
          <Badge variant={preflight?.sandboxImageReady ? "success" : "warning"}>
            Sandbox Image: {preflight?.sandboxImageReady ? "PRONTA" : "ASSENTE"}
          </Badge>
          <Badge variant={preflight?.heavyReady !== false ? "success" : "warning"}>
            RAM Heavy MoE: {preflight?.heavyReady !== false ? "OK" : "LIMITATA"}
          </Badge>
          {preflight?.warnings?.map((w, idx) => (
            <span key={idx} className="text-fg-muted">
              {w}
            </span>
          ))}
        </div>

        {/* Timeline FSM */}
        <Card>
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between">
              <CardTitle className="text-sm">Timeline Stato FSM ({step} / {maxSt} step)</CardTitle>
              <span className="text-xs font-mono text-fg-muted">
                Run: {liveStatus?.run_id || liveStatus?.runId || "--"}
              </span>
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-5 gap-2">
              {FSM_PHASES.map((phase) => {
                const active = stateName === phase;
                return (
                  <div
                    key={phase}
                    className={`p-2 rounded-sm border text-center text-xs font-mono ${
                      active
                        ? "border-primary bg-info-bg text-primary font-semibold"
                        : "border-border bg-surface-muted text-fg-muted"
                    }`}
                  >
                    {phase}
                  </div>
                );
              })}
            </div>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="secondary" disabled={!isActive} onClick={() => void handleControl("pause")}>
                <Pause className="h-3.5 w-3.5" /> Pausa
              </Button>
              <Button size="sm" variant="secondary" disabled={stateName !== "PAUSED"} onClick={() => void handleControl("resume")}>
                <Play className="h-3.5 w-3.5" /> Riprendi
              </Button>
              <Button size="sm" variant="danger" disabled={!isActive} onClick={() => void handleControl("stop")}>
                <Square className="h-3.5 w-3.5" /> Arresta Run
              </Button>
              <Button size="sm" variant="secondary" onClick={() => void fetchReport()}>
                Carica Report Finale
              </Button>
              <Button size="sm" variant="danger" onClick={() => void handleControl("purge-report")}>
                <Trash2 className="h-3.5 w-3.5" /> Wipe Report RAM
              </Button>
            </div>
          </CardContent>
        </Card>

        {/* Mission Wizard + Direct code_audit */}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Wizard Missione & Scan code_audit</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {/* Human Mission Presets */}
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-fg-muted font-medium">Preset rapidi:</span>
              {MISSION_PRESETS.map((preset) => (
                <Button
                  key={preset.id}
                  size="sm"
                  variant={missionProfile === preset.profile ? "primary" : "secondary"}
                  onClick={() => {
                    setMissionProfile(preset.profile);
                    setMission(preset.mission);
                  }}
                >
                  {preset.label}
                </Button>
              ))}
            </div>

            <div className="grid grid-cols-1 md:grid-cols-5 gap-3">
              <div>
                <label htmlFor="orchRetention" className="block text-xs text-fg-muted mb-1">
                  Policy Ritenzione
                </label>
                <select
                  id="orchRetention"
                  value={retentionMode}
                  onChange={(e) => setRetentionMode(e.target.value as "ephemeral" | "persistent")}
                  className="w-full px-2.5 py-1.5 text-xs rounded-sm border border-border bg-bg text-fg"
                >
                  <option value="ephemeral">Ephemeral (Zero Disco)</option>
                  <option value="persistent">Persistent Vault</option>
                </select>
              </div>
              <div>
                <label htmlFor="orchSandbox" className="block text-xs text-fg-muted mb-1">
                  Modalità Isolamento
                </label>
                <select
                  id="orchSandbox"
                  value={sandboxMode}
                  onChange={(e) =>
                    setSandboxMode(e.target.value as "auto" | "host_readonly" | "docker")
                  }
                  className="w-full px-2.5 py-1.5 text-xs rounded-sm border border-border bg-bg text-fg"
                >
                  <option value="auto">Auto (Docker o Host Safe Mode)</option>
                  <option value="host_readonly">Host Read-Only Safe Mode</option>
                  <option value="docker">Docker Isolated (Strict)</option>
                </select>
              </div>
              <div>
                <label htmlFor="orchProfile" className="block text-xs text-fg-muted mb-1">
                  Profilo Missione
                </label>
                <select
                  id="orchProfile"
                  value={missionProfile}
                  onChange={(e) => setMissionProfile(e.target.value)}
                  className="w-full px-2.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
                >
                  <option value="code_audit">Security & Dependency Audit</option>
                  <option value="general">Private Codebase Review</option>
                  <option value="document_research">Document Analysis</option>
                  <option value="data_analysis">Structured Data Analysis</option>
                </select>
              </div>
              <div>
                <label htmlFor="orchExec" className="block text-xs text-fg-muted mb-1">
                  Motori
                </label>
                <select
                  id="orchExec"
                  value={executionMode}
                  onChange={(e) => setExecutionMode(e.target.value as "dual_tier" | "fast_only")}
                  className="w-full px-2.5 py-1.5 text-xs rounded-sm border border-border bg-bg text-fg"
                >
                  <option value="dual_tier">Dual-Tier (Heavy + Fast)</option>
                  <option value="fast_only">Fast-Only (Ollama)</option>
                </select>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <label htmlFor="orchSteps" className="block text-xs text-fg-muted mb-1">
                    Max Steps
                  </label>
                  <input
                    id="orchSteps"
                    type="number"
                    min={1}
                    max={120}
                    value={maxSteps}
                    onChange={(e) => setMaxSteps(Number(e.target.value) || 30)}
                    className="w-full px-2 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
                  />
                </div>
                <div>
                  <label htmlFor="orchEsc" className="block text-xs text-fg-muted mb-1">
                    Escalations
                  </label>
                  <input
                    id="orchEsc"
                    type="number"
                    min={0}
                    max={10}
                    value={maxEscalations}
                    onChange={(e) => setMaxEscalations(Number(e.target.value) || 3)}
                    className="w-full px-2 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
                  />
                </div>
              </div>
            </div>

            <div>
              <label htmlFor="orchTarget" className="block text-xs text-fg-muted mb-1">
                Target Directory (:ro)
              </label>
              <input
                id="orchTarget"
                value={targetPath}
                onChange={(e) => setTargetPath(e.target.value)}
                placeholder="./workspaces/orchestrator/tests/fixtures/sample_vulnerable_app"
                className="w-full px-2.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
              />
            </div>

            <div>
              <label htmlFor="orchMission" className="block text-xs text-fg-muted mb-1">
                Missione
              </label>
              <textarea
                id="orchMission"
                rows={2}
                value={mission}
                onChange={(e) => setMission(e.target.value)}
                className="w-full px-2.5 py-1.5 text-xs rounded-sm border border-border bg-bg text-fg"
              />
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Button variant="primary" size="sm" disabled={isActive} onClick={() => void handleStart()}>
                <Play className="h-3.5 w-3.5" /> Avvia Missione
              </Button>
              <Button variant="secondary" size="sm" loading={auditBusy} onClick={() => void runDeterministicCodeAudit()}>
                <ShieldCheck className="h-3.5 w-3.5" /> Esegui code_audit Deterministico
              </Button>
              {wizardMsg && <span className="text-xs font-mono text-fg-muted">{wizardMsg}</span>}
            </div>
          </CardContent>
        </Card>

        {/* Live Events Stream & Hypotheses Cards */}
        {(eventsList.length > 0 || hypothesesList.length > 0 || Boolean(liveStatus?.ledger)) && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Log Eventi Live ({eventsList.length})</CardTitle>
              </CardHeader>
              <CardContent className="max-h-64 overflow-y-auto space-y-1.5 font-mono text-[11px]">
                {eventsList.length === 0 ? (
                  <div className="text-fg-subtle">Nessun evento registrato ancora.</div>
                ) : (
                  eventsList.map((evt, idx) => {
                    if (typeof evt === "string") {
                      return (
                        <div key={idx} className="p-1.5 rounded-sm bg-surface-muted border border-border">
                          {evt}
                        </div>
                      );
                    }
                    return (
                      <div
                        key={idx}
                        className="p-1.5 rounded-sm bg-surface-muted border border-border flex items-start justify-between gap-2"
                      >
                        <div>
                          <span className="font-semibold text-primary">
                            [{String(evt.phase || "INFO")} #{String(evt.step ?? 0)}]
                          </span>{" "}
                          <span>{String(evt.message || "")}</span>
                        </div>
                        <span className="text-[10px] text-fg-subtle shrink-0">
                          {String(evt.ts || "").slice(11, 19)}
                        </span>
                      </div>
                    );
                  })
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Ipotesi & Evidenze ({hypothesesList.length})</CardTitle>
              </CardHeader>
              <CardContent className="max-h-64 overflow-y-auto space-y-2">
                {hypothesesList.length === 0 ? (
                  <div className="text-xs font-mono text-fg-subtle">
                    Nessuna ipotesi attiva (verranno generate nella fase Strategist).
                  </div>
                ) : (
                  hypothesesList.map((h, idx) => {
                    const st = String(h.status || "OPEN").toUpperCase();
                    const badgeVar =
                      st === "CONFIRMED"
                        ? "danger"
                        : st === "REFUTED"
                        ? "success"
                        : st === "ESCALATE"
                        ? "warning"
                        : "info";
                    return (
                      <div
                        key={String(h.id || idx)}
                        className="p-2.5 rounded-md border border-border bg-surface-muted space-y-1 text-xs"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-semibold">
                            {String(h.id || `H${idx + 1}`)} · {String(h.title || "")}
                          </span>
                          <Badge variant={badgeVar}>{st}</Badge>
                        </div>
                        {Boolean(h.evidence) && (
                          <div className="text-[11px] font-mono text-fg-muted whitespace-pre-wrap">
                            {String(h.evidence)}
                          </div>
                        )}
                      </div>
                    );
                  })
                )}
                {Boolean(liveStatus?.ledger) && (
                  <details className="pt-2 border-t border-border">
                    <summary className="text-xs font-mono text-fg-muted cursor-pointer">
                      Evidence Ledger compatto
                    </summary>
                    <pre className="mt-1.5 text-[11px] font-mono whitespace-pre-wrap text-fg-subtle max-h-40 overflow-y-auto">
                      {liveStatus?.ledger}
                    </pre>
                  </details>
                )}
              </CardContent>
            </Card>
          </div>
        )}

        {/* code_audit Anti-Slopsquatting & Secret Scan Panel */}
        <Card>
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base flex items-center gap-2">
                {activeCodeAudit?.passed ? (
                  <ShieldCheck className="h-4 w-4 text-success" />
                ) : (
                  <ShieldAlert className="h-4 w-4 text-warning" />
                )}
                code_audit · Anti-Slopsquatting & Secret Scan
              </CardTitle>
              {activeCodeAudit && (
                <Badge variant={activeCodeAudit.passed ? "success" : "danger"}>
                  {activeCodeAudit.passed ? "PASS" : "BLOCKED"}
                </Badge>
              )}
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            {activeCodeAudit ? (
              <>
                <div className="text-xs font-mono text-fg-muted">{activeCodeAudit.summary}</div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  <div className="p-3 rounded-md border border-border bg-surface-muted space-y-1.5">
                    <div className="flex items-center justify-between text-xs font-semibold">
                      <span>Anti-Slopsquatting</span>
                      <Badge variant={activeCodeAudit.slopsquatting.passed ? "success" : "danger"}>
                        {activeCodeAudit.slopsquatting.findings.length} rischi
                      </Badge>
                    </div>
                    <div className="text-[11px] font-mono text-fg-subtle">
                      Import verificati: {activeCodeAudit.slopsquatting.checked_imports} · Dichiarati:{" "}
                      {activeCodeAudit.slopsquatting.checked_declared}
                    </div>
                    {activeCodeAudit.slopsquatting.findings.map((f, idx) => (
                      <div key={idx} className="text-xs font-mono text-danger">
                        [{f.type}] {f.package} ({f.file}:{f.line})
                        {f.closest_known ? ` -> vicino a '${f.closest_known}'` : ""}
                      </div>
                    ))}
                  </div>

                  <div className="p-3 rounded-md border border-border bg-surface-muted space-y-1.5">
                    <div className="flex items-center justify-between text-xs font-semibold">
                      <span>Secret Scan (gitleaks / regex)</span>
                      <Badge variant={activeCodeAudit.secrets.passed ? "success" : "danger"}>
                        {activeCodeAudit.secrets.findings.length} segreti
                      </Badge>
                    </div>
                    <div className="text-[11px] font-mono text-fg-subtle">
                      File scansionati: {activeCodeAudit.secrets.files_scanned} · gitleaks:{" "}
                      {activeCodeAudit.secrets.gitleaks_used ? "attivo" : "fallback regex"}
                    </div>
                    {activeCodeAudit.secrets.findings.map((f, idx) => (
                      <div key={idx} className="text-xs font-mono text-danger">
                        [{f.rule}] {f.file}:{f.line} ({f.preview})
                      </div>
                    ))}
                  </div>
                </div>
              </>
            ) : (
              <div className="text-xs text-fg-subtle font-mono">
                Nessun risultato code_audit ancora disponibile. Avvia una missione con profilo code_audit oppure esegui lo scan deterministico diretto.
              </div>
            )}
          </CardContent>
        </Card>

        {reportMd && (
          <Card>
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between">
                <CardTitle className="text-base">Report Finale Orchestrator</CardTitle>
                {reportVerdict && <Badge variant="info">{reportVerdict}</Badge>}
              </div>
            </CardHeader>
            <CardContent>
              <div
                className="llm-prose max-h-96 overflow-y-auto"
                // devsecops:ignore-xss renderSafeMarkdownHtml escapes all HTML entities first
                dangerouslySetInnerHTML={{ __html: renderSafeMarkdownHtml(reportMd) }}
              />
            </CardContent>
          </Card>
        )}

        {vaults.length > 0 && (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">Archivi Persistent Vault ({vaults.length})</CardTitle>
            </CardHeader>
            <CardContent className="space-y-1.5">
              {vaults.map((v, idx) => {
                const rid = String(v.run_id || idx);
                return (
                  <div
                    key={rid}
                    className="flex items-center justify-between p-2 rounded-sm border border-border bg-surface-muted text-xs font-mono"
                  >
                    <span>
                      {rid} · {String(v.verdict || v.state || "--")}
                    </span>
                    <Button size="sm" variant="danger" onClick={() => void deleteVault(rid)}>
                      <Trash2 className="h-3.5 w-3.5" /> Elimina
                    </Button>
                  </div>
                );
              })}
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
};
