# Prediction Market Extension

Example Companest extension that monitors prediction markets (Polymarket, Kalshi, Metaculus).

## Quick Start

```bash
# 1. Start Companest server
companest serve

# 2. Register this extension (from this directory)
python register.py http://localhost:8000

# With authentication:
COMPANEST_API_TOKEN=your-token python register.py http://localhost:8000
```

## What It Does

- **analyst-team**: Analyzes prediction market data, identifies mispriced markets
- **collector-team**: Periodically fetches market data (every 10 minutes)
- **Routing**: Queries about prediction markets auto-route to analyst-team
- **Memory seed**: Pre-configured watchlist with default queries and sources

## Architecture

This extension is registered purely via HTTP API — no file copying needed.
The `manifest.json` contains the complete company definition including
inline team configurations and Pi soul definitions.
