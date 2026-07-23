#!/usr/bin/env python3
"""Base chain dry alpha scanner built from catecoin-scanner primitives.

This prototype uses DexScreener's free API, writes all observations into the
existing AlertJournal, and can optionally send conservative Telegram alerts when explicitly enabled.
Only candidate or entry-ready queue states are marked alert-worthy; default live
config sends Base entry_ready transitions only.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from alert_journal import AlertJournal
from chain_config import ChainConfig, get_chain_config
from dexscreener import DexScreenerClient
from telegram_alert import TelegramAlerter
from token_identity import token_identity_from_pair

logger = logging.getLogger("base-alpha-scanner")

DEFAULT_STATE_DB = "state/base_alert_journal.db"
QUEUE_STATE_RANK = {"observe": 0, "candidate": 1, "entry_ready": 2, "take_profit_watch": 3, "avoid_or_cut": 3}



def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def pair_token_address(pair: Dict[str, Any]) -> str:
    base_token = pair.get("baseToken") or {}
    return str(base_token.get("address") or "")


def pair_symbol(pair: Dict[str, Any]) -> str:
    symbol, _ = token_identity_from_pair(pair)
    return symbol


def pair_name(pair: Dict[str, Any]) -> str:
    _, name = token_identity_from_pair(pair)
    return name


def score_pair(pair: Dict[str, Any], cfg: ChainConfig) -> Dict[str, Any]:
    filters = cfg.filters or {}
    price = _float(pair.get("priceUsd"))
    liquidity = _float((pair.get("liquidity") or {}).get("usd"))
    volume_24h = _float((pair.get("volume") or {}).get("h24"))
    fdv = _float(pair.get("fdv") or pair.get("marketCap"))
    market_cap = _float(pair.get("marketCap"))
    price_change = pair.get("priceChange") or {}
    h1_change = _float(price_change.get("h1"))
    h24_change = _float(price_change.get("h24"))
    m5_txns = pair.get("txns", {}).get("m5", {}) if isinstance(pair.get("txns"), dict) else {}
    buys_5m = int(m5_txns.get("buys") or 0)
    sells_5m = int(m5_txns.get("sells") or 0)

    liq_fdv = (liquidity / fdv) if fdv > 0 else None
    vol_liq = (volume_24h / liquidity) if liquidity > 0 else None

    score = 0
    reasons: List[str] = []
    risks: List[str] = []

    min_liq = _float(filters.get("min_liquidity_usd"), 25000)
    min_fdv = _float(filters.get("min_fdv_usd"), 50000)
    max_fdv = _float(filters.get("max_fdv_usd"), 10000000)
    min_ratio = _float(filters.get("min_liq_fdv_ratio"), 0.08)
    min_vol_liq = _float(filters.get("min_volume_liquidity_ratio"), 0.2)
    max_vol_liq = _float(filters.get("max_volume_liquidity_ratio"), 5.0)

    if liquidity >= min_liq:
        score += 25
        reasons.append("liquidity>=25k")
    else:
        risks.append("liquidity below Base candidate floor")

    if min_fdv <= fdv <= max_fdv:
        score += 20
        reasons.append("fdv_in_alpha_range")
    elif fdv <= 0:
        risks.append("fdv unknown")
    else:
        risks.append("fdv outside target range")

    if liq_fdv is not None and liq_fdv >= min_ratio:
        score += 20
        reasons.append("liq_fdv>=8pct")
    elif liq_fdv is None:
        risks.append("liq/fdv unknown")
    else:
        risks.append("thin liquidity relative to fdv")

    if vol_liq is not None and min_vol_liq <= vol_liq <= max_vol_liq:
        score += 15
        reasons.append("volume_liquidity_balanced")
    elif vol_liq is not None and vol_liq > max_vol_liq:
        score += 5
        risks.append("volume/liquidity hot; possible churn")
    else:
        risks.append("volume/liquidity weak or unknown")

    if h1_change >= 0 and h24_change > -10:
        score += 10
        reasons.append("momentum_not_broken")
    elif h24_change <= -25:
        risks.append("24h momentum deeply negative")

    if buys_5m > sells_5m and buys_5m >= 3:
        score += 10
        reasons.append("5m_buy_pressure")

    candidate_score = int(filters.get("candidate_score", 60))
    entry_score = int(filters.get("entry_ready_score", 75))
    if score >= entry_score and h1_change >= 0 and liquidity >= min_liq:
        state = "entry_ready"
    elif score >= candidate_score:
        state = "candidate"
    else:
        state = "observe"

    return {
        "score": min(score, 100),
        "queue_state": state,
        "queue_reasons": reasons,
        "risk_factors": risks,
        "price_usd": price,
        "liquidity_usd": liquidity,
        "volume_24h": volume_24h,
        "fdv": fdv,
        "market_cap": market_cap,
        "liq_fdv_ratio": liq_fdv,
        "volume_liquidity_ratio": vol_liq,
        "h1_change_pct": h1_change,
        "h24_change_pct": h24_change,
    }


def build_trade_plan(pair: Dict[str, Any], metrics: Dict[str, Any]) -> Dict[str, Any]:
    price = metrics["price_usd"]
    liquidity = metrics["liquidity_usd"]
    state = metrics["queue_state"]

    if price <= 0:
        entry_low = entry_high = stop = tp1 = tp2 = tp3 = None
    else:
        entry_low = price * 0.97
        entry_high = price * 1.03
        stop_pct = 0.25 if liquidity < 75000 else 0.30 if liquidity < 250000 else 0.35
        stop = price * (1 - stop_pct)
        tp1 = price * 1.25
        tp2 = price * 1.50
        tp3 = price * (2.0 if liquidity < 75000 else 3.0)

    if liquidity < 25000:
        max_position = 0
    elif liquidity < 75000:
        max_position = 10
    elif liquidity < 250000:
        max_position = 20
    else:
        max_position = 35

    return {
        "entry_zone": {"low": entry_low, "high": entry_high},
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "max_position_usd": max_position if state in {"candidate", "entry_ready"} else 0,
        "thesis": "; ".join(metrics["queue_reasons"]) or "Base observation only; waiting for stronger confirmation.",
        "risk_factors": metrics["risk_factors"],
        "expiry_seconds": 3600 if state == "entry_ready" else 4 * 3600,
    }


def dedupe_pairs(pairs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for pair in pairs:
        key = str(pair.get("pairAddress") or pair_token_address(pair) or json.dumps(pair, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        out.append(pair)
    return out


def fetch_base_pairs(dex: DexScreenerClient, cfg: ChainConfig, max_pairs: int) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    queries = cfg.search_queries or ("base meme", "aerodrome base")
    for query in queries:
        for pair in dex.search(query):
            if pair.get("chainId") == cfg.dexscreener_chain:
                found.append(pair)
        time.sleep(0.25)
        if len(found) >= max_pairs * 2:
            break
    pairs = dedupe_pairs(found)
    pairs.sort(key=lambda p: _float((p.get("liquidity") or {}).get("usd")), reverse=True)
    return pairs[:max_pairs]


def observation_from_pair(pair: Dict[str, Any], metrics: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    alert_worthy = metrics["queue_state"] in {"candidate", "entry_ready"}
    token_address = pair_token_address(pair)
    return {
        "alert_type": f"base_{metrics['queue_state']}",
        "token_symbol": pair_symbol(pair),
        "token_name": pair_name(pair),
        "token_address": token_address,
        "pair_address": pair.get("pairAddress") or "",
        "chain": "base",
        "price_usd": metrics["price_usd"],
        "liquidity_usd": metrics["liquidity_usd"],
        "volume_24h": metrics["volume_24h"],
        "market_cap": metrics["market_cap"],
        "fdv": metrics["fdv"],
        "holders": None,
        "alpha_score": metrics["score"],
        "risk_level": "medium" if alert_worthy else "low_confidence",
        "thesis": plan["thesis"],
        "risk_factors": "; ".join(metrics["risk_factors"]),
        "telegram_sent": False,
        "queue_state": metrics["queue_state"],
        "queue_reasons": metrics["queue_reasons"],
        "trade_plan": plan,
        "alert_worthy": alert_worthy,
        "confidence": "high" if metrics["queue_state"] == "entry_ready" else "medium" if alert_worthy else "low",
        "dex_url": pair.get("url") or "",
    }


def _state_rank(state: str) -> int:
    return QUEUE_STATE_RANK.get(str(state or "observe"), 0)


def _telegram_allowed(state: str, telegram_min_state: str) -> bool:
    return _state_rank(state) >= _state_rank(telegram_min_state) and state in {"candidate", "entry_ready"}


def _send_base_alert(alerter: TelegramAlerter, obs: Dict[str, Any]) -> bool:
    category = "🔵 BASE ENTRY_READY" if obs["queue_state"] == "entry_ready" else "🔵 BASE CANDIDATE"
    return alerter.send_alpha_alert(
        symbol=obs.get("token_symbol", "UNKNOWN"),
        name=obs.get("token_name", "UNKNOWN"),
        contract=obs.get("token_address", ""),
        price=float(obs.get("price_usd") or 0),
        liquidity=float(obs.get("liquidity_usd") or 0),
        volume_24h=float(obs.get("volume_24h") or 0),
        holders=int(obs.get("holders") or 0),
        alpha_score=int(obs.get("alpha_score") or 0),
        thesis=obs.get("thesis", ""),
        risk_level=str(obs.get("risk_level") or "MEDIUM").upper(),
        risk_factors=obs.get("risk_factors", ""),
        market_cap=float(obs.get("market_cap") or 0),
        fdv=float(obs.get("fdv") or 0),
        category=category,
        chain="base",
    )


def run_dry_scan(
    max_pairs: int = 20,
    journal_db: str = DEFAULT_STATE_DB,
    chains_config: str | Path | None = None,
    observation_cooldown_hours: float = 6.0,
    telegram_enabled: bool = False,
    telegram_min_state: str = "entry_ready",
    config: Optional[Dict[str, Any]] = None,
    alerter: Optional[TelegramAlerter] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    cfg = get_chain_config("base", chains_config) if chains_config else get_chain_config("base")
    dex = DexScreenerClient()
    journal = AlertJournal(db_path=journal_db, enabled=True)
    if dry_run:
        telegram_enabled = False
    alerter = alerter or (TelegramAlerter.from_config(config or {"chain": "base"}) if telegram_enabled else None)
    pairs = fetch_base_pairs(dex, cfg, max_pairs=max_pairs)

    observations: List[Dict[str, Any]] = []
    skipped_duplicates = 0
    cooldown_seconds = max(0, int(float(observation_cooldown_hours or 0) * 3600))
    since_ts = int(time.time()) - cooldown_seconds
    for pair in pairs:
        metrics = score_pair(pair, cfg)
        plan = build_trade_plan(pair, metrics)
        obs = observation_from_pair(pair, metrics, plan)
        recent = journal.find_recent_observation(
            chain="base",
            pair_address=str(obs.get("pair_address") or ""),
            token_address=str(obs.get("token_address") or ""),
            since_ts=since_ts,
        ) if cooldown_seconds else None
        send_now = False
        if recent:
            old_rank = _state_rank(str(recent.get("queue_state") or "observe"))
            new_rank = _state_rank(str(obs.get("queue_state") or "observe"))
            if old_rank >= new_rank:
                skipped_duplicates += 1
                continue
            send_now = telegram_enabled and _telegram_allowed(obs.get("queue_state", "observe"), telegram_min_state)
        else:
            send_now = telegram_enabled and _telegram_allowed(obs.get("queue_state", "observe"), telegram_min_state)
        if send_now and alerter:
            obs["telegram_sent"] = _send_base_alert(alerter, obs)
        obs["alert_id"] = journal.log_alert(obs)
        observations.append(obs)

    counts: Dict[str, int] = {}
    for obs in observations:
        counts[obs["queue_state"]] = counts.get(obs["queue_state"], 0) + 1

    return {
        "mode": "dry_run" if dry_run else "live_journal",
        "chain": cfg.as_public_dict(),
        "pairs_scanned": len(pairs),
        "observations_logged": len(observations),
        "duplicates_skipped": skipped_duplicates,
        "observation_cooldown_hours": observation_cooldown_hours,
        "queue_counts": counts,
        "alert_worthy_count": sum(1 for obs in observations if obs["alert_worthy"]),
        "telegram_sent": sum(1 for obs in observations if obs.get("telegram_sent")),
        "telegram_enabled": telegram_enabled,
        "telegram_min_state": telegram_min_state,
        "observations": observations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Base dry alpha scanner")
    parser.add_argument("--once", action="store_true", help="Run one dry scan cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Force no Telegram/no trade execution behavior")
    parser.add_argument("--max-pairs", type=int, default=20)
    parser.add_argument("--journal-db", default=DEFAULT_STATE_DB)
    parser.add_argument("--observation-cooldown-hours", type=float, default=6.0)
    parser.add_argument("--chains-config", default=str(Path(__file__).with_name("chains.yaml")))
    parser.add_argument("--output", default="")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.once:
        raise SystemExit("Prototype supports --once only. Use --once --dry-run.")
    result = run_dry_scan(max_pairs=args.max_pairs, journal_db=args.journal_db, chains_config=args.chains_config, observation_cooldown_hours=args.observation_cooldown_hours)
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
