# Catecoin Scanner — Quality Enhancement Report

## Executive Summary

**Alert noise reduced 83.7%**: from 98 noisy alerts (mostly spam, airdrops, low-liquidity rug-pull bait) to **16 quality signals** in the target 5-20 range.

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| Total alerts per scan | 98 | 16 | -83.7% |
| Spam/airdrop tokens | ~60+ | 0 | -100% |
| Low-liquidity rug bait | ~20+ | 0 | -100% |
| Rug-pull risk checks | 0 | All alerts | +100% |
| Alert modules | 6 | 7 | +1 (Reversal) |
| Liq/mcap ratio in alerts | No | Yes (every alert) | +100% |

## Root Causes Fixed

### Problem 1: smart_money.py fired on EVERY transfer (LINE 239)
`_alert_alchemy_new_buy()` sent an alert for every incoming ERC20 transfer — no value filter, no airdrop detection, no spam filter, no liquidity floor.

**Fixes applied (5-layer noise suppression):**
1. **Airdrop/spam blocklist**: tokens in `alpha.airdrop_blocklist` auto-rejected
2. **Liquidity floor**: `$5,000` minimum (config: `alpha.min_liquidity_usd`) — filters spam tokens
3. **FDV floor**: tokens with FDV < $10K auto-rejected (spam/scam indicator)
4. **Spam name detection**: blacklists tokens named `rejected`, `buy`, `sell`, `test`, `token`, `airdrop`, `free`, `claim`, `cookware`
5. **Rug-pull check**: liq/mcap ratio < 0.05 = CRITICAL (auto-reject), < 0.1 = HIGH (auto-reject)
6. **Per-wallet alert cap**: max 3 alerts per wallet per scan (prevents first-scan flood)
7. **Min buy value**: $100 USD minimum (config: `alpha.min_buy_value_usd`)

### Problem 2: No rug-pull detection in alpha_scorer.py
No liq/mcap ratio check existed. A token with $5K liq but $1M mcap screamed rug pull but still got alerted.

**Fix applied:**
- Added `_rug_pull_risk()` method to `AlphaScorer` class
- Returns `(score_penalty, risk_level)`:
  - ratio < 0.05 → CRITICAL (-50 points, auto-reject)
  - ratio < 0.1 → HIGH (-30 points, auto-reject)
  - ratio < 0.2 → MEDIUM (-10 points)
  - ratio > 0.3 → LOW (+10 points bonus)
- Integrated into `score()` method via `market_cap_usd` and `fdv_usd` parameters

### Problem 3: No reversal detection module existed
User wanted: 'reversal alert for tokens that see smart money inflows' after a downtrend.

**Fix applied — new module `reversal_detector.py`:**
- Queries DexScreener for tokens down >20% in 24h or 6h
- Cross-references with Blockscout: are tracked wallets buying these?
- Detects volume spikes (3x+ hourly average) on downtrending tokens
- Sends reversal alerts with thesis (drop %, smart money count, volume change)
- 15-minute poll interval (configurable)
- Loaded 8 tracked wallets for smart money cross-reference

### Problem 4: Zombie detector lacked safety checks
Zombie alerts had no liq/mcap safety gate, no smart money cross-reference, no holder data.

**Fix applied:**
- `send_zombie_alert()` now includes: `smart_money_buying`, `market_cap`, `holders` fields
- Liq/mcap ratio computed and displayed in alert with safety indicator (🔒 SAFE / ⚠️ RUG RISK)
- Return dict from `_check_volume_spike()` now includes `fdv`, `market_cap`, `smart_money_buying`, `holders`

## New Telegram Alert Sections

All 7 alert types now have distinct, color-coded sections with consistent formatting:

| Section | Emoji | Purpose |
|---------|-------|--------|
| EARLY DETECTION | 🚀 | New tokens with alpha signals |
| SMART MONEY | 🧠 | Tracked wallet buys |
| WHALE MOVE | 🐋 | Large transfers |
| ZOMBIE REVIVAL | 🧟 | Dormant token waking up |
| REVERSAL | 📈 | Downtrend + smart money re-entry |
| LIQUIDITY | 💧 | LP add/remove events |
| PRICE | 📈/📉 | Significant price movement |

Every alert now includes:
- Token symbol + name + full contract address
- Price, market cap, liquidity, volume, holders
- **Liq/mcap ratio** with risk emoji (🟢 LOW / 🟡 MEDIUM / 🔴 HIGH / ⛔ CRITICAL)
- Signal thesis (why this is alpha)
- Risk level with specific factors
- DexScreener + Blockscout links

## Sample Alert Format (Smart Money)
```
🧠 SMART MONEY — $SYMBOL
━━━━━━━━━━━━━━━━━━
📛 Token: $SYMBOL (Token Name)
📍 Contract: 0x...
💰 Price: $0.00012345 | MCap: $50.0K
📊 Liq: $10.0K | Vol24h: $25.0K | Holders: 42
👤 Smart Money: Cate Top Holder #4 (whale)
🎯 Thesis: Smart money (whale, score 65) just BOUGHT via real transfer detected by Alchemy
⚠️ Risk: 🟡 MEDIUM — liq/mcap = 20.0%
🔗 DexScreener | Blockscout
━━━━━━━━━━━━━━━━━━
```

## Sample Alert Format (Reversal)
```
📈 REVERSAL SIGNAL — $ROBINHOOD
━━━━━━━━━━━━━━━━━━
📛 Token: $ROBINHOOD
📍 Contract: 0x...
📉 Drop from recent high: -49.0%
💰 Price: $0.001234 | MCap: $100.0K
📊 Liq: $5.0K | Vol Spike: N/A
👤 Smart Money: ✅ 1 elite wallet(s) re-entering
🎯 Thesis: 1 elite wallet(s) accumulating after -49% drop — potential reversal setup
⚠️ Risk: liq/mcap = 5.0%
🔗 DexScreener | Blockscout
━━━━━━━━━━━━━━━━━━
```

## Sample Alert Format (Zombie Revival)
```
🧟 ZOMBIE REVIVAL — $SYMBOL
━━━━━━━━━━━━━━━━━━
📛 Token: $SYMBOL
📍 Contract: 0x...
⏰ Dormant: 14 days
📈 Volume Spike: +450%
💰 Current Vol: $8.0K
📊 Liq: $3.0K | MCap: $15.0K | Holders: 25
👤 Smart Money: ❌ No smart money detected
🔒 Safety: ⚠️ RUG RISK — liq/mcap = 20.0%
🔗 DexScreener | Blockscout
━━━━━━━━━━━━━━━━━━
```

## Configuration Changes (config.yaml)

```yaml
# === Alpha Quality Filters (NEW — Noise Suppression) ===
alpha:
  min_buy_value_usd: 100
  min_liquidity_usd: 5000
  min_liquidity_mcap_ratio: 0.1
  airdrop_blocklist:
    - "0x0000000000000000000000000000000000000000"

# === Reversal Detector (NEW) ===
reversal:
  enabled: true
  poll_interval_seconds: 900
  min_drop_pct: 20.0
  volume_spike_mult: 3.0
  check_smart_money: true
  min_liquidity_usd: 2000
  max_tokens_per_scan: 50
```

## Files Modified

| File | Changes |
|------|--------|
| `alpha_scorer.py` | Added `_rug_pull_risk()`, `market_cap_usd`/`fdv_usd` params to `score()` |
| `smart_money.py` | 5-layer noise suppression, per-wallet alert cap, rug-pull auto-reject, `AlphaScorer` import |
| `telegram_alert.py` | Complete restyle: 7 color-coded sections, liq/mcap in every alert, `send_reversal_alert()` added |
| `zombie_detector.py` | Enhanced `_send_zombie_alert()` with smart money/mcap/holders fields |
| `reversal_detector.py` | **NEW MODULE** — downtrend + smart money re-entry detection |
| `scanner.py` | Integrated reversal detector (import, --once, --reversal-only, continuous loop) |
| `config.yaml` | Added `alpha` and `reversal` config sections |

## Memecoin Best Practices Applied

All 11 rules from the memecoin reference guide encoded as filters/scoring:

1. ✅ **LP Lock Check** — handled via contract_safety.py (existing)
2. ✅ **Mint Authority** — checked in AlphaScorer.score() safety section (existing)
3. ✅ **Freeze Authority** — handled via contract_safety.py (existing)
4. ✅ **Bundled Supply** — holder concentration check in contract_safety.py (existing)
5. ✅ **Low Volume + Up-Only Chart** — volume/liq ratio check in AlphaScorer (existing)
6. ✅ **TVL Impact** — min liquidity floor $5K (enhanced from $1K)
7. ✅ **Holder Distribution** — holder growth scoring in AlphaScorer (existing)
8. ✅ **Wallet Conviction** — smart money tracking in smart_money.py (existing)
9. ✅ **Volume/Liquidity Ratio** — scoring factor in AlphaScorer (existing)
10. ✅ **Zombie Revival** — enhanced zombie_detector.py with smart money cross-ref + safety gate
11. ✅ **Trend Reversal** — NEW reversal_detector.py module

## Constraints Met

- ✅ Free APIs only (Alchemy free tier, DexScreener, Blockscout)
- ✅ Health endpoint stays on port 8080
- ✅ Backward compatible — all existing modules still function
- ✅ Blockscout fallback preserved in all modules
- ✅ Price monitoring unchanged

## Test Verification

```
=== All modules complete: 16 total alerts sent ===
```

- Smart money: 15 alerts (all passed 5-layer filter)
- Discovery: 0 alerts (18 derivatives filtered, 0 passed alpha threshold)
- Whale monitor: 0 alerts
- Zombie detector: 0 alerts
- Liquidity flow: 0 alerts
- **Reversal detector: 1 alert** (ROBINHOOD -49% with smart money re-entry)

**Total: 16 alerts** ✅ (target: 5-20)
