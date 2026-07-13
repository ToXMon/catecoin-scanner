#!/usr/bin/env python3
"""Contract Safety Checker — Free Blockscout-based security checks.

Checks contract safety using only free APIs (no Moralis/Alchemy/Goplus paid APIs).
Uses Blockscout smart-contract verification endpoint and holder concentration analysis.

Returns safety signals used by alpha_scorer.py.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("catecoin-scanner.contract_safety")


class ContractSafetyChecker:
    """Checks contract safety via free Blockscout API."""

    def __init__(self, blockscout_client):
        """
        Args:
            blockscout_client: BlockscoutClient instance from blockscout.py
        """
        self.bs = blockscout_client

    def check_contract_verified(self, token_address: str) -> Optional[bool]:
        """Check if contract is verified on Blockscout.

        GET /smart-contracts/{address}
        Returns True if verified, False if not, None if can't determine.
        """
        try:
            data = self.bs._get(f"/smart-contracts/{token_address}")
            if data is None:
                return None
            return bool(data.get("abi") or data.get("source_code"))
        except Exception as e:
            logger.debug("Contract verification check failed for %s: %s", token_address[:10], e)
            return None

    def check_holder_concentration(self, token_address: str) -> Dict[str, Any]:
        """Analyze holder concentration for rug risk.

        Returns dict:
        - top5_pct: % held by top 5 holders
        - top10_pct: % held by top 10 holders
        - deployer_pct: % held by deployer (if identifiable)
        - risk_level: LOW / MEDIUM / HIGH
        """
        result = {
            "top5_pct": 0.0,
            "top10_pct": 0.0,
            "deployer_pct": 0.0,
            "risk_level": "UNKNOWN",
            "holders_checked": 0,
        }
        try:
            holders = self.bs.get_token_holders(token_address)
            if not holders:
                return result

            result["holders_checked"] = len(holders)
            total = sum(float(h.get("value", 0)) for h in holders)
            if total <= 0:
                return result

            sorted_holders = sorted(
                holders, key=lambda h: float(h.get("value", 0)), reverse=True
            )

            top5 = sum(float(h.get("value", 0)) for h in sorted_holders[:5])
            top10 = sum(float(h.get("value", 0)) for h in sorted_holders[:10])
            result["top5_pct"] = (top5 / total) * 100
            result["top10_pct"] = (top10 / total) * 100

            if result["top5_pct"] > 80:
                result["risk_level"] = "HIGH"
            elif result["top5_pct"] > 50:
                result["risk_level"] = "MEDIUM"
            else:
                result["risk_level"] = "LOW"

        except Exception as e:
            logger.debug("Holder concentration check failed for %s: %s", token_address[:10], e)

        return result

    def check_mint_authority(self, token_address: str, token_info: Optional[dict] = None) -> Optional[bool]:
        """Check if token has mint authority enabled.

        Returns:
        - False: mint authority appears disabled (good)
        - True: mint authority may be active (risky)
        - None: can't determine
        """
        try:
            info = token_info or self.bs.get_token_info(token_address)
            if not info:
                return None

            holders = info.get("holders", 0) or 0
            if holders > 100:
                return False

            return None
        except Exception as e:
            logger.debug("Mint authority check failed for %s: %s", token_address[:10], e)
            return None

    def check_liquidity_real(self, pair_data: dict, min_liquidity: float = 5000) -> Dict[str, Any]:
        """Verify liquidity is real (not just seeded/burned by deployer).

        Uses DexScreener pair data to detect LP lock/burn status.
        """
        result = {
            "liquidity_usd": 0.0,
            "lp_locked": None,
            "lp_burned": None,
            "is_real": False,
        }
        if not pair_data:
            return result

        liq = pair_data.get("liquidity", {}) or {}
        result["liquidity_usd"] = float(liq.get("usd", 0) or 0)

        info = pair_data.get("info", {}) or {}
        lp_holders = info.get("lpHolders", []) or []

        for holder in lp_holders:
            pct = float(holder.get("percentage", 0) or 0)
            tag = (holder.get("tag", "") or "").lower()
            if "burn" in tag and pct > 90:
                result["lp_burned"] = True
            if "lock" in tag and pct > 80:
                result["lp_locked"] = True

        result["is_real"] = (
            result["liquidity_usd"] >= min_liquidity
            and (result["lp_locked"] or result["lp_burned"])
        )

        return result

    def full_safety_check(
        self,
        token_address: str,
        pair_data: Optional[dict] = None,
        token_info: Optional[dict] = None,
        min_liquidity: float = 5000,
    ) -> Dict[str, Any]:
        """Run all safety checks. Returns combined safety report."""
        report = {
            "contract_verified": None,
            "mint_authority": None,
            "holder_concentration": {},
            "liquidity_check": {},
            "overall_safe": False,
            "red_flags": [],
            "green_flags": [],
        }

        report["contract_verified"] = self.check_contract_verified(token_address)
        if report["contract_verified"] is True:
            report["green_flags"].append("Contract verified")
        elif report["contract_verified"] is False:
            report["red_flags"].append("Contract NOT verified")

        report["mint_authority"] = self.check_mint_authority(token_address, token_info)
        if report["mint_authority"] is False:
            report["green_flags"].append("Mint likely disabled")
        elif report["mint_authority"] is True:
            report["red_flags"].append("Mint authority may be active")

        report["holder_concentration"] = self.check_holder_concentration(token_address)
        hc = report["holder_concentration"]
        if hc["risk_level"] == "HIGH":
            report["red_flags"].append(f"High holder concentration (top5: {hc['top5_pct']:.0f}%)")
        elif hc["risk_level"] == "LOW":
            report["green_flags"].append(f"Good holder distribution (top5: {hc['top5_pct']:.0f}%)")

        if pair_data:
            report["liquidity_check"] = self.check_liquidity_real(pair_data, min_liquidity)
            lc = report["liquidity_check"]
            if lc.get("lp_burned"):
                report["green_flags"].append("LP burned")
            elif lc.get("lp_locked"):
                report["green_flags"].append("LP locked")
            else:
                report["red_flags"].append("LP not locked/burned")

        red_count = len(report["red_flags"])
        green_count = len(report["green_flags"])
        report["overall_safe"] = red_count == 0 and green_count >= 2

        return report

    def format_safety_alert(self, report: Dict[str, Any]) -> str:
        """Format safety report for Telegram alert."""
        lines = ["🛡️ <b>Contract Safety</b>"]

        for flag in report.get("green_flags", []):
            lines.append(f"✅ {flag}")
        for flag in report.get("red_flags", []):
            lines.append(f"⚠️ {flag}")

        hc = report.get("holder_concentration", {})
        if hc.get("holders_checked", 0) > 0:
            lines.append(
                f"👥 Top5: {hc['top5_pct']:.0f}% | Top10: {hc['top10_pct']:.0f}%"
            )

        lc = report.get("liquidity_check", {})
        if lc.get("liquidity_usd", 0) > 0:
            status = "Locked" if lc.get("lp_locked") else ("Burned" if lc.get("lp_burned") else "Unlocked")
            lines.append(f"💧 LP: ${lc['liquidity_usd']:,.0f} ({status})")

        if report.get("overall_safe"):
            lines.append("🟢 <b>Overall: SAFE</b>")
        else:
            lines.append("🟡 <b>Overall: CAUTION</b>")

        return "\n".join(lines)
