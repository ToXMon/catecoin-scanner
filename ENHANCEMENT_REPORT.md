# Catecoin Scanner Enhancement Report

**Date:** 2026-07-13
**Status:** Code complete, tested locally, ready for rebuild
**Live deployment:** DSEQ 27683252 (not redeployed yet)

---

## The Problem

The deployed scanner (image `ghcr.io/toxmon/catecoin-scanner:sha-12aff43`) had a signal quality problem: discovery alerts were mostly catching **derivatives and spawn tokens** of existing Robinhood memecoins, not true alpha.

### Root Causes

1. **No derivative detection** — `token_discovery.py` would alert on any token that hit basic volume/holder thresholds. Tokens like `CATE2`, `BABYCATE`, `DOGE2`, `PEPECEO` would trigger alerts despite being obvious clones.

2. **No alpha scoring** — There was no composite score to rank tokens by actual alpha quality. A low-volume clone could trigger the same alert as a genuine early-stage token with smart money inflow.

3. **No wallet scoring** — `smart_money.py` treated all 8 tracked wallets equally. A `whale` tier wallet (large holder, no track record of profitable entries) carried the same consensus weight as a `smart_money_elite` wallet ($173K verified PnL).

4. **Consensus too generic** — Required 6+ wallets for a "strong" consensus. With only 8 wallets tracked, this meant either 75% of all wallets had to buy the same token (extremely rare), or the threshold was effectively unreachable.

---

## What Changed

### New Files

| File | Purpose |
|------|--------|
| `alpha_scorer.py` | Derivative detection + composite alpha scoring (0-100) |
| `contract_safety.py` | Free Blockscout-based contract safety checks |

### Enhanced Files

| File | Changes |
|------|---------|
| `smart_wallets.py` | Wallet scoring metadata (win_rate, ROI, PnL, consistency), tier weights |
| `smart_money.py` | WalletScorer class, weighted consensus, derivative filtering on signals |
| `token_discovery.py` | Derivative filtering pipeline, alpha scoring integration, contract safety, growth tracking |
| `config.yaml` | New `alpha` section, consensus weight thresholds, min_wallet_score |

---

## How Alpha Scoring Works

### Derivative Detection (the main noise fix)

The `DerivativeDetector` class filters clone/spawn tokens using six checks:

1. **Spam patterns** — regex matches for `TEST`, `TOKEN\d+`, `COIN\d+`, single letters
2. **Base name + derivative marker** — `CATE` + `2.0`/`v2`/`baby`/`ceo`/`king` = derivative
3. **High similarity** — SequenceMatcher ratio ≥85% to known base names (`cate`, `doge`, `cashcat`, `pepe`)
4. **Numbered variants** — `CATE2`, `CATE3` detected if `CATE` was registered as existing
5. **Long symbols** — >12 characters = likely spam
6. **Repeated characters** — `CAAATE`, `DOOGE` = suspicious

**Live scan result:** Out of 30 DexScreener trending pairs, 15 were filtered as derivatives. The old system would have alerted on all of them.

### Composite Alpha Score (0-100)

The `AlphaScorer` calculates a score from positive signals and penalties:

**Positive signals (max 100):**

| Signal | Points | Condition |
|--------|--------|----------|
| Smart money buying | +15 per wallet (max 30) | 2+ tracked wallets bought |
| Liquidity growth | +20 (scaled) | >10% liquidity growth |
| High liquidity (base) | +5 | Liquidity ≥ 4× minimum |
| Holder growth | +20 (scaled) | >20% holder growth |
| Strong holders (base) | +5 | ≥50 holders |
| Volume/liquidity ratio | +15 | Volume ≥ 2× liquidity |
| Contract verified | +8 | Verified on Blockscout |
| Mint disabled | +7 | Mint authority disabled |

**Penalties:**

| Penalty | Points | Condition |
|---------|--------|----------|
| Derivative detected | -50 | Clone/spawn token |
| Bot activity | -20 | Identical-amount repeat buys |
| Low liquidity | -15 | < $5,000 liquidity |
| Low holders | -10 | < 10 holders |

**Verdicts:**
- `ALPHA` — score ≥ 50 (threshold), alert sent
- `WATCH` — score 30-49, logged but no alert
- `REJECT` — score < 30 OR derivative detected

### Wallet Scoring

Each tracked wallet now has a 0-100 score based on:

- **Tier weight** (0-60 pts): `smart_money_elite`=60, `smart_money_whale`=48, `whale`=30
- **Consistency** (0-25 pts): from `consistency_score` in `smart_wallets.json`
- **PnL bonus** (0-15 pts): logarithmic scaling from `total_pnl_usd`

**Current wallet scores:**

| Wallet | Tier | Score |
|--------|------|-------|
| CASHCAT Insider 1 (50s timing) | elite | 99.0 |
| CASHCAT Insider 2 ($173K PnL) | elite | 100.0 |
| CASHCAT Insider 3 ($109K PnL) | elite | 100.0 |
| CASHCAT Insider 4 (3min timing) | whale | 86.3 |
| CASHCAT Insider 5 (consistent) | whale | 86.8 |
| Cate Top Holder #3 | whale | 42.5 |
| Cate Top Holder #4 | whale | 42.5 |
| Cate Top Holder #5 | whale | 42.5 |

The Cate top holders score below `min_wallet_score: 50`, so they won't generate low-quality buy signals. They're still tracked for movement but won't contribute to consensus noise.

### Weighted Consensus

Consensus is now **quality-weighted**, not just count-based:

- **Strong signal:** weighted score ≥ 1.5 (e.g., 2 elite wallets: 1.0+1.0=2.0 > 1.5)
- **Moderate signal:** weighted score ≥ 0.8 (e.g., 1 elite wallet: 1.0 > 0.8)
- Legacy count-based fallback preserved (6+ wallets = strong, 3+ = moderate)

This means **2 elite wallets buying the same token** triggers a strong consensus alert. The old system required 6+ wallets regardless of quality.

---

## Sample Alert Format

### Alpha Token Discovery

```
🚀 ALPHA DETECTED: NEWGEM

💰 New Gem
📍 CA: 0xabc123...
💵 Price: $0.00100000
📊 MC: $250,000
💧 Liquidity: $25,000
📈 24h: +250.0%
👥 Holders: 80
🔄 5m: 45B/12S
📊 Volume 24h: $80,000
⚡ Age: 2.3h

🎯 Alpha Score: 88/100 [ALPHA]
✅ Smart money: +30 (2 wallets)
✅ Liq growth: +16 (+40%)
✅ Holder growth: +12 (+60%)
✅ Vol/Liq ratio: +15 (3.2x)
✅ Safety: +15 (verified, mint_disabled)

🛡️ Contract Safety
✅ Contract verified
✅ Mint likely disabled
✅ Good holder distribution (top5: 25%)
✅ LP burned
👥 Top5: 25% | Top10: 38%
💧 LP: $25,000 (Burned)
🟢 Overall: SAFE

🔗 Chart | Explorer
⏰ 2026-07-13 15:05:39 UTC
```

### Smart Money Consensus

```
🎯 CONSENSUS SIGNAL: 2 smart wallets buying NEWGEM!

Wallets: CASHCAT Insider 1, CASHCAT Insider 2
Weighted score: 2.00 (quality-weighted)
Signal level: STRONG
💰 New Gem
CA: 0xabc123...

🎯 Alpha Score: 88/100 [ALPHA]
✅ Smart money: +30 (2 wallets)
✅ Vol/Liq ratio: +15 (3.2x)
...

🔗 Chart | Explorer
⏰ 2026-07-13 15:05:39 UTC
```

---

## Test Results

### Unit Tests

**Derivative detector** (9 test cases):
- ✅ Correctly filters: `CATE`, `CATE2`, `DOGE2`, `CASHCAT2`, `PEPECEO`, `BABYCATE`
- ✅ Correctly passes: `NEWALPHA`, `GIGA`, `WAGMI`

**Alpha scorer** (4 test cases):
- ✅ `NEWGEM` (strong fundamentals): 88/100, ALPHA, passes threshold
- ✅ `CATE2` (derivative with strong fundamentals): 20/100, REJECT (derivative penalty -50 works correctly despite +70 in positive signals)
- ✅ `WEAK` (low everything): 0/100, REJECT
- ✅ `PUMP` (volume only, no smart money): 15/100, REJECT

### Live API Tests

**Token discovery scan** (`token_discovery.py --once`):
- Found 30 pairs from DexScreener
- Filtered 15 derivative/clone tokens ← **this is the noise that was getting through before**
- 0 alerts sent (none passed alpha threshold of 50)
- Completed in <1 second

**Smart money scan** (`smart_money.py --once`):
- 8 wallets loaded, all scored correctly
- 0 alerts sent (no new buys this cycle)
- Completed in <2 seconds

---

## Constraints Maintained

- ✅ **Free APIs only** — DexScreener, Blockscout, Telegram Bot API. No Moralis, Alchemy, or paid services.
- ✅ **Akash-compatible** — Same Dockerfile pattern, health endpoint on 8080, no new dependencies.
- ✅ **Price monitoring unchanged** — `scanner.py` price module not modified.
- ✅ **config.yaml is single source of truth** — All new thresholds in `alpha:` and `smart_money:` sections.
- ✅ **Backward compatible** — Same CLI flags (`--once`, `--config`), same module structure.

---

## Deployment Next Steps

Code is ready but **not deployed**. To deploy:

1. Commit changes to git
2. Push to trigger GitHub Actions CI/CD
3. New image builds automatically at `ghcr.io/toxmon/catecoin-scanner:sha-<new>`
4. Redeploy on Akash with new image tag (update SDL `akash-deploy.yml`)
5. Verify health endpoint responds on port 8080
6. Monitor Telegram alerts over first 24h to confirm derivative filtering works in production

---

## File Summary

```
catecoin-scanner/
├── alpha_scorer.py         ← NEW (358 lines)
├── contract_safety.py      ← NEW (225 lines)
├── smart_money.py          ← ENHANCED (524 lines, was 359)
├── token_discovery.py      ← ENHANCED (580 lines, was 380)
├── smart_wallets.json      ← ENHANCED (185 lines, was 77)
├── config.yaml             ← ENHANCED (60 lines, was 47)
├── scanner.py              ← UNCHANGED
├── dexscreener.py          ← UNCHANGED
├── blockscout.py           ← UNCHANGED
├── telegram_alert.py       ← UNCHANGED
├── health_server.py        ← UNCHANGED
└── requirements.txt        ← UNCHANGED (no new dependencies)
```
