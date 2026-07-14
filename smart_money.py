#!/usr/bin/env python3
"""Smart Money Tracking Module — Holder-Based Detection.

Tracks smart-money wallets on Robinhood Chain by cross-referencing token
holder lists. Since Blockscout address transfers are not indexed on
Robinhood Chain, we use a holder-list approach:

1. For each tracked wallet, check tokens where they appear as holders
2. When a wallet appears as a top holder of a NEW trending token, alert
3. When 2+ elite wallets hold the same token, consensus alert

Usage (standalone):
    python smart_money.py --once     # Single scan then exit
    python smart_money.py            # Continuous loop (5 min intervals)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from blockscout import BlockscoutClient
from dexscreener import DexScreenerClient
from telegram_alert import TelegramAlerter
from alpha_scorer import DerivativeDetector, AlphaScorer
from alchemy_client import AlchemyClient
from alert_journal import AlertJournal

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

logger = logging.getLogger("catecoin-scanner.smart_money")

DEXSCREENER_CHART = "https://dexscreener.com/robinhood/{pair}"
CONSENSUS_WINDOW = 1800  # 30 minutes

DEFAULT_TIER_WEIGHTS = {
    "smart_money_elite": 1.0,
    "smart_money_whale": 0.8,
    "sniper": 0.85,
    "whale": 0.5,
    "watch": 0.3,
    "insider": 0.9,
    "mev_sniper": 0.1,
    "unknown": 0.3,
}
SNIPER_EARLY_ALPHA_MCAP_USD = 50_000  # Sub-$50K mcap = sniper EARLY_ALPHA trigger
SNIPER_CONSENSUS_MIN = 2  # 2+ snipers on same token = STRONG EARLY SIGNAL


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


class WalletScorer:
    """Scores wallets based on tier, PnL history, and consistency."""

    def __init__(self, tier_weights: Optional[Dict[str, float]] = None):
        self.tier_weights = tier_weights or DEFAULT_TIER_WEIGHTS

    def score_wallet(self, wallet: dict) -> float:
        """Returns 0-100 score for a wallet based on tier + scoring metadata."""
        tier = wallet.get("tier", "unknown")
        base_weight = self.tier_weights.get(tier, 0.3)

        scoring = wallet.get("scoring", {}) or {}
        consistency = scoring.get("consistency_score", 50)
        pnl = scoring.get("total_pnl_usd") or 0

        score = base_weight * 60
        score += (consistency / 100) * 25

        if pnl > 0:
            import math
            pnl_score = min(15, math.log10(max(pnl, 1)) * 3)
            score += pnl_score

        return min(100, score)

    def weighted_buy_signal(self, wallet: dict) -> float:
        return self.score_wallet(wallet) / 100
 
    def _wallet_signal_weight(self, wallet: dict) -> tuple:
        """Returns (weight, signal_type) based on wallet tier + score.
 
        Signal types:
          - ELITE_CONVICTION: large single-bet by elite-tier wallet (score>=90)
          - STRONG_BUY: elite-tier wallet (score<90)
          - EARLY_ALPHA: sniper-tier wallet (score>=85) — early momentum signal
          - EARLY_WATCH: sniper-tier wallet (score<85)
          - GENERIC: any other tier
        """
        tier = wallet.get("tier", "unknown")
        score = self.score_wallet(wallet)
 
        if tier == "smart_money_elite" and score >= 90:
            return 1.0, "ELITE_CONVICTION"
        elif tier == "smart_money_elite":
            return 0.8, "STRONG_BUY"
        elif tier == "sniper" and score >= 85:
            return 0.9, "EARLY_ALPHA"
        elif tier == "sniper":
            return 0.7, "EARLY_WATCH"
        return 0.5, "GENERIC"


class SmartMoneyTracker:
    """Tracks smart-money wallet activity via token holder cross-referencing.

    Since Blockscout address transfers return empty on Robinhood Chain,
    we use a token-centric approach: poll trending tokens for their holders,
    cross-reference against tracked wallets, and alert when tracked wallets
    are accumulating.
    """

    def __init__(self, config: dict) -> None:
        sm_cfg = config.get("smart_money", {})
        base_dir = Path(__file__).parent

        self.enabled = sm_cfg.get("enabled", True)
        self.poll_interval = sm_cfg.get("poll_interval_seconds", 300)
        wallets_file = sm_cfg.get("wallets_file", "smart_wallets.json")
        self.wallets_file = str(base_dir / wallets_file) if not os.path.isabs(wallets_file) else wallets_file

        # Load wallet scoring config
        wallets_data = self._load_wallets_data(self.wallets_file)
        ws_cfg = wallets_data.get("wallet_scoring_config", {})
        self.tier_weights = {**DEFAULT_TIER_WEIGHTS, **ws_cfg.get("tier_weights", {})}

        self.consensus_strong_weight = sm_cfg.get("consensus_strong_weight", 1.5)
        self.consensus_moderate_weight = sm_cfg.get("consensus_moderate_weight", 0.8)
        self.consensus_strong = sm_cfg.get("consensus_strong_30min", 2)
        self.consensus_moderate = sm_cfg.get("consensus_moderate_30min", 1)
        self.min_wallet_score = sm_cfg.get("min_wallet_score", 35)  # Lowered to catch whale-tier holders

        bs_base = sm_cfg.get("blockscout_base", "https://robinhoodchain.blockscout.com/api/v2")
        self.blockscout = BlockscoutClient(base_url=bs_base)
        self.dex = DexScreenerClient()
        self.alerter = TelegramAlerter.from_config(config)

        # Alchemy (PRIMARY data source for wallet transfer history)
        alch_cfg = config.get("alchemy", {}) or {}
        self.alchemy = AlchemyClient(
            api_key=alch_cfg.get("api_key"),
            network=alch_cfg.get("network", "robinhood-mainnet"),
            cu_warning_threshold=alch_cfg.get("cu_warning_threshold", 0.8),
            cu_monthly_limit=alch_cfg.get("cu_monthly_limit", 30_000_000),
        )

        self.scorer = WalletScorer(self.tier_weights)
        self.derivative_detector = DerivativeDetector()

        # Noise suppression: alpha quality filters
        alpha_cfg = config.get("alpha", {}) or {}
        self.min_buy_value_usd: float = float(alpha_cfg.get("min_buy_value_usd", 100))
        self.min_alert_liquidity: float = float(alpha_cfg.get("min_liquidity_usd", 5000))
        self.airdrop_blocklist: Set[str] = set(
            (a or "").lower() for a in alpha_cfg.get("airdrop_blocklist", [])
        )

        # Rug-pull scoring: use AlphaScorer helper
        self.alpha_scorer = AlphaScorer(min_liquidity=self.min_alert_liquidity)

        self.wallets: List[Dict[str, Any]] = load_wallets(self.wallets_file)

        # Build wallet address lookup (lowercase -> wallet dict)
        self.wallet_lookup: Dict[str, dict] = {}
        for w in self.wallets:
            addr = w.get("address", "").lower()
            if addr:
                self.wallet_lookup[addr] = w

        logger.info("Wallet lookup: %d addresses mapped", len(self.wallet_lookup))

        # Track which tokens each wallet has been alerted for (dedup)
        self.alerted_holdings: Dict[str, Set[str]] = {}  # wallet_addr -> set of token_addrs

        # GLOBAL persistent dedup: flat set of token_addrs alerted by ANY wallet.
        # Prevents the same token being re-alerted across wallets and container restarts.
        # Respect STATE_DIR for Akash persistent volume mount.
        state_dir = os.environ.get("STATE_DIR", "")
        dedup_filename = "alerted_tokens.json"
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
            self.dedup_file = os.path.join(state_dir, dedup_filename)
        else:
            self.dedup_file = os.path.join(os.path.dirname(self.wallets_file), dedup_filename)
        self.global_alerted: Dict[str, dict] = {}  # token_addr -> {first_alerted, last_alerted, count}
        self.re_alert_cooldown_hours = sm_cfg.get("re_alert_cooldown_hours", 24)
        self._load_global_dedup()

        # Alchemy dedup: wallet_addr -> set of token contracts already detected via transfers
        self._alchemy_known_wallet_tokens: Dict[str, Set[str]] = {}

        # Consensus: token_addr -> {wallets, weights, timestamps}
        self.consensus: Dict[str, Dict[str, Any]] = {}
        self.consensus_alerted: Dict[str, str] = {}

        # Alert journal (SQLite + forward price tracking for LLM training)
        journal_cfg = config.get("journal", {}) or {}
        self.journal = AlertJournal(
            db_path=journal_cfg.get("db_path", "state/alert_journal.db"),
            enabled=journal_cfg.get("enabled", True),
            intervals=journal_cfg.get("price_check_intervals"),
        )

    @staticmethod
    def _load_wallets_data(wallets_file: str) -> dict:
        try:
            with open(wallets_file) as f:
                return json.load(f)
        except Exception:
            return {}

    # ─── Global persistent dedup ──────────────────────────────────────

    def _load_global_dedup(self) -> None:
        """Load global alerted-token state from JSON file."""
        try:
            with open(self.dedup_file) as f:
                data = json.load(f)
            self.global_alerted = data if isinstance(data, dict) else {}
            logger.info("Loaded global dedup: %d tokens", len(self.global_alerted))
        except (FileNotFoundError, json.JSONDecodeError):
            self.global_alerted = {}

    def _save_global_dedup(self) -> None:
        """Persist global alerted-token state."""
        try:
            os.makedirs(os.path.dirname(self.dedup_file) or ".", exist_ok=True)
            with open(self.dedup_file, "w") as f:
                json.dump(self.global_alerted, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save dedup state: %s", e)

    def _is_token_alerted(self, token_addr: str) -> bool:
        """Check if token was recently alerted. Respects re_alert_cooldown_hours."""
        token_addr = (token_addr or "").lower()
        entry = self.global_alerted.get(token_addr)
        if not entry:
            return False
        last = entry.get("last_alerted", 0)
        cooldown_s = self.re_alert_cooldown_hours * 3600
        if (time.time() - last) < cooldown_s:
            return True
        return False

    def _mark_token_alerted(self, token_addr: str) -> None:
        """Record token as alerted in global dedup."""
        token_addr = (token_addr or "").lower()
        now = time.time()
        entry = self.global_alerted.get(token_addr, {})
        entry["first_alerted"] = entry.get("first_alerted", now)
        entry["last_alerted"] = now
        entry["count"] = entry.get("count", 0) + 1
        self.global_alerted[token_addr] = entry
        self._save_global_dedup()

    def scan_wallet_transfers_via_alchemy(self) -> int:
        """PRIMARY scan path: detect new token buys by tracked wallets via Alchemy.

        For each tracked wallet, query incoming ERC20 transfers. When we see
        a token contract the wallet has NOT previously received, that's a NEW
        BUY — alert immediately. Also drives multi-wallet consensus detection
        using REAL buy timestamps (not balance snapshots).
        """
        if not self.wallets:
            return 0

        alerts_sent = 0
        now = int(time.time())
        # Only look at last 24h of transfers to keep CU cost bounded
        from_block_window = 0  # Alchemy accepts 0 = genesis; we filter by timestamp client-side

        for wallet in self.wallets:
            wallet_addr = (wallet.get("address") or "").lower()
            if not wallet_addr:
                continue

            score = self.scorer.score_wallet(wallet)
            if score < self.min_wallet_score:
                continue

            known_tokens = self._alchemy_known_wallet_tokens.setdefault(wallet_addr, set())

            # Per-wallet alert cap to prevent first-scan flood
            wallet_alert_cap = 3
            wallet_alerts_this_scan = 0

            try:
                # Query incoming ERC20 transfers to this wallet (max 50 for CU efficiency)
                transfers = self.alchemy.get_asset_transfers(
                    to_addr=wallet_addr,
                    category=["erc20"],
                    max_count=50,
                    order="desc",
                )
            except Exception as e:
                logger.warning("Alchemy transfer query failed for %s: %s", wallet_addr[:10], e)
                continue

            for t in transfers:
                token_contract = (t.get("token_contract") or "").lower()
                if not token_contract:
                    continue

                # Skip native gas token and zero-value
                if token_contract in ("0x0000000000000000000000000000000000000000",):
                    continue

                # NEW BUY DETECTION
                if token_contract not in known_tokens:
                    known_tokens.add(token_contract)

                    # Per-wallet alert cap (prevents 30-alert flood on first scan)
                    if wallet_alerts_this_scan >= wallet_alert_cap:
                        continue

                    # Only alert on tokens we haven't already alerted via holder scan
                    alerted_set = self.alerted_holdings.setdefault(wallet_addr, set())
                    if token_contract in alerted_set:
                        continue

                    sent = self._alert_alchemy_new_buy(wallet, t, score)
                    if sent:
                        wallet_alerts_this_scan += 1
                        alerts_sent += 1
                        alerted_set.add(token_contract)

                    # Track consensus with REAL buy timestamp from Alchemy
                    _, sig_type = self.scorer._wallet_signal_weight(wallet)
                    self._track_consensus_with_timestamp(
                        token_contract,
                        wallet_addr,
                        wallet.get("label", "?"),
                        score,
                        t.get("timestamp"),
                        signal_type=sig_type,
                        wallet_tier=wallet.get("tier", "unknown"),
                    )

            # Throttle to avoid burning Alchemy CU
            time.sleep(0.1)

        logger.info(
            "Alchemy smart-money scan: %d wallets, %d alerts, CU used=%d",
            len(self.wallets),
            alerts_sent,
            self.alchemy.cu_used,
        )

        # Run forward price tracking for journal (checks due intervals)
        try:
            self.journal.run_price_tracker(self.dex)
        except Exception as e:
            logger.debug("Price tracker cycle failed: %s", e)

        return alerts_sent

    def _alert_alchemy_new_buy(self, wallet: dict, transfer: dict, wallet_score: float) -> bool:
        """Alert that a tracked wallet just received a NEW token (fresh buy)."""
        # ─── GLOBAL DEDUP CHECK (before any API calls) ───
        token_addr = (transfer.get("token_contract", "") or "").lower()
        if self._is_token_alerted(token_addr):
            logger.debug("Skipping %s — globally deduped (already alerted recently)", token_addr[:10])
            return False

        # ─── NOISE SUPPRESSION FILTERS (memecoin best practices) ───

        # Filter 1: airdrop/spam blocklist
        if token_addr in self.airdrop_blocklist:
            logger.debug("Skipping airdrop/spam token %s", token_addr[:10])
            return False

        label = wallet.get("label", wallet.get("address", "?")[:10])
        tier = wallet.get("tier", "unknown")
        symbol = "???"
        name = "Unknown"
        price = 0.0
        liquidity = 0.0
        volume = 0.0
        fdv = 0.0

        # Enrich from DexScreener
        try:
            pair = self.dex.get_token(token_addr) or {}
            symbol = (pair.get("baseToken") or {}).get("symbol", "???")
            name = (pair.get("baseToken") or {}).get("name", "Unknown")
            price = float(pair.get("priceUsd", 0) or 0)
            liquidity = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
            volume = float((pair.get("volume") or {}).get("h24", 0) or 0)
            fdv = float(pair.get("fdv") or 0)
        except Exception:
            pass

        # Filter 2: skip obvious derivatives unless high-conviction wallet
        upper_symbol = symbol.upper()
        if upper_symbol not in ("CATE", "CASHCAT", "ROBINHOOD", "HOOD") and wallet_score < 70:
            is_deriv, _ = self.derivative_detector.is_derivative(symbol, name)
            if is_deriv:
                return False

        # Filter 3: min liquidity floor (spam/airdrop suppression)
        if liquidity < self.min_alert_liquidity:
            logger.debug("Skipping low-liquidity spam token %s (liq=$%.0f < $%.0f)", symbol, liquidity, self.min_alert_liquidity)
            return False

        # Filter 3b: spam name detection
        sym_clean = symbol.strip().lstrip("$")
        if not sym_clean or len(sym_clean) < 2:
            logger.debug("Skipping spam token %s (invalid symbol)", symbol)
            return False
        spam_names = {"rejected", "buy", "sell", "test", "token", "airdrop", "free", "claim", "cookware"}
        if sym_clean.lower() in spam_names:
            logger.debug("Skipping spam token %s (blacklisted name)", symbol)
            return False

        # Filter 3c: FDV floor (low FDV = spam/scam)
        if 0 < fdv < 10000:
            logger.debug("Skipping low-FDV token %s (fdv=$%.0f)", symbol, fdv)
            return False

        # Filter 4: rug-pull check via liq/mcap ratio
        rug_penalty, rug_level = self.alpha_scorer._rug_pull_risk(liquidity, fdv)
        if rug_level in ("CRITICAL", "HIGH"):
            logger.info("Auto-reject %s: %s rug risk liq/mcap ratio (liq=$%.0f fdv=$%.0f)", symbol, rug_level, liquidity, fdv)
            return False

        # Parse value
        try:
            value = float(transfer.get("value", 0))
        except (TypeError, ValueError):
            value = 0.0

        # Filter 5: min buy value USD (spam/airdrop suppression)
        est_value_usd = 0.0
        if price > 0 and value > 0:
            est_value_usd = value * price
        elif liquidity >= 10000:
            est_value_usd = self.min_buy_value_usd

        if est_value_usd > 0 and est_value_usd < self.min_buy_value_usd:
            logger.debug("Skipping low-value buy %s (~$%.0f < $%.0f)", symbol, est_value_usd, self.min_buy_value_usd)
            return False

        try:
            holders = self.blockscout.get_token_holder_count(token_addr)
        except Exception:
            holders = 0

        thesis = (
            f"Smart money ({tier}, score {wallet_score:.0f}) just BOUGHT via real transfer "
            f"detected by Alchemy at {transfer.get('timestamp', '?')[:19]}"
        )
        risk = rug_level if rug_level in ("HIGH", "CRITICAL") else ("LOW" if wallet_score >= 70 else "MEDIUM")
        risk_factors = f"rug_risk={rug_level}" if rug_level not in ("LOW", "UNKNOWN") else ""

        logger.info(
            "🧠 ALCHEMY NEW BUY: %s (%s) acquired %s ($%s) — %s tokens",
            label[:25], tier, symbol, symbol, value,
        )

        sent = self.alerter.send_alpha_alert(
            symbol=symbol,
            name=name,
            contract=token_addr,
            price=price,
            liquidity=liquidity,
            volume_24h=volume,
            holders=holders,
            alpha_score=int(wallet_score),
            thesis=thesis,
            risk_level=risk,
            risk_factors=risk_factors,
            smart_money=f"{label} ({tier})",
            market_cap=fdv,
            fdv=fdv,
            category="🧠 SMART MONEY",
        )

        if sent:
            self._mark_token_alerted(token_addr)
            self.journal.log_alert({
                "alert_type": "smart_money_buy",
                "token_symbol": symbol,
                "token_name": name,
                "token_address": token_addr,
                "price_usd": price,
                "liquidity_usd": liquidity,
                "volume_24h": volume,
                "fdv": fdv,
                "holders": holders,
                "wallet_address": wallet.get("address", ""),
                "wallet_label": label,
                "wallet_tier": tier,
                "wallet_score": wallet_score,
                "alpha_score": int(wallet_score),
                "risk_level": risk,
                "thesis": thesis,
                "risk_factors": risk_factors,
                "telegram_sent": True,
            })

        return sent

    def _track_consensus_with_timestamp(
        self,
        token_addr: str,
        wallet_addr: str,
        label: str,
        wallet_score: float,
        iso_timestamp: Optional[str],
        signal_type: str = "GENERIC",
        wallet_tier: str = "unknown",
    ) -> None:
        """Track consensus using REAL Alchemy transfer timestamps."""
        # Convert ISO timestamp to epoch
        epoch = None
        if iso_timestamp:
            try:
                from datetime import datetime
                # Strip 'Z' suffix and parse
                ts_clean = iso_timestamp.rstrip("Z").replace("T", " ").split(".")[0]
                dt = datetime.strptime(ts_clean, "%Y-%m-%d %H:%M:%S")
                epoch = dt.replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                epoch = time.time()
        else:
            epoch = time.time()

        entry = self.consensus.setdefault(
            token_addr, {"wallets": set(), "labels": [], "weights": [], "timestamps": [], "signal_types": [], "tiers": []}
        )
        entry["wallets"].add(wallet_addr.lower())
        entry["labels"].append(label)
        entry["weights"].append(wallet_score / 100)
        entry["timestamps"].append(epoch)
        entry["signal_types"].append(signal_type)
        entry["tiers"].append(wallet_tier)

    def scan_all_wallets(self) -> int:
        """Main scan: PRIMARY Alchemy transfer path + SECONDARY Blockscout holder scan.

        Alchemy (PRIMARY): detect real-time token buys via incoming ERC20 transfers
        to tracked wallets. Replaces the broken Blockscout address-transfer query.

        Blockscout (SECONDARY): holder cross-referencing on trending tokens remains
        as a complementary signal (catches wallets we may have missed via transfers).
        """
        alerts_sent = 0

        # ---- PRIMARY PATH: Alchemy real-time buy detection ----
        try:
            alchemy_alerts = self.scan_wallet_transfers_via_alchemy()
            alerts_sent += alchemy_alerts
        except Exception as e:
            logger.warning("Alchemy smart-money scan failed (degraded to Blockscout): %s", e)

        # Get trending tokens from DexScreener
        try:
            pairs = self.dex.search("robinhood")
            if not pairs:
                pairs = []
        except Exception as e:
            logger.warning("DexScreener search failed: %s", e)
            pairs = []

        # Always include tracked tokens in the scan (CATE + CASHCAT)
        # 5 of 8 wallets are CASHCAT insiders, 3 are CATE whales
        for tracked_addr in [
            "0xfc5ABD01E4Def799549eee154449Ff6a7ae0cAc7",  # CATE
            "0x020bfC650A365f8BB26819deAAbF3E21291018b4",    # CASHCAT (from memories)
        ]:
            try:
                t_pair = self.dex.get_token(tracked_addr)
                if t_pair and t_pair not in pairs:
                    pairs.insert(0, t_pair)
            except Exception:
                pass

        if not pairs:
            logger.info("No pairs to scan")
            return 0

        logger.info("Scanning %d tokens for tracked wallet holders", len(pairs[:15]))

        # Check top 15 tokens (trending + Catecoin) for tracked wallet holders
        for pair in pairs[:15]:
            try:
                token_addr = (pair.get("baseToken") or {}).get("address", "").lower()
                if not token_addr:
                    continue

                symbol = (pair.get("baseToken") or {}).get("symbol", "???")
                name = (pair.get("baseToken") or {}).get("name", "Unknown")

                # Skip derivative filter for explicitly tracked base tokens
                # (Cate, Cash Cat ARE the base tokens, not derivatives of themselves)
                # Only filter obvious clone tokens like BABYCATE, CATE2, etc.
                is_deriv = False
                upper_symbol = symbol.upper()
                if upper_symbol not in ("CATE", "CASHCAT", "ROBINHOOD", "HOOD"):
                    is_deriv, _ = self.derivative_detector.is_derivative(symbol, name)
                if is_deriv:
                    continue

                # Get token holders from Blockscout
                holders = self.blockscout.get_token_holders(token_addr, limit=50)
                if not holders:
                    continue

                # Cross-reference holders against tracked wallets
                found_wallets = []
                for holder in holders:
                    holder_addr = (holder.get("address") or {}).get("hash", "").lower()
                    if holder_addr in self.wallet_lookup:
                        wallet = self.wallet_lookup[holder_addr]
                        score = self.scorer.score_wallet(wallet)
                        if score >= self.min_wallet_score:
                            found_wallets.append(wallet)
                            # Track consensus
                            _, sig_type = self.scorer._wallet_signal_weight(wallet)
                            self._track_consensus(
                                token_addr, holder_addr, wallet.get("label", "?"), score,
                                signal_type=sig_type,
                                wallet_tier=wallet.get("tier", "unknown"),
                            )

                if found_wallets:
                    # Check if any of these wallets haven't been alerted for this token
                    for wallet in found_wallets:
                        wallet_addr = wallet.get("address", "").lower()
                        alerted_set = self.alerted_holdings.setdefault(wallet_addr, set())
                        if token_addr not in alerted_set:
                            alerted_set.add(token_addr)
                            sent = self._alert_smart_money_holding(pair, wallet, token_addr)
                            alerts_sent += 1 if sent else 0

                time.sleep(0.2)  # Rate limit between tokens

            except Exception as e:
                logger.warning("Error checking token %s: %s", token_addr[:10], e)

        # Check consensus after all tokens scanned
        alerts_sent += self._check_consensus()

        logger.info("Smart money scan complete: %d alerts sent", alerts_sent)
        return alerts_sent

    def _alert_smart_money_holding(self, pair: dict, wallet: dict, token_addr: str) -> bool:
        """Alert that a tracked smart money wallet holds this token."""
        label = wallet.get("label", wallet.get("address", "?")[:10])
        tier = wallet.get("tier", "unknown")
        score = self.scorer.score_wallet(wallet)

        symbol = (pair.get("baseToken") or {}).get("symbol", "???")
        name = (pair.get("baseToken") or {}).get("name", "Unknown")

        price = float(pair.get("priceUsd", 0) or 0)
        liquidity = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
        volume = float((pair.get("volume") or {}).get("h24", 0) or 0)

        # Try to get holder count from Blockscout
        holders = self.blockscout.get_token_holder_count(token_addr)

        thesis = f"Smart money wallet ({tier}, score {score:.0f}) holds this token"
        risk = "LOW" if score >= 70 else "MEDIUM"

        logger.info("🧠 SMART MONEY HOLDING: %s (%s) holds %s ($%s)", label[:25], tier, symbol, symbol)

        return self.alerter.send_alpha_alert(
            symbol=symbol,
            name=name,
            contract=token_addr,
            price=price,
            liquidity=liquidity,
            volume_24h=volume,
            holders=holders,
            alpha_score=int(score),
            thesis=thesis,
            risk_level=risk,
            risk_factors="",
            smart_money=f"{label} ({tier})",
        )

    def _track_consensus(
        self, token_addr: str, wallet_addr: str, label: str, wallet_score: float = 50.0,
        signal_type: str = "GENERIC", wallet_tier: str = "unknown",
    ) -> None:
        entry = self.consensus.setdefault(
            token_addr, {"wallets": set(), "labels": [], "weights": [], "timestamps": [], "signal_types": [], "tiers": []}
        )
        entry["wallets"].add(wallet_addr.lower())
        entry["labels"].append(label)
        entry["weights"].append(wallet_score / 100)
        entry["timestamps"].append(time.time())
        entry["signal_types"].append(signal_type)
        entry["tiers"].append(wallet_tier)

    def _check_consensus(self) -> int:
        """Check for consensus: multiple tracked wallets holding same token."""
        alerts_sent = 0
        now = time.time()
        cutoff = now - CONSENSUS_WINDOW

        for token_addr, entry in list(self.consensus.items()):
            # Prune old timestamps
            entry["timestamps"] = [t for t in entry["timestamps"] if t >= cutoff]
            if not entry["timestamps"]:
                continue

            unique_wallets = {w for w in entry["wallets"]}
            count = len(unique_wallets)
            weighted_score = sum(entry.get("weights", [0.5] * count))

            already = self.consensus_alerted.get(token_addr)

            strong_signal = weighted_score >= self.consensus_strong_weight or count >= self.consensus_strong

            if strong_signal and already != "strong":
                self._alert_consensus(token_addr, entry, weighted_score, count)
                self.consensus_alerted[token_addr] = "strong"
                alerts_sent += 1

            # ── SNIPER TIER: EARLY_ALPHA logic ──
            # Single sniper EARLY_ALPHA buy on sub-$50K mcap token = early signal
            # 2+ snipers on same token = STRONG EARLY SIGNAL
            signal_types = entry.get("signal_types", [])
            tiers = entry.get("tiers", [])
            sniper_buys = sum(1 for st in signal_types if st in ("EARLY_ALPHA", "EARLY_WATCH"))
            elite_conviction = sum(1 for st in signal_types if st == "ELITE_CONVICTION")

            if sniper_buys > 0 and already not in ("strong", "early_alpha"):
                # Get token mcap via DexScreener
                try:
                    pair = self.dex.get_token(token_addr) or {}
                    fdv = float(pair.get("fdv", 0) or 0)
                except Exception:
                    fdv = 0

                # Single sniper on sub-$50K mcap token = EARLY ALPHA
                if sniper_buys >= SNIPER_CONSENSUS_MIN:
                    self._alert_strong_early(token_addr, entry, sniper_buys)
                    self.consensus_alerted[token_addr] = "strong_early"
                    alerts_sent += 1
                elif 0 < fdv < SNIPER_EARLY_ALPHA_MCAP_USD:
                    self._alert_sniper_early_alpha(token_addr, entry, fdv)
                    self.consensus_alerted[token_addr] = "early_alpha"
                    alerts_sent += 1

        return alerts_sent

    def _alert_sniper_early_alpha(self, token_addr: str, entry: dict, mcap_usd: float) -> None:
        """Send EARLY ALPHA alert — single sniper buy on sub-$50K mcap token."""
        labels = entry.get("labels", [])
        try:
            pair = self.dex.get_token(token_addr) or {}
            symbol = (pair.get("baseToken") or {}).get("symbol", "???")
            name = (pair.get("baseToken") or {}).get("name", "Unknown")
            price = float(pair.get("priceUsd", 0) or 0)
            liquidity = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
            volume = float((pair.get("volume") or {}).get("h24", 0) or 0)
        except Exception:
            symbol, name, price, liquidity, volume = "???", "Unknown", 0, 0, 0

        # Rug-pull check (snipers must respect same checks as elite)
        try:
            rug_penalty, rug_level = self.alpha_scorer._rug_pull_risk(liquidity, mcap_usd)
        except Exception:
            rug_level = "UNKNOWN"

        if rug_level in ("CRITICAL", "HIGH"):
            logger.info("Skipping sniper EARLY_ALPHA %s: %s rug risk", symbol, rug_level)
            return

        sniper_label = labels[0] if labels else "Sniper"
        thesis = (
            f"Sniper-tier wallet ({sniper_label}) just bought NEW low-mcap token. "
            f"Early momentum signal — diversified early-entry hunter."
        )

        logger.info("🚀 EARLY ALPHA: Sniper %s bought %s at $%.0fK mcap", sniper_label[:20], symbol, mcap_usd / 1000)

        self.alerter.send_alpha_alert(
            symbol=symbol,
            name=name,
            contract=token_addr,
            price=price,
            liquidity=liquidity,
            volume_24h=volume,
            holders=0,
            alpha_score=85,
            thesis=thesis,
            risk_level="MEDIUM",
            risk_factors=f"rug_risk={rug_level}",
            smart_money=f"{sniper_label} (sniper)",
            market_cap=mcap_usd,
            fdv=mcap_usd,
            category="🚀 EARLY ALPHA",
        )

    def _alert_strong_early(self, token_addr: str, entry: dict, sniper_count: int) -> None:
        """Send STRONG EARLY SIGNAL alert — 2+ snipers on same token."""
        labels = entry.get("labels", [])
        wallets = [{"label": l} for l in labels[:5]]
        try:
            pair = self.dex.get_token(token_addr) or {}
            symbol = (pair.get("baseToken") or {}).get("symbol", "???")
            name = (pair.get("baseToken") or {}).get("name", "Unknown")
            price = float(pair.get("priceUsd", 0) or 0)
            liquidity = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
            volume = float((pair.get("volume") or {}).get("h24", 0) or 0)
            fdv = float(pair.get("fdv", 0) or 0)
        except Exception:
            symbol, name, price, liquidity, volume, fdv = "???", "Unknown", 0, 0, 0, 0

        # Rug-pull check (snipers must respect same checks as elite)
        try:
            rug_penalty, rug_level = self.alpha_scorer._rug_pull_risk(liquidity, fdv)
        except Exception:
            rug_level = "UNKNOWN"

        if rug_level in ("CRITICAL", "HIGH"):
            logger.info("Skipping STRONG EARLY %s: %s rug risk", symbol, rug_level)
            return

        thesis = (
            f"{sniper_count} sniper-tier wallets bought the SAME early token. "
            f"Strong early convergence signal."
        )

        logger.info("🧠 STRONG EARLY: %d snipers converged on %s", sniper_count, symbol)

        self.alerter.send_alpha_alert(
            symbol=symbol,
            name=name,
            contract=token_addr,
            price=price,
            liquidity=liquidity,
            volume_24h=volume,
            holders=0,
            alpha_score=90,
            thesis=thesis,
            risk_level="LOW",
            risk_factors=f"rug_risk={rug_level}",
            smart_money=f"{sniper_count} snipers",
            market_cap=fdv,
            fdv=fdv,
            category="🧠 STRONG EARLY",
        )

    def _alert_consensus(self, token_addr: str, entry: dict, weighted_score: float, count: int) -> None:
        """Send STRONG CONSENSUS alert."""
        wallets = []
        labels = entry.get("labels", [])
        for i, label in enumerate(labels[:5]):
            wallets.append({"label": label})

        pair = self.dex.get_token(token_addr) or {}
        price = float(pair.get("priceUsd", 0) or 0)
        liquidity = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
        symbol = (pair.get("baseToken") or {}).get("symbol", "???")

        logger.info("🔥 CONSENSUS: %d tracked wallets hold %s (weight=%.2f)", count, symbol, weighted_score)

        self.alerter.send_consensus_alert(
            symbol=symbol,
            contract=token_addr,
            wallets=wallets,
            price=price,
            liquidity=liquidity,
        )


def main():
    parser = argparse.ArgumentParser(description="Smart Money Tracker")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--once", action="store_true", help="Run once then exit")
    parser.add_argument("--interval", type=int, default=300, help="Poll interval (seconds)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    config = load_config(args.config)
    tracker = SmartMoneyTracker(config)

    if args.once:
        alerts = tracker.scan_all_wallets()
        print(f"Smart money scan complete: {alerts} alerts sent")
    else:
        while True:
            try:
                tracker.scan_all_wallets()
            except Exception as e:
                logger.error("Scan error: %s", e, exc_info=True)
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
