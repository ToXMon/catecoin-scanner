#!/usr/bin/env python3
"""Alert Analyzer — Self-improvement loop for catecoin-scanner.

Runs every 4 hours (configurable) to analyze Telegram alerts that were
received, including price performance, volume, liquidity, holder changes,
and on-chain signals.  Generates improvement reports with actionable
threshold recommendations and optional scorer_adjustments.json for the
feedback loop.

Usage (inside scanner.py main loop):
    analyzer = AlertAnalyzer(config, journal, dex_client, blockscout_client)
    analyzer.run_analysis()
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("catecoin-scanner.analyzer")


# ---------------------------------------------------------------------------
# Outcome windows mapped to seconds
# ---------------------------------------------------------------------------
WINDOW_SECONDS = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "24h": 24 * 60 * 60,
    "48h": 48 * 60 * 60,
}


# ---------------------------------------------------------------------------
# AlertAnalyzer
# ---------------------------------------------------------------------------
class AlertAnalyzer:
    """Periodic analyser that reviews past alerts, computes outcomes, and
    generates improvement recommendations for the live scanner."""

    def __init__(
        self,
        config: dict,
        journal: Any,  # AlertJournal instance
        dex_client: Any,  # DexScreenerClient
        blockscout_client: Any = None,  # BlockscoutClient (optional)
    ) -> None:
        cfg = config if isinstance(config, dict) else {}
        journal_cfg = cfg.get("journal", {}) or {}
        analyzer_cfg = cfg.get("alert_analyzer", {}) or {}

        self.enabled: bool = bool(analyzer_cfg.get("enabled", True))
        self.interval_hours: float = float(analyzer_cfg.get("interval_hours", 4))
        self.interval_seconds: int = int(self.interval_hours * 3600)
        self.min_alerts_for_analysis: int = int(analyzer_cfg.get("min_alerts_for_analysis", 5))
        self.auto_apply_thresholds: bool = bool(analyzer_cfg.get("auto_apply_thresholds", False))
        self.outcome_windows: List[str] = list(
            analyzer_cfg.get("outcome_windows", ["15m", "1h", "4h", "24h", "48h"])
        )

        self.journal = journal
        self.dex = dex_client
        self.blockscout = blockscout_client

        # Persistence paths
        state_dir = os.environ.get("STATE_DIR", "")
        db_path = journal_cfg.get("db_path", "state/alert_journal.db")
        if state_dir and not os.path.isabs(db_path):
            db_path = os.path.join(state_dir, db_path)
        self.db_path = db_path

        adj_path = analyzer_cfg.get("scorer_adjustments_path", "state/scorer_adjustments.json")
        if state_dir and not os.path.isabs(adj_path):
            adj_path = os.path.join(state_dir, adj_path)
        self.scorer_adjustments_path = adj_path

        report_path = analyzer_cfg.get("report_path", "state/analyzer_report.json")
        if state_dir and not os.path.isabs(report_path):
            report_path = os.path.join(state_dir, report_path)
        self.report_path = report_path

        # Runtime state
        self.last_run_time: float = 0.0
        self.last_report: Optional[dict] = None
        self.total_runs: int = 0
        self.total_alerts_analyzed: int = 0
        self.total_reports_generated: int = 0

        if self.enabled:
            self._ensure_tables()
            # Load last report from disk if available
            self._load_last_report()
            logger.info(
                "Alert analyzer ready: interval=%dh min_alerts=%d auto_apply=%s",
                self.interval_hours, self.min_alerts_for_analysis, self.auto_apply_thresholds,
            )
        else:
            logger.info("Alert analyzer DISABLED")

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_tables(self) -> None:
        """Create alert_analysis and improvement_reports tables if missing."""
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS alert_analysis (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id TEXT UNIQUE NOT NULL,
                    analysis_timestamp INTEGER NOT NULL,
                    current_price_usd REAL,
                    current_liquidity_usd REAL,
                    current_volume_24h REAL,
                    current_fdv REAL,
                    price_change_pct REAL,
                    max_return_pct REAL,
                    max_drawdown_pct REAL,
                    liquidity_change_pct REAL,
                    holder_count_at_alert INTEGER,
                    holder_count_current INTEGER,
                    holder_change_pct REAL,
                    volume_trajectory TEXT,
                    buy_sell_pressure REAL,
                    rug_pull_score INTEGER DEFAULT 0,
                    rug_indicators TEXT,
                    overall_outcome TEXT DEFAULT 'neutral',
                    FOREIGN KEY (alert_id) REFERENCES alerts(alert_id)
                );

                CREATE TABLE IF NOT EXISTS improvement_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    report_json TEXT NOT NULL,
                    summary_text TEXT,
                    alerts_analyzed INTEGER DEFAULT 0,
                    positive_edge_count INTEGER DEFAULT 0,
                    negative_edge_count INTEGER DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_analysis_alert ON alert_analysis(alert_id);
                CREATE INDEX IF NOT EXISTS idx_analysis_outcome ON alert_analysis(overall_outcome);
                CREATE INDEX IF NOT EXISTS idx_reports_timestamp ON improvement_reports(timestamp);
                """
            )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def run_analysis(self) -> dict:
        """Run one analysis cycle.  Returns summary dict."""
        if not self.enabled:
            return {"enabled": False}

        start = time.time()
        logger.info("Alert analyzer: starting analysis cycle")

        # 1. Get alerts to analyse
        alerts = self._get_alerts_for_analysis()
        if not alerts:
            logger.info("Alert analyzer: no alerts to analyse")
            return {"alerts_analyzed": 0, "reason": "no_alerts"}

        # 2. First run the existing price tracker to refresh price_tracking data
        try:
            self.journal.run_price_tracker(self.dex)
        except Exception as e:
            logger.warning("Price tracker pre-run failed (non-fatal): %s", e)

        # 3. For each alert, fetch current data and compute outcomes
        analyzed: List[dict] = []
        for alert in alerts:
            try:
                outcome = self._analyze_one_alert(alert)
                if outcome:
                    analyzed.append(outcome)
            except Exception as e:
                logger.debug("Alert analysis failed for %s: %s", alert.get("alert_id", "?"), e)
            # Throttle API calls
            time.sleep(0.3)

        logger.info("Alert analyzer: analysed %d / %d alerts", len(analyzed), len(alerts))

        # 4. Generate improvement report (even with few alerts)
        report = None
        if len(analyzed) >= self.min_alerts_for_analysis:
            report = self._generate_improvement_report(analyzed)
        else:
            logger.info(
                "Alert analyzer: only %d analysed (min %d) — skipping full report",
                len(analyzed), self.min_alerts_for_analysis,
            )

        # 5. Optionally write scorer_adjustments.json
        if report and self.auto_apply_thresholds:
            self._write_scorer_adjustments(report)
            logger.info("Alert analyzer: wrote scorer_adjustments.json (auto_apply=true)")
        elif report:
            self._write_scorer_adjustments(report)
            logger.info("Alert analyzer: wrote scorer_adjustments.json (review-only, auto_apply=false)")

        # 6. Update runtime state
        self.last_run_time = start
        self.total_runs += 1
        self.total_alerts_analyzed += len(analyzed)
        if report:
            self.total_reports_generated += 1
            self.last_report = report
            self._save_report_to_disk(report)

        elapsed = time.time() - start
        logger.info(
            "Alert analyzer: cycle complete in %.1fs — analysed=%d report=%s",
            elapsed, len(analyzed), "yes" if report else "no",
        )

        return {
            "alerts_analyzed": len(analyzed),
            "report_generated": bool(report),
            "elapsed_seconds": round(elapsed, 1),
        }

    # ------------------------------------------------------------------
    # Alert selection
    # ------------------------------------------------------------------
    def _get_alerts_for_analysis(self) -> List[dict]:
        """Return alerts that need analysis.

        Priority:
        1. Alerts with telegram_sent=1 that have not been analysed yet
        2. Alerts with tracking_complete=0 that have some intervals due
        """
        now = int(time.time())
        results: List[dict] = []

        try:
            with self._conn() as conn:
                # Priority 1: Telegram-delivered alerts not yet analysed
                unanalysed = conn.execute(
                    """
                    SELECT a.* FROM alerts a
                    LEFT JOIN alert_analysis aa ON a.alert_id = aa.alert_id
                    WHERE a.telegram_sent = 1
                      AND aa.alert_id IS NULL
                      AND a.token_address != ''
                      AND a.token_address LIKE '0x%'
                    ORDER BY a.timestamp DESC
                    LIMIT 100
                    """
                ).fetchall()

                for row in unanalysed:
                    results.append(dict(row))

                # Priority 2: Stale analysed alerts (re-analyse if > 24h since last analysis)
                stale_ts = now - 86400
                stale = conn.execute(
                    """
                    SELECT a.* FROM alerts a
                    JOIN alert_analysis aa ON a.alert_id = aa.alert_id
                    WHERE a.telegram_sent = 1
                      AND aa.analysis_timestamp < ?
                      AND a.token_address != ''
                      AND a.token_address LIKE '0x%'
                    ORDER BY a.timestamp DESC
                    LIMIT 50
                    """,
                    (stale_ts,),
                ).fetchall()

                for row in stale:
                    if not any(r.get("alert_id") == row["alert_id"] for r in results):
                        results.append(dict(row))

                # Priority 3: Alerts with tracking_complete=0 that are old enough for
                # at least the 15m interval to have passed
                min_age = WINDOW_SECONDS.get("15m", 900)
                tracking_incomplete = conn.execute(
                    """
                    SELECT a.* FROM alerts a
                    LEFT JOIN alert_analysis aa ON a.alert_id = aa.alert_id
                    WHERE a.tracking_complete = 0
                      AND (a.telegram_sent = 1 OR a.alert_worthy = 1)
                      AND a.token_address != ''
                      AND a.token_address LIKE '0x%'
                      AND a.timestamp < ?
                      AND aa.alert_id IS NULL
                    ORDER BY a.timestamp DESC
                    LIMIT 50
                    """,
                    (now - min_age,),
                ).fetchall()

                for row in tracking_incomplete:
                    if not any(r.get("alert_id") == row["alert_id"] for r in results):
                        results.append(dict(row))

        except Exception as e:
            logger.error("_get_alerts_for_analysis failed: %s", e)

        return results

    # ------------------------------------------------------------------
    # Single alert analysis
    # ------------------------------------------------------------------
    def _analyze_one_alert(self, alert: dict) -> Optional[dict]:
        """Analyse one alert: fetch current data, compute outcomes, store."""
        alert_id = alert.get("alert_id", "")
        token_addr = alert.get("token_address", "")
        chain = alert.get("chain", "robinhood")
        alert_price = float(alert.get("price_usd", 0) or 0)
        alert_liq = float(alert.get("liquidity_usd", 0) or 0)
        alert_holders = alert.get("holders")  # may be None
        alert_ts = int(alert.get("timestamp", 0) or 0)

        # Fetch current DexScreener data
        dex_data = self._fetch_dexscreener_data(token_addr, chain)
        if not dex_data:
            logger.debug("No DexScreener data for %s", token_addr[:10])
            return None

        current_price = dex_data.get("price_usd", 0)
        current_liq = dex_data.get("liquidity_usd", 0)
        current_vol = dex_data.get("volume_24h", 0)
        current_fdv = dex_data.get("fdv", 0)
        buy_ratio = dex_data.get("buy_sell_ratio", 0.5)

        if current_price <= 0:
            return None

        # Fetch Blockscout data (holders, safety)
        holder_count_current: Optional[int] = None
        rug_indicators: List[str] = []
        if self.blockscout and token_addr.startswith("0x") and len(token_addr) >= 42:
            try:
                holder_count_current = self.blockscout.get_token_holder_count(token_addr)
            except Exception:
                pass
            # Contract safety signals
            try:
                info = self.blockscout.get_token_info(token_addr)
                if info:
                    # Check for suspicious patterns
                    if info.get("is_smart_contract") and not info.get("is_verified"):
                        rug_indicators.append("unverified_contract")
                    holder_items = self.blockscout.get_token_holders(token_addr, limit=10)
                    if holder_items and len(holder_items) >= 3:
                        # Check holder concentration
                        total_supply = float(info.get("total_supply", 0) or 0)
                        if total_supply > 0:
                            top_pct = sum(
                                float(h.get("value", 0) or 0) for h in holder_items[:3]
                            ) / total_supply * 100
                            if top_pct > 50:
                                rug_indicators.append(f"top3_holders_{top_pct:.0f}pct")
            except Exception:
                pass

        # Compute outcome metrics
        price_change_pct = ((current_price - alert_price) / alert_price * 100) if alert_price > 0 else 0.0
        liquidity_change_pct = ((current_liq - alert_liq) / alert_liq * 100) if alert_liq > 0 else 0.0

        # Get max return / drawdown from price_tracking
        max_return_pct = price_change_pct
        max_drawdown_pct = 0.0
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT MAX(price_change_pct) as mx_ret, MIN(max_drawdown_pct) as mx_dd "
                    "FROM price_tracking WHERE alert_id = ?",
                    (alert_id,),
                ).fetchone()
                if row and row["mx_ret"] is not None:
                    max_return_pct = max(max_return_pct, float(row["mx_ret"]))
                if row and row["mx_dd"] is not None:
                    max_drawdown_pct = float(row["mx_dd"])
        except Exception:
            pass

        # Holder change
        holder_change_pct: Optional[float] = None
        if alert_holders is not None and holder_count_current is not None and alert_holders > 0:
            holder_change_pct = (holder_count_current - alert_holders) / alert_holders * 100

        # Volume trajectory
        volume_trajectory = self._compute_volume_trajectory(alert_id, alert_ts)

        # Rug pull score (0 = safe, 100 = definite rug)
        rug_score = self._compute_rug_score(
            current_liq, current_fdv, holder_change_pct, rug_indicators, price_change_pct
        )

        # Overall outcome classification
        overall = self._classify_outcome(price_change_pct, max_return_pct, max_drawdown_pct, rug_score)

        # Store in alert_analysis
        analysis = {
            "alert_id": alert_id,
            "analysis_timestamp": int(time.time()),
            "current_price_usd": current_price,
            "current_liquidity_usd": current_liq,
            "current_volume_24h": current_vol,
            "current_fdv": current_fdv,
            "price_change_pct": round(price_change_pct, 2),
            "max_return_pct": round(max_return_pct, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "liquidity_change_pct": round(liquidity_change_pct, 2),
            "holder_count_at_alert": alert_holders,
            "holder_count_current": holder_count_current,
            "holder_change_pct": round(holder_change_pct, 2) if holder_change_pct is not None else None,
            "volume_trajectory": volume_trajectory,
            "buy_sell_pressure": round(buy_ratio, 3) if buy_ratio else None,
            "rug_pull_score": rug_score,
            "rug_indicators": json.dumps(rug_indicators),
            "overall_outcome": overall,
        }

        self._store_analysis(analysis)

        # Attach alert metadata for report generation
        analysis["alert_type"] = alert.get("alert_type", "unknown")
        analysis["chain"] = chain
        analysis["wallet_tier"] = alert.get("wallet_tier", "")
        analysis["wallet_label"] = alert.get("wallet_label", "")
        analysis["alpha_score"] = alert.get("alpha_score", 0)
        analysis["liquidity_usd_at_alert"] = alert_liq

        return analysis

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------
    def _fetch_dexscreener_data(self, token_addr: str, chain: str) -> Optional[dict]:
        """Fetch current token data from DexScreener."""
        try:
            pair = self.dex.get_token(token_addr, chain=chain)
        except TypeError:
            pair = self.dex.get_token(token_addr)
        except Exception as e:
            logger.debug("DexScreener fetch failed for %s: %s", token_addr[:10], e)
            return None

        if not pair:
            return None

        price = float(pair.get("priceUsd", 0) or 0)
        liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
        vol = float((pair.get("volume") or {}).get("h24", 0) or 0)
        fdv = float(pair.get("fdv", 0) or 0)

        # Buy/sell pressure from 5m and 1h tx counts
        txns = pair.get("txns", {})
        m5 = txns.get("m5", {})
        h1 = txns.get("h1", {})
        m5_buys = int(m5.get("buys", 0) or 0)
        m5_sells = int(m5.get("sells", 0) or 0)
        h1_buys = int(h1.get("buys", 0) or 0)
        h1_sells = int(h1.get("sells", 0) or 0)
        total_buys = m5_buys + h1_buys
        total_sells = m5_sells + h1_sells
        buy_sell_ratio = total_buys / (total_buys + total_sells) if (total_buys + total_sells) > 0 else 0.5

        return {
            "price_usd": price,
            "liquidity_usd": liq,
            "volume_24h": vol,
            "fdv": fdv,
            "buy_sell_ratio": buy_sell_ratio,
        }

    # ------------------------------------------------------------------
    # Outcome computation helpers
    # ------------------------------------------------------------------
    def _compute_volume_trajectory(self, alert_id: str, alert_ts: int) -> str:
        """Classify volume trajectory as accelerating/decaying/stable."""
        try:
            with self._conn() as conn:
                checks = conn.execute(
                    "SELECT volume_24h, check_timestamp FROM price_tracking "
                    "WHERE alert_id = ? ORDER BY check_timestamp ASC",
                    (alert_id,),
                ).fetchall()

                if len(checks) < 2:
                    return "insufficient_data"

                volumes = [float(c["volume_24h"] or 0) for c in checks]
                # Compare first half average to second half average
                mid = len(volumes) // 2
                first_half_avg = sum(volumes[:mid]) / max(mid, 1)
                second_half_avg = sum(volumes[mid:]) / max(len(volumes) - mid, 1)

                if first_half_avg <= 0:
                    return "insufficient_data"

                ratio = second_half_avg / first_half_avg
                if ratio >= 1.5:
                    return "accelerating"
                elif ratio <= 0.5:
                    return "decaying"
                else:
                    return "stable"
        except Exception:
            return "unknown"

    def _compute_rug_score(
        self,
        liquidity: float,
        fdv: float,
        holder_change_pct: Optional[float],
        rug_indicators: List[str],
        price_change_pct: float,
    ) -> int:
        """Compute rug-pull risk score (0=safe, 100=definite rug)."""
        score = 0

        # Liq/MCap ratio deterioration
        if fdv > 0 and liquidity > 0:
            liq_mcap = liquidity / fdv
            if liq_mcap < 0.02:
                score += 30  # Very thin liquidity relative to cap
            elif liq_mcap < 0.05:
                score += 15
            elif liq_mcap < 0.10:
                score += 5

        # Holder concentration spike (holders leaving fast)
        if holder_change_pct is not None and holder_change_pct < -20:
            score += 20
        elif holder_change_pct is not None and holder_change_pct < -10:
            score += 10

        # Price crash
        if price_change_pct < -50:
            score += 25
        elif price_change_pct < -25:
            score += 15
        elif price_change_pct < -10:
            score += 5

        # Specific rug indicators from Blockscout
        for indicator in rug_indicators:
            if "unverified" in indicator:
                score += 10
            if "holders" in indicator and "pct" in indicator:
                score += 15

        return min(score, 100)

    def _classify_outcome(
        self,
        price_change_pct: float,
        max_return_pct: float,
        max_drawdown_pct: float,
        rug_score: int,
    ) -> str:
        """Classify overall outcome as positive/negative/neutral."""
        if rug_score >= 50:
            return "rug_pull"
        if price_change_pct > 10 or max_return_pct > 25:
            return "positive"
        if price_change_pct < -25 or max_drawdown_pct < -40:
            return "negative"
        if price_change_pct > 0:
            return "slightly_positive"
        if price_change_pct < -10:
            return "slightly_negative"
        return "neutral"

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------
    def _store_analysis(self, analysis: dict) -> None:
        """Insert or update analysis in alert_analysis table."""
        try:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO alert_analysis (
                        alert_id, analysis_timestamp,
                        current_price_usd, current_liquidity_usd,
                        current_volume_24h, current_fdv,
                        price_change_pct, max_return_pct, max_drawdown_pct,
                        liquidity_change_pct,
                        holder_count_at_alert, holder_count_current, holder_change_pct,
                        volume_trajectory, buy_sell_pressure,
                        rug_pull_score, rug_indicators, overall_outcome
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        analysis["alert_id"],
                        analysis["analysis_timestamp"],
                        analysis["current_price_usd"],
                        analysis["current_liquidity_usd"],
                        analysis["current_volume_24h"],
                        analysis["current_fdv"],
                        analysis["price_change_pct"],
                        analysis["max_return_pct"],
                        analysis["max_drawdown_pct"],
                        analysis["liquidity_change_pct"],
                        analysis["holder_count_at_alert"],
                        analysis["holder_count_current"],
                        analysis["holder_change_pct"],
                        analysis["volume_trajectory"],
                        analysis["buy_sell_pressure"],
                        analysis["rug_pull_score"],
                        analysis["rug_indicators"],
                        analysis["overall_outcome"],
                    ),
                )
        except Exception as e:
            logger.error("_store_analysis failed for %s: %s", analysis.get("alert_id"), e)

    # ------------------------------------------------------------------
    # Improvement report generation
    # ------------------------------------------------------------------
    def _generate_improvement_report(self, analyzed: List[dict]) -> dict:
        """Generate improvement report with edge analysis and recommendations."""
        now = int(time.time())

        # --- Aggregate by alert_type ---
        type_stats: Dict[str, dict] = {}
        for a in analyzed:
            key = a.get("alert_type", "unknown")
            if key not in type_stats:
                type_stats[key] = {"count": 0, "wins": 0, "returns": [], "drawdowns": []}
            s = type_stats[key]
            s["count"] += 1
            if a.get("price_change_pct", 0) > 0:
                s["wins"] += 1
            s["returns"].append(a.get("price_change_pct", 0))
            s["drawdowns"].append(a.get("max_drawdown_pct", 0))

        type_recommendations = []
        for t, s in type_stats.items():
            win_rate = s["wins"] / s["count"] if s["count"] > 0 else 0
            avg_return = sum(s["returns"]) / len(s["returns"]) if s["returns"] else 0
            if win_rate > 0.5 and avg_return > 0:
                edge = "positive"
                recommendation = "continue_alerting"
            elif win_rate < 0.25 or avg_return < -10:
                edge = "negative"
                recommendation = "increase_thresholds_or_stop"
            else:
                edge = "neutral"
                recommendation = "monitor"
            type_recommendations.append({
                "alert_type": t,
                "count": s["count"],
                "win_rate": round(win_rate, 3),
                "avg_return_pct": round(avg_return, 2),
                "edge": edge,
                "recommendation": recommendation,
            })

        # --- Aggregate by wallet_tier ---
        tier_stats: Dict[str, dict] = {}
        for a in analyzed:
            key = a.get("wallet_tier", "unknown")
            if not key:
                key = "unknown"
            if key not in tier_stats:
                tier_stats[key] = {"count": 0, "wins": 0, "returns": []}
            s = tier_stats[key]
            s["count"] += 1
            if a.get("price_change_pct", 0) > 0:
                s["wins"] += 1
            s["returns"].append(a.get("price_change_pct", 0))

        tier_recommendations = []
        for t, s in tier_stats.items():
            win_rate = s["wins"] / s["count"] if s["count"] > 0 else 0
            avg_return = sum(s["returns"]) / len(s["returns"]) if s["returns"] else 0
            tier_recommendations.append({
                "wallet_tier": t,
                "count": s["count"],
                "win_rate": round(win_rate, 3),
                "avg_return_pct": round(avg_return, 2),
                "edge": "positive" if (win_rate > 0.5 and avg_return > 0) else ("negative" if avg_return < -10 else "neutral"),
            })

        # --- Aggregate by chain ---
        chain_stats: Dict[str, dict] = {}
        for a in analyzed:
            key = a.get("chain", "unknown")
            if key not in chain_stats:
                chain_stats[key] = {"count": 0, "wins": 0, "returns": []}
            s = chain_stats[key]
            s["count"] += 1
            if a.get("price_change_pct", 0) > 0:
                s["wins"] += 1
            s["returns"].append(a.get("price_change_pct", 0))

        chain_recommendations = []
        for c, s in chain_stats.items():
            win_rate = s["wins"] / s["count"] if s["count"] > 0 else 0
            avg_return = sum(s["returns"]) / len(s["returns"]) if s["returns"] else 0
            chain_recommendations.append({
                "chain": c,
                "count": s["count"],
                "win_rate": round(win_rate, 3),
                "avg_return_pct": round(avg_return, 2),
            })

        # --- Specific threshold adjustment proposals ---
        threshold_proposals = self._generate_threshold_proposals(analyzed, type_stats)

        # --- New scoring factor proposals ---
        scoring_proposals = self._generate_scoring_proposals(analyzed)

        # --- Count edges ---
        positive_edge_count = sum(1 for r in type_recommendations if r["edge"] == "positive")
        negative_edge_count = sum(1 for r in type_recommendations if r["edge"] == "negative")

        report = {
            "timestamp": now,
            "interval_hours": self.interval_hours,
            "alerts_analyzed": len(analyzed),
            "positive_edge_count": positive_edge_count,
            "negative_edge_count": negative_edge_count,
            "type_recommendations": type_recommendations,
            "tier_recommendations": tier_recommendations,
            "chain_recommendations": chain_recommendations,
            "threshold_proposals": threshold_proposals,
            "scoring_proposals": scoring_proposals,
            "auto_apply_thresholds": self.auto_apply_thresholds,
            "summary": self._build_summary_text(
                len(analyzed), positive_edge_count, negative_edge_count,
                type_recommendations, tier_recommendations,
            ),
        }

        # Store report in DB
        self._store_report(report)

        return report

    def _generate_threshold_proposals(
        self, analyzed: List[dict], type_stats: Dict[str, dict]
    ) -> List[dict]:
        """Generate concrete threshold adjustment proposals based on outcomes."""
        proposals = []

        # --- Liquidity threshold by alert type ---
        for alert_type, s in type_stats.items():
            if s["count"] < 3:
                continue
            win_rate = s["wins"] / s["count"]
            avg_return = sum(s["returns"]) / len(s["returns"]) if s["returns"] else 0

            # If low win rate, recommend higher liquidity threshold
            if win_rate < 0.3 and alert_type in ("base_candidate", "new_token", "zombie_revival"):
                # Find the min liquidity of winning alerts
                winners_liq = [
                    a.get("liquidity_usd_at_alert", 0) for a in analyzed
                    if a.get("alert_type") == alert_type and a.get("price_change_pct", 0) > 0
                ]
                if winners_liq:
                    min_winner_liq = min(winners_liq)
                    if min_winner_liq > 10000:  # Only propose if meaningfully higher
                        proposals.append({
                            "parameter": "min_liquidity_usd",
                            "alert_type": alert_type,
                            "current_value": 10000,
                            "proposed_value": int(min_winner_liq),
                            "reason": f"win_rate={win_rate:.2f} for {alert_type}; winners had min_liq>={int(min_winner_liq)}",
                        })

            # If high win rate at current threshold, maybe can lower threshold to catch more
            if win_rate > 0.6 and avg_return > 5 and alert_type in ("smart_money", "runner_radar"):
                proposals.append({
                    "parameter": "min_alpha_score",
                    "alert_type": alert_type,
                    "current_value": 60,
                    "proposed_value": 50,
                    "reason": f"win_rate={win_rate:.2f} avg_return={avg_return:.1f}% — may tolerate lower alpha score",
                })

        # --- Rug-pull based proposals ---
        rug_alerts = [a for a in analyzed if a.get("overall_outcome") == "rug_pull"]
        if rug_alerts:
            avg_rug_liq = sum(a.get("current_liquidity_usd", 0) for a in rug_alerts) / len(rug_alerts)
            avg_rug_fdv = sum(a.get("current_fdv", 0) for a in rug_alerts) / len(rug_alerts)
            if avg_rug_fdv > 0 and avg_rug_liq / avg_rug_fdv < 0.05:
                proposals.append({
                    "parameter": "min_liquidity_mcap_ratio",
                    "alert_type": "all",
                    "current_value": 0.1,
                    "proposed_value": 0.15,
                    "reason": f"{len(rug_alerts)} rug-pulls had avg liq/mcap={avg_rug_liq/avg_rug_fdv:.3f}; raising threshold",
                })

        return proposals

    def _generate_scoring_proposals(self, analyzed: List[dict]) -> List[dict]:
        """Propose new scoring factors based on discovered patterns."""
        proposals = []

        # Volume trajectory edge
        accel = [a for a in analyzed if a.get("volume_trajectory") == "accelerating"]
        decaying = [a for a in analyzed if a.get("volume_trajectory") == "decaying"]
        if accel and decaying and len(accel) >= 3 and len(decaying) >= 3:
            accel_win = sum(1 for a in accel if a.get("price_change_pct", 0) > 0) / len(accel)
            decay_win = sum(1 for a in decaying if a.get("price_change_pct", 0) > 0) / len(decaying)
            if accel_win > decay_win + 0.15:
                proposals.append({
                    "factor": "volume_trajectory_acceleration",
                    "weight": 5,
                    "reason": f"Accelerating tokens win_rate={accel_win:.2f} vs decaying={decay_win:.2f}",
                })

        # Buy/sell pressure edge
        high_pressure = [a for a in analyzed if (a.get("buy_sell_pressure") or 0.5) >= 0.6]
        low_pressure = [a for a in analyzed if (a.get("buy_sell_pressure") or 0.5) <= 0.4]
        if high_pressure and low_pressure and len(high_pressure) >= 3 and len(low_pressure) >= 3:
            hp_win = sum(1 for a in high_pressure if a.get("price_change_pct", 0) > 0) / len(high_pressure)
            lp_win = sum(1 for a in low_pressure if a.get("price_change_pct", 0) > 0) / len(low_pressure)
            if hp_win > lp_win + 0.15:
                proposals.append({
                    "factor": "buy_sell_pressure",
                    "weight": 3,
                    "reason": f"High buy_pressure win_rate={hp_win:.2f} vs low={lp_win:.2f}",
                })

        # Holder growth edge
        holder_growing = [a for a in analyzed if (a.get("holder_change_pct") or 0) > 10]
        holder_shrinking = [a for a in analyzed if (a.get("holder_change_pct") or 0) < -10]
        if holder_growing and holder_shrinking and len(holder_growing) >= 3 and len(holder_shrinking) >= 3:
            hg_win = sum(1 for a in holder_growing if a.get("price_change_pct", 0) > 0) / len(holder_growing)
            hs_win = sum(1 for a in holder_shrinking if a.get("price_change_pct", 0) > 0) / len(holder_shrinking)
            if hg_win > hs_win + 0.15:
                proposals.append({
                    "factor": "holder_growth_positive",
                    "weight": 4,
                    "reason": f"Holder-growing tokens win_rate={hg_win:.2f} vs shrinking={hs_win:.2f}",
                })

        return proposals

    def _build_summary_text(
        self,
        total: int,
        positive: int,
        negative: int,
        type_recs: List[dict],
        tier_recs: List[dict],
    ) -> str:
        """Build human-readable summary."""
        lines = [f"Analyzer Report: {total} alerts analysed"]
        lines.append(f"Edge: {positive} positive, {negative} negative")

        for r in type_recs:
            if r["edge"] != "neutral":
                lines.append(f"  {r['alert_type']}: {r['edge']} edge (win_rate={r['win_rate']:.2f}, avg_ret={r['avg_return_pct']:.1f}%)")

        for r in tier_recs:
            if r["count"] >= 3:
                lines.append(f"  {r['wallet_tier']}: win_rate={r['win_rate']:.2f}, avg_ret={r['avg_return_pct']:.1f}%")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _store_report(self, report: dict) -> None:
        """Store improvement report in DB."""
        try:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT INTO improvement_reports (
                        timestamp, report_json, summary_text,
                        alerts_analyzed, positive_edge_count, negative_edge_count
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report["timestamp"],
                        json.dumps(report, default=str),
                        report.get("summary", ""),
                        report.get("alerts_analyzed", 0),
                        report.get("positive_edge_count", 0),
                        report.get("negative_edge_count", 0),
                    ),
                )
        except Exception as e:
            logger.error("_store_report failed: %s", e)

    def _save_report_to_disk(self, report: dict) -> None:
        """Save latest report as JSON for easy external access."""
        try:
            os.makedirs(os.path.dirname(self.report_path) or ".", exist_ok=True)
            with open(self.report_path, "w") as f:
                json.dump(report, f, indent=2, default=str)
        except Exception as e:
            logger.error("_save_report_to_disk failed: %s", e)

    def _load_last_report(self) -> None:
        """Load last report from disk (for service restart resilience)."""
        try:
            if os.path.exists(self.report_path):
                with open(self.report_path) as f:
                    self.last_report = json.load(f)
        except Exception:
            pass

    def _write_scorer_adjustments(self, report: dict) -> None:
        """Write scorer_adjustments.json that scanner modules can load.

        When auto_apply_thresholds=False this file is informational only;
        actual threshold changes require manual review."""
        adjustments = {
            "generated_at": int(time.time()),
            "auto_apply": self.auto_apply_thresholds,
            "alert_type_adjustments": {},
            "wallet_tier_adjustments": {},
            "chain_adjustments": {},
            "new_scoring_factors": report.get("scoring_proposals", []),
        }

        for prop in report.get("threshold_proposals", []):
            alert_type = prop.get("alert_type", "all")
            param = prop.get("parameter", "")
            adjustments["alert_type_adjustments"].setdefault(alert_type, {})[param] = {
                "original": prop.get("current_value"),
                "recommended": prop.get("proposed_value"),
                "reason": prop.get("reason", ""),
            }

        for rec in report.get("tier_recommendations", []):
            tier = rec.get("wallet_tier", "")
            if rec.get("edge") == "negative":
                adjustments["wallet_tier_adjustments"][tier] = {
                    "action": "increase_thresholds",
                    "win_rate": rec.get("win_rate"),
                    "avg_return_pct": rec.get("avg_return_pct"),
                }
            elif rec.get("edge") == "positive":
                adjustments["wallet_tier_adjustments"][tier] = {
                    "action": "maintain_or_lower_thresholds",
                    "win_rate": rec.get("win_rate"),
                    "avg_return_pct": rec.get("avg_return_pct"),
                }

        for rec in report.get("chain_recommendations", []):
            chain = rec.get("chain", "")
            adjustments["chain_adjustments"][chain] = {
                "win_rate": rec.get("win_rate"),
                "avg_return_pct": rec.get("avg_return_pct"),
                "count": rec.get("count"),
            }

        try:
            os.makedirs(os.path.dirname(self.scorer_adjustments_path) or ".", exist_ok=True)
            with open(self.scorer_adjustments_path, "w") as f:
                json.dump(adjustments, f, indent=2, default=str)
        except Exception as e:
            logger.error("_write_scorer_adjustments failed: %s", e)

    # ------------------------------------------------------------------
    # API endpoints support
    # ------------------------------------------------------------------
    def get_report(self) -> dict:
        """Return latest improvement report (for /analyzer/report)."""
        if self.last_report:
            return self.last_report
        # Try loading from DB
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT report_json FROM improvement_reports ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
                if row:
                    return json.loads(row["report_json"])
        except Exception:
            pass
        return {"enabled": self.enabled, "reports_generated": 0, "message": "no reports yet"}

    def get_stats(self) -> dict:
        """Return analyzer statistics (for /analyzer/stats)."""
        stats = {
            "enabled": self.enabled,
            "interval_hours": self.interval_hours,
            "interval_seconds": self.interval_seconds,
            "min_alerts_for_analysis": self.min_alerts_for_analysis,
            "auto_apply_thresholds": self.auto_apply_thresholds,
            "total_runs": self.total_runs,
            "total_alerts_analyzed": self.total_alerts_analyzed,
            "total_reports_generated": self.total_reports_generated,
            "last_run_time": self.last_run_time,
            "last_run_ago_seconds": int(time.time() - self.last_run_time) if self.last_run_time else None,
        }

        # Add DB-level stats
        try:
            with self._conn() as conn:
                analysed_count = conn.execute(
                    "SELECT COUNT(*) as c FROM alert_analysis"
                ).fetchone()["c"]
                report_count = conn.execute(
                    "SELECT COUNT(*) as c FROM improvement_reports"
                ).fetchone()["c"]
                outcome_dist = conn.execute(
                    "SELECT overall_outcome, COUNT(*) as c FROM alert_analysis GROUP BY overall_outcome"
                ).fetchall()
                recent_analysis = conn.execute(
                    "SELECT COUNT(*) as c FROM alert_analysis WHERE analysis_timestamp > ?",
                    (int(time.time()) - 86400,),
                ).fetchone()["c"]

                stats["db_analysis_count"] = analysed_count
                stats["db_report_count"] = report_count
                stats["outcome_distribution"] = {r["overall_outcome"]: r["c"] for r in outcome_dist}
                stats["analysis_last_24h"] = recent_analysis
        except Exception as e:
            stats["db_error"] = str(e)

        return stats

    def should_run(self) -> bool:
        """Check if enough time has passed since last run."""
        if not self.enabled:
            return False
        return (time.time() - self.last_run_time) >= self.interval_seconds
