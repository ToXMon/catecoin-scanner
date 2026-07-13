#!/usr/bin/env python3
"""Blockscout API client (free, no key required).

Resilient client for Robinhood Chain explorer queries.
Base: https://robinhoodchain.blockscout.com/api/v2

NOTE: Robinhood Chain Blockscout does NOT index address transfers/transactions.
Only token-level queries work reliably (tokens list, token info, token holders).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("catecoin-scanner.blockscout")

DEFAULT_BASE = "https://robinhoodchain.blockscout.com/api/v2"
REQUEST_TIMEOUT = 20
MAX_RETRIES = 2
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
        self._last_request_time = 0.0

    def _rate_limit(self) -> None:
        elapsed = time.time() - self._last_request_time
        if elapsed < RATE_LIMIT_DELAY:
            time.sleep(RATE_LIMIT_DELAY - elapsed)
        self._last_request_time = time.time()

    def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        url = f"{self.base_url}{path}"
        for attempt in range(self.max_retries + 1):
            try:
                self._rate_limit()
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (429, 502, 503):
                    wait = 2 ** attempt
                    logger.warning(
                        "Blockscout %d on %s, retry in %ds", resp.status_code, path, wait
                    )
                    time.sleep(wait)
                    continue
                logger.warning("Blockscout %d on %s", resp.status_code, path)
                return None
            except requests.exceptions.Timeout:
                logger.warning("Blockscout timeout on %s (attempt %d)", path, attempt + 1)
            except Exception as e:
                logger.warning("Blockscout error on %s: %s", path, e)
        return None

    def get_tokens(self, limit: int = 50, q: str = "") -> Optional[dict]:
        """GET /tokens — list tokens (supports ?q=text search)."""
        params = {"limit": limit}
        if q:
            params["q"] = q
        return self._get("/tokens", params=params)

    def get_all_token_addresses(self) -> List[str]:
        """Fetch all known token addresses from Blockscout. Used for seen-set diffing."""
        data = self.get_tokens(limit=50)
        if not data or not data.get("items"):
            return []
        addresses = []
        for item in data["items"]:
            addr = item.get("address", "")
            if addr:
                addresses.append(addr.lower())
        return addresses

    def get_token_info(self, address: str) -> Optional[dict]:
        """GET /tokens/{address} — token metadata."""
        return self._get(f"/tokens/{address}")

    def get_token_holders(self, address: str, limit: int = 50) -> List[dict]:
        """GET /tokens/{address}/holders — top holders.

        NOTE: Robinhood Chain Blockscout does NOT accept `limit` param.
        Returns all available holders.
        """
        data = self._get(f"/tokens/{address}/holders")
        if not data or not data.get("items"):
            return []
        return data["items"][:limit]  # Slice locally instead

    def get_token_transfers(self, address: str, params: Optional[dict] = None) -> Optional[dict]:
        """GET /tokens/{address}/transfers — token transfer history.

        NOTE: Returns empty on Robinhood Chain. Kept for compatibility.
        """
        if params is None:
            params = {"limit": 50}
        return self._get(f"/tokens/{address}/transfers", params=params)

    def get_address_transfers(self, address: str, params: Optional[dict] = None) -> Optional[dict]:
        """GET /addresses/{address}/token-transfers — wallet transfer history.

        NOTE: Returns empty on Robinhood Chain. Kept for compatibility.
        """
        if params is None:
            params = {"limit": 50}
        return self._get(f"/addresses/{address}/token-transfers", params=params)

    def search_tokens(self, query: str) -> List[dict]:
        """Search tokens by text query."""
        data = self.get_tokens(limit=50, q=query)
        if not data or not data.get("items"):
            return []
        return data["items"]

    def get_token_holder_count(self, address: str) -> int:
        """Get holder count via /counters endpoint."""
        data = self._get(f"/tokens/{address}/counters")
        if not data:
            return 0
        holders = data.get("token_holders_count", "0")
        try:
            return int(holders)
        except (TypeError, ValueError):
            return 0

    def get_new_tokens(self, limit: int = 20) -> List[dict]:
        """Get tokens using seen-set diff approach.

        Since Blockscout doesn't support timestamp sorting on Robinhood Chain,
        we fetch all tokens and the caller tracks what's new.
        """
        data = self.get_tokens(limit=50)
        if not data or not data.get("items"):
            return []
        return data["items"][:limit]

    def is_healthy(self) -> bool:
        """Quick health check — can we reach the API?"""
        data = self.get_tokens(limit=1)
        return bool(data and data.get("items"))
