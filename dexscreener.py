#!/usr/bin/env python3
"""DexScreener API client (free, no key required).

Shared client used by price monitor, smart money tracker, and token discovery.
All endpoints are free with no authentication.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("catecoin-scanner.dexscreener")

DEXSCREENER_API_BASE = "https://api.dexscreener.com"
DEXSCREENER_BASE = f"{DEXSCREENER_API_BASE}/latest/dex"
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
RATE_LIMIT_DELAY = 0.25


class DexScreenerClient:
    """Thin wrapper around DexScreener free API with exponential backoff retry."""

    def __init__(
        self,
        timeout: int = REQUEST_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        default_chain: str = "robinhood",
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.default_chain = default_chain
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "catecoin-scanner/2.0",
        })

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        return self._get_url(f"{DEXSCREENER_BASE}{path}", params=params)

    def _get_url(self, url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 429:
                    wait = 2 ** attempt
                    logger.warning("DexScreener 429 rate limit, waiting %ds", wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                wait = 2 ** attempt
                logger.warning(
                    "DexScreener fetch attempt %d/%d failed: %s",
                    attempt + 1, self.max_retries, e,
                )
                if attempt < self.max_retries - 1:
                    time.sleep(wait)
        logger.error("DexScreener fetch exhausted for %s", url)
        return None

    def get_pair(self, chain: str, pair_address: str) -> Optional[Dict[str, Any]]:
        """GET /pairs/{chain}/{pair} — single pair data."""
        data = self._get(f"/pairs/{chain}/{pair_address}")
        if not data:
            return None
        pair = data.get("pair")
        if not pair and data.get("pairs"):
            pair = data["pairs"][0]
        return pair

    def get_token(self, token_address: str, chain: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """GET /tokens/{address} — best pair for token, preferring the requested chain."""
        data = self._get(f"/tokens/{token_address}")
        if not data:
            return None
        pairs = data.get("pairs") or []
        if not pairs:
            return None
        preferred_chain = chain or self.default_chain
        matches = [p for p in pairs if p.get("chainId") == preferred_chain]
        if matches:
            matches.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
            return matches[0]
        return pairs[0]

    def search(self, query: str) -> List[Dict[str, Any]]:
        """GET /search?q={query} — search pairs/tokens."""
        data = self._get("/search", params={"q": query})
        if not data:
            return []
        return data.get("pairs") or []

    def get_tokens_batch(self, addresses: List[str]) -> List[Dict[str, Any]]:
        """GET /tokens/{commaSepAddresses} — batch resolve up to 30 tokens to pairs."""
        if not addresses:
            return []
        data = self._get(f"/tokens/{','.join(addresses[:30])}")
        if not data:
            return []
        return data.get("pairs") or []

    def get_token_profiles(self) -> List[Dict[str, Any]]:
        """GET /token-profiles/latest/v1 — latest token profiles (fail-open: [])."""
        data = self._get_url(f"{DEXSCREENER_API_BASE}/token-profiles/latest/v1")
        return data if isinstance(data, list) else []

    def get_token_boosts_latest(self) -> List[Dict[str, Any]]:
        """GET /token-boosts/latest/v1 — latest boosted tokens (fail-open: [])."""
        data = self._get_url(f"{DEXSCREENER_API_BASE}/token-boosts/latest/v1")
        return data if isinstance(data, list) else []

    def get_token_boosts_top(self) -> List[Dict[str, Any]]:
        """GET /token-boosts/top/v1 — top boosted tokens (fail-open: [])."""
        data = self._get_url(f"{DEXSCREENER_API_BASE}/token-boosts/top/v1")
        return data if isinstance(data, list) else []
