# I'm Running a Crypto Token Scanner for $1.37/Month on Akash Network

*The same workload on AWS would cost $294/month. Here's what I built, how it works, and where this approach falls short.*

---

## The Problem

I wanted to track a meme coin called Catecoin across three dimensions: price changes, what smart money wallets were doing, and new tokens appearing on the chain. The goal was simple — get a Telegram message when something interesting happened.

The standard answer for running something like this is AWS. Spin up an ECS Fargate task, grab a Moralis or Alchemy API key for blockchain data, pay CoinGecko for price feeds. When I priced it out, the numbers were ugly:

| Component | AWS / Paid APIs | Monthly Cost |
|-----------|---------------|-------------|
| ECS Fargate (0.5 vCPU, 0.5GB RAM, 1GB storage) | AWS | $17.17 |
| Moralis Web3 API Pro | Replaces Blockscout | $49.00 |
| Alchemy Enhanced API Growth | Replaces Blockscout | $99.00 |
| CoinGecko Pro API | Replaces DexScreener | $129.00 |
| **Total** | | **$294.17** |

Three hundred dollars a month to poll some APIs and send myself Telegram messages. That's $3,500 a year for a side project that tracks a meme coin. I build monitoring tools because they're useful, not because I want to pay enterprise SaaS prices for them.

The problem wasn't just the compute cost. It was the API economics. Moralis, Alchemy, and CoinGecko all charge for access to blockchain data that is, by definition, public. The blockchain is a public ledger. Why am I paying $99/month to read it?

---

## The Approach

I split the problem in half: compute and data.

### Compute: Akash Network

[Akash](https://akash.network/) is a decentralized compute marketplace. Instead of renting a VM from AWS, you post a deployment spec and providers bid on it. The cheapest bid wins. It's like Uber surge pricing in reverse — providers compete downward on price.

I'd heard about Akash but assumed it was either a gimmick or too hard to use. The gimmick assumption was wrong (it's real infrastructure). The difficulty assumption was half right — my first three deploy attempts failed before I got the SDL right.

### Data: Free Public APIs

The blockchain data problem has a free solution most people skip past:

- **DexScreener** — free price, liquidity, and volume data for DEX pairs. No API key. Rate limit around 300 requests per minute. I need one request per minute.
- **Blockscout** — open-source block explorer with a free API. Returns token transfers, holders, and address activity. Same data Moralis and Alchemy charge for.
- **Telegram Bot API** — free, has no meaningful rate limit for a single-user bot.

The paid API providers charge for rate limits, historical data archives, and SLAs. For a real-time monitoring tool that only cares about what's happening *right now*, I don't need any of those things.

---

## The Solution

### What I Built

A single-process Python scanner with three modules running on independent timers:

1. **Price Monitor** — polls DexScreener every 60 seconds for the Catecoin pair. Fires alerts at +100%, +200%, +500%, +1000%, and -50% from baseline.
2. **Smart Money Tracker** — polls 8 verified wallets via Blockscout every 5 minutes. Alerts when a tracked wallet buys a new token.
3. **Token Discovery** — scans DexScreener trending and Blockscout's newest tokens list every 10 minutes. Flags tokens with unusual volume or liquidity patterns.

All three write to the same process, share API clients, and send alerts through a single Telegram bot. Total image size: 50MB. No database — state lives in memory and resets on restart, which is fine for a monitoring tool.

### Architecture

```
Free APIs                    Scanner Process              Output
────────────                ──────────────────            ─────────
DexScreener  ─── 60s ───▶   Price Monitor    ───┐
                            (threshold alerts)   │
                                                 ├──▶  Telegram Bot
Blockscout   ─── 5min ──▶   Smart Money      ───┤     (free)
                            (wallet tracking)    │
                                                 │
DexScreener  ─── 10min ─▶   Token Discovery  ───┘
Blockscout                  (early detection)

Runs on: Akash Network (0.5 CPU, 512Mi RAM, 1Gi storage)
Cost: 3.163 uact/block × 14,400 blocks/day = $1.37/month
```

### The Akash Deployment

Here's the actual SDL (Stack Definition Language) that's running right now, deployment sequence DSEQ 27683252:

~~~yaml
version: "2.0"

services:
  catecoin-scanner:
    image: ghcr.io/toxmon/catecoin-scanner:sha-12aff43
    env:
      - TELEGRAM_BOT_TOKEN=REPLACE_AT_DEPLOY
      - TELEGRAM_CHAT_ID=REPLACE_AT_DEPLOY
    expose:
      - port: 8080
        as: 80
        to:
          - global: true

profiles:
  compute:
    catecoin-scanner:
      resources:
        cpu:
          units: 0.5
        memory:
          size: 512Mi
        storage:
          size: 1Gi

  placement:
    akash:
      pricing:
        catecoin-scanner:
          denom: uact
          amount: 1000

deployment:
  catecoin-scanner:
    akash:
      profile: catecoin-scanner
      count: 1
~~~

When I deployed this, I got 7 bids from providers within 15 seconds. I took the cheapest. That bid has been running for weeks without interruption.

### Handling API Failures

Free APIs don't have SLAs. They go down. I learned this the hard way during testing when Blockscout had an outage and my scanner started throwing errors every 5 minutes.

The fix was a degraded mode in the Blockscout client. After 5 consecutive failures, the client stops making requests for 60 seconds instead of hammering a dead API:

~~~python
# All retries exhausted
self._consecutive_failures += 1
if self._consecutive_failures >= 5:
    self._degraded = True
    logger.error("Blockscout appears to be DOWN — entering degraded mode")
    # Schedule recovery
    threading.Timer(60.0, self._recover).start()
~~~

The DexScreener client has similar logic for 429 rate limit responses — exponential backoff with 3 retries before giving up on that request.

---

## Results

### Cost Comparison

| | Akash + Free APIs | AWS + Paid APIs |
|---|---|---|
| Compute | $1.37/mo (0.5 CPU, 512Mi RAM) | $17.17/mo (ECS Fargate equivalent) |
| Price data API | $0 (DexScreener free) | $129.00/mo (CoinGecko Pro) |
| Blockchain data API | $0 (Blockscout free) | $99.00/mo (Alchemy) or $49.00/mo (Moralis) |
| Alert delivery | $0 (Telegram Bot API) | $0 (Telegram Bot API) |
| **Total monthly** | **$1.37** | **$294.17** |
| **Annual** | **$16.44** | **$3,530.04** |
| **Multiplier** | **1x** | **215x** |

Compute alone is 12x cheaper on Akash. But the real multiplier comes from not paying for API access to public data. That's where the gap goes from 12x to 215x.

### Proof It's Running

This is live right now. The health endpoint responds:

```bash
curl http://oo7qeiol8lfnd5hipdqqkj1ba4.ingress.akt.engineer/health
{"status":"ok"}
```

Price monitor logs from the last hour, polling every 60 seconds:

```
Price: $0.00006425 | Δ: -0.3% | Liq: $22648 | Vol24h: $81741
Price: $0.00006412 | Δ: -0.5% | Liq: $22580 | Vol24h: $81820
Price: $0.00006430 | Δ: -0.2% | Liq: $22691 | Vol24h: $81755
```

Token discovery logs, scanning every 10 minutes:

```
[2026-07-12 08:10:00] Scanning DexScreener trending...
[2026-07-12 08:10:02] Found 30 pairs
[2026-07-12 08:10:04] 5 alerts sent
```

All three modules are running, all alerts are firing, and my monthly bill is less than the cost of a coffee.

---

## Why This Works (And Where It Doesn't)

The cost gap isn't magic. It's two specific things:

**1. Decentralized compute is cheaper for small workloads.** Akash providers compete on price because they're trying to fill idle capacity. My workload is tiny — 0.5 CPU and 512MB RAM doing periodic HTTP requests. For providers with spare capacity, any bid above their marginal cost is profit. AWS prices for committed enterprise usage; Akash prices for spare capacity arbitrage. For small, stateless workloads, Akash wins.

**2. Public blockchain data should be free to read.** Blockchains are public ledgers. Blockscout is an open-source explorer that serves the same data as paid APIs. DexScreener provides real-time DEX pricing as a free public good. The paid API providers add value through historical archives, higher rate limits, and normalized data formats. If you don't need those extras, you're paying for nothing.

---

## What I'd Do Differently

This section matters more than the cost savings. If you're considering this approach, read it.

**Akash can evict you.** During testing, my deployment got evicted once when a provider went offline. The scanner stopped running and I didn't notice for a few hours. AWS would have auto-migrated the task. Akash doesn't do that — if your provider disappears, your workload disappears until you redeploy. For anything mission-critical, you need redundancy across providers or an alerting layer that detects eviction and redeploys automatically. I don't have that yet.

**Free APIs have real rate limits.** DexScreener caps at roughly 300 requests per minute. That's fine for my scanner (1 request per minute), but if I scaled to tracking 50 tokens, I'd hit the ceiling fast. Blockscout is generous but has outages — sometimes daily during peak load. My degraded mode handles this, but degraded mode means you're flying blind during outages. If you need guaranteed data availability, paid APIs exist for a reason.

**There's no SLA.** Decentralized compute is best-effort. My $1.37/month doesn't buy me anyone to call when things break. AWS support costs more, but it exists. If this scanner were part of a trading system where downtime meant lost money, I'd pay the premium. It's a side project, so I accept the risk.

**The Akash learning curve is real.** It took me three failed deployment attempts to get the SDL right. The Akash docs are decent but assume familiarity with deployment concepts that aren't obvious if you're used to `docker run` or `kubectl apply`. The SDL format is specific to Akash, the bid/lease workflow is unfamiliar, and the CLI flags for `provider-services` are underdocumented. Budget a few hours for your first deployment, not 15 minutes.

**The math assumes a specific workload profile.** My scanner is small, stateless, and tolerant of interruptions. If I were running a database, a high-traffic API, or anything with persistent state, Akash would be harder to justify. The savings come from matching the right infrastructure to the right workload — not from Akash being universally better.

---

## The Takeaway

I'm not arguing everyone should move everything to Akash. I'm arguing that for a specific category of workload — small, stateless, real-time monitoring tools that read public data — the traditional cloud + paid API stack is dramatically overpriced.

My scanner runs for $1.37/month. The equivalent AWS setup costs 215 times more. The savings come from two places: decentralized compute for tiny workloads, and free public APIs for data that was always meant to be public.

If you're building a monitoring tool, a scraper, a webhook handler, or anything that polls public APIs and sends alerts, run the numbers. You might be paying 215x more than you need to.

The code is [on GitHub](https://github.com/ToXMon/catecoin-scanner). The deployment is live on Akash. The Telegram alerts are firing. And my monthly cloud bill is $1.37.

---

*Built and deployed by [Tolu](https://github.com/ToXMon). Deployment DSEQ 27683252 on Akash Network. If you want to build something similar, the [Akash SDL](https://github.com/ToXMon/catecoin-scanner/blob/main/akash.yml) and [full scanner code](https://github.com/ToXMon/catecoin-scanner) are open source.*
