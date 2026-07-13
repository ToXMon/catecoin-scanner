#!/usr/bin/env python3
"""Early Token Detection + Zombie Revival Module — Enhanced with Alpha Scoring.

Discovers new and reviving tokens on Robinhood Chain using only free APIs.
Solves the core noise problem: derivative/clone token filtering + composite alpha scoring.

Enhancements over v1:
- Derivative detection: filters clones of known memecoins (Cate, Doge, CashCat variants)
- Alpha scoring (0-100): composite score, only alert tokens scoring > threshold
- Contract safety: Blockscout verification + holder concentration + LP lock checks
- Stricter filters: min liquidity, min holders, early-stage (<24h) preference
- Bot detection preserved

Sources:
- DexScreener trending search (free)
- Blockscout newest tokens list (free)
- Bot pattern detection (preserved from v1)
- Zombie revival detection (preserved from v1)

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
from alpha_scorer import (
    DerivativeDetector,
    AlphaScorer,
    is_early_stage,
    get_token_age_hours,
)
from contract_safety import ContractSafetyChecker
from alchemy_client import AlchemyClient

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
    """Detect bot-like buying patterns (preserved from v1)."""
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
    if len(amounts) > 1 and len(set(amounts)) == 1 and amounts[0] > 0:
        return True
    return False


class TokenDiscovery:
    """Discovers new tokens and detects zombie revivals on Robinhood Chain.

    Enhanced with derivative detection, alpha scoring, and contract safety checks.
    """

    def __init__(self, config: dict) -> None:
        disc_cfg = config.get("discovery", {})

        self.enabled = disc_cfg.get("enabled", True)
        self.poll_interval = disc_cfg.get("poll_interval_seconds", 600)
        self.min_liquidity = disc_cfg.get("min_liquidity_usd", 5000)
        self.min_volume_24h = disc_cfg.get("min_volume_24h", 10000)
        self.min_holders = disc_cfg.get("min_holders", 10)
        self.zombie_dormancy_days = disc_cfg.get("zombie_dormancy_days", 7)
        self.zombie_spike_mult = disc_cfg.get("zombie_volume_spike_multiplier", 3.0)

        # Alpha scoring config
        alpha_cfg = disc_cfg.get("alpha", {})
        self.min_alpha_score = alpha_cfg.get("min_alpha_score", 50)
        self.max_age_hours = alpha_cfg.get("max_age_hours", 24)
        self.enable_derivative_filter = alpha_cfg.get("enable_derivative_filter", True)
        self.enable_safety_check = alpha_cfg.get("enable_safety_check", True)

        bs_base = disc_cfg.get("blockscout_base", "https://robinhoodchain.blockscout.com/api/v2")
        self.blockscout = BlockscoutClient(base_url=bs_base)
        self.dex = DexScreenerClient()
        self.alerter = TelegramAlerter.from_config(config)

        # Alchemy (PRIMARY data source for real-time new token transfer detection)
        alch_cfg = config.get("alchemy", {}) or {}
        self.alchemy = AlchemyClient(
            api_key=alch_cfg.get("api_key"),
            network=alch_cfg.get("network", "robinhood-mainnet"),
            cu_warning_threshold=alch_cfg.get("cu_warning_threshold", 0.8),
            cu_monthly_limit=alch_cfg.get("cu_monthly_limit", 30_000_000),
        )

        # New modules
        self.derivative_detector = DerivativeDetector()
        self.alpha_scorer = AlphaScorer(
            min_liquidity=self.min_liquidity,
            min_holders=self.min_holders,
            min_alpha_score=self.min_alpha_score,
        )
        self.safety_checker = ContractSafetyChecker(self.blockscout)

        # Register known tokens for derivative detection
        self._register_known_base_tokens()

        # Track known tokens to avoid re-alerting
        self.known_tokens: Set[str] = set()
        # Track token first-seen timestamps for zombie detection
        self.token_first_seen: Dict[str, float] = {}
        # Track token metrics history for growth calculation
        self.token_history: Dict[str, Dict[str, Any]] = {}

    def _register_known_base_tokens(self) -> None:
        """Register existing well-known tokens for derivative detection."""
        # Register the base names in DERIVATIVE_BASE_NAMES with the detector
        # Also register Cate and CashCat specifically since they're the main clones
        known_tokens = [
            ("CATE", "Catecoin"),
            ("CASHCAT", "Cash Cat"),
            ("DOGE", "Dogecoin"),
            ("SHIB", "Shiba Inu"),
            ("PEPE", "Pepe"),
            ("NOXA", "Noxa"),
            ("VLAD", "Vlad"),
        ]
        for symbol, name in known_tokens:
            self.derivative_detector.register_existing_token(symbol, name)

    def scan_new_tokens(self) -> int:
        """Main scan: PRIMARY Alchemy new-contract detection + secondary Blockscout/DexScreener."""
        alerts = 0

        # ---- PRIMARY PATH: Alchemy new token contract detection ----
        try:
            alerts += self._scan_alchemy_new_contracts()
        except Exception as e:
            logger.warning("Alchemy new contract scan failed (degraded to Blockscout): %s", e)

        alerts += self._scan_dexscreener_trending()
        alerts += self._scan_blockscout_new()
        return alerts

    def _scan_alchemy_new_contracts(self) -> int:
        """PRIMARY: detect brand-new token contracts via Alchemy transfer indexing.

        Queries recent ERC20 transfers chain-wide, extracts unique token
        contracts, and flags any that aren't yet in our known_tokens set.
        Cross-references with DexScreener for liquidity/price validation.
        This catches new tokens BEFORE they appear on Blockscout's lagging
        /tokens endpoint.
        """
        alerts = 0

        # Get a broad sweep of recent ERC20 transfers chain-wide
        try:
            transfers = self.alchemy.get_asset_transfers(
                category=["erc20"],
                max_count=100,
                order="desc",
            )
        except Exception as e:
            logger.warning("Alchemy chain-wide transfer query failed: %s", e)
            return 0

        if not transfers:
            return 0

        # Extract unique token contracts we haven't seen before
        new_contracts: Dict[str, List[Dict[str, Any]]] = {}
        for t in transfers:
            token_addr = (t.get("token_contract") or "").lower()
            if not token_addr:
                continue
            # Skip native / zero address
            if token_addr == "0x0000000000000000000000000000000000000000":
                continue
            if token_addr in self.known_tokens:
                continue
            new_contracts.setdefault(token_addr, []).append(t)

        if not new_contracts:
            return 0

        logger.info(
            "Alchemy new-contract scan: %d new token contracts detected",
            len(new_contracts),
        )

        # Evaluate each new contract via DexScreener for liquidity + alpha scoring
        for token_addr, token_transfers in new_contracts.items():
            self.known_tokens.add(token_addr)
            self.token_first_seen[token_addr] = time.time()

            try:
                pair = self.dex.get_token(token_addr) or {}
            except Exception:
                continue

            if not pair:
                continue

            symbol = (pair.get("baseToken") or {}).get("symbol", "???")
            name = (pair.get("baseToken") or {}).get("name", "Unknown")

            # Derivative filter (unless very early stage with significant volume)
            if self.enable_derivative_filter:
                is_deriv, reason = self.derivative_detector.is_derivative(symbol, name)
                if is_deriv:
                    logger.debug("Filtered Alchemy new contract derivative: %s (%s)", symbol, reason)
                    continue

            # Alpha score + safety check (delegating to existing eval method)
            if self._evaluate_early_token(pair, token_addr):
                alerts += 1

            # Track token history for growth metrics
            self._update_token_history(token_addr, pair)

            time.sleep(0.1)  # Rate limit DexScreener calls

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
        deriv_filtered = 0

        for pair in pairs[:20]:
            token_addr = (pair.get("baseToken") or {}).get("address", "")
            if not token_addr:
                continue
            token_addr = token_addr.lower()

            if token_addr in self.known_tokens:
                # Still update history for growth tracking
                self._update_token_history(token_addr, pair)
                continue

            symbol = (pair.get("baseToken") or {}).get("symbol", "???")
            name = (pair.get("baseToken") or {}).get("name", "Unknown")

            # EARLY derivative filter — skip clones before any expensive API calls
            if self.enable_derivative_filter:
                is_deriv, deriv_reason = self.derivative_detector.is_derivative(symbol, name)
                if is_deriv:
                    deriv_filtered += 1
                    logger.debug("  Filtered derivative: %s (%s) — %s", symbol, token_addr[:10], deriv_reason)
                    self.known_tokens.add(token_addr)
                    continue

            self.known_tokens.add(token_addr)
            self.token_first_seen[token_addr] = time.time()
            self._update_token_history(token_addr, pair)

            if self._evaluate_early_token(pair, token_addr):
                alerts += 1

            # Check for zombie revival
            if self._check_zombie(token_addr, pair):
                alerts += 1

        if deriv_filtered > 0:
            logger.info("  Filtered %d derivative/clone tokens", deriv_filtered)

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
        deriv_filtered = 0

        for token in tokens:
            token_addr = (token.get("address") or {}).get("hash", "")
            if not token_addr:
                continue
            token_addr = token_addr.lower()

            if token_addr in self.known_tokens:
                continue

            symbol = token.get("symbol", "???")
            name = token.get("name", "Unknown")

            # EARLY derivative filter
            if self.enable_derivative_filter:
                is_deriv, deriv_reason = self.derivative_detector.is_derivative(symbol, name)
                if is_deriv:
                    deriv_filtered += 1
                    logger.debug("  Filtered derivative: %s (%s) — %s", symbol, token_addr[:10], deriv_reason)
                    self.known_tokens.add(token_addr)
                    continue

            self.known_tokens.add(token_addr)
            self.token_first_seen[token_addr] = time.time()

            # Fetch DexScreener data for this token
            pair = self.dex.get_token(token_addr)
            if not pair:
                logger.debug("  %s: no DexScreener data yet (may be very new)", token_addr[:10])
                continue

            self._update_token_history(token_addr, pair)

            if self._evaluate_early_token(pair, token_addr):
                alerts += 1

        if deriv_filtered > 0:
            logger.info("  Filtered %d derivative/clone tokens", deriv_filtered)

        return alerts

    def _update_token_history(self, token_addr: str, pair: dict) -> None:
        """Track token metrics over time for growth calculation."""
        liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        volume = float(pair.get("volume", {}).get("h24", 0) or 0)
        now = time.time()

        history = self.token_history.setdefault(token_addr, {"snapshots": []})
        history["snapshots"].append({
            "ts": now,
            "liquidity": liquidity,
            "volume": volume,
        })
        # Keep only last 10 snapshots
        if len(history["snapshots"]) > 10:
            history["snapshots"] = history["snapshots"][-10:]

    def _calculate_growth(self, token_addr: str, metric: str = "liquidity") -> float:
        """Calculate growth percentage for a metric. Returns 0 if insufficient data."""
        history = self.token_history.get(token_addr, {})
        snapshots = history.get("snapshots", [])
        if len(snapshots) < 2:
            return 0.0
        first = snapshots[0].get(metric, 0)
        last = snapshots[-1].get(metric, 0)
        if first <= 0:
            return 0.0
        return ((last - first) / first) * 100

    def _evaluate_early_token(self, pair: dict, token_addr: str) -> bool:
        """Check if a new token meets alpha criteria with enhanced filtering.

        Pipeline:
        1. Derivative check (heavy penalty)
        2. Hard filters (liquidity, holders, age)
        3. Contract safety check
        4. Bot detection
        5. Alpha score calculation
        6. Alert only if score > threshold
        """
        symbol = (pair.get("baseToken") or {}).get("symbol", "???")
        name = (pair.get("baseToken") or {}).get("name", "Unknown")
        price = float(pair.get("priceUsd", 0) or 0)
        liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        volume_h24 = float(pair.get("volume", {}).get("h24", 0) or 0)
        change_h24 = float(pair.get("priceChange", {}).get("h24", 0) or 0)
        fdv = float(pair.get("fdv", 0) or 0)
        pair_addr = pair.get("pairAddress", token_addr)

        # ─── Step 1: Derivative check ───
        is_deriv, deriv_reason = (False, "")
        if self.enable_derivative_filter:
            is_deriv, deriv_reason = self.derivative_detector.is_derivative(symbol, name)
            if is_deriv:
                logger.info("  🚫 Filtered derivative: %s — %s", symbol, deriv_reason)
                return False

        # ─── Step 2: Hard filters ───
        # Liquidity check
        if liquidity < self.min_liquidity:
            logger.debug("  %s: liquidity $%.0f < min $%.0f", symbol, liquidity, self.min_liquidity)
            return False

        # Token holders from Blockscout
        token_info = self.blockscout.get_token_info(token_addr)
        holders = (token_info or {}).get("holders", 0) or 0

        if holders < self.min_holders:
            logger.debug("  %s: holders %d < min %d", symbol, holders, self.min_holders)
            return False

        # Age check — only alert tokens < max_age_hours (default 24h)
        age_hours = get_token_age_hours(pair)
        if age_hours is not None and age_hours > self.max_age_hours:
            logger.debug("  %s: age %.1fh > max %.1fh", symbol, age_hours, self.max_age_hours)
            # Don't return False — zombie check may still catch older tokens

        # ─── Step 3: Contract safety check ───
        contract_verified = None
        mint_authority = None
        safety_report = None
        if self.enable_safety_check:
            safety_report = self.safety_checker.full_safety_check(
                token_addr, pair_data=pair, token_info=token_info,
                min_liquidity=self.min_liquidity,
            )
            contract_verified = safety_report.get("contract_verified")
            mint_authority = safety_report.get("mint_authority")

        # ─── Step 4: Bot detection ───
        bot_detected = False
        transfers_data = self.blockscout.get_token_transfers(token_addr, params={"limit": 30})
        if transfers_data and transfers_data.get("items"):
            txs = transfers_data["items"]
            for tx in txs[:10]:
                buyer = ((tx.get("to") or {}).get("hash") or "")
                if buyer and is_bot_pattern(txs, buyer):
                    bot_detected = True
                    break

        # ─── Step 5: Growth metrics ───
        liquidity_growth = self._calculate_growth(token_addr, "liquidity")
        # Holder growth (would need historical holder data — use volume growth as proxy)
        volume_growth = self._calculate_growth(token_addr, "volume")
        holder_growth = volume_growth * 0.5  # Approximation

        # ─── Step 6: Alpha score calculation ───
        alpha_result = self.alpha_scorer.score(
            symbol=symbol,
            name=name,
            token_addr=token_addr,
            price=price,
            liquidity=liquidity,
            volume_24h=volume_h24,
            holders=holders,
            price_change_24h=change_h24,
            smart_money_buyers=0,  # Discovery doesn't track smart money — that's smart_money.py
            liquidity_growth_pct=liquidity_growth,
            holder_growth_pct=holder_growth,
            contract_verified=contract_verified,
            mint_authority=mint_authority,
            bot_detected=bot_detected,
            is_derivative=is_deriv,
            derivative_reason=deriv_reason,
            pair_data=pair,
        )

        logger.info(
            "  📊 %s: alpha=%d/100 [%s] | liq=$%.0f vol=$%.0f holders=%d bot=%s deriv=%s",
            symbol, alpha_result["alpha_score"], alpha_result["verdict"],
            liquidity, volume_h24, holders, bot_detected, is_deriv,
        )

        # ─── Step 7: Alert only if passes threshold ───
        if not alpha_result["pass_threshold"]:
            logger.debug("  %s: alpha %d < threshold %d [%s]",
                        symbol, alpha_result["alpha_score"], self.min_alpha_score,
                        alpha_result["verdict"])
            return False

        return self._alert_alpha_token(
            symbol, name, token_addr, price, fdv, liquidity, change_h24,
            holders, pair, alpha_result, safety_report, bot_detected, age_hours
        )

    def _check_zombie(self, token_addr: str, pair: dict) -> bool:
        """Check for zombie revival pattern (dormant token waking up).

        Preserved from v1 but now also applies derivative filter.
        """
        # Apply derivative filter to zombie checks too
        symbol = (pair.get("baseToken") or {}).get("symbol", "???")
        name = (pair.get("baseToken") or {}).get("name", "Unknown")
        if self.enable_derivative_filter:
            is_deriv, _ = self.derivative_detector.is_derivative(symbol, name)
            if is_deriv:
                return False

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

        if age_days < self.zombie_dormancy_days:
            return False

        volume = pair.get("volume") or {}
        vol_m5 = float(volume.get("m5", 0) or 0)
        vol_h24 = float(volume.get("h24", 0) or 0)
        if vol_h24 <= 0:
            return False

        hourly_avg = vol_h24 / 24
        if hourly_avg <= 0:
            return False
        spike_ratio = vol_m5 / (hourly_avg / 12)

        if spike_ratio < self.zombie_spike_mult:
            return False

        liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        if liquidity < self.min_liquidity:
            return False

        price = float(pair.get("priceUsd", 0) or 0)
        change_h1 = float(pair.get("priceChange", {}).get("h1", 0) or 0)

        return self._alert_zombie(
            symbol, token_addr, age_days, spike_ratio, change_h1,
            liquidity, vol_m5, price
        )

    def _alert_alpha_token(
        self, symbol: str, name: str, token_addr: str, price: float,
        mcap: float, liquidity: float, change: float, holders: int,
        pair: dict, alpha_result: dict, safety_report: Optional[dict],
        bot_detected: bool, age_hours: Optional[float],
    ) -> bool:
        """Send Telegram alert for a high-alpha token discovery."""
        pair_addr = pair.get("pairAddress", token_addr)
        txns = pair.get("txns", {}).get("m5", {})
        buys = txns.get("buys", 0)
        sells = txns.get("sells", 0)
        volume = float(pair.get("volume", {}).get("h24", 0) or 0)

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if age_hours is not None:
            age_str = f"{age_hours:.1f}h" if age_hours < 24 else f"{age_hours/24:.1f}d"
        else:
            age_str = "unknown"

        chart_url = DEXSCREENER_CHART.format(addr=pair_addr)
        explorer_url = BLOCKSCOUT_TOKEN.format(addr=token_addr)

        # Alpha score breakdown
        alpha_text = self.alpha_scorer.format_score_breakdown(alpha_result)

        # Safety report
        safety_text = ""
        if safety_report:
            safety_text = self.safety_checker.format_safety_alert(safety_report)

        bot_warn = "\n⚠️ <b>Bot activity detected</b>" if bot_detected else ""

        msg = (
            f"🚀 <b>ALPHA DETECTED: {symbol}</b>\n\n"
            f"💰 <b>{name}</b>\n"
            f"📍 CA: <code>{token_addr}</code>\n"
            f"💵 Price: ${price:.8f}\n"
            f"📊 MC: ${mcap:,.0f}\n"
            f"💧 Liquidity: ${liquidity:,.0f}\n"
            f"📈 24h: {change:+.1f}%\n"
            f"👥 Holders: {holders:,}\n"
            f"🔄 5m: {buys}B/{sells}S\n"
            f"📊 Volume 24h: ${volume:,.0f}\n"
            f"⚡ Age: {age_str}{bot_warn}\n\n"
            f"{alpha_text}\n"
        )

        if safety_text:
            msg += f"\n{safety_text}\n"

        msg += (
            f"\n🔗 <a href=\"{chart_url}\">Chart</a> | "
            f"<a href=\"{explorer_url}\">Explorer</a>\n"
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
            f"🔗 <a href=\"{chart_url}\">Chart</a> | <a href=\"{explorer_url}\">Explorer</a>\n"
            f"⏰ {ts}"
        )
        return self.alerter.send(msg)

    def run_loop(self) -> None:
        """Continuous monitoring loop."""
        logger.info("Token discovery started | interval=%ds | min_alpha=%d",
                   self.poll_interval, self.min_alpha_score)
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
