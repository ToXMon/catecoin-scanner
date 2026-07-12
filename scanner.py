#!/usr/bin/env python3
"""
Catecoin Multi-Scanner
=======================
Monitors Cate (Catecoin) on Robinhood Chain and sends Telegram alerts.

Three modules run in a single process with independent timers:
  1. Price Monitor      — polls DexScreener every 60s, alerts on thresholds
  2. Smart Money Tracker — polls tracked wallets every 5min, alerts on new buys
  3. Token Discovery     — scans for new/zombie tokens every 10min

Usage:
  python scanner.py                          # Run all three (default)
  python scanner.py --once                   # Single pass of all modules then exit
  python scanner.py --price-only             # Only price monitor (original behavior)
  python scanner.py --smart-money-only       # Only smart money tracking
  python scanner.py --discovery-only         # Only token discovery
  python scanner.py --test-alert             # Send test Telegram message
  python scanner.py --config /path/to/cfg    # Custom config file
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dexscreener import DexScreenerClient
from telegram_alert import TelegramAlerter, resolve_telegram
from smart_money import SmartMoneyTracker
from token_discovery import TokenDiscovery

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

from health_server import start_health_server
logger = logging.getLogger("catecoin-scanner")

DEFAULT_PAIR_ADDRESS = "0xaC366079B95E56AA2dF22dE84373e47594dc1031"
DEFAULT_CHAIN = "robinhood"
DEFAULT_THRESHOLDS = [100, 200, 500, 1000, -50]
DEFAULT_POLL_INTERVAL = 60
DEXSCREENER_CHART_URL = "https://dexscreener.com/robinhood/0xac366079b95e56aa2df22de84373e47594dc1031"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(config_path: str) -> dict:
    """Load scanner config from YAML file."""
    if not config_path or not Path(config_path).exists():
        return {}
    if not HAS_YAML:
        logger.warning("PyYAML not installed — using defaults")
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Price Monitor (existing logic, refactored to use shared clients)
# ---------------------------------------------------------------------------
class CatecoinScanner:
    """Price-based alert scanner for Catecoin via DexScreener."""

    def __init__(self, config: dict) -> None:
        self.chain = config.get("chain", DEFAULT_CHAIN)
        self.pair_address = config.get("pair_address", DEFAULT_PAIR_ADDRESS)
        self.poll_interval = config.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL)
        self.thresholds = sorted(
            config.get("thresholds", DEFAULT_THRESHOLDS), reverse=True
        )
        self.baseline_override = config.get("baseline_override")
        self.dex = DexScreenerClient()
        self.alerter = TelegramAlerter.from_config(config)
        self.baseline_price = None
        self.triggered: dict[int, bool] = {}

    def init_baseline(self) -> None:
        """Set baseline price from override or first API fetch."""
        if self.baseline_override is not None:
            self.baseline_price = float(self.baseline_override)
            logger.info("Baseline from override: $%.8f", self.baseline_price)
            return

        pair = self.dex.get_pair(self.chain, self.pair_address)
        if pair:
            self.baseline_price = float(pair.get("priceUsd", 0))
            logger.info("Baseline auto-detected: $%.8f", self.baseline_price)
        else:
            logger.error("Failed to set baseline — will retry next cycle")

    def check_thresholds(self, pair: dict) -> None:
        """Check if any untriggered threshold has been hit."""
        if self.baseline_price is None or self.baseline_price <= 0:
            return

        current = float(pair.get("priceUsd", 0))
        pct = ((current - self.baseline_price) / self.baseline_price) * 100

        for threshold in self.thresholds:
            if self.triggered.get(threshold):
                continue
            hit = (threshold > 0 and pct >= threshold) or \
                  (threshold < 0 and pct <= threshold)
            if not hit:
                continue

            self.triggered[threshold] = True
            logger.info("🎯 THRESHOLD HIT: %+d%% (actual: %+.1f%%)", threshold, pct)
            msg = self._format_alert(pair, pct, threshold)
            if self.alerter.send(msg):
                logger.info("Alert sent for %+d%%", threshold)
            else:
                logger.error("Alert FAILED for %+d%%", threshold)

    def _format_alert(self, pair: dict, pct_change: float, threshold: float) -> str:
        price = float(pair.get("priceUsd", 0))
        h24 = pair.get("priceChange", {}).get("h24", 0)
        liq = pair.get("liquidity", {}).get("usd", 0)
        vol = pair.get("volume", {}).get("h24", 0)
        m5 = pair.get("txns", {}).get("m5", {})
        mult = price / self.baseline_price if self.baseline_price > 0 else 0
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        emoji = "🚨" if threshold > 0 else "⚠️"
        direction = f"+{pct_change:.1f}" if pct_change >= 0 else f"{pct_change:.1f}"

        return (
            f"{emoji} <b>CATE ALERT: {direction}%</b>\n\n"
            f"💰 Price: ${price:.8f} ({mult:.2f}x from baseline)\n"
            f"📊 Baseline: ${self.baseline_price:.8f}\n"
            f"📈 24h Change: {h24:+.1f}%\n"
            f"💧 Liquidity: ${liq:,.0f}\n"
            f"🔄 5m: {m5.get('buys', 0)} buys / {m5.get('sells', 0)} sells\n"
            f"📊 Volume 24h: ${vol:,.0f}\n\n"
            f'<a href="{DEXSCREENER_CHART_URL}">View Chart</a>\n'
            f"⏰ {ts}"
        )

    def poll_once(self) -> None:
        """Single poll cycle: fetch, log, check thresholds."""
        pair = self.dex.get_pair(self.chain, self.pair_address)
        if not pair:
            return

        if self.baseline_price is None:
            self.init_baseline()
            if self.baseline_price is None:
                return

        current = float(pair.get("priceUsd", 0))
        pct = ((current - self.baseline_price) / self.baseline_price) * 100
        m5 = pair.get("txns", {}).get("m5", {})
        liq = pair.get("liquidity", {}).get("usd", 0)
        vol = pair.get("volume", {}).get("h24", 0)
        logger.info(
            "Price: $%.8f | Δ: %+.1f%% | 5m B/S: %d/%d | Liq: $%.0f | Vol24h: $%.0f",
            current, pct, m5.get("buys", 0), m5.get("sells", 0), liq, vol,
        )
        self.check_thresholds(pair)

    def run_loop(self) -> None:
        """Continuous price monitoring loop."""
        logger.info(
            "Price monitor started | chain=%s pair=%s... poll=%ds thresholds=%s",
            self.chain, self.pair_address[:10], self.poll_interval, self.thresholds,
        )
        self.init_baseline()
        while True:
            try:
                self.poll_once()
            except Exception as e:
                logger.error("Price cycle error: %s", e, exc_info=True)
            time.sleep(self.poll_interval)

    def send_test_alert(self) -> None:
        """Send a test Telegram message to verify integration."""
        if not self.alerter.enabled:
            logger.error(
                "Telegram not configured. Set TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID env vars or check robinhood-alpha config."
            )
            sys.exit(1)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        msg = (
            "🧪 <b>Catecoin Scanner — Test Alert</b>\n\n"
            "Telegram integration is working.\n"
            f"⏰ {ts}"
        )
        logger.info("Sending test alert...")
        if self.alerter.send(msg):
            logger.info("✅ Test alert sent successfully")
        else:
            logger.error("❌ Test alert failed")
            sys.exit(1)


# ---------------------------------------------------------------------------
# Multi-Scanner Orchestrator
# ---------------------------------------------------------------------------
class MultiScanner:
    """Orchestrates price monitor, smart money tracker, and token discovery."""

    def __init__(self, config: dict) -> None:
        self.price_monitor = CatecoinScanner(config)
        self.smart_money = SmartMoneyTracker(config)
        self.discovery = TokenDiscovery(config)

    def run_loop(self) -> None:
        """Run all three monitors with independent timers in a single process."""
        logger.info("Multi-scanner starting — all modules active")
        self.price_monitor.init_baseline()

        last_smart_money = 0.0
        last_discovery = 0.0

        while True:
            now = time.time()

            # Price check (every 60s)
            try:
                self.price_monitor.poll_once()
            except Exception as e:
                logger.error("Price monitor error: %s", e, exc_info=True)

            # Smart money (every 5 min)
            if now - last_smart_money > self.smart_money.poll_interval:
                try:
                    alerts = self.smart_money.scan_all_wallets()
                    if alerts:
                        logger.info("Smart money: %d alerts sent", alerts)
                except Exception as e:
                    logger.error("Smart money error: %s", e, exc_info=True)
                last_smart_money = now

            # Discovery (every 10 min)
            if now - last_discovery > self.discovery.poll_interval:
                try:
                    alerts = self.discovery.scan_new_tokens()
                    if alerts:
                        logger.info("Discovery: %d alerts sent", alerts)
                except Exception as e:
                    logger.error("Discovery error: %s", e, exc_info=True)
                last_discovery = now

            time.sleep(self.price_monitor.poll_interval)

    def run_once(self) -> None:
        """Single pass of all three modules then exit."""
        logger.info("Running single pass of all modules (--once)")
        self.price_monitor.init_baseline()
        self.price_monitor.poll_once()
        sm_alerts = self.smart_money.scan_all_wallets()
        disc_alerts = self.discovery.scan_new_tokens()
        logger.info(
            "Single pass complete | smart_money_alerts=%d discovery_alerts=%d",
            sm_alerts, disc_alerts,
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    start_health_server()
    parser = argparse.ArgumentParser(
        description="Catecoin Multi-Scanner (price + smart money + discovery)"
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--once", action="store_true", help="Single pass then exit")
    parser.add_argument("--test-alert", action="store_true", help="Send test Telegram message")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--price-only", action="store_true", help="Only run price monitor")
    mode.add_argument("--smart-money-only", action="store_true", help="Only run smart money tracking")
    mode.add_argument("--discovery-only", action="store_true", help="Only run token discovery")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = load_config(args.config)

    # Test alert mode
    if args.test_alert:
        CatecoinScanner(config).send_test_alert()
        return

    # Price-only mode
    if args.price_only:
        scanner = CatecoinScanner(config)
        if args.once:
            scanner.init_baseline()
            scanner.poll_once()
        else:
            scanner.run_loop()
        return

    # Smart-money-only mode
    if args.smart_money_only:
        tracker = SmartMoneyTracker(config)
        if args.once:
            alerts = tracker.scan_all_wallets()
            logger.info("Smart money scan complete: %d alerts sent", alerts)
        else:
            tracker.run_loop()
        return

    # Discovery-only mode
    if args.discovery_only:
        discovery = TokenDiscovery(config)
        if args.once:
            alerts = discovery.scan_new_tokens()
            logger.info("Discovery scan complete: %d alerts sent", alerts)
        else:
            discovery.run_loop()
        return

    # Default: all three modules
    multi = MultiScanner(config)
    if args.once:
        multi.run_once()
    else:
        multi.run_loop()


# ---------------------------------------------------------------------------
# Container keep-alive (NEVER let the process exit in container mode)
# ---------------------------------------------------------------------------
def _container_keep_alive():
    """Infinite loop to keep container alive. Health server runs as daemon thread."""
    import time as _time
    while True:
        try:
            _time.sleep(3600)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.getLogger("catecoin-scanner").error(
            "Main crashed: %s — keeping container alive", e, exc_info=True
        )
    # If main() returns (e.g., --once or --test-alert), keep alive in container
    _container_keep_alive()
