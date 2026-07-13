#!/usr/bin/env python3
"""Alpha Scoring Engine + Derivative Detection.

Solves the core noise problem: discovery alerts were catching derivatives/spawn
tokens of existing memecoins, not true alpha.

This module provides:
1. Derivative detection — filters clones of known tokens (Cate, Doge, CashCat variants)
2. Alpha score (0-100) — composite score for true alpha detection
3. Signal weighting — smart money buys, liquidity/holder growth, volume ratio, safety

Only requires free APIs (DexScreener + Blockscout data already fetched).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("catecoin-scanner.alpha_scorer")

# ─── Known memecoin base names for derivative detection ─────────────────────

# Tokens that frequently get cloned/spawned on Robinhood Chain
DERIVATIVE_BASE_NAMES: List[str] = [
    "cate", "catecoin", "cashcat", "cash cat", "doge", "dogecoin", "shib",
    "pepe", "wojak", "brett", "bonk", "floki", "chad", "giga", "mog",
    "robinhood", "vlad", "tenev", "noxa", "buck", "gme", "gamestop",
    "wallstreet", "juggernaut", "repe",
]

# Suffixes/prefixes that indicate a derivative/spawn token
DERIVATIVE_MARKERS: List[str] = [
    "2.0", "v2", "v3", "next", "new", "real", "original",
    "official", "wrapped", "safe", "moon", "inu", "ceo", "king",
    "baby", "mini", "micro", "mega", "super", "ultra", "pro",
    "based", "rare", "golden", "diamond", "rocket",
]

# Symbols/names that are pure spam patterns
SPAM_PATTERNS: List[str] = [
    r"^A[A-Z]{0,2}$",
    r"^TEST",
    r"^MEME\d+",
    r"^TOKEN\d+",
    r"^COIN\d+",
]


class DerivativeDetector:
    """Detects derivative/spawn/clone tokens of known memecoins."""

    def __init__(self, base_names: Optional[List[str]] = None, markers: Optional[List[str]] = None):
        self.base_names = [n.lower() for n in (base_names or DERIVATIVE_BASE_NAMES)]
        self.markers = [m.lower() for m in (markers or DERIVATIVE_MARKERS)]
        self._seen_symbols: Set[str] = set()

    def register_existing_token(self, symbol: str, name: str = "") -> None:
        """Register an existing token to detect future derivatives of it."""
        sym = (symbol or "").upper().strip()
        if sym and len(sym) <= 12:
            self._seen_symbols.add(sym)

    def is_derivative(self, symbol: str, name: str) -> Tuple[bool, str]:
        """Check if a token is likely a derivative/clone.

        Returns (is_derivative: bool, reason: str)
        """
        sym = (symbol or "").strip()
        full_name = (name or "").strip()
        sym_lower = sym.lower()
        name_lower = full_name.lower()

        if not sym or not full_name:
            return False, ""

        # Check 1: Spam patterns
        for pattern in SPAM_PATTERNS:
            if re.match(pattern, sym, re.IGNORECASE):
                return True, f"Spam pattern match: {pattern}"

        # Check 2: Direct base name match with derivative marker
        for base in self.base_names:
            if base in sym_lower or base in name_lower:
                for marker in self.markers:
                    if marker in sym_lower and sym_lower != base:
                        return True, f"Derivative of '{base}' (marker: {marker})"
                if sym_lower == base and full_name:
                    for marker in self.markers:
                        if marker in name_lower:
                            return True, f"'{base}' variant (marker in name: {marker})"

        # Check 3: High similarity to known base names
        for base in self.base_names:
            ratio = SequenceMatcher(None, sym_lower, base).ratio()
            if ratio >= 0.85 and sym_lower != base:
                return True, f"High similarity to '{base}' ({ratio:.0%})"
            if len(full_name) > 3:
                ratio = SequenceMatcher(None, name_lower, base).ratio()
                if ratio >= 0.80:
                    return True, f"Name similar to '{base}' ({ratio:.0%})"

        # Check 4: Numbered variant of seen symbol (CATE -> CATE2, CATE3)
        sym_base = re.match(r"^([A-Z]+)", sym)
        if sym_base:
            base_sym = sym_base.group(1)
            if base_sym in self._seen_symbols and sym != base_sym:
                return True, f"Numbered variant of existing token '{base_sym}'"

        # Check 5: Very long symbol (>12 chars) often indicates spam
        if len(sym) > 12:
            return True, f"Symbol too long ({len(sym)} chars) — likely spam"

        # Check 6: Repeated characters (CAAATE, DOOGE)
        if re.search(r"([A-Z])\1{2,}", sym):
            return True, "Repeated characters in symbol"

        return False, ""

    def similarity_score(self, symbol: str, name: str) -> float:
        """Returns 0-1 similarity to nearest known base name. 0 = unique, 1 = exact match."""
        sym_lower = (symbol or "").lower()
        name_lower = (name or "").lower()
        max_ratio = 0.0
        for base in self.base_names:
            r1 = SequenceMatcher(None, sym_lower, base).ratio()
            r2 = SequenceMatcher(None, name_lower, base).ratio() if name_lower else 0
            max_ratio = max(max_ratio, r1, r2)
        return max_ratio


class AlphaScorer:
    """Composite alpha score (0-100) for discovered tokens.

    Scoring components (max 100):
    - Smart money buying: +30 max (capped)
    - Liquidity growth: +20 if liquidity increasing
    - Holder growth: +20 if holders growing
    - Volume/liquidity ratio: +15 if volume > 2x liquidity
    - Contract safety: +15 if verified, no mint authority

    Penalties:
    - Derivative detected: -50
    - Bot activity: -20
    - Low liquidity (<$5K): -15
    - Low holders (<10): -10
    """

    SMART_MONEY_WEIGHT = 30
    LIQUIDITY_GROWTH_WEIGHT = 20
    HOLDER_GROWTH_WEIGHT = 20
    VOLUME_RATIO_WEIGHT = 15
    SAFETY_WEIGHT = 15

    DERIVATIVE_PENALTY = 50
    BOT_PENALTY = 20
    LOW_LIQUIDITY_PENALTY = 15
    LOW_HOLDERS_PENALTY = 10

    def __init__(
        self,
        min_liquidity: float = 5000,
        min_holders: int = 10,
        min_alpha_score: int = 50,
        volume_ratio_threshold: float = 2.0,
    ):
        self.min_liquidity = min_liquidity
        self.min_holders = min_holders
        self.min_alpha_score = min_alpha_score
        self.volume_ratio_threshold = volume_ratio_threshold

    def score(
        self,
        symbol: str,
        name: str,
        token_addr: str,
        price: float,
        liquidity: float,
        volume_24h: float,
        holders: int,
        price_change_24h: float,
        smart_money_buyers: int = 0,
        liquidity_growth_pct: float = 0.0,
        holder_growth_pct: float = 0.0,
        contract_verified: Optional[bool] = None,
        mint_authority: Optional[bool] = None,
        bot_detected: bool = False,
        is_derivative: bool = False,
        derivative_reason: str = "",
        pair_data: Optional[dict] = None,
        market_cap_usd: float = 0,
        fdv_usd: float = 0,
    ) -> Dict[str, Any]:
        """Calculate alpha score for a token.

        Returns dict with alpha_score, pass_threshold, breakdown, penalties, verdict.
        """
        breakdown: Dict[str, Any] = {}
        score = 0

        # 1. Smart money buying (capped)
        sm_points = min(smart_money_buyers * 15, self.SMART_MONEY_WEIGHT)
        if sm_points > 0:
            score += sm_points
            breakdown["smart_money"] = {"points": sm_points, "buyers": smart_money_buyers}

        # 2. Liquidity growth
        if liquidity_growth_pct > 10:
            liq_pts = int(self.LIQUIDITY_GROWTH_WEIGHT * min(liquidity_growth_pct / 50, 1.0))
            score += liq_pts
            breakdown["liquidity_growth"] = {"points": liq_pts, "growth_pct": liquidity_growth_pct}
        elif liquidity >= self.min_liquidity * 4:
            score += 5
            breakdown["liquidity_base"] = {"points": 5, "note": "High absolute liquidity"}

        # 3. Holder growth
        if holder_growth_pct > 20:
            holder_pts = int(self.HOLDER_GROWTH_WEIGHT * min(holder_growth_pct / 100, 1.0))
            score += holder_pts
            breakdown["holder_growth"] = {"points": holder_pts, "growth_pct": holder_growth_pct}
        elif holders >= 50:
            score += 5
            breakdown["holder_base"] = {"points": 5, "holders": holders}

        # 4. Volume/liquidity ratio
        if liquidity > 0:
            vol_liq_ratio = volume_24h / liquidity
            if vol_liq_ratio >= self.volume_ratio_threshold:
                vol_pts = self.VOLUME_RATIO_WEIGHT
                score += vol_pts
                breakdown["volume_ratio"] = {"points": vol_pts, "ratio": vol_liq_ratio}

        # 5. Contract safety
        safety_pts = 0
        safety_notes = []
        if contract_verified is True:
            safety_pts += 8
            safety_notes.append("verified")
        if mint_authority is False:
            safety_pts += 7
            safety_notes.append("mint_disabled")
        if safety_pts > 0:
            score += safety_pts
            breakdown["safety"] = {"points": safety_pts, "notes": safety_notes}

        # ─── Penalties ───
        penalties = []

        if is_derivative:
            score -= self.DERIVATIVE_PENALTY
            penalties.append(f"DERIVATIVE: -{self.DERIVATIVE_PENALTY} ({derivative_reason})")

        if bot_detected:
            score -= self.BOT_PENALTY
            penalties.append(f"BOT_ACTIVITY: -{self.BOT_PENALTY}")

        if 0 < liquidity < self.min_liquidity:
            score -= self.LOW_LIQUIDITY_PENALTY
            penalties.append(f"LOW_LIQ: -{self.LOW_LIQUIDITY_PENALTY}")

        if 0 < holders < self.min_holders:
            score -= self.LOW_HOLDERS_PENALTY
            penalties.append(f"LOW_HOLDERS: -{self.LOW_HOLDERS_PENALTY}")

        # Rug-pull risk check (liq/mcap ratio)
        rug_penalty, rug_level = self._rug_pull_risk(liquidity, market_cap_usd or fdv_usd)
        if rug_penalty != 0:
            score += rug_penalty
            if rug_penalty < 0:
                penalties.append(f"RUG_RISK: {rug_penalty} ({rug_level})")
            elif rug_penalty > 0:
                breakdown["liquidity_health"] = {"points": rug_penalty, "level": rug_level}

        score = max(0, min(100, score))

        if is_derivative:
            verdict = "REJECT"
        elif score >= self.min_alpha_score:
            verdict = "ALPHA"
        elif score >= self.min_alpha_score - 20:
            verdict = "WATCH"
        else:
            verdict = "REJECT"

        return {
            "alpha_score": score,
            "pass_threshold": score >= self.min_alpha_score and not is_derivative,
            "breakdown": breakdown,
            "penalties": penalties,
            "verdict": verdict,
            "is_derivative": is_derivative,
            "derivative_reason": derivative_reason,
        }

    def format_score_breakdown(self, result: Dict[str, Any]) -> str:
        """Format score breakdown for Telegram alert."""
        score = result["alpha_score"]
        verdict = result["verdict"]
        lines = [f"🎯 <b>Alpha Score: {score}/100</b> [{verdict}]"]

        bd = result.get("breakdown", {})
        if bd.get("smart_money"):
            sm = bd["smart_money"]
            lines.append(f"✅ Smart money: +{sm['points']} ({sm['buyers']} wallets)")
        if bd.get("liquidity_growth"):
            lg = bd["liquidity_growth"]
            lines.append(f"✅ Liq growth: +{lg['points']} (+{lg['growth_pct']:.0f}%)")
        if bd.get("liquidity_base"):
            lines.append(f"✅ High liquidity: +{bd['liquidity_base']['points']}")
        if bd.get("holder_growth"):
            hg = bd["holder_growth"]
            lines.append(f"✅ Holder growth: +{hg['points']} (+{hg['growth_pct']:.0f}%)")
        if bd.get("holder_base"):
            lines.append(f"✅ Strong holders: +{bd['holder_base']['points']}")
        if bd.get("volume_ratio"):
            vr = bd["volume_ratio"]
            lines.append(f"✅ Vol/Liq ratio: +{vr['points']} ({vr['ratio']:.1f}x)")
        if bd.get("safety"):
            sf = bd["safety"]
            lines.append(f"✅ Safety: +{sf['points']} ({', '.join(sf['notes'])})")

        for penalty in result.get("penalties", []):
            lines.append(f"❌ {penalty}")

        return "\n".join(lines)


    def _rug_pull_risk(self, liquidity: float, market_cap: float) -> tuple:
        """Returns (score_modifier, risk_level) based on liq/mcap ratio.

        Memecoin best practice: healthy tokens have liquidity as a meaningful
        fraction of market cap. Low ratio = rug pull risk.
        """
        if market_cap <= 0:
            return 0, 'UNKNOWN'
        ratio = liquidity / market_cap
        if ratio < 0.05:
            return -50, 'CRITICAL'
        elif ratio < 0.1:
            return -30, 'HIGH'
        elif ratio < 0.2:
            return -10, 'MEDIUM'
        elif ratio > 0.3:
            return 10, 'LOW'
        return 0, 'LOW'


# ─── Convenience functions ───────────────────────────────────────────────────

def is_early_stage(pair_data: dict, max_age_hours: int = 24) -> bool:
    """Check if token is within early-stage window based on pairCreatedAt."""
    created = pair_data.get("pairCreatedAt", "") if pair_data else ""
    if not created:
        return True
    try:
        if isinstance(created, (int, float)):
            dt = datetime.fromtimestamp(
                created / 1000 if created > 1e12 else created, tz=timezone.utc
            )
        elif isinstance(created, str):
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        else:
            return True
        age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        return age_hours <= max_age_hours
    except (ValueError, TypeError):
        return True


def get_token_age_hours(pair_data: dict) -> Optional[float]:
    """Get token age in hours from pairCreatedAt, or None if unknown."""
    created = pair_data.get("pairCreatedAt", "") if pair_data else ""
    if not created:
        return None
    try:
        if isinstance(created, (int, float)):
            dt = datetime.fromtimestamp(
                created / 1000 if created > 1e12 else created, tz=timezone.utc
            )
        elif isinstance(created, str):
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        else:
            return None
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except (ValueError, TypeError):
        return None
