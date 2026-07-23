"""Defensive token display identity helpers for DexScreener pairs."""
from __future__ import annotations

from typing import Any, Dict, Tuple

CHAIN_IDENTITY_NOISE = {"robinhood", "hood", "base", "monad"}
UNKNOWN_IDENTITY = {"", "unknown", "unk", "n/a", "na", "none", "null", "?", "???"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _is_noise(value: Any) -> bool:
    text = _clean(value)
    return text.lower() in CHAIN_IDENTITY_NOISE or text.lower() in UNKNOWN_IDENTITY


def shorten_address(address: Any) -> str:
    """Return a compact contract fallback that cannot be confused with a chain name."""
    addr = _clean(address)
    if len(addr) >= 10:
        return f"{addr[:6]}…{addr[-4:]}"
    return addr or "UNKNOWN"


def _nested(mapping: Dict[str, Any], *path: str) -> Any:
    cur: Any = mapping
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def token_identity_from_pair(pair: Dict[str, Any]) -> Tuple[str, str]:
    """Return display-safe (symbol, name) for a DexScreener pair.

    DexScreener search/profile payloads can occasionally echo chain/search terms
    into token identity fields. Reject only chain-only/unknown identities while
    preserving legitimate names that contain those words, e.g.
    "GameStop • Robinhood Token".
    """
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    address = _clean(base.get("address") or pair.get("tokenAddress"))
    fallback = shorten_address(address)

    raw_symbol = _clean(base.get("symbol"))
    raw_name = _clean(base.get("name"))

    candidates = [
        raw_symbol,
        raw_name,
        _clean(pair.get("baseTokenSymbol")),
        _clean(pair.get("baseTokenName")),
        _clean(_nested(pair, "info", "baseToken", "symbol")),
        _clean(_nested(pair, "info", "baseToken", "name")),
    ]

    # Never use quote token names such as WETH/USDG as identity unless no base
    # contract exists; even then, prefer the address fallback.
    quote_noise = {_clean(quote.get("symbol")).lower(), _clean(quote.get("name")).lower()}

    def good(value: str) -> bool:
        if _is_noise(value):
            return False
        if value.lower() in quote_noise and address:
            return False
        return True

    symbol = next((value for value in candidates if good(value)), fallback)
    name = raw_name if good(raw_name) else next((value for value in candidates if good(value) and value != symbol), symbol)
    if _is_noise(symbol):
        symbol = fallback
    if _is_noise(name):
        name = symbol
    return symbol, name


def sanitize_alert_identity(symbol: Any, name: Any, contract: Any = "") -> Tuple[str, str]:
    """Defensive final guard for alert formatters."""
    fallback = shorten_address(contract)
    safe_symbol = _clean(symbol)
    safe_name = _clean(name)
    if _is_noise(safe_symbol):
        safe_symbol = fallback
    if _is_noise(safe_name):
        safe_name = safe_symbol
    return safe_symbol, safe_name
