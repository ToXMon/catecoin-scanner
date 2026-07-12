#!/usr/bin/env python3
"""Shared Telegram alert sender for all scanner modules.

Resolves bot_token and chat_id from env vars or robinhood-alpha config.
Never hardcodes secrets — always resolved at runtime.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Tuple

import requests

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

logger = logging.getLogger("catecoin-scanner.telegram")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
ROBINHOOD_ALPHA_CONFIG = "/a0/usr/workdir/robinhood-alpha/config.yaml"


def resolve_telegram(config: dict) -> Tuple[Optional[str], Optional[str]]:
    """Resolve Telegram bot_token and chat_id.
    Priority: env vars > robinhood-alpha config > scanner config.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        source = config.get("telegram_config_source", "env")
        if source == "robinhood-alpha":
            rh_path = config.get(
                "robinhood_alpha_config_path", ROBINHOOD_ALPHA_CONFIG
            )
            if rh_path and Path(rh_path).exists() and HAS_YAML:
                try:
                    with open(rh_path) as f:
                        rh_cfg = yaml.safe_load(f) or {}
                    tg = rh_cfg.get("alerts", {}).get("telegram", {})
                    if not bot_token:
                        bot_token = tg.get("bot_token")
                    if not chat_id:
                        chat_id = (
                            str(tg.get("chat_id", ""))
                            if tg.get("chat_id")
                            else None
                        )
                except Exception as e:
                    logger.warning("Could not read robinhood-alpha config: %s", e)

    if not bot_token:
        bot_token = config.get("telegram", {}).get("bot_token")
    if not chat_id:
        chat_id = config.get("telegram", {}).get("chat_id")

    return bot_token, chat_id


class TelegramAlerter:
    """Send HTML-formatted Telegram messages."""

    def __init__(
        self, bot_token: Optional[str] = None, chat_id: Optional[str] = None
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)
        if not self._enabled:
            logger.warning("Telegram not configured — alerts will be logged only")

    @classmethod
    def from_config(cls, config: dict) -> "TelegramAlerter":
        bot_token, chat_id = resolve_telegram(config)
        return cls(bot_token, chat_id)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send(self, message: str) -> bool:
        """Send HTML-formatted message. Returns True on success."""
        if not self._enabled:
            logger.debug("Telegram disabled, skipping alert")
            return False
        url = TELEGRAM_API.format(token=self.bot_token)
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                return True
            logger.error(
                "Telegram send failed (%d): %s", resp.status_code, resp.text[:300]
            )
            return False
        except requests.RequestException as e:
            logger.error("Telegram send error: %s", e)
            return False
