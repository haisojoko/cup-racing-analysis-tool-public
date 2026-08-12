"use strict";

const historyEl = document.getElementById("history");
const statusEl = document.getElementById("status");
const pendingEl = document.getElementById("pending");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const clearBtn = document.getElementById("clear");

let busy = false;
let pendingCache = [];

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function atBottom() {
  return historyEl.scrollHeight - historyEl.scrollTop - historyEl.clientHeight < 80;
}
function scrollDown(force) {
  if (force || atBottom()) historyEl.scrollTop = historyEl.scrollHeight;
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

if (window.marked) marked.setOptions({ gfm: true, breaks: true });

// Render markdown safely. marked turns the text into HTML; DOMPurify strips any
// scripts/event handlers (the model — or a pasted interview it echoes — could
// otherwise inject markup). Falls back to escaped plain text if either is absent.
function renderMarkdown(text) {
  const t = text || "";
  try {
    const html = window.marked ? marked.parse(t) : escapeHtml(t);
    return window.DOMPurify ? DOMPurify.sanitize(html) : html;
  } catch (e) {
    return escapeHtml(t);
  }
}

function setBubble(bubble, role, text) {
  if (role === "assistant") {
    bubble.classList.add("md");
    bubble.innerHTML = renderMarkdown(text);
  } else {
    bubble.textContent = text || "";
  }
}

function addMessage(role, text) {
  const wrap = el("div", "msg " + role);
  wrap.appendChild(el("div", "who", role === "user" ? "you" : role === "assistant" ? "buddy" : "note"));
  const bubble = el("div", "bubble");
  setBubble(bubble, role, text);
  wrap.appendChild(bubble);
  historyEl.appendChild(wrap);
  scrollDown(true);
  return bubble;
}

function addToolLine(summary) {
  let group = historyEl.lastElementChild;
  if (!group || !group.classList.contains("tools")) {
    group = el("div", "tools");
    historyEl.appendChild(group);
  }
  group.appendChild(el("div", "tool", "→ " + summary));
  scrollDown();
}

function renderEmpty() {
  if (historyEl.children.length === 0) {
    historyEl.appendChild(el("div", "empty",
      "Ask about a driver, a season, or paste an interview to get started."));
  }
}

function rowBlock(label, text) {
  const r = el("div", "row");
  r.appendChild(el("span", "label", label + ":"));
  r.appendChild(el("pre", null, text || "(empty)"));
  return r;
}

function setPending(list) {
  pendingCache = list || [];
  pendingEl.innerHTML = "";
  pendingCache.forEach((p) => {
    const card = el("div", "proposal");
    card.dataset.id = String(p.id);
    card.appendChild(el("h3", null, `Profile update proposed — your call (#${p.id})`));
    const meta = el("div", "row");
    meta.innerHTML = `<span class="label">Driver:</span> ${escapeHtml(p.driver)} &nbsp; ` +
                     `<span class="label">Section:</span> ${escapeHtml(p.section)}`;
    card.appendChild(meta);
    card.appendChild(rowBlock("Current", p.current));
    card.appendChild(rowBlock("Proposed", p.proposed));
    if (p.rationale) {
      const r = el("div", "row");
      r.innerHTML = `<span class="label">Rationale:</span> ${escapeHtml(p.rationale)}`;
      card.appendChild(r);
    }
    const actions = el("div", "actions");
    const apply = el("button", "apply", "Apply");
    const decline = el("button", "decline", "Decline");
    apply.onclick = () => decide(p.id, true);
    decline.onclick = () => decide(p.id, false);
    actions.appendChild(apply);
    actions.appendChild(decline);
    card.appendChild(actions);
    pendingEl.appendChild(card);
  });
}

function addPending(p) {
  if (pendingCache.some((x) => x.id === p.id)) return;
  setPending([...pendingCache, p]);
}

async function decide(id, yes) {
  pendingEl.querySelectorAll("button").forEach((b) => (b.disabled = true));
  const res = await fetch("/api/approve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, yes }),
  }).then((r) => r.json());
  if (res.message) addMessage("system", res.message);
  setPending(res.pending);
}

function setStatus(s) {
  if (!s) return;
  const key = s.has_key ? "" : " · ⚠ no API key";
  statusEl.textContent = `${s.model} · ${s.drivers} drivers${key}`;
}

async function loadHistory() {
  try {
    const h = await fetch("/api/history").then((r) => r.json());
    setStatus(h.status);
    (h.transcript || []).forEach((m) => addMessage(
      m.role === "user" ? "user" : m.role === "assistant" ? "assistant" : "system", m.content));
    setPending(h.pending);
    renderEmpty();
    scrollDown(true);
  } catch (e) {
    statusEl.textContent = "offline";
  }
}

function handleEvent(ev, bubble) {
  if (ev.event === "tool") {
    bubble.classList.remove("cursor");
    addToolLine(ev.summary);
  } else if (ev.event === "proposal") {
    addPending(ev.proposal);
  } else if (ev.event === "error") {
    addMessage("system", "Error: " + ev.message);
  } else if (ev.event === "notice") {
    bubble.classList.remove("cursor");
    addToolLine(ev.message);
  } else if (ev.event === "final") {
    setStatus(ev.status);
    setPending(ev.pending);
  }
}

async function send(text) {
  busy = true;
  sendBtn.disabled = true;
  addMessage("user", text);
  let bubble = addMessage("assistant", "");
  bubble.classList.add("cursor");
  let acc = "";

  try {
    const res = await fetch("/api/submit_stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const frames = buf.split("\n\n");
      buf = frames.pop();
      for (const frame of frames) {
        const line = frame.trim();
        if (!line.startsWith("data:")) continue;
        let ev;
        try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }
        if (ev.event === "delta") {
          // a new round of prose after a tool call needs a fresh bubble
          if (!bubble.isConnected || bubble.dataset.closed) {
            bubble = addMessage("assistant", "");
            acc = "";
          }
          bubble.classList.add("cursor");
          acc += ev.text;
          bubble.innerHTML = renderMarkdown(acc);
          scrollDown();
        } else if (ev.event === "reset") {
          // A retry regenerates the reply from scratch, so the partial text in
          // this bubble is garbage. Dropping the bubble from the DOM makes
          // `isConnected` false, so the retry's first delta opens a fresh bubble
          // *below* the retry notice instead of writing back in above it.
          acc = "";
          bubble.closest(".msg")?.remove();
        } else {
          if (ev.event === "tool") bubble.dataset.closed = "1";
          handleEvent(ev, bubble);
        }
      }
    }
  } catch (e) {
    addMessage("system", "Connection error: " + e.message);
  }

  bubble.classList.remove("cursor");
  if (!bubble.textContent) bubble.closest(".msg")?.remove();
  busy = false;
  sendBtn.disabled = false;
  input.focus();
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (busy) return;
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  historyEl.querySelector(".empty")?.remove();
  send(text);
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

clearBtn.addEventListener("click", async () => {
  if (busy) return;
  if (!confirm("Clear this conversation? Driver profiles are not affected.")) return;
  const h = await fetch("/api/clear", { method: "POST" }).then((r) => r.json());
  historyEl.innerHTML = "";
  setPending(h.pending);
  renderEmpty();
});

loadHistory();
input.focus();
