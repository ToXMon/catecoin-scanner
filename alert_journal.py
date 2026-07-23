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

from token_identity import sanitize_alert_identity

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
        # Respect STATE_DIR env var for persistent storage (e.g. Akash /data mount)
        state_dir = os.environ.get("STATE_DIR", "")
        if state_dir and not os.path.isabs(db_path):
            db_path = os.path.join(state_dir, db_path)
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
            # Create minimal tables first. Legacy databases may already have an
            # alerts table with only a few columns, so all current columns must
            # be ensured before indexes or queries reference them.
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id TEXT UNIQUE NOT NULL,
                    timestamp INTEGER NOT NULL,
                    alert_type TEXT NOT NULL,
                    token_address TEXT NOT NULL
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
                """
            )
            self._ensure_alert_columns(conn)
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp);
                CREATE INDEX IF NOT EXISTS idx_alerts_chain ON alerts(chain);
                CREATE INDEX IF NOT EXISTS idx_alerts_pair_chain ON alerts(chain, pair_address, timestamp);
                CREATE INDEX IF NOT EXISTS idx_alerts_token_chain ON alerts(chain, token_address, timestamp);
                CREATE INDEX IF NOT EXISTS idx_alerts_queue_state ON alerts(queue_state);
                CREATE INDEX IF NOT EXISTS idx_tracking_alert ON price_tracking(alert_id);
                """
            )

    def _ensure_alert_columns(self, conn: sqlite3.Connection) -> None:
        """Add columns introduced after the inherited Robinhood journal schema."""
        existing = {row[1] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()}
        columns = {
            "alert_id": "TEXT",
            "timestamp": "INTEGER DEFAULT 0",
            "alert_type": "TEXT DEFAULT 'unknown'",
            "token_symbol": "TEXT",
            "token_name": "TEXT",
            "token_address": "TEXT DEFAULT ''",
            "chain": "TEXT DEFAULT 'robinhood'",
            "price_usd": "REAL",
            "liquidity_usd": "REAL",
            "volume_24h": "REAL",
            "market_cap": "REAL",
            "fdv": "REAL",
            "holders": "INTEGER",
            "liq_mcap_ratio": "REAL",
            "wallet_address": "TEXT",
            "wallet_label": "TEXT",
            "wallet_tier": "TEXT",
            "wallet_score": "REAL",
            "alpha_score": "INTEGER",
            "risk_level": "TEXT",
            "thesis": "TEXT",
            "risk_factors": "TEXT",
            "telegram_sent": "INTEGER DEFAULT 0",
            "telegram_message_id": "TEXT",
            "queue_state": "TEXT DEFAULT 'observe'",
            "queue_reasons": "TEXT",
            "trade_plan_json": "TEXT",
            "alert_worthy": "INTEGER DEFAULT 0",
            "confidence": "TEXT",
            "pair_address": "TEXT",
            "dex_url": "TEXT",
            "tracking_complete": "INTEGER DEFAULT 0",
        }
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE alerts ADD COLUMN {name} {ddl}")

    def find_recent_observation(
        self,
        *,
        chain: str,
        pair_address: str = "",
        token_address: str = "",
        since_ts: int,
    ) -> Optional[dict]:
        """Return most recent alert for a chain+pair/token since timestamp."""
        if not self.enabled:
            return None
        clauses = ["chain = ?", "timestamp >= ?"]
        params: List[Any] = [chain, since_ts]
        identity_clauses = []
        if pair_address:
            identity_clauses.append("pair_address = ?")
            params.append(pair_address)
        if token_address:
            identity_clauses.append("token_address = ?")
            params.append(token_address)
        if not identity_clauses:
            return None
        where = " AND ".join(clauses) + " AND (" + " OR ".join(identity_clauses) + ")"
        try:
            with self._conn() as conn:
                row = conn.execute(
                    f"SELECT * FROM alerts WHERE {where} ORDER BY timestamp DESC LIMIT 1",
                    params,
                ).fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.error("find_recent_observation failed: %s", e)
            return None

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
                        telegram_sent, queue_state, queue_reasons, trade_plan_json,
                        alert_worthy, confidence, pair_address, dex_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        None if data.get("holders") is None else int(data.get("holders", 0) or 0),
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
                        data.get("queue_state", "observe"),
                        json.dumps(data.get("queue_reasons", [])),
                        json.dumps(data.get("trade_plan", {})),
                        1 if data.get("alert_worthy", False) else 0,
                        data.get("confidence", ""),
                        data.get("pair_address", ""),
                        data.get("dex_url", ""),
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
            chain = alert["chain"] or "robinhood"

            # Validate address for DexScreener
            if not token_addr or not token_addr.startswith("0x") or len(token_addr) < 42:
                continue

            try:
                try:
                    pair = dexscreener_client.get_token(token_addr, chain=chain) or {}
                except TypeError:
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

    def export_jsonl(self, output_path: str, min_alerts: int = 0, chain: Optional[str] = None) -> int:
        """Export completed alerts as JSONL for LLM training."""
        if not self.enabled:
            return 0

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        records: List[dict] = []
        try:
            with self._conn() as conn:
                if chain:
                    alerts = conn.execute(
                        "SELECT * FROM alerts WHERE tracking_complete = 1 AND chain = ? ORDER BY timestamp ASC",
                        (chain,),
                    ).fetchall()
                else:
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
                            "chain": alert["chain"],
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

    def _alert_with_outcomes(self, conn: sqlite3.Connection, alert: sqlite3.Row) -> dict:
        """Return one alert row plus attached price-tracking outcomes."""
        checks = conn.execute(
            "SELECT * FROM price_tracking WHERE alert_id = ? ORDER BY check_timestamp",
            (alert["alert_id"],),
        ).fetchall()
        outcomes = {
            c["check_interval"]: {
                "check_timestamp": c["check_timestamp"],
                "price_usd": c["price_usd"],
                "liquidity_usd": c["liquidity_usd"],
                "volume_24h": c["volume_24h"],
                "price_change_pct": c["price_change_pct"],
                "max_price_usd": c["max_price_usd"],
                "min_price_usd": c["min_price_usd"],
                "max_drawdown_pct": c["max_drawdown_pct"],
            }
            for c in checks
        }
        rec = dict(alert)
        rec["token_symbol"], rec["token_name"] = sanitize_alert_identity(
            rec.get("token_symbol"),
            rec.get("token_name"),
            rec.get("token_address"),
        )
        for key in ("queue_reasons", "trade_plan_json"):
            if rec.get(key):
                try:
                    rec[key] = json.loads(rec[key])
                except (TypeError, json.JSONDecodeError):
                    pass
        if "trade_plan_json" in rec:
            rec["trade_plan"] = rec.get("trade_plan_json") or {}
        rec["alert_worthy"] = bool(rec.get("alert_worthy"))
        rec["outcomes"] = outcomes
        changes = [c["price_change_pct"] for c in checks if c["price_change_pct"] is not None]
        rec["max_return_pct"] = max(changes) if changes else None
        rec["min_return_pct"] = min(changes) if changes else None
        rec["latest_return_pct"] = changes[-1] if changes else None
        return rec

    def completed_records(self, limit: int = 1000, chain: Optional[str] = None) -> List[dict]:
        """Return completed alerts with attached outcomes for API/export use."""
        if not self.enabled:
            return []
        with self._conn() as conn:
            if chain:
                alerts = conn.execute(
                    "SELECT * FROM alerts WHERE tracking_complete = 1 AND chain = ? ORDER BY timestamp DESC LIMIT ?",
                    (chain, limit),
                ).fetchall()
            else:
                alerts = conn.execute(
                    "SELECT * FROM alerts WHERE tracking_complete = 1 ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._alert_with_outcomes(conn, a) for a in alerts]

    def recent_alerts(self, limit: int = 50, chain: Optional[str] = None) -> List[dict]:
        """Return recent alerts with any available outcomes."""
        if not self.enabled:
            return []
        with self._conn() as conn:
            if chain:
                alerts = conn.execute(
                    "SELECT * FROM alerts WHERE chain = ? ORDER BY timestamp DESC LIMIT ?",
                    (chain, limit),
                ).fetchall()
            else:
                alerts = conn.execute(
                    "SELECT * FROM alerts ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._alert_with_outcomes(conn, a) for a in alerts]

    def wallet_stats(self, min_alerts: int = 1) -> List[dict]:
        """Return wallet performance leaderboard from completed outcomes."""
        if not self.enabled:
            return []
        rows: Dict[str, dict] = {}
        with self._conn() as conn:
            alerts = conn.execute("SELECT * FROM alerts ORDER BY timestamp ASC").fetchall()
            for alert in alerts:
                key = alert["wallet_label"] or alert["wallet_address"] or "unknown"
                if key not in rows:
                    rows[key] = {
                        "wallet": key,
                        "wallet_address": alert["wallet_address"],
                        "wallet_tier": alert["wallet_tier"],
                        "alerts": 0,
                        "completed": 0,
                        "wins_15m": 0,
                        "wins_1h": 0,
                        "wins_4h": 0,
                        "wins_24h": 0,
                        "wins_48h": 0,
                        "max_returns": [],
                        "latest_returns": [],
                        "drawdowns": [],
                    }
                stat = rows[key]
                stat["alerts"] += 1
                checks = conn.execute(
                    "SELECT check_interval, price_change_pct, max_drawdown_pct FROM price_tracking WHERE alert_id = ?",
                    (alert["alert_id"],),
                ).fetchall()
                if checks:
                    changes = [c["price_change_pct"] for c in checks if c["price_change_pct"] is not None]
                    dds = [c["max_drawdown_pct"] for c in checks if c["max_drawdown_pct"] is not None]
                    if changes:
                        stat["max_returns"].append(max(changes))
                        stat["latest_returns"].append(changes[-1])
                    if dds:
                        stat["drawdowns"].append(min(dds))
                    if alert["tracking_complete"]:
                        stat["completed"] += 1
                    for c in checks:
                        interval = c["check_interval"]
                        if c["price_change_pct"] is not None and c["price_change_pct"] > 0:
                            field = f"wins_{interval}"
                            if field in stat:
                                stat[field] += 1
        out = []
        for stat in rows.values():
            if stat["alerts"] < min_alerts:
                continue
            completed = max(stat["completed"], 1)
            max_returns = stat.pop("max_returns")
            latest_returns = stat.pop("latest_returns")
            drawdowns = stat.pop("drawdowns")
            stat["avg_max_return_pct"] = sum(max_returns) / len(max_returns) if max_returns else None
            stat["avg_latest_return_pct"] = sum(latest_returns) / len(latest_returns) if latest_returns else None
            stat["avg_max_drawdown_pct"] = sum(drawdowns) / len(drawdowns) if drawdowns else None
            for interval in ["15m", "1h", "4h", "24h", "48h"]:
                wins = stat.get(f"wins_{interval}", 0)
                stat[f"win_rate_{interval}"] = wins / completed if stat["completed"] else None
            edge = 0.0
            if stat["avg_max_return_pct"] is not None:
                edge += stat["avg_max_return_pct"]
            if stat["avg_max_drawdown_pct"] is not None:
                edge += stat["avg_max_drawdown_pct"] * 0.5
            stat["edge_score"] = edge
            out.append(stat)
        return sorted(out, key=lambda r: (r["edge_score"], r["completed"], r["alerts"]), reverse=True)

    def queue_items(self, limit: int = 100, chain: Optional[str] = None) -> List[dict]:
        """Return current trade queue derived from recent alert outcomes."""
        items = []
        for rec in self.recent_alerts(limit=limit, chain=chain):
            if rec.get("queue_state") and rec.get("trade_plan"):
                rec["age_seconds"] = int(time.time()) - int(rec.get("timestamp") or time.time())
                items.append(rec)
                continue
            liq = rec.get("liquidity_usd") or 0
            fdv = rec.get("fdv") or rec.get("market_cap") or 0
            ratio = rec.get("liq_mcap_ratio")
            latest = rec.get("latest_return_pct")
            max_ret = rec.get("max_return_pct")
            age_s = int(time.time()) - int(rec.get("timestamp") or time.time())
            state = "observe"
            reasons = []
            if liq >= 10000:
                reasons.append("liquidity>=10k")
            if ratio is not None and ratio >= 0.08:
                reasons.append("liq_fdv>=8pct")
            if rec.get("wallet_tier") in ("smart_money_elite", "sniper"):
                reasons.append("strong_wallet_tier")
            if len(reasons) >= 2:
                state = "candidate"
            if latest is not None and latest > 0 and age_s >= 15 * 60 and state == "candidate":
                state = "entry_ready"
            if max_ret is not None and max_ret >= 50:
                state = "take_profit_watch"
            if latest is not None and latest <= -25:
                state = "avoid_or_cut"
            rec["queue_state"] = state
            rec["queue_reasons"] = reasons
            rec["age_seconds"] = age_s
            items.append(rec)
        return items

    def export_jsonl_text(self, limit: int = 1000, chain: Optional[str] = None) -> str:
        """Return completed records as JSONL string for HTTP download."""
        return "\n".join(json.dumps(r) for r in self.completed_records(limit=limit, chain=chain)) + "\n"

    def stats(self, chain: Optional[str] = None) -> dict:
        """Return summary stats for monitoring."""
        if not self.enabled:
            return {"enabled": False}
        try:
            with self._conn() as conn:
                if chain:
                    total = conn.execute("SELECT COUNT(*) as c FROM alerts WHERE chain = ?", (chain,)).fetchone()["c"]
                    complete = conn.execute(
                        "SELECT COUNT(*) as c FROM alerts WHERE tracking_complete = 1 AND chain = ?",
                        (chain,),
                    ).fetchone()["c"]
                else:
                    total = conn.execute("SELECT COUNT(*) as c FROM alerts").fetchone()["c"]
                    complete = conn.execute(
                        "SELECT COUNT(*) as c FROM alerts WHERE tracking_complete = 1"
                    ).fetchone()["c"]
                if chain:
                    checks = conn.execute(
                        """
                        SELECT COUNT(*) as c FROM price_tracking pt
                        JOIN alerts a ON pt.alert_id = a.alert_id
                        WHERE a.chain = ?
                        """,
                        (chain,),
                    ).fetchone()["c"]
                    profitable = conn.execute(
                        """
                        SELECT COUNT(*) as c FROM price_tracking pt
                        JOIN alerts a ON pt.alert_id = a.alert_id
                        WHERE a.chain = ? AND pt.check_interval = '48h' AND pt.price_change_pct > 0
                        """,
                        (chain,),
                    ).fetchone()["c"]
                else:
                    checks = conn.execute("SELECT COUNT(*) as c FROM price_tracking").fetchone()["c"]
                    profitable = conn.execute(
                        """
                        SELECT COUNT(*) as c FROM price_tracking pt
                        JOIN alerts a ON pt.alert_id = a.alert_id
                        WHERE pt.check_interval = '48h' AND pt.price_change_pct > 0
                        """
                    ).fetchone()["c"]
                intervals = {}
                for interval in self.intervals:
                    row = conn.execute(
                        """
                        SELECT COUNT(*) as n,
                               SUM(CASE WHEN price_change_pct > 0 THEN 1 ELSE 0 END) as wins,
                               AVG(price_change_pct) as avg_change
                        FROM price_tracking pt
                        JOIN alerts a ON pt.alert_id = a.alert_id
                        WHERE pt.check_interval = ? AND (? IS NULL OR a.chain = ?)
                        """,
                        (interval, chain, chain),
                    ).fetchone()
                    n = row["n"] or 0
                    wins = row["wins"] or 0
                    intervals[interval] = {
                        "checks": n,
                        "wins": wins,
                        "win_rate": (wins / n) if n else None,
                        "avg_change_pct": row["avg_change"],
                    }
                return {
                    "enabled": True,
                    "db_path": self.db_path,
                    "chain": chain,
                    "total_alerts": total,
                    "tracking_complete": complete,
                    "price_checks": checks,
                    "profitable_48h": profitable,
                    "completion_rate": (complete / total) if total else 0,
                    "intervals": intervals,
                }
        except Exception as e:
            logger.error("stats failed: %s", e)
            return {"enabled": True, "error": str(e), "db_path": self.db_path}
