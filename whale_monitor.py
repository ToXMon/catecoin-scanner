#!/usr/bin/env python3
"""Whale Transaction Monitor — Holder Balance Tracking.

Since Blockscout address transfers are broken on Robinhood Chain (422),
we track whale activity by monitoring holder balance changes between polls.
When a top holder's balance changes significantly, that's a whale move.

Usage:
    python whale_monitor.py --once     # Single scan
    python whale_monitor.py            # Continuous (5 min intervals)
"""

from __future__ import annotations

import argparse
import json
import logging
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

logger = logging.getLogger("catecoin-scanner.whale_monitor")


class WhaleMonitor:
    """Monitors whale wallet balances via holder list comparison."""

    def __init__(self, config: dict) -> None:
        wm_cfg = config.get("whale_monitor", {})
        self.enabled = wm_cfg.get("enabled", True)
        self.poll_interval = wm_cfg.get("poll_interval_seconds", 300)
        self.min_transfer_usd = wm_cfg.get("min_transfer_usd", 10000)

        # Tracked tokens
        self.tracked_tokens = wm_cfg.get("tracked_tokens", [
            {"address": "0xfc5ABD01E4Def799549eee154449Ff6a7ae0cAc7", "symbol": "CATE"}
        ])

        bs_base = wm_cfg.get("blockscout_base", "https://robinhoodchain.blockscout.com/api/v2")
        self.blockscout = BlockscoutClient(base_url=bs_base)
        self.dex = DexScreenerClient()
        self.alerter = TelegramAlerter.from_config(config)

        # Alchemy (PRIMARY data source for real-time transfer tracking)
        alch_cfg = config.get("alchemy", {}) or {}
        self.alchemy = AlchemyClient(
            api_key=alch_cfg.get("api_key"),
            network=alch_cfg.get("network", "robinhood-mainnet"),
            cu_warning_threshold=alch_cfg.get("cu_warning_threshold", 0.8),
            cu_monthly_limit=alch_cfg.get("cu_monthly_limit", 30_000_000),
        )

        # Previous holder balances: {(token_addr, holder_addr): balance_str}
        self.previous_balances: Dict[str, str] = {}
        self.first_scan = True

        # Dedup: alerts sent in last hour
        self.alerted: Dict[str, float] = {}

        # Cache of known whale addresses (top holders) for Alchemy classification
        self.previous_top_holders: set = set()

    def scan_via_alchemy(self) -> int:
        """PRIMARY scan path: detect real-time whale moves via Alchemy transfers.

        For each tracked token, query the last 50 transfers via
        alchemy_getAssetTransfers. Classify any transfer > $10K as:
          - ACCUMULATION: transfer TO a new or growing holder
          - DISTRIBUTION: transfer FROM a whale (likely exit)
        This replaces the broken Blockscout balance-diff approach with
        REAL, timestamped transfer data.
        """
        alerts_sent = 0

        for token in self.tracked_tokens:
            token_addr = (token.get("address") or "").lower()
            symbol = token.get("symbol", "???")
            if not token_addr:
                continue

            # Get current token price for USD conversion
            try:
                pair = self.dex.get_token(token_addr) or {}
                price = float(pair.get("priceUsd", 0) or 0)
            except Exception:
                price = 0.0

            try:
                transfers = self.alchemy.get_asset_transfers(
                    token_contract=token_addr,
                    max_count=50,
                    order="desc",
                )
            except Exception as e:
                logger.warning("Alchemy transfer query failed for %s: %s", symbol, e)
                continue

            for t in transfers:
                try:
                    value_tokens = float(t.get("value") or 0)
                except (TypeError, ValueError):
                    value_tokens = 0.0

                value_usd = value_tokens * price
                if value_usd < self.min_transfer_usd:
                    continue

                # Dedup by transaction hash
                tx_hash = t.get("hash", "")
                if not tx_hash:
                    continue
                dedup_key = f"alchemy:{tx_hash}"
                last_alerted = self.alerted.get(dedup_key, 0)
                if time.time() - last_alerted < 3600:
                    continue

                # Classify: transfers INTO top holders = ACCUMULATION,
                # otherwise treat as DISTRIBUTION (broad heuristic)
                from_addr = t.get("from", "").lower()
                to_addr = t.get("to", "").lower()

                # Heuristic: if the recipient is a known top holder, accumulate;
                # if the sender is a known top holder, distribute.
                direction = "DISTRIBUTION"
                if from_addr in self.previous_top_holders or self._is_known_whale(from_addr):
                    direction = "DISTRIBUTION"
                elif to_addr in self.previous_top_holders or self._is_known_whale(to_addr):
                    direction = "ACCUMULATION"
                elif value_usd >= 50000:
                    # Large unknown transfer — flag as accumulation by default
                    direction = "ACCUMULATION"

                self.alerted[dedup_key] = time.time()

                logger.info(
                    "🐳 ALCHEMY WHALE MOVE: %s %s %s ($%.0f) tx=%s",
                    symbol, direction, to_addr[:10], value_usd, tx_hash[:10],
                )

                sent = self.alerter.send_whale_alert(
                    symbol=symbol,
                    contract=token_addr,
                    amount_usd=value_usd,
                    direction=direction,
                    whale_addr=to_addr,
                )
                alerts_sent += 1 if sent else 0

        # Clean old alerts (>1 hour)
        now = time.time()
        self.alerted = {k: v for k, v in self.alerted.items() if now - v < 3600}

        logger.info(
            "Alchemy whale scan complete: %d alerts, CU used=%d",
            alerts_sent, self.alchemy.cu_used,
        )
        return alerts_sent

    def _is_known_whale(self, addr: str) -> bool:
        """Check if address is in our previous top-holders set (cached)."""
        return addr.lower() in self.previous_top_holders if hasattr(self, "previous_top_holders") else False

    def poll_once(self) -> int:
        """Main scan: PRIMARY Alchemy transfer path + SECONDARY Blockscout balance diff.

        Alchemy (PRIMARY): real-time whale transfers via alchemy_getAssetTransfers.
        Blockscout (SECONDARY): holder balance diff remains as a complementary snapshot.
        """
        if not self.enabled:
            return 0

        alerts_sent = 0

        # ---- PRIMARY PATH: Alchemy real-time whale transfer detection ----
        try:
            alerts_sent += self.scan_via_alchemy()
        except Exception as e:
            logger.warning("Alchemy whale scan failed (degraded to Blockscout): %s", e)

        current_balances: Dict[str, str] = {}

        for token in self.tracked_tokens:
            token_addr = token.get("address", "").lower()
            symbol = token.get("symbol", "???")

            if not token_addr:
                continue

            try:
                holders = self.blockscout.get_token_holders(token_addr, limit=20)
                if not holders:
                    continue

                # Get token price for USD conversion
                pair = self.dex.get_token(token_addr) or {}
                price = float(pair.get("priceUsd", 0) or 0)

                # Build current balance snapshot
                for holder in holders[:20]:
                    h_addr = (holder.get("address") or {}).get("hash", "").lower()
                    value = holder.get("value", "0")
                    current_balances[f"{token_addr}:{h_addr}"] = value

                # Skip comparison on first scan (just record baseline)
                if self.first_scan:
                    logger.info("Whale monitor: baseline recorded for %s (%d holders)",
                              symbol, len(holders[:20]))
                    continue

                # Compare balances to detect changes
                for key, new_balance in current_balances.items():
                    ta, ha = key.split(":", 1)
                    old_balance = self.previous_balances.get(key, "0")

                    if old_balance == new_balance:
                        continue

                    # Balance changed — calculate delta
                    try:
                        old_val = int(old_balance)
                        new_val = int(new_balance)
                        delta = new_val - old_val

                        # Get token decimals for USD calc
                        token_info = self.blockscout.get_token_info(ta) or {}
                        decimals = int(token_info.get("decimals", 18))
                        delta_tokens = delta / (10 ** decimals)
                        delta_usd = delta_tokens * price

                        if abs(delta_usd) >= self.min_transfer_usd:
                            # Dedup check
                            dedup_key = f"{ta}:{ha}:{'in' if delta > 0 else 'out'}"
                            last_alerted = self.alerted.get(dedup_key, 0)
                            if time.time() - last_alerted < 3600:
                                continue

                            direction = "ACCUMULATION" if delta > 0 else "DISTRIBUTION"
                            self.alerted[dedup_key] = time.time()

                            logger.info("🐳 WHALE MOVE: %s %s %s ($%.0f)",
                                      ha[:10], direction, symbol, abs(delta_usd))

                            sent = self.alerter.send_whale_alert(
                                symbol=symbol,
                                contract=ta,
                                amount_usd=abs(delta_usd),
                                direction=direction,
                                whale_addr=ha,
                            )
                            alerts_sent += 1 if sent else 0

                    except (ValueError, TypeError):
                        continue

                time.sleep(0.2)

            except Exception as e:
                logger.warning("Whale monitor error on %s: %s", symbol, e)

        # Update state
        self.previous_balances = current_balances
        self.first_scan = False

        # Clean old alerts (>1 hour)
        now = time.time()
        self.alerted = {k: v for k, v in self.alerted.items() if now - v < 3600}

        logger.info("Whale monitor scan complete: %d alerts sent", alerts_sent)
        return alerts_sent


def main():
    parser = argparse.ArgumentParser(description="Whale Monitor")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--once", action="store_true", help="Run once")
    parser.add_argument("--interval", type=int, default=300, help="Poll interval")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    config = {}
    if Path(args.config).exists() and HAS_YAML:
        with open(args.config) as f:
            config = yaml.safe_load(f) or {}

    monitor = WhaleMonitor(config)

    if args.once:
        monitor.poll_once()  # baseline
        print("Whale monitor baseline recorded. Run again to detect changes.")
    else:
        while True:
            try:
                monitor.poll_once()
            except Exception as e:
                logger.error("Scan error: %s", e, exc_info=True)
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
