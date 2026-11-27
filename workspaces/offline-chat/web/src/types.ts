export type AppTab = "chat" | "engines" | "models" | "prompts" | "orchestrator" | "gomode" | "batch";
export type OperatorMode = "ghost" | "orchestrator";
export type VirtualModelAlias = "auto" | "local-fast" | "local-balanced" | "local-heavy" | string;

export interface TelemetryData {
  gpu?: {
    available?: boolean;
    name?: string | null;
    vramUsedMb?: number;
    vramFreeMb?: number;
    vramTotalMb?: number;
    vramUsedGb?: number;
    vramTotalGb?: number;
    vramFreeGb?: number;
    vramUsedPct?: number;
    tempC?: number | null;
    powerW?: number | null;
    powerLimitW?: number | null;
    utilPct?: number | null;
    memoryUsedMb?: number;
    memoryFreeMb?: number;
    memoryTotalMb?: number;
    memoryUsedGb?: number;
    memoryFreeGb?: number;
    memoryTotalGb?: number;
    memoryUsedPct?: number;
    temperatureC?: number | null;
    powerDrawW?: number | null;
    utilizationPct?: number | null;
  };
  ram?: {
    totalGb?: number;
    usedGb?: number;
    freeGb?: number;
    usedPct?: number;
    swapWarning?: boolean;
    heavyReady?: boolean;
    heavyMoeMinFreeGb?: number;
    heavyMoeSafe?: boolean;
  };
  disk?: {
    drive?: string;
    totalGb?: number;
    usedGb?: number;
    freeGb?: number;
    usedPct?: number;
  };
  backends?: {
    ollama?: {
      online?: boolean;
      url?: string;
      models?: string[];
      activeModel?: string | null;
      activeModels?: Array<{ name: string; sizeVramGb?: number }>;
    };
    heavy?: {
      online?: boolean;
      url?: string;
      models?: string[];
      activeModel?: string | null;
      name?: string | null;
      quant?: string | null;
      backend?: string | null;
    };
    heavyMoe?: {
      online?: boolean;
      url?: string;
      models?: string[];
      activeModel?: string | null;
      name?: string | null;
      quant?: string | null;
      backend?: string | null;
    };
    lmstudio?: {
      online?: boolean;
      url?: string;
      models?: string[];
      activeModel?: string | null;
    };
  };
  orchestrator?: OrchestratorTelemetrySummary;
  privacy?: Record<string, unknown>;
}

export interface OrchestratorTelemetrySummary {
  available?: boolean;
  state?: string;
  active?: boolean;
  retentionMode?: string | null;
  hasReport?: boolean;
  step?: number;
  maxSteps?: number;
  runId?: string | null;
  verdict?: string | null;
  activeEngine?: string | null;
  autoPurgeRemainingSec?: number | null;
}

export interface SystemConfig {
  ok: boolean;
  gatewayUrl: string;
  unifiedOpenAIUrl: string;
  backends: {
    ollama: string;
    heavyMoe: string;
    lmstudio: string;
  };
  models: {
    fast: string;
    balanced: string;
    heavy: string;
    embedding: string;
  };
  hardware?: Record<string, unknown>;
}

export interface PromptPreset {
  id: string;
  name: string;
  description?: string;
  recommended_temperature?: number;
  language?: string;
  version?: string;
  target_models?: string[];
  content: string;
  protected?: boolean;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  thinking?: string;
  model?: string;
  resolvedModel?: string;
  escalated?: boolean;
  stats?: string;
  timestamp: string;
  citations?: RagHit[];
}

export interface EphemeralDocMeta {
  doc_id: string;
  source_file: string;
  page_count: number;
  method: string;
}

export interface RagHit {
  doc_id?: string;
  chunk_id?: string;
  filename: string;
  source_file?: string;
  page?: number;
  page_start?: number;
  line_start?: number | null;
  line_end?: number | null;
  chunk_index?: number;
  score: number;
  text: string;
  citation: string;
  mode?: string;
  collection?: string;
}

export interface RagStatus {
  ok?: boolean;
  ghost?: {
    collections?: Record<string, { chunks?: number; documents?: number }>;
    total_chunks?: number;
  };
  vault?: {
    root?: string;
    collections?: Record<string, { chunks?: number; documents?: number }>;
    total_chunks?: number;
  };
}

export interface LedgerEntry {
  app_id: string;
  workflow_id: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  requests: number;
  retries: number;
  escalations: number;
  estimated_cost_usd: number;
  budget: {
    max_tokens: number;
    max_cost_usd: number;
    remaining_tokens: number;
    remaining_cost_usd: number;
    exceeded: boolean;
  };
  by_model?: Record<string, Record<string, number>>;
}

export interface LedgerSnapshot {
  ok?: boolean;
  retention_mode?: string;
  storage?: string;
  totals: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    requests: number;
    retries: number;
    escalations: number;
    estimated_cost_usd: number;
  };
  entries: LedgerEntry[];
  workflows?: LedgerEntry[];
}

export interface ModelInventoryItem {
  name: string;
  rel_path: string;
  relative_path?: string;
  size_bytes: number;
  size_gb: number;
  ext: string;
  format?: string;
  manifest_sha256?: string | null;
}

export interface ModelfileItem {
  name: string;
  filename: string;
  path: string;
  from?: string | null;
  num_ctx?: number | null;
  parameters?: Record<string, string>;
}

export interface ModelInventoryResponse {
  ok: boolean;
  air_gapped: boolean;
  models_dir: string;
  models_dir_source: string;
  count: number;
  models: ModelInventoryItem[];
  modelfiles: ModelfileItem[];
}

export interface ModelVerifyResponse {
  ok: boolean;
  status: "VERIFIED" | "MISMATCH" | "UNVERIFIED_NO_MANIFEST" | "MISSING";
  verified: boolean;
  path: string;
  rel_path?: string;
  size_bytes?: number;
  size_gb?: number;
  computed_sha256?: string;
  actual_sha256?: string;
  expected_sha256?: string | null;
  error?: string;
}

export interface BenchmarkHardwareSummary {
  gpu_name?: string | null;
  vram_total_gb?: number | null;
  vram_free_gb?: number | null;
  ram_total_gb?: number | null;
  ram_free_gb?: number | null;
}

export interface BenchmarkRunResponse {
  ok: boolean;
  status: "OK" | "OFFLINE" | "MODEL_NOT_FOUND" | "ERROR";
  model: string;
  requested_model?: string;
  resolved_alias?: string;
  tier?: string;
  backend?: string;
  cold_start?: boolean;
  ttft_ms?: number | null;
  thinking_tokens?: number;
  visible_tokens?: number;
  total_tokens?: number;
  duration_s?: number;
  thinking_tok_s?: number;
  visible_tok_s?: number;
  tok_per_sec?: number;
  hardware_summary?: BenchmarkHardwareSummary;
  timestamp?: string;
  code?: string;
  error?: string;
  available_models?: string[];
}

export interface HardwareWizardResponse {
  ok: boolean;
  profile_id: string;
  label: string;
  gpu_name: string;
  vram_gb: number;
  ram_gb: number;
  free_disk_gb: number;
  recommended_tier: string;
  num_ctx: number;
  heavy_moe_enabled: boolean;
  heavy_moe_ctx: number;
  heavy_moe_ram_gb: number;
  heavy_moe_min_free_ram_gb: number;
  wsl_memory_gb: number;
  local_llm_models_dir: string;
  recommended_models: {
    fast: string;
    balanced: string;
    heavy: string | null;
  };
  env_overrides: Record<string, string>;
}

export interface CodeAuditResult {
  ok: boolean;
  passed: boolean;
  blocked: boolean;
  target_path: string;
  summary: string;
  blockers: string[];
  slopsquatting: {
    passed: boolean;
    checked_imports: number;
    checked_declared: number;
    findings: Array<{
      type: string;
      package: string;
      file: string;
      line: number;
      closest_known?: string;
    }>;
  };
  secrets: {
    passed: boolean;
    files_scanned: number;
    gitleaks_used: boolean;
    findings: Array<{
      engine: string;
      rule: string;
      severity: string;
      file: string;
      line: number;
      preview: string;
    }>;
  };
}

export interface OrchestratorStatusPayload {
  available?: boolean;
  state?: string;
  phase?: string;
  active?: boolean;
  run_id?: string | null;
  runId?: string | null;
  retention_mode?: string | null;
  retentionMode?: string | null;
  mission_profile?: string | null;
  active_engine?: string | null;
  activeEngine?: string | null;
  execution_mode?: string | null;
  sandbox_mode?: "auto" | "host_readonly" | "docker" | string | null;
  resolved_sandbox_mode?: "host_readonly" | "docker" | string | null;
  step?: number;
  current_step?: number;
  max_steps?: number;
  maxSteps?: number;
  escalations_used?: number;
  max_heavy_escalations?: number;
  elapsed_seconds?: number;
  elapsed_sec?: number;
  auto_purge_remaining_sec?: number | null;
  autoPurgeRemainingSec?: number | null;
  has_report?: boolean;
  hasReport?: boolean;
  verdict?: string | { code?: string; summary?: string } | null;
  sandbox?: Record<string, unknown>;
  hypotheses?: Array<Record<string, unknown>>;
  ledger?: string;
  events?: Array<string | Record<string, unknown>>;
  budget?: Record<string, unknown>;
  error?: string | null;
  code_audit?: CodeAuditResult | null;
  surface_map?: {
    mode?: string;
    executionMode?: string;
    fallbackReason?: string;
    code_audit?: CodeAuditResult;
    codeAudit?: CodeAuditResult;
    [key: string]: unknown;
  } | null;
}

export interface TuningSliderBounds {
  min: number;
  recommended: number;
  max_safe: number;
  active?: number | null;
  steps?: number[];
  zones: {
    green_max: number;
    amber_max: number;
    hard_max: number;
  };
}

export interface TuningBoundsResponse {
  ok: boolean;
  layer: string;
  fast_ctx: TuningSliderBounds;
  heavy_ram_limit_gb: TuningSliderBounds;
  heavy_ctx: TuningSliderBounds;
  budget: {
    vram_total_gb: number;
    vram_free_gb: number;
    ram_total_gb: number;
    ram_free_gb: number;
    os_vram_reserve_gb: number;
    os_ram_reserve_gb: number;
    usable_vram_gb: number;
    usable_ram_gb: number;
    model_params_b: number;
    model_weights_gb: number;
    kv_quant: string;
    kv_gb_per_1k: number;
    heavy_kv_gb_per_1k: number;
    kv_budget_vram_gb: number;
    vk_host_pool_gb: number;
    colibri_planner_used: boolean;
  };
}

export interface TuningAdvisorKbHit {
  chunk_id: string;
  source_file: string;
  heading?: string | null;
  citation: string;
  score: number;
  text: string;
}

export interface TuningAdvisorResponse {
  ok: boolean;
  mode: "llm+kb" | "kb_only_fallback";
  question: string;
  recommended: {
    fast_ctx: number;
    heavy_ram_limit_gb: number;
    heavy_ctx: number;
  };
  raw_recommendation: {
    fast_ctx: number;
    heavy_ram_limit_gb: number;
    heavy_ctx: number;
  };
  clamped: boolean;
  explanation: string;
  warnings: string[];
  kb_hits: TuningAdvisorKbHit[];
  bounds: TuningBoundsResponse;
}

export interface HardwareFitInfo {
  emoji: "🟢" | "🟡" | "🔴" | string;
  level: "green" | "yellow" | "red" | string;
  status: string;
  label: string;
  quant: string;
  required_gb: number;
  available_vram_gb: number;
  free_ram_gb: number;
  est_tok_s: number;
}

export interface ModelCatalogItem {
  id: string;
  name: string;
  tier: "cpu" | "vram_8gb" | "vram_16gb" | "vram_24gb+" | string;
  role: "fast_tools" | "uncensored" | "heavy_moe" | "embeddings" | string;
  backend: "ollama" | "gguf";
  ollama_tag?: string;
  filename?: string;
  vram_required_gb: number;
  ram_required_gb: number;
  disk_required_gb: number;
  params_b?: number;
  expected_sha256?: string | null;
  sha256_note?: string;
  summary?: string;
  has_download_source?: boolean;
  fits_hardware: "optimal" | "tight" | "too_large";
  fits_reason: string;
  hardware_fit?: HardwareFitInfo;
  installed: boolean;
}

export interface PreparedDownloadInfo {
  ok: boolean;
  download_token: string;
  model_id: string;
  backend: "ollama" | "gguf" | string;
  source_url: string;
  filename?: string | null;
  exact_bytes: number;
  exact_gb: number;
  free_disk_bytes: number;
  free_disk_gb: number;
  sufficient_disk: boolean;
  resume_offset_bytes: number;
  license_spdx: string;
  expected_sha256?: string | null;
  hardware_fit: HardwareFitInfo;
}

export interface DiscoveredLocalModel {
  source: string;
  name: string;
  path: string;
  size_bytes: number;
  size_gb: number;
  zero_copy: boolean;
  extra_disk_gb: number;
  hardware_fit: HardwareFitInfo;
}

export interface ModelCatalogResponse {
  ok: boolean;
  hardware: {
    gpu_name?: string | null;
    vram_total_gb: number;
    ram_total_gb: number;
    disk_free_gb: number;
  };
  models: ModelCatalogItem[];
  count: number;
}

export interface ModelPinShaResponse {
  ok: boolean;
  pinned: boolean;
  verified?: boolean;
  status: string;
  path?: string;
  rel_path?: string;
  sha256?: string;
  expected_sha256?: string;
  actual_sha256?: string;
  computed_sha256?: string;
  manifest_path?: string;
  error?: string;
}

export interface ModelPullProgressEvent {
  model_id: string;
  backend?: string;
  stage: "starting" | "downloading" | "verifying" | "completed" | "error";
  status?: string;
  completed?: number;
  total?: number;
  pct?: number;
  verified?: boolean;
  path?: string;
  sha256?: string;
  code?: string;
  error?: string;
}

export interface GoModeConfigData {
  workspace_root: string;
  session_id: string;
  hitl_per_diff: boolean;
  undo_journal_enabled: boolean;
  undo_journal_mode: "disk" | "ram";
  confirm_deletions: boolean;
  shell_profile: "read_only" | "dev_allowlist" | "workspace_shell";
  confirm_shell: boolean;
  shell_timeout_sec: number;
  confirm_external_and_packages: boolean;
  level5_shield: boolean;
}

export interface GoModeBreakGlassStatus {
  unlocked: boolean;
  level5_shield_active: boolean;
  expires_at?: string | null;
  remaining_sec: number;
  session_id?: string | null;
  required_phrase: string;
  required_checkboxes: Array<{ id: string; label: string }>;
}

export interface GoModeProposalItem {
  proposal_id: string;
  session_id: string;
  action: string;
  risk_level: number;
  status: "pending_approval" | "executed" | "rejected" | "failed" | string;
  created_at: string;
  target_path: string;
  rel_path: string;
  command: string;
  timeout_sec: number;
  diff: string;
  reason: string;
  requires_break_glass: boolean;
  executed_at?: string | null;
  execution_result?: Record<string, unknown> | null;
}

export interface GoModeUndoEntry {
  step_id: string;
  session_id: string;
  proposal_id: string;
  action: string;
  target_path: string;
  existed_before: boolean;
  size_bytes: number;
  backup_file?: string | null;
  created_at: string;
}

export interface BatchWatchdogStatus {
  triggered: boolean;
  can_resume: boolean;
  reason?: string | null;
  gpu_temp_c?: number | null;
  free_ram_gb?: number | null;
  checked_at?: string | null;
  thresholds: {
    enabled: boolean;
    max_gpu_temp_c: number;
    cooldown_gpu_temp_c: number;
    min_free_ram_gb: number;
    resume_free_ram_gb: number;
    poll_interval_sec: number;
  };
}

export interface BatchJobItem {
  job_id: string;
  task_type: "translate_paragraphs" | "bulk_summarize" | "code_docstring_audit" | "jsonl_extract" | string;
  model: string;
  max_safe_ctx: number;
  chunk_chars: number;
  target_language: string;
  instruction: string;
  persist_checkpoint: boolean;
  status: "queued" | "running" | "paused" | "paused_watchdog" | "completed" | "cancelled" | "failed" | string;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  total_chunks: number;
  completed_chunks: number;
  progress_pct: number;
  eta_seconds?: number | null;
  watchdog_pause_count: number;
  watchdog_reason?: string | null;
  error?: string | null;
}

export interface IdeConfigItem {
  id: string;
  name: string;
  subtitle: string;
  filename: string;
  language: string;
  snippet: string;
}

export interface IdeConfigsResponse {
  ok: boolean;
  gatewayUrl: string;
  openaiBaseUrl: string;
  mcpUrl: string;
  tokenPlaceholder: string;
  tokenStorageHint: string;
  configs: IdeConfigItem[];
}

export interface DeveloperGatewayStatus {
  ok: boolean;
  enabled: boolean;
  pinned_port: number;
  fallback_range: string;
  active_port?: number | null;
  ephemeral_default: boolean;
  restart_required: boolean;
  runtime_discovery_file: string;
}
