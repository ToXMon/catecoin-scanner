#!/usr/bin/env python3
"""Blockscout API client (free, no key required).

Resilient client for Robinhood Chain explorer queries.
Handles outages, rate limits, and client errors gracefully.
Base: https://robinhoodchain.blockscout.com/api/v2
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("catecoin-scanner.blockscout")

DEFAULT_BASE = "https://robinhoodchain.blockscout.com/api/v2"
REQUEST_TIMEOUT = 20
MAX_RETRIES = 2  # reduced from 3 — don't hammer on outages
RATE_LIMIT_DELAY = 0.3


class BlockscoutClient:
    """Thin wrapper around Blockscout explorer API with smart retry logic."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE,
        timeout: int = REQUEST_TIMEOUT,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "catecoin-scanner/2.0",
        })
        self._consecutive_failures = 0
        self._degraded = False

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        """GET with retry on 429/5xx only. 4xx errors fail immediately."""
        # If we've had 5+ consecutive failures, enter degraded mode (skip requests for 60s)
        if self._degraded:
            logger.debug("Blockscout in degraded mode — skipping request to %s", path)
            return None

        url = f"{self.base_url}{path}"
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)

                # Rate limited — retry with backoff
                if resp.status_code == 429:
                    wait = 2 ** attempt
                    logger.warning("Blockscout 429 rate limit, waiting %ds", wait)
                    time.sleep(wait)
                    continue

                # Server error — retry with backoff
                if resp.status_code >= 500:
                    wait = 2 ** attempt
                    logger.warning("Blockscout %d server error, attempt %d/%d", resp.status_code, attempt + 1, self.max_retries)
                    if attempt < self.max_retries - 1:
                        time.sleep(wait)
                        continue

                # Client error (400, 422, 404) — don't retry, these won't fix themselves
                if 400 <= resp.status_code < 500:
                    logger.debug("Blockscout %d client error for %s — not retrying", resp.status_code, path)
                    self._consecutive_failures = 0
                    return None

                resp.raise_for_status()
                self._consecutive_failures = 0
                self._degraded = False
                return resp.json()

            except requests.exceptions.Timeout:
                logger.warning("Blockscout timeout, attempt %d/%d", attempt + 1, self.max_retries)
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
            except requests.RequestException as e:
                logger.warning("Blockscout fetch error: %s", e)
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)

        # All retries exhausted
        self._consecutive_failures += 1
        if self._consecutive_failures >= 5:
            self._degraded = True
            logger.error("Blockscout appears to be DOWN — entering degraded mode (skipping requests for 60s)")
            # Schedule recovery
            import threading
            threading.Timer(60.0, self._recover).start()

        logger.error("Blockscout fetch failed for %s", path)
        return None

    def _recover(self):
        """Exit degraded mode after cooldown."""
        self._degraded = False
        self._consecutive_failures = 0
        logger.info("Blockscout exiting degraded mode — will retry requests")

    def get_token_info(self, token_address: str) -> Optional[Dict[str, Any]]:
        """GET /tokens/{address} — token metadata."""
        return self._get(f"/tokens/{token_address}")

    def get_token_holders(self, token_address: str) -> List[Dict[str, Any]]:
        """GET /tokens/{address}/holders — top token holders."""
        data = self._get(f"/tokens/{token_address}/holders")
        return data.get("items") if data else []

    def get_address_transfers(
        self, address: str, params: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """GET /addresses/{address}/token-transfers — ERC-20 transfers."""
        return self._get(f"/addresses/{address}/token-transfers", params=params)

    def get_token_transfers(
        self, token_address: str, params: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """GET /tokens/{address}/transfers — all transfers for a token."""
        return self._get(f"/tokens/{token_address}/transfers", params=params)

    def get_new_tokens(self, limit: int = 50) -> List[Dict[str, Any]]:
        """GET /tokens?sort=address_timestamp&order=desc — newest tokens."""
        data = self._get("/tokens", params={"sort": "address_timestamp", "order": "desc", "limit": str(limit)})
        return data.get("items") if data else []

    def get_address_balance(self, address: str) -> Optional[Dict[str, Any]]:
        """GET /addresses/{address} — address info, balance, tx count."""
        return self._get(f"/addresses/{address}")
