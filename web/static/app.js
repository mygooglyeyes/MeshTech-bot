/* MeshTech-Bot dashboard - vanilla JS, no build step. */
"use strict";

const TOKEN_KEY = "mcb_token";
let token = localStorage.getItem(TOKEN_KEY) || "";
let authRequired = false;
let feedPaused = false;
let ws = null;
let lastStatus = null;

const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ fetch helper

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (token) headers["Authorization"] = "Bearer " + token;
  const res = await fetch(path, Object.assign({}, options, { headers }));
  if (res.status === 401) {
    showLogin(true);
    throw new Error("unauthorized");
  }
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// Same as api(), but for endpoints that return plain text (license, notices).
async function apiText(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (token) headers["Authorization"] = "Bearer " + token;
  const res = await fetch(path, Object.assign({}, options, { headers }));
  if (res.status === 401) {
    showLogin(true);
    throw new Error("unauthorized");
  }
  if (!res.ok) throw new Error(await res.text());
  return res.text();
}

// ------------------------------------------------------------------ login

function showLogin(show) {
  $("login-overlay").classList.toggle("hidden", !show);
  if (show) setTimeout(() => $("login-password").focus(), 50);
}

// ------------------------------------------------------------------ about

let aboutTab = "license";

// Canonical upstream repo - the "view this commit" link points here. GitHub
// resolves the short commit SHA the stamp carries (needs the commit to be
// pushed); for unknown/empty stamps the link is hidden.
const GITHUB_REPO = "https://github.com/mygooglyeyes/MeshTech-bot";

function openAbout() {
  const v = (lastStatus && lastStatus.version) || {};
  const vText = v.version ? "v" + v.version : "(unknown version)";
  const cText = v.commit ? (v.branch ? v.branch + "@" + v.commit : v.commit) : "commit: -";
  $("about-version").textContent =
    "MeshTech-Bot " + vText + "\n" +
    cText + "  ·  source: " + (v.source || "?");
  // Link to the exact commit on GitHub (short SHAs resolve there). Hidden
  // when no commit is known (e.g. a build with no git source).
  const linkWrap = $("about-commit-link");
  linkWrap.replaceChildren();
  if (v.commit) {
    const a = document.createElement("a");
    a.href = GITHUB_REPO + "/commit/" + v.commit;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = "View this commit on GitHub";
    linkWrap.appendChild(a);
    linkWrap.classList.remove("hidden");
  } else {
    linkWrap.classList.add("hidden");
  }
  $("about-overlay").classList.remove("hidden");
  loadAboutTab(aboutTab);
}

async function loadAboutTab(tab) {
  aboutTab = tab;
  const name = tab === "notices" ? "Third-party notices" : "License (MIT)";
  $("btn-about-license").classList.toggle("active", tab === "license");
  $("btn-about-notices").classList.toggle("active", tab === "notices");
  const pre = $("about-text");
  pre.textContent = "loading…";
  try {
    pre.textContent = await apiText("/api/legal/" + (tab === "notices" ? "notices" : "license"));
  } catch (e) {
    pre.textContent = "Could not load " + name + ".\n\n" + e.message;
  }
}

$("btn-about").addEventListener("click", openAbout);
$("btn-about-close").addEventListener("click", () => $("about-overlay").classList.add("hidden"));
$("btn-about-license").addEventListener("click", () => loadAboutTab("license"));
$("btn-about-notices").addEventListener("click", () => loadAboutTab("notices"));
// Click on the dark backdrop (outside the box) closes the dialog.
$("about-overlay").addEventListener("click", (e) => {
  if (e.target === $("about-overlay")) $("about-overlay").classList.add("hidden");
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("about-overlay").classList.contains("hidden")) {
    $("about-overlay").classList.add("hidden");
  }
});

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  const password = $("login-password").value;
  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (!res.ok) throw new Error("wrong");
    const data = await res.json();
    token = data.token || "";
    localStorage.setItem(TOKEN_KEY, token);
    showLogin(false);
    refreshAll();
    startPolling();  // timers are skipped at boot when login is required
    connectWs();     // open the live feed - missed on first login (token was empty at boot)
  } catch (err) {
    $("login-error").textContent = "Wrong password.";
  }
});

async function ensureAuth() {
  try {
    const state = await fetch("/api/login").then((r) => r.json());
    authRequired = !!state.auth_required;
    if (authRequired && !token) { showLogin(true); return false; }
    return true;
  } catch (err) {
    return false;
  }
}

// ------------------------------------------------------------------ feed

function feedLine(cls, parts) {
  const div = document.createElement("div");
  div.className = "row " + cls;
  div.textContent = parts.join("  ");
  return div;
}

function appendFeed(html) { // html is a DOM node
  const feed = $("feed");
  if (feed.firstChild && feed.firstChild.nodeName === "EM") feed.innerHTML = "";
  feed.appendChild(html);
  while (feed.childElementCount > 400) feed.removeChild(feed.firstChild);
  if (!feedPaused) feed.scrollTop = feed.scrollHeight;
}

// Catch-up bookkeeping for the live feed.  Every event carries a process
// generation id ("inst") and a monotonic sequence ("seq").  On reconnect
// the page tells the server what it has already rendered, so history is
// not re-pasted over rows that are still on screen.  The seq also drops
// the rare replay/live duplicate that a reconnect race can produce.
let feedInst = null;    // generation we have been rendering
let lastFeedSeq = 0;    // highest sequence rendered from that generation

function handleFeedEvent(event) {
  const hasSeq = typeof event.seq === "number" && event.seq > 0;
  if (hasSeq && event.inst !== undefined && event.inst !== null) {
    if (feedInst !== event.inst) {
      // Bot restarted (sequences restart at 1) - the old baseline is stale.
      feedInst = event.inst;
      lastFeedSeq = 0;
    }
    if (lastFeedSeq > 0 && event.seq <= lastFeedSeq) return;  // already shown
    if (event.seq > lastFeedSeq) lastFeedSeq = event.seq;
  }
  const p = event.payload || {};
  const t = new Date((event.ts || 0) * 1000);
  const ts = t.toTimeString().slice(0, 8);
  switch (event.type) {
    case "message_in":
      appendFeed(feedLine("in", [
        "[in]", ts, p.kind === "dm" ? "DM " + (p.sender || "?") : (p.channel || "?"),
        "hops=" + (p.hops === null || p.hops === undefined ? "?" : p.hops),
        p.text,
      ]));
      break;
    case "message_out":
      appendFeed(feedLine("out", [
        "[out]", ts, p.kind === "dm" ? "DM " + (p.sender || "?") : (p.channel || "?"), p.text,
      ]));
      break;
    case "dropped":
      appendFeed(feedLine("bad", [
        "[skip]", ts, p.reason || "filtered", (p.channel || "") + " " + (p.text || ""),
      ]));
      break;
    case "connected":
      appendFeed(feedLine("sys", ["[conn]", ts, "connected to " + p.host + ":" + p.port]));
      break;
    case "disconnected":
      appendFeed(feedLine("bad", ["[conn]", ts, "disconnected"]));
      break;
    default:
      appendFeed(feedLine("sys", ["[sys]", ts, p.text || event.type]));
  }
}

let wsSeq = 0;

function connectWs() {
  if (authRequired && !token) return;  // wait for login
  wsSeq++;
  const seq = wsSeq;
  if (ws) try { ws.close(); } catch (e) { /* ignore */ }
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") +
                     location.host + "/ws");
  // The token travels as the first frame, never in the URL - so it cannot
  // end up in browser history, access logs or proxy logs. The frame also
  // carries our catch-up position so the server only replays what we have
  // not already rendered (no more duplicated history walls on reconnect).
  // The server closes with 4401 if the token is missing/invalid.
  ws.onopen = () => {
    try {
      ws.send(JSON.stringify({ token, inst: feedInst, last_seq: lastFeedSeq }));
    } catch (e) { /* ignore */ }
  };
  ws.onmessage = (event) => {
    try { handleFeedEvent(JSON.parse(event.data)); } catch (e) { /* ignore */ }
  };
  ws.onclose = (event) => {
    if (seq !== wsSeq) return;  // superseded by a newer connection attempt
    if (event && event.code === 4401) {  // token rejected - force a fresh login
      token = "";
      localStorage.removeItem(TOKEN_KEY);
      showLogin(true);
      return;
    }
    setTimeout(connectWs, 3000);
  };
}

$("btn-pause").addEventListener("click", () => {
  feedPaused = !feedPaused;
  $("btn-pause").textContent = feedPaused ? "resume" : "pause";
});
$("btn-clear-feed").addEventListener("click", () => { $("feed").innerHTML = ""; });

// ------------------------------------------------------------------ status + channels

function fmtUptime(seconds) {
  const s = Math.floor(seconds);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
  if (s < 86400) return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
  return Math.floor(s / 86400) + "d " + Math.floor((s % 86400) / 3600) + "h";
}

async function refreshStatus() {
  const st = await api("/api/status");
  lastStatus = st;
  const conn = st.connection;
  const chip = $("chip-conn");
  chip.textContent = conn && conn.connected ? "connected " + conn.host + ":" + conn.port
                                            : "not connected";
  chip.className = "chip " + (conn && conn.connected ? "ok" : "bad");
  $("chip-uptime").textContent = "up " + fmtUptime(st.uptime_seconds);
  // Build stamp: release version + the git commit it runs (v0.0.1).
  // Hover shows the full detail (version, branch, commit, source).
  const v = st.version || {};
  const vChip = $("chip-version");
  const vText = v.version ? "v" + v.version : "";
  const cText = v.commit ? (v.branch ? v.branch + "@" + v.commit : v.commit) : "";
  vChip.textContent = vText || cText || "version: -";
  vChip.title = vText || cText
    ? (vText ? "version " + vText + "\n" : "") +
      (cText ? "commit " + cText + "\n" : "") +
      "source: " + (v.source || "?")
    : "no build stamp available - run from a git clone or set MESHTECH_COMMIT";
  $("chip-nodes").textContent = "nodes: " + (st.db ? st.db.nodes : "-");
  $("chip-msgs").textContent = "msgs: " + (st.db && st.db.messages ? st.db.messages : "-");

  // channels card
  const wrap = $("channel-list");
  wrap.innerHTML = "";
  (st.channels || []).forEach((ch) => {
    const el = document.createElement("div");
    el.className = "channel";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = ch.name;
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = ch.reply ? "answering" : "listening only";
    if (ch.override === false) tag.classList.add("muted-note");
    const btn = document.createElement("button");
    btn.className = "btn small";
    btn.textContent = ch.reply ? "mute" : "unmute";
    btn.addEventListener("click", async () => {
      await api("/api/channels", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel: ch.name, reply: !ch.reply }),
      });
      refreshStatus();
    });
    el.append(name, tag, btn);
    wrap.appendChild(el);
  });

  const muteBtn = $("btn-mute");
  muteBtn.textContent = st.muted ? "Unmute bot (global)" : "Mute bot (global)";
  muteBtn.className = "btn " + (st.muted ? "danger" : "");
}

// ------------------------------------------------------------------ nodes

// Rendered rows keyed by prefix, so periodic refreshes can update cells
// IN PLACE instead of rebuilding the table (rebuilding resets scroll and
// would drop listeners). Set/order changes still trigger a full rebuild.
let nodeRows = new Map();
let nodeOrder = [];

const NODE_MAX_ROWS = 150;

async function refreshNodes(filterText) {
  const data = await api("/api/nodes?limit=300");
  const nodes = data.nodes || [];
  const filter = (filterText || "").toLowerCase();
  const filtered = filter
    ? nodes.filter((n) =>
        ((n.name || "") + " " + (n.prefix || "")).toLowerCase().includes(filter))
    : nodes;
  const wrap = $("node-list");

  if (!filtered.length) {
    wrap.innerHTML = "";
    nodeRows.clear();
    nodeOrder = [];
    const em = document.createElement("em");
    em.textContent = nodes.length ? "no matches" : "no nodes seen yet - they appear after advertising";
    wrap.appendChild(em);
    return;
  }

  const visible = filtered.slice(0, NODE_MAX_ROWS);
  const order = visible.map((n) => n.prefix);
  const sameSet = order.length === nodeOrder.length &&
                  order.every((p, i) => p === nodeOrder[i]);

  if (sameSet) {
    // Same rows in the same order: patch cells in place, keep scroll/focus.
    const byPrefix = new Map(visible.map((n) => [n.prefix, n]));
    for (const [prefix, row] of nodeRows) {
      const n = byPrefix.get(prefix);
      if (!n) continue;
      row.cb.checked = !!n.blocked;
      row.nameCell.textContent = n.name || "-";
      row.seenCell.textContent = ago(n.last_seen);
      row.snrCell.textContent =
        (n.last_snr !== null && n.last_snr !== undefined ? n.last_snr.toFixed(0) : "-");
      row.routeCell.textContent =
        (n.route_hops === null || n.route_hops === undefined ? "?" : n.route_hops);
    }
    return;
  }

  // Set or order changed: full rebuild (keeps listeners on the checkboxes).
  wrap.innerHTML = "";
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  thead.innerHTML = "<tr><th></th><th>Name</th><th>Prefix</th><th>Seen</th><th>SNR</th><th>Route</th></tr>";
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  const nextRows = new Map();
  visible.forEach((n) => {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    // Text cells first (setting innerHTML would drop the live checkbox node
    // and its listeners below), then insert the block checkbox at the front.
    tr.innerHTML =
      "<td>" + esc(n.name || "-") + "</td>" +
      "<td>" + esc(n.prefix || "") + "</td>" +
      "<td>" + ago(n.last_seen) + "</td>" +
      "<td>" + (n.last_snr !== null && n.last_snr !== undefined ? n.last_snr.toFixed(0) : "-") + "</td>" +
      "<td>" + (n.route_hops === null || n.route_hops === undefined ? "?" : n.route_hops) + "</td>";
    // Snapshot the cell nodes BEFORE inserting the checkbox cell - tr.children
    // is a LIVE collection, so later index reads would shift after insertBefore.
    const cells = Array.from(tr.children);
    const tdBlock = document.createElement("td");
    tdBlock.className = "node-block";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.title = "Ignore all messages from this node";
    cb.checked = !!n.blocked;
    cb.addEventListener("click", (ev) => ev.stopPropagation());
    cb.addEventListener("change", async () => {
      cb.disabled = true;
      const want = cb.checked;
      try {
        await api("/api/nodes/" + encodeURIComponent(n.prefix) + "/block",
                  { method: want ? "POST" : "DELETE" });
      } catch (e) {
        cb.checked = !want;  // revert on failure
      } finally {
        cb.disabled = false;
      }
    });
    tdBlock.appendChild(cb);
    tr.insertBefore(tdBlock, tr.firstChild);
    tr.addEventListener("click", () => showNodeDetail(n.prefix));
    tbody.appendChild(tr);
    // [0]=checkbox cell [1]=name [2]=prefix [3]=seen [4]=snr [5]=route
    nextRows.set(n.prefix, {
      cb, nameCell: cells[0], seenCell: cells[2], snrCell: cells[3],
      routeCell: cells[4],
    });
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
  nodeRows = nextRows;
  nodeOrder = order;
}

async function showNodeDetail(prefix) {
  const detail = $("node-detail");
  detail.classList.remove("hidden");
  detail.innerHTML = "<em>loading…</em>";
  try {
    const data = await api("/api/nodes/" + encodeURIComponent(prefix));
    const n = data.node;
    const s = data.stats || {};
    const link = data.link_history || [];
    let html = "<h3>" + esc(n.name || prefix) + " <span class='chip'>" + esc(n.prefix) + "</span></h3>";
    html += "<dl>" +
      "<dt>first seen</dt><dd>" + ago(n.first_seen) + "</dd>" +
      "<dt>last seen</dt><dd>" + ago(n.last_seen) + "</dd>" +
      "<dt>last SNR</dt><dd>" + (n.last_snr != null ? n.last_snr.toFixed(1) + " dB" : "?") + "</dd>" +
      "<dt>route hops</dt><dd>" + (n.route_hops == null ? "?" : n.route_hops) + "</dd>" +
      "<dt>messages</dt><dd>" + (s.count || 0) + "</dd>" +
      "<dt>delay avg</dt><dd>" + fmtDelay(s.delay_avg) + "</dd>" +
      "<dt>delay min/max</dt><dd>" + fmtDelay(s.delay_min) + " / " + fmtDelay(s.delay_max) + "</dd>" +
      "</dl>";
    if (link.length) {
      html += "<h3>Link quality history</h3>";
      html += "<table><tr><th>When</th><th>Hops</th><th>SNR</th><th>Source</th></tr>";
      link.forEach((r) => {
        const src = r.source === "advert" ? "advert" :
                    r.source === "dm" ? "DM" : "channel";
        html += "<tr><td>" + new Date(r.ts * 1000).toLocaleString() + "</td><td>" +
                (r.hops == null ? "-" : r.hops) + "</td><td>" +
                (r.snr == null ? "-" : r.snr.toFixed(1) + " dB") + "</td><td>" +
                src + "</td></tr>";
      });
      html += "</table>";
    } else {
      html += "<p><em>no link-quality observations yet - they build up as this node " +
              "talks (DM/channel) or advertises</em></p>";
    }
    detail.innerHTML = html;
  } catch (e) {
    detail.innerHTML = "<em>failed to load detail</em>";
  }
}

$("node-filter").addEventListener("input", (e) => refreshNodes(e.target.value));

// ------------------------------------------------------------------ messages

async function refreshMessages() {
  const channel = $("msg-channel").value;
  const kind = $("msg-kind").value;
  const qs = new URLSearchParams();
  if (channel && channel !== "") qs.set("channel", channel);
  if (kind) qs.set("kind", kind);
  qs.set("limit", "150");
  const data = await api("/api/messages?" + qs.toString());
  const rows = data.messages || [];
  const wrap = $("msg-list");
  wrap.innerHTML = "";
  if (!rows.length) {
    const em = document.createElement("em");
    em.textContent = "no messages yet";
    wrap.appendChild(em);
    return;
  }
  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>When</th><th>Dir</th><th>Target</th><th>Hops</th><th>Text</th></tr></thead>";
  const tbody = document.createElement("tbody");
  rows.forEach((r) => {
    const target = r.kind === "dm"
      ? (r.direction === "in" ? "from " + (r.sender_prefix || "?") : "to " + (r.sender_prefix || "?"))
      : (r.channel_name || "?");
    const tr = document.createElement("tr");
    tr.innerHTML =
      "<td>" + new Date(r.recv_ts * 1000).toLocaleString() + "</td>" +
      "<td>" + (r.direction === "in" ? "in" : "out") + "</td>" +
      "<td>" + esc(target) + "</td>" +
      "<td>" + (r.hops == null ? "-" : r.hops) + "</td>" +
      "<td>" + esc((r.text || "").slice(0, 120)) + "</td>";
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
}

async function loadMsgChannelOptions() {
  const data = await api("/api/status");
  const select = $("msg-channel");
  select.innerHTML = "<option value=''>all channels</option>";
  (data.channels || []).forEach((ch) => {
    const opt = document.createElement("option");
    opt.value = ch.name;
    opt.textContent = ch.name;
    select.appendChild(opt);
  });
}

$("btn-load-msgs").addEventListener("click", refreshMessages);
$("msg-kind").addEventListener("change", refreshMessages);
$("msg-channel").addEventListener("change", refreshMessages);

// ------------------------------------------------------------------ analysis

const AN_C = { decoded: "#4cc2ff", raw: "#ffb454", accent: "#3ecf8e" };
const AN_W = 560, AN_H = 170, AN_ML = 42, AN_MR = 6, AN_MT = 12, AN_MB = 20;

function _niceMax(v) {
  if (v <= 0) return 5;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const r = v / p;
  return (r <= 1 ? 1 : r <= 2 ? 2 : r <= 5 ? 5 : 10) * p;
}

function _bucketLabel(ts, spanSec) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  if (spanSec >= 86400) return (d.getMonth() + 1) + "/" + d.getDate();
  if (spanSec >= 3600) return d.getHours() + "h";
  return p(d.getHours()) + ":" + p(d.getMinutes());
}

// Grouped vertical bars; labels are pre-formatted strings.
function vbarSvg(labels, valuesList, colors, unit) {
  const n = Math.max(labels.length, 1);
  const plotW = AN_W - AN_ML - AN_MR;
  const plotH = AN_H - AN_MT - AN_MB;
  const maxV = _niceMax(Math.max(0, ...valuesList.flat()));
  const catW = plotW / n;
  const sets = valuesList.length;
  const barW = Math.max(2, Math.min(12, (catW * 0.6) / sets));
  const ticks = 3;
  let s = "";
  for (let i = 0; i <= ticks; i++) {
    const val = (maxV * i) / ticks;
    const y = AN_MT + plotH - (plotH * i) / ticks;
    s += `<line x1="${AN_ML}" y1="${y}" x2="${AN_W - AN_MR}" y2="${y}" stroke="#2c3a4d" stroke-width="0.5"/>`;
    s += `<text x="${AN_ML - 4}" y="${y + 3}" text-anchor="end" font-size="9">${Math.round(val)}${unit || ""}</text>`;
  }
  const labelStep = Math.max(1, Math.ceil(n / 10));
  valuesList.forEach((values, si) => {
    values.forEach((v, i) => {
      const x = AN_ML + catW * i + (catW - barW * sets) / 2 + barW * si;
      const h = maxV > 0 ? Math.max(1, (plotH * v) / maxV) : 0;
      s += `<rect x="${x.toFixed(1)}" y="${(AN_MT + plotH - h).toFixed(1)}" ` +
           `width="${barW.toFixed(1)}" height="${h.toFixed(1)}" rx="1" fill="${colors[si]}" opacity="0.92"/>`;
      if (i % labelStep === 0) {
        s += `<text x="${(AN_ML + catW * i + catW / 2).toFixed(1)}" y="${AN_H - 6}" ` +
             `text-anchor="middle" font-size="9">${labels[i]}</text>`;
      }
    });
  });
  if (!maxV) {
    s += `<text x="${AN_W / 2}" y="${AN_H / 2}" text-anchor="middle" font-size="10" opacity="0.5">no traffic in window</text>`;
  }
  return s;
}

// SNR line chart: avg line + min/max band. points: [{ts, avg, min, max}]
function snrSvg(points) {
  const n = Math.max(points.length, 1);
  const plotW = AN_W - AN_ML - AN_MR;
  const plotH = AN_H - AN_MT - AN_MB;
  const all = points.flatMap((p) => [p.min, p.max, p.avg]);
  let yMin = Math.floor(Math.min(-10, ...all));
  let yMax = Math.ceil(Math.max(15, ...all));
  if (yMax - yMin < 8) yMax = yMin + 8;
  const xAt = (i) => AN_ML + (n === 1 ? plotW / 2 : (plotW * i) / (n - 1));
  const yAt = (v) => AN_MT + plotH - ((v - yMin) / (yMax - yMin)) * plotH;
  let s = "";
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const val = yMin + ((yMax - yMin) * i) / ticks;
    const y = yAt(val);
    s += `<line x1="${AN_ML}" y1="${y}" x2="${AN_W - AN_MR}" y2="${y}" stroke="#2c3a4d" stroke-width="0.5"/>`;
    s += `<text x="${AN_ML - 4}" y="${y + 3}" text-anchor="end" font-size="9">${Math.round(val * 10) / 10}</text>`;
  }
  if (n === 1) {
    const p = points[0];
    s += `<circle cx="${xAt(0)}" cy="${yAt(p.avg)}" r="2.5" fill="${AN_C.accent}"/>`;
  } else {
    const avgPts = points.map((p, i) => xAt(i).toFixed(1) + "," + yAt(p.avg).toFixed(1)).join(" ");
    const bandPts = points.map((p, i) => xAt(i).toFixed(1) + "," + yAt(p.min).toFixed(1)).join(" ") +
      " " + points.map((p, i) => xAt(i).toFixed(1) + "," + yAt(p.max).toFixed(1)).reverse().join(" ");
    s += `<polygon points="${bandPts}" fill="${AN_C.accent}" opacity="0.12"/>`;
    s += `<polyline points="${avgPts}" fill="none" stroke="${AN_C.accent}" stroke-width="1.6"/>`;
  }
  const labelStep = Math.max(1, Math.ceil(n / 8));
  points.forEach((p, i) => {
    if (i % labelStep === 0) {
      s += `<text x="${xAt(i).toFixed(1)}" y="${AN_H - 6}" text-anchor="middle" font-size="9">${_bucketLabel(p.ts, p.spanSec || 3600)}</text>`;
    }
  });
  return s;
}

function mixHtml(mix, total, title) {
  if (!total) return "<div class='an-none'><em>none in window</em></div>";
  const rows = (mix || []).map((m) => {
    const pct = total ? (100 * m.n) / total : 0;
    return "<div class='mix-row'><span class='mix-name'>" + esc(m.frame_type) +
      "</span><span class='mix-bar'><i style='width:" + Math.max(1, pct).toFixed(1) +
      "%'></i></span><span class='mix-pct'>" + m.n + " (" + pct.toFixed(1) + "%)</span></div>";
  }).join("");
  return "<div class='mix-title'>" + title + "</div>" + rows;
}

function anPanel(title) {
  return "<div class='an-panel'><div class='an-title'>" + title + "</div>";
}

async function refreshAnalysis() {
  const hours = parseFloat($("an-window").value) || 24;
  const data = (await api("/api/packets/analysis?hours=" + hours)).analysis;
  const grid = $("an-grid");
  if (!data || !data.timeline || !data.timeline.length) {
    grid.innerHTML = "<em>no packets captured yet - analysis builds as traffic arrives</em>";
    return;
  }
  const spanSec = data.bucket_seconds || 3600;
  const t = data.timeline;
  const timeLabels = t.map((b) => _bucketLabel(b.bucket, spanSec));
  let html = "";

  // 1. traffic timeline (decoded vs raw per bucket)
  html += anPanel("Traffic per " + (spanSec >= 3600 ? "hour" : spanSec >= 1800 ? "30 min" : spanSec >= 300 ? "5 min" : "bucket"));
  html += "<svg viewBox='0 0 " + AN_W + " " + AN_H + "' preserveAspectRatio='xMidYMid meet'>" +
    vbarSvg(timeLabels, [t.map((b) => b.decoded), t.map((b) => b.raw)], [AN_C.decoded, AN_C.raw]) + "</svg>";
  html += "<div class='an-legend'><span><i style='background:" + AN_C.decoded + "'></i>decoded</span>" +
    "<span><i style='background:" + AN_C.raw + "'></i>raw</span></div></div>";

  // 2. frame-type mix (decoded + raw)
  html += anPanel("Frame-type mix");
  html += mixHtml(data.mix_decoded, data.decoded_total,
                  "decoded · " + data.decoded_total + " frames");
  html += mixHtml(data.mix_raw, data.raw_total,
                  "raw · " + data.raw_total + " frames");
  html += "</div>";

  // 3. hop distribution
  const hops = data.hops || [];
  html += anPanel("Hop distribution (decoded frames)");
  if (hops.length) {
    html += "<svg viewBox='0 0 " + AN_W + " " + AN_H + "' preserveAspectRatio='xMidYMid meet'>" +
      vbarSvg(hops.map((h) => h.hops), [hops.map((h) => h.count)], [AN_C.accent]) + "</svg></div>";
  } else {
    html += "<div class='an-none'><em>no hop counts in window</em></div></div>";
  }

  // 4. SNR trend
  const snr = (data.snr || []).map((p) => ({ ...p, ts: p.bucket, spanSec }));
  html += anPanel("SNR trend (dB per bucket)");
  if (snr.length) {
    html += "<svg viewBox='0 0 " + AN_W + " " + AN_H + "' preserveAspectRatio='xMidYMid meet'>" +
      snrSvg(snr) + "</svg><div class='an-legend'><span><i style='background:" + AN_C.accent +
      "'></i>avg (band = min/max)</span></div></div>";
  } else {
    html += "<div class='an-none'><em>no SNR readings in window</em></div></div>";
  }

  grid.innerHTML = html;
}

$("an-window").addEventListener("change", refreshAnalysis);

// ------------------------------------------------------------------ packets

async function refreshPackets() {
  const layer = $("pkt-layer").value;
  const qs = new URLSearchParams();
  if (layer) qs.set("layer", layer);
  qs.set("limit", "40");
  const data = await api("/api/packets?" + qs.toString());
  const rows = data.packets || [];
  const stats = data.stats || {};
  const wrap = $("pkt-list");
  wrap.innerHTML = "";

  const caption = document.createElement("div");
  const byLayer = stats.by_layer || {};
  caption.className = "muted-note";
  caption.style.fontSize = "11px";
  caption.style.fontWeight = "400";
  caption.textContent = "total " + (data.total || 0) +
    " · decoded " + (byLayer.decoded || 0) +
    " · raw " + (byLayer.raw || 0);
  wrap.appendChild(caption);

  // Raw link profile: packet size + inter-frame timing of the companion link
  try {
    const prof = (await api("/api/packets/profile")).profile;
    if (prof && prof.frames >= 2 && prof.size && prof.gaps) {
      const b = prof.size.buckets || {};
      const sizeLine = "size: min " + prof.size.min + " B · avg " + prof.size.avg +
        " B · max " + prof.size.max + " B   [" +
        ["<32", "32-63", "64-127", "128-255", ">=256"].map((k) =>
          k + ": " + (b[k] ? b[k].pct : 0) + "%").join("  ") + "]";
      const gapLine = "gaps: min " + fmtDur(prof.gaps.min) + " · avg " + fmtDur(prof.gaps.avg) +
        " · p50 " + fmtDur(prof.gaps.p50) + " · p95 " + fmtDur(prof.gaps.p95) +
        " · max " + fmtDur(prof.gaps.max);
      const block = document.createElement("div");
      block.className = "pkt-profile";
      block.innerHTML = "<div><b>raw link profile</b> — " + prof.frames +
        " frames over " + fmtDur(prof.span_seconds) +
        " (" + prof.rate_fps + "/s)</div>" +
        "<div>" + sizeLine + "</div>" +
        "<div>" + gapLine + "</div>";
      wrap.insertBefore(block, caption.nextSibling);
    }
  } catch (e) { /* profile is best-effort */ }

  if (!rows.length) {
    const em = document.createElement("em");
    em.textContent = "no packets captured yet";
    wrap.appendChild(em);
    return;
  }
  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>When</th><th>Dir</th><th>Type</th><th>Hops</th><th>Hash</th><th>SNR</th><th>Channel / sender</th><th>Text</th></tr></thead>";
  const tbody = document.createElement("tbody");
  rows.forEach((r) => {
    const target = r.channel_name || (r.sender ? (r.direction === "out" ? "to " : "from ") + r.sender : "-");
    const hash = r.path_hash_size == null ? "-" :
      (r.path_hash_size === 1 ? "1B" : r.path_hash_size === 2 ? "2B" : r.path_hash_size + "B");
    const tr = document.createElement("tr");
    tr.innerHTML =
      "<td>" + new Date(r.ts * 1000).toLocaleTimeString() + "</td>" +
      "<td>" + (r.direction === "out" ? "out" : "in") + "</td>" +
      "<td>" + esc(r.frame_type || "-") + "</td>" +
      "<td>" + (r.hops == null ? "-" : r.hops) + "</td>" +
      "<td title='bytes per path hash - 1B = 1-byte, 2B = 2-byte, 3B+ = longer addresses'>" + hash + "</td>" +
      "<td>" + (r.snr == null ? "-" : r.snr.toFixed(1)) + "</td>" +
      "<td>" + esc(target) + "</td>" +
      "<td>" + esc((r.text || (r.size != null ? r.size + " bytes" : "")).slice(0, 100)) + "</td>";
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
}

$("btn-load-packets").addEventListener("click", refreshPackets);
$("pkt-layer").addEventListener("change", refreshPackets);

// ------------------------------------------------------------------ actions + config

$("btn-mute").addEventListener("click", async () => {
  const muted = !(lastStatus && lastStatus.muted);
  await api("/api/mute", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ muted }),
  });
  refreshStatus();
});

$("btn-reload").addEventListener("click", async () => {
  const r = await api("/api/actions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "reload" }),
  });
  $("action-result").textContent = (r && r.message) || "reloaded";
  refreshStatus();
});

$("btn-shutdown").addEventListener("click", async () => {
  if (!confirm("Really shut down the bot?")) return;
  await api("/api/actions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "shutdown" }),
  });
  $("action-result").textContent = "shutting down…";
});

$("btn-config").addEventListener("click", async () => {
  const view = $("config-view");
  if (!view.classList.contains("hidden")) { view.classList.add("hidden"); return; }
  const data = await api("/api/config");
  view.textContent = JSON.stringify(data.config, null, 2);
  if (data.warnings && data.warnings.length) {
    view.textContent += "\n\nWarnings:\n" + data.warnings.join("\n");
  }
  view.classList.remove("hidden");
});

// ------------------------------------------------------------------ helpers

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function ago(ts) {
  if (!ts) return "?";
  const s = Math.max(0, (Date.now() / 1000) - ts);
  if (s < 60) return Math.floor(s) + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
}

function fmtDur(sec) {
  if (sec === null || sec === undefined) return "-";
  if (sec < 0.001) return "<1ms";
  if (sec < 1) return Math.round(sec * 1000) + "ms";
  if (sec < 60) return sec.toFixed(1) + "s";
  if (sec < 3600) return Math.floor(sec / 60) + "m " + Math.round(sec % 60) + "s";
  return Math.floor(sec / 3600) + "h";
}

function fmtDelay(v) {
  if (v === null || v === undefined) return "?";
  if (v < 1) return Math.round(v * 1000) + "ms";
  if (v < 90) return v.toFixed(1) + "s";
  if (v < 3600) return Math.floor(v / 60) + "m";
  return Math.floor(v / 3600) + "h";
}

// ------------------------------------------------------------------ collapsible sections

const COLLAPSE_KEY = "mcb_col_";

// Sections that DISPLAY DATA get a caret button in their h2 (clicking the
// caret or the section title collapses/expands the body; clicks on the
// header's own controls - refresh, pause, selects... - never toggle).
// Sections that only hold options/buttons/static info are NOT marked
// `collapsible` in index.html, so they keep a plain header - anything added
// to the UI in future should follow that rule: collapse only what contains
// data, never options.
// State is remembered per section in localStorage, so the big lists (nodes,
// packets) can stay closed between visits. Until the visitor has expressed a
// choice, the tallest sections start collapsed for a cleaner landing view.
const DEFAULT_COLLAPSED = new Set(["card-nodes", "card-packets"]);

function setupCollapsibleSections() {
  document.querySelectorAll("main section.card.collapsible").forEach((card) => {
    const h2 = card.querySelector("h2");
    if (!h2) return;
    const caret = document.createElement("button");
    caret.type = "button";
    caret.className = "caret";
    caret.title = "Collapse / expand this section";
    caret.setAttribute("aria-label", "Collapse or expand this section");
    caret.textContent = "\u25be";  // ▾ points down when open, rotates right when closed
    h2.insertBefore(caret, h2.firstChild);
    const apply = () =>
      caret.setAttribute("aria-expanded", card.classList.contains("collapsed") ? "false" : "true");
    const toggle = () => {
      const collapsed = card.classList.toggle("collapsed");
      localStorage.setItem(COLLAPSE_KEY + card.id, collapsed ? "1" : "0");
      apply();
    };
    h2.addEventListener("click", (ev) => {
      if (ev.target.closest("button, select, input, a, label")) return;
      toggle();
    });
    caret.addEventListener("click", (ev) => {
      ev.stopPropagation();
      toggle();
    });
    const saved = localStorage.getItem(COLLAPSE_KEY + card.id);
    if (saved === "1" ||
        (saved === null && DEFAULT_COLLAPSED.has(card.id))) {
      card.classList.add("collapsed");
    }
    apply();
  });
  // Any preference for a section that is no longer collapsible is stale.
  document.querySelectorAll("main section.card:not(.collapsible)").forEach((card) => {
    localStorage.removeItem(COLLAPSE_KEY + card.id);
  });
}

// ------------------------------------------------------------------ boot

async function refreshAll() {
  try {
    await refreshStatus();
    await loadMsgChannelOptions();
    await refreshNodes("");
    await refreshMessages();
    await refreshPackets();
    await refreshAnalysis();
  } catch (e) { /* auth or network handled elsewhere */ }
}

// Periodic refreshes keep the dashboard live (uptime chip, messages, node
// cells, packets, analysis). Registered exactly once - from boot() when the
// page loads with a valid token, AND from the login handler, because boot()
// returns early when the password screen is shown first. Without the login
// call the page loaded once and then froze (uptime never advanced).
let pollingStarted = false;
function startPolling() {
  if (pollingStarted) return;
  pollingStarted = true;
  setInterval(() => { refreshStatus().catch(() => {}); }, 3000);
  setInterval(() => { refreshMessages().catch(() => {}); }, 15000);
  // Keep block checkboxes / node cells current (e.g. blocks from another
  // tab) without rebuilding the table, so scroll position is preserved.
  setInterval(() => { refreshNodes($("node-filter").value).catch(() => {}); }, 30000);
  setInterval(() => { refreshPackets().catch(() => {}); }, 30000);
  setInterval(() => { refreshAnalysis().catch(() => {}); }, 30000);
}

async function boot() {
  const ok = await ensureAuth();
  if (!ok) return;
  connectWs();
  refreshAll();
  startPolling();
}

setupCollapsibleSections();
boot();
