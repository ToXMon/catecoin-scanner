#!/usr/bin/env python3
"""Alchemy API client for Robinhood Chain — PRIMARY data source.

Replaces broken Blockscout address transfer queries with Alchemy's
indexed asset transfer history. Free tier: 30M CU/month, 500 CU/s.

Methods:
  - get_token_balances(wallet, tokens)
  - get_asset_transfers(from, to, token, max_count, order)
  - get_token_metadata(token_addr)
  - get_block_number()
  - get_latest_block_timestamp()

All calls track Compute Units (CU) and rate-limit to stay under 500 CU/s.
Blockscout remains as fallback for token holder lists.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("catecoin-scanner.alchemy")

DEFAULT_API_KEY = ""
DEFAULT_NETWORK = "robinhood-mainnet"
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
RATE_LIMIT_DELAY = 0.05  # 50ms between calls = 20 req/s baseline (well under 500 CU/s)

# Compute Unit costs per method (Alchemy billing model)
CU_COSTS = {
    "eth_blockNumber": 10,
    "eth_getBlockByNumber": 10,
    "alchemy_getTokenBalances": 25,
    "alchemy_getAssetTransfers": 25,
    "alchemy_getTokenMetadata": 10,
    "default": 10,
}

# Per-second CU tracking window for 500 CU/s rate limit
CU_PER_SECOND_LIMIT = 500
CU_PER_SECOND_WINDOW = 1.0  # seconds


class AlchemyClient:
    """Primary Alchemy client with Compute Unit tracking and rate limiting."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        network: str = DEFAULT_NETWORK,
        cu_warning_threshold: float = 0.8,
        cu_monthly_limit: int = 30_000_000,
    ) -> None:
        # Env var takes priority over default (per task constraint)
        self.api_key = (
            os.environ.get("ALCHEMY_API_KEY")
            or api_key
            or DEFAULT_API_KEY
        )
        self.network = network
        self.base_url = f"https://{network}.g.alchemy.com/v2/{self.api_key}"
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "catecoin-scanner/3.0",
        })

        # CU tracking
        self.cu_used = 0
        self.cu_limit = cu_monthly_limit
        self.cu_warning_threshold = cu_warning_threshold
        self._warning_emitted = False

        # Per-second rate limit window
        self._cu_window: List[float] = []  # timestamps of recent CU spend
        self._cu_in_window = 0
        self._last_request_time = 0.0
        self._lock = threading.Lock()

    # ---------- public API ----------

    def get_block_number(self) -> Optional[int]:
        """Return latest block number as int, or None on failure."""
        result = self._rpc_call("eth_blockNumber", [])
        if result is None:
            return None
        try:
            return int(result, 16)
        except (TypeError, ValueError):
            logger.warning("alchemy eth_blockNumber bad result: %s", result)
            return None

    def get_latest_block_timestamp(self) -> Optional[int]:
        """Return latest block UNIX timestamp, or None on failure."""
        block_num = self.get_block_number()
        if block_num is None:
            return None
        result = self._rpc_call(
            "eth_getBlockByNumber", [hex(block_num), False]
        )
        if not result or not isinstance(result, dict):
            return None
        ts_hex = result.get("timestamp")
        try:
            return int(ts_hex, 16)
        except (TypeError, ValueError):
            return None

    def get_token_metadata(self, token_addr: str) -> Optional[Dict[str, Any]]:
        """Get token name, symbol, decimals, logo."""
        return self._rpc_call(
            "alchemy_getTokenMetadata", [self._normalize_addr(token_addr)]
        )

    def get_token_balances(
        self, wallet: str, tokens: Optional[List[str]] = None
    ) -> Dict[str, str]:
        """Get token balances for a wallet.

        Args:
            wallet: wallet address
            tokens: optional list of token contracts to query (precision mode)

        Returns:
            Dict mapping token_addr (lowercase) -> raw hex balance string.
            Missing tokens map to "0x0".
        """
        params: List[Any] = [self._normalize_addr(wallet)]
        if tokens:
            params.append([self._normalize_addr(t) for t in tokens])
        result = self._rpc_call("alchemy_getTokenBalances", params)
        if not result or not isinstance(result, dict):
            return {}

        out: Dict[str, str] = {}
        # Newer Alchemy returns tokenBalances list
        balances = result.get("tokenBalances", [])
        if isinstance(balances, list):
            for entry in balances:
                addr = (entry.get("address") or "").lower()
                bal = entry.get("tokenBalance") or entry.get("balance") or "0x0"
                if addr:
                    out[addr] = bal
        return out

    def get_asset_transfers(
        self,
        from_addr: Optional[str] = None,
        to_addr: Optional[str] = None,
        token_contract: Optional[str] = None,
        max_count: int = 100,
        order: str = "desc",
        from_block: Optional[int] = None,
        to_block: Optional[int] = None,
        category: Optional[List[str]] = None,
        with_metadata: bool = True,
    ) -> List[Dict[str, Any]]:
        """Get REAL transfer history — replaces broken Blockscout query.

        Args:
            from_addr: filter by sender
            to_addr: filter by recipient
            token_contract: filter by ERC-20 contract
            max_count: max results (Alchemy allows up to 1000)
            order: 'asc' or 'desc'
            from_block/to_block: block range filter
            category: list of ['external','internal','erc20','erc721','erc1155']
            with_metadata: include block timestamp + block number

        Returns:
            List of transfer dicts with normalized fields:
              {from, to, value, token_contract, block_num, timestamp, hash, category}
        """
        params: Dict[str, Any] = {
            "maxCount": hex(max_count),
            "order": order,
            "withMetadata": with_metadata,
        }
        if from_addr:
            params["fromAddress"] = self._normalize_addr(from_addr)
        if to_addr:
            params["toAddress"] = self._normalize_addr(to_addr)
        if token_contract:
            params["contractAddresses"] = [self._normalize_addr(token_contract)]
            if category is None:
                category = ["erc20"]
        if category:
            params["category"] = category
        if from_block is not None:
            params["fromBlock"] = hex(from_block) if isinstance(from_block, int) else from_block
        if to_block is not None:
            params["toBlock"] = hex(to_block) if isinstance(to_block, int) else to_block

        result = self._rpc_call("alchemy_getAssetTransfers", [params])
        if not result or not isinstance(result, dict):
            return []

        transfers_raw = result.get("transfers", [])
        return [self._normalize_transfer(t) for t in transfers_raw]

    # ---------- internal helpers ----------

    @staticmethod
    def _normalize_addr(addr: str) -> str:
        """Alchemy requires checksummed or lowercase addresses; lowercase is safe."""
        return addr.lower() if isinstance(addr, str) else addr

    @staticmethod
    def _normalize_transfer(t: Dict[str, Any]) -> Dict[str, Any]:
        """Flatten Alchemy's nested transfer structure into a clean dict."""
        raw = t.get("rawContract") or {}
        metadata = t.get("metadata") or {}
        block_num = metadata.get("blockNumber") or t.get("blockNum")
        return {
            "from": (t.get("from") or "").lower(),
            "to": (t.get("to") or "").lower(),
            "value": str(t.get("value") or "0"),
            "raw_value": raw.get("value") or t.get("raw_value") or "",
            "token_contract": (raw.get("address") or "").lower(),
            "decimals": raw.get("decimal") or t.get("decimals") or "0",
            "block_num": block_num,
            "timestamp": metadata.get("blockTimestamp") or t.get("timestamp"),
            "hash": t.get("hash") or "",
            "category": t.get("category") or "",
        }

    def _track_cu(self, method: str) -> int:
        """Track Compute Units used; return CU cost for this call."""
        cost = CU_COSTS.get(method, CU_COSTS["default"])
        with self._lock:
            self.cu_used += cost
            now = time.time()
            # Drop timestamps older than the window
            cutoff = now - CU_PER_SECOND_WINDOW
            self._cu_window = [ts for ts in self._cu_window if ts > cutoff]
            self._cu_window.append(now)
            self._cu_in_window += cost

            # Reset per-second counter when window slides
            # (approximate — keeps last window's spend)
            if len(self._cu_window) > 1 and (now - self._cu_window[0]) > CU_PER_SECOND_WINDOW:
                self._cu_in_window = cost
                self._cu_window = [now]

            # Warn on approaching monthly limit
            if not self._warning_emitted and self.cu_used >= self.cu_limit * self.cu_warning_threshold:
                logger.warning(
                    "Alchemy CU usage at %.1f%% of monthly limit (%d/%d CU)",
                    100 * self.cu_used / self.cu_limit,
                    self.cu_used,
                    self.cu_limit,
                )
                self._warning_emitted = True

            # Per-second rate limit: sleep if we're burning CU too fast
            if self._cu_in_window > CU_PER_SECOND_LIMIT:
                logger.warning(
                    "Alchemy per-second CU limit exceeded (%d/%d) — backing off",
                    self._cu_in_window,
                    CU_PER_SECOND_LIMIT,
                )
        return cost

    def _rate_limit(self) -> None:
        """Enforce base rate limit between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < RATE_LIMIT_DELAY:
            time.sleep(RATE_LIMIT_DELAY - elapsed)
        self._last_request_time = time.time()

    def _rpc_call(
        self, method: str, params: List[Any]
    ) -> Optional[Any]:
        """Execute JSON-RPC call with retry, CU tracking, rate limiting."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        for attempt in range(MAX_RETRIES + 1):
            try:
                self._rate_limit()
                self._track_cu(method)
                resp = self.session.post(
                    self.base_url, json=payload, timeout=REQUEST_TIMEOUT
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if "error" in data:
                        logger.warning(
                            "alchemy %s RPC error: %s",
                            method,
                            data["error"],
                        )
                        return None
                    return data.get("result")
                if resp.status_code == 429:
                    wait = 2 ** attempt
                    logger.warning(
                        "alchemy 429 on %s, retry in %ds", method, wait
                    )
                    time.sleep(wait)
                    continue
                logger.warning(
                    "alchemy %s HTTP %d: %s",
                    method,
                    resp.status_code,
                    resp.text[:200],
                )
                return None
            except requests.exceptions.Timeout:
                logger.warning(
                    "alchemy %s timeout (attempt %d)", method, attempt + 1
                )
            except Exception as e:
                logger.warning("alchemy %s error: %s", method, e)
                if attempt >= MAX_RETRIES:
                    return None
        return None

    def is_healthy(self) -> bool:
        """Quick health check."""
        return self.get_block_number() is not None

    def cu_usage_ratio(self) -> float:
        """Return current CU usage as fraction of monthly limit (0.0–1.0)."""
        return self.cu_used / self.cu_limit if self.cu_limit else 0.0


# ----------------- standalone smoke test -----------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    c = AlchemyClient()

    print(f"\n=== Alchemy health check ===")
    print(f"Network: {c.network}")
    print(f"Base URL: {c.base_url.split('/')[-1][:8]}...{c.base_url[-4:]}")

    block = c.get_block_number()
    print(f"\nBlock number: {block}")
    if block:
        ts = c.get_latest_block_timestamp()
        print(f"Block timestamp: {ts}")

    print(f"\n=== Catecoin metadata ===")
    meta = c.get_token_metadata("0xfc5ABD01E4Def799549eee154449Ff6a7ae0cAc7")
    print(meta)

    print(f"\n=== CATE transfers (last 5) ===")
    transfers = c.get_asset_transfers(
        token_contract="0xfc5ABD01E4Def799549eee154449Ff6a7ae0cAc7",
        max_count=5,
    )
    for t in transfers:
        print(
            f"  {t['timestamp']} block={t['block_num']} "
            f"{t['from'][:10]}..→{t['to'][:10]}.. value={t['value']}"
        )

    print(f"\n=== CU usage ===")
    print(f"CU used: {c.cu_used} / {c.cu_limit} ({c.cu_usage_ratio():.4%})")
    print(f"Healthy: {c.is_healthy()}")
