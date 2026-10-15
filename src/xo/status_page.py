"""The status page `xo serve` answers at `/`: health, counts and recent requests, from `/status`.

One self-contained HTML page with its own style and script, both pinned by hash in the page's
Content-Security-Policy, so the page can load nothing else and talk only to its own server. It
renders every value with textContent, never as HTML. The look follows shadcn/ui's default theme
(cards, badges, a table) without React, since the server has no build step.
"""

from __future__ import annotations

import base64
import hashlib

STYLE = """
:root{--bg:#fff;--fg:#0a0a0a;--muted:#f5f5f5;--muted-fg:#666666;--border:#e5e5e5;--card:#fff;
--ok:#15803d;--ok-bg:#dcfce7;--warn:#b45309;--warn-bg:#fef3c7;--bad:#b91c1c;--bad-bg:#fee2e2;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#0a0a0a;--fg:#fafafa;--muted:#171717;--muted-fg:#a3a3a3;
--border:#262626;--card:#0a0a0a;--ok:#4ade80;--ok-bg:#052e16;--warn:#fbbf24;--warn-bg:#451a03;--bad:#f87171;--bad-bg:#450a0a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px}
h1{font-size:20px;font-weight:600;margin:0}header{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.sub{color:var(--muted-fg);margin:4px 0 20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px}
.label{color:var(--muted-fg);font-size:13px}.value{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
.badge{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:500;
border:1px solid var(--border)}.dot{width:8px;height:8px;border-radius:50%;background:currentColor}
.ok{color:var(--ok);background:var(--ok-bg)}.warn{color:var(--warn);background:var(--warn-bg)}.bad{color:var(--bad);background:var(--bad-bg)}
.table{border:1px solid var(--border);border-radius:12px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--border);white-space:nowrap}
th{color:var(--muted-fg);font-weight:500;font-size:13px;background:var(--muted)}tr:last-child td{border-bottom:0}
td.route{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;white-space:normal}
.stale .value,.stale td{color:var(--muted-fg)}
.empty{color:var(--muted-fg);padding:16px;text-align:center}.note{color:var(--muted-fg);font-size:13px;margin-top:12px}
@media (max-width:520px){.hide-sm{display:none}}
"""

SCRIPT = """
"use strict";
const $ = (id) => document.getElementById(id);
const EVERY = 5000, SLOW = 30000, TIMEOUT = 8000;
let failures = 0, timer = null, seenAt = null, busy = false;
function setBadge(cls, text) {
  const b = $("state"); b.className = "badge " + cls; $("state-text").textContent = text;
}
function ago(iso) {
  const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  return Math.floor(s / 3600) + "h ago";
}
function cell(text, cls) {
  const td = document.createElement("td"); td.textContent = text; if (cls) td.className = cls; return td;
}
function routeCell(route) {  // breaks only after a "/", never inside a word
  const td = cell("", "route"); const parts = route.split("/");
  parts.forEach((part, i) => {
    td.append(i < parts.length - 1 ? part + "/" : part);
    if (i < parts.length - 1) td.append(document.createElement("wbr"));
  });
  return td;
}
function deadline() {  // AbortSignal.timeout is missing before Safari 16, Chrome 103 and Firefox 100
  if (AbortSignal.timeout) return AbortSignal.timeout(TIMEOUT);
  const c = new AbortController(); setTimeout(() => c.abort(), TIMEOUT); return c.signal;
}
function message(text) {
  const tr = document.createElement("tr"); const td = cell(text, "empty");
  td.colSpan = 5; tr.append(td); $("rows").replaceChildren(tr);
}
function render(d) {
  if (d.status === "ok") setBadge("ok", "Operational");
  else setBadge("warn", "Degraded: " + d.recent.failed + " of " + d.recent.entries + " failed in 5 min");
  $("since").textContent = "This instance started " + ago(d.instance.startedAt) + " (" + d.instance.startedAt + ")";
  $("requests").textContent = d.instance.requests;
  $("pings").textContent = d.instance.pings;
  const by = d.instance.byStatus;
  $("errors").textContent = (by["4xx"] || 0) + " / " + (by["5xx"] || 0);
  $("data").className = "";
  if (!d.activity.length) { message("No requests yet on this instance."); return; }
  const rows = [];
  for (const a of d.activity) {
    const tr = document.createElement("tr");
    const status = cell("");
    const span = document.createElement("span");
    span.className = "badge " + (a.status < 400 ? "ok" : a.status < 500 ? "warn" : "bad");
    span.textContent = String(a.status); status.replaceChildren(span);
    const when = cell(ago(a.time)); when.title = a.time;
    tr.append(when, cell(a.method), routeCell(a.route), status, cell(a.ms + " ms", "hide-sm"));
    rows.push(tr);
  }
  $("rows").replaceChildren(...rows);
}
async function refresh() {
  timer = null;
  if (document.hidden || busy) return;  // polling stops while the tab is hidden and resumes when it shows
  busy = true;
  try {
    const r = await fetch("status", {cache: "no-store", signal: deadline()});
    if (!r.ok) throw new Error("HTTP " + r.status);
    const body = await r.json();
    render(body.data);
    failures = 0; seenAt = new Date();
    $("updated").textContent = "Updated " + seenAt.toLocaleTimeString() + ".";
  } catch (e) {
    failures += 1;
    setBadge("bad", "Unreachable");
    if (seenAt) {
      $("data").className = "stale";
      $("updated").textContent = "Last seen " + seenAt.toLocaleTimeString() + "; the figures below are from then.";
    } else {
      $("since").textContent = "Could not reach the server.";
      message("No data yet.");
      $("updated").textContent = "";
    }
  } finally {
    busy = false;
  }
  schedule();
}
function schedule() {
  if (timer === null && !document.hidden) timer = setTimeout(refresh, failures >= 3 ? SLOW : EVERY);
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && timer === null) refresh();
});
refresh();
"""

BODY = """<main>
<header><h1>xo server</h1><span id="state" class="badge warn"><span class="dot"></span><span id="state-text">Checking</span></span></header>
<p class="sub" id="since">Loading...</p>
<div id="data"><div class="grid">
<div class="card"><div class="label">Requests</div><div class="value" id="requests">-</div></div>
<div class="card"><div class="label">Pings (/healthz)</div><div class="value" id="pings">-</div></div>
<div class="card"><div class="label">Refused / failed (4xx / 5xx)</div><div class="value" id="errors">-</div></div>
</div>
<div class="table"><table>
<thead><tr><th>When</th><th>Method</th><th>Route</th><th>Status</th><th class="hide-sm">Time</th></tr></thead>
<tbody id="rows"></tbody></table></div></div>
<p class="note"><span id="updated"></span> The last 100 requests this server instance answered: time, method,
route, status and duration only, never keys, user ids or addresses. On Vercel each instance keeps its own list,
which starts empty when the instance starts, so the figures can change between refreshes. Refreshes every 5
seconds while this tab is visible.</p>
</main>"""


def _hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


PAGE = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex\"><title>xo server status</title>"
        f"<style>{STYLE}</style></head><body>{BODY}<script>{SCRIPT}</script></body></html>").encode()

CSP = (f"default-src 'none'; script-src {_hash(SCRIPT)}; style-src {_hash(STYLE)}; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
