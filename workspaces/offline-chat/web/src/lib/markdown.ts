// Pure TypeScript safe Markdown renderer (zero external CDNs, strict XSS escaping, no inline handlers)

export function escapeHtml(str: string): string {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderInline(escaped: string): string {
  const inlineCodes: string[] = [];
  let s = escaped.replace(/`([^`\n]+)`/g, (_m, code: string) => {
    const idx = inlineCodes.length;
    inlineCodes.push(`<code>${code}</code>`);
    return `\u0000IC${idx}\u0000`;
  });

  // Neutralize external links into safe non-navigating references
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_m, label: string, href: string) => {
    return `<strong>${label}</strong> <span class="text-fg-subtle text-xs font-mono">(${href})</span>`;
  });

  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  s = s.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");

  s = s.replace(/\u0000IC(\d+)\u0000/g, (_m, i: string) => inlineCodes[Number(i)] || "");
  return s;
}

export function renderSafeMarkdownHtml(rawText: string): string {
  const text = String(rawText || "").replace(/\r\n/g, "\n");
  if (!text.trim()) return "";

  const codeBlocks: Array<{ lang: string; code: string }> = [];
  let processed = text.replace(/```([a-zA-Z0-9_+-]*)[ \t]*\n([\s\S]*?)```/g, (_m, lang: string, code: string) => {
    const idx = codeBlocks.length;
    codeBlocks.push({ lang: lang || "text", code: code.replace(/\n$/, "") });
    return `\n\u0000CB${idx}\u0000\n`;
  });

  const openIdx = processed.indexOf("```");
  if (openIdx !== -1) {
    const rest = processed.slice(openIdx + 3);
    const nlIdx = rest.indexOf("\n");
    const lang = nlIdx !== -1 ? rest.slice(0, nlIdx).trim() || "text" : "text";
    const code = nlIdx !== -1 ? rest.slice(nlIdx + 1) : rest;
    const idx = codeBlocks.length;
    codeBlocks.push({ lang, code });
    processed = `${processed.slice(0, openIdx)}\n\u0000CB${idx}\u0000\n`;
  }

  const lines = processed.split("\n");
  const htmlOut: string[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      i++;
      continue;
    }

    const cbMatch = /^\u0000CB(\d+)\u0000$/.exec(trimmed);
    if (cbMatch) {
      const blk = codeBlocks[Number(cbMatch[1])];
      htmlOut.push(
        `<pre><div class="text-[11px] font-mono text-fg-subtle mb-1">${escapeHtml(blk.lang)}</div><code>${escapeHtml(blk.code)}</code></pre>`
      );
      i++;
      continue;
    }

    const hMatch = /^(#{1,3})\s+(.*)$/.exec(trimmed);
    if (hMatch) {
      const level = hMatch[1].length;
      htmlOut.push(`<h${level}>${renderInline(escapeHtml(hMatch[2]))}</h${level}>`);
      i++;
      continue;
    }

    if (trimmed.startsWith(">")) {
      const quoteLines: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        quoteLines.push(lines[i].trim().replace(/^>\s?/, ""));
        i++;
      }
      htmlOut.push(`<blockquote>${quoteLines.map((q) => renderInline(escapeHtml(q))).join("<br />")}</blockquote>`);
      continue;
    }

    if (/^[-*+]\s+/.test(trimmed)) {
      const items: string[] = [];
      while (i < lines.length && /^[-*+]\s+/.test(lines[i].trim())) {
        items.push(`<li>${renderInline(escapeHtml(lines[i].trim().replace(/^[-*+]\s+/, "")))}</li>`);
        i++;
      }
      htmlOut.push(`<ul>${items.join("")}</ul>`);
      continue;
    }

    if (/^\d+\.\s+/.test(trimmed)) {
      const items: string[] = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i].trim())) {
        items.push(`<li>${renderInline(escapeHtml(lines[i].trim().replace(/^\d+\.\s+/, "")))}</li>`);
        i++;
      }
      htmlOut.push(`<ol>${items.join("")}</ol>`);
      continue;
    }

    // Paragraph
    const paraLines: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^\u0000CB\d+\u0000$/.test(lines[i].trim()) &&
      !/^(#{1,3})\s+/.test(lines[i].trim()) &&
      !lines[i].trim().startsWith(">") &&
      !/^[-*+]\s+/.test(lines[i].trim()) &&
      !/^\d+\.\s+/.test(lines[i].trim())
    ) {
      paraLines.push(renderInline(escapeHtml(lines[i])));
      i++;
    }
    htmlOut.push(`<p>${paraLines.join("<br />")}</p>`);
  }

  return htmlOut.join("\n");
}

export function extractThinkSegments(rawText: string): { thinking: string; visible: string } {
  const thinkParts: string[] = [];
  let visible = String(rawText || "").replace(/<think>([\s\S]*?)<\/think>/gi, (_m, inner: string) => {
    if (inner.trim()) thinkParts.push(inner.trim());
    return "";
  });
  const openIdx = visible.toLowerCase().indexOf("<think>");
  if (openIdx !== -1) {
    const partial = visible.slice(openIdx + 7).trim();
    if (partial) thinkParts.push(partial);
    visible = visible.slice(0, openIdx);
  }
  return {
    thinking: thinkParts.join("\n\n").trim(),
    visible: visible.trimStart(),
  };
}
