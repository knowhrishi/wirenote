// wirenote UI (docs/dev/ui-design.md, docs/dev/features-2-design.md). No framework, no build step.
// All text from transcripts and notes goes in through textContent (h()), never innerHTML.

"use strict";

const TOKEN = document.querySelector('meta[name="wirenote-token"]').content;
const $ = (sel, root = document) => root.querySelector(sel);

const state = {
  calls: [],
  query: "",
  status: {},
  call: null,          // the open call (full detail)
  view: "home",        // home | call | ask | tasks | settings | updates
  tab: "notes",
  asking: false,
  find: "",
  statusKey: "",
  namesChanged: false, // a speaker was renamed since the summary was written
  notesChanged: false, // your notes changed since the summary was written
  showDone: false,
  askingAll: false,
  doctor: null,        // /api/doctor result, fetched once per page load
  updates: null,       // /api/updates: version, new release, update progress
  changelog: null,     // /api/changelog, fetched once per page load
};

// DOM helpers ---------------------------------------------------------------

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function icon(name, cls = "icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `/static/icons.svg#${name}`);
  svg.append(use);
  return svg;
}

// Text with every case-insensitive occurrence of q wrapped in <mark>.
function highlighted(text, q) {
  const frag = document.createDocumentFragment();
  if (!q) { frag.append(text); return frag; }
  const lower = text.toLowerCase(), needle = q.toLowerCase();
  let i = 0, j;
  while ((j = lower.indexOf(needle, i)) >= 0) {
    frag.append(text.slice(i, j), h("mark", {}, text.slice(j, j + q.length)));
    i = j + q.length;
  }
  frag.append(text.slice(i));
  return frag;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 2800);
}

function openModal(content, onClose) {
  const modal = $("#modal");
  const close = () => { modal.hidden = true; modal.replaceChildren(); if (onClose) onClose(); };
  modal.replaceChildren(content);
  modal.onclick = (e) => { if (e.target === modal) close(); };
  modal.hidden = false;
  return close;
}

function confirmDialog(title, text, okLabel, danger = true) {
  return new Promise((resolve) => {
    let result = false;
    const ok = h("button", { class: danger ? "btn btn-danger" : "btn btn-primary" }, okLabel);
    const cancel = h("button", { class: "btn" }, "Cancel");
    const close = openModal(h("div", { class: "dialog", role: "dialog", "aria-modal": "true" },
      h("h3", {}, title), h("p", {}, text), h("div", { class: "row" }, cancel, ok)), () => resolve(result));
    ok.onclick = () => { result = true; close(); };
    cancel.onclick = close;
    ok.focus();
  });
}

// API -----------------------------------------------------------------------

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (init.method !== "GET") {
    init.headers["X-Wirenote-Token"] = TOKEN;
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body || {});
  }
  const res = await fetch(path, init);
  if (res.status === 401) { location.reload(); throw new Error("Not logged in. Open Wirenote from the app menu."); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// Formatting ----------------------------------------------------------------

const LOCALE = undefined;  // the browser's language
const fmtTime = (d) => d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
const fmtDate = (d) => d.toLocaleDateString(LOCALE, { weekday: "long", day: "numeric", month: "long" });
const fmtShort = (d) => d.toLocaleDateString(LOCALE, { day: "numeric", month: "short" });

function fmtDuration(s) {
  if (!s) return "";
  const m = Math.round(s / 60);
  if (m < 1) return `${Math.round(s)} s`;
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

function clock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const hh = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60), ss = sec % 60;
  const p = (n) => String(n).padStart(2, "0");
  return hh ? `${hh}:${p(mm)}:${p(ss)}` : `${mm}:${p(ss)}`;
}

function dayGroup(d) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(d); day.setHours(0, 0, 0, 0);
  const diff = Math.round((today - day) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  if (diff < 7) return day.toLocaleDateString(LOCALE, { weekday: "long" });
  if (day.getFullYear() === today.getFullYear()) return day.toLocaleDateString(LOCALE, { month: "long" });
  return day.toLocaleDateString(LOCALE, { month: "long", year: "numeric" });
}

// Green is "Me"; grey would read as "unknown".
const PALETTE = ["#3b6fd8", "#c2571f", "#8a4fc7", "#c23b6b", "#2a8a9a", "#8a7a1a", "#4f5bd5", "#9a5a3a"];
function colorFor(key) {
  if (key === "Me") return "var(--accent)";
  let hash = 0;
  for (const ch of key || "?") hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[hash % PALETTE.length];
}

function avatar(name, key) {
  const n = (name || "?").trim();
  const initials = n === "Me" ? "Me" : /^Speaker \d+$/.test(n) ? n.split(" ")[1]
    : n.split(/[\s@._-]+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
  const el = h("span", { class: "avatar", "aria-hidden": "true" }, initials.slice(0, 2) || "?");
  el.style.setProperty("background", colorFor(key || n));
  return el;
}

function sourceLabel(c) {
  if (c.app === "manual") return "In person";
  if (c.app === "import") return `Imported · ${c.name}`;
  return c.service ? `${c.service} · ${c.name}` : c.name || c.app;
}

// Where a summary or answer was made: on this laptop, or by Claude.
function cloudBadge(backend) {
  if (!backend) return null;
  if (backend.startsWith("anthropic:")) return h("span", { class: "pill cloud", title: "Sent to Anthropic's Claude API" }, "Sent to Claude");
  if (backend.startsWith("ollama:")) return h("span", { class: "pill local", title: "Made on this laptop" }, "Local");
  return null;
}

function stateBadge(c) {
  if (c.state === "recording") return h("span", { class: "dot rec", title: "Recording" });
  if (c.state === "queued" || c.state === "transcribing" || c.state === "summarizing")
    return h("span", { class: "dot work", title: "Writing the note" });
  if (c.state === "failed") return h("span", { class: "dot fail", title: "Note failed" });
  return null;
}

const nameOf = (label) => (state.call && state.call.names && state.call.names[label]) || (label === "Others" ? "Others" : label.replace(/^S(\d+)$/, "Speaker $1"));

// Setup check (wirenote doctor) ---------------------------------------------

async function loadDoctor(force) {
  if (force || !state.doctor) state.doctor = await api("/api/doctor");
  return state.doctor;
}

function checksList(checks) {
  const mark = { ok: "✓", warn: "!", bad: "✗" };
  return h("ul", { class: "doctor-checks" }, checks.map((c) => h("li", { class: `doctor-check ${c.status}` },
    h("span", { class: "check-mark", "aria-hidden": "true" }, mark[c.status] || "?"),
    h("span", { class: "grow" }, c.message,
      c.fix ? h("button", { class: "check-fix", title: "Copy this command", onclick: async () => {
        try { await navigator.clipboard.writeText(c.fix); toast("Copied. Run it in a terminal."); }
        catch { toast(c.fix); }
      } }, c.fix) : null))));
}

// Sidebar -------------------------------------------------------------------

function renderNav() {
  for (const a of document.querySelectorAll(".nav-item"))
    a.classList.toggle("active", a.dataset.nav === state.view);
  const mine = state.calls.reduce((n, c) => n + (c.my_open_actions || 0), 0);
  $("#tasks-count").textContent = mine ? String(mine) : "";
}

function renderList() {
  renderNav();
  const nav = $("#list");
  const activeId = state.view === "call" && state.call ? state.call.id : null;
  if (!state.calls.length) {
    nav.replaceChildren(h("div", { class: "list-empty" },
      state.query ? `No meetings match “${state.query}”.` : "No meetings yet. Record a call and it shows up here."));
    return;
  }
  const out = [];
  let group = null;
  for (const c of state.calls) {
    const d = new Date(c.started);
    const g = dayGroup(d);
    if (g !== group) { out.push(h("div", { class: "group-label" }, g)); group = g; }
    const meta = [fmtTime(d), fmtDuration(c.duration_s)].filter(Boolean).join(" · ");
    out.push(h("a", { class: "item" + (c.id === activeId ? " active" : ""), href: `#/call/${c.id}` },
      h("div", { class: "item-title" }, stateBadge(c), h("span", {}, c.title)),
      h("div", { class: "item-meta" }, meta),
      c.match ? h("div", { class: "item-match" }, highlighted(c.match.text, state.query)) : null));
  }
  nav.replaceChildren(...out);
}

function renderSidebarStatus() {
  const st = state.status;
  const el = $("#sidebar-status");
  const btn = $("#record-btn");
  btn.classList.toggle("recording", !!st.recording);
  $("#record-label").textContent = st.recording ? "Stop recording" : "Record a meeting";
  btn.disabled = !st.alive;
  if (!st.alive) {
    el.replaceChildren(h("span", { class: "dot fail" }), "Detector not running");
    return;
  }
  const jobs = st.jobs || {};
  const working = (jobs.queued || 0) + (jobs.transcribing || 0) + (jobs.summarizing || 0);
  el.replaceChildren(h("span", { class: "dot" + (st.recording ? " rec" : working ? " work" : "") }),
    st.recording ? "Recording" : working ? `Writing ${working} note${working > 1 ? "s" : ""}…` : "Listening for calls");
}

function recordingElapsed() {
  const st = state.status;
  const app = (st.apps || {})[st.recording] || {};
  if (app.recording_for_s === undefined) return 0;
  return app.recording_for_s + (Date.now() - Date.parse(st.updated)) / 1000;
}

function renderBanner() {
  renderUpdateBanner();
  const st = state.status;
  const b = $("#banner");
  const onRecordingPage = state.view === "call" && state.call && state.call.id === st.recording_id;
  if (!st.recording || onRecordingPage) { b.hidden = true; return; }
  const app = (st.apps || {})[st.recording] || {};
  b.replaceChildren(h("span", { class: "dot rec" }),
    h("span", {}, h("b", {}, `Recording ${app.name || st.recording}`)),
    h("span", { class: "elapsed" }, clock(recordingElapsed())),
    h("span", { class: "grow" }),
    st.recording_id ? h("a", { class: "btn", href: `#/call/${st.recording_id}` }, icon("pen"), "Take notes") : null,
    h("button", { class: "btn btn-stop", onclick: stopRecording }, icon("stop"), "Stop"));
  b.hidden = false;
}

async function stopRecording() {
  try {
    const r = await api("/api/stop", { method: "POST" });
    toast(r.ok ? "Recording stopped. Writing the note…" : r.error);
    refreshStatus();
  } catch (e) { toast(e.message); }
}

async function startRecording() {
  if (state.status.recording) return stopRecording();
  try {
    const r = await api("/api/record", { method: "POST" });
    if (!r.ok) return toast(r.error);
    toast("Recording. Take notes while you talk.");
    // Open the new recording as soon as the daemon has created it.
    for (let i = 0; i < 20; i++) {
      await new Promise((res) => setTimeout(res, 400));
      await refreshStatus();
      if (state.status.recording_id) { location.hash = `#/call/${state.status.recording_id}`; return; }
    }
  } catch (e) { toast(e.message); }
}

// Home ----------------------------------------------------------------------

async function renderHome() {
  state.view = "home";
  state.call = null;
  setPlayer(null);
  renderList();
  renderBanner();
  const now = new Date();
  const hour = now.getHours();
  const hello = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const page = h("div", { class: "page" },
    h("h1", { class: "hello" }, hello), h("div", { class: "today" }, fmtDate(now)));
  const upcoming = h("section", { class: "home-section" },
    h("div", { class: "section" }, h("h2", {}, icon("calendar"), "Coming up")),
    h("div", { class: "hint" }, "Loading…"));
  page.append(upcoming);

  const recent = state.calls.slice(0, 6);
  const recentSec = h("section", { class: "home-section" },
    h("div", { class: "section" }, h("h2", {}, "Recent meetings")));
  if (recent.length) {
    recentSec.append(h("div", { class: "cards" }, recent.map((c) => {
      const d = new Date(c.started);
      const pill = c.state === "recording" ? h("span", { class: "pill bad" }, "Recording")
        : ["queued", "transcribing", "summarizing"].includes(c.state) ? h("span", { class: "pill warn" }, "Writing note…")
        : c.my_open_actions ? h("span", { class: "pill" }, `${c.my_open_actions} for you`) : null;
      const who = c.speakers.length ? c.speakers.join(", ") : c.attendees.join(", ");
      return h("a", { class: "card", href: `#/call/${c.id}` },
        h("div", { class: "card-title" }, h("span", { class: "grow" }, c.title), pill),
        h("div", { class: "card-meta" }, [`${dayGroup(d)}, ${fmtTime(d)}`, fmtDuration(c.duration_s), sourceLabel(c), who].filter(Boolean).join(" · ")),
        c.preview ? h("div", { class: "card-preview" }, c.preview) : null);
    })));
  } else {
    recentSec.append(h("div", { class: "steps" },
      h("div", { class: "step" }, "Join a call in Chrome, Firefox, Brave, Discord or Rocket.Chat. Wirenote asks whether to record. For a meeting in a room, click “Record a meeting”."),
      h("div", { class: "step" }, "Take notes here while you talk, if you like. Your mic and the call audio are recorded on this laptop."),
      h("div", { class: "step" }, "After the call: who said what, a summary, action items and a follow-up email draft.")));
  }
  page.append(recentSec);
  const setup = h("section", { class: "home-section setup-check", hidden: true });
  page.insertBefore(setup, upcoming);
  $("#main").replaceChildren(page);
  loadDoctor().then((d) => {
    const bad = d.checks.filter((c) => c.status === "bad");
    if (!bad.length) return;
    setup.replaceChildren(
      h("div", { class: "section" }, h("h2", {}, "Finish setting up"), h("a", { class: "hint", href: "#/settings" }, "All checks")),
      checksList(bad));
    setup.hidden = false;
  }).catch(() => {});

  try {
    const { events } = await api("/api/upcoming");
    const box = upcoming.lastChild;
    if (!events.length) {
      box.replaceChildren("Nothing in your calendar for the next hours. Connect one with ",
        h("code", {}, "wirenote calendar add"), " to see meetings here and get their titles and attendees in your notes.");
      return;
    }
    box.replaceWith(h("div", { class: "cards" }, events.map((e) => {
      const s = new Date(e.start), en = new Date(e.end);
      const live = s <= new Date() && new Date() < en;
      return h("div", { class: "card event" + (live ? " now" : "") },
        h("div", { class: "event-time" }, h("b", {}, live ? "Now" : fmtTime(s)), `${fmtTime(s)}–${fmtTime(en)}`),
        h("div", { class: "event-bar" + (e.call ? " call" : "") }),
        h("div", { class: "grow" },
          h("div", { class: "card-title" }, h("span", { class: "grow" }, e.title), e.call ? h("span", { class: "pill" }, "Call") : null),
          e.attendees.length ? h("div", { class: "card-meta" }, e.attendees.slice(0, 5).join(", ") + (e.attendees.length > 5 ? ` +${e.attendees.length - 5}` : "")) : null));
    })));
  } catch (e) {
    upcoming.lastChild.textContent = `Calendar: ${e.message}`;
  }
}

// Call page -----------------------------------------------------------------

async function openCall(id, tab, seek) {
  try {
    const call = await api(`/api/calls/${encodeURIComponent(id)}`);
    const sameCall = state.call && state.call.id === call.id;
    if (!sameCall) { state.namesChanged = false; state.notesChanged = false; state.find = ""; }
    state.view = "call";
    state.call = call;
    if (tab) state.tab = tab;
    else if (!sameCall) state.tab = "notes";
    renderList();
    renderBanner();
    renderCall();
    if (!sameCall) setPlayer(call);
    if (seek !== undefined) jumpTo(seek, true);
  } catch (e) {
    $("#main").replaceChildren(h("div", { class: "page" }, h("h1", { class: "hello" }, "Not found"),
      h("p", { class: "hint" }, e.message), h("a", { class: "btn", href: "#/" }, "Back to all meetings")));
  }
}

function renderCall() {
  const c = state.call;
  const main = $("#main");
  const scroll = main.scrollTop;
  const keepFocus = document.activeElement && document.activeElement.id === "mynotes-text";
  const caret = keepFocus ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
  if (c.state === "recording") main.replaceChildren(recordingView(c));
  else main.replaceChildren(callView(c));
  main.scrollTop = scroll;
  if (keepFocus) {
    const ta = $("#mynotes-text");
    if (ta) { ta.focus(); ta.setSelectionRange(caret[0], caret[1]); }
  }
}

function titleEditor(c) {
  const title = h("h1", { class: "title", contenteditable: "true", spellcheck: "false", title: "Click to rename" }, c.title);
  title.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); title.blur(); }
    if (e.key === "Escape") { title.textContent = c.title; title.blur(); }
  });
  title.addEventListener("blur", async () => {
    const t = title.textContent.trim();
    if (t === c.title) return;
    try { await api(`/api/calls/${c.id}/rename`, { method: "POST", body: { title: t } }); toast("Renamed"); refreshList(); openCall(c.id); }
    catch (e) { toast(e.message); }
  });
  return title;
}

function metaLine(c) {
  const d = new Date(c.started);
  return h("div", { class: "meta" },
    h("span", {}, icon("calendar"), `${fmtDate(d)}, ${fmtTime(d)}`),
    c.duration_s && c.state !== "recording" ? h("span", {}, icon("clock"), fmtDuration(c.duration_s)) : null,
    h("span", {}, icon(c.app === "manual" ? "users" : "video"), sourceLabel(c) + (c.room ? ` · ${c.room}` : "")),
    c.calendar && c.calendar !== c.title ? h("span", {}, icon("calendar"), c.calendar) : null);
}

function recordingView(c) {
  const clockEl = h("span", { class: "clock" }, clock(recordingElapsed()));
  clearInterval(recordingView.timer);
  recordingView.timer = setInterval(() => {
    if (!document.body.contains(clockEl)) return clearInterval(recordingView.timer);
    clockEl.textContent = clock(recordingElapsed());
  }, 1000);
  return h("div", { class: "page" },
    h("div", { class: "head-row" }, h("div", { class: "grow" }, titleEditor(c), metaLine(c),
      c.attendees.length ? h("div", { class: "people" }, c.attendees.map((p) => h("span", { class: "person" }, avatar(p), p))) : null)),
    h("div", { class: "live-head" }, h("span", { class: "dot rec" }), "Recording", clockEl,
      h("span", { class: "grow" }), h("button", { class: "btn btn-stop", onclick: stopRecording }, icon("stop"), "Stop")),
    myNotesEditor(c, true),
    h("div", { class: "live-tip" }, "Jot what matters: decisions, who does what, names. A word or two is enough; the summary fills in the rest from the transcript."),
    liveTranscript(c));
}

// What is said during the call (docs/dev/live-design.md). Each poll asks only for lines
// after the last byte it has; the lines are kept per call, so a re-render keeps them.
const live = { id: null, after: 0, lines: [], status: null, enabled: false, timer: null };

function liveTranscript(c) {
  if (live.id !== c.id) Object.assign(live, { id: c.id, after: 0, lines: [], status: null, enabled: false });
  const head = h("div", { class: "live-transcript-head" });
  const list = h("div", { class: "live-lines", "aria-live": "polite" });
  const box = h("section", { class: "live-transcript" }, head, list);
  const line = (l) => h("div", { class: "live-line" + (l.speaker === "Me" ? " me" : "") },
    h("span", { class: "live-who" }, l.speaker === "Me" ? "Me" : "Others"),
    h("span", { class: "live-time" }, clock(l.start)), h("span", { class: "live-text" }, l.text));
  const show = () => {
    box.hidden = !live.enabled && !live.status;
    const st = live.status;
    head.replaceChildren(icon("sparkle"), "Live transcript",
      h("span", { class: "live-state" }, !st ? "starting…" : st.status === "listening" ? st.engine
        : st.reason || st.status));
    if (!live.lines.length) list.replaceChildren(h("div", { class: "live-empty" },
      st && st.status === "unavailable" ? "Your notes and the recording are not affected." : "Lines appear here a few seconds after they are said."));
  };
  // Each track is cut at its own pauses, so a line can arrive after a later one: it goes
  // in at its time, in the list and on the page alike.
  const add = (l) => {
    if (!live.lines.length) list.replaceChildren();
    let i = live.lines.length;
    while (i > 0 && live.lines[i - 1].start > l.start) i--;
    live.lines.splice(i, 0, l);
    list.insertBefore(line(l), list.children[i] || null);
  };
  const poll = async () => {
    if (!box.isConnected) { clearInterval(live.timer); return; }
    try {
      const r = await api(`/api/calls/${encodeURIComponent(c.id)}/live?after=${live.after}`);
      if (r.after < live.after) live.lines = [];
      const nearEnd = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
      Object.assign(live, { after: r.after, enabled: r.enabled, status: r.status || live.status });
      r.lines.forEach(add);
      show();
      if (nearEnd && r.lines.length) list.scrollTop = list.scrollHeight;
    } catch { /* the next poll retries */ }
  };
  list.replaceChildren(...live.lines.map(line));
  show();
  clearInterval(live.timer);
  live.timer = setInterval(poll, 2000);
  setTimeout(poll, 0);           // once the panel is on the page; poll() stops when it is gone
  return box;
}

function callView(c) {
  const page = h("div", { class: "page" },
    h("div", { class: "head-row" },
      h("div", { class: "grow" }, titleEditor(c), metaLine(c), speakersRow(c)),
      h("div", { class: "head-actions" },
        c.summary ? h("button", { class: "btn-icon", title: "Draft a follow-up email", onclick: () => followupDialog(c) }, icon("mail")) : null,
        c.summary ? h("button", { class: "btn-icon", title: "Copy notes as Markdown", onclick: copyNotes }, icon("copy")) : null,
        moreMenu(c))),
    statusNotice(c));
  const lines = c.lines || [];
  page.append(h("div", { class: "tabs", role: "tablist" },
    tabButton("notes", "Notes"),
    tabButton("transcript", "Transcript", lines.length ? String(lines.length) : null)));
  page.append(state.tab === "transcript" ? transcriptView(c) : notesView(c));
  return page;
}

function speakersRow(c) {
  const people = c.people || [];
  if (!people.length) return c.attendees.length ? h("div", { class: "people" }, c.attendees.map((p) => h("span", { class: "person" }, avatar(p), p))) : null;
  const total = people.reduce((n, p) => n + p.seconds, 0) || 1;
  const unnamed = people.some((p) => p.label !== "Me" && !p.named);
  return h("div", { class: "speakers" },
    people.map((p) => {
      const share = `${Math.round((p.seconds / total) * 100)}%`;
      if (p.label === "Me") return h("span", { class: "speaker-chip" }, avatar("Me"), "Me", h("span", { class: "share" }, share));
      return h("button", { class: "speaker-chip" + (p.named ? "" : " unnamed"), title: p.named ? `Change who ${p.name} is` : "Who is this? Click to name",
        onclick: () => speakerDialog(c, p.label) }, avatar(p.name, p.label), p.name, h("span", { class: "share" }, share), icon("pen"));
    }),
    unnamed ? h("span", { class: "speakers-hint" }, "Click a speaker to name them") : null);
}

function tabButton(id, label, count) {
  return h("button", {
    class: "tab" + (state.tab === id ? " active" : ""), role: "tab",
    "aria-selected": String(state.tab === id),
    onclick: () => { location.hash = `#/call/${state.call.id}${id === "notes" ? "" : "/" + id}`; },
  }, label, count ? h("span", { class: "count" }, count) : null);
}

function statusNotice(c) {
  const working = { queued: "Waiting to write the note…", transcribing: "Transcribing and telling the speakers apart…", summarizing: "Writing the summary…" }[c.state];
  if (working)
    return h("div", { class: "notice info" }, h("span", { class: "spinner" }), h("span", { class: "grow" }, working,
      c.state === "transcribing" ? " A long call takes a few minutes." : ""));
  if (c.state === "failed")
    return h("div", { class: "notice bad" }, h("span", { class: "grow" }, `The note failed: ${c.error || "unknown error"}`),
      c.has_audio ? h("button", { class: "btn", onclick: () => requeue(false) }, icon("refresh"), "Try again") : null);
  if (!c.summary && c.has_transcript && c.summary_error)
    return h("div", { class: "notice" }, h("span", { class: "grow" }, `No summary: ${c.summary_error}`),
      h("button", { class: "btn", onclick: () => requeue(true) }, icon("sparkle"), "Retry summary"));
  if (c.has_transcript && (state.namesChanged || state.notesChanged))
    return h("div", { class: "notice info" }, h("span", { class: "grow" },
      state.namesChanged ? "Speaker names changed. Update the summary so owners and quotes use them." : "Your notes changed. Update the summary to build them in."),
      h("button", { class: "btn btn-primary", onclick: () => { state.namesChanged = state.notesChanged = false; requeue(true); } }, icon("sparkle"), "Update summary"));
  return null;
}

function moreMenu(c) {
  const wrap = h("div", { class: "menu-wrap" });
  const btn = h("button", { class: "btn-icon", title: "More", "aria-haspopup": "true" }, icon("more"));
  let menu = null;
  const close = () => { if (menu) { menu.remove(); menu = null; document.removeEventListener("click", outside, true); } };
  const outside = (e) => { if (!wrap.contains(e.target)) close(); };
  const item = (ic, label, fn, cls) => h("button", { class: cls || "", onclick: () => { close(); fn(); } }, icon(ic), label);
  btn.addEventListener("click", () => {
    if (menu) return close();
    menu = h("div", { class: "menu", role: "menu" },
      c.summary ? item("mail", "Draft follow-up email", () => followupDialog(c)) : null,
      c.summary ? item("copy", "Copy notes as Markdown", copyNotes) : null,
      c.note ? item("file", "Open Markdown file", () => post(`/api/calls/${c.id}/open`)) : null,
      c.has_transcript ? item("download", "Download as Word (.docx)", () => { location.href = `/api/calls/${encodeURIComponent(c.id)}/docx`; }) : null,
      c.has_transcript ? item("printer", "Print or save as PDF", () => printCall(c)) : null,
      item("folder", "Show recording folder", () => post(`/api/calls/${c.id}/folder`)),
      h("hr"),
      c.has_transcript ? item("sparkle", "Redo summary", () => requeue(true)) : null,
      c.has_audio ? item("refresh", "Transcribe again…", () => transcribeAgainDialog(c)) : null,
      h("hr"),
      c.has_audio ? item("audio-off", "Delete audio, keep notes", deleteAudio, "danger") : null,
      item("trash", "Delete meeting", deleteCall, "danger"));
    wrap.append(menu);
    document.addEventListener("click", outside, true);
  });
  wrap.append(btn);
  return wrap;
}

// Printing (and so "Save as PDF") shows only #print-view: the notes and the whole
// transcript, whatever tab is open (app.css › @media print).
function printCall(c) {
  const s = c.summary || {};
  const names = c.names || {};
  const list = (head, xs) => (xs && xs.length ? [h("h2", {}, head), h("ul", {}, xs.map((x) => h("li", {}, x)))] : []);
  const started = new Date(c.started);
  const people = (c.people || []).filter((p) => p.label !== "Me").map((p) => p.name);
  $("#print-view").replaceChildren(
    h("h1", {}, c.title),
    h("p", { class: "print-meta" }, [`${fmtDate(started)}, ${fmtTime(started)}`, c.duration_s ? fmtDuration(c.duration_s) : null, sourceLabel(c)].filter(Boolean).join(" · ")),
    people.length ? h("p", {}, h("b", {}, "With: "), people.join(", ")) : null,
    ...list("Summary", s.summary), ...list("Decisions", s.decisions),
    ...(s.action_items && s.action_items.length ? [h("h2", {}, "Action items"), h("ul", { class: "print-actions" },
      s.action_items.map((a) => h("li", {}, a.done ? "☑ " : "☐ ", h("b", {}, `${a.owner}: `), a.task + (a.due ? ` (due: ${a.due})` : ""))))] : []),
    ...list("Open questions", s.open_questions),
    ...(c.my_notes && c.my_notes.trim() ? [h("h2", {}, "My notes"), h("pre", { class: "print-notes" }, c.my_notes.trim())] : []),
    h("h2", {}, "Transcript"),
    ...(c.lines || []).map((l) => h("p", { class: "print-line" }, h("b", {}, names[l.speaker] || l.speaker), h("span", { class: "print-time" }, clock(l.start)), l.text)));
  window.print();
}

// Import ---------------------------------------------------------------------

function importFile(file) {
  if (!file) return;
  const xhr = new XMLHttpRequest();      // fetch() cannot report upload progress
  xhr.open("POST", "/api/import");
  xhr.setRequestHeader("X-Wirenote-Token", TOKEN);
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
  if (file.lastModified) xhr.setRequestHeader("X-Last-Modified", String(file.lastModified));
  let shown = -1;
  xhr.upload.onprogress = (e) => {
    const pct = e.lengthComputable ? Math.floor((e.loaded / e.total) * 100) : 0;
    if (pct !== shown) { shown = pct; toast(pct < 100 ? `Uploading ${file.name}… ${pct}%` : "Converting the audio…"); }
  };
  xhr.onload = async () => {
    let r = {};
    try { r = JSON.parse(xhr.responseText); } catch { /* shown below */ }
    if (xhr.status !== 200) { toast(r.error || `Import failed (HTTP ${xhr.status})`); return; }
    toast("Imported. The note is being written.");
    await refreshList();
    location.hash = `#/call/${r.id}`;
  };
  xhr.onerror = () => toast("Import failed: the upload stopped.");
  toast(`Uploading ${file.name}…`);
  xhr.send(file);
}

function wireImport() {
  const input = $("#import-file");
  $("#import-btn").addEventListener("click", () => input.click());
  input.addEventListener("change", () => { importFile(input.files[0]); input.value = ""; });
  const hint = $("#drop-hint");
  const hasFile = (e) => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
  let depth = 0;
  window.addEventListener("dragenter", (e) => { if (hasFile(e)) { depth++; hint.hidden = false; } });
  window.addEventListener("dragleave", (e) => { if (hasFile(e) && --depth <= 0) { depth = 0; hint.hidden = true; } });
  window.addEventListener("dragover", (e) => { if (hasFile(e)) e.preventDefault(); });
  window.addEventListener("drop", (e) => {
    if (!hasFile(e)) return;
    e.preventDefault();
    depth = 0;
    hint.hidden = true;
    importFile(e.dataTransfer.files[0]);
  });
}

async function post(path) {
  try { await api(path, { method: "POST" }); } catch (e) { toast(e.message); }
}

async function requeue(summaryOnly, choices) {
  const c = state.call;
  try {
    await api(`/api/calls/${c.id}/${summaryOnly ? "resummarize" : "retranscribe"}`, { method: "POST", body: choices ? { choices } : {} });
    toast(summaryOnly ? "Updating the summary…" : "Transcribing again…");
    await refreshList();
    openCall(c.id);
  } catch (e) { toast(e.message); }
}

const ENGINES = [["auto", "Automatic (the fastest that fits)"], ["whisper", "Whisper"], ["parakeet", "Parakeet (faster on a CPU)"], ["whisper.cpp", "whisper.cpp (GPU, if installed)"]];

// Transcribe one call again, with another engine, model or language (docs/dev/engines-design.md).
function transcribeAgainDialog(c) {
  const t = c.transcribe || {};
  const engine = h("select", {}, ENGINES.map(([v, label]) => h("option", { value: v, selected: v === t.engine }, label)));
  const model = h("input", { type: "text", value: t.model || "", placeholder: "large-v3-turbo", spellcheck: "false", maxlength: "100" });
  const language = h("input", { type: "text", value: t.language || "auto", placeholder: "auto, de, en…", spellcheck: "false", maxlength: "20" });
  const modelRow = field("Whisper model", model, "e.g. large-v3-turbo, large-v3, medium.");
  const langRow = field("Language", language, "auto, or a code like de or en. Parakeet always detects it.");
  const show = () => { modelRow.hidden = !["auto", "whisper"].includes(engine.value); langRow.hidden = engine.value === "parakeet"; };
  engine.onchange = show;
  show();
  const ok = h("button", { class: "btn btn-primary" }, "Transcribe again");
  const cancel = h("button", { class: "btn" }, "Cancel");
  const close = openModal(h("div", { class: "dialog wide", role: "dialog", "aria-modal": "true" },
    h("h3", {}, "Transcribe again"),
    h("p", {}, `Now: ${c.transcript_model || "not transcribed yet"}. The new transcript replaces it. Speakers are named again from remembered voices and the call.`),
    field("Speech engine", engine), modelRow, langRow,
    h("div", { class: "row" }, cancel, ok)));
  cancel.onclick = close;
  ok.onclick = () => { close(); requeue(false, { engine: engine.value, model: model.value, language: language.value }); };
}

async function deleteAudio() {
  const c = state.call;
  if (!await confirmDialog("Delete the audio?", "The transcript and notes stay. You can no longer play the call or transcribe it again.", "Delete audio")) return;
  try { await api(`/api/calls/${c.id}/audio`, { method: "DELETE" }); toast("Audio deleted"); setPlayer(null); openCall(c.id); }
  catch (e) { toast(e.message); }
}

async function deleteCall() {
  const c = state.call;
  if (!await confirmDialog("Delete this meeting?", `“${c.title}”: the audio, transcript, summary and the Markdown note are deleted for good.`, "Delete meeting")) return;
  try { await api(`/api/calls/${c.id}`, { method: "DELETE" }); toast("Meeting deleted"); location.hash = "#/"; refreshList(); }
  catch (e) { toast(e.message); }
}

function notesMarkdown(c) {
  const s = c.summary;
  const out = [`# ${c.title}`, "", `${fmtDate(new Date(c.started))}, ${fmtTime(new Date(c.started))} · ${fmtDuration(c.duration_s)}`, ""];
  const people = (c.people || []).filter((p) => p.label !== "Me").map((p) => p.name);
  if (people.length) out.push(`With: ${people.join(", ")}`, "");
  const list = (head, xs) => { if (xs && xs.length) out.push(`## ${head}`, "", ...xs.map((x) => `- ${x}`), ""); };
  list("Summary", s.summary);
  list("Decisions", s.decisions);
  if (s.action_items.length) {
    out.push("## Action items", "", ...s.action_items.map((a) => `- [${a.done ? "x" : " "}] **${a.owner}**: ${a.task}${a.due ? ` (due: ${a.due})` : ""}`), "");
  }
  list("Open questions", s.open_questions);
  return out.join("\n");
}

async function copyNotes() {
  try { await navigator.clipboard.writeText(notesMarkdown(state.call)); toast("Notes copied as Markdown"); }
  catch { toast("Copy failed"); }
}

// Your notes -----------------------------------------------------------------

function myNotesEditor(c, big) {
  const saved = h("span", { class: "saved" }, c.my_notes ? "Saved" : "");
  const ta = h("textarea", { id: "mynotes-text", placeholder: big ? "Type notes while you talk…\n- decision: …\n- Anna: send the draft by Friday" : "Add your own notes. They shape the summary.", spellcheck: "true" });
  ta.value = c.my_notes || "";
  const grow = () => { ta.style.height = "auto"; ta.style.height = `${Math.max(ta.scrollHeight, big ? 320 : 96)}px`; };
  let timer = null, last = ta.value, saving = Promise.resolve();
  // One save at a time, so each sends as base what the previous one saved.
  const save = () => (saving = saving.then(saveNow));
  const saveNow = async () => {
    if (ta.value === last) return;
    const text = ta.value;
    saved.textContent = "Saving…";
    try {
      await api(`/api/calls/${c.id}/notes`, { method: "POST", body: { text, base: last } });
      last = text;
      c.my_notes = text;
      saved.textContent = "Saved";
      if (c.state !== "recording" && c.has_transcript && !state.notesChanged) { state.notesChanged = true; renderCall(); }
    } catch (e) { saved.textContent = "Not saved"; toast(e.message); }
  };
  ta.addEventListener("input", () => { grow(); saved.textContent = "Editing…"; clearTimeout(timer); timer = setTimeout(save, 700); });
  ta.addEventListener("blur", () => { clearTimeout(timer); save(); });
  requestAnimationFrame(grow);
  if (big) requestAnimationFrame(() => { if (!document.activeElement || document.activeElement === document.body) ta.focus(); });
  return h("div", { class: "mynotes" + (big ? " big" : "") },
    h("div", { class: "mynotes-head" }, icon("pen"), "Your notes", saved), ta);
}

// Speakers ---------------------------------------------------------------

function speakerDialog(c, label) {
  const p = (c.people || []).find((x) => x.label === label);
  if (!p) return;
  const input = h("input", { type: "text", value: p.named ? p.name : "", placeholder: "Name", autocomplete: "off" });
  const remember = h("input", { type: "checkbox", id: "remember-voice" });
  remember.checked = true;
  const taken = new Set((c.people || []).filter((x) => x.label !== label && x.named).map((x) => x.name.toLowerCase()));
  const current = p.named ? p.name.toLowerCase() : "";
  const suggestions = [...new Set([...(c.attendees || []), ...(c.known_voices || [])])]
    .filter((n) => !taken.has(n.toLowerCase()) && n.toLowerCase() !== current);
  const sample = (c.lines || []).filter((l) => l.speaker === label).sort((a, b) => b.text.length - a.text.length)[0];
  const others = (c.people || []).filter((x) => x.label !== "Me" && x.label !== label);
  const save = async (body) => {
    try {
      await api(`/api/calls/${c.id}/speaker`, { method: "POST", body });
      close();
      state.namesChanged = !!c.summary;
      toast(body.merge_into ? "Speakers merged" : body.name ? `Named ${body.name}` : "Name removed");
      openCall(c.id);
      refreshList();
    } catch (e) { toast(e.message); }
  };
  const ok = h("button", { class: "btn btn-primary" }, "Save");
  ok.onclick = () => save({ label, name: input.value, remember: remember.checked });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") ok.click(); });
  const close = openModal(h("div", { class: "dialog wide", role: "dialog", "aria-modal": "true" },
    h("h3", {}, `Who is ${p.name}?`),
    sample ? h("p", {}, "“" + (sample.text.length > 160 ? sample.text.slice(0, 160) + "…" : sample.text) + "”") : null,
    p.source === "transcript" && p.evidence ? h("div", { class: "evidence" }, `Guessed from the call: “${p.evidence}”`) : null,
    p.source === "voice" ? h("div", { class: "evidence" }, "Recognized from a remembered voice.") : null,
    h("div", { class: "field" }, h("label", {}, "Name"), input),
    suggestions.length ? h("div", { class: "suggest" }, suggestions.slice(0, 10).map((n) => h("button", { onclick: () => { input.value = n; input.focus(); } }, n))) : null,
    p.can_remember ? h("label", { class: "check-row", for: "remember-voice" }, remember, "Recognize this voice in future calls") : null,
    others.length ? h("div", { class: "sub" }, "Or: this is the same person as") : null,
    others.length ? h("div", { class: "merge-list" }, others.map((o) => h("button", { class: "speaker-chip", onclick: () => save({ label, merge_into: o.label }) }, icon("merge"), avatar(o.name, o.label), o.name))) : null,
    h("div", { class: "row" }, h("button", { class: "btn", onclick: () => close() }, "Cancel"), ok)));
  input.focus();
  input.select();
}

// Follow-up -------------------------------------------------------------------

function followupDialog(c) {
  const subject = h("input", { type: "text", value: c.followup ? c.followup.subject : "" });
  const body = h("textarea", {}, "");
  body.value = c.followup ? c.followup.body : "";
  const status = h("span", { class: "hint" });
  const gen = h("button", { class: "btn" }, icon("sparkle"), c.followup ? "Write again" : "Write draft");
  const copy = h("button", { class: "btn" }, icon("copy"), "Copy");
  const mail = h("button", { class: "btn btn-primary" }, icon("mail"), "Open in mail");
  const to = (c.followup && c.followup.to && c.followup.to.length ? c.followup.to : c.emails) || [];
  const run = async () => {
    gen.disabled = true;
    status.replaceChildren(h("span", { class: "spinner" }), " Writing… a local model takes up to a minute.");
    try {
      const f = await api(`/api/calls/${c.id}/followup`, { method: "POST" });
      c.followup = f;
      subject.value = f.subject;
      body.value = f.body;
      status.textContent = "";
    } catch (e) { status.textContent = e.message; }
    gen.disabled = false;
  };
  gen.onclick = run;
  copy.onclick = async () => {
    try { await navigator.clipboard.writeText(`${subject.value}\n\n${body.value}`); toast("Email copied"); }
    catch { toast("Copy failed"); }
  };
  mail.onclick = () => {
    const url = `mailto:${to.map(encodeURIComponent).join(",")}?subject=${encodeURIComponent(subject.value)}&body=${encodeURIComponent(body.value)}`;
    window.location.href = url;
  };
  openModal(h("div", { class: "dialog wide", role: "dialog", "aria-modal": "true" },
    h("h3", {}, "Follow-up email"),
    h("p", {}, to.length ? `To: ${to.join(", ")}` : "Written from the summary and your notes. Edit it before sending."),
    h("div", { class: "field" }, h("label", {}, "Subject"), subject),
    h("div", { class: "field" }, h("label", {}, "Message"), body),
    h("div", { class: "row" }, status, h("span", { class: "grow" }), gen, copy, mail)));
  if (!c.followup) run();
}

// Notes tab ------------------------------------------------------------------

function notesView(c) {
  const s = c.summary;
  const wrap = h("div", {});
  wrap.append(myNotesEditor(c, false));
  if (!s) {
    if (c.has_transcript) {
      wrap.append(h("p", { class: "empty-line" }, "No summary for this call. The full transcript is in the Transcript tab."));
      wrap.append(askView(c));
    } else if (!["queued", "transcribing", "summarizing", "recording"].includes(c.state)) wrap.append(h("p", { class: "empty-line" }, "Nothing here yet."));
    return wrap;
  }
  const bulletList = (items, cls) => h("ul", { class: "bullets " + (cls || "") },
    items.map((text) => h("li", {}, h("span", {}, text), jumpButton(text))));

  wrap.append(h("section", { class: "section" }, h("h2", {}, "Summary", cloudBadge(c.summary_backend)),
    s.summary.length ? bulletList(s.summary) : h("div", { class: "empty-line" }, "—")));

  wrap.append(h("section", { class: "section" },
    h("h2", {}, "Action items", s.action_items.length ? h("span", { class: "n" }, String(s.action_items.length)) : null),
    s.action_items.length ? h("ul", { class: "actions" }, s.action_items.map((a, i) => actionItem(c.id, a, i, false)))
      : h("div", { class: "empty-line" }, "No action items.")));

  if (s.decisions.length) wrap.append(h("section", { class: "section" }, h("h2", {}, "Decisions"), bulletList(s.decisions, "decisions")));
  if (s.open_questions.length) wrap.append(h("section", { class: "section" }, h("h2", {}, "Open questions"), bulletList(s.open_questions, "questions")));
  if (c.has_transcript) wrap.append(askView(c));
  return wrap;
}

function actionItem(callId, a, i, withCall) {
  const li = h(withCall ? "div" : "li", { class: (withCall ? "task-row" : "") + (a.done ? " done" : "") });
  const box = h("button", { class: "check", role: "checkbox", "aria-checked": String(!!a.done), title: a.done ? "Mark as open" : "Mark as done" }, icon("check"));
  box.addEventListener("click", async () => {
    const done = !a.done;
    a.done = done;
    li.classList.toggle("done", done);
    box.setAttribute("aria-checked", String(done));
    try {
      await api(`/api/calls/${callId}/action/${i}`, { method: "POST", body: { done } });
      if (state.call && state.call.summary && state.call.id === callId) state.call.summary.action_items[i].done = done;
      refreshList();
    } catch (e) { a.done = !done; li.classList.toggle("done", !done); toast(e.message); }
  });
  li.append(box, h("div", { class: "a-body" },
    h("div", { class: "a-task" }, a.task),
    h("div", { class: "a-meta" }, h("span", { class: "tag" }, avatar(a.owner), a.owner),
      a.due ? h("span", { class: "tag due" }, a.due) : null,
      withCall ? h("a", { href: `#/call/${callId}` }, `${a.title} · ${fmtShort(new Date(a.started))}`) : null)));
  if (!withCall) li.append(jumpButton(a.task));
  return li;
}

function jumpButton(text) {
  return h("button", { class: "jump", title: "Jump to this moment in the transcript", onclick: () => {
    const t = bestMoment(text);
    if (t === null) return toast("No matching moment found");
    jumpTo(t, false);
  } }, icon("jump"));
}

// The transcript line that shares the most words with a summary point.
const WORD = /[\p{L}\p{N}]{3,}/gu;
function words(s) { return new Set((s.toLowerCase().match(WORD) || []).map((w) => w.slice(0, 5))); }
function bestMoment(text) {
  const want = words(text);
  let best = null, bestScore = 0;
  for (const l of state.call.lines || []) {
    const got = words(l.text);
    let common = 0;
    for (const w of want) if (got.has(w)) common++;
    const score = common / Math.sqrt(got.size + 1);
    if (score > bestScore) { bestScore = score; best = l.start; }
  }
  return bestScore > 0 ? best : null;
}

function jumpTo(seconds, quiet) {
  if (state.tab !== "transcript") {
    state.tab = "transcript";
    history.replaceState(null, "", `#/call/${state.call.id}/transcript`);
    renderCall();
  }
  const lines = [...document.querySelectorAll(".t-line")];
  let target = lines[0];
  for (const el of lines) { if (Number(el.dataset.start) <= seconds + 0.01) target = el; else break; }
  if (target) {
    target.scrollIntoView({ block: "center", behavior: quiet ? "auto" : "smooth" });
    target.classList.remove("flash"); void target.offsetWidth; target.classList.add("flash");
  }
  if (state.call.has_audio) {
    seekAudio(seconds);
    if (!quiet && audio().paused) toast("Press Space to play from here");
  }
}

// Ask (one call) ---------------------------------------------------------------

function askBox(placeholder, busy, onAsk) {
  const input = h("input", { type: "text", placeholder, maxlength: "500" });
  const send = h("button", { class: "send", title: "Ask", disabled: true }, icon("send"));
  input.addEventListener("input", () => { send.disabled = !input.value.trim() || busy(); });
  const go = () => { const q = input.value.trim(); if (q && !busy()) onAsk(q); };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  send.addEventListener("click", go);
  return { box: h("div", { class: "ask-box" }, icon("sparkle"), input, send), input };
}

function askView(c) {
  const sec = h("section", { class: "section ask" }, h("h2", {}, icon("sparkle"), "Ask about this meeting"));
  for (const qa of c.asks || []) sec.append(qaCard(qa, (t) => jumpTo(t, false)));
  if (state.asking) sec.append(h("div", { class: "ask-wait" }, h("span", { class: "spinner" }), "Thinking… a local model takes up to a minute."));
  const { box } = askBox("What did we decide about…? Who owns…?", () => state.asking, async (q) => {
    state.asking = true;
    renderCall();
    try {
      const entry = await api(`/api/calls/${c.id}/ask`, { method: "POST", body: { question: q } });
      if (state.call && state.call.id === c.id) state.call.asks = [...(state.call.asks || []), entry];
    } catch (e) { toast(e.message); }
    state.asking = false;
    if (state.call && state.call.id === c.id) renderCall();
  });
  sec.append(box);
  return sec;
}

function qaCard(qa, onCite) {
  return h("div", { class: "qa" },
    h("div", { class: "qa-q" }, qa.question),
    h("div", { class: "qa-a" }, qa.answer),
    qa.citations.length ? h("div", { class: "qa-cites" }, qa.citations.map((t) =>
      typeof t === "number"
        ? h("button", { class: "cite", title: "Jump to this moment", onclick: () => onCite(t) }, clock(t))
        : h("a", { class: "cite call", href: `#/call/${t.call}/transcript?t=${t.t}`, title: "Open this moment" }, `${t.title} · ${clock(t.t)}`))) : null,
    h("div", { class: "qa-meta" }, cloudBadge(qa.backend), [qa.backend, qa.partial ? "searched the most relevant parts" : "",
      qa.searched !== undefined ? `searched ${qa.searched} call${qa.searched === 1 ? "" : "s"}` : ""].filter(Boolean).join(" · ")));
}

// Transcript tab -------------------------------------------------------------

function transcriptView(c) {
  const lines = c.lines || [];
  const wrap = h("div", {});
  if (!lines.length) {
    wrap.append(h("p", { class: "empty-line" }, c.has_transcript ? "No speech was recognized." : "The transcript appears here once the call is transcribed."));
    return wrap;
  }
  const count = h("span", { class: "t-count" });
  const find = h("input", { type: "search", placeholder: "Find in transcript", value: state.find });
  const body = h("div", {});
  const draw = () => {
    const q = state.find.trim();
    let hits = 0;
    body.replaceChildren(...lines.map((l) => {
      if (q && l.text.toLowerCase().includes(q.toLowerCase())) hits++;
      const me = l.speaker === "Me";
      const name = nameOf(l.speaker);
      const who = me ? h("div", { class: "t-speaker me" }, "Me")
        : h("button", { class: "t-speaker others", title: "Name this speaker", onclick: () => speakerDialog(c, l.speaker) }, name);
      if (!me) who.style.setProperty("color", colorFor(l.speaker));
      return h("div", { class: "t-line", "data-start": String(l.start) },
        h("button", { class: "t-time", title: "Play from here", onclick: () => { seekAudio(l.start); playAudio(); } }, clock(l.start)),
        h("div", {}, who, h("div", { class: "t-text" }, highlighted(l.text, q))));
    }));
    count.textContent = q ? `${hits} match${hits === 1 ? "" : "es"}` : "";
    const first = body.querySelector("mark");
    if (first) first.closest(".t-line").scrollIntoView({ block: "center" });
    markPlaying();
  };
  find.addEventListener("input", () => { state.find = find.value; draw(); });
  const runs = c.runs || [];
  const langs = [...new Set(runs.map((r) => r.language).filter(Boolean))];
  wrap.append(h("div", { class: "t-tools" },
    h("label", { class: "t-find" }, icon("search"), find), count,
    h("span", { class: "t-info" }, [c.transcript_model, langs.join(", "), c.echo_dropped ? `${c.echo_dropped} echo line${c.echo_dropped > 1 ? "s" : ""} removed` : ""].filter(Boolean).join(" · "))));
  wrap.append(body);
  draw();
  return wrap;
}

// Ask across meetings ----------------------------------------------------------

async function renderAsk() {
  state.view = "ask";
  state.call = null;
  setPlayer(null);
  renderList();
  renderBanner();
  const history = h("div", {});
  const wait = h("div", { class: "ask-wait", hidden: true }, h("span", { class: "spinner" }), "Searching your meetings… a local model takes up to a minute.");
  const ask = async (q) => {
    state.askingAll = true;
    wait.hidden = false;
    try {
      const entry = await api("/api/ask", { method: "POST", body: { question: q } });
      history.prepend(qaCard(entry));
      input.value = "";
    } catch (e) { toast(e.message); }
    state.askingAll = false;
    wait.hidden = true;
  };
  const { box, input } = askBox("Ask anything about your past meetings…", () => state.askingAll, ask);
  const examples = ["What did I promise to do this week?", "What was decided about the release date?", "Which open questions are still unanswered?"];
  $("#main").replaceChildren(h("div", { class: "page" },
    h("h1", { class: "page-title" }, "Ask your meetings"),
    h("div", { class: "page-sub" }, "Answers come from your transcripts, with links to the exact moments."),
    h("div", { class: "big-ask" }, box),
    h("div", { class: "examples" }, examples.map((x) => h("button", { onclick: () => { input.value = x; ask(x); } }, x))),
    h("div", { class: "section" }, wait, history)));
  input.focus();
  try {
    const { asks } = await api("/api/asks");
    history.replaceChildren(...asks.slice().reverse().map((qa) => qaCard(qa)));
  } catch (e) { /* history is optional */ }
}

// Action items -----------------------------------------------------------------

async function renderTasks() {
  state.view = "tasks";
  state.call = null;
  setPlayer(null);
  renderList();
  renderBanner();
  const page = h("div", { class: "page" }, h("h1", { class: "page-title" }, "Action items"),
    h("div", { class: "page-sub" }, "Everything agreed in your meetings. Tick items off here or in the meeting."));
  $("#main").replaceChildren(page);
  let items;
  try { ({ items } = await api("/api/actions")); } catch (e) { page.append(h("p", { class: "hint" }, e.message)); return; }
  const mine = items.filter((a) => !a.done && a.owner === "Me");
  const others = items.filter((a) => !a.done && a.owner !== "Me");
  const done = items.filter((a) => a.done);
  const section = (title, list, empty) => h("section", { class: "section" },
    h("h2", {}, title, list.length ? h("span", { class: "n" }, String(list.length)) : null),
    list.length ? list.map((a) => actionItem(a.call, a, a.index, true)) : h("div", { class: "empty-line" }, empty));
  page.append(section("Yours", mine, "Nothing open for you."));
  page.append(section("Others", others, "Nothing open for others."));
  if (done.length) {
    const toggle = h("button", { class: "btn-ghost toggle-done", onclick: () => { state.showDone = !state.showDone; renderTasks(); } },
      state.showDone ? `Hide ${done.length} done` : `Show ${done.length} done`);
    page.append(toggle);
    if (state.showDone) page.append(section("Done", done, ""));
  }
}

// Settings ---------------------------------------------------------------------

async function renderSettings() {
  state.view = "settings";
  state.call = null;
  setPlayer(null);
  renderList();
  renderBanner();
  const page = h("div", { class: "page" }, h("h1", { class: "page-title" }, "Settings"));
  $("#main").replaceChildren(page);
  let s;
  try { s = await api("/api/settings"); } catch (e) { page.append(h("p", { class: "hint" }, e.message)); return; }

  const vocab = h("textarea", { placeholder: "One per line, e.g.\nAnna Schmidt\nJonas Weber\nSuchindex\nRocket.Chat" });
  vocab.value = s.vocabulary.join("\n");
  const saveVocab = h("button", { class: "btn btn-primary" }, "Save");
  saveVocab.onclick = async () => {
    try {
      const r = await api("/api/vocabulary", { method: "POST", body: { terms: vocab.value.split("\n") } });
      vocab.value = r.terms.join("\n");
      toast(`Saved ${r.terms.length} term${r.terms.length === 1 ? "" : "s"}. Used for the next transcripts.`);
    } catch (e) { toast(e.message); }
  };
  page.append(h("div", { class: "settings-card" },
    h("h2", {}, "Vocabulary"),
    h("p", {}, "Names of colleagues, products and terms the transcript should spell right. Also helps the summary."),
    vocab, h("div", { class: "row-end" }, saveVocab)));

  const voices = h("div", {});
  const drawVoices = () => voices.replaceChildren(...(s.voices.length ? s.voices.map((v) => h("div", { class: "voice-row" },
    avatar(v.name), h("span", { class: "grow" }, v.name), h("span", { class: "muted" }, `${v.samples} call${v.samples === 1 ? "" : "s"}`),
    h("button", { class: "btn-ghost", onclick: async () => {
      if (!await confirmDialog(`Forget ${v.name}'s voice?`, "Future calls will show this person as “Speaker N” until you name them again.", "Forget voice")) return;
      try { await api(`/api/voices/${encodeURIComponent(v.name)}`, { method: "DELETE" }); s.voices = s.voices.filter((x) => x.name !== v.name); drawVoices(); }
      catch (e) { toast(e.message); }
    } }, "Forget"))) : [h("div", { class: "empty-line" }, "None yet. Name a speaker in a meeting's transcript and tick “Recognize this voice”.")]));
  drawVoices();
  page.append(h("div", { class: "settings-card" },
    h("h2", {}, "Remembered voices"),
    h("p", {}, "People recognized automatically in new calls. Voice prints stay on this laptop."), voices));

  const checksBox = h("div", {}, h("div", { class: "hint" }, "Checking…"));
  const rerun = h("button", { class: "btn" }, "Check again");
  const drawChecks = (force) => loadDoctor(force)
    .then((d) => checksBox.replaceChildren(checksList(d.checks)))
    .catch((e) => checksBox.replaceChildren(h("div", { class: "hint" }, e.message)));
  rerun.onclick = () => { checksBox.replaceChildren(h("div", { class: "hint" }, "Checking…")); drawChecks(true); };
  page.append(h("div", { class: "settings-card" },
    h("h2", {}, "Setup check"),
    h("p", {}, "The same checks as wirenote doctor. Click a command to copy it."),
    checksBox, h("div", { class: "row-end" }, rerun)));
  drawChecks(false);

  page.append(setupCard(s));
  page.append(calendarsCard(s));
  try { state.updates = await api("/api/updates"); page.append(updatesCard(state.updates)); } catch { /* shown on the Updates page */ }
}

const BACKENDS = [["ollama", "On this laptop (Ollama)"], ["anthropic", "Claude (sent to Anthropic)"], ["none", "Off"]];

function field(label, control, hint) {
  return h("div", { class: "field" }, h("label", {}, label), control, hint ? h("div", { class: "hint" }, hint) : null);
}

// Summaries, transcription, consent line and the Claude key (PUT /api/settings).
function setupCard(s) {
  const e = s.editable;
  const text = (key, placeholder) => h("input", { type: "text", value: e[key], placeholder, maxlength: "500", spellcheck: "false" });
  const backend = h("select", {}, BACKENDS.map(([v, label]) => h("option", { value: v, selected: v === e.summary_backend }, label)));
  const inputs = {
    summary_backend: backend,
    ollama_model: text("ollama_model"), anthropic_model: text("anthropic_model"),
    summary_language: text("summary_language", "auto"),
    whisper_model: text("whisper_model"), whisper_language: text("whisper_language", "auto"),
    asr_engine: h("select", {}, ENGINES.map(([v, label]) => h("option", { value: v, selected: v === e.asr_engine }, label))),
    consent_line: h("textarea", { class: "short", maxlength: "500", placeholder: "e.g. Is it OK if I record this call for my notes?" }),
    live_transcript: h("input", { type: "checkbox", id: "live-transcript" }),
  };
  inputs.consent_line.value = e.consent_line;
  inputs.live_transcript.checked = e.live_transcript;
  const value = (el) => (el.type === "checkbox" ? el.checked : el.value);
  const ollamaRow = field("Ollama model", inputs.ollama_model);
  const claudeRow = field("Claude model", inputs.anthropic_model);
  const keyBox = h("div", {});
  const showFor = () => {
    ollamaRow.hidden = backend.value !== "ollama";
    claudeRow.hidden = keyBox.hidden = backend.value !== "anthropic";
  };
  backend.onchange = showFor;

  const keyInput = h("input", { type: "password", autocomplete: "off", placeholder: s.anthropic_key_set ? "A key is saved; paste a new one to replace it" : "sk-ant-…" });
  const saveKey = h("button", { class: "btn" }, "Save key");
  saveKey.onclick = async () => {
    try {
      await api("/api/settings/anthropic-key", { method: "POST", body: { key: keyInput.value } });
      keyInput.value = "";
      keyInput.placeholder = "A key is saved; paste a new one to replace it";
      toast("Key saved. It stays on this laptop, in secrets.toml.");
    } catch (err) { toast(err.message); }
  };
  keyBox.append(field("Claude API key", h("div", { class: "inline" }, keyInput, saveKey),
    "Kept in secrets.toml on this laptop; never shown here again."));
  showFor();

  const save = h("button", { class: "btn btn-primary" }, "Save");
  save.onclick = async () => {
    const changes = {};
    for (const [k, el] of Object.entries(inputs)) if (value(el) !== e[k]) changes[k] = value(el);
    if (!Object.keys(changes).length) { toast("Nothing changed."); return; }
    let confirm_cloud = false;
    if (changes.summary_backend === "anthropic") {
      if (!await confirmDialog("Send call data to Claude?", s.cloud_disclosure, "Use Claude")) return;
      confirm_cloud = true;
    }
    try {
      const r = await api("/api/settings", { method: "PUT", body: { changes, confirm_cloud } });
      Object.assign(s, r);
      Object.assign(e, r.editable);
      state.doctor = null;
      toast("Saved. New calls use these settings.");
    } catch (err) { toast(err.message); }
  };

  const kv = (k, v) => [h("dt", {}, k), h("dd", {}, v)];
  return h("div", { class: "settings-card" },
    h("h2", {}, "Setup"),
    h("p", {}, "Summaries, transcription and the consent line. Everything else is in config.toml."),
    field("Summaries and Ask", backend, "Ollama and Off keep everything on this laptop."),
    ollamaRow, claudeRow, keyBox,
    field("Summary language", inputs.summary_language, "auto: the language of the call, or e.g. English, German."),
    field("Whisper model", inputs.whisper_model, "e.g. large-v3-turbo, medium, small."),
    field("Call language", inputs.whisper_language, "auto, or a code like en, de."),
    field("Speech engine", inputs.asr_engine, "Automatic uses Parakeet, several times faster on a CPU, when it knows your language and no vocabulary is set; otherwise Whisper. whisper.cpp uses a GPU if you installed a whisper-cli built for it."),
    h("label", { class: "check-row tight", for: "live-transcript" }, inputs.live_transcript, "Live transcript during calls"),
    h("div", { class: "hint below-check" }, "See what is said while you record, a few seconds behind. It uses two CPU threads during the call. If its model is not downloaded yet, it starts after your next note."),
    field("Consent line", inputs.consent_line, "Copied to the clipboard when you press Record, to paste into the call chat."),
    h("div", { class: "row-end" }, save),
    h("dl", { class: "kv" },
      kv("Speakers", s.diarize ? "told apart" : "not told apart"),
      kv("Audio kept for", `${s.audio_retention_days} days after the note`),
      kv("Notes folder", s.notes_dir)));
}

// Calendar links are secret: the page only ever sees an id and the host name.
function calendarsCard(s) {
  const list = h("div", {});
  const draw = () => list.replaceChildren(...(s.calendars.length ? s.calendars.map((c) => h("div", { class: "voice-row" },
    h("span", { class: "grow" }, c.label), h("span", { class: "muted" }, c.kind === "link" ? "link" : "file in config.toml"),
    c.kind === "link" ? h("button", { class: "btn-ghost", onclick: async () => {
      if (!await confirmDialog(`Remove the calendar from ${c.label}?`, "Wirenote stops reading it. You can add the link again later.", "Remove")) return;
      try { s.calendars = (await api(`/api/calendars/${encodeURIComponent(c.id)}`, { method: "DELETE" })).calendars; draw(); }
      catch (e) { toast(e.message); }
    } }, "Remove") : null)) : [h("div", { class: "empty-line" }, "None yet. Calendars give calls their title and invitees.")]));
  draw();
  const link = h("input", { type: "password", autocomplete: "off", placeholder: "https://… or webcal://… (.ics)" });
  const add = h("button", { class: "btn" }, "Add");
  add.onclick = async () => {
    add.disabled = true;
    try {
      s.calendars = (await api("/api/calendars", { method: "POST", body: { url: link.value } })).calendars;
      link.value = "";
      draw();
      toast("Calendar added.");
    } catch (e) { toast(e.message); }
    add.disabled = false;
  };
  return h("div", { class: "settings-card" },
    h("h2", {}, "Calendars"),
    h("p", {}, "Paste a calendar's secret ICS link (Outlook: Publish a calendar › ICS; Google: Secret address in iCal format). It stays in secrets.toml and is not shown again."),
    list, h("div", { class: "field inline-add" }, h("div", { class: "inline" }, link, add)));
}

// Updates (docs/dev/updates-design.md) -----------------------------------------

// Changelog text: `code` and [links](https://…) become elements, everything else stays text.
function inlineMd(text) {
  const frag = document.createDocumentFragment();
  const re = /`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;
  let i = 0, m;
  while ((m = re.exec(text))) {
    frag.append(text.slice(i, m.index),
      m[1] !== undefined ? h("code", {}, m[1]) : h("a", { href: m[3], target: "_blank", rel: "noopener noreferrer" }, m[2]));
    i = re.lastIndex;
  }
  frag.append(text.slice(i));
  return frag;
}

const fmtReleaseDate = (d) => new Date(`${d}T12:00:00`).toLocaleDateString(LOCALE, { day: "numeric", month: "long", year: "numeric" });
const fmtWhen = (ts) => { const d = new Date(ts * 1000); return `${fmtShort(d)}, ${fmtTime(d)}`; };

function releaseNotes(releases) {
  return h("div", { class: "release-notes" }, releases.map((r) => h("section", { class: "release" },
    h("h3", {}, `Wirenote ${r.version}`, r.date ? h("span", { class: "muted" }, ` · ${fmtReleaseDate(r.date)}`) : null),
    r.intro ? h("p", {}, inlineMd(r.intro)) : null,
    r.groups.flatMap((g) => [h("h4", {}, g.title), h("ul", {}, g.items.map((it) => h("li", {}, inlineMd(it))))]))));
}

const RUN_TEXT = {
  starting: "Starting the update…", waiting: "The update waits for the recording to end…",
  installing: "Installing the new version…", restarting: "Restarting Wirenote…",
};
const updateRunning = (u) => !!(u && u.run && RUN_TEXT[u.run.state]);

function updateDismissed(version) {
  try { return localStorage.getItem("wirenote-update-hidden") === version; } catch { return false; }
}

function hideUpdate(version) {
  try { localStorage.setItem("wirenote-update-hidden", version); } catch { /* private window: hidden until reload */ }
  $("#update-banner").hidden = true;
}

function renderUpdateBanner() {
  const b = $("#update-banner");
  const u = state.updates;
  const avail = u && u.available;
  const running = updateRunning(u);
  if (state.view === "updates" || !(running || (avail && !updateDismissed(avail.version)))) { b.hidden = true; return; }
  if (running) {
    b.replaceChildren(h("span", { class: "dot work" }), h("b", {}, RUN_TEXT[u.run.state]), h("span", { class: "grow" }),
      h("a", { class: "btn", href: "#/updates" }, "Details"));
  } else {
    b.replaceChildren(icon("sparkle"), h("b", {}, `Wirenote ${avail.version} is available`), h("span", { class: "grow" }),
      h("a", { class: "btn", href: "#/updates" }, "See what's new"),
      u.can_update ? h("button", { class: "btn btn-primary", onclick: startUpdate }, "Update") : null,
      h("button", { class: "btn-ghost", title: "Hide until the next version", onclick: () => hideUpdate(avail.version) }, "Not now"));
  }
  b.hidden = false;
}

async function refreshUpdates() {
  try { state.updates = await api("/api/updates"); } catch { return; }
  renderUpdateBanner();
}

async function startUpdate() {
  const u = state.updates;
  const text = state.status.recording
    ? "A call is recording. Wirenote updates as soon as it ends, so the recording is not cut short."
    : "Wirenote restarts for a moment; nothing is lost. This page reloads when the new version runs.";
  if (!await confirmDialog(`Update to Wirenote ${u.available.version}?`, text, "Update", false)) return;
  try { state.updates = await api("/api/updates/install", { method: "POST" }); }
  catch (e) { toast(e.message); return; }
  followUpdate();
}

// Follows the update through the restart: the server goes away, comes back on the
// new version, and the page reloads into "What's new".
async function followUpdate() {
  if (followUpdate.running) return;
  followUpdate.running = true;
  const from = state.updates.current;
  const redraw = () => {
    renderUpdateBanner();
    const card = $("#updates-card");
    if (card) card.replaceWith(updatesCard(state.updates));
  };
  redraw();
  for (;;) {
    await new Promise((r) => setTimeout(r, 2000));
    let u;
    try { u = await api("/api/updates"); } catch { continue; }   // restarting
    if (u.current !== from) { location.reload(); return; }
    state.updates = u;
    redraw();
    if (!updateRunning(u)) break;
  }
  followUpdate.running = false;
  const run = state.updates.run || {};
  if (run.state === "failed") toast(`The update failed: ${run.message || "run wirenote update in a terminal to see why"}`);
  else if (run.state === "done") toast(`Wirenote ${run.to} is installed. Restart Wirenote to use it.`);
}

function updatesCard(u) {
  const running = updateRunning(u);
  const status = running ? RUN_TEXT[u.run.state]
    : u.available ? `Wirenote ${u.available.version} is available. You have ${u.current}.`
    : u.checked && !u.error ? `You have Wirenote ${u.current}, the newest version (checked ${fmtWhen(u.checked)}).`
    : `You have Wirenote ${u.current}.`;
  const card = h("div", { class: "settings-card", id: "updates-card" }, h("h2", {}, "Updates"), h("p", {}, status));
  if (u.error && !running) card.append(h("div", { class: "notice bad" }, `The last check failed: ${u.error}`));
  if (u.run && u.run.state === "failed" && !running) {
    card.append(h("div", { class: "notice bad" }, `The last update failed: ${u.run.message || "unknown error"}. Try again, or run wirenote update in a terminal.`));
  }
  if (u.available && !running) {
    card.append(u.available.notes.length ? releaseNotes(u.available.notes)
      : h("p", {}, h("a", { href: u.available.url, target: "_blank", rel: "noopener noreferrer" }, "Release notes on GitHub")));
  }
  if (!u.can_update) card.append(h("p", { class: "hint" }, u.how));

  const toggle = h("input", { type: "checkbox", id: "update-check" });
  toggle.checked = u.check;
  toggle.onchange = async () => {
    try {
      state.updates = await api("/api/updates/setting", { method: "POST", body: { check: toggle.checked } });
      toast(toggle.checked ? "Wirenote now checks once a day and tells you about new versions." : "Update check off.");
    } catch (e) { toggle.checked = !toggle.checked; toast(e.message); }
  };
  const checkNow = h("button", { class: "btn" }, "Check now");
  checkNow.onclick = async () => {
    checkNow.disabled = true;
    try {
      state.updates = await api("/api/updates/check", { method: "POST" });
      card.replaceWith(updatesCard(state.updates));
      renderUpdateBanner();
      toast(state.updates.error ? "Could not reach PyPI." : state.updates.available ? `Wirenote ${state.updates.available.version} is available.` : "You have the newest version.");
    } catch (e) { toast(e.message); checkNow.disabled = false; }
  };
  card.append(h("label", { class: "check-row", for: "update-check" }, toggle, "Check for new versions once a day"),
    h("p", { class: "hint" }, u.disclosure),
    h("div", { class: "row-end" }, checkNow,
      u.available && u.can_update && !running ? h("button", { class: "btn btn-primary", onclick: startUpdate }, `Update to ${u.available.version}`) : null));
  return card;
}

async function renderUpdates() {
  state.view = "updates";
  state.call = null;
  setPlayer(null);
  renderList();
  renderBanner();
  const page = h("div", { class: "page" }, h("h1", { class: "page-title" }, "Updates"),
    h("div", { class: "page-sub" }, "Your version, new releases, and what changed in each."));
  $("#main").replaceChildren(page);
  try {
    [state.updates, state.changelog] = await Promise.all([api("/api/updates"), state.changelog || api("/api/changelog")]);
  } catch (e) { page.append(h("p", { class: "hint" }, e.message)); return; }
  if (state.view !== "updates") return;
  page.append(updatesCard(state.updates),
    h("h2", { class: "section-title" }, "Release notes"), releaseNotes(state.changelog.releases));
}

function whatsNewDialog(w) {
  const seen = () => api("/api/updates/seen", { method: "POST" }).catch(() => { /* shown again next time */ });
  const ok = h("button", { class: "btn btn-primary" }, "Got it");
  const all = h("a", { class: "btn", href: "#/updates" }, "All release notes");
  const close = openModal(h("div", { class: "dialog wide whats-new", role: "dialog", "aria-modal": "true", "aria-labelledby": "whats-new-title" },
    h("h3", { id: "whats-new-title" }, `What's new in Wirenote ${w.to}`),
    w.from ? h("p", {}, `Updated from ${w.from}.`) : null,
    releaseNotes(w.releases),
    h("div", { class: "row" }, all, ok)), seen);
  ok.onclick = close;
  all.addEventListener("click", () => close());
  ok.focus();
}

// Player ---------------------------------------------------------------------

const audio = () => $("#audio");

function setPlayer(c) {
  const p = $("#player"), a = audio();
  if (c && a.dataset.call === c.id && c.has_audio) { p.hidden = false; return; }
  a.pause();
  a.removeAttribute("src");
  a.dataset.call = "";
  $("#p-play").classList.remove("loading");
  if (!c || !c.has_audio || c.state === "recording") { p.hidden = true; return; }
  a.dataset.call = c.id;
  $("#p-dur").textContent = clock(c.duration_s);
  $("#p-time").textContent = "0:00";
  $("#p-scrub").value = 0;
  p.hidden = false;
}

function ensureSrc() {
  const a = audio();
  if (!a.getAttribute("src") && a.dataset.call) {
    $("#p-play").classList.add("loading");
    a.src = `/api/calls/${encodeURIComponent(a.dataset.call)}/audio`;
  }
}

function playAudio() {
  if (!state.call || !state.call.has_audio) return;
  ensureSrc();
  audio().play().catch((e) => { $("#p-play").classList.remove("loading"); toast(`Cannot play: ${e.message}`); });
}

function seekAudio(t) {
  const a = audio();
  if (!a.dataset.call) return;
  ensureSrc();
  const apply = () => { a.currentTime = t; };
  if (a.readyState >= 1) apply(); else a.addEventListener("loadedmetadata", apply, { once: true });
}

function markPlaying() {
  const a = audio();
  if (!a.getAttribute("src") || a.paused) {
    document.querySelectorAll(".t-line.playing").forEach((el) => el.classList.remove("playing"));
    return;
  }
  let cur = null;
  for (const el of document.querySelectorAll(".t-line")) {
    if (Number(el.dataset.start) <= a.currentTime) cur = el; else break;
  }
  document.querySelectorAll(".t-line.playing").forEach((el) => { if (el !== cur) el.classList.remove("playing"); });
  if (cur && !cur.classList.contains("playing")) cur.classList.add("playing");
}

function wirePlayer() {
  const a = audio(), play = $("#p-play"), scrub = $("#p-scrub");
  let dragging = false;
  play.addEventListener("click", () => (a.paused ? playAudio() : a.pause()));
  $("#p-back").addEventListener("click", () => seekAudio(Math.max(0, a.currentTime - 15)));
  $("#p-fwd").addEventListener("click", () => seekAudio(a.currentTime + 15));
  const speeds = [1, 1.25, 1.5, 2];
  $("#p-speed").addEventListener("click", (e) => {
    const next = speeds[(speeds.indexOf(a.playbackRate) + 1) % speeds.length];
    a.playbackRate = next;
    e.currentTarget.textContent = `${next}×`;
  });
  a.addEventListener("play", () => { play.replaceChildren(icon("pause")); });
  a.addEventListener("pause", () => { play.replaceChildren(icon("play")); markPlaying(); });
  a.addEventListener("playing", () => play.classList.remove("loading"));
  a.addEventListener("canplay", () => play.classList.remove("loading"));
  a.addEventListener("error", () => { play.classList.remove("loading"); if (a.getAttribute("src")) toast("Could not load the audio"); });
  a.addEventListener("loadedmetadata", () => { if (isFinite(a.duration)) $("#p-dur").textContent = clock(a.duration); });
  a.addEventListener("timeupdate", () => {
    $("#p-time").textContent = clock(a.currentTime);
    if (!dragging && isFinite(a.duration) && a.duration) scrub.value = Math.round((a.currentTime / a.duration) * 1000);
    markPlaying();
  });
  const dur = () => (isFinite(a.duration) && a.duration ? a.duration : state.call ? state.call.duration_s : 0);
  scrub.addEventListener("input", () => { dragging = true; $("#p-time").textContent = clock((scrub.value / 1000) * dur()); });
  scrub.addEventListener("change", () => { dragging = false; seekAudio((scrub.value / 1000) * dur()); });
}

// Routing and refresh --------------------------------------------------------

function route() {
  const hash = location.hash;
  const m = hash.match(/^#\/call\/([^/?]+)(?:\/(transcript))?(?:\?t=(\d+(?:\.\d+)?))?$/);
  if (m) return openCall(decodeURIComponent(m[1]), m[2] || "notes", m[3] !== undefined ? Number(m[3]) : undefined);
  if (hash === "#/ask") return renderAsk();
  if (hash === "#/tasks") return renderTasks();
  if (hash === "#/settings") return renderSettings();
  if (hash === "#/updates") return renderUpdates();
  return renderHome();
}

async function refreshList() {
  try {
    const { calls } = await api(`/api/calls?q=${encodeURIComponent(state.query)}`);
    state.calls = calls;
    renderList();
  } catch (e) { /* server restarting; the next poll retries */ }
}

async function refreshStatus() {
  try {
    state.status = await api("/api/status");
  } catch { state.status = {}; }
  renderSidebarStatus();
  renderBanner();
  const st = state.status;
  const key = JSON.stringify([st.recording_id, st.jobs]);
  if (key !== state.statusKey) {
    const first = state.statusKey === "";
    state.statusKey = key;
    if (!first) {
      await refreshList();
      if (state.view === "call" && state.call && !state.asking) {
        const fresh = state.calls.find((c) => c.id === state.call.id);
        if (fresh && fresh.state !== state.call.state) openCall(state.call.id);
      } else if (state.view === "home") renderHome();
    }
  }
}

function wireKeys() {
  const search = $("#search");
  let timer;
  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.query = search.value; refreshList(); }, 180);
  });
  search.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { search.value = ""; state.query = ""; refreshList(); search.blur(); }
    if (e.key === "Enter" && state.calls[0]) {
      const c = state.calls[0];
      location.hash = c.match && c.match.start !== undefined ? `#/call/${c.id}/transcript?t=${c.match.start}` : `#/call/${c.id}`;
    }
  });
  $("#record-btn").addEventListener("click", startRecording);
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA)$/.test(e.target.tagName) || e.target.isContentEditable;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); search.focus(); search.select(); return; }
    if (e.key === "Escape" && $("#modal").hidden === false) { $("#modal").click(); return; }
    if (typing) return;
    if (e.key === "/") { e.preventDefault(); search.focus(); }
    else if (e.key === " " && state.call && state.call.has_audio) { e.preventDefault(); audio().paused ? playAudio() : audio().pause(); }
    else if (e.key === "ArrowLeft" && state.call && audio().getAttribute("src")) seekAudio(Math.max(0, audio().currentTime - 15));
    else if (e.key === "ArrowRight" && state.call && audio().getAttribute("src")) seekAudio(audio().currentTime + 15);
  });
}

async function start() {
  wirePlayer();
  wireKeys();
  wireImport();
  await Promise.all([refreshStatus(), refreshList()]);
  window.addEventListener("hashchange", route);
  route();
  setInterval(refreshStatus, 3000);
  setInterval(() => { if (state.status.recording) renderBanner(); }, 1000);
  await refreshUpdates();
  if (state.updates && state.updates.whats_new) whatsNewDialog(state.updates.whats_new);
  if (updateRunning(state.updates)) followUpdate();
  setInterval(() => { if (!followUpdate.running) refreshUpdates(); }, 10 * 60 * 1000);
}

start();
