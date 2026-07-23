#!/usr/bin/env python3
"""Robinhood real-time runner radar for MOON-like token transitions.

Scans DexScreener Robinhood search results, journals every observation, and
only sends Telegram when a token upgrades into candidate/entry_ready.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Iterable, List, Optional

from alert_journal import AlertJournal
from dexscreener import DexScreenerClient
from telegram_alert import TelegramAlerter

logger = logging.getLogger("catecoin-scanner.runner-radar")

QUEUE_STATE_RANK = {"observe": 0, "candidate": 1, "entry_ready": 2}
DEFAULT_QUERIES = ("robinhood", "hood", "WETH robinhood", "USDG robinhood")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _nested(pair: Dict[str, Any], *path: str, default: Any = None) -> Any:
    cur: Any = pair
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def pair_token_address(pair: Dict[str, Any]) -> str:
    return str(_nested(pair, "baseToken", "address", default="") or "")


def pair_symbol(pair: Dict[str, Any]) -> str:
    return str(_nested(pair, "baseToken", "symbol", default="UNKNOWN") or "UNKNOWN")


def pair_name(pair: Dict[str, Any]) -> str:
    return str(_nested(pair, "baseToken", "name", default=pair_symbol(pair)) or pair_symbol(pair))


def pair_holders(pair: Dict[str, Any]) -> Optional[int]:
    for key in ("holders", "holderCount", "baseTokenHolders"):
        if pair.get(key) is not None:
            return _int(pair.get(key))
    base = pair.get("baseToken") or {}
    for key in ("holders", "holderCount"):
        if base.get(key) is not None:
            return _int(base.get(key))
    return None


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


def fetch_discovery_pairs(dex: DexScreenerClient, cfg: Dict[str, Any], chain: str) -> List[Dict[str, Any]]:
    """Collect robinhood token addresses from DexScreener profiles/boosts and resolve to pairs.

    Fail-open: any endpoint error logs a warning and returns whatever resolved so far.
    """
    profiles_enabled = bool(cfg.get("discovery_profiles_enabled", True))
    boosts_enabled = bool(cfg.get("discovery_boosts_enabled", True))
    if not profiles_enabled and not boosts_enabled:
        return []
    max_tokens = int(cfg.get("discovery_max_tokens", 60) or 60)
    min_liquidity = _float(cfg.get("discovery_min_liquidity_usd", 2000), 2000)
    delay = float(cfg.get("query_delay_seconds", 0.25) or 0.25)

    addresses: List[str] = []
    seen: set[str] = set()

    def _collect(items: Iterable[Dict[str, Any]], source: str) -> None:
        for item in items or []:
            if not isinstance(item, dict) or item.get("chainId") != chain:
                continue
            address = str(item.get("tokenAddress") or "")
            if address and address not in seen:
                seen.add(address)
                addresses.append(address)
        logger.info("runner-radar discovery %s: %d chain-matching tokens", source, len(addresses))

    try:
        if profiles_enabled:
            _collect(dex.get_token_profiles(), "token-profiles")
        if boosts_enabled:
            _collect(dex.get_token_boosts_latest(), "token-boosts/latest")
            _collect(dex.get_token_boosts_top(), "token-boosts/top")
    except Exception as e:
        logger.warning("runner-radar discovery endpoints failed, continuing search-only: %s", e)

    pairs: List[Dict[str, Any]] = []
    for i in range(0, min(len(addresses), max_tokens), 30):
        batch = addresses[i:i + 30]
        try:
            for pair in dex.get_tokens_batch(batch):
                if pair.get("chainId") != chain:
                    continue
                if _float((pair.get("liquidity") or {}).get("usd")) < min_liquidity:
                    continue
                pairs.append(pair)
        except Exception as e:
            logger.warning("runner-radar discovery token resolution failed, continuing: %s", e)
        time.sleep(delay)
    logger.info("runner-radar discovery: %d addresses -> %d pairs above $%.0f liquidity", len(addresses), len(pairs), min_liquidity)
    return pairs


def fetch_runner_pairs(dex: DexScreenerClient, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    queries = cfg.get("search_queries") or DEFAULT_QUERIES
    chain = cfg.get("dexscreener_chain", "robinhood")
    max_pairs = int(cfg.get("max_pairs", 40) or 40)
    found: List[Dict[str, Any]] = []
    for query in queries:
        for pair in dex.search(str(query)):
            if pair.get("chainId") == chain:
                found.append(pair)
        time.sleep(float(cfg.get("query_delay_seconds", 0.25) or 0.25))
        if len(found) >= max_pairs * 2:
            break
    search_pairs = dedupe_pairs(found)
    search_pairs.sort(key=lambda p: _float((p.get("liquidity") or {}).get("usd")), reverse=True)
    discovery_pairs = dedupe_pairs(fetch_discovery_pairs(dex, cfg, chain))
    discovery_pairs.sort(key=lambda p: _float((p.get("liquidity") or {}).get("usd")), reverse=True)
    # Reserve a lane for discovery-sourced pairs so trending micro-caps are not
    # crowded out by large static search results (and vice versa).
    discovery_lane = min(int(cfg.get("discovery_max_pairs", 20) or 20), max_pairs)
    search_lane = max_pairs - discovery_lane
    merged: List[Dict[str, Any]] = []
    seen_keys: set = set()
    for pair in discovery_pairs[:discovery_lane] + search_pairs[:search_lane]:
        key = pair.get("pairAddress") or _nested(pair, "baseToken", "address")
        if key and key not in seen_keys:
            seen_keys.add(key)
            merged.append(pair)
    logger.info(
        "runner-radar pairs: search=%d discovery=%d merged=%d (lane=%d/%d)",
        len(search_pairs), len(discovery_pairs), len(merged), search_lane, discovery_lane,
    )
    return merged


def score_runner_pair(pair: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    liquidity = _float(_nested(pair, "liquidity", "usd"))
    volume_24h = _float(_nested(pair, "volume", "h24"))
    fdv = _float(pair.get("fdv") or pair.get("marketCap"))
    market_cap = _float(pair.get("marketCap"))
    price = _float(pair.get("priceUsd"))
    holders = pair_holders(pair)
    price_change = pair.get("priceChange") or {}
    m5 = _float(price_change.get("m5"))
    h1 = _float(price_change.get("h1"))
    h6 = _float(price_change.get("h6"))
    h24 = _float(price_change.get("h24"))
    tx5 = pair.get("txns", {}).get("m5", {}) if isinstance(pair.get("txns"), dict) else {}
    tx1 = pair.get("txns", {}).get("h1", {}) if isinstance(pair.get("txns"), dict) else {}
    buys_5m = _int(tx5.get("buys"))
    sells_5m = _int(tx5.get("sells"))
    buys_1h = _int(tx1.get("buys"))
    sells_1h = _int(tx1.get("sells"))
    vol_liq = (volume_24h / liquidity) if liquidity > 0 else 0.0
    buy_pressure_5m = buys_5m / max(buys_5m + sells_5m, 1)
    buy_pressure_1h = buys_1h / max(buys_1h + sells_1h, 1)

    score = 0
    reasons: List[str] = []
    risks: List[str] = []

    liq_candidate = _float(cfg.get("min_liquidity_candidate_usd"), 25000)
    liq_entry = _float(cfg.get("min_liquidity_entry_usd"), 50000)
    holders_candidate = int(cfg.get("min_holders_candidate", 100) or 100)
    holders_entry = int(cfg.get("min_holders_entry", 300) or 300)
    min_vol_liq = _float(cfg.get("min_volume_liquidity_ratio", 3.0), 3.0)
    min_buy_pressure = _float(cfg.get("min_buy_pressure", 0.55), 0.55)
    min_5m_buys = int(cfg.get("min_5m_buys", 5) or 5)
    candidate_score = int(cfg.get("candidate_score", 65) or 65)
    entry_score = int(cfg.get("entry_ready_score", 85) or 85)

    if liquidity >= liq_entry:
        score += 20
        reasons.append("liquidity>=entry_floor")
    elif liquidity >= liq_candidate:
        score += 14
        reasons.append("liquidity>=candidate_floor")
    else:
        risks.append("liquidity_below_runner_floor")

    if holders is None:
        score += int(cfg.get("unknown_holders_score", 0) or 0)
        risks.append("holders_unknown")
    elif holders >= holders_entry:
        score += 20
        reasons.append("holders>=entry_floor")
    elif holders >= holders_candidate:
        score += 12
        reasons.append("holders>=candidate_floor")
    else:
        risks.append("holder_count_weak")

    if vol_liq >= min_vol_liq:
        score += 20
        reasons.append("volume_liquidity_runner_ratio")
    elif vol_liq >= _float(cfg.get("watch_volume_liquidity_ratio", 1.0), 1.0):
        score += 8
        reasons.append("volume_liquidity_watch")
    else:
        risks.append("volume_liquidity_weak")

    if h6 >= _float(cfg.get("strong_h6_change_pct", 50), 50) or h24 >= _float(cfg.get("strong_h24_change_pct", 75), 75):
        score += 16
        reasons.append("strong_6h_or_24h_acceleration")
    elif h6 > 0 or h24 > 0:
        score += 8
        reasons.append("positive_medium_term_momentum")
    else:
        risks.append("medium_term_momentum_not_positive")

    if m5 > 0 and buys_5m >= min_5m_buys and buy_pressure_5m >= min_buy_pressure:
        score += 16
        reasons.append("positive_5m_buy_pressure")
    elif buy_pressure_1h >= min_buy_pressure and buys_1h >= int(cfg.get("min_1h_buys", 30) or 30):
        score += 8
        reasons.append("positive_1h_buy_pressure")
    else:
        risks.append("buy_pressure_absent")

    if h1 > 0:
        score += 8
        reasons.append("positive_1h_confirmation")
    elif h1 < 0:
        risks.append("h1_negative")

    dump_m5 = m5 <= _float(cfg.get("dump_m5_change_pct", -10), -10)
    dump_h1 = h1 <= _float(cfg.get("dump_h1_change_pct", -20), -20)
    no_buy_pressure = buy_pressure_5m < min_buy_pressure and buy_pressure_1h < min_buy_pressure
    if dump_m5 and dump_h1:
        score -= 30
        risks.append("late_dump_5m_and_1h")
    if no_buy_pressure:
        score -= 18

    score = max(0, min(100, score))
    if score >= entry_score and not dump_m5 and buy_pressure_5m >= min_buy_pressure and liquidity >= liq_entry:
        state = "entry_ready"
    elif score >= candidate_score and not (dump_m5 and dump_h1) and not no_buy_pressure:
        state = "candidate"
    else:
        state = "observe"

    if state == "observe" and liquidity >= liq_entry and (holders or 0) >= holders_entry and (h6 < 0 or h1 < 0 or no_buy_pressure):
        reasons.append("revival_watch_strong_base_waiting_for_buy_pressure")

    return {
        "score": score,
        "queue_state": state,
        "queue_reasons": reasons,
        "risk_factors": risks,
        "price_usd": price,
        "liquidity_usd": liquidity,
        "volume_24h": volume_24h,
        "fdv": fdv,
        "market_cap": market_cap,
        "holders": holders,
        "volume_liquidity_ratio": vol_liq,
        "m5_change_pct": m5,
        "h1_change_pct": h1,
        "h6_change_pct": h6,
        "h24_change_pct": h24,
        "buys_5m": buys_5m,
        "sells_5m": sells_5m,
        "buys_1h": buys_1h,
        "sells_1h": sells_1h,
        "buy_pressure_5m": buy_pressure_5m,
        "buy_pressure_1h": buy_pressure_1h,
    }


def build_trade_plan(pair: Dict[str, Any], metrics: Dict[str, Any]) -> Dict[str, Any]:
    price = metrics["price_usd"]
    state = metrics["queue_state"]
    return {
        "entry_zone": {"low": price * 0.97 if price else None, "high": price * 1.03 if price else None},
        "stop": price * 0.72 if price else None,
        "tp1": price * 1.35 if price else None,
        "tp2": price * 1.80 if price else None,
        "tp3": price * 2.75 if price else None,
        "max_position_usd": 20 if state == "entry_ready" else 10 if state == "candidate" else 0,
        "thesis": "; ".join(metrics["queue_reasons"]) or "Runner/revival observation only; waiting for stronger confirmation.",
        "risk_factors": metrics["risk_factors"],
        "expiry_seconds": 1800 if state == "entry_ready" else 3600,
    }


def observation_from_pair(pair: Dict[str, Any], metrics: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    state = metrics["queue_state"]
    alert_worthy = state in {"candidate", "entry_ready"}
    return {
        "alert_type": f"robinhood_runner_{state}",
        "token_symbol": pair_symbol(pair),
        "token_name": pair_name(pair),
        "token_address": pair_token_address(pair),
        "pair_address": pair.get("pairAddress") or "",
        "chain": "robinhood",
        "price_usd": metrics["price_usd"],
        "liquidity_usd": metrics["liquidity_usd"],
        "volume_24h": metrics["volume_24h"],
        "market_cap": metrics["market_cap"],
        "fdv": metrics["fdv"],
        "holders": metrics["holders"],
        "alpha_score": metrics["score"],
        "risk_level": "LOW" if state == "entry_ready" else "MEDIUM" if state == "candidate" else "LOW_CONFIDENCE",
        "thesis": plan["thesis"],
        "risk_factors": "; ".join(metrics["risk_factors"]),
        "telegram_sent": False,
        "queue_state": state,
        "queue_reasons": metrics["queue_reasons"],
        "trade_plan": plan,
        "alert_worthy": alert_worthy,
        "confidence": "high" if state == "entry_ready" else "medium" if state == "candidate" else "low",
        "dex_url": pair.get("url") or "",
    }


def should_send_transition(
    recent: Optional[dict],
    obs: Dict[str, Any],
    cooldown_seconds: int,
    *,
    telegram_enabled: bool = True,
    dry_run: bool = False,
    warmup_no_alert_first_observation: bool = True,
    telegram_min_state: str = "entry_ready",
) -> bool:
    if dry_run or not telegram_enabled:
        return False
    if QUEUE_STATE_RANK.get(str(obs.get("queue_state") or "observe"), 0) < QUEUE_STATE_RANK.get(telegram_min_state, 2):
        return False
    if obs.get("queue_state") not in {"candidate", "entry_ready"}:
        return False
    if not recent and warmup_no_alert_first_observation:
        return False
    if not recent:
        return True
    old_rank = QUEUE_STATE_RANK.get(str(recent.get("queue_state") or "observe"), 0)
    new_rank = QUEUE_STATE_RANK.get(str(obs.get("queue_state") or "observe"), 0)
    if new_rank > old_rank:
        return True
    if recent.get("telegram_sent") and cooldown_seconds:
        return False
    return old_rank < new_rank


def send_runner_alert(alerter: TelegramAlerter, obs: Dict[str, Any]) -> bool:
    category = "🔥 ROBINHOOD RUNNER ENTRY_READY" if obs["queue_state"] == "entry_ready" else "👀 ROBINHOOD RUNNER CANDIDATE"
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
        chain="robinhood",
    )


def run_scan(config: Dict[str, Any], alerter: Optional[TelegramAlerter] = None) -> Dict[str, Any]:
    cfg = config.get("runner_radar", {}) or {}
    if not cfg.get("enabled", False):
        return {"enabled": False, "telegram_sent": 0}
    journal_cfg = config.get("journal", {}) or {}
    journal = AlertJournal(db_path=journal_cfg.get("db_path", "state/alert_journal.db"), enabled=journal_cfg.get("enabled", True))
    dex = DexScreenerClient(default_chain="robinhood")
    dry_run = bool(cfg.get("dry_run", False))
    telegram_enabled = bool(cfg.get("telegram_enabled", False)) and not dry_run
    telegram_min_state = str(cfg.get("telegram_min_queue_state", "entry_ready") or "entry_ready")
    warmup = bool(cfg.get("warmup_no_alert_first_observation", True))
    max_telegram = max(0, int(cfg.get("max_telegram_per_cycle", 3) or 3))
    alerter = alerter or (TelegramAlerter.from_config(config) if telegram_enabled else None)
    pairs = fetch_runner_pairs(dex, cfg)
    cooldown_seconds = int(float(cfg.get("transition_cooldown_hours", journal_cfg.get("re_alert_cooldown_hours", 24)) or 24) * 3600)
    obs_cooldown_seconds = max(0, int(float(cfg.get("observation_cooldown_hours", 6.0) or 0) * 3600))
    since_ts = int(time.time()) - cooldown_seconds
    obs_since_ts = int(time.time()) - obs_cooldown_seconds
    observations: List[Dict[str, Any]] = []
    telegram_sent = 0
    skipped_duplicates = 0
    observations_deduped = 0

    for pair in pairs:
        metrics = score_runner_pair(pair, cfg)
        plan = build_trade_plan(pair, metrics)
        obs = observation_from_pair(pair, metrics, plan)
        recent = journal.find_recent_observation(
            chain="robinhood",
            pair_address=str(obs.get("pair_address") or ""),
            token_address=str(obs.get("token_address") or ""),
            since_ts=since_ts,
        ) if cooldown_seconds else None
        recent_obs = recent if (recent and _int(recent.get("timestamp")) >= obs_since_ts) else None
        if recent_obs and obs_cooldown_seconds:
            old_obs_rank = QUEUE_STATE_RANK.get(str(recent_obs.get("queue_state") or "observe"), 0)
            new_obs_rank = QUEUE_STATE_RANK.get(str(obs.get("queue_state") or "observe"), 0)
            if old_obs_rank >= new_obs_rank:
                observations_deduped += 1
                continue
        send_now = should_send_transition(
            recent,
            obs,
            cooldown_seconds,
            telegram_enabled=telegram_enabled,
            dry_run=dry_run,
            warmup_no_alert_first_observation=warmup,
            telegram_min_state=telegram_min_state,
        )
        if send_now and telegram_sent >= max_telegram:
            obs.setdefault("queue_reasons", []).append("telegram_cycle_cap_suppressed")
            send_now = False
        if recent and not send_now:
            old_rank = QUEUE_STATE_RANK.get(str(recent.get("queue_state") or "observe"), 0)
            new_rank = QUEUE_STATE_RANK.get(str(obs.get("queue_state") or "observe"), 0)
            if old_rank >= new_rank and obs.get("queue_state") != "observe":
                skipped_duplicates += 1
                obs.setdefault("queue_reasons", []).append("duplicate_telegram_suppressed_journal_snapshot")
        if send_now and alerter:
            obs["telegram_sent"] = send_runner_alert(alerter, obs)
            telegram_sent += 1 if obs["telegram_sent"] else 0
        obs["alert_id"] = journal.log_alert(obs)
        observations.append(obs)

    counts: Dict[str, int] = {}
    for obs in observations:
        counts[obs["queue_state"]] = counts.get(obs["queue_state"], 0) + 1
    logger.info(
        "runner-radar cycle: pairs=%d logged=%d deduped=%d telegram=%d",
        len(pairs), len(observations), observations_deduped, telegram_sent,
    )
    return {
        "enabled": True,
        "pairs_scanned": len(pairs),
        "observations_logged": len(observations),
        "observations_deduped": observations_deduped,
        "duplicates_skipped": skipped_duplicates,
        "queue_counts": counts,
        "telegram_sent": telegram_sent,
        "telegram_enabled": telegram_enabled,
        "telegram_min_state": telegram_min_state,
        "dry_run": dry_run,
        "max_telegram_per_cycle": max_telegram,
        "observations": observations,
    }
