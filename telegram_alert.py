#!/usr/bin/env python3
"""Actionable Telegram Alert Formatter — Enhanced with Color-Coded Sections.

Every alert type has a distinct, clearly defined section:
- 🚀 EARLY DETECTION (green) — new tokens with alpha signals
- 🧠 SMART MONEY (blue) — tracked wallet buys
- 🐋 WHALE MOVE (purple) — large transfers
- 🧟 ZOMBIE REVIVAL (orange) — dormant token waking up
- 📈 REVERSAL (yellow) — downtrend + smart money re-entry
- 💧 LIQUIDITY (cyan) — LP add/remove events

All alerts include token, contract, price, liq/mcap ratio, thesis, risk.
Works with free Telegram Bot API.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("catecoin-scanner.alerts")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
DEXSCREENER_LINK = "https://dexscreener.com/robinhood/{addr}"
BLOCKSCOUT_LINK = "https://robinhoodchain.blockscout.com/token/{addr}"

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


def _fmt_price(price: float) -> str:
    if price == 0:
        return "$0"
    if price < 0.0001:
        return f"${price:.10f}"
    elif price < 0.01:
        return f"${price:.8f}"
    elif price < 1:
        return f"${price:.6f}"
    else:
        return f"${price:.4f}"


def _fmt_usd(amount: float) -> str:
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.2f}M"
    elif amount >= 1000:
        return f"${amount / 1000:.1f}K"
    else:
        return f"${amount:.0f}"


class TelegramAlerter:
    """Sends actionable Telegram alerts with full token context."""

    def __init__(self, bot_token: str = "", chat_id: str = ""):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        if not self.enabled:
            logger.warning("Telegram alerts DISABLED — no bot_token or chat_id")

    @classmethod
    def from_config(cls, config: dict) -> "TelegramAlerter":
        alerts_cfg = config.get("alerts", {}).get("telegram", {})
        bot_token = (
            alerts_cfg.get("bot_token")
            or os.environ.get("TELEGRAM_BOT_TOKEN")
            or ""
        )
        chat_id = (
            str(alerts_cfg.get("chat_id"))
            or os.environ.get("TELEGRAM_CHAT_ID")
            or ""
        )
        return cls(bot_token=bot_token, chat_id=chat_id)

    def send(self, message: str) -> bool:
        if not self.enabled:
            logger.debug("Telegram disabled, skipping alert")
            return False
        try:
            url = TELEGRAM_API.format(token=self.bot_token)
            data = urllib.parse.urlencode({
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }).encode()
            req = urllib.request.Request(url, data=data)
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read())
            if result.get("ok"):
                return True
            logger.error("Telegram API error: %s", result.get("description"))
            return False
        except Exception as e:
            logger.error("Telegram send failed: %s", e)
            return False

    # ─── Formatted Alert Builders ───

    def send_alpha_alert(
        self,
        symbol: str,
        name: str,
        contract: str,
        price: float = 0,
        liquidity: float = 0,
        volume_24h: float = 0,
        holders: int = 0,
        alpha_score: int = 0,
        thesis: str = "",
        risk_level: str = "MEDIUM",
        risk_factors: str = "",
        smart_money: str = "",
        market_cap: float = 0,
        fdv: float = 0,
        category: str = "🚀 EARLY DETECTION",
    ) -> bool:
        """Send alpha alert with clear section formatting.

        category: '🚀 EARLY DETECTION' for new tokens, '🧠 SMART MONEY' for wallet buys.
        """
        mcap_val = market_cap or fdv or 0
        liq_mcap_ratio = (liquidity / mcap_val) if mcap_val > 0 else 0
        ratio_str = f"{liq_mcap_ratio:.1%}" if liq_mcap_ratio > 0 else "N/A"
        price_str = _fmt_price(price)
        liq_str = _fmt_usd(liquidity)
        vol_str = _fmt_usd(volume_24h)
        mcap_str = _fmt_usd(mcap_val) if mcap_val > 0 else "N/A"

        risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴", "CRITICAL": "⛔"}.get(risk_level, "🟡")

        msg = (
            f"{category} — ${symbol}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📛 Token: ${symbol} ({name})\n"
            f"📍 Contract: <code>{contract}</code>\n"
            f"💰 Price: {price_str} | MCap: {mcap_str}\n"
            f"📊 Liq: {liq_str} | Vol24h: {vol_str} | Holders: {holders}\n"
        )

        if smart_money:
            msg += f"👤 Smart Money: {smart_money}\n"

        msg += f"🎯 Thesis: {thesis}\n"
        msg += f"⚠️ Risk: {risk_emoji} {risk_level} — liq/mcap = {ratio_str}\n"

        if risk_factors:
            msg += f"📝 Factors: {risk_factors}\n"

        msg += f'🔗 <a href="{DEXSCREENER_LINK.format(addr=contract)}">DexScreener</a>'
        msg += f' | <a href="{BLOCKSCOUT_LINK.format(addr=contract)}">Blockscout</a>\n'
        msg += f"━━━━━━━━━━━━━━━━━━"

        return self.send(msg)

    def send_whale_alert(
        self,
        symbol: str,
        contract: str,
        amount_usd: float,
        direction: str,
        whale_addr: str,
    ) -> bool:
        dir_emoji = "📈" if direction == "ACCUMULATION" else "📉"
        whale_short = f"{whale_addr[:6]}...{whale_addr[-4:]}"
        amt_str = _fmt_usd(amount_usd)

        msg = (
            f"🐋 WHALE MOVE — ${symbol}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📛 Token: ${symbol}\n"
            f"📍 Contract: <code>{contract}</code>\n"
            f"💵 Amount: {amt_str}\n"
            f"{dir_emoji} Direction: {direction}\n"
            f"👤 Whale: <code>{whale_short}</code>\n"
            f'🔗 <a href="{DEXSCREENER_LINK.format(addr=contract)}">DexScreener</a>\n'
            f"━━━━━━━━━━━━━━━━━━"
        )
        return self.send(msg)

    def send_zombie_alert(
        self,
        symbol: str,
        contract: str,
        dormancy_days: int,
        volume_spike_pct: float,
        current_volume: float,
        liquidity: float,
        smart_money_buying: bool = False,
        market_cap: float = 0,
        holders: int = 0,
    ) -> bool:
        vol_str = _fmt_usd(current_volume)
        liq_str = _fmt_usd(liquidity)
        mcap_str = _fmt_usd(market_cap) if market_cap > 0 else "N/A"
        liq_mcap_ratio = (liquidity / market_cap) if market_cap > 0 else 0
        ratio_str = f"{liq_mcap_ratio:.1%}" if liq_mcap_ratio > 0 else "N/A"
        sm_str = "✅ YES — elite wallets accumulating" if smart_money_buying else "❌ No smart money detected"
        safety_str = "🔒 SAFE" if liq_mcap_ratio > 0.1 else "⚠️ RUG RISK" if liq_mcap_ratio > 0 else "❓ UNKNOWN"

        msg = (
            f"🧟 ZOMBIE REVIVAL — ${symbol}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📛 Token: ${symbol}\n"
            f"📍 Contract: <code>{contract}</code>\n"
            f"⏰ Dormant: {dormancy_days} days\n"
            f"📈 Volume Spike: +{volume_spike_pct:.0f}%\n"
            f"💰 Current Vol: {vol_str}\n"
            f"📊 Liq: {liq_str} | MCap: {mcap_str} | Holders: {holders}\n"
            f"👤 Smart Money: {sm_str}\n"
            f"🔒 Safety: {safety_str} — liq/mcap = {ratio_str}\n"
            f'🔗 <a href="{DEXSCREENER_LINK.format(addr=contract)}">DexScreener</a>'
            f' | <a href="{BLOCKSCOUT_LINK.format(addr=contract)}">Blockscout</a>\n'
            f"━━━━━━━━━━━━━━━━━━"
        )
        return self.send(msg)

    def send_reversal_alert(
        self,
        symbol: str,
        contract: str,
        drop_pct: float,
        price: float,
        liquidity: float,
        volume_change: float,
        smart_money_count: int = 0,
        market_cap: float = 0,
        thesis: str = "",
    ) -> bool:
        price_str = _fmt_price(price)
        liq_str = _fmt_usd(liquidity)
        mcap_str = _fmt_usd(market_cap) if market_cap > 0 else "N/A"
        liq_mcap_ratio = (liquidity / market_cap) if market_cap > 0 else 0
        ratio_str = f"{liq_mcap_ratio:.1%}" if liq_mcap_ratio > 0 else "N/A"

        sm_str = f"✅ {smart_money_count} elite wallet(s) re-entering" if smart_money_count > 0 else "⚠️ No smart money yet"
        vol_str = f"+{volume_change:.1f}x" if volume_change > 0 else "N/A"

        msg = (
            f"📈 REVERSAL SIGNAL — ${symbol}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📛 Token: ${symbol}\n"
            f"📍 Contract: <code>{contract}</code>\n"
            f"📉 Drop from recent high: -{drop_pct:.1f}%\n"
            f"💰 Price: {price_str} | MCap: {mcap_str}\n"
            f"📊 Liq: {liq_str} | Vol Spike: {vol_str}\n"
            f"👤 Smart Money: {sm_str}\n"
            f"🎯 Thesis: {thesis}\n"
            f"⚠️ Risk: liq/mcap = {ratio_str}\n"
            f'🔗 <a href="{DEXSCREENER_LINK.format(addr=contract)}">DexScreener</a>'
            f' | <a href="{BLOCKSCOUT_LINK.format(addr=contract)}">Blockscout</a>\n'
            f"━━━━━━━━━━━━━━━━━━"
        )
        return self.send(msg)

    def send_liquidity_alert(
        self,
        symbol: str,
        contract: str,
        action: str,
        amount: float,
        old_liquidity: float,
        new_liquidity: float,
        signal: str,
    ) -> bool:
        amt_str = _fmt_usd(amount)
        old_str = _fmt_usd(old_liquidity)
        new_str = _fmt_usd(new_liquidity)
        signal_emoji = "🟢" if signal == "BULLISH" else "🔴"

        msg = (
            f"💧 LIQUIDITY FLOW — ${symbol}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📛 Token: ${symbol}\n"
            f"📍 Contract: <code>{contract}</code>\n"
            f"📊 Action: {action}\n"
            f"💵 Amount: {amt_str}\n"
            f"💰 Pool: {old_str} → {new_str}\n"
            f"{signal_emoji} Signal: {signal}\n"
            f'🔗 <a href="{DEXSCREENER_LINK.format(addr=contract)}">DexScreener</a>\n'
            f"━━━━━━━━━━━━━━━━━━"
        )
        return self.send(msg)

    def send_consensus_alert(
        self,
        symbol: str,
        contract: str,
        wallets: list,
        price: float = 0,
        liquidity: float = 0,
    ) -> bool:
        wallet_list = " | ".join(w.get("label", "?")[:30] for w in wallets[:5])
        price_str = _fmt_price(price)
        liq_str = _fmt_usd(liquidity)

        msg = (
            f"🧠 SMART MONEY — {len(wallets)} WALLETS CONSENSUS\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📛 Token: ${symbol}\n"
            f"📍 Contract: <code>{contract}</code>\n"
            f"💰 Price: {price_str} | Liq: {liq_str}\n"
            f"👤 Wallets: {wallet_list}\n"
            f"🎯 Thesis: {len(wallets)} tracked elite wallets buying — high-conviction signal\n"
            f"⚠️ Risk: MEDIUM — consensus reduces risk but always DYOR\n"
            f'🔗 <a href="{DEXSCREENER_LINK.format(addr=contract)}">DexScreener</a>\n'
            f"━━━━━━━━━━━━━━━━━━"
        )
        return self.send(msg)

    def send_price_alert(
        self,
        symbol: str,
        contract: str,
        price: float,
        change_pct: float,
        old_price: float,
        liquidity: float = 0,
        volume_24h: float = 0,
    ) -> bool:
        emoji = "📈" if change_pct > 0 else "📉"
        price_str = _fmt_price(price)
        old_str = _fmt_price(old_price)
        liq_str = _fmt_usd(liquidity)

        msg = (
            f"{emoji} PRICE ALERT — ${symbol}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 Price: {old_str} → {price_str} ({change_pct:+.1f}%)\n"
            f"📊 Liq: {liq_str}\n"
            f"📍 Contract: <code>{contract[:10]}...{contract[-6:]}</code>\n"
            f'🔗 <a href="{DEXSCREENER_LINK.format(addr=contract)}">DexScreener</a>\n'
            f"━━━━━━━━━━━━━━━━━━"
        )
        return self.send(msg)
