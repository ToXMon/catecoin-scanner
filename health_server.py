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
<title>Catecoin Scanner — Analysis Dashboard</title>
<meta http-equiv="refresh" content="60">
<style>
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f1117; color: #e0e0e0; margin: 0; padding: 20px; }
h1 { color: #00d4ff; font-size: 24px; margin-bottom: 5px; }
h2 { color: #00d4ff; font-size: 18px; margin-top: 25px; border-bottom: 1px solid #333; padding-bottom: 5px; }
.status { color: #00ff88; font-size: 14px; margin-bottom: 15px; }
.card { background: #1a1d26; border-radius: 8px; padding: 15px; margin-bottom: 12px; border-left: 3px solid #00d4ff; }
.card-title { font-weight: bold; color: #fff; margin-bottom: 8px; }
.metric { display: inline-block; margin-right: 20px; font-size: 13px; color: #aaa; }
.metric b { color: #fff; }
.positive { color: #00ff88; } .negative { color: #ff4757; } .neutral { color: #ffd700; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { text-align: left; padding: 8px; color: #00d4ff; border-bottom: 1px solid #333; }
td { padding: 8px; border-bottom: 1px solid #222; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; }
.tag-entry { background: #00d4ff; color: #000; }
.tag-candidate { background: #ffd700; color: #000; }
.tag-observe { background: #555; color: #fff; }
.tag-telegram { background: #00ff88; color: #000; }
.tag-no-telegram { background: #ff4757; color: #000; }
.refresh { color: #666; font-size: 11px; margin-top: 30px; }
.empty { color: #666; font-style: italic; }
</style>
</head>
<body>
<h1>Catecoin Scanner — Analysis Dashboard</h1>
<div class="status" id="status">Loading...</div>
<div id="analyzer" class="card">
  <div class="card-title">Analyzer Report</div>
  <div id="analyzer-content" class="empty">Loading analyzer report...</div>
</div>
<div id="adjustments" class="card">
  <div class="card-title">Data-Driven Adjustments</div>
  <div id="adjustments-content" class="empty">Loading adjustments...</div>
</div>
<div id="stats" class="card">
  <div class="card-title">Journal Stats</div>
  <div id="stats-content" class="empty">Loading stats...</div>
</div>
<div id="queue" class="card">
  <div class="card-title">Active Queue</div>
  <div id="queue-content" class="empty">Loading queue...</div>
</div>
<div id="recent" class="card">
  <div class="card-title">Recent Alerts</div>
  <div id="recent-content" class="empty">Loading recent alerts...</div>
</div>
<div class="refresh">Auto-refresh every 60 seconds | Last updated: <span id="timestamp"></span></div>
<script>
function fetchJSON(url) {
  return fetch(url).then(r => r.json()).catch(e => ({ error: e.message }));
}
function fmtPct(v) { return v !== null && v !== undefined ? (v * 100).toFixed(1) + '%' : 'N/A'; }
function fmtNum(v) { return v !== null && v !== undefined ? v.toLocaleString() : 'N/A'; }
function fmtUSD(v) { return v !== null && v !== undefined ? '$' + v.toLocaleString() : 'N/A'; }
function tagClass(state) {
  if (state === 'entry_ready') return 'tag-entry';
  if (state === 'candidate') return 'tag-candidate';
  return 'tag-observe';
}
function tagTelegram(sent) {
  return sent ? '<span class="tag tag-telegram">Telegram</span>' : '<span class="tag tag-no-telegram">No Telegram</span>';
}
async function load() {
  const ts = new Date().toLocaleString();
  document.getElementById('timestamp').textContent = ts;

  // Health
  const health = await fetchJSON('/health');
  if (health.status === 'ok') {
    document.getElementById('status').innerHTML = '<span class="positive">●</span> Live — service ok';
  } else {
    document.getElementById('status').innerHTML = '<span class="negative">●</span> Issue detected';
  }

  // Analyzer report
  const report = await fetchJSON('/analyzer/report');
  const ac = document.getElementById('analyzer-content');
  if (report.status === 'no_report_yet') {
    ac.innerHTML = '<span class="neutral">Analyzer has not generated a report yet. First cycle runs on next 4-hour interval.</span>';
  } else if (report.error) {
    ac.innerHTML = '<span class="negative">Error: ' + report.error + '</span>';
  } else {
    let html = '<div class="metric">Alerts analyzed: <b>' + (report.alerts_analyzed || 0) + '</b></div>';
    html += '<div class="metric">Report generated: <b>' + (report.generated_at || 'N/A') + '</b></div>';
    if (report.recommendations && report.recommendations.length > 0) {
      html += '<div style="margin-top:10px;"><b>Recommendations:</b><ul style="margin:5px 0; padding-left:20px;">';
      report.recommendations.slice(0, 5).forEach(r => { html += '<li>' + r + '</li>'; });
      html += '</ul></div>';
    }
    ac.innerHTML = html;
  }

  // Adjustments
  const adj = await fetchJSON('/analyzer/stats');
  const adc = document.getElementById('adjustments-content');
  if (adj.status === 'no_adjustments_yet') {
    adc.innerHTML = '<span class="neutral">No data-driven adjustments generated yet.</span>';
  } else if (adj.error) {
    adc.innerHTML = '<span class="negative">Error: ' + adj.error + '</span>';
  } else {
    adc.innerHTML = '<pre style="font-size:11px; color:#aaa;">' + JSON.stringify(adj, null, 2).slice(0, 800) + '</pre>';
  }

  // Stats
  const stats = await fetchJSON('/journal/stats');
  const sc = document.getElementById('stats-content');
  if (stats.error) {
    sc.innerHTML = '<span class="negative">Error: ' + stats.error + '</span>';
  } else {
    let html = '<div class="metric">Total alerts: <b>' + fmtNum(stats.total_alerts) + '</b></div>';
    html += '<div class="metric">Tracking complete: <b>' + fmtNum(stats.tracking_complete) + '</b></div>';
    html += '<div class="metric">Price checks: <b>' + fmtNum(stats.price_checks) + '</b></div>';
    html += '<div class="metric">Profitable 48h: <b>' + fmtNum(stats.profitable_48h) + '</b></div>';
    if (stats.intervals) {
      html += '<div style="margin-top:10px;"><b>Win rates by interval:</b><br>';
      ['15m', '1h', '4h', '24h', '48h'].forEach(i => {
        const d = stats.intervals[i];
        if (d && d.win_rate !== null) {
          html += '<div class="metric">' + i + ': <b>' + fmtPct(d.win_rate) + '</b> (' + d.checks + ' checks)</div>';
        }
      });
      html += '</div>';
    }
    sc.innerHTML = html;
  }

  // Queue
  const queue = await fetchJSON('/queue?limit=20');
  const qc = document.getElementById('queue-content');
  if (queue.error) {
    qc.innerHTML = '<span class="negative">Error: ' + queue.error + '</span>';
  } else if (!queue.items || queue.items.length === 0) {
    qc.innerHTML = '<span class="neutral">No active queue items.</span>';
  } else {
    let html = '<table><tr><th>Token</th><th>Chain</th><th>State</th><th>Telegram</th><th>Score</th></tr>';
    queue.items.slice(0, 15).forEach(item => {
      html += '<tr><td>' + (item.token_symbol || '?') + '</td>' +
        '<td>' + (item.chain || '?') + '</td>' +
        '<td><span class="tag ' + tagClass(item.queue_state) + '">' + (item.queue_state || '?') + '</span></td>' +
        '<td>' + tagTelegram(item.telegram_sent) + '</td>' +
        '<td>' + (item.alpha_score || item.runner_score || 'N/A') + '</td></tr>';
    });
    html += '</table>';
    qc.innerHTML = html;
  }

  // Recent
  const recent = await fetchJSON('/journal/recent?limit=15');
  const rc = document.getElementById('recent-content');
  if (recent.error) {
    rc.innerHTML = '<span class="negative">Error: ' + recent.error + '</span>';
  } else if (!recent.alerts || recent.alerts.length === 0) {
    rc.innerHTML = '<span class="neutral">No recent alerts.</span>';
  } else {
    let html = '<table><tr><th>Time</th><th>Token</th><th>Type</th><th>Chain</th><th>State</th><th>Telegram</th></tr>';
    recent.alerts.slice(0, 15).forEach(a => {
      const dt = a.timestamp ? new Date(a.timestamp * 1000).toLocaleString() : '?';
      html += '<tr><td>' + dt + '</td>' +
        '<td>' + (a.token_symbol || '?') + '</td>' +
        '<td>' + (a.alert_type || '?') + '</td>' +
        '<td>' + (a.chain || '?') + '</td>' +
        '<td><span class="tag ' + tagClass(a.queue_state) + '">' + (a.queue_state || '?') + '</span></td>' +
        '<td>' + tagTelegram(a.telegram_sent) + '</td></tr>';
    });
    html += '</table>';
    rc.innerHTML = html;
  }
}
load();
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
