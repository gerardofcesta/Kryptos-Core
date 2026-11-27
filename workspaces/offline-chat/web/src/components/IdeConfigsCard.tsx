import React, { useCallback, useEffect, useState } from "react";
import { Check, Copy, Plug, RefreshCw, X } from "lucide-react";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle } from "@wdk";
import { apiJson } from "../lib/api";
import type { DeveloperGatewayStatus, IdeConfigsResponse } from "../types";

interface IdeConfigsBodyProps {
  compact?: boolean;
}

export const IdeConfigsBody: React.FC<IdeConfigsBodyProps> = ({ compact = false }) => {
  const [data, setData] = useState<IdeConfigsResponse | null>(null);
  const [devGw, setDevGw] = useState<DeveloperGatewayStatus | null>(null);
  const [selectedId, setSelectedId] = useState<string>("continue");
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [toggling, setToggling] = useState<boolean>(false);

  const loadConfigs = useCallback(async () => {
    setLoading(true);
    try {
      const [cfgRes, gwRes] = await Promise.all([
        apiJson<IdeConfigsResponse>("/api/system/ide-configs"),
        apiJson<DeveloperGatewayStatus>("/api/system/developer-gateway"),
      ]);
      if (cfgRes.ok && resHasOk(cfgRes.data)) {
        setData(cfgRes.data);
        if (cfgRes.data.configs.length > 0) {
          setSelectedId((prev) =>
            cfgRes.data.configs.some((c) => c.id === prev) ? prev : cfgRes.data.configs[0].id
          );
        }
      }
      if (gwRes.ok && gwRes.data?.ok) {
        setDevGw(gwRes.data);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadConfigs();
  }, [loadConfigs]);

  const toggleDeveloperGateway = async (enabled: boolean) => {
    setToggling(true);
    try {
      const res = await apiJson<DeveloperGatewayStatus>("/api/system/developer-gateway", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      if (res.ok && res.data?.ok) {
        setDevGw(res.data);
      }
    } finally {
      setToggling(false);
    }
  };

  const activeConfig = data?.configs.find((c) => c.id === selectedId) || data?.configs[0] || null;

  const copySnippet = async (id: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedId(id);
      setTimeout(() => setCopiedId((prev) => (prev === id ? null : prev)), 1800);
    } catch {
      // Ignore clipboard unavailability in headless environments
    }
  };

  return (
    <div className="space-y-3">
      <div className="p-2.5 rounded-md border border-border bg-surface-muted space-y-2 text-xs">
        <label className="flex items-start gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={Boolean(devGw?.enabled)}
            disabled={toggling}
            onChange={(e) => void toggleDeveloperGateway(e.target.checked)}
            className="mt-0.5"
          />
          <div>
            <div className="font-semibold text-fg">
              Esponi Gateway Locale per IDE esterni &amp; MCP (porta fissa)
            </div>
            <div className="text-[11px] text-fg-muted">
              Default desktop usa porta effimera invisibile con auto-discovery locale (runtime.json).
              Attiva per fissare la porta standard al prossimo riavvio.
            </div>
          </div>
        </label>
        {devGw?.restart_required && (
          <div className="text-[11px] font-mono text-warning">
            Riavvia l&apos;applicazione per applicare la nuova modalità di binding.
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-fg-muted">
        <div>
          Endpoint OpenAI:{" "}
          <code className="font-mono text-fg">{data?.openaiBaseUrl || "--"}</code> · MCP:{" "}
          <code className="font-mono text-fg">{data?.mcpUrl || "--"}</code>
        </div>
        <Button size="sm" variant="ghost" onClick={() => void loadConfigs()} disabled={loading}>
          <RefreshCw className="h-3.5 w-3.5" /> Aggiorna
        </Button>
      </div>

      <div className="p-2 rounded-md border border-border bg-surface-muted text-[11px] text-fg-muted">
        Sicurezza: gli snippet usano il segnaposto{" "}
        <code className="font-mono text-fg">{data?.tokenPlaceholder || "<YOUR_GATEWAY_TOKEN>"}</code> (mai il token reale in chiaro).
        Recupera il tuo token locale da{" "}
        <code className="font-mono text-fg">{data?.tokenStorageHint || "gateway.token"}</code>.
      </div>

      {data?.configs && data.configs.length > 0 && (
        <>
          <div className="flex flex-wrap gap-1.5">
            {data.configs.map((cfg) => (
              <Button
                key={cfg.id}
                size="sm"
                variant={cfg.id === activeConfig?.id ? "primary" : "secondary"}
                onClick={() => setSelectedId(cfg.id)}
              >
                {cfg.name.split(" ")[0]}
              </Button>
            ))}
          </div>

          {activeConfig && (
            <div className="rounded-md border border-border bg-bg overflow-hidden">
              <div className="flex items-center justify-between gap-2 px-3 py-2 border-b border-border bg-surface-muted">
                <div className="min-w-0">
                  <div className="text-xs font-semibold text-fg flex items-center gap-2">
                    <span>{activeConfig.name}</span>
                    <Badge variant="neutral">{activeConfig.filename}</Badge>
                  </div>
                  {!compact && (
                    <div className="text-[11px] text-fg-subtle">{activeConfig.subtitle}</div>
                  )}
                </div>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => void copySnippet(activeConfig.id, activeConfig.snippet)}
                >
                  {copiedId === activeConfig.id ? (
                    <>
                      <Check className="h-3.5 w-3.5 text-success" /> Copiato
                    </>
                  ) : (
                    <>
                      <Copy className="h-3.5 w-3.5" /> Copia Snippet
                    </>
                  )}
                </Button>
              </div>
              <pre className="p-3 text-[11px] font-mono text-fg overflow-x-auto max-h-72">
                <code>{activeConfig.snippet}</code>
              </pre>
            </div>
          )}
        </>
      )}
    </div>
  );
};

function resHasOk(val: IdeConfigsResponse | undefined): val is IdeConfigsResponse {
  return Boolean(val && val.ok);
}

export const IdeConfigsCard: React.FC = () => (
  <Card>
    <CardHeader className="pb-2">
      <CardTitle className="text-sm flex items-center gap-2">
        <Plug className="h-4 w-4 text-primary" /> Impostazioni Sviluppatore &amp; Integrazione IDE
      </CardTitle>
    </CardHeader>
    <CardContent>
      <IdeConfigsBody />
    </CardContent>
  </Card>
);

interface IdeConfigsDrawerProps {
  open: boolean;
  onClose: () => void;
}

export const IdeConfigsDrawer: React.FC<IdeConfigsDrawerProps> = ({ open, onClose }) => {
  if (!open) return null;
  return (
    <aside className="w-[26rem] shrink-0 border-l border-border bg-surface p-4 overflow-y-auto flex flex-col gap-3">
      <div className="flex items-center justify-between border-b border-border pb-2">
        <div className="flex items-center gap-2 text-sm font-semibold text-fg">
          <Plug className="h-4 w-4 text-primary" /> Impostazioni Sviluppatore &amp; IDE
        </div>
        <Button size="sm" variant="ghost" onClick={onClose} aria-label="Chiudi Impostazioni Sviluppatore">
          <X className="h-4 w-4" />
        </Button>
      </div>
      <IdeConfigsBody compact />
    </aside>
  );
};
