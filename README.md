# Catecoin Price Alert Scanner

Lightweight price alert scanner for **Cate (Catecoin)** on Robinhood Chain. Polls DexScreener's free API every 60 seconds and sends Telegram alerts when price thresholds are hit.

## Architecture

```
DexScreener API (free, no key)
        │
        ▼
   scanner.py
   ├─ Fetch price every 60s
   ├─ Compare against baseline
   ├─ Check thresholds (+100%, +200%, +500%, +1000%, -50%)
   └─ Send Telegram alert on hit
        │
        ▼
   Telegram Bot API
```

**Zero API token cost**: DexScreener is completely free. 1 req/min = 1,440 req/day (limit is 300 req/min). No database, no incoming ports, no auth keys.

## Token Details

| Field | Value |
|-------|-------|
| Symbol | Cate (Catecoin) |
| Contract | `0xfc5ABD01E4Def799549eee154449Ff6a7ae0cAc7` |
| Pair Address | `0xaC366079B95E56AA2dF22dE84373e47594dc1031` |
| Chain | Robinhood (Arbitrum Orbit L2) |
| Chart | [DexScreener](https://dexscreener.com/robinhood/0xac366079b95e56aa2df22de84373e47594dc1031) |

## Alert Thresholds

Each threshold fires **once** (then marks as triggered):

| Threshold | Meaning |
-----------|---------|
| +100% | 2x from baseline |
| +200% | 3x from baseline |
| +500% | 6x from baseline |
| +1000% | 11x from baseline |
| -50% | Stop loss warning |

## Quick Start (Local)

```bash
cd /a0/usr/workdir/catecoin-scanner
pip install -r requirements.txt

# Test Telegram integration
python scanner.py --test-alert

# Single price check
python scanner.py --once

# Continuous monitoring (default, polls every 60s)
python scanner.py
```

## Telegram Configuration

The scanner reads Telegram credentials in this priority order:

1. **Environment variables** (for Docker/Akash): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
2. **Robinhood-alpha config**: `/a0/usr/workdir/robinhood-alpha/config.yaml` (under `alerts.telegram`)
3. **Scanner config**: `config.yaml` (under `telegram` section, commented out by default)

No credentials are hardcoded in the source code.

## Docker

### Build

```bash
docker build -t catecoin-scanner .
```

### Run

```bash
docker run -d \
  --name catecoin-scanner \
  -e TELEGRAM_BOT_TOKEN="your_bot_token" \
  -e TELEGRAM_CHAT_ID="your_chat_id" \
  catecoin-scanner
```

Image size: ~50MB (python:3.12-slim + requests + pyyaml).

## Akash Network Deployment

### Prerequisites

- Akash CLI (`provider-services`) installed
- AKT in wallet for deployment
- Docker image pushed to a registry (GHCR recommended)

### 1. Build and Push Image

```bash
docker build -t ghcr.io/toxmon/catecoin-scanner:latest .
docker push ghcr.io/toxmon/catecoin-scanner:latest
```

### 2. Update akash.yml

Replace the placeholder env vars in `akash.yml`:
```yaml
env:
  - TELEGRAM_BOT_TOKEN=8978112955:your_actual_token
  - TELEGRAM_CHAT_ID=6748258274
```

### 3. Deploy

```bash
# Create deployment
provider-services tx deployment create akash.yml --from wallet

# Accept first bid
provider-services tx marketplace lease-create \
  --dseq <DSEQ> --oseq 1 --gseq 1 \
  --provider <PROVIDER> --from wallet

# Send manifest
provider-services send-manifest akash.yml \
  --dseq <DSEQ> --provider <PROVIDER> --from wallet
```

### 4. Monitor

```bash
# Check lease status
provider-services query lease list --from wallet

# View logs (if provider supports it)
provider-services lease-logs --dseq <DSEQ> --provider <PROVIDER> --from wallet
```

### Cost Estimate

- **Resources**: 0.1 CPU, 128Mi RAM, 512Mi storage
- **Expected cost**: ~$5-10/month on Akash
- This is a tiny outbound-only script — no ports exposed, no database

## Files

| File | Purpose |
|------|---------|
| `scanner.py` | Main monitoring script (single file, no package structure) |
| `requirements.txt` | Python dependencies: requests, pyyaml |
| `config.yaml` | Scanner configuration (thresholds, poll interval, pair address) |
| `Dockerfile` | Container image definition (python:3.12-slim) |
| `akash.yml` | Akash Network SDL for deployment |
| `README.md` | This file |

## Constraints

- No API keys needed (DexScreener is free)
- No database (state is in-memory: `triggered` dict resets on restart)
- No incoming ports (outbound only: poll API + send Telegram)
- Total image under 50MB
- Single-file Python script — no package structure
