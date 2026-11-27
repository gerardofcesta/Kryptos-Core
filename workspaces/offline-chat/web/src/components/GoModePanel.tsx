import React, { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  Check,
  FolderLock,
  Lock,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
  Terminal,
  Unlock,
  X,
} from "lucide-react";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle } from "@wdk";
import { apiJson } from "../lib/api";
import type {
  GoModeBreakGlassStatus,
  GoModeConfigData,
  GoModeProposalItem,
  GoModeUndoEntry,
} from "../types";

export const GoModePanel: React.FC = () => {
  const [config, setConfig] = useState<GoModeConfigData>({
    workspace_root: "",
    session_id: "default",
    hitl_per_diff: true,
    undo_journal_enabled: true,
    undo_journal_mode: "disk",
    confirm_deletions: true,
    shell_profile: "dev_allowlist",
    confirm_shell: true,
    shell_timeout_sec: 30,
    confirm_external_and_packages: true,
    level5_shield: true,
  });
  const [breakGlass, setBreakGlass] = useState<GoModeBreakGlassStatus | null>(null);
  const [breakGlassToken, setBreakGlassToken] = useState<string | null>(null);
  const [pending, setPending] = useState<GoModeProposalItem[]>([]);
  const [history, setHistory] = useState<GoModeProposalItem[]>([]);
  const [undoEntries, setUndoEntries] = useState<GoModeUndoEntry[]>([]);
  const [auditLog, setAuditLog] = useState<Array<Record<string, unknown>>>([]);
  const [approvedFolders, setApprovedFolders] = useState<string[]>([]);
  const [statusMsg, setStatusMsg] = useState<string>("");

  // Action proposer form state
  const [proposeAction, setProposeAction] = useState<string>("write_file");
  const [proposePath, setProposePath] = useState<string>("notes/demo.txt");
  const [proposeContent, setProposeContent] = useState<string>("Hello from Go Mode!\n");
  const [proposeCommand, setProposeCommand] = useState<string>("pytest -q");

  // Break-Glass modal state
  const [showBreakGlassModal, setShowBreakGlassModal] = useState<boolean>(false);
  const [bgChecks, setBgChecks] = useState<Record<string, boolean>>({});
  const [bgPhraseInput, setBgPhraseInput] = useState<string>("");
  const [bgError, setBgError] = useState<string>("");

  const refreshAll = useCallback(async () => {
    const [cfgRes, sessRes] = await Promise.all([
      apiJson<{
        ok: boolean;
        config?: GoModeConfigData;
        approved_folders?: string[];
        break_glass?: GoModeBreakGlassStatus;
      }>("/api/go-mode/config"),
      apiJson<{
        ok: boolean;
        pending_proposals?: GoModeProposalItem[];
        history?: GoModeProposalItem[];
        undo_journal?: Record<string, GoModeUndoEntry[]>;
        approved_folders?: string[];
        break_glass?: GoModeBreakGlassStatus;
        audit_log?: Array<Record<string, unknown>>;
      }>("/api/go-mode/sessions"),
    ]);

    let activeSid = "default";
    if (cfgRes.ok && cfgRes.data.config) {
      setConfig(cfgRes.data.config);
      activeSid = cfgRes.data.config.session_id || "default";
      if (cfgRes.data.break_glass) {
        setBreakGlass(cfgRes.data.break_glass);
        if (!cfgRes.data.break_glass.unlocked) setBreakGlassToken(null);
      }
      if (Array.isArray(cfgRes.data.approved_folders)) {
        setApprovedFolders(cfgRes.data.approved_folders);
      }
    }
    if (sessRes.ok) {
      setPending(sessRes.data.pending_proposals || []);
      setHistory(sessRes.data.history || []);
      const journalMap = sessRes.data.undo_journal || {};
      setUndoEntries(journalMap[activeSid] || []);
      setAuditLog(sessRes.data.audit_log || []);
      if (sessRes.data.break_glass) {
        setBreakGlass(sessRes.data.break_glass);
      }
    }
  }, []);

  useEffect(() => {
    void refreshAll();
  }, [refreshAll]);

  const saveConfig = async (nextCfg: Partial<GoModeConfigData>) => {
    const merged = { ...config, ...nextCfg };
    setConfig(merged);
    const res = await apiJson<{ ok: boolean; error?: string; config?: GoModeConfigData }>(
      "/api/go-mode/config",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(merged),
      }
    );
    if (res.ok && res.data.config) {
      setConfig(res.data.config);
      setStatusMsg("Configurazione Go Mode salvata.");
      void refreshAll();
    } else {
      setStatusMsg(res.data.error || `Errore salvataggio (${res.status})`);
    }
  };

  const handlePropose = async () => {
    const body: Record<string, unknown> = {
      action: proposeAction,
      session_id: config.session_id,
    };
    if (proposeAction === "run_command" || proposeAction === "install_package") {
      body.command = proposeCommand;
    } else {
      body.path = proposePath;
      if (proposeAction === "write_file" || proposeAction === "create_file" || proposeAction === "overwrite_file") {
        body.content = proposeContent;
      }
    }
    if (breakGlassToken) {
      body.break_glass_token = breakGlassToken;
    }

    const res = await apiJson<{
      ok: boolean;
      executed?: boolean;
      requires_approval?: boolean;
      blocked_by?: string;
      requires_break_glass?: boolean;
      error?: string;
    }>("/api/go-mode/propose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (res.ok) {
      setStatusMsg(
        res.data.executed
          ? "Azione eseguita automaticamente (auto-approve attivo)."
          : "Proposta aggiunta alla coda HITL in attesa di approvazione."
      );
      void refreshAll();
    } else {
      setStatusMsg(res.data.error || `Azione bloccata (${res.status})`);
      void refreshAll();
    }
  };

  const handleDecision = async (
    proposalId: string,
    decision: "approve_once" | "approve_folder" | "reject"
  ) => {
    if (decision === "reject") {
      const res = await apiJson<{ ok: boolean; error?: string }>("/api/go-mode/reject", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proposal_id: proposalId }),
      });
      setStatusMsg(res.ok ? "Proposta rifiutata." : res.data.error || "Errore reject");
      void refreshAll();
      return;
    }

    const res = await apiJson<{ ok: boolean; error?: string }>("/api/go-mode/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        proposal_id: proposalId,
        approve_mode: decision === "approve_folder" ? "folder" : "once",
        break_glass_token: breakGlassToken,
      }),
    });
    setStatusMsg(
      res.ok ? "Azione eseguita e salvata nello Undo Journal." : res.data.error || "Errore esecuzione"
    );
    void refreshAll();
  };

  const handleRollback = async (mode: "last" | "all") => {
    const res = await apiJson<{
      ok: boolean;
      restored_count?: number;
      error?: string;
    }>("/api/go-mode/rollback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: config.session_id, mode }),
    });
    if (res.ok) {
      setStatusMsg(
        `Rollback (${mode}) completato: ${res.data.restored_count ?? 0} step ripristinati.`
      );
      void refreshAll();
    } else {
      setStatusMsg(res.data.error || "Errore rollback");
    }
  };

  const handleUnlockBreakGlass = async () => {
    setBgError("");
    const res = await apiJson<{
      ok: boolean;
      token?: string;
      error?: string;
    }>("/api/go-mode/break-glass", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: config.session_id,
        checkboxes: bgChecks,
        confirmation_phrase: bgPhraseInput,
        ttl_seconds: 600,
      }),
    });
    if (res.ok && res.data.token) {
      setBreakGlassToken(res.data.token);
      setShowBreakGlassModal(false);
      setBgChecks({});
      setBgPhraseInput("");
      setStatusMsg("ATTENZIONE: Break-Glass Livello 5 sbloccato per sessione temporanea.");
      void refreshAll();
    } else {
      setBgError(res.data.error || `Unlock rifiutato (${res.status})`);
    }
  };

  const handleRelockBreakGlass = async () => {
    await apiJson("/api/go-mode/break-glass/lock", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    setBreakGlassToken(null);
    setStatusMsg("Level 5 Hard System Shield riattivato.");
    void refreshAll();
  };

  const requiredChecks = breakGlass?.required_checkboxes || [];
  const requiredPhrase =
    breakGlass?.required_phrase ||
    "I-ACKNOWLEDGE-CRITICAL-SYSTEM-RISK-AND-UNLOCK-PROTECTED-BOUNDARY";
  const allChecksChecked =
    requiredChecks.length >= 3 && requiredChecks.every((c) => Boolean(bgChecks[c.id]));

  return (
    <div className="h-full overflow-y-auto p-4 md:p-6">
      <div className="max-w-6xl mx-auto space-y-5">
        {/* Persistent Red Warning Banner when Break-Glass is unlocked */}
        {breakGlass?.unlocked && (
          <div
            role="alert"
            className="p-3 rounded-md border-2 border-danger bg-danger-bg text-danger flex items-center justify-between gap-3 flex-wrap"
          >
            <div className="flex items-center gap-2 text-xs font-mono font-semibold">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              <span>
                ATTENZIONE CRITICA: Level 5 Break-Glass SBLOCCATO (scade tra {breakGlass.remaining_sec}s).
                Nessun rollback automatico su file OS/Registro.
              </span>
            </div>
            <Button size="sm" variant="danger" onClick={() => void handleRelockBreakGlass()}>
              <Lock className="h-3.5 w-3.5" /> Riattiva Shield Ora
            </Button>
          </div>
        )}

        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <h2 className="text-lg font-semibold">
              Go Mode · Agente Locale con 5 Livelli di Guardrail
            </h2>
            <p className="text-xs font-mono text-fg-muted">
              Livelli 1-4 configurabili · Unified Diff HITL · Rollback Atomico · Livello 5 Hard OS/Kernel Shield
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Badge variant={breakGlass?.unlocked ? "danger" : "success"}>
              {breakGlass?.unlocked ? "LEVEL 5: UNLOCKED" : "LEVEL 5 SHIELD: ACTIVE"}
            </Badge>
            <Badge variant="info">Shell: {config.shell_profile}</Badge>
          </div>
        </div>

        {/* Workspace Root Picker */}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm flex items-center gap-2">
              <FolderLock className="h-4 w-4" /> Workspace Root & Guardrail Livelli 1-4
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex gap-2 flex-wrap">
              <input
                value={config.workspace_root}
                onChange={(e) => setConfig({ ...config, workspace_root: e.target.value })}
                placeholder="./workspaces/orchestrator/tests/fixtures/sample_vulnerable_app"
                className="flex-1 min-w-[260px] px-2.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
              />
              <Button size="sm" variant="primary" onClick={() => void saveConfig({})}>
                Imposta Workspace & Salva
              </Button>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
              {/* Level 1 */}
              <div className="p-3 rounded-md border border-border bg-surface-muted space-y-2">
                <div className="font-semibold flex items-center justify-between">
                  <span>Livello 1 · Workspace Sandbox & Scritture</span>
                  <Badge variant={config.hitl_per_diff ? "success" : "warning"}>
                    {config.hitl_per_diff ? "HITL Diff" : "Auto-Approve"}
                  </Badge>
                </div>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={config.hitl_per_diff}
                    onChange={(e) => void saveConfig({ hitl_per_diff: e.target.checked })}
                  />
                  <span>Richiedi conferma HITL per ogni Unified Diff</span>
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={config.undo_journal_enabled}
                    onChange={(e) => void saveConfig({ undo_journal_enabled: e.target.checked })}
                  />
                  <span>Atomic Rollback Journal attivo</span>
                </label>
                <div className="flex items-center gap-2">
                  <span className="text-fg-muted">Storage Journal:</span>
                  <select
                    value={config.undo_journal_mode}
                    onChange={(e) =>
                      void saveConfig({ undo_journal_mode: e.target.value as "disk" | "ram" })
                    }
                    className="px-2 py-1 rounded-sm border border-border bg-bg text-fg font-mono"
                  >
                    <option value="disk">Disk (.localllm_undo/)</option>
                    <option value="ram">RAM Ephemeral</option>
                  </select>
                </div>
                {approvedFolders.length > 0 && (
                  <div className="text-[11px] font-mono text-fg-subtle">
                    Cartelle auto-approvate: {approvedFolders.length}
                  </div>
                )}
              </div>

              {/* Level 2 */}
              <div className="p-3 rounded-md border border-border bg-surface-muted space-y-2">
                <div className="font-semibold flex items-center justify-between">
                  <span>Livello 2 · Eliminazioni & Sovrascritture</span>
                  <Badge variant={config.confirm_deletions ? "success" : "warning"}>
                    {config.confirm_deletions ? "Protetto" : "Auto"}
                  </Badge>
                </div>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={config.confirm_deletions}
                    onChange={(e) => void saveConfig({ confirm_deletions: e.target.checked })}
                  />
                  <span>Conferma esplicita separata per delete_file & overwrite (con backup pre-delete)</span>
                </label>
              </div>

              {/* Level 3 */}
              <div className="p-3 rounded-md border border-border bg-surface-muted space-y-2">
                <div className="font-semibold flex items-center justify-between">
                  <span>Livello 3 · Profilo Shell & Comandi</span>
                  <Badge variant="info">{config.shell_profile}</Badge>
                </div>
                <div className="flex items-center gap-2 flex-wrap">
                  <select
                    value={config.shell_profile}
                    onChange={(e) =>
                      void saveConfig({
                        shell_profile: e.target.value as "read_only" | "dev_allowlist" | "workspace_shell",
                      })
                    }
                    className="px-2 py-1 rounded-sm border border-border bg-bg text-fg font-mono"
                  >
                    <option value="read_only">read_only (Shell disabilitata)</option>
                    <option value="dev_allowlist">dev_allowlist (pytest/ruff/git/python/node/npm/uv)</option>
                    <option value="workspace_shell">workspace_shell (Shell generale in workspace)</option>
                  </select>
                  <label className="flex items-center gap-1 font-mono">
                    Timeout(s):
                    <input
                      type="number"
                      min={1}
                      max={120}
                      value={config.shell_timeout_sec}
                      onChange={(e) =>
                        void saveConfig({ shell_timeout_sec: Number(e.target.value) || 30 })
                      }
                      className="w-16 px-1.5 py-0.5 rounded-sm border border-border bg-bg text-fg"
                    />
                  </label>
                </div>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={config.confirm_shell}
                    onChange={(e) => void saveConfig({ confirm_shell: e.target.checked })}
                  />
                  <span>Anteprima e conferma HITL prima di eseguire comandi</span>
                </label>
              </div>

              {/* Level 4 */}
              <div className="p-3 rounded-md border border-border bg-surface-muted space-y-2">
                <div className="font-semibold flex items-center justify-between">
                  <span>Livello 4 · Path Esterni & Pacchetti</span>
                  <Badge variant={config.confirm_external_and_packages ? "success" : "warning"}>
                    {config.confirm_external_and_packages ? "Consenso Attivo" : "Off"}
                  </Badge>
                </div>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={config.confirm_external_and_packages}
                    onChange={(e) =>
                      void saveConfig({ confirm_external_and_packages: e.target.checked })
                    }
                  />
                  <span>Consenso esplicito prima di path fuori workspace o installazioni (pip/npm/uv)</span>
                </label>
              </div>
            </div>

            {/* Level 5 Hard System Shield */}
            <div className="p-3 rounded-md border-2 border-danger/60 bg-surface flex items-center justify-between gap-3 flex-wrap">
              <div className="space-y-0.5">
                <div className="text-xs font-semibold flex items-center gap-2 text-danger">
                  <ShieldAlert className="h-4 w-4" />
                  Livello 5 · HARD SYSTEM PROTECTION SHIELD (Enforced Server-Side)
                </div>
                <div className="text-[11px] font-mono text-fg-muted">
                  Blocca C:\Windows, System32, Program Files, Boot/EFI, HKLM, format/diskpart/bcdedit/reg/sc/shutdown e vault credenziali (.ssh, .aws, browser). Non disattivabile da toggle.
                </div>
              </div>
              {breakGlass?.unlocked ? (
                <Button size="sm" variant="danger" onClick={() => void handleRelockBreakGlass()}>
                  <Lock className="h-3.5 w-3.5" /> Riattiva Shield
                </Button>
              ) : (
                <Button
                  size="sm"
                  variant="danger"
                  onClick={() => {
                    setBgError("");
                    setShowBreakGlassModal(true);
                  }}
                >
                  <Unlock className="h-3.5 w-3.5" /> Break-Glass Override...
                </Button>
              )}
            </div>

            {statusMsg && <div className="text-xs font-mono text-primary">{statusMsg}</div>}
          </CardContent>
        </Card>

        {/* Action Proposer + HITL Unified Diff Queue */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm flex items-center gap-2">
                <Terminal className="h-4 w-4" /> Proponi Azione Workspace (Test / Uso Diretto)
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-xs">
              <div className="grid grid-cols-2 gap-2">
                <select
                  value={proposeAction}
                  onChange={(e) => setProposeAction(e.target.value)}
                  className="px-2 py-1.5 rounded-sm border border-border bg-bg text-fg font-mono"
                >
                  <option value="write_file">write_file (L1)</option>
                  <option value="create_file">create_file (L1)</option>
                  <option value="overwrite_file">overwrite_file (L2)</option>
                  <option value="delete_file">delete_file (L2)</option>
                  <option value="run_command">run_command (L3)</option>
                  <option value="install_package">install_package (L4)</option>
                  <option value="read_external_path">read_external_path (L4)</option>
                </select>
                {proposeAction === "run_command" || proposeAction === "install_package" ? (
                  <input
                    value={proposeCommand}
                    onChange={(e) => setProposeCommand(e.target.value)}
                    placeholder="es. pytest -q"
                    className="px-2 py-1.5 rounded-sm border border-border bg-bg text-fg font-mono"
                  />
                ) : (
                  <input
                    value={proposePath}
                    onChange={(e) => setProposePath(e.target.value)}
                    placeholder="es. notes/demo.txt"
                    className="px-2 py-1.5 rounded-sm border border-border bg-bg text-fg font-mono"
                  />
                )}
              </div>
              {(proposeAction === "write_file" ||
                proposeAction === "create_file" ||
                proposeAction === "overwrite_file") && (
                <textarea
                  rows={4}
                  value={proposeContent}
                  onChange={(e) => setProposeContent(e.target.value)}
                  className="w-full px-2.5 py-1.5 rounded-sm border border-border bg-bg text-fg font-mono"
                />
              )}
              <Button size="sm" variant="primary" onClick={() => void handlePropose()}>
                Invia Proposta ai Guardrail
              </Button>
            </CardContent>
          </Card>

          {/* Atomic Rollback Journal */}
          <Card>
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between flex-wrap gap-2">
                <CardTitle className="text-sm flex items-center gap-2">
                  <RotateCcw className="h-4 w-4" /> Atomic Rollback Journal ({undoEntries.length} step)
                </CardTitle>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={undoEntries.length === 0}
                    onClick={() => void handleRollback("last")}
                  >
                    Undo Last Step
                  </Button>
                  <Button
                    size="sm"
                    variant="danger"
                    disabled={undoEntries.length === 0}
                    onClick={() => void handleRollback("all")}
                  >
                    Rollback All
                  </Button>
                </div>
              </div>
            </CardHeader>
            <CardContent className="max-h-48 overflow-y-auto space-y-1.5 text-[11px] font-mono">
              {undoEntries.length === 0 ? (
                <div className="text-fg-subtle">
                  Nessuna modifica registrata nello Undo Journal per questa sessione.
                </div>
              ) : (
                [...undoEntries].reverse().map((u) => (
                  <div
                    key={u.step_id}
                    className="p-2 rounded-sm border border-border bg-surface-muted flex items-center justify-between gap-2"
                  >
                    <span className="truncate">
                      [{u.action}] {u.target_path} ({u.existed_before ? "restore" : "unlink"})
                    </span>
                    <span className="text-fg-subtle shrink-0">{u.step_id}</span>
                  </div>
                ))
              )}
            </CardContent>
          </Card>
        </div>

        {/* HITL Pending Approval Queue */}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm flex items-center gap-2">
              <ShieldCheck className="h-4 w-4" /> Coda Approvazione HITL ({pending.length} in attesa)
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {pending.length === 0 ? (
              <div className="text-xs font-mono text-fg-subtle">
                Nessuna proposta in attesa di approvazione.
              </div>
            ) : (
              pending.map((p) => (
                <div
                  key={p.proposal_id}
                  className="p-3 rounded-md border border-border bg-surface-muted space-y-2"
                >
                  <div className="flex items-center justify-between gap-2 flex-wrap">
                    <div className="flex items-center gap-2 text-xs font-mono">
                      <Badge variant={p.risk_level >= 3 ? "danger" : p.risk_level === 2 ? "warning" : "info"}>
                        L{p.risk_level}
                      </Badge>
                      <span className="font-semibold">{p.action}</span>
                      <span>{p.rel_path || p.target_path || p.command}</span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <Button
                        size="sm"
                        variant="primary"
                        onClick={() => void handleDecision(p.proposal_id, "approve_once")}
                      >
                        <Check className="h-3.5 w-3.5" /> Approve Once
                      </Button>
                      {p.risk_level === 1 && (
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => void handleDecision(p.proposal_id, "approve_folder")}
                        >
                          Approve Folder
                        </Button>
                      )}
                      <Button
                        size="sm"
                        variant="danger"
                        onClick={() => void handleDecision(p.proposal_id, "reject")}
                      >
                        <X className="h-3.5 w-3.5" /> Reject
                      </Button>
                    </div>
                  </div>
                  {p.diff && (
                    <pre className="p-2 rounded-sm bg-bg border border-border text-[11px] font-mono overflow-x-auto max-h-48 whitespace-pre-wrap">
                      {p.diff}
                    </pre>
                  )}
                  {p.command && (
                    <pre className="p-2 rounded-sm bg-bg border border-border text-[11px] font-mono">
                      $ {p.command} (timeout={p.timeout_sec}s)
                    </pre>
                  )}
                </div>
              ))
            )}
          </CardContent>
        </Card>

        {/* Execution History & Security Audit Log */}
        {(history.length > 0 || auditLog.length > 0) && (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">Audit Log & Storico Esecuzioni</CardTitle>
            </CardHeader>
            <CardContent className="max-h-48 overflow-y-auto space-y-1 text-[11px] font-mono">
              {auditLog.map((entry, idx) => (
                <div key={idx} className="text-fg-muted">
                  [{String(entry.ts || "").slice(11, 19)}] <strong>{String(entry.event || "")}</strong>:{" "}
                  {String(entry.detail || "")}
                </div>
              ))}
            </CardContent>
          </Card>
        )}
      </div>

      {/* High-Contrast Level 5 Break-Glass Modal */}
      {showBreakGlassModal && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-4"
        >
          <div className="max-w-lg w-full rounded-md border-2 border-danger bg-surface p-5 space-y-4 shadow-xl">
            <div className="flex items-center justify-between gap-2 text-danger">
              <div className="flex items-center gap-2 font-semibold text-sm">
                <ShieldAlert className="h-5 w-5" />
                Zona Critica di Sistema — Override Livello 5 (Break-Glass)
              </div>
              <button
                type="button"
                aria-label="Chiudi modale"
                onClick={() => setShowBreakGlassModal(false)}
                className="text-fg-muted hover:text-fg"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="space-y-2 text-xs">
              <p className="text-fg-muted">
                Per sbloccare temporaneamente la protezione Livello 5 devi completare tutti i passaggi:
              </p>
              {requiredChecks.map((chk) => (
                <label
                  key={chk.id}
                  className="flex items-start gap-2 p-2 rounded-sm border border-border bg-surface-muted cursor-pointer"
                >
                  <input
                    type="checkbox"
                    checked={Boolean(bgChecks[chk.id])}
                    onChange={(e) =>
                      setBgChecks((prev) => ({ ...prev, [chk.id]: e.target.checked }))
                    }
                    className="mt-0.5"
                  />
                  <span>{chk.label}</span>
                </label>
              ))}
            </div>

            <div className="space-y-1.5 text-xs">
              <div className="text-fg-muted">
                2. Ricopia manualmente (copia-incolla disabilitato) la seguente frase esatta:
              </div>
              <div className="p-2 rounded-sm bg-bg border border-danger/50 font-mono text-[11px] select-none text-danger font-semibold">
                {requiredPhrase}
              </div>
              <input
                type="text"
                value={bgPhraseInput}
                onChange={(e) => setBgPhraseInput(e.target.value)}
                onPaste={(e) => e.preventDefault()}
                onDrop={(e) => e.preventDefault()}
                autoComplete="off"
                spellCheck={false}
                placeholder="Digita manualmente la frase di conferma..."
                className="w-full px-2.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
              />
            </div>

            {bgError && <div className="text-xs font-mono text-danger">{bgError}</div>}

            <div className="flex items-center justify-end gap-2">
              <Button
                size="sm"
                variant="secondary"
                onClick={() => setShowBreakGlassModal(false)}
              >
                Annulla
              </Button>
              <Button
                size="sm"
                variant="danger"
                disabled={!allChecksChecked || bgPhraseInput.trim() !== requiredPhrase}
                onClick={() => void handleUnlockBreakGlass()}
              >
                <Unlock className="h-3.5 w-3.5" /> Conferma Break-Glass (10 min)
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
