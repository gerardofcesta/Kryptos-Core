// In-memory gateway session token & SSE streaming helpers (zero localStorage/disk persistence)

let inMemoryGatewayToken: string | null = null;

export async function fetchSessionToken(): Promise<string | null> {
  try {
    const res = await fetch("/api/auth/session-token", { cache: "no-store" });
    if (!res.ok) return null;
    const data = (await res.json()) as { token?: string };
    if (data && data.token) {
      inMemoryGatewayToken = String(data.token);
      return inMemoryGatewayToken;
    }
  } catch {
    // Ignore transient startup failures
  }
  return null;
}

export async function apiFetch(url: string, options: RequestInit = {}): Promise<Response> {
  if (!inMemoryGatewayToken) {
    await fetchSessionToken();
  }
  const buildHeaders = (tok: string | null): Headers => {
    const hdrs = new Headers(options.headers || undefined);
    if (tok) {
      hdrs.set("X-Kryptos-Token", tok);
    }
    return hdrs;
  };

  let res = await fetch(url, {
    ...options,
    headers: buildHeaders(inMemoryGatewayToken),
    cache: "no-store",
  });

  if (res.status === 401) {
    const refreshed = await fetchSessionToken();
    if (refreshed) {
      res = await fetch(url, {
        ...options,
        headers: buildHeaders(refreshed),
        cache: "no-store",
      });
    }
  }
  return res;
}

export async function apiJson<T>(url: string, options: RequestInit = {}): Promise<{ ok: boolean; status: number; data: T }> {
  const res = await apiFetch(url, options);
  let data: unknown = {};
  try {
    data = await res.json();
  } catch {
    data = { ok: false, error: `HTTP ${res.status}` };
  }
  return { ok: res.ok, status: res.status, data: data as T };
}

export interface SseStreamCallbacks {
  onNotice?: (notice: string) => void;
  onStage?: (stage: string, elapsedMs?: number) => void;
  onChunk?: (chunk: Record<string, unknown>) => void;
  onDone?: () => void;
  onError?: (message: string) => void;
}

/**
 * Stream OpenAI-compatible SSE responses via fetch + ReadableStream.
 * Preserves SSE stage/keep-alive comments (`: stage=...`, `: switching-tier heavy`).
 */
export async function streamGatewayChat(
  body: Record<string, unknown>,
  extraHeaders: Record<string, string>,
  callbacks: SseStreamCallbacks,
  signal?: AbortSignal
): Promise<Response> {
  const res = await apiFetch("/v1/chat/completions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      ...extraHeaders,
    },
    body: JSON.stringify({ ...body, stream: true }),
    signal,
  });

  if (!res.ok || !res.body) {
    let errText = `HTTP ${res.status}`;
    try {
      const errJson = (await res.json()) as { error?: { message?: string } | string };
      if (typeof errJson.error === "string") errText = errJson.error;
      else if (errJson.error?.message) errText = errJson.error.message;
    } catch {
      // Fallback to HTTP status
    }
    callbacks.onError?.(errText);
    return res;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  const processBlock = (rawBlock: string): boolean => {
    const lines = rawBlock.split(/\r?\n/);
    const dataLines: string[] = [];
    let parsedStage: string | null = null;
    let parsedElapsedMs: number | undefined;

    for (const line of lines) {
      if (line.startsWith(":")) {
        const comment = line.slice(1).trim();
        if (!comment) continue;
        const stageMatch = /\bstage=([a-zA-Z0-9_-]+)/.exec(comment);
        const elapsedMatch = /\belapsed_ms=(\d+)/.exec(comment);
        if (elapsedMatch) {
          parsedElapsedMs = Number(elapsedMatch[1]);
        }
        if (stageMatch) {
          parsedStage = stageMatch[1];
        } else if (!elapsedMatch) {
          callbacks.onNotice?.(comment);
        }
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }
    if (parsedStage) {
      callbacks.onStage?.(parsedStage, parsedElapsedMs);
    }
    if (dataLines.length === 0) return false;
    const payloadStr = dataLines.join("\n").trim();
    if (payloadStr === "[DONE]") {
      callbacks.onDone?.();
      return true;
    }
    try {
      const parsed = JSON.parse(payloadStr) as Record<string, unknown>;
      if (parsed.error) {
        const errMsg =
          typeof parsed.error === "string"
            ? parsed.error
            : String((parsed.error as { message?: string }).message || "Errore stream");
        callbacks.onError?.(errMsg);
      } else {
        callbacks.onChunk?.(parsed);
      }
    } catch {
      // Ignore malformed partial chunk
    }
    return false;
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sepIdx: number;
    while ((sepIdx = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, sepIdx);
      buffer = buffer.slice(sepIdx + 2);
      if (processBlock(block)) {
        return res;
      }
    }
  }
  if (buffer.trim()) {
    processBlock(buffer);
  }
  callbacks.onDone?.();
  return res;
}

/**
 * Authenticated SSE loop for `/api/system/telemetry/stream` and `/api/orchestrator/stream`.
 * Automatically pauses when `document.hidden === true` to avoid background CPU/RAM wakeups.
 */
export function startAuthenticatedSseLoop(
  url: string,
  onPayload: (payload: Record<string, unknown>) => void,
  shouldRun?: () => boolean
): () => void {
  let activeCtrl: AbortController | null = null;
  let stopped = false;

  const runStream = async (): Promise<void> => {
    while (!stopped) {
      if (document.hidden || (shouldRun && !shouldRun())) {
        await new Promise((r) => setTimeout(r, 1000));
        continue;
      }
      activeCtrl = new AbortController();
      try {
        const res = await apiFetch(url, {
          headers: { Accept: "text/event-stream" },
          signal: activeCtrl.signal,
        });
        if (!res.ok || !res.body) {
          await new Promise((r) => setTimeout(r, 2500));
          continue;
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        while (!stopped && !document.hidden) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let sepIdx: number;
          while ((sepIdx = buffer.indexOf("\n\n")) !== -1) {
            const block = buffer.slice(0, sepIdx);
            buffer = buffer.slice(sepIdx + 2);
            const dataLines: string[] = [];
            for (const rawLine of block.split(/\r?\n/)) {
              if (rawLine.startsWith("data:")) {
                dataLines.push(rawLine.slice(5).trimStart());
              }
            }
            if (dataLines.length > 0) {
              try {
                const parsed = JSON.parse(dataLines.join("\n")) as Record<string, unknown>;
                onPayload(parsed);
              } catch {
                // Ignore parse glitch
              }
            }
          }
        }
      } catch {
        // Reconnect cleanly
      } finally {
        activeCtrl = null;
      }
      if (!stopped) {
        await new Promise((r) => setTimeout(r, 1500));
      }
    }
  };

  const onVisibility = (): void => {
    if (document.hidden && activeCtrl) {
      try {
        activeCtrl.abort();
      } catch {
        // Ignore
      }
    }
  };

  document.addEventListener("visibilitychange", onVisibility);
  void runStream();

  return () => {
    stopped = true;
    document.removeEventListener("visibilitychange", onVisibility);
    if (activeCtrl) {
      try {
        activeCtrl.abort();
      } catch {
        // Ignore
      }
    }
  };
}

/**
 * Authenticated POST SSE helper for streaming JSON `data:` events (e.g. `/api/models/pull`).
 */
export async function streamSsePost(
  url: string,
  body: Record<string, unknown>,
  onEvent: (evt: Record<string, unknown>) => void,
  onError: (message: string) => void,
  signal?: AbortSignal
): Promise<void> {
  const res = await apiFetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok || !res.body) {
    let errText = `HTTP ${res.status}`;
    try {
      const errJson = (await res.json()) as { error?: string | { message?: string } };
      if (typeof errJson.error === "string") errText = errJson.error;
      else if (errJson.error?.message) errText = errJson.error.message;
    } catch {
      // Ignore JSON parse fallback
    }
    onError(errText);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  const emitBlock = (rawBlock: string) => {
    const dataLines: string[] = [];
    for (const line of rawBlock.split(/\r?\n/)) {
      if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }
    if (dataLines.length === 0) return;
    const rawStr = dataLines.join("\n").trim();
    if (!rawStr || rawStr === "[DONE]") return;
    try {
      const parsed = JSON.parse(rawStr) as Record<string, unknown>;
      onEvent(parsed);
    } catch {
      // Ignore partial chunk
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sepIdx: number;
    while ((sepIdx = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, sepIdx);
      buffer = buffer.slice(sepIdx + 2);
      emitBlock(block);
    }
  }
  if (buffer.trim()) {
    emitBlock(buffer);
  }
}
