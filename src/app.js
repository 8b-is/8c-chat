"use strict";
(() => {
  const el = id => document.getElementById(id);
  const get = key => { try { return localStorage.getItem(key) || ""; } catch { return ""; } };
  function store(key, value) {
    try { localStorage.setItem(key, value); return true; }
    catch { el("draftStatus").textContent = "Browser storage is unavailable. Keep this tab open to retain your profile and draft."; return false; }
  }
  function load(key, fallback) { try { return JSON.parse(get(key)) || fallback; } catch { return fallback; } }
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
  const sending = new Set();

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
    el("sendStatus").textContent = draft(room).error || "";
    el("msgInput").placeholder = profile ? "Talk or type. Either one counts." : "Introduce yourself, then talk or type.";
  }
  function identityLabels() {
    el("whoBtn").textContent = profile ? profile.name : "Introduce yourself";
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
    el("identityStatus").textContent = "";
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
    el("ayeMode").replaceChildren(new Option("A local Aye", "auto"));
    el("agentList").replaceChildren();
    for (const agent of agents) {
      if (agent.rooms.includes(room)) el("ayeMode").add(new Option(agent.name + (agent.online ? " · local" : " · offline"), agent.id));
      const box = document.createElement("div"); box.className = "agent";
      const name = document.createElement("strong"); name.textContent = agent.name + (agent.online ? " · connected" : " · offline");
      const info = document.createElement("p"); info.textContent = agent.runtime + " · " + agent.model;
      const follows = document.createElement("p"); follows.textContent = agent.rooms.map(id => rooms.find(r => r.id === id)?.name || id).join(" · ");
      box.append(name, info, follows); el("agentList").append(box);
      const memory = document.createElement("p");
      memory.className = "memory-status";
      memory.textContent = agent.evidence?.memory?.ready ? "MEM|8 · shared family + individual memory" : "MEM|8 awaiting verified connection";
      box.append(memory);
    }
    el("ayeMode").add(new Option("People only", "no"));
    if ([...el("ayeMode").options].some(o => o.value === previous)) el("ayeMode").value = previous;
  }
  function timeLabel(secs) {
    const date = new Date(Number(BigInt(secs) * 1000n));
    return date.toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
  }
  function article(note) {
    const node = document.createElement("article");
    node.className = "msg" + (note.sender_kind === "aye" ? " aye" : note.sender_handle === profile?.handle ? " me" : "");
    node.dataset.id = String(note.id);
    const head = document.createElement("div"); head.className = "msg-head";
    const name = document.createElement("strong"); name.textContent = note.sender_name;
    const time = document.createElement("time"); time.textContent = timeLabel(note.timestamp_secs);
    head.append(name, time);
    const text = document.createElement("p"); text.className = "msg-body"; text.textContent = note.text;
    node.append(head, text);
    if (note.origin === "local-worker") {
      const details = document.createElement("details"); details.className = "provenance quiet";
      const title = document.createElement("summary"); title.textContent = "Local · " + note.model + (note.memory?.ready ? " · MEM|8" : "");
      const proof = document.createElement("p");
      proof.textContent = note.runtime + " · " + note.personality_revision + " · " + note.elapsed_ms + " ms · " + note.output_tokens + " tokens · digest " + note.model_digest;
      details.append(title, proof); node.append(details);
      if (note.memory?.ready) {
        const memory = document.createElement("p"); memory.className = "memory-receipt";
        memory.textContent = "Aurora " + note.memory.format + " · " + note.memory.family_seed + " · Recalled: " + [...new Set(note.memory.records.map(r => r.scope))].join(" · ");
        const refs = document.createElement("p");
        refs.textContent = note.memory.records.map(r => r.scope + " #" + r.ref + ":" + r.event_id).join(" · ") + " · Saved turn #" + note.memory.written.ref + ":" + note.memory.written.event_id;
        details.append(memory, refs);
      }
    }
    return node;
  }
  function renderMessages(messages) {
    const signature = messages.map(m => m.id).join(",") + ":" + (profile?.handle || "");
    if (signature === lastMessages) return;
    const box = el("messages");
    const current = new Map([...box.querySelectorAll("article")].map(node => [node.dataset.id, node]));
    const ids = new Set(messages.map(m => String(m.id)));
    for (const node of [...box.children]) if (!ids.has(node.dataset.id)) node.remove();
    let newest;
    for (const message of messages) {
      const existing = current.get(String(message.id));
      if (!existing) { box.append(article(message)); newest = message; }
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
      row.textContent = (agents.find(a => a.id === job.agent)?.name || job.agent) + ": " + (job.status === "failed" ? job.error : job.status === "replying" ? "replying locally…" : "queued for the local model…");
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
    if (d.submitted !== text || !d.id) { d.id = crypto.randomUUID(); d.submitted = text; }
    const id = d.id;
    d.error = ""; saveDrafts(); sending.add(selected);
    if (selected === room) refreshComposer();
    try {
      await api("/api/messages", {room: selected, text, client_id: id, agent: mode === "no" ? "auto" : mode, ask_aye: mode === "no" ? "no" : "always"});
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
      const result = profile ? await api("/api/profile", {bio: el("bioInput").value, favorites: profile.favorites})
        : await api("/api/introduce", {name: el("nameInput").value, bio: el("bioInput").value});
      profile = result.profile;
      if (result.token) { token = result.token; store("8c_token_v8", token); }
      generation += 1; identityLabels(); el("whoSheet").close(); el("msgInput").focus(); poll();
    } catch (error) { el("identityStatus").textContent = error.message; }
    finally { identityBusy = false; el("identitySave").disabled = false; }
  });
  el("favoriteBtn").addEventListener("click", async () => {
    if (!profile) { showIdentity(); return; }
    const favorites = profile.favorites.includes(room) ? profile.favorites.filter(id => id !== room) : [...profile.favorites, room];
    el("favoriteBtn").disabled = true;
    try { profile = (await api("/api/profile", {bio: profile.bio, favorites})).profile; identityLabels(); }
    catch (error) { el("sendStatus").textContent = error.message; }
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
  el("msgInput").addEventListener("input", () => { draft(room).text = el("msgInput").value; saveDrafts(); });
  el("chatForm").addEventListener("submit", event => { event.preventDefault(); send(); });
  el("msgInput").addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); } });
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
