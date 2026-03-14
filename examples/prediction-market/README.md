# Prediction Market Extension

This is a complete example of a company extension that monitors prediction markets across Polymarket, Kalshi, and Metaculus.

## Included Files

- `manifest.json`: the complete company definition
- `register.py`: helper script for `POST /api/companies`

## Quick Start

```bash
# 1. Start the Companest server
companest serve

# 2. Register this extension from this directory
python register.py http://localhost:8000

# With authentication:
COMPANEST_API_TOKEN=your-token python register.py http://localhost:8000
```

## What It Does

- `analyst-team`: analyzes market data and identifies notable pricing changes
- `collector-team`: fetches market data on a 10-minute schedule
- routing bindings: send prediction-related queries to `analyst-team`
- memory seed: creates a watchlist with default queries and sources

## How It Is Packaged

This extension is registered purely through the HTTP API. No file copying is required.

The manifest includes:

- inline team configurations
- Pi soul definitions
- routing bindings
- company schedules
- company-scoped memory seed

## Expected Runtime Result

After registration, you should see:

- private teams under the `prediction-market/` namespace
- a company schedule named `company_prediction-market_market-collection`
- prediction-related queries routed to `prediction-market/analyst-team`
- seeded shared memory containing `watchlist.json`
