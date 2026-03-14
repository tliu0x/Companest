#!/usr/bin/env python3
"""Register the prediction-market company via the Companest API.

Usage:
    python register.py [base_url]

Examples:
    python register.py                          # default: http://localhost:8000
    python register.py http://companest:8000    # custom server
    COMPANEST_API_TOKEN=xxx python register.py  # with auth
"""

import json
import os
import sys

try:
    import httpx
except ImportError:
    print("httpx required. Install with: pip install httpx")
    sys.exit(1)

base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
token = os.environ.get("COMPANEST_API_TOKEN", "")

manifest_path = os.path.join(os.path.dirname(__file__), "manifest.json")
with open(manifest_path) as f:
    manifest = json.load(f)

headers = {}
if token:
    headers["Authorization"] = f"Bearer {token}"

resp = httpx.post(f"{base}/api/companies", json=manifest, headers=headers)
resp.raise_for_status()
print(json.dumps(resp.json(), indent=2))
