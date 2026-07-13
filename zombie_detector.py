#!/usr/bin/env python3
"""Zombie Token Detector — finds dormant tokens with sudden volume spikes.

Scans Robinhood Chain tokens via DexScreener and detects "zombie revivals":
tokens that existed 7+ days, had low average volume (<$1K), but now show a
3x+ volume spike. Free APIs only (DexScreener + Blockscout + Telegram).

Usage:
    python zombie_detector.py --once     # Single scan then exit
    python zombie_detector.py            # Continuous loop (10 min intervals)
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

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

logger = logging.getLogger("catecoin-scanner.zombie_detector")

DEXSCREENER_CHART = "https://dexscreener.com/robinhood/{addr}"
DEDUP_WINDOW = 86400  # 24 hours
DEFAULT_SEARCH_QUERY = "robinhood"


def load_config(config_path: str) -> dict:
    if not config_path or not Path(config_path).exists():
        return {}
    if not HAS_YAML:
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


class ZombieDetector:
    """Detects dormant tokens experiencing sudden volume spikes (zombie revivals)."""

    def __init__(self, config: dict) -> None:
        cfg = config.get("zombie_detector", {}) or {}
        self.search_query: str = cfg.get("search_query", DEFAULT_SEARCH_QUERY)
        self.dormancy_days: int = int(cfg.get("dormancy_days", 7))
        self.dormant_volume_threshold: float = float(
            cfg.get("dormant_volume_threshold", 1000)
        )
        self.spike_multiplier: float = float(cfg.get("spike_multiplier", 3.0))
        self.min_current_volume: float = float(cfg.get("min_current_volume", 5000))
        self.max_tokens_per_scan: int = int(cfg.get("max_tokens_per_scan", 50))
        self.interval: int = int(cfg.get("poll_interval_seconds", 600))

        self.blockscout = BlockscoutClient(
            base_url=cfg.get("blockscout_base", "https://robinhoodchain.blockscout.com/api/v2")
        )
        self.dexscreener = DexScreenerClient()
        self.alerter = TelegramAlerter.from_config(config)

        self.token_history: Dict[str, Dict[str, Any]] = {}  # addr -> history snapshot
        self.alerted: Dict[str, float] = {}  # addr -> timestamp

    def _pair_to_token(self, pair: dict) -> Optional[Dict[str, Any]]:
        """Normalize a DexScreener pair dict to our token record."""
        try:
            base = pair.get("baseToken", {}) or {}
            addr = base.get("address", "")
            if not addr:
                return None
            symbol = base.get("symbol", "???")
            liquidity = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
            volume_24h = float((pair.get("volume") or {}).get("h24", 0) or 0)
            volume_6h = float((pair.get("volume") or {}).get("h6", 0) or 0)
            volume_1h = float((pair.get("volume") or {}).get("h1", 0) or 0)
            pair_created_raw = pair.get("pairCreatedAt") or pair.get("createdAt")
            created_ts = self._parse_iso_ts(pair_created_raw)
            fdv = float(pair.get("fdv") or 0)
            return {
                "address": addr,
                "symbol": symbol,
                "liquidity_usd": liquidity,
                "volume_24h": volume_24h,
                "volume_6h": volume_6h,
                "volume_1h": volume_1h,
                "pair_created_ts": created_ts,
                "fdv": fdv,
                "pair_address": pair.get("pairAddress", ""),
            }
        except Exception as e:
            logger.debug("pair_to_token parse error: %s", e)
            return None

    @staticmethod
    def _parse_iso_ts(raw: Any) -> Optional[float]:
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            return float(raw)
        try:
            from datetime import datetime
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    def _get_dormant_tokens(self) -> List[Dict[str, Any]]:
        """Query DexScreener for Robinhood tokens, filter to dormant candidates."""
        candidates: List[Dict[str, Any]] = []
        now = time.time()
        min_age_seconds = self.dormancy_days * 86400

        try:
            pairs = self.dexscreener.search(self.search_query)
        except Exception as e:
            logger.warning("DexScreener search failed: %s", e)
            return []

        if not pairs:
            logger.debug("No pairs returned for query '%s'", self.search_query)
            return []

        for pair in pairs[: self.max_tokens_per_scan]:
            token = self._pair_to_token(pair)
            if not token:
                continue

            # Must be 7+ days old
            created = token.get("pair_created_ts")
            if not created or (now - created) < min_age_seconds:
                continue

            # Compute rolling average volume from history if available
            hist = self.token_history.get(token["address"], {})
            prev_volume = hist.get("volume_24h")
            if prev_volume is not None:
                token["prev_volume_24h"] = prev_volume

            candidates.append(token)
            time.sleep(0.1)  # gentle pacing

        return candidates

    def _check_volume_spike(self, token: dict) -> Optional[Dict[str, Any]]:
        """Compare current volume vs historical baseline. Returns spike info or None."""
        addr = token.get("address")
        now = time.time()
        current_vol = token.get("volume_24h", 0) or 0

        # Determine baseline volume
        hist = self.token_history.get(addr, {})
        avg_volume = hist.get("avg_volume")
        first_seen = hist.get("first_seen_ts", now)
        observations = hist.get("observations", [])

        if avg_volume is None:
            # Seed history on first sighting — need at least one baseline before
            # we can declare a spike. Use current as initial baseline.
            observations.append({"ts": now, "volume": current_vol})
            avg_volume = current_vol
            self.token_history[addr] = {
                "first_seen_ts": now,
                "last_checked": now,
                "avg_volume": avg_volume,
                "volume_24h": current_vol,
                "observations": observations,
            }
            logger.debug("Seeded history for %s avg_volume=$%.0f", addr, avg_volume)
            return None

        # Update rolling observations (cap to last 20 to bound memory)
        observations.append({"ts": now, "volume": current_vol})
        if len(observations) > 20:
            observations = observations[-20:]

        # Recompute average from historical observations (excluding current)
        historical = [o["volume"] for o in observations[:-1]]
        new_avg = (
            sum(historical) / len(historical) if historical else avg_volume
        )

        # Persist updated history
        self.token_history[addr] = {
            "first_seen_ts": first_seen,
            "last_checked": now,
            "avg_volume": new_avg,
            "volume_24h": current_vol,
            "observations": observations,
        }

        # Qualify as zombie revival
        if new_avg >= self.dormant_volume_threshold:
            return None
        if current_vol < self.min_current_volume:
            return None
        if new_avg <= 0:
            # brand new activity from dead token — treat as spike
            spike_pct = 100.0
        else:
            spike_multiplier = current_vol / new_avg
            if spike_multiplier < self.spike_multiplier:
                return None
            spike_pct = ((current_vol - new_avg) / new_avg) * 100.0

        dormancy_days = int((now - first_seen) / 86400)
        return {
            "address": addr,
            "symbol": token.get("symbol", "???"),
            "dormancy_days": max(dormancy_days, self.dormancy_days),
            "spike_pct": spike_pct,
            "current_volume": current_vol,
            "avg_volume": new_avg,
            "liquidity_usd": token.get("liquidity_usd", 0) or 0,
            "fdv": token.get("fdv", 0) or 0,
            "market_cap": token.get("fdv", 0) or 0,
            "smart_money_buying": False,
            "holders": 0,
        }

    def _send_zombie_alert(self, token_data: dict) -> bool:
        return self.alerter.send_zombie_alert(
            symbol=token_data['symbol'],
            contract=token_data['address'],
            dormancy_days=token_data['dormancy_days'],
            volume_spike_pct=token_data['spike_pct'],
            current_volume=token_data['current_volume'],
            liquidity=token_data['liquidity_usd'],
            smart_money_buying=token_data.get('smart_money_buying', False),
            market_cap=token_data.get('fdv', 0) or token_data.get('market_cap', 0),
            holders=token_data.get('holders', 0),
        )

    def poll_once(self) -> int:
        """Main scan. Returns number of alerts sent."""
        alerts_sent = 0
        now = time.time()

        # Prune dedup
        self.alerted = {
            k: v for k, v in self.alerted.items() if (now - v) < DEDUP_WINDOW
        }

        try:
            tokens = self._get_dormant_tokens()
        except Exception as e:
            logger.warning("_get_dormant_tokens failed: %s", e)
            return 0

        if not tokens:
            logger.debug("No dormant token candidates this scan")
            return 0

        for token in tokens:
            addr = token.get("address")
            if not addr:
                continue
            try:
                spike = self._check_volume_spike(token)
                if not spike:
                    continue

                if addr in self.alerted:
                    logger.debug("Dedup skip zombie %s", addr)
                    continue

                ok = self._send_zombie_alert(spike)
                if ok:
                    alerts_sent += 1
                    self.alerted[addr] = now
                    logger.info(
                        "Zombie revival alert: %s +%.0f%% vol=$%.0f",
                        spike["symbol"], spike["spike_pct"], spike["current_volume"],
                    )
                time.sleep(0.2)
            except Exception as e:
                logger.warning("zombie check error for %s: %s", addr, e)
                continue

        if alerts_sent:
            logger.info("Zombie detector: %d alerts sent", alerts_sent)
        return alerts_sent

    def run_loop(self, interval: Optional[int] = None) -> None:
        wait = interval or self.interval
        logger.info("Zombie detector loop started (interval=%ds)", wait)
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
    parser = argparse.ArgumentParser(description="Zombie token detector")
    parser.add_argument("--once", action="store_true", help="Run single scan then exit")
    parser.add_argument("--config", default="/a0/usr/workdir/catecoin-scanner/config.yaml")
    parser.add_argument("--interval", type=int, default=None, help="Override poll interval")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_config(args.config)
    detector = ZombieDetector(config)

    if args.once:
        count = detector.poll_once()
        print(f"Zombie detector: {count} alerts sent")
        return 0

    detector.run_loop(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
