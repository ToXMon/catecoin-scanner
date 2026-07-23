"""Health and journal HTTP server for Akash deployment."""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict

from alert_journal import AlertJournal

logger = logging.getLogger("catecoin-scanner")


def _json_default(obj: Any) -> str:
    """Fallback JSON serializer for API responses."""
    return str(obj)


class HealthHandler(BaseHTTPRequestHandler):
    """Small read-only HTTP API for health, journal exports, and trade queues."""

    def _send_json(self, payload: Dict[str, Any] | list, status: int = 200) -> None:
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, body: str, content_type: str = "text/plain", status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _query(self) -> Dict[str, str]:
        parsed = urllib.parse.urlparse(self.path)
        pairs = urllib.parse.parse_qs(parsed.query)
        return {k: v[-1] for k, v in pairs.items() if v}

    @staticmethod
    def _limit(query: Dict[str, str], default: int = 50, max_value: int = 1000) -> int:
        try:
            value = int(query.get("limit", default))
        except (TypeError, ValueError):
            value = default
        return max(1, min(value, max_value))

    def _journal(self) -> AlertJournal:
        return AlertJournal()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = self._query()

        try:
            if path in ("/", "/health"):
                self._send_json({"status": "ok", "service": "catecoin-scanner"})
                return

            journal = self._journal()

            if path == "/journal/stats":
                chain = query.get("chain") or None
                self._send_json(journal.stats(chain=chain))
                return

            if path == "/journal/export.jsonl":
                limit = self._limit(query, default=1000, max_value=10000)
                self._send_text(
                    journal.export_jsonl_text(limit=limit),
                    content_type="application/x-ndjson; charset=utf-8",
                )
                return

            if path == "/journal/recent":
                limit = self._limit(query, default=50, max_value=500)
                chain = query.get("chain") or None
                self._send_json({"alerts": journal.recent_alerts(limit=limit, chain=chain)})
                return

            if path == "/wallet/stats":
                limit = self._limit(query, default=100, max_value=1000)
                try:
                    min_alerts = int(query.get("min_alerts", 1))
                except (TypeError, ValueError):
                    min_alerts = 1
                self._send_json({"wallets": journal.wallet_stats(min_alerts=min_alerts)[:limit]})
                return

            if path == "/queue":
                limit = self._limit(query, default=100, max_value=500)
                chain = query.get("chain") or None
                items = journal.queue_items(limit=limit, chain=chain)
                grouped: Dict[str, list] = {}
                for item in items:
                    grouped.setdefault(item.get("queue_state", "observe"), []).append(item)
                self._send_json({"items": items, "grouped": grouped})
                return

            if path == "/analyzer/report":
                report_path = os.environ.get("ANALYZER_REPORT_PATH", "state/analyzer_report.json")
                if os.path.exists(report_path):
                    with open(report_path, "r", encoding="utf-8") as f:
                        self._send_json(json.load(f))
                else:
                    self._send_json({"status": "no_report_yet", "message": "Analyzer has not run yet"})
                return

            if path == "/analyzer/stats":
                adjustments_path = os.environ.get("ANALYZER_ADJUSTMENTS_PATH", "state/scorer_adjustments.json")
                if os.path.exists(adjustments_path):
                    with open(adjustments_path, "r", encoding="utf-8") as f:
                        self._send_json(json.load(f))
                else:
                    self._send_json({"status": "no_adjustments_yet", "message": "No data-driven adjustments generated yet"})
                return

            if path == "/dashboard":
                self._send_html(DASHBOARD_HTML)
                return

            self._send_json({"error": "not_found", "path": path}, status=404)
        except Exception as e:
            logger.exception("HTTP endpoint failed: %s", path)
            self._send_json({"error": str(e), "path": path}, status=500)

    def log_message(self, *args):
        pass

    def _send_html(self, html: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))


# --- Dashboard HTML (self-contained, auto-refresh) ---
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Catecoin Scanner — Trader Command Center</title>
<style>
:root { --bg:#07090d; --panel:#10151f; --panel2:#151b27; --line:#253044; --text:#d7deea; --muted:#7d8ba3; --green:#20d17d; --yellow:#f4c542; --red:#ff5d6c; --blue:#35b7ff; --cyan:#31e0d8; --violet:#b58cff; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size:13px; }
a { color:var(--cyan); text-decoration:none; } a:hover { text-decoration:underline; }
.shell { max-width: 1680px; margin:0 auto; padding:18px; }
.topbar { position:sticky; top:0; z-index:5; background:linear-gradient(180deg,#07090d 80%,rgba(7,9,13,.72)); border-bottom:1px solid var(--line); padding:14px 18px; margin:0 -18px 16px; display:flex; justify-content:space-between; gap:16px; align-items:center; }
h1 { margin:0; font-size:20px; letter-spacing:.03em; color:#f7fbff; }
.subtitle { color:var(--muted); margin-top:4px; font-size:12px; }
.status-dot { display:inline-block; width:8px; height:8px; border-radius:99px; background:var(--yellow); margin-right:6px; }
.status-dot.live { background:var(--green); box-shadow:0 0 10px var(--green); }
.controls { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
button, select, input { background:#0c111a; color:var(--text); border:1px solid var(--line); border-radius:6px; padding:7px 9px; font-size:12px; }
button { cursor:pointer; } button:hover { border-color:var(--cyan); }
.grid { display:grid; gap:12px; }
.kpis { grid-template-columns:repeat(6,minmax(130px,1fr)); }
.two { grid-template-columns:1.35fr .85fr; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
.card h2 { margin:0; padding:11px 13px; font-size:14px; color:#f7fbff; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:center; }
.card-body { padding:12px; }
.kpi { background:var(--panel2); border:1px solid var(--line); border-radius:10px; padding:12px; min-height:78px; }
.kpi .label { color:var(--muted); text-transform:uppercase; font-size:10px; letter-spacing:.09em; }
.kpi .value { font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:24px; margin-top:8px; font-weight:800; }
.kpi .note { color:var(--muted); font-size:11px; margin-top:4px; }
.green { color:var(--green); } .yellow { color:var(--yellow); } .red { color:var(--red); } .blue { color:var(--blue); } .muted { color:var(--muted); }
table { width:100%; border-collapse:collapse; font-size:12px; }
th { color:var(--muted); text-align:left; font-weight:700; border-bottom:1px solid var(--line); padding:8px 7px; position:sticky; top:65px; background:var(--panel); z-index:2; }
td { border-bottom:1px solid #1b2433; padding:8px 7px; vertical-align:top; }
tr:hover td { background:#111b28; }
.token { font-weight:800; color:#fff; }
.name { display:block; color:var(--muted); font-size:11px; max-width:260px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.badge { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:2px 7px; font-size:10px; font-weight:800; text-transform:uppercase; letter-spacing:.04em; }
.entry_ready { color:#03120b; background:var(--green); border-color:var(--green); }
.candidate { color:#1b1400; background:var(--yellow); border-color:var(--yellow); }
.take_profit_watch { color:#070013; background:var(--violet); border-color:var(--violet); }
.observe { color:var(--muted); background:#182130; }
.avoid_or_cut { color:#210005; background:var(--red); border-color:var(--red); }
.linkbtn { display:inline-block; border:1px solid var(--cyan); border-radius:6px; padding:4px 7px; color:var(--cyan); font-size:11px; margin-top:3px; }
code { color:#b7c8e6; background:#090d14; border:1px solid #202a3b; border-radius:4px; padding:2px 4px; cursor:pointer; }
.reasons { color:#c6d1e3; max-width:330px; }
.plan { color:#d5e1f5; font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:11px; line-height:1.45; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:10px; }
.winner { border-left:3px solid var(--green); background:var(--panel2); border-radius:8px; padding:10px; }
.error { color:var(--red); }
.empty { color:var(--muted); padding:10px; }
.small { font-size:11px; color:var(--muted); }
@media (max-width: 1000px) { .kpis,.two { grid-template-columns:1fr; } .topbar { position:static; flex-direction:column; align-items:flex-start; } th { position:static; } }
</style>
</head>
<body>
<div class="shell">
  <div class="topbar">
    <div><h1>Signal Command Center</h1><div class="subtitle"><span id="live-dot" class="status-dot"></span><span id="status">Loading scanner state…</span> · <span id="timestamp"></span></div></div>
    <div class="controls"><input id="search" placeholder="filter token / contract"><select id="state-filter"><option value="">all states</option><option value="entry_ready">entry_ready</option><option value="candidate">candidate</option><option value="take_profit_watch">take_profit_watch</option><option value="observe">observe</option><option value="avoid_or_cut">avoid_or_cut</option></select><button id="refresh">Refresh</button></div>
  </div>

  <section class="grid kpis" id="kpis"></section>

  <section class="card" style="margin-top:12px;">
    <h2><span>Action Queue</span><span class="small">entry_ready → candidate → take_profit_watch → observe</span></h2>
    <div class="card-body" id="queue-panel"></div>
  </section>

  <section class="grid two" style="margin-top:12px;">
    <div class="card"><h2><span>Winners / Outcome Panel</span><span class="small">positive max/latest returns</span></h2><div class="card-body" id="winners-panel"></div></div>
    <div class="card"><h2><span>Smart Money Leaderboard</span><span class="small">wallet edge ranking</span></h2><div class="card-body" id="wallet-panel"></div></div>
  </section>

  <section class="card" style="margin-top:12px;">
    <h2><span>Recent Alerts</span><span class="small">compact feed with Telegram + Dex links</span></h2>
    <div class="card-body" id="recent-panel"></div>
  </section>
</div>
<script>
const stateWeight = {entry_ready: 40, candidate: 30, take_profit_watch: 20, observe: 10, avoid_or_cut: 0};
let dashboardData = {queue: [], recent: [], wallets: [], stats: {}};
function esc(v) { return String(v ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
function n(v) { const x = Number(v); return Number.isFinite(x) ? x : 0; }
function fmtUSD(v) { v = n(v); if (!v) return '—'; if (v >= 1e9) return '$' + (v/1e9).toFixed(2) + 'B'; if (v >= 1e6) return '$' + (v/1e6).toFixed(2) + 'M'; if (v >= 1e3) return '$' + (v/1e3).toFixed(1) + 'K'; return '$' + v.toFixed(0); }
function fmtPct(v) { if (v === null || v === undefined || v === '') return '—'; v = Number(v); if (!Number.isFinite(v)) return '—'; return (Math.abs(v) <= 3 ? v * 100 : v).toFixed(1) + '%'; }
function fmtPrice(v) { v = n(v); if (!v) return '—'; return v < 0.0001 ? '$' + v.toPrecision(4) : '$' + v.toFixed(v < 1 ? 6 : 4); }
function shortAddr(a) { a = String(a || ''); return a.length > 12 ? a.slice(0,6) + '…' + a.slice(-4) : a; }
function ts(v) { if (!v) return 0; if (typeof v === 'number') return v > 1e12 ? v : v * 1000; const parsed = Date.parse(v); return Number.isFinite(parsed) ? parsed : 0; }
function age(v) { const t = ts(v); if (!t) return '—'; const mins = Math.max(0, Math.floor((Date.now() - t) / 60000)); if (mins < 60) return mins + 'm'; if (mins < 1440) return Math.floor(mins/60) + 'h'; return Math.floor(mins/1440) + 'd'; }
function arr(payload, key) { return Array.isArray(payload) ? payload : Array.isArray(payload?.[key]) ? payload[key] : []; }
async function fetchJSON(url) { const ctl = new AbortController(); const id = setTimeout(() => ctl.abort(), 8000); try { const r = await fetch(url, {signal: ctl.signal}); if (!r.ok) throw new Error(r.status + ' ' + r.statusText); return await r.json(); } finally { clearTimeout(id); } }
function dexUrl(item) { const stored = item.dex_url || item.url; if (stored && (stored.startsWith('http://') || stored.startsWith('https://'))) return stored; const chain = item.chain || 'robinhood'; const addr = item.pair_address || item.pairAddress || item.token_address || item.contract || ''; return addr ? `https://dexscreener.com/${encodeURIComponent(chain)}/${encodeURIComponent(addr)}` : ''; }
function tokenLine(item) { const symbol = item.token_symbol || item.symbol || shortAddr(item.token_address || item.contract) || '?'; const name = item.token_name || item.name || ''; const addr = item.token_address || item.contract || ''; return `<span class="token">${esc(symbol)}</span><span class="name">${esc(name)}</span>${addr ? `<code title="click to copy" data-copy="${esc(addr)}">${esc(shortAddr(addr))}</code>` : ''}`; }
function stateBadge(s) { s = s || 'observe'; return `<span class="badge ${esc(s)}">${esc(s)}</span>`; }
function score(item) { return n(item.alpha_score ?? item.runner_score ?? item.score); }
function reasons(item) { const r = item.queue_reasons || item.reasons || item.reason || item.thesis || ''; return Array.isArray(r) ? r.join('; ') : String(r || ''); }
function plan(item) { const p = item.trade_plan || {}; const entry = p.entry_zone || {}; return [`Entry ${fmtPrice(entry.low)}–${fmtPrice(entry.high)}`, `Stop ${fmtPrice(p.stop)}`, `TP ${fmtPrice(p.tp1)} / ${fmtPrice(p.tp2)} / ${fmtPrice(p.tp3)}`, p.max_position_usd ? `Max $${esc(p.max_position_usd)}` : 'Log-only'].join('<br>'); }
function sortSignals(items) { return [...items].sort((a,b) => (stateWeight[b.queue_state || 'observe'] - stateWeight[a.queue_state || 'observe']) || (score(b)-score(a)) || (ts(b.timestamp)-ts(a.timestamp))); }
function filtered(items) { const q = document.getElementById('search').value.toLowerCase(); const st = document.getElementById('state-filter').value; return items.filter(i => (!st || i.queue_state === st) && (!q || JSON.stringify([i.token_symbol,i.token_name,i.token_address,i.contract]).toLowerCase().includes(q))); }
function renderKpis() { const q = dashboardData.queue, r = dashboardData.recent, s = dashboardData.stats || {}; const all = [...q, ...r]; const count = st => all.filter(x => x.queue_state === st).length; const profitable = s.profitable_48h ?? all.filter(x => n(x.max_return_pct) > 0 || n(x.latest_return_pct) > 0 || n(x.latest_return) > 0).length; const kpis = [ ['Entry Ready', count('entry_ready'), 'green', 'act now / verify chart'], ['Candidates', count('candidate'), 'yellow', 'watch confirmation'], ['Observe', count('observe'), 'muted', 'logged not blasted'], ['Avoid / Cut', count('avoid_or_cut'), 'red', 'risk controls'], ['Total Alerts', s.total_alerts ?? r.length, 'blue', 'journal scope'], ['Profitable 48h', profitable, 'green', 'if tracking available'] ]; document.getElementById('kpis').innerHTML = kpis.map(k => `<div class="kpi"><div class="label">${esc(k[0])}</div><div class="value ${k[2]}">${esc(k[1])}</div><div class="note">${esc(k[3])}</div></div>`).join(''); }
function renderQueue() { const items = filtered(sortSignals(dashboardData.queue)); if (!items.length) { document.getElementById('queue-panel').innerHTML = '<div class="empty">No actionable queue items after filters.</div>'; return; } document.getElementById('queue-panel').innerHTML = `<table><thead><tr><th>Token</th><th>Chain / State</th><th>Score</th><th>Liq / MCap / Vol</th><th>Age</th><th>Reasons</th><th>Trade Plan</th><th>Link</th></tr></thead><tbody>${items.map(item => { const url = dexUrl(item); return `<tr><td>${tokenLine(item)}</td><td>${esc(item.chain || '—')}<br>${stateBadge(item.queue_state)}</td><td class="${score(item)>=75?'green':score(item)>=60?'yellow':'muted'}">${score(item)||'—'}</td><td>${fmtUSD(item.liquidity_usd)} / ${fmtUSD(item.market_cap || item.fdv)} / ${fmtUSD(item.volume_24h)}</td><td>${age(item.timestamp)}</td><td class="reasons">${esc(reasons(item))}</td><td class="plan">${plan(item)}</td><td>${url ? `<a class="linkbtn" target="_blank" rel="noopener" href="${esc(url)}">DexScreener</a>` : '—'}</td></tr>`; }).join('')}</tbody></table>`; }
function renderWinners() { const seen = new Map(); [...dashboardData.recent, ...dashboardData.queue].forEach(a => { const key = a.token_address || a.contract || a.token_symbol; const ret = Math.max(n(a.max_return_pct), n(a.latest_return_pct), n(a.latest_return)); if (key && ret > 0 && (!seen.has(key) || ret > seen.get(key).ret)) seen.set(key, {item:a, ret}); }); const winners = [...seen.values()].sort((a,b)=>b.ret-a.ret).slice(0,8); if (!winners.length) { document.getElementById('winners-panel').innerHTML = '<div class="empty">No positive outcome fields found yet. Outcome tracker will populate winners when max/latest returns are journaled.</div>'; return; } document.getElementById('winners-panel').innerHTML = `<div class="cards">${winners.map(w => { const url = dexUrl(w.item); return `<div class="winner">${tokenLine(w.item)}<div class="value green" style="font-size:20px;margin:8px 0;">${fmtPct(w.ret)}</div><div class="small">State: ${esc(w.item.queue_state || '—')} · Age: ${age(w.item.timestamp)}</div>${url ? `<a class="linkbtn" target="_blank" rel="noopener" href="${esc(url)}">DexScreener</a>` : ''}</div>`; }).join('')}</div>`; }
function renderWallets() { const wallets = [...dashboardData.wallets].sort((a,b) => n(b.edge_score ?? b.avg_max_return_pct) - n(a.edge_score ?? a.avg_max_return_pct)).slice(0,15); if (!wallets.length) { document.getElementById('wallet-panel').innerHTML = '<div class="empty">No wallet stats yet.</div>'; return; } document.getElementById('wallet-panel').innerHTML = `<table><thead><tr><th>Wallet</th><th>Tier</th><th>Alerts</th><th>Avg Max</th><th>Win 15m/1h/4h</th><th>Edge</th></tr></thead><tbody>${wallets.map(w => { const addr = w.wallet || w.address || w.wallet_address || ''; return `<tr><td><code title="click to copy" data-copy="${esc(addr)}">${esc(w.label || shortAddr(addr))}</code></td><td>${esc(w.tier || '—')}</td><td>${esc(w.alerts ?? w.total_alerts ?? 0)} / ${esc(w.completed ?? w.tracking_complete ?? 0)}</td><td class="green">${fmtPct(w.avg_max_return_pct ?? w.avg_max_return)}</td><td>${fmtPct(w.win_rate_15m)} / ${fmtPct(w.win_rate_1h)} / ${fmtPct(w.win_rate_4h)}</td><td>${esc(w.edge_score ?? w.score ?? '—')}</td></tr>`; }).join('')}</tbody></table>`; }
function renderRecent() { const items = filtered(sortSignals(dashboardData.recent)).slice(0,60); if (!items.length) { document.getElementById('recent-panel').innerHTML = '<div class="empty">No recent alerts after filters.</div>'; return; } document.getElementById('recent-panel').innerHTML = `<table><thead><tr><th>Age</th><th>Token</th><th>Type</th><th>State</th><th>Score</th><th>Telegram</th><th>Dex</th></tr></thead><tbody>${items.map(a => { const url = dexUrl(a); return `<tr><td>${age(a.timestamp)}</td><td>${tokenLine(a)}</td><td>${esc(a.alert_type || '—')}</td><td>${stateBadge(a.queue_state)}</td><td>${score(a)||'—'}</td><td>${a.telegram_sent ? '<span class="green">sent</span>' : '<span class="muted">logged</span>'}</td><td>${url ? `<a target="_blank" rel="noopener" href="${esc(url)}">open</a>` : '—'}</td></tr>`; }).join('')}</tbody></table>`; }
function renderAll() { renderKpis(); renderQueue(); renderWinners(); renderWallets(); renderRecent(); }
async function load() { document.getElementById('timestamp').textContent = new Date().toLocaleString(); try { const [health, stats, recent, wallets, queue] = await Promise.all([fetchJSON('/health'), fetchJSON('/journal/stats'), fetchJSON('/journal/recent?limit=200'), fetchJSON('/wallet/stats?limit=100'), fetchJSON('/queue?limit=200')]); document.getElementById('live-dot').className = 'status-dot live'; document.getElementById('status').textContent = health.status === 'ok' ? 'Live — service ok' : 'Service status unknown'; dashboardData = { stats, recent: arr(recent,'alerts'), wallets: arr(wallets,'wallets'), queue: arr(queue,'items') }; renderAll(); } catch (e) { document.getElementById('status').innerHTML = '<span class="error">Dashboard load failed: ' + esc(e.message) + '</span>'; } }
document.getElementById('refresh').addEventListener('click', load); document.getElementById('search').addEventListener('input', renderAll); document.getElementById('state-filter').addEventListener('change', renderAll); document.addEventListener('click', e => { const c = e.target.closest('[data-copy]'); if (c) navigator.clipboard?.writeText(c.dataset.copy); });
load(); setInterval(load, 60000);
</script>
</body>
</html>
"""


def start_health_server(port=8080):
    """Start the health/API server in a daemon thread."""
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        logger.info("Health/API server listening on :%s", port)
    except Exception as e:
        logger.warning("Health server failed to start: %s", e)
