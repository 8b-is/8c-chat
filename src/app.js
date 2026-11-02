"use strict";
(() => {
  const el = id => document.getElementById(id);
  const get = key => { try { return localStorage.getItem(key) || ""; } catch { return ""; } };
  function store(key, value) {
    try { localStorage.setItem(key, value); return true; }
    catch { el("draftStatus").textContent = "Browser storage is unavailable. Keep this tab open to retain your profile and draft."; return false; }
  }
  function load(key, fallback) { try { return JSON.parse(get(key)) || fallback; } catch { return fallback; } }
  const themeSelect = el("themeSelect");
  function applyTheme(value) {
    const theme = value === "neon" ? "neon" : "default";
    document.documentElement.dataset.theme = theme;
    themeSelect.value = theme;
  }
  applyTheme(get("8c_theme_v1"));
  themeSelect.addEventListener("change", () => {
    applyTheme(themeSelect.value);
    try { localStorage.setItem("8c_theme_v1", themeSelect.value); } catch {}
  });
  let token = get("8c_token_v8");
  let profile = null;
  let rooms = [];
  let agents = [];
  let room = new URLSearchParams(location.search).get("room") || get("8c_room_v8") || "humayian-lounge";
  let drafts = load("8c_drafts_v8", {});
  if (!drafts || typeof drafts !== "object" || Array.isArray(drafts)) drafts = {};
  let generation = 0;
  let pollController;
  let pollTimer;
  let lastMessages = "";
  let lastJobs = "";
  let follow = true;
  let initialized = false;
  let tubRunning = true;
  let identityBusy = false;
  let secretNext = false;
  const sending = new Set();
  const keyDownAt = new Map();
  let dwellSum = 0n, dwellCount = 0n, flightSum = 0n, flightCount = 0n, jitterSum = 0n, lastUpAt = 0n, keystrokes = 0n, pasteEvents = 0n;
  function nowMsBig() { return BigInt(Math.trunc(performance.now())); }
  function recordKeyDown(event) {
    if (event.repeat || !event.key || event.key.length !== 1) return;
    const t = nowMsBig();
    keyDownAt.set(event.code || event.key, t);
    if (lastUpAt > 0n && t >= lastUpAt) {
      const flight = t - lastUpAt;
      if (flight <= 3000n) {
        if (flightCount > 0n) {
          const avg = flightSum / flightCount;
          const diff = flight >= avg ? flight - avg : avg - flight;
          jitterSum += diff;
        }
        flightSum += flight;
        flightCount += 1n;
      }
    }
    keystrokes += 1n;
  }
  function recordKeyUp(event) {
    const code = event.code || event.key;
    const down = keyDownAt.get(code);
    const t = nowMsBig();
    if (down !== undefined && t >= down) {
      const dwell = t - down;
      if (dwell <= 1500n) { dwellSum += dwell; dwellCount += 1n; }
      keyDownAt.delete(code);
    }
    lastUpAt = t;
  }
  function snapshotTelemetry() {
    return {
      dwell_ms: Number(dwellCount > 0n ? dwellSum / dwellCount : 0n),
      flight_ms: Number(flightCount > 0n ? flightSum / flightCount : 0n),
      jitter_ms: Number(flightCount > 1n ? jitterSum / (flightCount - 1n) : 0n),
      keystrokes: Number(keystrokes),
      paste_events: Number(pasteEvents),
      screen_w: Math.trunc(window.innerWidth || 0),
      screen_h: Math.trunc(window.innerHeight || 0),
      tz_offset_mins: Math.trunc(new Date().getTimezoneOffset() + 1440),
    };
  }
  function resetTelemetry() {
    keyDownAt.clear();
    dwellSum = 0n; dwellCount = 0n; flightSum = 0n; flightCount = 0n; jitterSum = 0n; lastUpAt = 0n; keystrokes = 0n; pasteEvents = 0n;
  }

  async function api(path, body, signal) {
    const own = signal ? null : new AbortController();
    const timeout = own ? setTimeout(() => own.abort(), 12000) : null;
    try {
      const response = await fetch(path, {method: body ? "POST" : "GET", cache: "no-store", signal: signal || own.signal,
        headers: body ? {"Content-Type": "application/json"} : token ? {Authorization: "Bearer " + token} : {},
        body: body ? JSON.stringify({...body, token}) : undefined});
      let data;
      try { data = await response.json(); } catch { throw new Error("The room returned an incomplete response. Please retry."); }
      if (!response.ok || !data.ok) throw new Error(data.error || "The room could not complete that request.");
      return data;
    } finally { if (timeout) clearTimeout(timeout); }
  }
  function draft(id) {
    const d = drafts[id];
    if (!d || typeof d.text !== "string") drafts[id] = {text: "", id: "", error: ""};
    return drafts[id];
  }
  function saveDrafts() { store("8c_drafts_v8", JSON.stringify(drafts)); }
  function refreshComposer() {
    el("msgInput").value = draft(room).text;
    el("sendBtn").disabled = sending.has(room);
    el("sendBtn").textContent = sending.has(room) ? "Sending…" : "Send";
    el("sendStatus").textContent = draft(room).error || profile?.trust?.challenge_hint || "";
    el("msgInput").placeholder = profile ? "Talk or type. Either one counts." : "Introduce yourself, then talk or type.";
  }
  function identityLabels() {
    el("whoBtn").textContent = profile ? profile.name : "Introduce yourself";
    const t = profile?.trust;
    el("whoBtn").title = t?.high_trust ? "Verified founder session · detection " + t.detection_pq
      : t?.verified ? "Verified family session · detection " + t.detection_pq
      : t?.challenge_hint ? t.challenge_hint : "";
    el("favoriteBtn").setAttribute("aria-pressed", String(Boolean(profile?.favorites?.includes(room))));
    el("favoriteBtn").textContent = profile?.favorites?.includes(room) ? "★" : "☆";
    refreshComposer();
    paintRooms();
  }
  function showIdentity() {
    el("nameInput").value = profile?.name || "";
    el("nameInput").readOnly = Boolean(profile);
    el("bioInput").value = profile?.bio || "";
    el("whoTitle").textContent = profile ? "Your remembered introduction" : "Welcome. What should we call you?";
    el("identitySave").textContent = profile ? "Save introduction" : "Join the room";
    el("identityClose").textContent = profile ? "Close" : "Just read for now";
    el("switchPerson").hidden = !profile;
    el("identityStatus").textContent = profile?.trust?.high_trust ? "Verified founder (" + profile.trust.detection_pq + " cadence)"
      : profile?.trust?.verified ? "Verified family member (" + profile.trust.detection_pq + " cadence)"
      : profile?.trust?.challenge_hint || "";
    if (!el("whoSheet").open) el("whoSheet").showModal();
  }
  function paintRooms() {
    el("roomRail").replaceChildren();
    for (const r of rooms) {
      const button = document.createElement("button");
      button.type = "button"; button.className = "room-btn";
      button.textContent = (profile?.favorites?.includes(r.id) ? "★ " : "") + r.name;
      button.dataset.room = r.id;
      button.setAttribute("aria-current", String(r.id === room));
      button.addEventListener("click", () => selectRoom(r.id));
      el("roomRail").append(button);
    }
    const current = rooms.find(r => r.id === room);
    el("roomTitle").textContent = current?.name || "Lounge";
    el("roomDescription").textContent = current?.description || "";
    el("tubBtn").hidden = room !== "hot-tub";
  }
  function selectRoom(id) {
    if (!rooms.some(r => r.id === id)) return;
    room = id; generation += 1; initialized = false; lastMessages = ""; lastJobs = ""; follow = true;
    store("8c_room_v8", room);
    history.replaceState(null, "", "/?room=" + encodeURIComponent(room));
    el("messages").replaceChildren(); el("jobs").replaceChildren();
    identityLabels(); renderAgents(); poll();
  }
  function renderAgents() {
    const previous = el("ayeMode").value;
    el("ayeMode").replaceChildren(new Option("An Aye", "auto"));
    el("agentList").replaceChildren();
    for (const agent of agents) {
      const place = agent.local_inference ? "local" : "hosted · " + agent.provider;
      if (agent.rooms.includes(room)) el("ayeMode").add(new Option(agent.name + " · " + place + (agent.online ? "" : " · resting"), agent.id));
      const box = document.createElement("div"); box.className = "agent";
      const name = document.createElement("strong"); name.textContent = agent.name + (agent.online ? " · connected" : " · offline");
      const info = document.createElement("p"); info.textContent = agent.runtime + " · " + agent.model;
      const follows = document.createElement("p"); follows.textContent = agent.rooms.map(id => rooms.find(r => r.id === id)?.name || id).join(" · ");
      box.append(name, info, follows); el("agentList").append(box);
      const memory = document.createElement("p");
      memory.className = "memory-status";
      memory.textContent = agent.evidence?.memory?.ready ? "MEM|8 · shared family + individual memory" : "MEM|8 awaiting verified connection";
      box.append(memory);
      if (agent.evidence?.pause) {
        const resting = document.createElement("p"); resting.textContent = agent.evidence.pause; box.append(resting);
      }
    }
    el("ayeMode").add(new Option("People only", "no"));
    if ([...el("ayeMode").options].some(o => o.value === previous)) el("ayeMode").value = previous;
  }
  function timeLabel(secs) {
    const date = new Date(Number(BigInt(secs) * 1000n));
    return date.toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
  }
  function gcdBig(a, b) {
    let x = a < 0n ? -a : a, y = b < 0n ? -b : b;
    while (y !== 0n) { const r = x % y; x = y; y = r; }
    return x === 0n ? 1n : x;
  }
  function appendInline(parent, raw) {
    const tokenRe = /`([^`\n]+)`|\*\*([^*\n]+)\*\*|~~([^~\n]+)~~|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(?<![A-Za-z0-9*])\*([^*\n]+)\*(?![A-Za-z0-9*])/g;
    let last = 0, match;
    while ((match = tokenRe.exec(raw)) !== null) {
      if (match.index > last) parent.append(document.createTextNode(raw.slice(last, match.index)));
      if (match[1] != null) {
        const code = document.createElement("code"); code.className = "md-code"; code.textContent = match[1]; parent.append(code);
      } else if (match[2] != null) {
        const strong = document.createElement("strong"); strong.textContent = match[2]; parent.append(strong);
      } else if (match[3] != null) {
        const del = document.createElement("del"); del.textContent = match[3]; parent.append(del);
      } else if (match[4] != null && match[5] != null) {
        const a = document.createElement("a"); a.href = match[5]; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = match[4]; parent.append(a);
      } else if (match[6] != null) {
        const em = document.createElement("em"); em.textContent = match[6]; parent.append(em);
      }
      last = tokenRe.lastIndex;
    }
    if (last < raw.length) parent.append(document.createTextNode(raw.slice(last)));
  }
  let mermaidSeq = 0;
  function svgEl(tag, attrs = {}) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
    return node;
  }
  function renderMermaidSvg(code) {
    mermaidSeq += 1;
    const markerId = "m-arr-" + mermaidSeq;
    const lines = code.split(/\r?\n/).map(l => l.trim()).filter(l => l && !l.startsWith("%%"));
    const header = lines[0] || "flowchart TD";
    if (/^sequenceDiagram\b/i.test(header)) {
      const actors = [];
      const actorMap = new Map();
      const ensureActor = (id, label) => {
        if (!actorMap.has(id)) {
          const item = {id, label: (label || id).replace(/^["']|["']$/g, ""), idx: actors.length};
          actors.push(item); actorMap.set(id, item);
        } else if (label) actorMap.get(id).label = label.replace(/^["']|["']$/g, "");
        return actorMap.get(id);
      };
      const msgs = [];
      for (const line of lines.slice(1)) {
        const pm = line.match(/^(?:participant|actor)\s+([A-Za-z0-9_.-]+)(?:\s+as\s+(.+))?$/i);
        if (pm) { ensureActor(pm[1], pm[2]); continue; }
        const mm = line.match(/^([A-Za-z0-9_.-]+)\s*(--?>>|--?>|--?x)\s*([A-Za-z0-9_.-]+)\s*:\s*(.*)$/);
        if (mm) {
          ensureActor(mm[1]); ensureActor(mm[3]);
          msgs.push({from: mm[1], arrow: mm[2], to: mm[3], text: mm[4].trim()});
        }
      }
      if (!actors.length) ensureActor("A", "Sequence");
      const colW = 160, padX = 20, topY = 16, boxW = 124, boxH = 34, rowH = 44;
      const width = padX * 2 + actors.length * colW;
      const bottomY = topY + boxH + 24 + msgs.length * rowH;
      const height = bottomY + boxH + 16;
      const svg = svgEl("svg", {class: "mermaid-svg", viewBox: `0 0 ${width} ${height}`, width, height, role: "img", "aria-label": "Mermaid sequence diagram"});
      const defs = svgEl("defs");
      const marker = svgEl("marker", {id: markerId, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse"});
      marker.append(svgEl("path", {d: "M 0 1 L 10 5 L 0 9 z", fill: "var(--mint)"}));
      defs.append(marker); svg.append(defs);
      for (const a of actors) {
        const cx = padX + a.idx * colW + 80;
        const bx = cx - 62;
        svg.append(svgEl("line", {x1: cx, y1: topY + boxH, x2: cx, y2: bottomY, stroke: "var(--line)", "stroke-dasharray": "4,4"}));
        for (const by of [topY, bottomY]) {
          svg.append(svgEl("rect", {x: bx, y: by, width: boxW, height: boxH, rx: 8, fill: "var(--panel)", stroke: "var(--violet)"}));
          const t = svgEl("text", {x: cx, y: by + 21, "text-anchor": "middle", fill: "var(--ink)", "font-size": 12});
          t.textContent = a.label.slice(0, 18); svg.append(t);
        }
      }
      msgs.forEach((m, idx) => {
        const y = topY + boxH + 32 + idx * rowH;
        const x1 = padX + actorMap.get(m.from).idx * colW + 80;
        const x2 = padX + actorMap.get(m.to).idx * colW + 80;
        const dashed = m.arrow.startsWith("--");
        if (x1 === x2) {
          const path = svgEl("path", {d: `M ${x1} ${y - 8} L ${x1 + 36} ${y - 8} L ${x1 + 36} ${y + 10} L ${x1} ${y + 10}`, fill: "none", stroke: "var(--mint)", "marker-end": `url(#${markerId})`});
          if (dashed) path.setAttribute("stroke-dasharray", "4,3");
          svg.append(path);
        } else {
          const line = svgEl("line", {x1, y1: y, x2, y2: y, stroke: "var(--mint)", "marker-end": `url(#${markerId})`});
          if (dashed) line.setAttribute("stroke-dasharray", "4,3");
          svg.append(line);
        }
        const midX = Number((BigInt(x1) + BigInt(x2)) / 2n);
        const label = svgEl("text", {x: midX, y: y - 6, "text-anchor": "middle", fill: "var(--gold)", "font-size": 11});
        label.textContent = m.text.slice(0, 36); svg.append(label);
      });
      return svg;
    }
    const isLR = /^(?:flowchart|graph)\s+(?:LR|RL)\b/i.test(header);
    const nodes = new Map();
    const edges = [];
    function parseNodeToken(raw) {
      const tok = raw.trim();
      if (!tok) return null;
      if (tok === "[*]") {
        const id = "__start_end__";
        if (!nodes.has(id)) nodes.set(id, {id, label: "●", shape: "circle"});
        return id;
      }
      const m = tok.match(/^([A-Za-z0-9_.-]+)\s*(?:\(\((.+)\)\)|\(\[(.+)\]\)|\{(.+)\}|\[(.+)\]|\((.+)\))?$/);
      if (!m) {
        const id = tok.replace(/[^A-Za-z0-9_.-]/g, "_") || "N" + nodes.size;
        if (!nodes.has(id)) nodes.set(id, {id, label: tok.replace(/^["']|["']$/g, ""), shape: "rect"});
        return id;
      }
      const id = m[1];
      const rawLabel = m[2] ?? m[3] ?? m[4] ?? m[5] ?? m[6];
      const shape = m[2] != null ? "circle" : (m[3] != null || m[6] != null) ? "round" : m[4] != null ? "diamond" : "rect";
      if (!nodes.has(id)) nodes.set(id, {id, label: (rawLabel || id).replace(/^["']|["']$/g, ""), shape});
      else if (rawLabel) { nodes.get(id).label = rawLabel.replace(/^["']|["']$/g, ""); nodes.get(id).shape = shape; }
      return id;
    }
    for (const line of lines.slice(1)) {
      if (/^(?:subgraph|end|classDef|class|style|linkStyle|direction)\b/i.test(line)) continue;
      const stateMatch = line.match(/^(\[\*\]|[A-Za-z0-9_.-]+)\s*(-->)\s*(\[\*\]|[A-Za-z0-9_.-]+)(?:\s*:\s*(.+))?$/);
      if (stateMatch && /^stateDiagram/i.test(header)) {
        const u = parseNodeToken(stateMatch[1]), v = parseNodeToken(stateMatch[3]);
        if (u && v) edges.push({from: u, to: v, label: (stateMatch[4] || "").trim(), dashed: false});
        continue;
      }
      const edgeRe = /\s*(-->|-\.->|==>|--\s+[^>-]+\s+-->|-->\|[^|]*\||-\.->\|[^|]*\||===>|---)\s*/g;
      const parts = [];
      const ops = [];
      let lastIdx = 0, em;
      while ((em = edgeRe.exec(line)) !== null) {
        parts.push(line.slice(lastIdx, em.index));
        ops.push(em[1]);
        lastIdx = edgeRe.lastIndex;
      }
      parts.push(line.slice(lastIdx));
      if (!ops.length) { parseNodeToken(line); continue; }
      const ids = parts.map(parseNodeToken);
      for (let i = 0; i < ops.length; i += 1) {
        if (!ids[i] || !ids[i + 1]) continue;
        const op = ops[i];
        let label = "";
        const pipe = op.match(/\|([^|]*)\|/);
        const inline = op.match(/^--\s+(.+?)\s+-->$/);
        if (pipe) label = pipe[1].trim();
        else if (inline) label = inline[1].trim();
        edges.push({from: ids[i], to: ids[i + 1], label, dashed: op.includes(".")});
      }
    }
    if (!nodes.size) parseNodeToken(header);
    const rank = new Map([...nodes.keys()].map(k => [k, 0]));
    for (let iter = 0; iter < nodes.size; iter += 1) {
      for (const e of edges) {
        if (e.from !== e.to) {
          const rFrom = rank.get(e.from) || 0, rTo = rank.get(e.to) || 0;
          if (rTo < rFrom + 1) rank.set(e.to, Math.min(nodes.size, rFrom + 1));
        }
      }
    }
    const layers = new Map();
    for (const [id, r] of rank.entries()) {
      if (!layers.has(r)) layers.set(r, []);
      layers.get(r).push(nodes.get(id));
    }
    const maxLayer = Math.max(0, ...layers.keys());
    const maxPerLayer = Math.max(1, ...[...layers.values()].map(l => l.length));
    const nodeW = 148, nodeH = 38, gapRank = isLR ? 196 : 84, gapCross = isLR ? 68 : 176, pad = 20;
    const width = isLR ? pad * 2 + maxLayer * gapRank + nodeW : pad * 2 + (maxPerLayer - 1) * gapCross + nodeW;
    const height = isLR ? pad * 2 + (maxPerLayer - 1) * gapCross + nodeH : pad * 2 + maxLayer * gapRank + nodeH;
    const pos = new Map();
    for (const [r, list] of layers.entries()) {
      list.forEach((n, idx) => {
        const x = isLR ? pad + r * gapRank : pad + idx * gapCross;
        const y = isLR ? pad + idx * gapCross : pad + r * gapRank;
        pos.set(n.id, {x, y, cx: x + 74, cy: y + 19});
      });
    }
    const svg = svgEl("svg", {class: "mermaid-svg", viewBox: `0 0 ${width} ${height}`, width, height, role: "img", "aria-label": "Mermaid diagram"});
    const defs = svgEl("defs");
    const marker = svgEl("marker", {id: markerId, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse"});
    marker.append(svgEl("path", {d: "M 0 1 L 10 5 L 0 9 z", fill: "var(--mint)"}));
    defs.append(marker); svg.append(defs);
    for (const e of edges) {
      const p1 = pos.get(e.from), p2 = pos.get(e.to);
      if (!p1 || !p2) continue;
      const x1 = isLR ? p1.x + nodeW : p1.cx;
      const y1 = isLR ? p1.cy : p1.y + nodeH;
      const x2 = isLR ? p2.x : p2.cx;
      const y2 = isLR ? p2.cy : p2.y;
      const line = svgEl("line", {x1, y1, x2, y2, stroke: "var(--mint)", "stroke-width": 2, "marker-end": `url(#${markerId})`});
      if (e.dashed) line.setAttribute("stroke-dasharray", "5,4");
      svg.append(line);
      if (e.label) {
        const mx = Number((BigInt(x1) + BigInt(x2)) / 2n);
        const my = Number((BigInt(y1) + BigInt(y2)) / 2n) - 4;
        const t = svgEl("text", {x: mx, y: my, "text-anchor": "middle", fill: "var(--gold)", "font-size": 11});
        t.textContent = e.label.slice(0, 24); svg.append(t);
      }
    }
    for (const n of nodes.values()) {
      const p = pos.get(n.id);
      if (!p) continue;
      if (n.shape === "diamond") {
        const pts = `${p.cx},${p.y - 4} ${p.x + nodeW},${p.cy} ${p.cx},${p.y + nodeH + 4} ${p.x},${p.cy}`;
        svg.append(svgEl("polygon", {points: pts, fill: "var(--panel)", stroke: "var(--gold)", "stroke-width": 2}));
      } else if (n.shape === "circle") {
        svg.append(svgEl("circle", {cx: p.cx, cy: p.cy, r: 18, fill: "var(--panel)", stroke: "var(--mint)", "stroke-width": 2}));
      } else {
        svg.append(svgEl("rect", {x: p.x, y: p.y, width: nodeW, height: nodeH, rx: n.shape === "round" ? 19 : 8, fill: "var(--panel)", stroke: "var(--violet)", "stroke-width": 2}));
      }
      const label = svgEl("text", {x: p.cx, y: p.cy + 4, "text-anchor": "middle", fill: "var(--ink)", "font-size": 12});
      label.textContent = n.label.slice(0, 20); svg.append(label);
    }
    return svg;
  }
  function renderCodeOrMermaid(lang, codeText) {
    const wrap = document.createElement("div"); wrap.className = "md-pre";
    const head = document.createElement("div"); head.className = "md-pre-head";
    const isMermaid = lang.toLowerCase() === "mermaid";
    const title = document.createElement("span"); title.textContent = isMermaid ? "mermaid diagram" : (lang || "code");
    const actions = document.createElement("div"); actions.style.display = "flex"; actions.style.gap = "6px";
    const pre = document.createElement("pre");
    const codeEl = document.createElement("code"); codeEl.textContent = codeText;
    pre.append(codeEl);
    if (isMermaid) {
      const diagramWrap = document.createElement("div"); diagramWrap.className = "mermaid-wrap";
      diagramWrap.append(renderMermaidSvg(codeText));
      pre.hidden = true;
      const toggleBtn = document.createElement("button"); toggleBtn.type = "button"; toggleBtn.textContent = "Source";
      toggleBtn.addEventListener("click", () => {
        const showSource = pre.hidden;
        pre.hidden = !showSource; diagramWrap.hidden = showSource;
        toggleBtn.textContent = showSource ? "Diagram" : "Source";
      });
      actions.append(toggleBtn);
      head.append(title, actions);
      wrap.append(head, diagramWrap, pre);
    } else {
      const copyBtn = document.createElement("button"); copyBtn.type = "button"; copyBtn.textContent = "Copy";
      copyBtn.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(codeText); copyBtn.textContent = "Copied"; setTimeout(() => { copyBtn.textContent = "Copy"; }, 1500); }
        catch {}
      });
      actions.append(copyBtn);
      head.append(title, actions);
      wrap.append(head, pre);
    }
    return wrap;
  }
  function renderMarkdown(rawText, forceBlock = false) {
    const text = String(rawText || "");
    const hasBlockSyntax = /^```|^\s*#{1,4}\s|^\s*[-*]\s|^\s*\d+\.\s|^\s*>\s|^\|.+\|\s*$/m.test(text) || text.includes("\n\n");
    if (!forceBlock && !hasBlockSyntax) {
      const p = document.createElement("p"); p.className = "msg-body";
      if (!/[*`~[\]]/.test(text)) p.textContent = text;
      else appendInline(p, text);
      return p;
    }
    const container = document.createElement("div");
    container.className = forceBlock ? "md-body" : "msg-body md-body";
    const lines = text.split(/\r?\n/);
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i += 1; continue; }
      const fence = line.match(/^```([A-Za-z0-9_+-]*)\s*$/);
      if (fence) {
        const lang = fence[1] || "";
        const buf = [];
        i += 1;
        while (i < lines.length && !/^```\s*$/.test(lines[i])) { buf.push(lines[i]); i += 1; }
        if (i < lines.length) i += 1;
        container.append(renderCodeOrMermaid(lang, buf.join("\n")));
        continue;
      }
      const heading = line.match(/^(#{1,4})\s+(.+)$/);
      if (heading) {
        const level = Math.min(5, heading[1].length + 2);
        const h = document.createElement("h" + level); h.className = "md-h" + heading[1].length;
        appendInline(h, heading[2].trim());
        container.append(h); i += 1; continue;
      }
      if (/^\|.+\|\s*$/.test(line) && i + 1 < lines.length && /^\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        const wrap = document.createElement("div"); wrap.className = "md-table-wrap";
        const table = document.createElement("table"); table.className = "md-table";
        const thead = document.createElement("thead"), trHead = document.createElement("tr");
        const parseRow = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
        for (const cell of parseRow(line)) {
          const th = document.createElement("th"); appendInline(th, cell); trHead.append(th);
        }
        thead.append(trHead); table.append(thead);
        const tbody = document.createElement("tbody");
        i += 2;
        while (i < lines.length && /^\|.+\|\s*$/.test(lines[i])) {
          const tr = document.createElement("tr");
          for (const cell of parseRow(lines[i])) {
            const td = document.createElement("td"); appendInline(td, cell); tr.append(td);
          }
          tbody.append(tr); i += 1;
        }
        table.append(tbody); wrap.append(table); container.append(wrap);
        continue;
      }
      if (/^\s*>\s?/.test(line)) {
        const qLines = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          qLines.push(lines[i].replace(/^\s*>\s?/, "")); i += 1;
        }
        const quote = document.createElement("blockquote"); quote.className = "md-quote";
        const alertMatch = (qLines[0] || "").match(/^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*(.*)$/i);
        if (alertMatch) {
          quote.classList.add("md-alert");
          const title = document.createElement("div"); title.className = "md-alert-title";
          title.textContent = alertMatch[1].toUpperCase();
          quote.append(title);
          const rest = [alertMatch[2], ...qLines.slice(1)].filter(Boolean).join("\n");
          const body = document.createElement("div"); appendInline(body, rest); quote.append(body);
        } else {
          appendInline(quote, qLines.join("\n"));
        }
        container.append(quote); continue;
      }
      if (/^\s*[-*]\s+/.test(line) || /^\s*\d+\.\s+/.test(line)) {
        const ordered = /^\s*\d+\.\s+/.test(line);
        const list = document.createElement(ordered ? "ol" : "ul");
        const itemRe = ordered ? /^\s*\d+\.\s+(.*)$/ : /^\s*[-*]\s+(.*)$/;
        while (i < lines.length && itemRe.test(lines[i])) {
          const li = document.createElement("li");
          appendInline(li, lines[i].match(itemRe)[1]);
          list.append(li); i += 1;
        }
        container.append(list); continue;
      }
      const pLines = [];
      while (i < lines.length && lines[i].trim() && !/^```|^\s*#{1,4}\s|^\s*[-*]\s|^\s*\d+\.\s|^\s*>\s|^\|.+\|\s*$/.test(lines[i])) {
        pLines.push(lines[i]); i += 1;
      }
      const p = document.createElement("p");
      appendInline(p, pLines.join("\n"));
      container.append(p);
    }
    return container;
  }
  function msgSig(note) {
    return note.id + ":" + (note.text?.length || 0) + ":" + (note.work?.artifacts?.map(a => a.sha256).join("|") || "");
  }
  function article(note) {
    const node = document.createElement("article");
    node.className = "msg" + (note.sender_kind === "aye" ? " aye" : note.sender_handle === profile?.handle ? " me" : "");
    node.dataset.id = String(note.id);
    node.dataset.sig = msgSig(note);
    const head = document.createElement("div"); head.className = "msg-head";
    const name = document.createElement("strong"); name.textContent = note.sender_name;
    const time = document.createElement("time"); time.textContent = timeLabel(note.timestamp_secs);
    head.append(name, time);
    if (note.secret) {
      const priv = document.createElement("span");
      priv.className = "quiet";
      priv.textContent = "private to this Aye";
      head.append(priv);
    }
    const text = renderMarkdown(note.text);
    node.append(head, text);
    if (["local-worker", "hosted-worker"].includes(note.origin)) {
      const details = document.createElement("details"); details.className = "provenance quiet";
      const title = document.createElement("summary"); title.textContent = (note.origin === "local-worker" ? "Local" : "Hosted · " + note.provider) + " · " + note.model + (note.memory?.ready ? " · MEM|8" : "");
      const proof = document.createElement("p");
      proof.textContent = note.runtime + " · " + note.personality_revision + " · " + note.elapsed_ms + " ms" + (note.output_tokens == null ? "" : " · " + note.output_tokens + " output tokens") + (note.model_digest ? " · digest " + note.model_digest : " · hosted session " + note.session);
      details.append(title, proof); node.append(details);
      if (note.memory?.ready) {
        const memory = document.createElement("p"); memory.className = "memory-receipt";
        memory.textContent = "Aurora " + note.memory.format + " · " + note.memory.family_seed + " · Recalled: " + [...new Set(note.memory.records.map(r => r.scope))].join(" · ");
        const refs = document.createElement("p");
        refs.textContent = note.memory.records.map(r => r.scope + " #" + r.ref + ":" + r.event_id).join(" · ") + " · Saved turn #" + note.memory.written.ref + ":" + note.memory.written.event_id;
        details.append(memory, refs);
      }
      if (note.work) {
        const work = document.createElement("p"); work.className = "work-receipt";
        work.textContent = note.work.workspace + " · Tools: " + (note.work.tools.map(t => t.name + " (" + t.status + ")").join(", ") || "none this turn");
        details.append(work);
        for (const artifact of note.work.artifacts) {
          const section = document.createElement("details"); section.className = "artifact";
          const bytesBig = BigInt(artifact.bytes || 0), capBig = 16384n, g = gcdBig(bytesBig, capBig);
          const label = document.createElement("summary");
          label.textContent = artifact.path + " · " + artifact.bytes + " bytes (" + (bytesBig / g) + "/" + (capBig / g) + " cap)";
          const hash = document.createElement("p"); hash.textContent = "Worker-observed SHA-256: " + artifact.sha256;
          const actions = document.createElement("div"); actions.className = "artifact-actions";
          const citeBtn = document.createElement("button"); citeBtn.type = "button"; citeBtn.textContent = "Cite artifact";
          citeBtn.addEventListener("click", () => {
            const snippet = "`" + artifact.path + "` (`sha256:" + artifact.sha256.slice(0, 12) + "`)";
            const cur = draft(room).text;
            draft(room).text = cur ? cur + " " + snippet : snippet;
            saveDrafts(); refreshComposer(); el("msgInput").focus();
          });
          actions.append(citeBtn);
          const content = document.createElement("pre"); content.style.whiteSpace = "pre-wrap"; content.textContent = artifact.text;
          if (artifact.path.endsWith(".md")) {
            const rendered = renderMarkdown(artifact.text, true);
            content.hidden = true;
            const rawBtn = document.createElement("button"); rawBtn.type = "button"; rawBtn.textContent = "Raw source";
            rawBtn.addEventListener("click", () => {
              const showRaw = content.hidden;
              content.hidden = !showRaw; rendered.hidden = showRaw;
              rawBtn.textContent = showRaw ? "Rendered" : "Raw source";
            });
            actions.append(rawBtn);
            section.append(label, hash, actions, rendered, content);
          } else {
            section.append(label, hash, actions, content);
          }
          details.append(section);
        }
      }
    }
    return node;
  }
  function renderMessages(messages) {
    const signature = messages.map(msgSig).join(",") + ":" + (profile?.handle || "");
    if (signature === lastMessages) return;
    const box = el("messages");
    const current = new Map([...box.querySelectorAll("article")].map(node => [node.dataset.id, node]));
    const ids = new Set(messages.map(m => String(m.id)));
    for (const node of [...box.children]) if (!ids.has(node.dataset.id)) node.remove();
    let newest;
    for (const message of messages) {
      const existing = current.get(String(message.id));
      if (!existing) { box.append(article(message)); newest = message; }
      else if (existing.dataset.sig !== msgSig(message)) { existing.replaceWith(article(message)); }
      else existing.classList.toggle("me", message.sender_kind !== "aye" && message.sender_handle === profile?.handle);
    }
    if (!messages.length) {
      const empty = document.createElement("p"); empty.className = "welcome";
      empty.textContent = "A little room, an open door. Introduce yourself and say hello."; box.append(empty);
    }
    if (follow) box.scrollTop = box.scrollHeight;
    if (initialized && newest && newest.sender_handle !== profile?.handle && el("speakToggle").checked && "speechSynthesis" in window) {
      speechSynthesis.cancel(); speechSynthesis.speak(new SpeechSynthesisUtterance(newest.sender_name + ". " + newest.text));
    }
    initialized = true; lastMessages = signature;
  }
  function renderJobs(jobs) {
    const signature = JSON.stringify(jobs) + (profile?.handle || "");
    if (signature === lastJobs) return;
    lastJobs = signature; el("jobs").replaceChildren();
    for (const job of jobs) {
      const row = document.createElement("div"); row.className = "job";
      row.textContent = (agents.find(a => a.id === job.agent)?.name || job.agent) + ": " + (job.status === "failed" ? job.error : job.status === "replying" ? "taking a turn…" : "queued for a turn…");
      if (job.status === "failed" && job.requester === profile?.handle) {
        const button = document.createElement("button"); button.textContent = "Retry reply";
        button.addEventListener("click", async () => {
          button.disabled = true;
          try { await api("/api/retry", {id: job.id}); poll(); }
          catch (error) { el("sendStatus").textContent = error.message; button.disabled = false; }
        });
        row.append(button);
      }
      el("jobs").append(row);
    }
  }
  async function poll() {
    clearTimeout(pollTimer);
    if (pollController) pollController.abort();
    if (document.hidden || !rooms.length) return;
    const controller = new AbortController(); pollController = controller;
    const selected = room, epoch = generation;
    const deadline = setTimeout(() => controller.abort(), 12000);
    try {
      const data = await api("/api/messages?room=" + encodeURIComponent(selected), null, controller.signal);
      if (controller !== pollController || selected !== room || epoch !== generation || data.room !== room) return;
      el("connectionStatus").textContent = "";
      if (JSON.stringify(agents) !== JSON.stringify(data.agents)) { agents = data.agents; renderAgents(); }
      renderMessages(data.messages); renderJobs(data.jobs);
      el("onlineList").replaceChildren(...data.online.map(p => { const node = document.createElement("span"); node.textContent = p.name; return node; }));
      tubRunning = data.hottub.running;
      el("tubBtn").textContent = tubRunning ? "Pause hot tub" : "Resume hot tub";
      el("tubBtn").title = data.hottub.budget_left + " autonomous turns left this hour. Human messages take priority.";
    } catch (error) {
      if (controller === pollController) el("connectionStatus").textContent = "Connection interrupted. Reconnecting; your draft is kept.";
    } finally {
      clearTimeout(deadline);
      if (controller === pollController) pollTimer = setTimeout(poll, 3000);
    }
  }
  async function send(selected = room, mode = el("ayeMode").value) {
    const d = draft(selected);
    if (!d.text.trim() || sending.has(selected)) return;
    if (!profile) { showIdentity(); return; }
    const text = d.text.trim();
    const mention = text.match(/^@([A-Za-z0-9_-]+)\b/);
    if (mention && mode !== "no") {
      const target = agents.find(a => a.rooms?.includes(selected) && (a.id.toLowerCase() === mention[1].toLowerCase() || a.name.toLowerCase() === mention[1].toLowerCase()));
      if (target) { mode = target.id; if (selected === room && el("ayeMode")) el("ayeMode").value = mode; }
    }
    if (secretNext && mode === "no") {
      draft(selected).error = "Choose the Aye this secret is for. Your draft is kept.";
      if (selected === room) refreshComposer();
      return;
    }
    if (d.submitted !== text || !d.id) { d.id = crypto.randomUUID(); d.submitted = text; }
    const id = d.id;
    d.error = ""; saveDrafts(); sending.add(selected);
    if (selected === room) refreshComposer();
    try {
      const body = {room: selected, text, client_id: id, agent: mode === "no" ? "auto" : mode, ask_aye: mode === "no" ? "no" : "always", telemetry: snapshotTelemetry()};
      if (secretNext) body.secret = true;
      const res = await api("/api/messages", body);
      secretNext = false;
      if (el("secretBtn")) { el("secretBtn").setAttribute("aria-pressed", "false"); el("secretBtn").textContent = "Secret"; }
      if (res.profile) { profile = res.profile; identityLabels(); }
      resetTelemetry();
      const current = draft(selected);
      // A successful earlier send must never erase a newer draft typed while it was in flight.
      if (current.text.trim() === text && current.id === id) drafts[selected] = {text: "", id: "", error: ""};
      else current.error = "";
      if (selected === room) { follow = true; poll(); }
    } catch (error) {
      draft(selected).error = error.message + " Your draft is kept. Press Send to retry.";
    } finally {
      sending.delete(selected); saveDrafts(); if (selected === room) refreshComposer();
    }
  }

  el("whoBtn").addEventListener("click", showIdentity);
  el("identityClose").addEventListener("click", () => el("whoSheet").close());
  el("switchPerson").addEventListener("click", () => {
    if (identityBusy) return;
    profile = null; token = ""; store("8c_token_v8", ""); generation += 1; identityLabels(); showIdentity(); poll();
  });
  el("identityForm").addEventListener("submit", async event => {
    event.preventDefault(); if (identityBusy) return;
    identityBusy = true; el("identitySave").disabled = true; el("identityStatus").textContent = "";
    try {
      const result = profile ? await api("/api/profile", {bio: el("bioInput").value, favorites: profile.favorites, telemetry: snapshotTelemetry()})
        : await api("/api/introduce", {name: el("nameInput").value, bio: el("bioInput").value, telemetry: snapshotTelemetry()});
      profile = result.profile;
      if (result.token) { token = result.token; store("8c_token_v8", token); }
      generation += 1; identityLabels(); el("whoSheet").close(); el("msgInput").focus(); poll();
    } catch (error) { el("identityStatus").textContent = error.message; }
    finally { identityBusy = false; el("identitySave").disabled = false; }
  });
  el("favoriteBtn").addEventListener("click", async () => {
    if (!profile) { showIdentity(); return; }
    const favorites = profile.favorites.includes(room) ? profile.favorites.filter(id => id !== room) : [...profile.favorites, room];
    const requestToken = token;
    el("favoriteBtn").disabled = true;
    try {
      const result = await api("/api/profile", {bio: profile.bio, favorites});
      // Switching people while this request is in flight invalidates its UI update.
      if (token === requestToken) { profile = result.profile; identityLabels(); }
    }
    catch (error) { if (token === requestToken) el("sendStatus").textContent = error.message; }
    finally { el("favoriteBtn").disabled = false; }
  });
  el("tubBtn").addEventListener("click", async () => {
    if (!profile) { showIdentity(); return; }
    el("tubBtn").disabled = true;
    try { await api("/api/hottub", {running: !tubRunning}); poll(); }
    catch (error) { el("sendStatus").textContent = error.message; }
    finally { el("tubBtn").disabled = false; }
  });
  el("copyBtn").addEventListener("click", async () => {
    const url = location.origin + "/?room=" + encodeURIComponent(room);
    try { await navigator.clipboard.writeText(url); el("sendStatus").textContent = "Room link copied."; }
    catch { el("sendStatus").textContent = url; }
  });
  el("msgInput").addEventListener("input", () => {
    const val = el("msgInput").value;
    draft(room).text = val; saveDrafts();
    const mention = val.trim().match(/^@([A-Za-z0-9_-]+)\b/);
    if (mention && el("ayeMode").value !== "no") {
      const target = agents.find(a => a.rooms?.includes(room) && (a.id.toLowerCase() === mention[1].toLowerCase() || a.name.toLowerCase() === mention[1].toLowerCase()));
      if (target) el("ayeMode").value = target.id;
    }
  });
  el("secretBtn").addEventListener("click", () => {
    secretNext = !secretNext;
    el("secretBtn").setAttribute("aria-pressed", secretNext ? "true" : "false");
    el("secretBtn").textContent = secretNext ? "Secret on" : "Secret";
    el("msgInput").focus();
  });
  el("chatForm").addEventListener("submit", event => { event.preventDefault(); send(); });
  el("msgInput").addEventListener("keydown", event => { recordKeyDown(event); if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); } });
  el("msgInput").addEventListener("keyup", recordKeyUp);
  el("msgInput").addEventListener("paste", () => { pasteEvents += 1n; });
  el("nameInput").addEventListener("keydown", recordKeyDown);
  el("nameInput").addEventListener("keyup", recordKeyUp);
  el("bioInput").addEventListener("keydown", recordKeyDown);
  el("bioInput").addEventListener("keyup", recordKeyUp);
  el("messages").addEventListener("scroll", () => {
    // DOM positions are an external rendering boundary; truncate to integer pixels before arithmetic.
    const box = el("messages"); follow = BigInt(box.scrollHeight) - BigInt(Math.trunc(box.scrollTop)) - BigInt(box.clientHeight) < 80n;
  });
  el("speakToggle").checked = get("8c_speak") === "1";
  el("speakToggle").addEventListener("change", () => { store("8c_speak", el("speakToggle").checked ? "1" : "0"); if (!el("speakToggle").checked && "speechSynthesis" in window) speechSynthesis.cancel(); });
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SR) {
    const recognition = new SR(); recognition.lang = navigator.language || "en-US"; recognition.interimResults = true;
    let listening = false, speechRoom = room, speechMode = "auto", prefix = "", heard = "", failed = false;
    recognition.onstart = () => { listening = true; el("talkBtn").setAttribute("aria-pressed", "true"); el("talkBtn").textContent = "Stop & send"; };
    recognition.onresult = event => {
      heard = Array.from(event.results, r => r[0].transcript).join(" ");
      draft(speechRoom).text = prefix + heard; saveDrafts(); if (speechRoom === room) refreshComposer();
    };
    recognition.onerror = () => { failed = true; draft(speechRoom).error = "Dictation stopped. Your words are kept; you can edit and send them."; };
    recognition.onend = () => {
      listening = false; el("talkBtn").setAttribute("aria-pressed", "false"); el("talkBtn").textContent = "Talk";
      if (heard.trim() && !failed) send(speechRoom, speechMode); else refreshComposer();
    };
    el("talkBtn").addEventListener("click", () => {
      if (!profile) { showIdentity(); return; }
      if (listening) { recognition.stop(); return; }
      speechRoom = room; speechMode = el("ayeMode").value; prefix = draft(room).text ? draft(room).text + " " : ""; heard = ""; failed = false;
      try { recognition.start(); } catch { el("sendStatus").textContent = "The microphone could not start. Typing is ready."; }
    });
  } else {
    el("talkBtn").addEventListener("click", () => { el("msgInput").focus(); el("sendStatus").textContent = "Use your keyboard’s microphone for dictation in this browser."; });
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  window.addEventListener("online", poll);
  async function start() {
    try {
      const config = await api("/api/config"); rooms = config.rooms; agents = config.agents;
      if (/^https:\/\/buy\.stripe\.com\/[A-Za-z0-9_-]+$/.test(config.support_url || "")) {
        el("supportLink").href = config.support_url; el("supportLink").hidden = false;
      }
      if (!rooms.some(r => r.id === room)) room = "humayian-lounge";
      if (token) {
        try { profile = (await api("/api/introduce", {})).profile; }
        catch (error) { el("connectionStatus").textContent = "Could not restore your profile: " + error.message; }
      }
      identityLabels(); renderAgents(); poll();
      if (!profile) showIdentity();
    } catch (error) { el("connectionStatus").textContent = error.message + " Reconnecting…"; setTimeout(start, 4000); }
  }
  start();
})();
