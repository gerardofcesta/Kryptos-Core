(function () {
  'use strict';

  // --- State ---
  const state = {
    activeTab: 'chat',
    backend: 'ollama', // 'ollama' | 'heavy'
    history: [], // { role, content, thinking, stats, model, backend, timestamp }
    presets: [],
    telemetry: null,
    telemetryInFlight: false,
    registry: null,
    abortCtrl: null,
    selectedPromptId: null,
    selectedModelsByBackend: { ollama: '', heavy: '' },
    benchRunning: false,
    lastBenchRow: '',
    gatewayToken: null,
    orchestrator: {
      available: true,
      status: null,
      active: false,
      hasReport: false,
      retentionMode: null,
      stateName: 'IDLE',
      runId: null,
      reportMarkdown: '',
      reportRunId: null,
      reportVerdict: null,
      reportSource: null, // 'live_ephemeral' | 'live_persistent' | 'vault'
      reportLoadedKey: null,
      vaults: [],
      statusInFlight: false
    }
  };

  // --- DOM Refs ---
  const $ = (id) => document.getElementById(id);

  // --- Local Gateway Session Token (in-memory only, never localStorage) ---
  async function fetchSessionToken() {
    try {
      const res = await fetch('/api/auth/session-token', { cache: 'no-store' });
      if (!res.ok) return null;
      const data = await res.json();
      if (data && data.token) {
        state.gatewayToken = String(data.token);
        return state.gatewayToken;
      }
    } catch (_) {}
    return null;
  }

  async function apiFetch(url, options = {}) {
    if (!state.gatewayToken) {
      await fetchSessionToken();
    }
    const headers = Object.assign({}, options.headers || {});
    if (state.gatewayToken) {
      headers['X-Local-LLM-Token'] = state.gatewayToken;
    }
    const opts = Object.assign({}, options, { headers, cache: 'no-store' });
    let res = await fetch(url, opts);
    if (res.status === 401) {
      const refreshed = await fetchSessionToken();
      if (refreshed) {
        const retryHeaders = Object.assign({}, options.headers || {});
        retryHeaders['X-Local-LLM-Token'] = refreshed;
        res = await fetch(url, Object.assign({}, options, { headers: retryHeaders, cache: 'no-store' }));
      }
    }
    return res;
  }
  const chatMessages = $('chatMessages');
  const modelSelect = $('modelSelect');
  const presetSelect = $('presetSelect');
  const systemPromptEl = $('systemPrompt');
  const tempInput = $('tempInput');
  const topPInput = $('topPInput');
  const ctxInput = $('ctxInput');
  const maxTokensInput = $('maxTokensInput');
  const thinkToggle = $('thinkToggle');
  const reasoningEffortSelect = $('reasoningEffortSelect');
  const promptInput = $('promptInput');
  const sendBtn = $('sendBtn');
  const stopBtn = $('stopBtn');
  const regenBtn = $('regenBtn');

  // --- Safe HTML & Pure JS Markdown Renderer ---
  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function renderInlineMarkdown(escapedText) {
    const inlineCodes = [];
    let s = escapedText.replace(/`([^`\n]+)`/g, (_, code) => {
      const idx = inlineCodes.length;
      inlineCodes.push('<code class="inline-code">' + code + '</code>');
      return '\u0000IC' + idx + '\u0000';
    });

    // Safe link rendering (no external clickable navigation, shows label + path/code cleanly)
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, href) => {
      return '<strong>' + label + '</strong> <span class="mono" style="color:var(--muted);font-size:0.88em;">(' + href + ')</span>';
    });

    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/__([^_]+)__/g, '<strong>$1</strong>');
    s = s.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
    s = s.replace(/~~([^~]+)~~/g, '<del>$1</del>');

    s = s.replace(/\u0000IC(\d+)\u0000/g, (_, i) => inlineCodes[Number(i)] || '');
    return s;
  }

  function isTableSeparator(line) {
    const trimmed = line.trim();
    if (!trimmed.startsWith('|')) return false;
    const cells = trimmed.replace(/^\|/, '').replace(/\|$/, '').split('|');
    return cells.length > 0 && cells.every((c) => /^\s*:?-{2,}:?\s*$/.test(c));
  }

  function splitTableRow(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
  }

  function renderMarkdown(rawText) {
    const text = String(rawText || '').replace(/\r\n/g, '\n');
    if (!text.trim()) return '';

    const codeBlocks = [];
    // Handle closed fenced blocks first, then any trailing open fence while streaming
    let processed = text.replace(/```([a-zA-Z0-9_+-]*)[ \t]*\n([\s\S]*?)```/g, (_, lang, code) => {
      const idx = codeBlocks.length;
      codeBlocks.push({ lang: lang || 'text', code: code.replace(/\n$/, '') });
      return '\n\u0000CB' + idx + '\u0000\n';
    });

    const openFenceIdx = processed.indexOf('```');
    if (openFenceIdx !== -1) {
      const rest = processed.slice(openFenceIdx + 3);
      const nlIdx = rest.indexOf('\n');
      let lang = 'text';
      let code = '';
      if (nlIdx !== -1) {
        lang = rest.slice(0, nlIdx).trim() || 'text';
        code = rest.slice(nlIdx + 1);
      } else {
        lang = rest.trim() || 'text';
      }
      const idx = codeBlocks.length;
      codeBlocks.push({ lang, code });
      processed = processed.slice(0, openFenceIdx) + '\n\u0000CB' + idx + '\u0000\n';
    }

    const lines = processed.split('\n');
    const out = [];
    let i = 0;

    while (i < lines.length) {
      const rawLine = lines[i];
      const trimmed = rawLine.trim();

      if (!trimmed) {
        i++;
        continue;
      }

      // Code block placeholder
      const cbMatch = /^\u0000CB(\d+)\u0000$/.exec(trimmed);
      if (cbMatch) {
        const cb = codeBlocks[Number(cbMatch[1])];
        const safeLang = escapeHtml(cb.lang || 'text');
        const safeCode = escapeHtml(cb.code);
        out.push(
          '<div class="code-block">' +
            '<div class="code-head"><span>' + safeLang + '</span>' +
            '<button type="button" class="btn sm copy-code-btn">Copia</button></div>' +
            '<pre><code>' + safeCode + '</code></pre>' +
          '</div>'
        );
        i++;
        continue;
      }

      // Headings
      const hMatch = /^(#{1,4})\s+(.+)$/.exec(trimmed);
      if (hMatch) {
        const level = hMatch[1].length;
        out.push('<h' + level + '>' + renderInlineMarkdown(escapeHtml(hMatch[2])) + '</h' + level + '>');
        i++;
        continue;
      }

      // Horizontal rule
      if (/^([-*_])\1{2,}$/.test(trimmed)) {
        out.push('<hr />');
        i++;
        continue;
      }

      // Blockquote
      if (trimmed.startsWith('>')) {
        const qLines = [];
        while (i < lines.length && lines[i].trim().startsWith('>')) {
          qLines.push(lines[i].trim().replace(/^>\s?/, ''));
          i++;
        }
        out.push('<blockquote>' + qLines.map((l) => renderInlineMarkdown(escapeHtml(l))).join('<br />') + '</blockquote>');
        continue;
      }

      // Table
      if (trimmed.startsWith('|') && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
        const headers = splitTableRow(lines[i]);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].trim().startsWith('|')) {
          rows.push(splitTableRow(lines[i]));
          i++;
        }
        let tbl = '<div class="table-wrap"><table><thead><tr>';
        for (const h of headers) {
          tbl += '<th>' + renderInlineMarkdown(escapeHtml(h)) + '</th>';
        }
        tbl += '</tr></thead><tbody>';
        for (const r of rows) {
          tbl += '<tr>';
          for (let c = 0; c < headers.length; c++) {
            tbl += '<td>' + renderInlineMarkdown(escapeHtml(r[c] || '')) + '</td>';
          }
          tbl += '</tr>';
        }
        tbl += '</tbody></table></div>';
        out.push(tbl);
        continue;
      }

      // Unordered List
      if (/^[-*+]\s+/.test(trimmed)) {
        let ul = '<ul>';
        while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
          const item = lines[i].replace(/^\s*[-*+]\s+/, '');
          ul += '<li>' + renderInlineMarkdown(escapeHtml(item)) + '</li>';
          i++;
        }
        ul += '</ul>';
        out.push(ul);
        continue;
      }

      // Ordered List
      if (/^\d+\.\s+/.test(trimmed)) {
        let ol = '<ol>';
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
          const item = lines[i].replace(/^\s*\d+\.\s+/, '');
          ol += '<li>' + renderInlineMarkdown(escapeHtml(item)) + '</li>';
          i++;
        }
        ol += '</ol>';
        out.push(ol);
        continue;
      }

      // Paragraph
      const pLines = [];
      while (
        i < lines.length &&
        lines[i].trim() !== '' &&
        !/^\u0000CB\d+\u0000$/.test(lines[i].trim()) &&
        !/^(#{1,4})\s+/.test(lines[i].trim()) &&
        !/^([-*_])\1{2,}$/.test(lines[i].trim()) &&
        !lines[i].trim().startsWith('>') &&
        !/^\s*[-*+]\s+/.test(lines[i]) &&
        !/^\s*\d+\.\s+/.test(lines[i]) &&
        !(lines[i].trim().startsWith('|') && i + 1 < lines.length && isTableSeparator(lines[i + 1]))
      ) {
        pLines.push(lines[i]);
        i++;
      }
      out.push('<p>' + pLines.map((l) => renderInlineMarkdown(escapeHtml(l))).join('<br />') + '</p>');
    }

    return out.join('\n');
  }

  // Extract inline <think>...</think> tags safely even during streaming
  function splitThinkTags(rawText) {
    const text = String(rawText || '');
    let visible = '';
    const thinkChunks = [];
    let cursor = 0;

    while (cursor < text.length) {
      const openIdx = text.indexOf('<think>', cursor);
      if (openIdx === -1) {
        visible += text.slice(cursor);
        break;
      }
      visible += text.slice(cursor, openIdx);
      const closeIdx = text.indexOf('</think>', openIdx + 7);
      if (closeIdx === -1) {
        thinkChunks.push(text.slice(openIdx + 7));
        break;
      }
      thinkChunks.push(text.slice(openIdx + 7, closeIdx));
      cursor = closeIdx + 8;
    }

    // Prevent flashing incomplete trailing "<think" while streaming
    const lastLt = visible.lastIndexOf('<');
    if (lastLt !== -1 && '<think>'.startsWith(visible.slice(lastLt))) {
      visible = visible.slice(0, lastLt);
    }

    return {
      visible: thinkChunks.length ? visible.replace(/^\s*\n+/, '') : visible,
      thinking: thinkChunks.join('\n').trim()
    };
  }

  async function copyTextToClipboard(text, btnEl) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      if (btnEl) {
        const old = btnEl.textContent;
        btnEl.textContent = 'Copiato!';
        setTimeout(() => { btnEl.textContent = old; }, 1300);
      }
    } catch (_) {}
  }

  // --- Tab Navigation & Operator Profile Switcher ---
  function syncOperatorProfileUI() {
    const isOrchTab = state.activeTab === 'orchestrator';
    const btnGhost = $('btnProfileGhost');
    const btnOrch = $('btnProfileOrchestrator');
    const policyTag = $('topOrchPolicyTag');
    const headerPill = $('orchHeaderPolicyPill');
    const liveDot = $('orchTabLiveDot');

    if (btnGhost) btnGhost.classList.toggle('active', !isOrchTab);
    if (btnOrch) {
      btnOrch.classList.toggle('active', isOrchTab);
      btnOrch.classList.toggle('persistent-mode', state.orchestrator.retentionMode === 'persistent');
    }

    const rm = state.orchestrator.retentionMode;
    const active = state.orchestrator.active;
    const hasRep = state.orchestrator.hasReport;
    let label = 'IDLE';
    let tagCls = 'op-policy-tag';
    let pillCls = 'pill';
    if (rm === 'ephemeral' && (active || hasRep)) {
      label = 'EPHEMERAL · ZERO DISCO';
      tagCls = 'op-policy-tag ephemeral';
      pillCls = 'pill online';
    } else if (rm === 'persistent' && (active || hasRep)) {
      label = 'PERSISTENT · VAULT';
      tagCls = 'op-policy-tag persistent';
      pillCls = 'pill warn';
    } else if (active) {
      label = state.orchestrator.stateName || 'RUNNING';
      tagCls = 'op-policy-tag';
      pillCls = 'pill cyan';
    }

    if (policyTag) {
      policyTag.textContent = label;
      policyTag.className = tagCls;
    }
    if (headerPill) {
      headerPill.textContent = label;
      headerPill.className = pillCls;
    }
    if (liveDot) {
      liveDot.style.display = active ? 'inline-block' : 'none';
    }
  }

  function switchTab(tabName) {
    state.activeTab = tabName;
    document.querySelectorAll('.tab-btn').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.tab === tabName);
    });
    document.querySelectorAll('.tab-panel').forEach((panel) => {
      panel.classList.toggle('active', panel.id === 'tab-' + tabName);
    });
    syncOperatorProfileUI();
    if (tabName === 'engines') {
      fetchTelemetry();
    } else if (tabName === 'models') {
      fetchRegistry();
      updateBenchTargetLabel();
    } else if (tabName === 'prompts') {
      renderPromptManagerList();
    } else if (tabName === 'orchestrator') {
      populateOrchFastModels();
      fetchOrchestratorStatus();
      fetchOrchestratorVaults();
    }
  }

  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  const btnProfileGhost = $('btnProfileGhost');
  if (btnProfileGhost) {
    btnProfileGhost.addEventListener('click', () => switchTab('chat'));
  }
  const btnProfileOrchestrator = $('btnProfileOrchestrator');
  if (btnProfileOrchestrator) {
    btnProfileOrchestrator.addEventListener('click', () => switchTab('orchestrator'));
  }

  // --- Backend & Models Management ---
  function setBackend(backend) {
    state.backend = backend;
    $('btnBackendFast').classList.toggle('active', backend === 'ollama');
    $('btnBackendHeavy').classList.toggle('active', backend === 'heavy');
    populateModelSelect();
    updateChatHeader();
    updateBenchTargetLabel();
  }

  $('btnBackendFast').addEventListener('click', () => setBackend('ollama'));
  $('btnBackendHeavy').addEventListener('click', () => setBackend('heavy'));

  modelSelect.addEventListener('change', () => {
    state.selectedModelsByBackend[state.backend] = modelSelect.value;
    updateChatHeader();
    updateBenchTargetLabel();
  });

  async function populateModelSelect() {
    modelSelect.innerHTML = '';
    if (state.backend === 'ollama') {
      try {
        const res = await apiFetch('/api/ollama/tags');
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Offline');
        const models = (data.models || []).slice();
        models.sort((a, b) => (b.name.startsWith('local-uncensored') ? 1 : 0) - (a.name.startsWith('local-uncensored') ? 1 : 0));
        if (models.length === 0) {
          const opt = document.createElement('option');
          opt.value = '';
          opt.textContent = 'Nessun modello Ollama installato';
          modelSelect.appendChild(opt);
        } else {
          for (const m of models) {
            const gb = m.size ? (m.size / (1024 ** 3)).toFixed(1) + ' GiB' : '';
            const opt = document.createElement('option');
            opt.value = m.name;
            opt.textContent = gb ? `${m.name} (${gb})` : m.name;
            modelSelect.appendChild(opt);
          }
          const saved = state.selectedModelsByBackend.ollama;
          if (saved && models.some((m) => m.name === saved)) {
            modelSelect.value = saved;
          } else {
            state.selectedModelsByBackend.ollama = modelSelect.value;
          }
        }
      } catch (_) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = 'Ollama offline (avvia da Tab Motori)';
        modelSelect.appendChild(opt);
      }
    } else {
      // Heavy MoE Colibrì
      let items = [];
      try {
        const res = await apiFetch('/api/heavy/models');
        const data = await res.json();
        if (res.ok && Array.isArray(data.data)) {
          items = data.data.map((m) => m.id).filter(Boolean);
        }
      } catch (_) {}

      if (items.length === 0 && state.telemetry && state.telemetry.backends && state.telemetry.backends.heavyMoe) {
        const hv = state.telemetry.backends.heavyMoe;
        if (Array.isArray(hv.models) && hv.models.length > 0) {
          items = hv.models.slice();
        } else if (hv.name) {
          items = [hv.name];
        }
      }

      if (items.length === 0) {
        const opt = document.createElement('option');
        opt.value = 'heavy-moe';
        opt.textContent = 'Colibrì Heavy MoE (offline o non configurato)';
        modelSelect.appendChild(opt);
      } else {
        for (const id of items) {
          const opt = document.createElement('option');
          opt.value = id;
          opt.textContent = id;
          modelSelect.appendChild(opt);
        }
        const saved = state.selectedModelsByBackend.heavy;
        if (saved && items.includes(saved)) {
          modelSelect.value = saved;
        } else {
          state.selectedModelsByBackend.heavy = modelSelect.value;
        }
      }
    }
    updateChatHeader();
    updateBenchTargetLabel();
  }

  $('refreshModelsBtn').addEventListener('click', () => {
    fetchTelemetry().then(() => populateModelSelect());
  });

  function updateChatHeader() {
    const bkLabel = state.backend === 'ollama' ? 'Fast Tier · Ollama (:11434)' : 'Heavy MoE · Colibrì (:11435)';
    const modelLabel = modelSelect.value || '--';
    $('chatActiveInfo').textContent = `${bkLabel} · Modello: ${modelLabel} · Think: ${thinkToggle.checked ? 'ON' : 'OFF'} (${reasoningEffortSelect.value})`;
    const turns = state.history.filter((m) => m.role === 'user').length;
    $('chatLiveStats').textContent = `Sessione RAM: ${turns} turni · ${state.history.length} messaggi`;
  }

  thinkToggle.addEventListener('change', updateChatHeader);
  reasoningEffortSelect.addEventListener('change', updateChatHeader);

  // --- Telemetry Polling ---
  function setMeter(meterEl, pct, mode) {
    const clamped = Math.max(0, Math.min(100, Number(pct) || 0));
    meterEl.style.width = clamped + '%';
    meterEl.className = 'meter-fill' + (mode ? ' ' + mode : '');
  }

  function renderTelemetry(t) {
    state.telemetry = t;
    const bk = t.backends || {};
    const ol = bk.ollama || {};
    const hv = bk.heavyMoe || {};
    const lms = bk.lmstudio || {};
    const gpu = t.gpu || {};
    const ram = t.ram || {};
    const disk = t.disk || {};

    // Top Bar Pills
    const olPill = $('topOllamaPill');
    olPill.textContent = ol.online ? 'Fast :11434 ON' : 'Fast :11434 OFF';
    olPill.className = 'pill ' + (ol.online ? 'online' : 'offline');

    const hvPill = $('topHeavyPill');
    hvPill.textContent = hv.online ? 'Heavy :11435 ON' : 'Heavy :11435 OFF';
    hvPill.className = 'pill ' + (hv.online ? 'online' : 'offline');

    $('topVramPill').textContent = gpu.available ? `VRAM ${gpu.memoryUsedGb}/${gpu.memoryTotalGb} GiB` : 'VRAM N/A';

    const topRam = $('topRamPill');
    topRam.textContent = `RAM ${ram.usedGb}/${ram.totalGb} GiB`;
    topRam.className = 'pill ' + (ram.swapWarning ? 'warn' : '');

    const topDot = $('topOnlineDot');
    if (ol.online || hv.online) {
      topDot.style.background = 'var(--accent)';
      topDot.style.boxShadow = '0 0 8px rgba(63, 185, 80, 0.7)';
    } else {
      topDot.style.background = 'var(--amber)';
      topDot.style.boxShadow = '0 0 8px rgba(210, 153, 34, 0.6)';
    }

    // Sidebar backend cards
    const fastBadge = $('fastCardBadge');
    fastBadge.textContent = ol.online ? 'ONLINE' : 'OFFLINE';
    fastBadge.className = 'status-dot ' + (ol.online ? 'on' : 'off');

    const heavyBadge = $('heavyCardBadge');
    heavyBadge.textContent = hv.online ? 'ONLINE' : 'OFFLINE';
    heavyBadge.className = 'status-dot ' + (hv.online ? 'on' : 'off');

    // Tab 2: Hardware cards
    if (gpu.available) {
      $('gpuNameText').textContent = gpu.name || 'NVIDIA GPU';
      $('gpuTempPill').textContent = gpu.temperatureC != null ? `${gpu.temperatureC.toFixed(0)} °C` : '-- °C';
      const gpuMode = gpu.memoryUsedPct > 92 ? 'danger' : gpu.memoryUsedPct > 78 ? 'warn' : '';
      setMeter($('gpuMeter'), gpu.memoryUsedPct, gpuMode);
      $('gpuKv').innerHTML =
        `<span>Usata: <strong>${gpu.memoryUsedGb} GiB</strong></span>` +
        `<span>Libera: <strong>${gpu.memoryFreeGb} GiB</strong></span>` +
        `<span>Totale: <strong>${gpu.memoryTotalGb} GiB</strong></span>` +
        `<span>Power: <strong>${gpu.powerDrawW != null ? gpu.powerDrawW.toFixed(1) + ' W' : '--'}</strong></span>` +
        `<span>Util: <strong>${gpu.utilizationPct != null ? gpu.utilizationPct.toFixed(0) + '%' : '--'}</strong></span>`;
    } else {
      $('gpuNameText').textContent = 'nvidia-smi non disponibile';
      setMeter($('gpuMeter'), 0, '');
    }

    const ramMode = ram.swapWarning ? 'danger' : ram.usedPct > 75 ? 'warn' : 'blue';
    setMeter($('ramMeter'), ram.usedPct, ramMode);
    const swapPill = $('ramSwapPill');
    if (ram.swapWarning) {
      swapPill.textContent = 'WARNING SWAP (<6 GB)';
      swapPill.className = 'pill offline';
      $('ramHeadroomNote').textContent = 'Memoria libera critica: rischio paging su Windows 11.';
    } else {
      swapPill.textContent = 'Anti-Swap OK';
      swapPill.className = 'pill online';
      $('ramHeadroomNote').textContent = ram.heavyReady
        ? 'Headroom sufficiente per Heavy MoE (>= 14 GiB liberi).'
        : 'OK per Fast Tier; libera RAM (< 14 GiB liberi per Heavy MoE).';
    }
    $('ramKv').innerHTML =
      `<span>Usata: <strong>${ram.usedGb} GiB (${ram.usedPct}%)</strong></span>` +
      `<span>Libera: <strong>${ram.freeGb} GiB</strong></span>` +
      `<span>Totale: <strong>${ram.totalGb} GiB</strong></span>`;

    $('diskDrivePill').textContent = disk.drive || 'C:\\';
    setMeter($('diskMeter'), disk.usedPct, disk.freeGb < 100 ? 'warn' : 'blue');
    $('diskKv').innerHTML =
      `<span>Libero: <strong>${disk.freeGb} GiB</strong></span>` +
      `<span>Usato: <strong>${disk.usedGb} GiB</strong></span>` +
      `<span>Totale: <strong>${disk.totalGb} GiB</strong></span>`;

    // Tab 2: Engine cards
    $('engOllamaBadge').textContent = ol.online ? 'ONLINE' : 'OFFLINE';
    $('engOllamaBadge').className = 'pill ' + (ol.online ? 'online' : 'offline');
    $('engOllamaUrl').textContent = ol.url || 'http://127.0.0.1:11434';
    $('engOllamaInfo').textContent = ol.activeModel
      ? `Attivo in VRAM: ${ol.activeModel}`
      : (ol.online ? `Pronto (${(ol.models || []).length} modelli)` : 'Daemon spento');

    $('engHeavyBadge').textContent = hv.online ? 'ONLINE' : 'OFFLINE';
    $('engHeavyBadge').className = 'pill ' + (hv.online ? 'online' : 'offline');
    $('engHeavyUrl').textContent = hv.url || 'http://127.0.0.1:11435';
    const hvParts = [];
    if (hv.activeModel) hvParts.push(`Attivo: ${hv.activeModel}`);
    else if (hv.name) hvParts.push(`Configurato: ${hv.name}`);
    else hvParts.push('Nessun modello configurato');
    if (hv.quant) hvParts.push(hv.quant);
    if (hv.backend) hvParts.push(hv.backend);
    $('engHeavyInfo').textContent = hvParts.join(' · ');

    $('engLmsBadge').textContent = lms.online ? 'ONLINE' : 'OFFLINE';
    $('engLmsBadge').className = 'pill ' + (lms.online ? 'online' : 'offline');
    $('engLmsUrl').textContent = lms.url || 'http://127.0.0.1:1234';
    $('engLmsInfo').textContent = lms.online
      ? `Online (${(lms.models || []).length} modelli)`
      : 'Server locale non attivo';

    // Privacy grid
    const priv = t.privacy || {};
    $('privacyGrid').innerHTML =
      `<div class="privacy-item"><strong>Loopback Exclusive</strong><br /><span class="mono">${priv.loopbackOnly ? '127.0.0.1 (OK)' : 'ERR'}</span></div>` +
      `<div class="privacy-item"><strong>Modalità Offline</strong><br /><span class="mono">${priv.offlineMode ? 'ATTIVA' : 'NO'}</span></div>` +
      `<div class="privacy-item"><strong>Cronologia Disco</strong><br /><span class="mono">${priv.diskChatHistory ? 'ABILITATA' : 'ZERO LOG (OK)'}</span></div>` +
      `<div class="privacy-item"><strong>CSP Strict</strong><br /><span class="mono">${priv.cspEnforced ? "default-src 'self'" : 'NO'}</span></div>`;

    // Sync compact Orchestrator telemetry into header pills even when on other tabs
    if (t.orchestrator && typeof t.orchestrator === 'object') {
      const ot = t.orchestrator;
      state.orchestrator.available = ot.available !== false;
      state.orchestrator.active = Boolean(ot.active);
      state.orchestrator.hasReport = Boolean(ot.hasReport);
      state.orchestrator.retentionMode = ot.retentionMode || null;
      state.orchestrator.stateName = String(ot.state || 'IDLE').toUpperCase();
      syncOperatorProfileUI();
    }
  }

  async function fetchTelemetry() {
    if (state.telemetryInFlight) return null;
    state.telemetryInFlight = true;
    try {
      const res = await apiFetch('/api/system/telemetry');
      if (!res.ok) return null;
      const data = await res.json();
      renderTelemetry(data);
      return data;
    } catch (_) {
      return null;
    } finally {
      state.telemetryInFlight = false;
    }
  }

  $('refreshTelemetryBtn').addEventListener('click', fetchTelemetry);

  // --- Engine Control ---
  async function runEngineAction(action) {
    const btns = ['btnStartFast', 'btnStartHeavy', 'btnUnloadVram', 'btnStopAll'];
    btns.forEach((id) => { $(id).disabled = true; });
    $('engineActionStatus').textContent = `Esecuzione '${action}' in corso...`;
    $('engineConsole').textContent = `> Invio azione '${action}'...`;

    const payload = {
      action,
      ramLimitGb: parseInt($('ctrlRamLimit').value, 10) || 12,
      ctx: parseInt($('ctrlCtx').value, 10) || 16384
    };

    try {
      const res = await apiFetch('/api/control/engine', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      $('engineConsole').textContent = data.output || data.error || JSON.stringify(data, null, 2);
      $('engineActionStatus').textContent = data.ok ? `Completato: ${action}` : `Errore (${res.status})`;
    } catch (err) {
      $('engineConsole').textContent = `Errore rete locale: ${err.message}`;
      $('engineActionStatus').textContent = 'Errore';
    } finally {
      btns.forEach((id) => { $(id).disabled = false; });
      await fetchTelemetry();
      await populateModelSelect();
    }
  }

  $('btnStartFast').addEventListener('click', () => runEngineAction('start_fast'));
  $('btnStartHeavy').addEventListener('click', () => runEngineAction('start_heavy'));
  $('btnUnloadVram').addEventListener('click', () => runEngineAction('unload_vram'));
  $('btnStopAll').addEventListener('click', () => runEngineAction('stop_all'));

  // --- Prompt Presets & Prompt Manager ---
  async function loadPresets(keepSelectionId) {
    try {
      const res = await apiFetch('/api/prompts');
      const data = await res.json();
      state.presets = data.prompts || [];

      const prevId = keepSelectionId || presetSelect.value;
      presetSelect.innerHTML = '<option value="">(Personalizzato / Modelfile default)</option>';
      for (const p of state.presets) {
        const opt = document.createElement('option');
        opt.value = p.id;
        const tempHint = p.meta && p.meta.recommended_temperature != null ? ` (T=${p.meta.recommended_temperature})` : '';
        opt.textContent = `${p.name}${tempHint}`;
        presetSelect.appendChild(opt);
      }

      if (prevId && state.presets.some((p) => p.id === prevId)) {
        presetSelect.value = prevId;
        const found = state.presets.find((p) => p.id === prevId);
        systemPromptEl.value = found ? found.content : '';
      } else if (state.presets.length > 0 && !systemPromptEl.value.trim()) {
        const def = state.presets.find((p) => p.id === 'uncensored-direct') || state.presets[0];
        presetSelect.value = def.id;
        systemPromptEl.value = def.content;
        if (def.meta && def.meta.recommended_temperature != null) {
          tempInput.value = def.meta.recommended_temperature;
        }
      }

      renderPromptManagerList();
    } catch (_) {}
  }

  presetSelect.addEventListener('change', () => {
    const found = state.presets.find((p) => p.id === presetSelect.value);
    systemPromptEl.value = found ? found.content : '';
    if (found && found.meta && found.meta.recommended_temperature != null) {
      tempInput.value = found.meta.recommended_temperature;
    }
  });

  function fillPromptEditor(preset) {
    if (!preset) {
      state.selectedPromptId = null;
      $('peEditorTitle').textContent = 'Nuovo Preset Prompt';
      $('peProtectedBadge').style.display = 'none';
      $('deletePromptBtn').disabled = true;
      $('peId').value = '';
      $('peName').value = '';
      $('peTemp').value = '0.7';
      $('peLang').value = 'it';
      $('peVersion').value = '1.0';
      $('peTargets').value = 'Qwen3.8-27B-abliterated, Heavy-MoE-122B-177B';
      $('peDesc').value = '';
      $('peContent').value = '';
      $('peStatusMsg').textContent = '';
      return;
    }
    state.selectedPromptId = preset.id;
    const meta = preset.meta || {};
    $('peEditorTitle').textContent = `Modifica: ${preset.id}.md`;
    $('peProtectedBadge').style.display = preset.protected ? 'inline-flex' : 'none';
    $('deletePromptBtn').disabled = Boolean(preset.protected);
    $('peId').value = preset.id;
    $('peName').value = preset.name || preset.id;
    $('peTemp').value = meta.recommended_temperature != null ? meta.recommended_temperature : '0.7';
    $('peLang').value = meta.language || 'it';
    $('peVersion').value = meta.version != null ? String(meta.version) : '1.0';
    $('peTargets').value = Array.isArray(meta.target_models) ? meta.target_models.join(', ') : (meta.target_models || '');
    $('peDesc').value = meta.description || '';
    $('peContent').value = preset.content || '';
    $('peStatusMsg').textContent = '';
  }

  function renderPromptManagerList() {
    const listEl = $('promptManagerList');
    $('promptCountLabel').textContent = `${state.presets.length} file`;
    listEl.innerHTML = '';
    for (const p of state.presets) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'prompt-item' + (state.selectedPromptId === p.id ? ' active' : '');
      const temp = p.meta && p.meta.recommended_temperature != null ? p.meta.recommended_temperature : '--';
      const lang = (p.meta && p.meta.language) || 'it';
      btn.innerHTML =
        `<div class="prompt-item-title"><span>${escapeHtml(p.name)}</span>` +
        (p.protected ? '<span class="pill warn" style="padding:1px 6px;font-size:10px;">Protetto</span>' : '') +
        `</div><div class="prompt-item-meta">${escapeHtml(p.id)}.md · T=${escapeHtml(temp)} · ${escapeHtml(lang)}</div>`;
      btn.addEventListener('click', () => {
        fillPromptEditor(p);
        renderPromptManagerList();
      });
      listEl.appendChild(btn);
    }
    if (!state.selectedPromptId && state.presets.length > 0) {
      fillPromptEditor(state.presets[0]);
      renderPromptManagerList();
    }
  }

  $('newPromptBtn').addEventListener('click', () => {
    fillPromptEditor(null);
    renderPromptManagerList();
  });

  $('savePromptBtn').addEventListener('click', async () => {
    const id = $('peId').value.trim();
    const content = $('peContent').value.trim();
    if (!/^[a-zA-Z0-9_-]{2,64}$/.test(id)) {
      $('peStatusMsg').textContent = 'ID non valido (usa 2-64 caratteri: a-z, 0-9, _, -).';
      return;
    }
    if (!content) {
      $('peStatusMsg').textContent = 'Inserisci il contenuto del prompt.';
      return;
    }
    const targets = $('peTargets').value.split(',').map((s) => s.trim()).filter(Boolean);
    const payload = {
      id,
      content,
      meta: {
        name: $('peName').value.trim() || id,
        version: $('peVersion').value.trim() || '1.0',
        language: $('peLang').value.trim() || 'it',
        recommended_temperature: parseFloat($('peTemp').value) || 0.7,
        target_models: targets,
        description: $('peDesc').value.trim()
      }
    };

    try {
      const res = await apiFetch('/api/prompts/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        $('peStatusMsg').textContent = data.error || 'Errore salvataggio.';
        return;
      }
      state.selectedPromptId = id;
      await loadPresets(id);
      const saved = state.presets.find((p) => p.id === id);
      if (saved) fillPromptEditor(saved);
      $('peStatusMsg').textContent = `Salvato: prompts/${id}.md`;
    } catch (err) {
      $('peStatusMsg').textContent = `Errore: ${err.message}`;
    }
  });

  $('usePromptInChatBtn').addEventListener('click', () => {
    const id = $('peId').value.trim();
    const editorText = $('peContent').value;
    const found = state.presets.find((p) => p.id === id);
    if (found && found.content === editorText) {
      presetSelect.value = found.id;
      systemPromptEl.value = found.content;
      if (found.meta && found.meta.recommended_temperature != null) {
        tempInput.value = found.meta.recommended_temperature;
      }
    } else {
      presetSelect.value = '';
      systemPromptEl.value = editorText;
      const tVal = parseFloat($('peTemp').value);
      if (Number.isFinite(tVal)) tempInput.value = tVal;
    }
    switchTab('chat');
  });

  $('deletePromptBtn').addEventListener('click', async () => {
    const id = $('peId').value.trim();
    if (!id) return;
    if (!confirm(`Eliminare definitivamente il preset "${id}.md"?`)) return;
    try {
      const res = await apiFetch('/api/prompts/' + encodeURIComponent(id), { method: 'DELETE' });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        $('peStatusMsg').textContent = data.error || 'Impossibile eliminare.';
        return;
      }
      state.selectedPromptId = null;
      await loadPresets();
      fillPromptEditor(state.presets[0] || null);
      renderPromptManagerList();
      $('peStatusMsg').textContent = `Eliminato: ${id}.md`;
    } catch (err) {
      $('peStatusMsg').textContent = `Errore: ${err.message}`;
    }
  });

  // --- Chat Message DOM & Streaming ---
  function createMessageCard(role, modelLabel, initialText) {
    const welcome = $('chatWelcome');
    if (welcome) welcome.remove();

    const wrap = document.createElement('div');
    wrap.className = `msg ${role}`;

    const head = document.createElement('div');
    head.className = 'msg-head';
    const titleSpan = document.createElement('span');
    titleSpan.textContent = role === 'user' ? 'Tu' : (modelLabel || 'Modello');
    const actionsSpan = document.createElement('span');
    actionsSpan.className = 'inline-row';
    const copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'btn sm copy-msg-btn';
    copyBtn.textContent = 'Copia';
    actionsSpan.appendChild(copyBtn);
    head.appendChild(titleSpan);
    head.appendChild(actionsSpan);
    wrap.appendChild(head);

    let thinkDetails = null;
    let thinkSummary = null;
    let thinkBody = null;
    if (role === 'assistant') {
      thinkDetails = document.createElement('details');
      thinkDetails.className = 'thinking';
      thinkDetails.style.display = 'none';
      thinkSummary = document.createElement('summary');
      thinkSummary.textContent = 'Ragionamento interno';
      thinkBody = document.createElement('div');
      thinkBody.className = 'thinking-body';
      thinkDetails.appendChild(thinkSummary);
      thinkDetails.appendChild(thinkBody);
      wrap.appendChild(thinkDetails);
    }

    const bodyEl = document.createElement('div');
    if (role === 'user') {
      bodyEl.className = 'msg-body-plain';
      bodyEl.textContent = initialText || '';
      wrap.dataset.rawCopy = initialText || '';
    } else {
      bodyEl.className = 'md-content';
      bodyEl.innerHTML = initialText ? renderMarkdown(initialText) : '';
      wrap.dataset.rawCopy = initialText || '';
    }
    wrap.appendChild(bodyEl);

    const statsEl = document.createElement('div');
    statsEl.className = 'msg-stats';
    statsEl.style.display = 'none';
    wrap.appendChild(statsEl);

    chatMessages.appendChild(wrap);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    return { wrap, bodyEl, thinkDetails, thinkSummary, thinkBody, statsEl };
  }

  // Event delegation for Copy Message & Copy Code Block buttons
  chatMessages.addEventListener('click', (e) => {
    const codeBtn = e.target.closest('.copy-code-btn');
    if (codeBtn) {
      const codeEl = codeBtn.closest('.code-block')?.querySelector('pre code');
      if (codeEl) copyTextToClipboard(codeEl.textContent || '', codeBtn);
      return;
    }
    const msgBtn = e.target.closest('.copy-msg-btn');
    if (msgBtn) {
      const msgWrap = msgBtn.closest('.msg');
      if (msgWrap) copyTextToClipboard(msgWrap.dataset.rawCopy || '', msgBtn);
    }
  });

  function buildApiMessages() {
    const msgs = [];
    const sys = systemPromptEl.value.trim();
    if (sys) msgs.push({ role: 'system', content: sys });
    for (const item of state.history) {
      msgs.push({ role: item.role, content: item.content });
    }
    return msgs;
  }

  function formatStatsLine(metrics) {
    const parts = [];
    if (metrics.ttftSec != null) parts.push(`TTFT: ${metrics.ttftSec.toFixed(2)}s`);
    if (metrics.prefillTps != null) parts.push(`Prefill: ${metrics.prefillTps.toFixed(1)} tok/s`);
    if (metrics.decodeTps != null) parts.push(`Decode: ${metrics.decodeTps.toFixed(1)} tok/s`);
    if (metrics.totalSec != null) parts.push(`Totale: ${metrics.totalSec.toFixed(2)}s`);
    if (metrics.inTokens != null || metrics.outTokens != null) {
      parts.push(`Token: ${metrics.inTokens ?? '--'} in / ${metrics.outTokens ?? '--'} out`);
    }
    return parts.join(' · ');
  }

  async function streamChatTurn() {
    const backend = state.backend;
    const model = modelSelect.value;
    if (!model || state.abortCtrl) return;

    const apiMessages = buildApiMessages();
    const bkTag = backend === 'ollama' ? 'Fast' : 'Heavy MoE';
    const ui = createMessageCard('assistant', `${model} (${bkTag})`, '');

    state.abortCtrl = new AbortController();
    sendBtn.style.display = 'none';
    stopBtn.style.display = 'inline-flex';
    regenBtn.disabled = true;

    const tStart = performance.now();
    let tFirstToken = null;
    let explicitThinking = '';
    let rawContent = '';
    let clientOutChunks = 0;
    let finalMetrics = {
      ttftSec: null,
      prefillTps: null,
      decodeTps: null,
      totalSec: null,
      inTokens: null,
      outTokens: null
    };

    const updateAssistantDom = (isFinal) => {
      const split = splitThinkTags(rawContent);
      const combinedThink = [explicitThinking, split.thinking].filter(Boolean).join('\n\n').trim();
      const visible = split.visible;

      if (combinedThink) {
        ui.thinkDetails.style.display = 'block';
        ui.thinkBody.textContent = combinedThink;
        ui.thinkSummary.textContent = isFinal
          ? `Ragionamento interno (${combinedThink.length} car.)`
          : 'Ragionamento interno (in corso...)';
      }

      ui.bodyEl.innerHTML = renderMarkdown(visible);
      ui.wrap.dataset.rawCopy = visible;
      chatMessages.scrollTop = chatMessages.scrollHeight;
      return { visible, combinedThink };
    };

    try {
      const temperature = parseFloat(tempInput.value);
      const topP = parseFloat(topPInput.value);
      const numCtx = parseInt(ctxInput.value, 10) || 16384;
      const maxTokens = parseInt(maxTokensInput.value, 10);
      const thinkEnabled = Boolean(thinkToggle.checked);
      const effort = reasoningEffortSelect.value;

      const url = backend === 'ollama' ? '/api/ollama/chat' : '/api/heavy/chat/completions';
      let bodyPayload;
      if (backend === 'ollama') {
        const options = {
          temperature: Number.isFinite(temperature) ? temperature : 0.7,
          top_p: Number.isFinite(topP) ? topP : 0.9,
          num_ctx: numCtx
        };
        if (Number.isFinite(maxTokens) && maxTokens > 0) {
          options.num_predict = maxTokens;
        }
        bodyPayload = {
          model,
          messages: apiMessages,
          stream: true,
          think: thinkEnabled,
          options
        };
      } else {
        bodyPayload = {
          model,
          messages: apiMessages,
          stream: true,
          temperature: Number.isFinite(temperature) ? temperature : 0.7,
          top_p: Number.isFinite(topP) ? topP : 0.9,
          max_tokens: Number.isFinite(maxTokens) && maxTokens > 0 ? maxTokens : 4096,
          enable_thinking: thinkEnabled,
          reasoning_effort: thinkEnabled ? effort : 'none'
        };
      }

      const res = await apiFetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: state.abortCtrl.signal,
        body: JSON.stringify(bodyPayload)
      });

      if (!res.ok) {
        let errMsg = `HTTP ${res.status}`;
        try {
          const errJson = await res.json();
          errMsg = typeof errJson.error === 'string' ? errJson.error : JSON.stringify(errJson.error || errJson);
        } catch (_) {
          errMsg = await res.text();
        }
        throw new Error(errMsg);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let sseDone = false;

      const processLine = (rawLine) => {
        const line = rawLine.replace(/\r$/, '').trim();
        if (!line || line.startsWith(':')) return;

        let jsonStr = line;
        if (backend === 'heavy' || line.startsWith('data:')) {
          if (line.startsWith('data:')) {
            jsonStr = line.slice(5).trim();
          }
          if (jsonStr === '[DONE]') {
            sseDone = true;
            return;
          }
        }

        let obj;
        try {
          obj = JSON.parse(jsonStr);
        } catch (_) {
          return;
        }

        if (backend === 'ollama') {
          if (obj.message) {
            if (obj.message.thinking) {
              if (tFirstToken === null) tFirstToken = performance.now();
              explicitThinking += obj.message.thinking;
            }
            if (obj.message.content) {
              if (tFirstToken === null) tFirstToken = performance.now();
              rawContent += obj.message.content;
            }
            updateAssistantDom(false);
          }
          if (obj.done) {
            const evalCount = obj.eval_count || 0;
            const evalSec = (obj.eval_duration || 0) / 1e9;
            const promptCount = obj.prompt_eval_count || 0;
            const promptSec = (obj.prompt_eval_duration || 0) / 1e9;
            finalMetrics.inTokens = promptCount;
            finalMetrics.outTokens = evalCount;
            finalMetrics.prefillTps = promptSec > 0 ? promptCount / promptSec : null;
            finalMetrics.decodeTps = evalSec > 0 ? evalCount / evalSec : null;
            finalMetrics.totalSec = obj.total_duration ? obj.total_duration / 1e9 : (performance.now() - tStart) / 1000;
          }
        } else {
          // Heavy MoE OpenAI-compatible SSE
          const choice = obj.choices && obj.choices[0];
          if (choice) {
            const delta = choice.delta || choice.message || {};
            const rPart = delta.reasoning_content || delta.reasoning || '';
            const cPart = delta.content || '';
            if (rPart) {
              if (tFirstToken === null) tFirstToken = performance.now();
              explicitThinking += rPart;
              clientOutChunks++;
            }
            if (cPart) {
              if (tFirstToken === null) tFirstToken = performance.now();
              rawContent += cPart;
              clientOutChunks++;
            }
            if (rPart || cPart) {
              updateAssistantDom(false);
            }
          }
          if (obj.usage) {
            if (obj.usage.prompt_tokens != null) finalMetrics.inTokens = obj.usage.prompt_tokens;
            if (obj.usage.completion_tokens != null) finalMetrics.outTokens = obj.usage.completion_tokens;
          }
          if (obj.timings) {
            if (obj.timings.prompt_per_second != null) finalMetrics.prefillTps = obj.timings.prompt_per_second;
            if (obj.timings.predicted_per_second != null) finalMetrics.decodeTps = obj.timings.predicted_per_second;
            if (obj.timings.prompt_n != null && finalMetrics.inTokens == null) finalMetrics.inTokens = obj.timings.prompt_n;
            if (obj.timings.predicted_n != null && finalMetrics.outTokens == null) finalMetrics.outTokens = obj.timings.predicted_n;
          }
        }
      };

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const ln of lines) {
          processLine(ln);
          if (sseDone) break;
        }
        if (sseDone) break;
      }
      if (buffer.trim() && !sseDone) {
        processLine(buffer);
      }

      const tEnd = performance.now();
      finalMetrics.totalSec = finalMetrics.totalSec ?? (tEnd - tStart) / 1000;
      finalMetrics.ttftSec = tFirstToken !== null ? (tFirstToken - tStart) / 1000 : finalMetrics.totalSec;

      if (backend === 'heavy') {
        const outCount = finalMetrics.outTokens != null ? finalMetrics.outTokens : clientOutChunks;
        finalMetrics.outTokens = outCount;
        if (finalMetrics.inTokens == null) {
          const promptChars = apiMessages.reduce((acc, m) => acc + (m.content || '').length, 0);
          finalMetrics.inTokens = Math.max(1, Math.round(promptChars / 4));
        }
        const genSec = tFirstToken !== null ? Math.max((tEnd - tFirstToken) / 1000, 0.001) : Math.max(finalMetrics.totalSec, 0.001);
        if (finalMetrics.decodeTps == null && outCount > 0) {
          finalMetrics.decodeTps = outCount / genSec;
        }
        if (finalMetrics.prefillTps == null && finalMetrics.ttftSec > 0.005) {
          finalMetrics.prefillTps = finalMetrics.inTokens / finalMetrics.ttftSec;
        }
      }

      const rendered = updateAssistantDom(true);
      ui.statsEl.style.display = 'flex';
      ui.statsEl.textContent = formatStatsLine(finalMetrics);

      state.history.push({
        role: 'assistant',
        content: rendered.visible,
        thinking: rendered.combinedThink,
        stats: finalMetrics,
        model,
        backend,
        timestamp: new Date().toISOString()
      });
    } catch (err) {
      const rendered = updateAssistantDom(true);
      if (err.name === 'AbortError') {
        const note = rendered.visible ? rendered.visible + '\n\n*[Interrotto dall’utente]*' : '*[Interrotto dall’utente]*';
        ui.bodyEl.innerHTML = renderMarkdown(note);
        ui.wrap.dataset.rawCopy = rendered.visible;
        if (rendered.visible) {
          state.history.push({
            role: 'assistant',
            content: rendered.visible,
            thinking: rendered.combinedThink,
            stats: null,
            model,
            backend,
            timestamp: new Date().toISOString()
          });
        }
      } else {
        ui.bodyEl.innerHTML = renderMarkdown(`**Errore:** ${err.message}`);
      }
    } finally {
      state.abortCtrl = null;
      sendBtn.style.display = 'inline-flex';
      stopBtn.style.display = 'none';
      regenBtn.disabled = false;
      updateChatHeader();
    }
  }

  async function sendMessage() {
    const text = promptInput.value.trim();
    if (!text || state.abortCtrl) return;
    if (!modelSelect.value) {
      createMessageCard('assistant', 'Sistema', 'Seleziona o avvia un backend prima di inviare un messaggio.');
      return;
    }
    promptInput.value = '';
    createMessageCard('user', 'Tu', text);
    state.history.push({
      role: 'user',
      content: text,
      timestamp: new Date().toISOString()
    });
    updateChatHeader();
    await streamChatTurn();
  }

  async function regenerateLast() {
    if (state.abortCtrl || state.history.length === 0) return;
    if (state.history[state.history.length - 1].role === 'assistant') {
      state.history.pop();
    }
    // Remove trailing assistant DOM element(s) after the last user message
    while (chatMessages.lastElementChild && chatMessages.lastElementChild.classList.contains('assistant')) {
      chatMessages.lastElementChild.remove();
    }
    if (state.history.length === 0) return;
    await streamChatTurn();
  }

  sendBtn.addEventListener('click', sendMessage);
  stopBtn.addEventListener('click', () => state.abortCtrl && state.abortCtrl.abort());
  regenBtn.addEventListener('click', regenerateLast);

  promptInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  $('clearBtn').addEventListener('click', () => {
    if (state.abortCtrl) state.abortCtrl.abort();
    state.history = [];
    chatMessages.innerHTML = '';
    updateChatHeader();
  });

  function triggerDownload(filename, content, mime) {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  $('exportMdBtn').addEventListener('click', () => {
    if (state.history.length === 0) return;
    const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    const md = state.history.map((m) => {
      const thinkSec = m.thinking ? `<details><summary>Ragionamento</summary>\n\n${m.thinking}\n\n</details>\n\n` : '';
      const statsSec = m.stats ? `\n\n> *${formatStatsLine(m.stats)}*\n` : '';
      return `## ${m.role.toUpperCase()}${m.model ? ' (' + m.model + ')' : ''}\n\n${thinkSec}${m.content}${statsSec}\n`;
    }).join('\n---\n\n');
    triggerDownload(`local-llm-chat-${ts}.md`, md, 'text/markdown;charset=utf-8');
  });

  $('exportJsonBtn').addEventListener('click', () => {
    if (state.history.length === 0) return;
    const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    const exportObj = {
      exportedAt: new Date().toISOString(),
      backend: state.backend,
      model: modelSelect.value,
      systemPrompt: systemPromptEl.value,
      messages: state.history
    };
    triggerDownload(`local-llm-chat-${ts}.json`, JSON.stringify(exportObj, null, 2), 'application/json;charset=utf-8');
  });

  // --- Tab 3: Registry & Quick Benchmark ---
  function renderSimpleTable(headers, rows) {
    if (!headers || headers.length === 0) return '<div style="padding:12px;color:var(--muted);">Nessun dato.</div>';
    let html = '<table><thead><tr>';
    for (const h of headers) html += `<th>${escapeHtml(h)}</th>`;
    html += '</tr></thead><tbody>';
    if (!rows || rows.length === 0) {
      html += `<tr><td colspan="${headers.length}" style="color:var(--muted);">Nessuna voce trovata.</td></tr>`;
    } else {
      for (const r of rows) {
        html += '<tr>';
        for (let i = 0; i < headers.length; i++) {
          html += `<td>${renderInlineMarkdown(escapeHtml(r[i] || ''))}</td>`;
        }
        html += '</tr>';
      }
    }
    html += '</tbody></table>';
    return html;
  }

  async function fetchRegistry() {
    try {
      const res = await apiFetch('/api/registry/local');
      const data = await res.json();
      state.registry = data;

      // Ollama Table
      const ol = data.ollama || {};
      $('regOllamaStatus').textContent = ol.online ? `${(ol.models || []).length} modelli` : 'Offline';
      $('regOllamaStatus').className = 'pill ' + (ol.online ? 'online' : 'offline');
      const olRows = (ol.models || []).map((m) => [
        m.name,
        `${m.sizeGb} GiB`,
        (m.details && m.details.quantization_level) || '--',
        (m.details && m.details.parameter_size) || '--'
      ]);
      $('regOllamaTableWrap').innerHTML = renderSimpleTable(['Modello', 'Dimensione', 'Quant', 'Parametri'], olRows);

      // Heavy MoE Table
      const hv = data.heavyMoe || {};
      const hvFiles = hv.files || [];
      $('regHeavyStatus').textContent = `${hvFiles.length} file (${hv.totalSizeGb || 0} GiB)`;
      $('regHeavyStatus').className = 'pill ' + (hvFiles.length > 0 ? 'info' : '');
      if (hv.activeModel) {
        const am = hv.activeModel;
        $('regHeavyActiveInfo').textContent = `ACTIVE_MODEL.json: ${am.model || am.name || am.id || '--'} | Quant: ${am.quant || am.quantization || '--'} | Backend: ${am.backend || am.engine || 'Colibri'}`;
      } else {
        $('regHeavyActiveInfo').textContent = 'ACTIVE_MODEL.json non presente in models/heavy-moe/';
      }
      const hvRows = hvFiles.map((f) => [f.relativePath || f.name, `${f.sizeGb} GiB`]);
      $('regHeavyFilesWrap').innerHTML = renderSimpleTable(['File GGUF', 'Dimensione'], hvRows);

      // Benchmarks table
      if (data.benchmarkTable) {
        $('benchHistoryTableWrap').innerHTML = renderSimpleTable(data.benchmarkTable.headers, data.benchmarkTable.rows);
      } else {
        $('benchHistoryTableWrap').innerHTML = '<div style="padding:12px;color:var(--muted);">Nessuna tabella benchmark trovata.</div>';
      }
      $('benchHistoryRaw').textContent = data.benchmarksMarkdown || '';

      // Registry Markdown
      $('registryMdRendered').innerHTML = renderMarkdown(data.registryMarkdown || '*REGISTRY.md vuoto.*');
    } catch (_) {}
  }

  $('refreshRegistryBtn').addEventListener('click', fetchRegistry);

  $('toggleRawBenchBtn').addEventListener('click', () => {
    const rawEl = $('benchHistoryRaw');
    const showing = rawEl.style.display !== 'none';
    rawEl.style.display = showing ? 'none' : 'block';
    $('toggleRawBenchBtn').textContent = showing ? 'Mostra Markdown Grezzo' : 'Nascondi Markdown Grezzo';
  });

  function updateBenchTargetLabel() {
    const bk = state.backend === 'ollama' ? 'Fast Tier · Ollama (:11434)' : 'Heavy MoE · Colibrì (:11435)';
    const model = modelSelect.value || '--';
    $('benchTargetLabel').textContent = `Target: ${bk} · ${model}`;
  }

  $('runQuickBenchBtn').addEventListener('click', async () => {
    if (state.benchRunning) return;
    const model = modelSelect.value;
    if (!model) {
      $('benchStatusText').textContent = 'Nessun modello selezionato o backend offline.';
      return;
    }

    state.benchRunning = true;
    $('runQuickBenchBtn').disabled = true;
    $('copyBenchRowBtn').disabled = true;
    $('benchStatusText').textContent = 'Esecuzione benchmark in corso...';
    $('benchOutputBox').textContent = '';

    const benchPrompt =
      'Spiega in italiano tecnico conciso (massimo 5 punti elenco) come funziona il KV Cache quantization q8_0 nei modelli LLM locali, poi scrivi una funzione Python pura di 6 righe per stimare i GiB di RAM necessari.';

    const backend = state.backend;
    const ctxVal = parseInt(ctxInput.value, 10) || 16384;
    let peakVram = 0;
    let peakRam = 0;

    const sampleTelemetry = async () => {
      const t = await fetchTelemetry();
      if (t && t.gpu && t.gpu.available) peakVram = Math.max(peakVram, t.gpu.memoryUsedGb || 0);
      if (t && t.ram) peakRam = Math.max(peakRam, t.ram.usedGb || 0);
    };
    await sampleTelemetry();
    const pollId = setInterval(sampleTelemetry, 800);

    const tStart = performance.now();
    let tFirst = null;
    let outText = '';
    let outChunks = 0;
    let prefillTps = null;
    let decodeTps = null;
    let inTok = null;
    let outTok = null;

    try {
      const url = backend === 'ollama' ? '/api/ollama/chat' : '/api/heavy/chat/completions';
      const payload = backend === 'ollama'
        ? {
            model,
            messages: [{ role: 'user', content: benchPrompt }],
            stream: true,
            think: false,
            options: { temperature: 0.2, num_ctx: ctxVal, num_predict: 384 }
          }
        : {
            model,
            messages: [{ role: 'user', content: benchPrompt }],
            stream: true,
            temperature: 0.2,
            max_tokens: 384,
            enable_thinking: false,
            reasoning_effort: 'none'
          };

      const res = await apiFetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        const errTxt = await res.text();
        throw new Error(errTxt);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const rawLine of lines) {
          const line = rawLine.replace(/\r$/, '').trim();
          if (!line || line.startsWith(':')) continue;
          let jStr = line.startsWith('data:') ? line.slice(5).trim() : line;
          if (jStr === '[DONE]') continue;
          let obj;
          try { obj = JSON.parse(jStr); } catch (_) { continue; }

          if (backend === 'ollama') {
            if (obj.message && obj.message.content) {
              if (tFirst === null) tFirst = performance.now();
              outText += obj.message.content;
              $('benchOutputBox').textContent = outText;
            }
            if (obj.done) {
              inTok = obj.prompt_eval_count || 0;
              outTok = obj.eval_count || 0;
              if (obj.prompt_eval_duration) prefillTps = inTok / (obj.prompt_eval_duration / 1e9);
              if (obj.eval_duration) decodeTps = outTok / (obj.eval_duration / 1e9);
            }
          } else {
            const delta = (obj.choices && obj.choices[0] && (obj.choices[0].delta || obj.choices[0].message)) || {};
            const part = delta.content || '';
            if (part) {
              if (tFirst === null) tFirst = performance.now();
              outText += part;
              outChunks++;
              $('benchOutputBox').textContent = outText;
            }
            if (obj.usage) {
              if (obj.usage.prompt_tokens != null) inTok = obj.usage.prompt_tokens;
              if (obj.usage.completion_tokens != null) outTok = obj.usage.completion_tokens;
            }
            if (obj.timings) {
              if (obj.timings.prompt_per_second != null) prefillTps = obj.timings.prompt_per_second;
              if (obj.timings.predicted_per_second != null) decodeTps = obj.timings.predicted_per_second;
            }
          }
        }
      }

      await sampleTelemetry();
      const tEnd = performance.now();
      const ttftSec = tFirst !== null ? (tFirst - tStart) / 1000 : (tEnd - tStart) / 1000;
      const genSec = tFirst !== null ? Math.max((tEnd - tFirst) / 1000, 0.001) : Math.max((tEnd - tStart) / 1000, 0.001);
      if (backend === 'heavy') {
        if (inTok == null) inTok = Math.round(benchPrompt.length / 4);
        if (outTok == null) outTok = outChunks;
        if (prefillTps == null && ttftSec > 0.005) prefillTps = inTok / ttftSec;
        if (decodeTps == null && outTok > 0) decodeTps = outTok / genSec;
      }

      const today = new Date().toISOString().slice(0, 10);
      const runtimeName = backend === 'ollama' ? 'Ollama (Fast Tier)' : 'Colibri (Heavy MoE)';
      const hvMeta = state.telemetry?.backends?.heavyMoe || {};
      const olRegList = state.registry?.ollama?.models || [];
      const olRegMatch = olRegList.find((m) => m.name === model);
      const olQuant = (olRegMatch && olRegMatch.details && olRegMatch.details.quantization_level) || 'UD-IQ4_XS';
      const quantLabel = backend === 'heavy' ? (hvMeta.quant || 'GGUF') : olQuant;
      const prefillStr = prefillTps != null ? prefillTps.toFixed(1) : '--';
      const decodeStr = decodeTps != null ? `**${decodeTps.toFixed(1)}**` : '--';
      const vramStr = peakVram > 0 ? peakVram.toFixed(2) : '--';
      const offloadStr = backend === 'ollama' ? 'Fast Tier' : `Heavy MoE (RAM ${peakRam.toFixed(1)} GiB)`;
      const row = `| ${today} | \`${model}\` | \`${quantLabel}\` | ${runtimeName} | ${ctxVal} | ${prefillStr} | ${decodeStr} | ${vramStr} | ${offloadStr} | Benchmark rapido UI (TTFT ${ttftSec.toFixed(2)}s, ${outTok ?? '--'} tok) |`;

      state.lastBenchRow = row;
      $('benchMarkdownRow').value = row;
      $('copyBenchRowBtn').disabled = false;
      $('benchStatusText').textContent = `Completato · TTFT ${ttftSec.toFixed(2)}s · Decode ${decodeTps ? decodeTps.toFixed(1) : '--'} tok/s · VRAM picco ${vramStr} GiB`;
    } catch (err) {
      $('benchOutputBox').textContent = `Errore benchmark: ${err.message}`;
      $('benchStatusText').textContent = 'Errore';
    } finally {
      clearInterval(pollId);
      state.benchRunning = false;
      $('runQuickBenchBtn').disabled = false;
    }
  });

  $('copyBenchRowBtn').addEventListener('click', () => {
    if (state.lastBenchRow) copyTextToClipboard(state.lastBenchRow, $('copyBenchRowBtn'));
  });

  // --- Tab 5: Autonomous Orchestrator ---
  const ORCH_ACTIVE_STATES = new Set([
    'STARTING', 'RUNNING', 'SCOUT', 'STRATEGIST', 'INVESTIGATOR', 'VALIDATOR', 'ADJUDICATOR', 'PAUSED'
  ]);
  const ORCH_FIXTURE_PATH = './workspaces/orchestrator/tests/fixtures/sample_vulnerable_app';

  function formatDurationMMSS(totalSec) {
    if (totalSec == null || !Number.isFinite(Number(totalSec))) return '--';
    const sec = Math.max(0, Math.floor(Number(totalSec)));
    const hrs = Math.floor(sec / 3600);
    const mins = Math.floor((sec % 3600) / 60);
    const rem = sec % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return hrs > 0 ? `${pad(hrs)}:${pad(mins)}:${pad(rem)}` : `${pad(mins)}:${pad(rem)}`;
  }

  function extractVerdictCode(verdict) {
    if (!verdict) return null;
    if (typeof verdict === 'string') return verdict.toUpperCase();
    if (typeof verdict === 'object') {
      const code = verdict.code || verdict.outcome || verdict.status || verdict.verdict;
      return code ? String(code).toUpperCase() : null;
    }
    return String(verdict).toUpperCase();
  }

  function extractVerdictDetail(verdict) {
    if (!verdict || typeof verdict !== 'object') return '';
    const parts = [];
    if (verdict.impossible_category || verdict.category || verdict.impossibility_category) {
      parts.push(verdict.impossible_category || verdict.category || verdict.impossibility_category);
    }
    if (verdict.summary || verdict.reason) {
      parts.push(verdict.summary || verdict.reason);
    }
    return parts.join(' — ');
  }

  function fsmPillClass(fsmState) {
    const s = String(fsmState || 'IDLE').toUpperCase();
    if (s === 'OBJECTIVE_ACHIEVED' || s === 'DONE') return 'pill online';
    if (s === 'PAUSED' || s === 'HALTED_CIRCUIT_BREAKER' || s === 'HALTED' || s === 'IMPOSSIBLE_PROVEN' || s === 'IMPOSSIBLE') return 'pill warn';
    if (s === 'ERROR' || s === 'UNAVAILABLE') return 'pill offline';
    if (ORCH_ACTIVE_STATES.has(s)) return 'pill cyan';
    return 'pill';
  }

  function getSelectedRetentionMode() {
    return $('orchRetentionPersistent') && $('orchRetentionPersistent').checked ? 'persistent' : 'ephemeral';
  }

  function syncWizardPolicyUI() {
    const locked = Boolean(state.orchestrator.active);
    const mode = getSelectedRetentionMode();
    const isEphemeral = mode === 'ephemeral';

    const cardEph = $('orchPolicyCardEphemeral');
    const cardPer = $('orchPolicyCardPersistent');
    if (cardEph) {
      cardEph.classList.toggle('selected', isEphemeral);
      cardEph.classList.toggle('locked', locked);
    }
    if (cardPer) {
      cardPer.classList.toggle('selected', !isEphemeral);
      cardPer.classList.toggle('locked', locked);
    }

    const lockBanner = $('orchWizardLockBanner');
    if (lockBanner) lockBanner.style.display = locked ? 'block' : 'none';

    const kvCheck = $('orchAllowKvSave');
    const kvRow = $('orchAllowKvSaveRow');
    const kvLabel = $('orchAllowKvSaveLabel');
    if (kvCheck && kvRow) {
      kvCheck.checked = false;
      kvCheck.disabled = true;
      kvRow.classList.add('disabled');
      if (kvLabel) {
        kvLabel.textContent = isEphemeral
          ? 'KV Cache Colibrì disabilitata (bloccata OFF in Ephemeral)'
          : 'KV Cache Colibrì disabilitata (Colibrì v2.1.0 non supporta isolamento per-vault)';
      }
    }

    const autoPurgeEl = $('orchAutoPurgeMin');
    if (autoPurgeEl) {
      autoPurgeEl.disabled = locked || !isEphemeral;
    }

    const lockIds = [
      'orchRetentionEphemeral', 'orchRetentionPersistent', 'orchEmbedPoc',
      'orchTargetPath', 'orchMission', 'orchEngineMode', 'orchFastModel',
      'orchMaxSteps', 'orchMaxEscalations', 'orchNoProgressLimit',
      'btnOrchStart', 'btnOrchFillFixture'
    ];
    for (const id of lockIds) {
      const el = $(id);
      if (el) el.disabled = locked;
    }
  }

  async function populateOrchFastModels() {
    const sel = $('orchFastModel');
    if (!sel) return;
    const prev = sel.value;
    let models = [];
    try {
      const res = await apiFetch('/api/ollama/tags');
      if (res.ok) {
        const data = await res.json();
        models = (data.models || []).map((m) => ({
          name: m.name,
          sizeGb: m.size ? (m.size / (1024 ** 3)).toFixed(1) : null
        })).filter((m) => m.name);
      }
    } catch (_) {}

    if (models.length === 0 && state.telemetry?.backends?.ollama?.models?.length) {
      models = state.telemetry.backends.ollama.models.map((name) => ({ name, sizeGb: null }));
    }

    const rankOrchModel = (name) => {
      if (name.startsWith('local-worker')) return 3;
      if (name.startsWith('local-uncensored')) return 2;
      return 1;
    };
    models.sort((a, b) => rankOrchModel(b.name) - rankOrchModel(a.name));

    sel.innerHTML = '';
    if (models.length === 0) {
      const opt = document.createElement('option');
      opt.value = 'local-worker:latest';
      opt.textContent = 'local-worker:latest (Ollama offline · auto-start)';
      sel.appendChild(opt);
    } else {
      for (const m of models) {
        const opt = document.createElement('option');
        opt.value = m.name;
        opt.textContent = m.sizeGb ? `${m.name} (${m.sizeGb} GiB)` : m.name;
        sel.appendChild(opt);
      }
    }

    if (prev && Array.from(sel.options).some((o) => o.value === prev)) {
      sel.value = prev;
    } else if (Array.from(sel.options).some((o) => o.value === 'local-worker:latest')) {
      sel.value = 'local-worker:latest';
    }
  }

  function clearOrchestratorReportView(noticeText) {
    state.orchestrator.reportMarkdown = '';
    state.orchestrator.reportRunId = null;
    state.orchestrator.reportVerdict = null;
    state.orchestrator.reportSource = null;
    state.orchestrator.reportLoadedKey = null;

    const verdictBadge = $('orchVerdictBadge');
    if (verdictBadge) {
      verdictBadge.textContent = 'Nessun Report';
      verdictBadge.className = 'pill';
    }
    const srcBadge = $('orchReportSourceBadge');
    if (srcBadge) srcBadge.style.display = 'none';

    const metaEl = $('orchReportMeta');
    if (metaEl) metaEl.textContent = noticeText || 'Nessun report caricato in memoria.';

    const box = $('orchReportRendered');
    if (box) {
      box.innerHTML = `<div style="color:var(--muted);">${escapeHtml(noticeText || 'Avvia una missione oppure apri un archivio Persistent salvato per visualizzare il Report Finale qui.')}</div>`;
    }

    if ($('btnOrchPurgeReport')) $('btnOrchPurgeReport').disabled = !state.orchestrator.hasReport;
    if ($('btnOrchExportMd')) $('btnOrchExportMd').disabled = true;
    if ($('btnOrchCopyReport')) $('btnOrchCopyReport').disabled = true;
  }

  function renderOrchestratorReport(markdown, opts) {
    const md = String(markdown || '');
    const options = opts || {};
    state.orchestrator.reportMarkdown = md;
    state.orchestrator.reportRunId = options.runId || null;
    state.orchestrator.reportVerdict = options.verdict || null;
    state.orchestrator.reportSource = options.source || 'live_ephemeral';

    const vCode = extractVerdictCode(options.verdict) || 'REPORT PRONTO';
    const vDetail = extractVerdictDetail(options.verdict);
    const verdictBadge = $('orchVerdictBadge');
    if (verdictBadge) {
      verdictBadge.textContent = vCode;
      verdictBadge.className = fsmPillClass(vCode);
    }

    const srcBadge = $('orchReportSourceBadge');
    if (srcBadge) {
      srcBadge.style.display = 'inline-flex';
      if (options.source === 'vault' || options.retentionMode === 'persistent') {
        srcBadge.textContent = 'PERSISTENT · VAULT';
        srcBadge.className = 'pill warn';
      } else {
        srcBadge.textContent = 'EPHEMERAL · SOLO RAM';
        srcBadge.className = 'pill online';
      }
    }

    const metaParts = [];
    if (options.runId) metaParts.push(`Run: ${options.runId}`);
    metaParts.push(`${md.length} car.`);
    if (vDetail) metaParts.push(vDetail);
    if ($('orchReportMeta')) $('orchReportMeta').textContent = metaParts.join(' · ');

    const box = $('orchReportRendered');
    if (box) {
      box.innerHTML = md.trim()
        ? renderMarkdown(md)
        : '<div style="color:var(--muted);">Report vuoto.</div>';
    }

    if ($('btnOrchPurgeReport')) $('btnOrchPurgeReport').disabled = false;
    if ($('btnOrchExportMd')) $('btnOrchExportMd').disabled = !md.trim();
    if ($('btnOrchCopyReport')) $('btnOrchCopyReport').disabled = !md.trim();
  }

  function renderOrchestratorHypotheses(hyps) {
    const wrap = $('orchHypothesesWrap');
    const countEl = $('orchHypCountLabel');
    const list = Array.isArray(hyps) ? hyps : [];
    if (countEl) countEl.textContent = `${list.length} ipotesi`;
    if (!wrap) return;
    if (list.length === 0) {
      wrap.innerHTML = '<div style="padding:12px; color:var(--muted);">Nessuna ipotesi generata al momento.</div>';
      return;
    }
    let html = '<table><thead><tr><th style="width:76px;">ID</th><th>Titolo / Ipotesi</th><th style="width:120px;">Stato</th><th>Evidenza</th></tr></thead><tbody>';
    for (const h of list) {
      const id = h.id || h.hypothesis_id || '--';
      const title = h.title || h.statement || h.description || '--';
      const status = String(h.status || 'OPEN').toUpperCase();
      const badgeClass = ['OPEN', 'TESTING', 'CONFIRMED', 'REFUTED', 'ESCALATE', 'BLOCKED'].includes(status) ? status : 'OPEN';
      const ev = h.evidence || h.evidence_summary || h.notes || ((h.sandbox_verified || h.poc_verified) ? 'PoC verificato in sandbox' : '--');
      html += '<tr>' +
        `<td class="mono">${escapeHtml(id)}</td>` +
        `<td>${escapeHtml(title)}</td>` +
        `<td><span class="hyp-badge ${badgeClass}">${escapeHtml(status)}</span></td>` +
        `<td style="font-size:12px; color:var(--muted);">${escapeHtml(ev)}</td>` +
        '</tr>';
    }
    html += '</tbody></table>';
    wrap.innerHTML = html;
  }

  function formatOrchEventLine(ev) {
    if (typeof ev === 'string') return ev;
    if (!ev || typeof ev !== 'object') return '';
    const tsRaw = ev.ts || ev.timestamp || ev.time || '';
    const tsShort = tsRaw ? String(tsRaw).replace(/^.*T/, '').replace(/Z$/, '').slice(0, 8) : '';
    const phase = ev.phase || ev.state || ev.type || ev.event || '';
    const msg = ev.message || ev.detail || ev.summary || ev.tool || '';
    const prefix = [tsShort, phase].filter(Boolean).join(' ');
    return prefix ? `[${prefix}] ${msg}` : msg;
  }

  function renderOrchestratorStatus(st) {
    const prevActive = state.orchestrator.active;
    const prevRetention = state.orchestrator.retentionMode;
    state.orchestrator.status = st;
    state.orchestrator.available = st.available !== false;

    const fsmState = String(st.state || st.phase || 'IDLE').toUpperCase();
    const retention = st.retention_mode ?? st.retentionMode ?? null;
    const active = typeof st.active === 'boolean' ? st.active : ORCH_ACTIVE_STATES.has(fsmState);
    const hasReport = Boolean(st.has_report ?? st.hasReport ?? false);
    const runId = st.run_id ?? st.runId ?? null;

    state.orchestrator.stateName = fsmState;
    state.orchestrator.retentionMode = retention;
    state.orchestrator.active = active;
    state.orchestrator.hasReport = hasReport;
    state.orchestrator.runId = runId;

    syncOperatorProfileUI();
    syncWizardPolicyUI();

    // Sync wizard policy selector when a run is active so UI reflects locked policy
    if (active && retention) {
      const rEph = $('orchRetentionEphemeral');
      const rPer = $('orchRetentionPersistent');
      if (rEph && rPer) {
        rEph.checked = retention === 'ephemeral';
        rPer.checked = retention === 'persistent';
        syncWizardPolicyUI();
      }
    }

    // Dashboard Header Badges
    const fsmBadge = $('orchFsmBadge');
    if (fsmBadge) {
      fsmBadge.textContent = fsmState;
      fsmBadge.className = fsmPillClass(fsmState);
    }

    const polBadge = $('orchPolicyBadge');
    if (polBadge) {
      if (retention === 'ephemeral') {
        polBadge.textContent = 'EPHEMERAL · ZERO DISCO';
        polBadge.className = 'pill online';
      } else if (retention === 'persistent') {
        polBadge.textContent = 'PERSISTENT · VAULT';
        polBadge.className = 'pill warn';
      } else {
        polBadge.textContent = 'IDLE';
        polBadge.className = 'pill';
      }
    }

    if ($('orchRunIdLabel')) {
      $('orchRunIdLabel').textContent = runId ? `Run: ${runId}` : 'Run: --';
    }

    // Pause / Resume / Stop buttons
    const btnPause = $('btnOrchPause');
    const btnResume = $('btnOrchResume');
    const btnStop = $('btnOrchStop');
    const isEphemeral = retention === 'ephemeral';
    const isPaused = fsmState === 'PAUSED';

    if (btnPause) {
      if (isEphemeral) {
        btnPause.disabled = true;
        btnPause.title = 'Pausa disabilitata in Ephemeral Privacy (Zero Disco): richiede checkpoint Persistent.';
      } else {
        btnPause.disabled = !(active && !isPaused);
        btnPause.title = 'Metti in pausa la run Persistent salvando il checkpoint corrente';
      }
    }
    if (btnResume) {
      if (isEphemeral) {
        btnResume.disabled = true;
        btnResume.title = 'Ripresa disabilitata in Ephemeral Privacy (Zero Disco): nessuno stato salvato su disco.';
      } else {
        btnResume.disabled = !isPaused;
        btnResume.title = 'Riprendi la run Persistent dal checkpoint';
      }
    }
    if (btnStop) {
      btnStop.disabled = !active;
    }

    // Status note line
    const ctrlMsg = $('orchControlMsg');
    if (ctrlMsg) {
      if (st.available === false) {
        ctrlMsg.textContent = st.error || 'Modulo Orchestrator non caricato sul backend.';
      } else if (st.error) {
        ctrlMsg.textContent = `Avviso/Errore: ${st.error}`;
      } else if (active) {
        ctrlMsg.textContent = isPaused
          ? `Run in PAUSA al checkpoint step ${st.step ?? st.current_step ?? 0}. Premi Riprendi o seleziona dal Vault.`
          : `Run in esecuzione (${fsmState}) · Policy immutabile: ${retention || '--'}`;
      } else if (hasReport) {
        ctrlMsg.textContent = retention === 'ephemeral'
          ? 'Run completata. Buffer di lavoro purgato (Rolling Purge). Solo il Report Finale è in RAM.'
          : 'Run completata e archiviata nel Persistent Vault.';
      } else {
        ctrlMsg.textContent = 'Nessuna run attiva. Configura una missione nel Wizard qui sopra.';
      }
    }

    // Metrics Strip
    const budget = (st.budget && typeof st.budget === 'object') ? st.budget : {};
    const step = Number(st.step ?? st.current_step ?? 0);
    const maxSteps = Number(st.max_steps ?? st.maxSteps ?? budget.max_steps ?? 0);
    if ($('orchMetricStep')) $('orchMetricStep').textContent = `${step} / ${maxSteps || '--'}`;

    const activeEngine = st.active_engine ?? st.activeEngine ?? st.current_engine ?? '--';
    if ($('orchMetricEngine')) $('orchMetricEngine').textContent = String(activeEngine || '--');

    const elapsedSec = st.elapsed_sec ?? st.elapsedSec ?? st.elapsed_seconds ?? null;
    if ($('orchMetricElapsed')) $('orchMetricElapsed').textContent = formatDurationMMSS(elapsedSec);

    let sandboxTxt = '--';
    if (st.sandbox_status || st.sandboxStatus) {
      sandboxTxt = String(st.sandbox_status || st.sandboxStatus);
    } else if (typeof st.sandbox_active === 'boolean') {
      sandboxTxt = st.sandbox_active ? 'Container Attivo' : 'Pulito (0 container)';
    } else if (typeof st.docker_available === 'boolean') {
      sandboxTxt = st.docker_available ? 'Docker Ready' : 'Docker OFF';
    }
    if ($('orchMetricSandbox')) $('orchMetricSandbox').textContent = sandboxTxt;

    const escUsed = Number(st.heavy_escalations ?? st.heavyEscalations ?? st.escalations_used ?? 0);
    const escMax = Number(st.max_heavy_escalations ?? st.maxHeavyEscalations ?? budget.max_heavy_escalations ?? 0);
    if ($('orchMetricEscalations')) $('orchMetricEscalations').textContent = `${escUsed} / ${escMax || '--'}`;

    const deadmanSec = st.auto_purge_remaining_sec ?? st.autoPurgeRemainingSec ?? null;
    const deadmanEl = $('orchMetricDeadman');
    if (deadmanEl) {
      if (retention === 'ephemeral' && deadmanSec != null) {
        deadmanEl.textContent = formatDurationMMSS(deadmanSec);
        deadmanEl.style.color = deadmanSec <= 300 ? 'var(--amber)' : 'var(--accent)';
      } else if (retention === 'persistent') {
        deadmanEl.textContent = 'N/A (Vault)';
        deadmanEl.style.color = 'var(--muted)';
      } else {
        deadmanEl.textContent = '--';
        deadmanEl.style.color = 'var(--text)';
      }
    }

    // Hypotheses Table
    renderOrchestratorHypotheses(st.hypotheses);

    // Evidence Ledger
    const ledger = st.evidence_ledger ?? st.evidenceLedger ?? st.ledger ?? '';
    const ledgerStr = typeof ledger === 'string' ? ledger : JSON.stringify(ledger, null, 2);
    if ($('orchLedgerBox')) {
      $('orchLedgerBox').textContent = ledgerStr.trim() || 'Nessuna evidenza registrata.';
    }
    if ($('orchLedgerMeta')) {
      const approxTok = Math.round(ledgerStr.length / 4);
      $('orchLedgerMeta').textContent = `${ledgerStr.length} car. (~${approxTok} tok)`;
    }

    // Live Events
    const events = Array.isArray(st.events)
      ? st.events
      : (Array.isArray(st.recent_events) ? st.recent_events : []);
    if ($('orchEventsCount')) $('orchEventsCount').textContent = `${events.length} eventi`;
    if ($('orchEventsBox')) {
      if (events.length === 0) {
        $('orchEventsBox').textContent = 'In attesa di eventi Orchestrator...';
      } else {
        const formatted = events.slice(-40).map(formatOrchEventLine).filter(Boolean).join('\n');
        $('orchEventsBox').textContent = formatted || 'In attesa di eventi Orchestrator...';
        $('orchEventsBox').scrollTop = $('orchEventsBox').scrollHeight;
      }
    }

    // Auto-load report when ready, or auto-wipe if RAM report was purged externally (Dead-Man's Switch / Tray)
    const reportKey = `${runId || 'live'}:${fsmState}:${st.report_updated_at || st.finished_at || ''}`;
    if (hasReport && state.orchestrator.reportLoadedKey !== reportKey) {
      state.orchestrator.reportLoadedKey = reportKey;
      fetchOrchestratorReport();
    } else if (!hasReport && state.orchestrator.reportSource === 'live_ephemeral') {
      clearOrchestratorReportView('Report in RAM distrutto (Wipe / Dead-Man\'s Switch).');
    }

    // Enable Wipe button if server reports a RAM report
    if ($('btnOrchPurgeReport') && hasReport) {
      $('btnOrchPurgeReport').disabled = false;
    }

    // Refresh vaults automatically when a persistent run finishes or pauses
    if (prevActive && !active && (prevRetention === 'persistent' || retention === 'persistent')) {
      fetchOrchestratorVaults();
    }
  }

  async function fetchOrchestratorStatus() {
    if (state.orchestrator.statusInFlight) return null;
    state.orchestrator.statusInFlight = true;
    try {
      const res = await apiFetch('/api/orchestrator/status');
      const data = await res.json();
      renderOrchestratorStatus(data || {});
      return data;
    } catch (err) {
      renderOrchestratorStatus({ available: false, state: 'UNAVAILABLE', error: `Backend non raggiungibile: ${err.message}` });
      return null;
    } finally {
      state.orchestrator.statusInFlight = false;
    }
  }

  async function fetchOrchestratorReport() {
    try {
      const res = await apiFetch('/api/orchestrator/report');
      const data = await res.json();
      if (!res.ok || data.ok === false) {
        return;
      }
      const md = data.report_md ?? data.report ?? data.final_report ?? data.markdown ?? '';
      if (md) {
        const rm = data.retention_mode || state.orchestrator.retentionMode || 'ephemeral';
        renderOrchestratorReport(md, {
          runId: data.run_id || state.orchestrator.runId,
          verdict: data.verdict || state.orchestrator.status?.verdict,
          retentionMode: rm,
          source: rm === 'persistent' ? 'live_persistent' : 'live_ephemeral'
        });
      }
    } catch (_) {}
  }

  async function fetchOrchestratorVaults() {
    const wrap = $('orchVaultsWrap');
    const msgEl = $('orchVaultsMsg');
    if (!wrap) return;
    try {
      const res = await apiFetch('/api/orchestrator/vaults');
      const data = await res.json();
      if (!res.ok || data.ok === false) {
        wrap.innerHTML = `<div style="padding:12px; color:var(--muted);">${escapeHtml(data.error || 'Impossibile leggere gli archivi.')}</div>`;
        return;
      }
      const vaults = Array.isArray(data.vaults) ? data.vaults : [];
      state.orchestrator.vaults = vaults;
      if (msgEl) msgEl.textContent = vaults.length ? `${vaults.length} archivi Persistent trovati.` : '';
      if (vaults.length === 0) {
        wrap.innerHTML = '<div style="padding:12px; color:var(--muted);">Nessun archivio Persistent presente in workspaces/vault/.</div>';
        return;
      }

      let html = '<table><thead><tr><th>Run ID</th><th>Data</th><th>Stato</th><th>Verdetto</th><th>Missione</th><th>Azioni</th></tr></thead><tbody>';
      for (const v of vaults) {
        const rid = v.run_id || v.id || '';
        const created = (v.created_at || v.updated_at || '--').replace('T', ' ').replace('Z', '');
        const stLabel = String(v.status || '--').toUpperCase();
        const vCode = extractVerdictCode(v.verdict) || '--';
        const missionPrev = v.mission_preview || v.mission || '--';
        const canResume = ['PAUSED', 'INTERRUPTED', 'STOPPED', 'INVESTIGATOR', 'STRATEGIST', 'SCOUT'].includes(stLabel) || (!v.has_report && Number(v.latest_step || 0) > 0);

        html += '<tr>' +
          `<td class="mono" style="font-size:11.5px;">${escapeHtml(rid)}</td>` +
          `<td class="mono" style="font-size:11.5px; white-space:nowrap;">${escapeHtml(created)}</td>` +
          `<td><span class="${fsmPillClass(stLabel)}" style="padding:1px 7px;font-size:10.5px;">${escapeHtml(stLabel)}</span></td>` +
          `<td class="mono" style="font-size:11.5px;">${escapeHtml(vCode)}</td>` +
          `<td style="font-size:12px; color:var(--muted); max-width:260px;">${escapeHtml(missionPrev)}</td>` +
          '<td style="white-space:nowrap;"><div style="display:flex; gap:6px; flex-wrap:wrap;">' +
            `<button type="button" class="btn sm" data-action="open-vault" data-run-id="${escapeHtml(rid)}">Apri Report</button>` +
            (canResume ? `<button type="button" class="btn sm success" data-action="resume-vault" data-run-id="${escapeHtml(rid)}">Riprendi</button>` : '') +
            `<button type="button" class="btn sm danger" data-action="delete-vault" data-run-id="${escapeHtml(rid)}">Elimina Archivio Run</button>` +
          '</div></td>' +
          '</tr>';
      }
      html += '</tbody></table>';
      wrap.innerHTML = html;
    } catch (err) {
      wrap.innerHTML = `<div style="padding:12px; color:var(--danger);">Errore lettura archivi: ${escapeHtml(err.message)}</div>`;
    }
  }

  async function startOrchestratorRun() {
    const msgEl = $('orchWizardMsg');
    const targetPath = ($('orchTargetPath')?.value || '').trim();
    const mission = ($('orchMission')?.value || '').trim();
    const retentionMode = getSelectedRetentionMode();

    if (!targetPath) {
      if (msgEl) msgEl.textContent = 'Inserisci una cartella target (montata in read_only).';
      return;
    }
    if (!mission) {
      if (msgEl) msgEl.textContent = 'Inserisci la missione / obiettivo da eseguire.';
      return;
    }

    const embedPoc = Boolean($('orchEmbedPoc')?.checked);
    const allowKvSave = retentionMode === 'persistent' ? Boolean($('orchAllowKvSave')?.checked) : false;
    const autoPurgeMin = Math.max(1, Math.min(240, parseInt($('orchAutoPurgeMin')?.value, 10) || 45));
    const maxSteps = Math.max(1, Math.min(120, parseInt($('orchMaxSteps')?.value, 10) || 30));
    const maxEscalations = Math.max(0, Math.min(10, parseInt($('orchMaxEscalations')?.value, 10) || 3));
    const noProgressLimit = Math.max(2, Math.min(8, parseInt($('orchNoProgressLimit')?.value, 10) || 3));
    const executionMode = $('orchEngineMode')?.value || 'dual_tier';
    const fastModel = ($('orchFastModel')?.value || '').trim() || 'local-worker:latest';

    const payload = {
      operator: 'orchestrator',
      retention_mode: retentionMode,
      mission,
      target_path: targetPath,
      target_access: 'read_only',
      execution_mode: executionMode,
      fast_model: fastModel,
      allow_kv_save: allowKvSave,
      embed_verified_poc_in_report: embedPoc,
      report_auto_purge_minutes: autoPurgeMin,
      ephemeral_rules: {
        rolling_purge_per_step: retentionMode === 'ephemeral',
        embed_verified_poc_in_report: embedPoc,
        report_auto_purge_minutes: autoPurgeMin,
        allow_kv_save: allowKvSave
      },
      budget: {
        max_steps: maxSteps,
        max_heavy_escalations: maxEscalations,
        no_progress_limit: noProgressLimit
      }
    };

    if ($('btnOrchStart')) $('btnOrchStart').disabled = true;
    if (msgEl) msgEl.textContent = `Avvio missione in modalità ${retentionMode.toUpperCase()}...`;

    try {
      const res = await apiFetch('/api/orchestrator/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!res.ok || data.ok === false) {
        if (msgEl) msgEl.textContent = `Errore avvio (${res.status}): ${data.error || 'Richiesta rifiutata.'}`;
        syncWizardPolicyUI();
        return;
      }
      state.orchestrator.reportLoadedKey = null;
      if (msgEl) msgEl.textContent = `Missione avviata (Run ID: ${data.run_id || '--'} · Policy bloccata: ${retentionMode.toUpperCase()}).`;
      await fetchOrchestratorStatus();
      if (retentionMode === 'persistent') fetchOrchestratorVaults();
    } catch (err) {
      if (msgEl) msgEl.textContent = `Errore rete locale: ${err.message}`;
      syncWizardPolicyUI();
    }
  }

  async function pauseOrchestratorRun() {
    const msgEl = $('orchControlMsg');
    try {
      const res = await apiFetch('/api/orchestrator/pause', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const data = await res.json();
      if (!res.ok || data.ok === false) {
        if (msgEl) msgEl.textContent = data.error || `Impossibile mettere in pausa (${res.status}).`;
      } else if (msgEl) {
        msgEl.textContent = data.message || 'Pausa richiesta: salvataggio checkpoint...';
      }
    } catch (err) {
      if (msgEl) msgEl.textContent = `Errore pausa: ${err.message}`;
    } finally {
      await fetchOrchestratorStatus();
    }
  }

  async function resumeOrchestratorRun(runId) {
    const msgEl = $('orchControlMsg');
    try {
      const payload = runId ? { run_id: runId } : {};
      const res = await apiFetch('/api/orchestrator/resume', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!res.ok || data.ok === false) {
        if (msgEl) msgEl.textContent = data.error || `Impossibile riprendere la run (${res.status}).`;
      } else if (msgEl) {
        msgEl.textContent = data.message || `Ripresa avviata${runId ? ' per ' + runId : ''}.`;
      }
    } catch (err) {
      if (msgEl) msgEl.textContent = `Errore ripresa: ${err.message}`;
    } finally {
      await fetchOrchestratorStatus();
      await fetchOrchestratorVaults();
    }
  }

  async function stopOrchestratorRun() {
    if (!confirm('Arrestare la missione Orchestrator attiva e distruggere eventuali container sandbox?')) return;
    const msgEl = $('orchControlMsg');
    try {
      const res = await apiFetch('/api/orchestrator/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const data = await res.json();
      if (msgEl) msgEl.textContent = data.message || data.error || 'Arresto run richiesto.';
    } catch (err) {
      if (msgEl) msgEl.textContent = `Errore arresto: ${err.message}`;
    } finally {
      await fetchOrchestratorStatus();
      await fetchOrchestratorVaults();
    }
  }

  async function purgeOrchestratorReport() {
    try {
      await apiFetch('/api/orchestrator/purge-report', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}'
      });
    } catch (_) {}
    state.orchestrator.hasReport = false;
    clearOrchestratorReportView('Report distrutto: buffer RAM azzerato sul server e DOM pulito.');
    await fetchOrchestratorStatus();
  }

  async function openOrchestratorVault(runId) {
    const msgEl = $('orchVaultsMsg');
    if (!runId) return;
    try {
      const res = await apiFetch('/api/orchestrator/vaults/' + encodeURIComponent(runId));
      const data = await res.json();
      if (!res.ok || data.ok === false) {
        if (msgEl) msgEl.textContent = data.error || `Impossibile aprire archivio ${runId}.`;
        return;
      }
      const detail = data.vault || data;
      const md = detail.final_report || detail.report_md || detail.report || '';
      if (md) {
        renderOrchestratorReport(md, {
          runId: detail.run_id || runId,
          verdict: detail.verdict,
          retentionMode: 'persistent',
          source: 'vault'
        });
      } else {
        const fallbackSummary = [
          `# Archivio Vault: \`${runId}\``,
          '',
          '*FINAL_REPORT.md non ancora generato per questa run (in corso, in pausa o interrotta).*',
          '',
          detail.ledger ? '## Evidence Ledger\n\n' + detail.ledger : ''
        ].join('\n');
        renderOrchestratorReport(fallbackSummary, {
          runId,
          verdict: detail.verdict || 'INCOMPLETO',
          retentionMode: 'persistent',
          source: 'vault'
        });
      }
      if (Array.isArray(detail.hypotheses) && !state.orchestrator.active) {
        renderOrchestratorHypotheses(detail.hypotheses);
      }
      if (msgEl) msgEl.textContent = `Caricato archivio Persistent: ${runId}`;
      $('orchReportCard')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (err) {
      if (msgEl) msgEl.textContent = `Errore apertura archivio: ${err.message}`;
    }
  }

  async function deleteOrchestratorVault(runId) {
    if (!runId) return;
    if (!confirm(`Eliminare definitivamente l'intero archivio Persistent "${runId}" da workspaces/vault/? Questa azione è irreversibile.`)) return;
    const msgEl = $('orchVaultsMsg');
    try {
      const res = await apiFetch('/api/orchestrator/vaults/' + encodeURIComponent(runId), { method: 'DELETE' });
      const data = await res.json();
      if (!res.ok || data.ok === false) {
        if (msgEl) msgEl.textContent = data.error || `Errore eliminazione archivio ${runId}.`;
        return;
      }
      if (state.orchestrator.reportRunId === runId) {
        clearOrchestratorReportView(`Archivio Persistent "${runId}" eliminato.`);
      }
      if (msgEl) msgEl.textContent = `Archivio eliminato: ${runId}`;
      await fetchOrchestratorVaults();
    } catch (err) {
      if (msgEl) msgEl.textContent = `Errore eliminazione: ${err.message}`;
    }
  }

  // Wire Orchestrator DOM Event Listeners (zero inline handlers)
  $('orchRetentionEphemeral')?.addEventListener('change', syncWizardPolicyUI);
  $('orchRetentionPersistent')?.addEventListener('change', syncWizardPolicyUI);
  $('btnOrchFillFixture')?.addEventListener('click', () => {
    const inp = $('orchTargetPath');
    if (inp && !inp.disabled) inp.value = ORCH_FIXTURE_PATH;
  });
  $('btnOrchRefreshModels')?.addEventListener('click', populateOrchFastModels);
  $('btnOrchRefreshStatus')?.addEventListener('click', () => {
    fetchOrchestratorStatus();
    fetchOrchestratorVaults();
  });
  $('btnOrchStart')?.addEventListener('click', startOrchestratorRun);
  $('btnOrchPause')?.addEventListener('click', pauseOrchestratorRun);
  $('btnOrchResume')?.addEventListener('click', () => resumeOrchestratorRun(null));
  $('btnOrchStop')?.addEventListener('click', stopOrchestratorRun);
  $('btnOrchPurgeReport')?.addEventListener('click', purgeOrchestratorReport);
  $('btnOrchExportMd')?.addEventListener('click', () => {
    if (!state.orchestrator.reportMarkdown) return;
    const slug = (state.orchestrator.reportRunId || new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-'))
      .replace(/[^a-zA-Z0-9._-]/g, '-');
    triggerDownload(`orchestrator-report-${slug}.md`, state.orchestrator.reportMarkdown, 'text/markdown;charset=utf-8');
  });
  $('btnOrchCopyReport')?.addEventListener('click', () => {
    if (state.orchestrator.reportMarkdown) {
      copyTextToClipboard(state.orchestrator.reportMarkdown, $('btnOrchCopyReport'));
    }
  });
  $('btnOrchRefreshVaults')?.addEventListener('click', fetchOrchestratorVaults);

  // Copy Code button delegation inside Orchestrator Final Report
  $('orchReportRendered')?.addEventListener('click', (e) => {
    const codeBtn = e.target.closest('.copy-code-btn');
    if (codeBtn) {
      const codeEl = codeBtn.closest('.code-block')?.querySelector('pre code');
      if (codeEl) copyTextToClipboard(codeEl.textContent || '', codeBtn);
    }
  });

  // Vault table action button delegation
  $('orchVaultsWrap')?.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    const runId = btn.dataset.runId || '';
    if (action === 'open-vault') openOrchestratorVault(runId);
    else if (action === 'resume-vault') resumeOrchestratorRun(runId);
    else if (action === 'delete-vault') deleteOrchestratorVault(runId);
  });

  const shutdownBtn = $('btnShutdownApp');
  if (shutdownBtn) {
    shutdownBtn.addEventListener('click', async () => {
      if (!confirm('Spegnere Orchestrator (wipe RAM), tutti i motori LLM (Ollama / Colibrì), liberare la VRAM e chiudere l\'App?')) return;
      shutdownBtn.disabled = true;
      shutdownBtn.textContent = 'Arresto...';
      state.history = [];
      state.orchestrator.reportMarkdown = '';
      try {
        await apiFetch('/api/control/engine', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'shutdown_app' })
        });
      } catch (_) {}
      document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;background:#0b0f14;color:#e6edf3;font-family:Segoe UI,sans-serif;text-align:center;"><div><h2 style="margin:0 0 8px;">Local LLM arrestato</h2><p style="color:#8b98a9;margin:0;">Orchestrator purgato, motori spenti e VRAM liberata. Puoi chiudere questa finestra.</p></div></div>';
      setTimeout(() => { try { window.close(); } catch (_) {} }, 600);
    });
  }

  // --- Authenticated SSE Stream Reader (replaces HTTP polling; sends X-Local-LLM-Token) ---
  function startAuthenticatedSseLoop(url, onPayload, shouldRun) {
    let activeCtrl = null;
    let stopped = false;

    async function runStream() {
      while (!stopped) {
        if (document.hidden || (typeof shouldRun === 'function' && !shouldRun())) {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          continue;
        }
        activeCtrl = new AbortController();
        try {
          const res = await apiFetch(url, {
            headers: { Accept: 'text/event-stream' },
            signal: activeCtrl.signal
          });
          if (!res.ok || !res.body) {
            await new Promise((resolve) => setTimeout(resolve, 2500));
            continue;
          }
          const reader = res.body.getReader();
          const decoder = new TextDecoder('utf-8');
          let buffer = '';
          while (!stopped && !document.hidden) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let sepIdx;
            while ((sepIdx = buffer.indexOf('\n\n')) !== -1) {
              const block = buffer.slice(0, sepIdx);
              buffer = buffer.slice(sepIdx + 2);
              const dataLines = [];
              for (const rawLine of block.split(/\r?\n/)) {
                if (rawLine.startsWith('data:')) {
                  dataLines.push(rawLine.slice(5).trimStart());
                }
              }
              if (dataLines.length > 0) {
                try {
                  onPayload(JSON.parse(dataLines.join('\n')));
                } catch (_) {}
              }
            }
          }
        } catch (_) {
          // Reconnect cleanly after transient stream interruption or visibility change
        } finally {
          activeCtrl = null;
        }
        if (!stopped) {
          await new Promise((resolve) => setTimeout(resolve, 1500));
        }
      }
    }

    document.addEventListener('visibilitychange', () => {
      if (document.hidden && activeCtrl) {
        try { activeCtrl.abort(); } catch (_) {}
      }
    });

    runStream();
    return () => {
      stopped = true;
      if (activeCtrl) {
        try { activeCtrl.abort(); } catch (_) {}
      }
    };
  }

  // --- Init ---
  async function init() {
    syncWizardPolicyUI();
    syncOperatorProfileUI();
    await fetchSessionToken();
    await Promise.all([fetchTelemetry(), loadPresets()]);
    await Promise.all([populateModelSelect(), populateOrchFastModels()]);
    fetchOrchestratorStatus();
    fetchOrchestratorVaults();
    startAuthenticatedSseLoop('/api/system/telemetry/stream', (data) => {
      if (data && typeof data === 'object') {
        renderTelemetry(data);
      }
    });
    startAuthenticatedSseLoop(
      '/api/orchestrator/stream',
      (data) => {
        if (data && typeof data === 'object') {
          renderOrchestratorStatus(data);
        }
      },
      () => state.activeTab === 'orchestrator' || state.orchestrator.active || state.orchestrator.hasReport
    );
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        fetchTelemetry();
        if (state.activeTab === 'orchestrator' || state.orchestrator.active || state.orchestrator.hasReport) {
          fetchOrchestratorStatus();
        }
      }
    });
  }

  init();
})();
