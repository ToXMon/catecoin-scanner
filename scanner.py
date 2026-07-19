#!/usr/bin/env python3
"""
Catecoin Multi-Scanner — World-Class Alpha Screener
====================================================
Monitors Robinhood Chain and sends actionable Telegram alerts.

Six modules run in a single process with independent timers:
  1. Price Monitor       — polls DexScreener every 60s, alerts on thresholds
  2. Smart Money Tracker  — cross-references trending token holders every 5min
  3. Token Discovery      — scans for new/zombie tokens every 10min
  4. Whale Monitor        — tracks large transfers every 5min
  5. Zombie Detector      — finds dormant tokens with volume spikes every 30min
  6. Liquidity Flow       — tracks LP add/remove every 10min

Usage:
  python scanner.py                          # Run all modules
  python scanner.py --once                   # Single pass of all modules then exit
  python scanner.py --price-only             # Only price monitor
  python scanner.py --smart-money-only       # Only smart money
  python scanner.py --discovery-only         # Only token discovery
  python scanner.py --whale-only             # Only whale monitor
  python scanner.py --zombie-only            # Only zombie detector
  python scanner.py --liquidity-only         # Only liquidity flow
  python scanner.py --test-alert             # Send test Telegram message
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dexscreener import DexScreenerClient
from telegram_alert import TelegramAlerter
from smart_money import SmartMoneyTracker
from token_discovery import TokenDiscovery
from whale_monitor import WhaleMonitor
from zombie_detector import ZombieDetector
from liquidity_flow import LiquidityFlowAnalyzer
from reversal_detector import ReversalDetector
from base_scanner import run_dry_scan as run_base_dry_scan
from monad_scanner import run_dry_scan as run_monad_dry_scan
from runner_radar import run_scan as run_runner_radar_scan
from alert_analyzer import AlertAnalyzer
from alert_journal import AlertJournal

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



def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value

def load_config(config_path: str) -> dict:
    """Load scanner config from YAML file."""
    if not config_path or not Path(config_path).exists():
        return {}
    if not HAS_YAML:
        logger.warning("PyYAML not installed — using defaults")
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def resolve_telegram_config(config: dict) -> dict:
    """Resolve Telegram bot token and chat id from multiple sources.

    Priority:
    1. config['alerts']['telegram'] (direct config)
    2. robinhood-alpha config file
    3. Environment variables TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    """
    # Already in config under alerts.telegram
    alerts_cfg = config.get("alerts", {}).get("telegram", {})
    if alerts_cfg.get("bot_token") and alerts_cfg.get("chat_id"):
        return config

    # Resolve from robinhood-alpha config
    source = config.get("telegram_config_source", "")
    if source == "robinhood-alpha":
        ra_path = config.get(
            "robinhood_alpha_config_path",
            "/a0/usr/workdir/robinhood-alpha/config.yaml",
        )
        try:
            if Path(ra_path).exists():
                with open(ra_path) as f:
                    ra_cfg = yaml.safe_load(f) or {}
                tg = ra_cfg.get("alerts", {}).get("telegram", {})
                bot_token = tg.get("bot_token", "")
                chat_id = str(tg.get("chat_id", ""))
                if bot_token and chat_id:
                    config.setdefault("alerts", {})
                    config["alerts"]["telegram"] = {
                        "bot_token": bot_token,
                        "chat_id": chat_id,
                    }
                    logger.info("Telegram config resolved from robinhood-alpha")
                    return config
        except Exception as e:
            logger.warning("Failed to resolve telegram from robinhood-alpha: %s", e)

    # Fall back to env vars
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if bot_token and chat_id:
        config.setdefault("alerts", {})
        config["alerts"]["telegram"] = {"bot_token": bot_token, "chat_id": chat_id}
        logger.info("Telegram config resolved from env vars")
        return config

    logger.warning("No Telegram config found — alerts disabled")
    return config


# ---------------------------------------------------------------------------
# Price Monitor
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
        self.last_price = 0.0
        self.price_alert_threshold = config.get("price_alert_pct", 15.0)  # alert on 15%+ moves
        self.last_price_alert_time = 0.0

    def init_baseline(self) -> None:
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

    def poll_once(self) -> None:
        """Single poll cycle: fetch, log, check thresholds + price alerts."""
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

        # Check threshold alerts (cumulative from baseline)
        self._check_thresholds(pair, pct)

        # Check rapid price movement alert (comparing to last poll)
        self._check_rapid_price_move(pair, current)

    def _check_thresholds(self, pair: dict, pct: float) -> None:
        """Check if any untriggered threshold has been hit."""
        if self.baseline_price is None or self.baseline_price <= 0:
            return

        for threshold in self.thresholds:
            if self.triggered.get(threshold):
                continue
            hit = (threshold > 0 and pct >= threshold) or \
                  (threshold < 0 and pct <= threshold)
            if not hit:
                continue

            self.triggered[threshold] = True
            logger.info("🎯 THRESHOLD HIT: %+d%% (actual: %+.1f%%)", threshold, pct)

            # Use actionable alert format
            contract = "0xfc5ABD01E4Def799549eee154449Ff6a7ae0cAc7"
            self.alerter.send_price_alert(
                symbol="CATE",
                contract=contract,
                price=float(pair.get("priceUsd", 0)),
                change_pct=pct,
                old_price=self.baseline_price,
                liquidity=float((pair.get("liquidity") or {}).get("usd", 0)),
                volume_24h=float((pair.get("volume") or {}).get("h24", 0)),
            )

    def _check_rapid_price_move(self, pair: dict, current: float) -> None:
        """Alert on rapid price movement between polls."""
        if self.last_price <= 0:
            self.last_price = current
            return

        change_pct = ((current - self.last_price) / self.last_price) * 100
        now = time.time()

        # Only alert if move is significant AND at least 10 min since last alert
        if abs(change_pct) >= self.price_alert_threshold and (now - self.last_price_alert_time) > 600:
            self.last_price_alert_time = now
            logger.info("⚡ RAPID PRICE MOVE: %+.1f%% in last poll interval", change_pct)

            contract = config_cate_address
            self.alerter.send_price_alert(
                symbol="CATE",
                contract=contract,
                price=current,
                change_pct=change_pct,
                old_price=self.last_price,
                liquidity=float((pair.get("liquidity") or {}).get("usd", 0)),
                volume_24h=float((pair.get("volume") or {}).get("h24", 0)),
            )

        self.last_price = current

    def run_loop(self) -> None:
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


# Global for CATE contract address (used in price alerts)
config_cate_address = "0xfc5ABD01E4Def799549eee154449Ff6a7ae0cAc7"



def _run_base_scanner_once(config: dict) -> int:
    """Run one Base-chain dry scan cycle, logging observations without Telegram by default."""
    base_cfg = config.get("base_scanner", {}) or {}
    if not base_cfg.get("enabled", False):
        return 0
    max_pairs = int(base_cfg.get("max_pairs", 20))
    journal_db = base_cfg.get("journal_db_path") or config.get("journal", {}).get("db_path", "state/alert_journal.db")
    chains_config = config.get("chains_config_path", "chains.yaml")
    dry_run = bool(base_cfg.get("dry_run", False))
    telegram_enabled = _env_bool("BASE_SCANNER_TELEGRAM_ENABLED", bool(base_cfg.get("telegram_enabled", False)))
    # dry_run means journal-only mode; Telegram is independently gated by telegram_enabled/env
    result = run_base_dry_scan(
        max_pairs=max_pairs,
        journal_db=journal_db,
        chains_config=chains_config,
        observation_cooldown_hours=float(base_cfg.get("observation_cooldown_hours", 6)),
        telegram_enabled=telegram_enabled,
        telegram_min_state=_env_str("BASE_SCANNER_TELEGRAM_MIN_STATE", str(base_cfg.get("telegram_min_queue_state", "entry_ready"))),
        config={**config, "chain": "base"},
        dry_run=dry_run,
    )
    logger.info(
        "Base scanner: scanned=%d logged=%d queue=%s alert_worthy=%d telegram_sent=%d min_state=%s",
        result.get("pairs_scanned", 0),
        result.get("observations_logged", 0),
        result.get("queue_counts", {}),
        result.get("alert_worthy_count", 0),
        result.get("telegram_sent", 0),
        result.get("telegram_min_state", "entry_ready"),
    )
    return int(result.get("alert_worthy_count", 0) or 0)


def _run_monad_scanner_once(config: dict) -> int:
    """Run one Monad-chain dry scan cycle, logging observations without Telegram by default."""
    monad_cfg = config.get("monad_scanner", {}) or {}
    if not _env_bool("MONAD_SCANNER_ENABLED", bool(monad_cfg.get("enabled", False))):
        return 0
    max_pairs = int(monad_cfg.get("max_pairs", 20))
    journal_db = monad_cfg.get("journal_db_path") or config.get("journal", {}).get("db_path", "state/alert_journal.db")
    chains_config = config.get("chains_config_path", "chains.yaml")
    dry_run = bool(monad_cfg.get("dry_run", False))
    telegram_enabled = _env_bool("MONAD_SCANNER_TELEGRAM_ENABLED", bool(monad_cfg.get("telegram_enabled", False)))
    # dry_run means journal-only mode; Telegram is independently gated by telegram_enabled/env
    result = run_monad_dry_scan(
        max_pairs=max_pairs,
        journal_db=journal_db,
        chains_config=chains_config,
        observation_cooldown_hours=float(monad_cfg.get("observation_cooldown_hours", 6)),
        telegram_enabled=telegram_enabled,
        telegram_min_state=_env_str("MONAD_SCANNER_TELEGRAM_MIN_STATE", str(monad_cfg.get("telegram_min_queue_state", "entry_ready"))),
        config={**config, "chain": "monad"},
        dry_run=dry_run,
        tracked_wallets=monad_cfg.get("tracked_wallets"),
        wallet_lookback_blocks=int(monad_cfg.get("wallet_lookback_blocks", 200)),
    )
    wallet_info = result.get("wallet_tracking", {}) or {}
    logger.info(
        "Monad scanner: scanned=%d logged=%d queue=%s alert_worthy=%d telegram_sent=%d min_state=%s wallets=%d wallet_alerts=%d wallet_skip=%s",
        result.get("pairs_scanned", 0),
        result.get("observations_logged", 0),
        result.get("queue_counts", {}),
        result.get("alert_worthy_count", 0),
        result.get("telegram_sent", 0),
        result.get("telegram_min_state", "entry_ready"),
        wallet_info.get("wallets_checked", 0),
        wallet_info.get("wallet_alerts_logged", 0),
        wallet_info.get("skip_reason", ""),
    )
    return int(result.get("alert_worthy_count", 0) or 0)


def _run_runner_radar_once(config: dict) -> int:
    """Run one Robinhood runner radar cycle with journal-first alert transitions."""
    radar_cfg = config.get("runner_radar", {}) or {}
    if not radar_cfg.get("enabled", False):
        return 0
    radar_cfg = dict(radar_cfg)
    radar_cfg["telegram_enabled"] = _env_bool("RUNNER_RADAR_TELEGRAM_ENABLED", bool(radar_cfg.get("telegram_enabled", False)))
    radar_cfg["telegram_min_queue_state"] = _env_str("RUNNER_RADAR_TELEGRAM_MIN_STATE", str(radar_cfg.get("telegram_min_queue_state", "entry_ready")))
    if _env_bool("RUNNER_RADAR_DRY_RUN", bool(radar_cfg.get("dry_run", False))):
        radar_cfg["dry_run"] = True
        radar_cfg["telegram_enabled"] = False
    config = {**config, "runner_radar": radar_cfg}
    result = run_runner_radar_scan(config)
    logger.info(
        "Runner radar: scanned=%d logged=%d queue=%s telegram_sent=%d duplicates=%d",
        result.get("pairs_scanned", 0),
        result.get("observations_logged", 0),
        result.get("queue_counts", {}),
        result.get("telegram_sent", 0),
        result.get("duplicates_skipped", 0),
    )
    return int(result.get("telegram_sent", 0) or 0)


def _run_alert_analyzer_once(config: dict) -> dict:
    """Run one alert self-improvement analysis cycle (4-hour interval)."""
    analyzer_cfg = config.get("alert_analyzer", {}) or {}
    if not analyzer_cfg.get("enabled", False):
        return {"enabled": False}
    journal_cfg = config.get("journal", {}) or {}
    journal_db = journal_cfg.get("db_path", "state/alert_journal.db")
    journal = AlertJournal(db_path=journal_db, enabled=journal_cfg.get("enabled", True))
    dex = DexScreenerClient(default_chain="robinhood")
    analyzer = AlertAnalyzer(config=config, journal=journal, dex_client=dex)
    result = analyzer.run_analysis()
    logger.info(
        "Alert analyzer: alerts=%d analyzed=%d outcomes_updated=%d recommendations=%d",
        result.get("alerts_fetched", 0),
        result.get("alerts_analyzed", 0),
        result.get("outcomes_updated", 0),
        len(result.get("recommendations", [])),
    )
    return result


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Catecoin Multi-Scanner — World-Class Alpha Screener"
    )
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--once", action="store_true", help="Run all modules once then exit")
    parser.add_argument("--price-only", action="store_true", help="Only price monitor")
    parser.add_argument("--smart-money-only", action="store_true", help="Only smart money")
    parser.add_argument("--discovery-only", action="store_true", help="Only token discovery")
    parser.add_argument("--whale-only", action="store_true", help="Only whale monitor")
    parser.add_argument("--zombie-only", action="store_true", help="Only zombie detector")
    parser.add_argument("--liquidity-only", action="store_true", help="Only liquidity flow")
    parser.add_argument("--reversal-only", action="store_true", help="Only reversal detector")
    parser.add_argument("--runner-radar-only", action="store_true", help="Only Robinhood runner radar")
    parser.add_argument("--test-alert", action="store_true", help="Send test Telegram message")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    config = load_config(args.config)
    config = resolve_telegram_config(config)

    # Update global cate address
    global config_cate_address
    config_cate_address = config.get(
        "cate_token_address", "0xfc5ABD01E4Def799549eee154449Ff6a7ae0cAc7"
    )

    # Start health server for Akash keep-alive
    try:
        start_health_server()
        logger.info("Health server started on :8080")
    except Exception as e:
        logger.warning("Health server failed (non-fatal): %s", e)

    # Test alert mode
    if args.test_alert:
        scanner = CatecoinScanner(config)
        scanner.send_test_alert()
        return

    # Determine which modules to run
    single_mode = (
        args.price_only or args.smart_money_only or args.discovery_only or
        args.whale_only or args.zombie_only or args.liquidity_only
        or args.reversal_only or args.runner_radar_only
    )

    if single_mode:
        # Run single module in loop
        if args.price_only:
            scanner = CatecoinScanner(config)
            scanner.run_loop()
        elif args.smart_money_only:
            tracker = SmartMoneyTracker(config)
            _run_module_loop("Smart Money", tracker, tracker.poll_interval if hasattr(tracker, 'poll_interval') else 300)
        elif args.discovery_only:
            discovery = TokenDiscovery(config)
            _run_module_loop("Discovery", discovery, 600)
        elif args.whale_only:
            monitor = WhaleMonitor(config)
            _run_module_loop("Whale Monitor", monitor, 300)
        elif args.zombie_only:
            detector = ZombieDetector(config)
            _run_module_loop("Zombie Detector", detector, 1800)
        elif args.liquidity_only:
            flow = LiquidityFlowAnalyzer(config)
            _run_module_loop("Liquidity Flow", flow, 600)
        elif args.reversal_only:
            reversal = ReversalDetector(config)
            _run_module_loop("Reversal Detector", reversal, 900)
        elif args.runner_radar_only:
            _run_function_loop("Runner Radar", lambda: _run_runner_radar_once(config), config.get("runner_radar", {}).get("poll_interval_seconds", 120))
        return

    # --once mode: run all modules once
    if args.once:
        logger.info("=== Running all modules (--once mode) ===")
        total_alerts = 0

        # Module 1: Price Monitor
        try:
            logger.info("--- Price Monitor ---")
            scanner = CatecoinScanner(config)
            scanner.init_baseline()
            scanner.poll_once()
            logger.info("Price monitor: complete")
        except Exception as e:
            logger.error("Price monitor error: %s", e, exc_info=True)

        # Module 2: Smart Money
        try:
            logger.info("--- Smart Money Tracker ---")
            sm = SmartMoneyTracker(config)
            alerts = sm.scan_all_wallets()
            total_alerts += alerts
            logger.info("Smart money: %d alerts", alerts)
        except Exception as e:
            logger.error("Smart money error: %s", e, exc_info=True)

        # Module 3: Token Discovery
        try:
            logger.info("--- Token Discovery ---")
            disc = TokenDiscovery(config)
            alerts = disc.scan_new_tokens()
            total_alerts += alerts
            logger.info("Discovery: %d alerts", alerts)
        except Exception as e:
            logger.error("Discovery error: %s", e, exc_info=True)

        # Module 4: Whale Monitor
        try:
            logger.info("--- Whale Monitor ---")
            whale = WhaleMonitor(config)
            alerts = whale.poll_once()
            total_alerts += alerts
            logger.info("Whale monitor: %d alerts", alerts)
        except Exception as e:
            logger.error("Whale monitor error: %s", e, exc_info=True)

        # Module 5: Zombie Detector
        try:
            logger.info("--- Zombie Detector ---")
            zombie = ZombieDetector(config)
            alerts = zombie.poll_once()
            total_alerts += alerts
            logger.info("Zombie detector: %d alerts", alerts)
        except Exception as e:
            logger.error("Zombie detector error: %s", e, exc_info=True)

        # Module 6: Liquidity Flow
        try:
            logger.info("--- Liquidity Flow ---")
            flow = LiquidityFlowAnalyzer(config)
            alerts = flow.poll_once()
            total_alerts += alerts
            logger.info("Liquidity flow: %d alerts", alerts)
        except Exception as e:
            logger.error("Liquidity flow error: %s", e, exc_info=True)

        # Module 7: Reversal Detector
        try:
            logger.info("--- Reversal Detector ---")
            reversal = ReversalDetector(config)
            alerts = reversal.poll_once()
            total_alerts += alerts
            logger.info("Reversal detector: %d alerts", alerts)
        except Exception as e:
            logger.error("Reversal detector error: %s", e, exc_info=True)

        # Module 8: Robinhood Runner Radar
        try:
            logger.info("--- Robinhood Runner Radar ---")
            total_alerts += _run_runner_radar_once(config)
        except Exception as e:
            logger.error("Runner radar error: %s", e, exc_info=True)

        # Module 9: Base Chain Scanner (journal observations; optional entry-ready Telegram)
        if (config.get("base_scanner", {}) or {}).get("enabled", False):
            try:
                logger.info("--- Base Chain Scanner (dry-run journal) ---")
                _run_base_scanner_once(config)
            except Exception as e:
                logger.error("Base scanner error: %s", e, exc_info=True)

        # Module 10: Monad Chain Scanner (journal observations; optional entry-ready Telegram)
        if (config.get("monad_scanner", {}) or {}).get("enabled", False):
            try:
                logger.info("--- Monad Chain Scanner (dry-run journal) ---")
                _run_monad_scanner_once(config)
            except Exception as e:
                logger.error("Monad scanner error: %s", e, exc_info=True)

        logger.info("=== All modules complete: %d total Telegram alerts sent ===", total_alerts)
        return

    # Full multi-module loop
    logger.info("Starting Catecoin Multi-Scanner (6 modules)")

    # Initialize all modules
    price_scanner = CatecoinScanner(config)
    price_scanner.init_baseline()

    smart_money = SmartMoneyTracker(config)
    discovery = TokenDiscovery(config)
    whale_monitor = WhaleMonitor(config)
    zombie_detector = ZombieDetector(config)
    liquidity_flow = LiquidityFlowAnalyzer(config)
    reversal_detector = ReversalDetector(config)

    price_interval = config.get("poll_interval_seconds", 60)
    sm_interval = config.get("smart_money", {}).get("poll_interval_seconds", 300)
    disc_interval = config.get("discovery", {}).get("poll_interval_seconds", 600)
    whale_interval = config.get("whale_monitor", {}).get("poll_interval_seconds", 300)
    zombie_interval = config.get("zombie_detector", {}).get("poll_interval_seconds", 1800)
    liq_interval = config.get("liquidity_flow", {}).get("poll_interval_seconds", 600)
    reversal_interval = config.get("reversal", {}).get("poll_interval_seconds", 900)
    base_interval = config.get("base_scanner", {}).get("poll_interval_seconds", 900)
    monad_interval = config.get("monad_scanner", {}).get("poll_interval_seconds", 900)
    runner_interval = config.get("runner_radar", {}).get("poll_interval_seconds", 120)
    analyzer_interval = int(float(config.get("alert_analyzer", {}).get("interval_hours", 4)) * 3600)

    last_sm = 0.0
    last_disc = 0.0
    last_whale = 0.0
    last_zombie = 0.0
    last_liq = 0.0
    last_reversal = 0.0
    last_base = 0.0
    last_monad = 0.0
    last_runner = 0.0
    last_analyzer = 0.0

    logger.info(
        "Intervals: price=%ds sm=%ds disc=%ds whale=%ds zombie=%ds liq=%ds runner=%ds base=%ds monad=%ds analyzer=%ds",
        price_interval, sm_interval, disc_interval, whale_interval, zombie_interval, liq_interval, runner_interval, base_interval, monad_interval, analyzer_interval,
    )

    while True:
        now = time.time()

        # Price monitor (every cycle)
        try:
            price_scanner.poll_once()
        except Exception as e:
            logger.error("Price error: %s", e)

        # Smart money
        if now - last_sm >= sm_interval:
            try:
                alerts = smart_money.scan_all_wallets()
                if alerts:
                    logger.info("Smart money: %d alerts", alerts)
            except Exception as e:
                logger.error("Smart money error: %s", e)
            last_sm = now

        # Discovery
        if now - last_disc >= disc_interval:
            try:
                alerts = discovery.scan_new_tokens()
                if alerts:
                    logger.info("Discovery: %d alerts", alerts)
            except Exception as e:
                logger.error("Discovery error: %s", e)
            last_disc = now

        # Whale monitor
        if now - last_whale >= whale_interval:
            try:
                alerts = whale_monitor.poll_once()
                if alerts:
                    logger.info("Whale monitor: %d alerts", alerts)
            except Exception as e:
                logger.error("Whale monitor error: %s", e)
            last_whale = now

        # Zombie detector
        if now - last_zombie >= zombie_interval:
            try:
                alerts = zombie_detector.poll_once()
                if alerts:
                    logger.info("Zombie detector: %d alerts", alerts)
            except Exception as e:
                logger.error("Zombie detector error: %s", e)
            last_zombie = now

        # Liquidity flow
        if now - last_liq >= liq_interval:
            try:
                alerts = liquidity_flow.poll_once()
                if alerts:
                    logger.info("Liquidity flow: %d alerts", alerts)
            except Exception as e:
                logger.error("Liquidity flow error: %s", e)
            last_liq = now

        # Reversal detector
        if now - last_reversal >= reversal_interval:
            try:
                alerts = reversal_detector.poll_once()
                if alerts:
                    logger.info("Reversal detector: %d alerts", alerts)
            except Exception as e:
                logger.error("Reversal detector error: %s", e)
            last_reversal = now

        # Robinhood runner radar (journal all, Telegram only transitions)
        if now - last_runner >= runner_interval:
            try:
                alerts = _run_runner_radar_once(config)
                if alerts:
                    logger.info("Runner radar: %d alerts", alerts)
            except Exception as e:
                logger.error("Runner radar error: %s", e)
            last_runner = now

        # Base chain scanner (journal observations; optional entry-ready Telegram)
        if now - last_base >= base_interval:
            try:
                _run_base_scanner_once(config)
            except Exception as e:
                logger.error("Base scanner error: %s", e)
            last_base = now

        # Monad chain scanner (journal observations; optional entry-ready Telegram)
        if now - last_monad >= monad_interval:
            try:
                _run_monad_scanner_once(config)
            except Exception as e:
                logger.error("Monad scanner error: %s", e)
            last_monad = now

        # Alert self-improvement analyzer (4-hour cycle)
        if now - last_analyzer >= analyzer_interval:
            try:
                result = _run_alert_analyzer_once(config)
                if result and result.get("alerts_analyzed", 0) > 0:
                    logger.info("Alert analyzer: %d alerts analyzed, %d recommendations",
                                result.get("alerts_analyzed", 0), len(result.get("recommendations", [])))
            except Exception as e:
                logger.error("Alert analyzer error: %s", e)
            last_analyzer = now

        time.sleep(price_interval)


def _run_function_loop(name: str, func, interval: int):
    """Run a stateless scanner function in a continuous loop."""
    logger.info("%s started (interval=%ds)", name, interval)
    while True:
        try:
            func()
        except Exception as e:
            logger.error("%s error: %s", name, e)
        time.sleep(interval)


def _run_module_loop(name: str, module, interval: int):
    """Run a single module in a continuous loop."""
    logger.info("%s started (interval=%ds)", name, interval)
    while True:
        try:
            if hasattr(module, "scan_all_wallets"):
                module.scan_all_wallets()
            elif hasattr(module, "scan_new_tokens"):
                module.scan_new_tokens()
            elif hasattr(module, "poll_once"):
                module.poll_once()
            elif hasattr(module, "run_loop"):
                module.run_loop()
                break  # run_loop is its own loop
        except Exception as e:
            logger.error("%s error: %s", name, e, exc_info=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
