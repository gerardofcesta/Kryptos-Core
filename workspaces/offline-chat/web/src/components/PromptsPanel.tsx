import React, { useState } from "react";
import { Plus, Save, Trash2 } from "lucide-react";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle } from "@wdk";
import { apiJson } from "../lib/api";
import type { PromptPreset } from "../types";

interface PromptsPanelProps {
  presets: PromptPreset[];
  onReloadPresets: () => Promise<void>;
  onUseInChat: (preset: PromptPreset) => void;
}

export const PromptsPanel: React.FC<PromptsPanelProps> = ({
  presets,
  onReloadPresets,
  onUseInChat,
}) => {
  const [editingId, setEditingId] = useState<string>("");
  const [name, setName] = useState<string>("");
  const [description, setDescription] = useState<string>("");
  const [temperature, setTemperature] = useState<number>(0.7);
  const [language, setLanguage] = useState<string>("it");
  const [version, setVersion] = useState<string>("1.0");
  const [content, setContent] = useState<string>("");
  const [isProtected, setIsProtected] = useState<boolean>(false);
  const [statusMsg, setStatusMsg] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);

  const selectPreset = (p: PromptPreset) => {
    setEditingId(p.id);
    setName(p.name || p.id);
    setDescription(p.description || "");
    setTemperature(p.recommended_temperature ?? 0.7);
    setLanguage(p.language || "it");
    setVersion(p.version || "1.0");
    setContent(p.content || "");
    setIsProtected(Boolean(p.protected));
    setStatusMsg("");
  };

  const resetNew = () => {
    setEditingId("");
    setName("");
    setDescription("");
    setTemperature(0.7);
    setLanguage("it");
    setVersion("1.0");
    setContent("");
    setIsProtected(false);
    setStatusMsg("Nuovo preset pronto per la modifica.");
  };

  const handleSave = async () => {
    setBusy(true);
    try {
      const res = await apiJson<{ ok: boolean; error?: string }>("/api/prompts/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: editingId.trim(),
          name: name.trim() || editingId.trim(),
          description: description.trim(),
          recommended_temperature: temperature,
          language: language.trim() || "it",
          version: version.trim() || "1.0",
          content,
        }),
      });
      if (res.ok && res.data.ok !== false) {
        setStatusMsg(`Preset '${editingId}' salvato.`);
        await onReloadPresets();
      } else {
        setStatusMsg(res.data.error || `Errore salvataggio (${res.status})`);
      }
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    if (!editingId.trim()) return;
    setBusy(true);
    try {
      const res = await apiJson<{ ok: boolean; error?: string }>(
        `/api/prompts/${encodeURIComponent(editingId.trim())}`,
        { method: "DELETE" }
      );
      if (res.ok && res.data.ok !== false) {
        setStatusMsg(`Preset '${editingId}' eliminato.`);
        resetNew();
        await onReloadPresets();
      } else {
        setStatusMsg(res.data.error || `Errore eliminazione (${res.status})`);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="h-full overflow-y-auto p-4 md:p-6">
      <div className="max-w-6xl mx-auto space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold">Gestione Libreria Prompt (prompts/*.md)</h2>
            <p className="text-xs font-mono text-fg-muted">
              Preset locali caricati in sola RAM durante la chat
            </p>
          </div>
          <Button variant="primary" size="sm" onClick={resetNew}>
            <Plus className="h-3.5 w-3.5" /> Nuovo Preset
          </Button>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">Preset Disponibili ({presets.length})</CardTitle>
            </CardHeader>
            <CardContent className="space-y-1.5 max-h-[65vh] overflow-y-auto">
              {presets.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => selectPreset(p)}
                  className={`w-full text-left p-2.5 rounded-md border transition-colors ${
                    editingId === p.id
                      ? "border-primary bg-info-bg text-fg"
                      : "border-border bg-surface-muted text-fg-muted hover:text-fg"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-semibold truncate">{p.name || p.id}</span>
                    {p.protected && <Badge variant="neutral">Protetto</Badge>}
                  </div>
                  <div className="text-[11px] font-mono text-fg-subtle truncate">{p.id}.md</div>
                </button>
              ))}
            </CardContent>
          </Card>

          <Card className="md:col-span-2">
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between">
                <CardTitle className="text-sm">Editor Preset</CardTitle>
                {isProtected && <Badge variant="warning">Protetto</Badge>}
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label htmlFor="peId" className="block text-xs text-fg-muted mb-1">
                    ID File (a-zA-Z0-9_-)
                  </label>
                  <input
                    id="peId"
                    value={editingId}
                    onChange={(e) => setEditingId(e.target.value)}
                    placeholder="security-reviewer"
                    className="w-full px-2.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
                  />
                </div>
                <div>
                  <label htmlFor="peName" className="block text-xs text-fg-muted mb-1">
                    Nome Preset
                  </label>
                  <input
                    id="peName"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="Security Reviewer"
                    className="w-full px-2.5 py-1.5 text-xs rounded-sm border border-border bg-bg text-fg"
                  />
                </div>
              </div>

              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label htmlFor="peTemp" className="block text-xs text-fg-muted mb-1">
                    Temperature
                  </label>
                  <input
                    id="peTemp"
                    type="number"
                    step="0.05"
                    min="0"
                    max="2"
                    value={temperature}
                    onChange={(e) => setTemperature(Number(e.target.value))}
                    className="w-full px-2.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
                  />
                </div>
                <div>
                  <label htmlFor="peLang" className="block text-xs text-fg-muted mb-1">
                    Lingua
                  </label>
                  <input
                    id="peLang"
                    value={language}
                    onChange={(e) => setLanguage(e.target.value)}
                    className="w-full px-2.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
                  />
                </div>
                <div>
                  <label htmlFor="peVer" className="block text-xs text-fg-muted mb-1">
                    Versione
                  </label>
                  <input
                    id="peVer"
                    value={version}
                    onChange={(e) => setVersion(e.target.value)}
                    className="w-full px-2.5 py-1.5 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
                  />
                </div>
              </div>

              <div>
                <label htmlFor="peDesc" className="block text-xs text-fg-muted mb-1">
                  Descrizione
                </label>
                <input
                  id="peDesc"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  className="w-full px-2.5 py-1.5 text-xs rounded-sm border border-border bg-bg text-fg"
                />
              </div>

              <div>
                <label htmlFor="peBody" className="block text-xs text-fg-muted mb-1">
                  System Prompt Markdown
                </label>
                <textarea
                  id="peBody"
                  rows={10}
                  value={content}
                  onChange={(e) => setContent(e.target.value)}
                  className="w-full px-2.5 py-2 text-xs font-mono rounded-sm border border-border bg-bg text-fg"
                />
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <Button size="sm" variant="primary" loading={busy} onClick={() => void handleSave()}>
                  <Save className="h-3.5 w-3.5" /> Salva Preset
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={!content.trim()}
                  onClick={() =>
                    onUseInChat({
                      id: editingId || "custom",
                      name: name || editingId || "Custom",
                      content,
                      recommended_temperature: temperature,
                    })
                  }
                >
                  Usa subito in Chat
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  disabled={!editingId || isProtected || busy}
                  onClick={() => void handleDelete()}
                >
                  <Trash2 className="h-3.5 w-3.5" /> Elimina
                </Button>
                {statusMsg && <span className="text-xs font-mono text-fg-muted">{statusMsg}</span>}
              </div>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
};
