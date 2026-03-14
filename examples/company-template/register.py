#!/usr/bin/env python3
"""Register this company manifest with a running Companest server."""

import json
import os
import sys
from pathlib import Path

try:
    import httpx
except ImportError:
    print("httpx required. Install with: pip install httpx")
    sys.exit(1)


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    token = os.environ.get("COMPANEST_API_TOKEN", "")
    manifest_path = Path(__file__).with_name("manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = httpx.post(f"{base}/api/companies", json=manifest, headers=headers)
    response.raise_for_status()
    print(json.dumps(response.json(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
