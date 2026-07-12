#!/usr/bin/env python3
"""Early Token Detection + Zombie Revival Module.

Discovers new and reviving tokens on Robinhood Chain using only free APIs:
- DexScreener trending search
- Blockscout newest tokens list
- Bot pattern detection (SpecterAI-inspired)
- Zombie revival detection (dormant tokens with volume spikes + smart money)

Usage (standalone):
    python token_discovery.py --once     # Single scan then exit
    python token_discovery.py            # Continuous loop (10 min intervals)
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone
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

logger = logging.getLogger("catecoin-scanner.discovery")

DEXSCREENER_CHART = "https://dexscreener.com/robinhood/{addr}"
BLOCKSCOUT_TOKEN = "https://robinhoodchain.blockscout.com/token/{addr}"
BLOCKSCOUT_ADDR = "https://robinhoodchain.blockscout.com/address/{addr}"


def load_config(config_path: str) -> dict:
    if not config_path or not Path(config_path).exists():
        return {}
    if not HAS_YAML:
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def is_bot_pattern(transfers: list, wallet: str) -> bool:
    """Detect bot-like buying patterns.

    Flagged when multiple transfers from the same wallet have identical amounts,
    suggesting automated bot activity rather than organic trading.
    """
    wallet_lower = wallet.lower()
    wallet_txs = [
        t for t in transfers
        if ((t.get("from") or {}).get("hash", "").lower() == wallet_lower or
            (t.get("to") or {}).get("hash", "").lower() == wallet_lower)
    ]
    if len(wallet_txs) < 2:
        return False
    amounts = []
    for t in wallet_txs:
        total = t.get("total") or {}
        raw = total.get("value", 0)
        try:
            amounts.append(float(raw))
        except (TypeError, ValueError):
            pass
    # All identical non-zero amounts = bot pattern
    if len(amounts) > 1 and len(set(amounts)) == 1 and amounts[0] > 0:
        return True
    return False


class TokenDiscovery:
    """Discovers new tokens and detects zombie revivals on Robinhood Chain."""

    def __init__(self, config: dict) -> None:
        disc_cfg = config.get("discovery", {})

        self.enabled = disc_cfg.get("enabled", True)
        self.poll_interval = disc_cfg.get("poll_interval_seconds", 600)
        self.min_liquidity = disc_cfg.get("min_liquidity_usd", 5000)
        self.min_volume_24h = disc_cfg.get("min_volume_24h", 10000)
        self.min_holders = disc_cfg.get("min_holders", 50)
        self.zombie_dormancy_days = disc_cfg.get("zombie_dormancy_days", 7)
        self.zombie_spike_mult = disc_cfg.get("zombie_volume_spike_multiplier", 3.0)

        bs_base = disc_cfg.get("blockscout_base", "https://robinhoodchain.blockscout.com/api/v2")
        self.blockscout = BlockscoutClient(base_url=bs_base)
        self.dex = DexScreenerClient()
        self.alerter = TelegramAlerter.from_config(config)

        # Track known tokens to avoid re-alerting
        self.known_tokens: Set[str] = set()
        # Track token first-seen timestamps for zombie detection
        self.token_first_seen: Dict[str, float] = {}

    def scan_new_tokens(self) -> int:
        """Main entry: scan all discovery sources. Returns alerts sent."""
        alerts = 0
        alerts += self._scan_dexscreener_trending()
        alerts += self._scan_blockscout_new()
        return alerts

    def _scan_dexscreener_trending(self) -> int:
        """Fetch DexScreener trending Robinhood Chain tokens."""
        logger.info("Scanning DexScreener for trending Robinhood tokens...")
        pairs = self.dex.search("robinhood")
        if not pairs:
            logger.info("  No pairs found in DexScreener search")
            return 0

        logger.info("  Found %d pairs", len(pairs))
        alerts = 0
        for pair in pairs[:20]:  # Top 20 results
            token_addr = (pair.get("baseToken") or {}).get("address", "")
            if not token_addr:
                continue
            token_addr = token_addr.lower()

            if token_addr in self.known_tokens:
                continue
            self.known_tokens.add(token_addr)
            self.token_first_seen[token_addr] = time.time()

            if self._evaluate_early_token(pair, token_addr):
                alerts += 1

            # Check for zombie revival
            if self._check_zombie(token_addr, pair):
                alerts += 1

        return alerts

    def _scan_blockscout_new(self) -> int:
        """Fetch newest tokens from Blockscout."""
        logger.info("Scanning Blockscout for newest tokens...")
        tokens = self.blockscout.get_new_tokens(limit=20)
        if not tokens:
            logger.info("  No new tokens from Blockscout")
            return 0

        logger.info("  Found %d tokens", len(tokens))
        alerts = 0
        for token in tokens:
            token_addr = (token.get("address") or {}).get("hash", "")
            if not token_addr:
                continue
            token_addr = token_addr.lower()

            if token_addr in self.known_tokens:
                continue
            self.known_tokens.add(token_addr)
            self.token_first_seen[token_addr] = time.time()

            # Fetch DexScreener data for this token
            pair = self.dex.get_token(token_addr)
            if not pair:
                logger.debug("  %s: no DexScreener data yet (may be very new)", token_addr[:10])
                continue

            if self._evaluate_early_token(pair, token_addr):
                alerts += 1

        return alerts

    def _evaluate_early_token(self, pair: dict, token_addr: str) -> bool:
        """Check if a new token meets early detection criteria."""
        symbol = (pair.get("baseToken") or {}).get("symbol", "???")
        name = (pair.get("baseToken") or {}).get("name", "Unknown")
        price = float(pair.get("priceUsd", 0) or 0)
        liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        volume_h24 = float(pair.get("volume", {}).get("h24", 0) or 0)
        change_h24 = float(pair.get("priceChange", {}).get("h24", 0) or 0)
        fdv = float(pair.get("fdv", 0) or 0)
        pair_addr = pair.get("pairAddress", token_addr)
        created = pair.get("pairCreatedAt", "")

        # Token holders from Blockscout
        token_info = self.blockscout.get_token_info(token_addr)
        holders = (token_info or {}).get("holders", 0) or 0

        # Calculate age in minutes
        age_minutes = 999999
        if created:
            try:
                if isinstance(created, (int, float)):
                    dt = datetime.fromtimestamp(created / 1000 if created > 1e12 else created, tz=timezone.utc)
                elif isinstance(created, str):
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                else:
                    dt = datetime.now(timezone.utc)
                age_minutes = (datetime.now(timezone.utc) - dt).total_seconds() / 60
            except (ValueError, TypeError):
                pass

        # Determine signal type
        signals = []
        if volume_h24 >= self.min_volume_24h:
            signals.append(f"Volume >${self.min_volume_24h/1000:.0f}K")
        if holders >= self.min_holders:
            signals.append(f"{holders}+ holders")
        if liquidity >= self.min_liquidity * 4:  # $20K for early token
            signals.append(f"Liquidity >${liquidity/1000:.0f}K")
        if change_h24 >= 300:
            signals.append(f"+{change_h24:.0f}% pump")

        # Must meet at least ONE criteria for <24h tokens
        if age_minutes < 1440 and not signals:
            return False
        # For older tokens, need at least volume + one other
        if age_minutes >= 1440 and len(signals) < 2:
            return False

        # Bot detection — check recent transfers
        transfers_data = self.blockscout.get_token_transfers(token_addr, params={"limit": 30})
        bot_detected = False
        if transfers_data and transfers_data.get("items"):
            txs = transfers_data["items"]
            for tx in txs[:10]:
                buyer = ((tx.get("to") or {}).get("hash") or "")
                if buyer and is_bot_pattern(txs, buyer):
                    bot_detected = True
                    break

        signal_text = " | ".join(signals) if signals else "New listing detected"
        return self._alert_new_token(
            symbol, name, token_addr, price, fdv, liquidity, change_h24,
            holders, pair, signal_text, bot_detected, age_minutes
        )

    def _check_zombie(self, token_addr: str, pair: dict) -> bool:
        """Check for zombie revival pattern (dormant token waking up)."""
        created = pair.get("pairCreatedAt", "")
        if not created:
            return False

        try:
            if isinstance(created, (int, float)):
                dt = datetime.fromtimestamp(created / 1000 if created > 1e12 else created, tz=timezone.utc)
            elif isinstance(created, str):
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            else:
                return False
            age_days = (datetime.now(timezone.utc) - dt).days
        except (ValueError, TypeError):
            return False

        # Must be dormant (>7 days old)
        if age_days < self.zombie_dormancy_days:
            return False

        # Check volume spike: m5 vs h24 average
        volume = pair.get("volume") or {}
        vol_m5 = float(volume.get("m5", 0) or 0)
        vol_h24 = float(volume.get("h24", 0) or 0)
        if vol_h24 <= 0:
            return False

        # Hourly average vs 5-min burst
        hourly_avg = vol_h24 / 24
        if hourly_avg <= 0:
            return False
        spike_ratio = vol_m5 / (hourly_avg / 12)  # normalize 5min vs hourly

        if spike_ratio < self.zombie_spike_mult:
            return False

        liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        if liquidity < self.min_liquidity:
            return False

        symbol = (pair.get("baseToken") or {}).get("symbol", "???")
        name = (pair.get("baseToken") or {}).get("name", "Unknown")
        price = float(pair.get("priceUsd", 0) or 0)
        change_h1 = float(pair.get("priceChange", {}).get("h1", 0) or 0)

        return self._alert_zombie(
            symbol, token_addr, age_days, spike_ratio, change_h1,
            liquidity, vol_m5, price
        )

    def _alert_new_token(
        self, symbol: str, name: str, token_addr: str, price: float,
        mcap: float, liquidity: float, change: float, holders: int,
        pair: dict, signal: str, bot_detected: bool, age_minutes: float
    ) -> bool:
        """Send Telegram alert for a new token discovery."""
        pair_addr = pair.get("pairAddress", token_addr)
        txns = pair.get("txns", {}).get("m5", {})
        buys = txns.get("buys", 0)
        sells = txns.get("sells", 0)

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        age_str = f"{age_minutes:.0f}min" if age_minutes < 1440 else f"{age_minutes/1440:.1f}d"

        chart_url = DEXSCREENER_CHART.format(addr=pair_addr)
        explorer_url = BLOCKSCOUT_TOKEN.format(addr=token_addr)

        bot_warn = "\n⚠️ <b>Bot activity detected</b>" if bot_detected else ""

        msg = (
            f"🆕 <b>NEW TOKEN: {symbol}</b>\n\n"
            f"💰 <b>{name}</b>\n"
            f"📍 CA: <code>{token_addr}</code>\n"
            f"💵 Price: ${price:.8f}\n"
            f"📊 MC: ${mcap:,.0f}\n"
            f"💧 Liquidity: ${liquidity:,.0f}\n"
            f"📈 24h: {change:+.1f}%\n"
            f"👥 Holders: {holders:,}\n"
            f"🔄 5m: {buys}B/{sells}S\n"
            f"⚡ Created: {age_str} ago\n\n"
            f"🎯 <b>Signal</b>: {signal}{bot_warn}\n\n"
            f'🔗 <a href="{chart_url}">Chart</a> | <a href="{explorer_url}">Explorer</a>\n'
            f"⏰ {ts}"
        )
        return self.alerter.send(msg)

    def _alert_zombie(
        self, symbol: str, token_addr: str, days: int, spike: float,
        change_h1: float, liquidity: float, volume_m5: float, price: float
    ) -> bool:
        """Send Telegram alert for a zombie token revival."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        chart_url = DEXSCREENER_CHART.format(addr=token_addr)
        explorer_url = BLOCKSCOUT_TOKEN.format(addr=token_addr)

        msg = (
            f"🧟 <b>ZOMBIE REVIVAL: {symbol}</b>\n\n"
            f"Dormant token waking up!\n"
            f"📊 Was dead for {days} days, now {spike:.1f}x volume spike\n"
            f"💰 {change_h1:+.1f}% in last hour\n"
            f"💧 Liquidity: ${liquidity:,.0f}\n"
            f"💵 Price: ${price:.8f}\n\n"
            f"High-alpha: dormancy + volume spike = accumulation pattern\n\n"
            f"📍 CA: <code>{token_addr}</code>\n"
            f'🔗 <a href="{chart_url}">Chart</a> | <a href="{explorer_url}">Explorer</a>\n'
            f"⏰ {ts}"
        )
        return self.alerter.send(msg)

    def run_loop(self) -> None:
        """Continuous monitoring loop."""
        logger.info("Token discovery started | interval=%ds", self.poll_interval)
        while True:
            try:
                self.scan_new_tokens()
            except Exception as e:
                logger.error("Discovery cycle error: %s", e, exc_info=True)
            time.sleep(self.poll_interval)


def main():
    parser = argparse.ArgumentParser(description="Token Discovery Scanner")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--once", action="store_true", help="Single scan then exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = load_config(args.config)
    discovery = TokenDiscovery(config)

    if args.once:
        alerts = discovery.scan_new_tokens()
        logger.info("Discovery scan complete: %d alerts sent", alerts)
    else:
        discovery.run_loop()


if __name__ == "__main__":
    main()
