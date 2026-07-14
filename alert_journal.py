#!/usr/bin/env python3
"""Alert Journal — SQLite-backed alert logging with forward price tracking.

Logs every alert with full market context, then tracks price outcomes at
configurable intervals (15m, 1h, 4h, 24h, 48h) after the alert. Exports
completed records as JSONL for LLM fine-tuning on profitable signals.

Usage:
    journal = AlertJournal()
    alert_id = journal.log_alert({...alert_data...})
    journal.run_price_tracker(dexscreener_client)  # call each scan cycle
    journal.export_jsonl("state/exports/alerts.jsonl")
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger("catecoin-scanner.journal")

INTERVAL_SECONDS = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "24h": 24 * 60 * 60,
    "48h": 48 * 60 * 60,
}


class AlertJournal:
    """SQLite alert journal with forward-looking price outcome tracking."""

    def __init__(
        self,
        db_path: str = "state/alert_journal.db",
        enabled: bool = True,
        intervals: Optional[List[str]] = None,
    ) -> None:
        self.db_path = db_path
        self.enabled = enabled
        self.intervals = intervals or ["15m", "1h", "4h", "24h", "48h"]

        if not self.enabled:
            logger.info("Alert journal DISABLED")
            return

        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_db()
        logger.info("Alert journal ready: %s", db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id TEXT UNIQUE NOT NULL,
                    timestamp INTEGER NOT NULL,
                    alert_type TEXT NOT NULL,
                    token_symbol TEXT,
                    token_name TEXT,
                    token_address TEXT NOT NULL,
                    chain TEXT DEFAULT 'robinhood',
                    price_usd REAL,
                    liquidity_usd REAL,
                    volume_24h REAL,
                    market_cap REAL,
                    fdv REAL,
                    holders INTEGER,
                    liq_mcap_ratio REAL,
                    wallet_address TEXT,
                    wallet_label TEXT,
                    wallet_tier TEXT,
                    wallet_score REAL,
                    alpha_score INTEGER,
                    risk_level TEXT,
                    thesis TEXT,
                    risk_factors TEXT,
                    telegram_sent INTEGER DEFAULT 0,
                    telegram_message_id TEXT,
                    tracking_complete INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS price_tracking (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id TEXT NOT NULL,
                    check_interval TEXT NOT NULL,
                    check_timestamp INTEGER NOT NULL,
                    price_usd REAL,
                    liquidity_usd REAL,
                    volume_24h REAL,
                    price_change_pct REAL,
                    max_price_usd REAL,
                    min_price_usd REAL,
                    max_drawdown_pct REAL,
                    UNIQUE(alert_id, check_interval),
                    FOREIGN KEY (alert_id) REFERENCES alerts(alert_id)
                );

                CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp);
                CREATE INDEX IF NOT EXISTS idx_tracking_alert ON price_tracking(alert_id);
                """
            )

    def log_alert(self, data: Dict[str, Any]) -> Optional[str]:
        """Insert alert, return alert_id (UUID). Returns None if disabled."""
        if not self.enabled:
            return None

        alert_id = str(uuid.uuid4())
        now = int(time.time())

        liquidity = float(data.get("liquidity_usd", 0) or 0)
        mcap = float(data.get("market_cap", 0) or data.get("fdv", 0) or 0)
        liq_mcap = (liquidity / mcap) if mcap > 0 else None

        try:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT INTO alerts (
                        alert_id, timestamp, alert_type,
                        token_symbol, token_name, token_address, chain,
                        price_usd, liquidity_usd, volume_24h, market_cap, fdv,
                        holders, liq_mcap_ratio,
                        wallet_address, wallet_label, wallet_tier, wallet_score,
                        alpha_score, risk_level, thesis, risk_factors,
                        telegram_sent
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        alert_id,
                        now,
                        data.get("alert_type", "unknown"),
                        data.get("token_symbol", ""),
                        data.get("token_name", ""),
                        data.get("token_address", ""),
                        data.get("chain", "robinhood"),
                        float(data.get("price_usd", 0) or 0),
                        liquidity,
                        float(data.get("volume_24h", 0) or 0),
                        float(data.get("market_cap", 0) or 0),
                        float(data.get("fdv", 0) or 0),
                        int(data.get("holders", 0) or 0),
                        liq_mcap,
                        data.get("wallet_address", ""),
                        data.get("wallet_label", ""),
                        data.get("wallet_tier", ""),
                        float(data.get("wallet_score", 0) or 0),
                        int(data.get("alpha_score", 0) or 0),
                        data.get("risk_level", ""),
                        data.get("thesis", ""),
                        data.get("risk_factors", ""),
                        1 if data.get("telegram_sent", False) else 0,
                    ),
                )
            logger.debug("Logged alert %s for %s", alert_id, data.get("token_symbol", "?"))
            return alert_id
        except Exception as e:
            logger.error("Failed to log alert: %s", e)
            return None

    def get_alerts_needing_price_check(self) -> List[sqlite3.Row]:
        """Return alerts where at least one interval is due but not yet checked."""
        if not self.enabled:
            return []
        now = int(time.time())
        results: List[sqlite3.Row] = []
        try:
            with self._conn() as conn:
                for alert in conn.execute(
                    "SELECT * FROM alerts WHERE tracking_complete = 0 ORDER BY timestamp DESC LIMIT 200"
                ).fetchall():
                    alert_ts = alert["timestamp"]
                    due = []
                    for interval in self.intervals:
                        seconds = INTERVAL_SECONDS.get(interval, 0)
                        if now >= alert_ts + seconds:
                            already = conn.execute(
                                "SELECT 1 FROM price_tracking WHERE alert_id = ? AND check_interval = ?",
                                (alert["alert_id"], interval),
                            ).fetchone()
                            if not already:
                                due.append(interval)
                    if due:
                        row = dict(alert)
                        row["due_intervals"] = due
                        results.append(row)
        except Exception as e:
            logger.error("get_alerts_needing_price_check failed: %s", e)
        return results

    def record_price_check(
        self,
        alert_id: str,
        interval: str,
        price: float,
        liquidity: float,
        volume: float,
        alert_price: float,
    ) -> None:
        """Record price at interval, compute change pct, update max/min/drawdown."""
        if not self.enabled:
            return

        change_pct = ((price - alert_price) / alert_price * 100) if alert_price > 0 else 0.0

        try:
            with self._conn() as conn:
                # Get existing max/min across prior checks
                row = conn.execute(
                    """
                    SELECT MAX(max_price_usd) as mx, MIN(min_price_usd) as mn
                    FROM price_tracking WHERE alert_id = ?
                    """,
                    (alert_id,),
                ).fetchone()

                prior_max = row["mx"] if row and row["mx"] is not None else price
                prior_min = row["mn"] if row and row["mn"] is not None else price

                max_price = max(prior_max, price, alert_price)
                min_price = min(prior_min, price, alert_price)
                max_drawdown = ((min_price - max_price) / max_price * 100) if max_price > 0 else 0.0

                conn.execute(
                    """
                    INSERT OR REPLACE INTO price_tracking
                        (alert_id, check_interval, check_timestamp,
                         price_usd, liquidity_usd, volume_24h,
                         price_change_pct, max_price_usd, min_price_usd, max_drawdown_pct)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        alert_id,
                        interval,
                        int(time.time()),
                        price,
                        liquidity,
                        volume,
                        change_pct,
                        max_price,
                        min_price,
                        max_drawdown,
                    ),
                )

                # Check if all intervals complete
                checked = conn.execute(
                    "SELECT COUNT(DISTINCT check_interval) as cnt FROM price_tracking WHERE alert_id = ?",
                    (alert_id,),
                ).fetchone()
                if checked and checked["cnt"] >= len(self.intervals):
                    conn.execute(
                        "UPDATE alerts SET tracking_complete = 1 WHERE alert_id = ?",
                        (alert_id,),
                    )
                    logger.info("Alert %s tracking complete", alert_id)
        except Exception as e:
            logger.error("record_price_check failed: %s", e)

    def run_price_tracker(self, dexscreener_client: Any) -> int:
        """Check due price intervals. Returns number of checks performed."""
        if not self.enabled:
            return 0

        alerts = self.get_alerts_needing_price_check()
        if not alerts:
            return 0

        checks_done = 0
        for alert in alerts:
            token_addr = alert["token_address"]
            alert_price = alert["price_usd"] or 0.0
            alert_id = alert["alert_id"]

            # Validate address for DexScreener
            if not token_addr or not token_addr.startswith("0x") or len(token_addr) < 42:
                continue

            try:
                pair = dexscreener_client.get_token(token_addr) or {}
                price = float(pair.get("priceUsd", 0) or 0)
                liquidity = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
                volume = float((pair.get("volume") or {}).get("h24", 0) or 0)

                if price <= 0:
                    continue

                for interval in alert["due_intervals"]:
                    self.record_price_check(
                        alert_id, interval, price, liquidity, volume, alert_price
                    )
                    checks_done += 1

                # Throttle DexScreener
                time.sleep(0.25)
            except Exception as e:
                logger.debug("Price tracker fetch failed for %s: %s", token_addr[:10], e)

        if checks_done:
            logger.info("Price tracker: %d checks across %d alerts", checks_done, len(alerts))
        return checks_done

    def export_jsonl(self, output_path: str, min_alerts: int = 0) -> int:
        """Export completed alerts as JSONL for LLM training."""
        if not self.enabled:
            return 0

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        records: List[dict] = []
        try:
            with self._conn() as conn:
                alerts = conn.execute(
                    "SELECT * FROM alerts WHERE tracking_complete = 1 ORDER BY timestamp ASC"
                ).fetchall()

                for alert in alerts:
                    aid = alert["alert_id"]
                    checks = conn.execute(
                        "SELECT * FROM price_tracking WHERE alert_id = ? ORDER BY check_timestamp",
                        (aid,),
                    ).fetchall()

                    if len(checks) < len(self.intervals):
                        continue

                    outcomes: Dict[str, dict] = {}
                    for c in checks:
                        outcomes[c["check_interval"]] = {
                            "price": c["price_usd"],
                            "liquidity": c["liquidity_usd"],
                            "volume": c["volume_24h"],
                            "change_pct": c["price_change_pct"],
                        }

                    alert_price = alert["price_usd"] or 0.0
                    max_return = max(
                        (c["price_change_pct"] for c in checks if c["price_change_pct"] is not None),
                        default=0.0,
                    )
                    max_drawdown = min(
                        (c["max_drawdown_pct"] for c in checks if c["max_drawdown_pct"] is not None),
                        default=0.0,
                    )
                    final_change = outcomes.get("48h", outcomes.get("24h", {})).get("change_pct", 0)

                    records.append(
                        {
                            "alert_id": aid,
                            "alert_type": alert["alert_type"],
                            "timestamp": alert["timestamp"],
                            "token_symbol": alert["token_symbol"],
                            "token_address": alert["token_address"],
                            "price_at_alert": alert_price,
                            "context": {
                                "liquidity_usd": alert["liquidity_usd"],
                                "volume_24h": alert["volume_24h"],
                                "market_cap": alert["market_cap"],
                                "fdv": alert["fdv"],
                                "holders": alert["holders"],
                                "liq_mcap_ratio": alert["liq_mcap_ratio"],
                                "wallet_label": alert["wallet_label"],
                                "wallet_tier": alert["wallet_tier"],
                                "wallet_score": alert["wallet_score"],
                                "alpha_score": alert["alpha_score"],
                                "risk_level": alert["risk_level"],
                                "thesis": alert["thesis"],
                                "risk_factors": alert["risk_factors"],
                            },
                            "outcomes": outcomes,
                            "max_return_pct": max_return,
                            "max_drawdown_pct": max_drawdown,
                            "final_change_pct": final_change,
                            "profitable": final_change > 0,
                        }
                    )
        except Exception as e:
            logger.error("export_jsonl failed: %s", e)
            return 0

        if len(records) < min_alerts:
            logger.info("Export skipped: %d records < min %d", len(records), min_alerts)
            return 0

        with open(output_path, "w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

        logger.info("Exported %d completed alerts to %s", len(records), output_path)
        return len(records)

    def stats(self) -> dict:
        """Return summary stats for monitoring."""
        if not self.enabled:
            return {"enabled": False}
        try:
            with self._conn() as conn:
                total = conn.execute("SELECT COUNT(*) as c FROM alerts").fetchone()["c"]
                complete = conn.execute(
                    "SELECT COUNT(*) as c FROM alerts WHERE tracking_complete = 1"
                ).fetchone()["c"]
                profitable = conn.execute(
                    """
                    SELECT COUNT(*) as c FROM price_tracking pt
                    JOIN alerts a ON pt.alert_id = a.alert_id
                    WHERE pt.check_interval = '48h' AND pt.price_change_pct > 0
                    """
                ).fetchone()["c"]
                return {
                    "enabled": True,
                    "total_alerts": total,
                    "tracking_complete": complete,
                    "profitable_48h": profitable,
                }
        except Exception as e:
            logger.error("stats failed: %s", e)
            return {"enabled": True, "error": str(e)}
