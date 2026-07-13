#!/usr/bin/env python3
"""Reversal Detector — finds downtrending tokens with smart money re-entry.

Detects potential reversal opportunities by:
1. Querying DexScreener for tokens down >20% from recent highs
2. Cross-referencing with Blockscout: are tracked wallets buying these?
3. Detecting volume spikes on downtrending tokens (potential reversal)
4. Sending reversal alerts with thesis and risk assessment

Free APIs only (DexScreener + Blockscout + Telegram).

Usage:
    python reversal_detector.py --once     # Single scan then exit
    python reversal_detector.py            # Continuous loop (15 min intervals)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from blockscout import BlockscoutClient
from dexscreener import DexScreenerClient
from telegram_alert import TelegramAlerter

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

logger = logging.getLogger("catecoin-scanner.reversal_detector")

DEXSCREENER_CHART = "https://dexscreener.com/robinhood/{addr}"
DEDUP_WINDOW = 3600  # 1 hour dedup


def load_config(config_path: str) -> dict:
    if not config_path or not Path(config_path).exists():
        return {}
    if not HAS_YAML:
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


class ReversalDetector:
    """Detects downtrending tokens with smart money re-entry or volume spikes."""

    def __init__(self, config: dict) -> None:
        cfg = config.get("reversal", {}) or {}
        self.enabled: bool = cfg.get("enabled", True)
        self.min_drop_pct: float = float(cfg.get("min_drop_pct", 20.0))
        self.volume_spike_mult: float = float(cfg.get("volume_spike_mult", 3.0))
        self.check_smart_money: bool = cfg.get("check_smart_money", True)
        self.interval: int = int(cfg.get("poll_interval_seconds", 900))
        self.search_query: str = cfg.get("search_query", "robinhood")
        self.max_tokens: int = int(cfg.get("max_tokens_per_scan", 50))
        self.min_liquidity: float = float(cfg.get("min_liquidity_usd", 2000))

        self.dexscreener = DexScreenerClient()
        self.alerter = TelegramAlerter.from_config(config)
        self.blockscout = BlockscoutClient(
            base_url=cfg.get("blockscout_base", "https://robinhoodchain.blockscout.com/api/v2")
        )

        # Load tracked wallets for smart money cross-reference
        self.tracked_wallets: Set[str] = set()
        wallets_file = config.get("smart_money", {}).get("wallets_file", "smart_wallets.json")
        base_dir = Path(__file__).parent
        wallets_path = str(base_dir / wallets_file) if not Path(wallets_file).is_absolute() else wallets_file
        try:
            with open(wallets_path) as f:
                data = json.load(f)
                for w in data.get("wallets", []):
                    addr = (w.get("address") or "").lower()
                    if addr:
                        self.tracked_wallets.add(addr)
            logger.info("Reversal detector: %d tracked wallets loaded", len(self.tracked_wallets))
        except Exception as e:
            logger.warning("Could not load wallets for smart money check: %s", e)

        self.alerted: Dict[str, float] = {}  # addr -> timestamp

    def _pair_to_token(self, pair: dict) -> Optional[Dict[str, Any]]:
        """Normalize a DexScreener pair dict."""
        try:
            base = pair.get("baseToken", {}) or {}
            addr = base.get("address", "")
            if not addr:
                return None

            price_change = pair.get("priceChange", {}) or {}
            change_24h = float(price_change.get("h24", 0) or 0)
            change_6h = float(price_change.get("h6", 0) or 0)

            volume = pair.get("volume", {}) or {}
            vol_24h = float(volume.get("h24", 0) or 0)
            vol_6h = float(volume.get("h6", 0) or 0)
            vol_1h = float(volume.get("h1", 0) or 0)

            liquidity = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
            fdv = float(pair.get("fdv") or 0)
            price = float(pair.get("priceUsd") or 0)

            return {
                "address": addr,
                "symbol": base.get("symbol", "???"),
                "name": base.get("name", "Unknown"),
                "price": price,
                "liquidity_usd": liquidity,
                "fdv": fdv,
                "market_cap": fdv,
                "volume_24h": vol_24h,
                "volume_6h": vol_6h,
                "volume_1h": vol_1h,
                "change_24h": change_24h,
                "change_6h": change_6h,
                "pair_address": pair.get("pairAddress", ""),
            }
        except Exception as e:
            logger.debug("pair_to_token error: %s", e)
            return None

    def _is_downtrending(self, token: dict) -> tuple:
        """Check if token is downtrending. Returns (is_down, drop_pct)."""
        change_24h = token.get("change_24h", 0)
        change_6h = token.get("change_6h", 0)
        worst_change = min(change_24h, change_6h)

        if worst_change <= -self.min_drop_pct:
            return True, abs(worst_change)
        return False, 0.0

    def _check_volume_spike(self, token: dict) -> Optional[float]:
        """Check for volume spike on downtrending token. Returns multiplier or None."""
        vol_24h = token.get("volume_24h", 0)
        vol_1h = token.get("volume_1h", 0)

        if vol_24h <= 0:
            return None

        avg_hourly = vol_24h / 24
        if avg_hourly <= 0:
            return None

        spike_mult = vol_1h / avg_hourly
        if spike_mult >= self.volume_spike_mult:
            return spike_mult
        return None

    def _check_smart_money_buying(self, token_addr: str) -> int:
        """Check if tracked wallets hold this token. Returns count."""
        if not self.check_smart_money or not self.tracked_wallets:
            return 0

        count = 0
        try:
            holders = self.blockscout.get_token_holders(token_addr, limit=50)
            if not holders:
                return 0
            for holder in holders:
                holder_addr = (holder.get("address") or {}).get("hash", "").lower()
                if holder_addr in self.tracked_wallets:
                    count += 1
        except Exception as e:
            logger.debug("Smart money check failed for %s: %s", token_addr[:10], e)
        return count

    def _build_thesis(self, token: dict, drop_pct: float, vol_spike: Optional[float], sm_count: int) -> str:
        parts = []
        if sm_count > 0:
            parts.append(f"{sm_count} elite wallet(s) accumulating after -{drop_pct:.0f}% drop")
        if vol_spike and vol_spike > 0:
            parts.append(f"volume spike {vol_spike:.1f}x hourly average")
        if not parts:
            parts.append(f"token down -{drop_pct:.0f}% showing unusual activity")
        return ". ".join(parts) + " — potential reversal setup"

    def poll_once(self) -> int:
        """Main scan. Returns number of alerts sent."""
        if not self.enabled:
            return 0

        alerts_sent = 0
        now = time.time()

        self.alerted = {k: v for k, v in self.alerted.items() if (now - v) < DEDUP_WINDOW}

        try:
            pairs = self.dexscreener.search(self.search_query)
        except Exception as e:
            logger.warning("DexScreener search failed: %s", e)
            return 0

        if not pairs:
            logger.debug("No pairs returned for query '%s'", self.search_query)
            return 0

        for pair in pairs[: self.max_tokens]:
            token = self._pair_to_token(pair)
            if not token:
                continue

            addr = token["address"]

            if token.get("liquidity_usd", 0) < self.min_liquidity:
                continue

            is_down, drop_pct = self._is_downtrending(token)
            if not is_down:
                continue

            if addr in self.alerted:
                continue

            vol_spike = self._check_volume_spike(token)
            sm_count = self._check_smart_money_buying(addr)

            if vol_spike is None and sm_count == 0:
                continue

            thesis = self._build_thesis(token, drop_pct, vol_spike, sm_count)

            ok = self.alerter.send_reversal_alert(
                symbol=token["symbol"],
                contract=addr,
                drop_pct=drop_pct,
                price=token["price"],
                liquidity=token["liquidity_usd"],
                volume_change=vol_spike or 0,
                smart_money_count=sm_count,
                market_cap=token.get("market_cap", 0),
                thesis=thesis,
            )

            if ok:
                alerts_sent += 1
                self.alerted[addr] = now
                logger.info(
                    "Reversal alert: %s -%.0f%% vol_spike=%s sm=%d",
                    token["symbol"], drop_pct,
                    f"{vol_spike:.1f}x" if vol_spike else "N/A",
                    sm_count,
                )

            time.sleep(0.2)

        if alerts_sent:
            logger.info("Reversal detector: %d alerts sent", alerts_sent)
        return alerts_sent

    def run_loop(self, interval: Optional[int] = None) -> None:
        wait = interval or self.interval
        logger.info("Reversal detector loop started (interval=%ds)", wait)
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
    parser = argparse.ArgumentParser(description="Reversal token detector")
    parser.add_argument("--once", action="store_true", help="Run single scan then exit")
    parser.add_argument("--config", default="/a0/usr/workdir/catecoin-scanner/config.yaml")
    parser.add_argument("--interval", type=int, default=None, help="Override poll interval")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_config(args.config)
    detector = ReversalDetector(config)

    if args.once:
        count = detector.poll_once()
        print(f"Reversal detector: {count} alerts sent")
        return 0

    detector.run_loop(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
