#!/usr/bin/env python3
"""Smart Money Tracking Module.

Polls tracked smart-money wallets on Robinhood Chain via Blockscout (free API).
Detects new token purchases, tracks consensus across wallets, and sends
Telegram alerts when smart wallets converge on a token.

Usage (standalone):
    python smart_money.py --once     # Single scan then exit
    python smart_money.py            # Continuous loop (5 min intervals)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
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

logger = logging.getLogger("catecoin-scanner.smart_money")

DEXSCREENER_CHART = "https://dexscreener.com/robinhood/{pair}"
BLOCKSCOUT_TOKEN = "https://robinhoodchain.blockscout.com/token/{addr}"
CONSENSUS_WINDOW = 1800  # 30 minutes


def load_config(config_path: str) -> dict:
    if not config_path or not Path(config_path).exists():
        return {}
    if not HAS_YAML:
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def load_wallets(wallets_file: str) -> List[Dict[str, Any]]:
    """Load wallet watchlist from JSON."""
    path = Path(wallets_file)
    if not path.exists():
        logger.error("Wallets file not found: %s", wallets_file)
        return []
    with open(path) as f:
        data = json.load(f)
    wallets = data.get("wallets", [])
    logger.info("Loaded %d tracked wallets from %s", len(wallets), wallets_file)
    return wallets


class SmartMoneyTracker:
    """Tracks smart-money wallet activity and detects token convergence."""

    def __init__(self, config: dict) -> None:
        sm_cfg = config.get("smart_money", {})
        base_dir = Path(__file__).parent

        self.enabled = sm_cfg.get("enabled", True)
        self.poll_interval = sm_cfg.get("poll_interval_seconds", 300)
        wallets_file = sm_cfg.get("wallets_file", "smart_wallets.json")
        self.wallets_file = str(base_dir / wallets_file) if not os.path.isabs(wallets_file) else wallets_file
        self.consensus_strong = sm_cfg.get("consensus_strong_30min", 6)
        self.consensus_moderate = sm_cfg.get("consensus_moderate_30min", 3)

        bs_base = sm_cfg.get("blockscout_base", "https://robinhoodchain.blockscout.com/api/v2")
        self.blockscout = BlockscoutClient(base_url=bs_base)
        self.dex = DexScreenerClient()
        self.alerter = TelegramAlerter.from_config(config)

        self.wallets: List[Dict[str, Any]] = load_wallets(self.wallets_file)

        # State — per-wallet last seen timestamp (ISO string from Blockscout)
        self.last_seen_ts: Dict[str, str] = {}
        # Per-wallet known token addresses (to avoid re-alerting)
        self.known_tokens: Dict[str, Set[str]] = {}
        # Consensus: token_addr -> {wallets: set, timestamps: [(ts_str, wallet_label)]}
        self.consensus: Dict[str, Dict[str, Any]] = {}
        # Already alerted consensus tokens (avoid spam)
        self.consensus_alerted: Dict[str, str] = {}  # token_addr -> level alerted

        self._init_known_tokens()

    def _init_known_tokens(self) -> None:
        """Seed known_tokens with each wallet's current token holdings."""
        logger.info("Seeding known tokens for %d wallets (this may take a moment)...", len(self.wallets))
        for w in self.wallets:
            addr = w.get("address", "")
            if not addr:
                continue
            self.known_tokens[addr] = set()
            # Seed with current transfers to avoid alerting on existing holdings
            data = self.blockscout.get_address_transfers(addr, params={"limit": 50})
            if data and data.get("items"):
                for item in data["items"]:
                    token_addr = self._extract_token_address(item)
                    if token_addr:
                        self.known_tokens[addr].add(token_addr.lower())
                    ts = self._extract_timestamp(item)
                    if ts:
                        self._update_last_seen(addr, ts)
            logger.debug("  %s: %d known tokens", addr[:10], len(self.known_tokens[addr]))
        logger.info("Seeding complete")

    @staticmethod
    def _extract_token_address(transfer: dict) -> Optional[str]:
        token = transfer.get("token") or {}
        return token.get("address")

    @staticmethod
    def _extract_timestamp(transfer: dict) -> Optional[str]:
        # Blockscout may put timestamp at top level or inside transaction
        ts = transfer.get("timestamp")
        if ts:
            return ts
        tx = transfer.get("transaction") or {}
        return tx.get("timestamp")

    @staticmethod
    def _extract_value(transfer: dict) -> float:
        total = transfer.get("total") or {}
        raw = total.get("value", "0")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _ts_to_epoch(ts_str: str) -> float:
        """Parse ISO timestamp to epoch seconds."""
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return dt.timestamp()
        except (ValueError, TypeError):
            return 0.0

    def _update_last_seen(self, addr: str, ts: str) -> None:
        old = self.last_seen_ts.get(addr)
        if old is None or self._ts_to_epoch(ts) > self._ts_to_epoch(old):
            self.last_seen_ts[addr] = ts

    def scan_all_wallets(self) -> int:
        """Scan all tracked wallets. Returns number of alerts sent."""
        alerts_sent = 0
        for w in self.wallets:
            try:
                alerts_sent += self._scan_wallet(w)
            except Exception as e:
                logger.error("Error scanning %s: %s", w.get("address", "?")[:12], e, exc_info=True)

        # Check consensus after all wallets scanned
        alerts_sent += self._check_consensus()
        return alerts_sent

    def _scan_wallet(self, wallet: dict) -> int:
        """Scan a single wallet for new token purchases."""
        addr = wallet.get("address", "")
        label = wallet.get("label", addr[:10])
        tier = wallet.get("tier", "unknown")

        if not addr:
            return 0

        last_ts = self.last_seen_ts.get(addr)
        data = self.blockscout.get_address_transfers(addr, params={"limit": 50})
        if not data or not data.get("items"):
            return 0

        alerts_sent = 0
        known = self.known_tokens.setdefault(addr, set())

        for item in data["items"]:
            token_addr = self._extract_token_address(item)
            ts = self._extract_timestamp(item)
            to_addr = ((item.get("to") or {}).get("hash") or "").lower()

            if ts:
                self._update_last_seen(addr, ts)

            # Only track INCOMING transfers (wallet receiving tokens = buy)
            if to_addr != addr.lower():
                continue
            if not token_addr:
                continue

            token_addr_lower = token_addr.lower()

            # Skip tokens we've already seen for this wallet
            if token_addr_lower in known:
                # Still track consensus even if already known
                self._track_consensus(token_addr_lower, addr, label, ts)
                continue

            # NEW token purchase detected
            known.add(token_addr_lower)
            logger.info("🆕 %s (%s) bought new token: %s", label, tier, token_addr[:12])

            # Enrich and alert
            self._track_consensus(token_addr_lower, addr, label, ts)
            sent = self._alert_new_buy(wallet, item, token_addr)
            alerts_sent += 1 if sent else 0

        return alerts_sent

    def _track_consensus(self, token_addr: str, wallet_addr: str, label: str, ts: str) -> None:
        """Record a buy in the consensus tracker."""
        entry = self.consensus.setdefault(token_addr, {"wallets": set(), "timestamps": []})
        entry["wallets"].add(wallet_addr.lower())
        ts_epoch = self._ts_to_epoch(ts) if ts else time.time()
        entry["timestamps"].append((ts_epoch, label))

    def _check_consensus(self) -> int:
        """Check all tracked tokens for consensus signals."""
        alerts_sent = 0
        now = time.time()
        cutoff = now - CONSENSUS_WINDOW

        for token_addr, entry in list(self.consensus.items()):
            # Prune old timestamps
            entry["timestamps"] = [(t, l) for t, l in entry["timestamps"] if t >= cutoff]
            if not entry["timestamps"]:
                continue

            unique_wallets = {w for w in entry["wallets"]}
            count = len(unique_wallets)

            already = self.consensus_alerted.get(token_addr)

            if count >= self.consensus_strong and already != "strong":
                self._alert_consensus(token_addr, entry, "strong")
                self.consensus_alerted[token_addr] = "strong"
                alerts_sent += 1
            elif count >= self.consensus_moderate and already not in ("strong", "moderate"):
                self._alert_consensus(token_addr, entry, "moderate")
                self.consensus_alerted[token_addr] = "moderate"
                alerts_sent += 1

        return alerts_sent

    def _alert_new_buy(self, wallet: dict, transfer: dict, token_addr: str) -> bool:
        """Send alert for a wallet buying a new token."""
        label = wallet.get("label", wallet.get("address", "?")[:10])
        tier = wallet.get("tier", "unknown")

        # Fetch token info + DexScreener data
        token_info = self.blockscout.get_token_info(token_addr)
        pair = self.dex.get_token(token_addr)

        token_symbol = ((token_info or {}).get("symbol") or
                        (pair or {}).get("baseToken", {}).get("symbol", "???"))
        token_name = ((token_info or {}).get("name") or
                      (pair or {}).get("baseToken", {}).get("name", "Unknown"))
        holders = (token_info or {}).get("holders", 0) or 0

        price = float((pair or {}).get("priceUsd", 0) or 0)
        liquidity = float((pair or {}).get("liquidity", {}).get("usd", 0) or 0)
        volume = float((pair or {}).get("volume", {}).get("h24", 0) or 0)
        change = float((pair or {}).get("priceChange", {}).get("h24", 0) or 0)
        pair_addr = (pair or {}).get("pairAddress", "")

        # Value (raw token amount — convert if decimals available)
        decimals = (token_info or {}).get("decimals", 18) or 18
        raw_value = self._extract_value(transfer)
        token_amount = raw_value / (10 ** decimals) if raw_value > 0 else 0
        value_eth = float((pair or {}).get("priceNative", 0) or 0) * token_amount

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        chart_url = DEXSCREENER_CHART.format(pair=pair_addr) if pair_addr else f"https://dexscreener.com/robinhood/{token_addr}"
        explorer_url = BLOCKSCOUT_TOKEN.format(addr=token_addr)

        msg = (
            f"🐋 <b>SMART MONEY ALERT</b>\n\n"
            f"<b>{label}</b> ({tier}) bought:\n"
            f"💰 <b>{token_symbol}</b> ({token_name})\n"
            f"📍 CA: <code>{token_addr}</code>\n"
            f"💵 Amount: {token_amount:,.2f} ({value_eth:.4f} ETH)\n\n"
            f"📊 <b>Token Stats</b>\n"
            f"• Price: ${price:.8f}\n"
            f"• Liquidity: ${liquidity:,.0f}\n"
            f"• 24h Volume: ${volume:,.0f}\n"
            f"• Holders: {holders:,}\n"
            f"• 24h Change: {change:+.1f}%\n\n"
            f'🔗 <a href="{chart_url}">Chart</a> | <a href="{explorer_url}">Explorer</a>\n\n'
            f"⏰ {ts}"
        )
        return self.alerter.send(msg)

    def _alert_consensus(self, token_addr: str, entry: dict, level: str) -> bool:
        """Send consensus alert."""
        wallet_labels = [l for _, l in entry["timestamps"]]
        unique_labels = list(dict.fromkeys(wallet_labels))  # preserve order, dedupe
        count = len(entry["wallets"])

        # Get token symbol
        token_info = self.blockscout.get_token_info(token_addr)
        symbol = (token_info or {}).get("symbol", token_addr[:8])

        emoji = "🎯" if level == "strong" else "📊"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        msg = (
            f"{emoji} <b>CONSENSUS SIGNAL: {count} smart wallets buying {symbol}!</b>\n\n"
            f"Wallets: {', '.join(unique_labels)}\n"
            f"CA: <code>{token_addr}</code>\n"
            f"Signal level: {level.upper()}\n"
            f"30-min window consensus triggered\n\n"
            f'🔗 <a href="https://dexscreener.com/robinhood/{token_addr}">Chart</a> | '
            f'<a href="https://robinhoodchain.blockscout.com/token/{token_addr}">Explorer</a>\n\n'
            f"⏰ {ts}"
        )
        return self.alerter.send(msg)

    def run_loop(self) -> None:
        """Continuous monitoring loop."""
        logger.info("Smart money tracker started | %d wallets | interval=%ds", len(self.wallets), self.poll_interval)
        while True:
            try:
                self.scan_all_wallets()
            except Exception as e:
                logger.error("Smart money cycle error: %s", e, exc_info=True)
            time.sleep(self.poll_interval)


def main():
    parser = argparse.ArgumentParser(description="Smart Money Tracker")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--once", action="store_true", help="Single scan then exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = load_config(args.config)
    tracker = SmartMoneyTracker(config)

    if args.once:
        alerts = tracker.scan_all_wallets()
        logger.info("Smart money scan complete: %d alerts sent", alerts)
    else:
        tracker.run_loop()


if __name__ == "__main__":
    main()
