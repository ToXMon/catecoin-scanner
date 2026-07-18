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

            self._send_json({"error": "not_found", "path": path}, status=404)
        except Exception as e:
            logger.exception("HTTP endpoint failed: %s", path)
            self._send_json({"error": str(e), "path": path}, status=500)

    def log_message(self, *args):
        pass


def start_health_server(port=8080):
    """Start the health/API server in a daemon thread."""
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        logger.info("Health/API server listening on :%s", port)
    except Exception as e:
        logger.warning("Health server failed to start: %s", e)
