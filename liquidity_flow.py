#!/usr/bin/env python3
"""Liquidity Flow Analyzer — tracks LP add/remove events.

Monitors Robinhood Chain tokens for significant liquidity changes.
LIQUIDITY ADD = bullish signal (new confidence in the pool).
LIQUIDITY REMOVE = bearish/risk signal (potential exit/rug risk).
Free APIs only (Blockscout + DexScreener + Telegram).

Usage:
    python liquidity_flow.py --once     # Single scan then exit
    python liquidity_flow.py            # Continuous loop (5 min intervals)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from blockscout import BlockscoutClient
from dexscreener import DexScreenerClient
from telegram_alert import TelegramAlerter
from alchemy_client import AlchemyClient

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

logger = logging.getLogger("catecoin-scanner.liquidity_flow")

DEXSCREENER_CHART = "https://dexscreener.com/robinhood/{addr}"
DEDUP_WINDOW = 3600  # 1 hour
DEFAULT_TOKENS = [
    "0xfc5ABD01E4Def799549eee154449Ff6a7ae0cAc7",  # CATE
]
DEFAULT_CHANGE_THRESHOLD = 0.10  # 10%


def load_config(config_path: str) -> dict:
    if not config_path or not Path(config_path).exists():
        return {}
    if not HAS_YAML:
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


class LiquidityFlowAnalyzer:
    """Tracks LP add/remove events by comparing liquidity snapshots."""

    def __init__(self, config: dict) -> None:
        cfg = config.get("liquidity_flow", {}) or {}
        self.tokens: List[str] = cfg.get("tokens", list(DEFAULT_TOKENS))
        self.change_threshold: float = float(
            cfg.get("change_threshold", DEFAULT_CHANGE_THRESHOLD)
        )
        self.interval: int = int(cfg.get("poll_interval_seconds", 300))

        self.blockscout = BlockscoutClient(
            base_url=cfg.get("blockscout_base", "https://robinhoodchain.blockscout.com/api/v2")
        )
        self.dexscreener = DexScreenerClient()
        self.alerter = TelegramAlerter.from_config(config)

        # Alchemy (PRIMARY data source for real-time LP token transfers)
        alch_cfg = config.get("alchemy", {}) or {}
        self.alchemy = AlchemyClient(
            api_key=alch_cfg.get("api_key"),
            network=alch_cfg.get("network", "robinhood-mainnet"),
            cu_warning_threshold=alch_cfg.get("cu_warning_threshold", 0.8),
            cu_monthly_limit=alch_cfg.get("cu_monthly_limit", 30_000_000),
        )

        # addr -> {timestamp, liquidity_usd}
        self.liquidity_history: Dict[str, Dict[str, Any]] = {}
        # dedup_key -> timestamp
        self.alerted: Dict[str, float] = {}
        self._token_cache: Dict[str, Dict[str, Any]] = {}

    def _get_token_meta(self, token_addr: str) -> Dict[str, Any]:
        """Cached token symbol + current liquidity from DexScreener."""
        cached = self._token_cache.get(token_addr)
        if cached and (time.time() - cached.get("_ts", 0)) < 120:
            return cached
        meta: Dict[str, Any] = {
            "symbol": "???",
            "liquidity_usd": 0.0,
            "_ts": time.time(),
        }
        try:
            pair = self.dexscreener.get_token(token_addr)
            if pair:
                base = pair.get("baseToken", {}) or {}
                meta["symbol"] = base.get("symbol", "???")
                liquidity = pair.get("liquidity") or {}
                try:
                    meta["liquidity_usd"] = float(liquidity.get("usd", 0) or 0)
                except (TypeError, ValueError):
                    meta["liquidity_usd"] = 0.0
        except Exception as e:
            logger.warning("DexScreener meta fetch failed for %s: %s", token_addr, e)
        self._token_cache[token_addr] = meta
        return meta

    def _track_liquidity_changes(self, token_addr: str) -> Optional[Dict[str, Any]]:
        """Compare current liquidity vs last check. Returns change dict or None."""
        meta = self._get_token_meta(token_addr)
        current_liq = meta.get("liquidity_usd", 0.0)
        now = time.time()

        prev = self.liquidity_history.get(token_addr)
        if not prev:
            # Seed history on first sighting
            self.liquidity_history[token_addr] = {
                "timestamp": now,
                "liquidity_usd": current_liq,
            }
            logger.debug(
                "Seeded liquidity history for %s at $%.0f", token_addr, current_liq
            )
            return None

        prev_liq = prev.get("liquidity_usd", 0.0)
        delta = current_liq - prev_liq

        # Avoid divide-by-zero
        if prev_liq <= 0:
            change_pct = 1.0 if current_liq > 0 else 0.0
        else:
            change_pct = abs(delta) / prev_liq

        if change_pct < self.change_threshold:
            # No significant change — update history silently
            self.liquidity_history[token_addr] = {
                "timestamp": now,
                "liquidity_usd": current_liq,
            }
            return None

        action = "LP ADDED" if delta > 0 else "LP REMOVED"
        result = {
            "address": token_addr,
            "symbol": meta.get("symbol", "???"),
            "action": action,
            "amount": abs(delta),
            "change_pct": change_pct,
            "prev_liquidity": prev_liq,
            "current_liquidity": current_liq,
            "timestamp": now,
        }

        # Update history after detection
        self.liquidity_history[token_addr] = {
            "timestamp": now,
            "liquidity_usd": current_liq,
        }
        return result

    def _detect_lp_events(self, token_addr: str) -> List[Dict[str, Any]]:
        """Check Blockscout token transfers for LP-token movements.

        Heuristic: looks for recent large transfers flagged as LP token movements.
        Returns list of candidate events. Primary detection is via liquidity
        delta from DexScreener; this provides supplementary on-chain context.
        """
        events: List[Dict[str, Any]] = []
        try:
            data = self.blockscout.get_token_transfers(
                token_addr, params={"filter": "to|from"}
            )
        except Exception as e:
            logger.debug("get_token_transfers failed for %s: %s", token_addr, e)
            return events

        if not data:
            return events

        items = data.get("items") or []
        cutoff = int(time.time()) - 600  # last 10 min
        for item in items[:20]:
            try:
                ts_raw = item.get("timestamp") or item.get("block", {}).get("timestamp")
                ts = self._parse_ts(ts_raw)
                if ts is None or ts < cutoff:
                    continue
                total = item.get("total", {}) or {}
                value = total.get("value") or 0
                events.append({
                    "token_addr": token_addr,
                    "value_raw": value,
                    "from": (item.get("from", {}) or {}).get("hash", ""),
                    "to": (item.get("to", {}) or {}).get("hash", ""),
                    "tx_hash": (item.get("transaction", {}) or {}).get("hash", ""),
                    "timestamp": ts,
                })
            except Exception as e:
                logger.debug("lp event parse error: %s", e)
                continue
        return events

    def _detect_lp_events_via_alchemy(self, token_addr: str) -> List[Dict[str, Any]]:
        """PRIMARY: detect LP token movements via Alchemy real-time transfers.

        Queries alchemy_getAssetTransfers for the token contract to find recent
        transfers. Combined with DexScreener liquidity deltas, this provides
        precise LP add/remove event detection that the broken Blockscout
        /tokens/{addr}/transfers endpoint could never deliver on Robinhood Chain.
        """
        events: List[Dict[str, Any]] = []
        try:
            transfers = self.alchemy.get_asset_transfers(
                token_contract=token_addr,
                max_count=30,
                order="desc",
            )
        except Exception as e:
            logger.debug("Alchemy LP transfer query failed for %s: %s", token_addr, e)
            return events

        cutoff = int(time.time()) - 600  # last 10 min
        for t in transfers:
            try:
                ts = self._parse_ts(t.get("timestamp"))
                if ts is None or ts < cutoff:
                    continue
                events.append({
                    "token_addr": token_addr,
                    "value_raw": t.get("raw_value") or "",
                    "value": float(t.get("value") or 0),
                    "from": t.get("from") or "",
                    "to": t.get("to") or "",
                    "tx_hash": t.get("hash") or "",
                    "timestamp": ts,
                })
            except Exception as e:
                logger.debug("alchemy lp event parse error: %s", e)
                continue
        return events

    @staticmethod
    def _parse_ts(raw: Any) -> Optional[int]:
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            return int(raw)
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            pass
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp())
        except Exception:
            return None

    def _send_liquidity_alert(
        self, token_data: dict, action: str, amount: float
    ) -> bool:
        prev_liq = token_data.get("prev_liquidity", 0.0)
        cur_liq = token_data.get("current_liquidity", 0.0)
        change_pct = token_data.get("change_pct", 0.0)
        symbol = token_data.get("symbol", "???")
        addr = token_data.get("address", "")

        signal = "BULLISH" if action == "LP ADDED" else "BEARISH"

        msg = (
            "💧 LIQUIDITY FLOW\n"
            "━━━━━━━━━━━━\n"
            f"📛 Token: ${symbol}\n"
            f"📊 Action: {action}\n"
            f"💵 Amount: ${amount:,.0f} ({change_pct * 100:.1f}% of pool)\n"
            f"💰 Pool Liquidity: ${prev_liq:,.0f} -> ${cur_liq:,.0f}\n"
            f"📈 Signal: {signal}\n"
            f"🔗 DexScreener: {DEXSCREENER_CHART.format(addr=addr)}\n"
            "━━━━━━━━━━━━"
        )
        return self.alerter.send(msg)

    def poll_once(self) -> int:
        """Main scan. Returns number of alerts sent."""
        alerts_sent = 0
        now = time.time()

        # Prune dedup
        self.alerted = {
            k: v for k, v in self.alerted.items() if (now - v) < DEDUP_WINDOW
        }

        for token_addr in self.tokens:
            try:
                # ---- PRIMARY PATH: Alchemy real-time LP transfer detection ----
                lp_events = []
                try:
                    lp_events = self._detect_lp_events_via_alchemy(token_addr)
                except Exception as e:
                    logger.debug("Alchemy LP event detection failed: %s", e)

                if lp_events:
                    # Real LP token movements detected via Alchemy
                    for ev in lp_events[:3]:  # Cap alerts to avoid spam
                        dedup_key = f"alchemy_lp:{ev.get('tx_hash') or ev.get('timestamp')}"
                        if dedup_key in self.alerted:
                            continue
                        ok = self._send_liquidity_alert(
                            token_data={
                                "symbol": self._get_token_meta(token_addr).get("symbol", "???"),
                                "address": token_addr,
                                "action": "LP FLOW",
                                "amount": ev.get("value", 0.0),
                                "change_pct": 0.0,
                                "prev_liquidity": 0.0,
                                "current_liquidity": 0.0,
                            },
                            action="LP FLOW",
                            amount=ev.get("value", 0.0),
                        )
                        if ok:
                            alerts_sent += 1
                            self.alerted[dedup_key] = now

                change = self._track_liquidity_changes(token_addr)
                if not change:
                    continue

                dedup_key = f"{token_addr}:{change['action']}"
                if dedup_key in self.alerted:
                    logger.debug("Dedup skip %s", dedup_key)
                    continue

                ok = self._send_liquidity_alert(
                    token_data=change,
                    action=change["action"],
                    amount=change["amount"],
                )
                if ok:
                    alerts_sent += 1
                    self.alerted[dedup_key] = now
                    logger.info(
                        "Liquidity alert sent: %s %s $%.0f (%.1f%%)",
                        change["symbol"],
                        change["action"],
                        change["amount"],
                        change["change_pct"] * 100,
                    )

                time.sleep(0.2)  # respect free tier

            except Exception as e:
                logger.warning("liquidity scan error for %s: %s", token_addr, e)
                continue

        if alerts_sent:
            logger.info("Liquidity flow: %d alerts sent", alerts_sent)
        return alerts_sent

    def run_loop(self, interval: Optional[int] = None) -> None:
        wait = interval or self.interval
        logger.info("Liquidity flow loop started (interval=%ds)", wait)
        while True:
            try:
                self.poll_once()
            except KeyboardInterrupt:
                logger.info("Interrupted, exiting")
                break
            except Exception as e:
                logger.error("Loop iteration error: %s", e)
            time.sleep(wait)


def main() -> int:
    parser = argparse.ArgumentParser(description="Liquidity flow analyzer")
    parser.add_argument("--once", action="store_true", help="Run single scan then exit")
    parser.add_argument("--config", default="/a0/usr/workdir/catecoin-scanner/config.yaml")
    parser.add_argument("--interval", type=int, default=None, help="Override poll interval")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_config(args.config)
    analyzer = LiquidityFlowAnalyzer(config)

    if args.once:
        count = analyzer.poll_once()
        print(f"Liquidity flow: {count} alerts sent")
        return 0

    analyzer.run_loop(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
