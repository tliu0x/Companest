"""
Companest LiteLLM Client

Thin async HTTP client for the LiteLLM management API.
Used by CostGate for accurate spend tracking and by CLI for key management.

Endpoints used:
- POST /team/new  create a team with budget
- POST /key/generate  create a virtual key for a team
- GET  /team/info  get team spend info
- GET  /global/spend/report  total spend across all teams
- GET  /key/info  list keys
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_RETRY_BACKOFF = [1.0, 3.0]  # seconds between retries


class LiteLLMClient:
    """Async client for LiteLLM proxy management API."""

    def __init__(self, base_url: str, master_key: str):
        self.base_url = base_url.rstrip("/")
        self.master_key = master_key
        self._headers = {
            "Authorization": f"Bearer {master_key}",
            "Content-Type": "application/json",
        }
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        """Lazily create a reusable HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """Make an authenticated request to LiteLLM API with retry on network errors."""
        url = f"{self.base_url}{path}"
        last_error: Optional[Exception] = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                client = self._get_client()
                response = await client.request(
                    method, url, headers=self._headers, json=json, params=params,
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError:
                raise  # Don't retry HTTP errors (4xx/5xx)  they're not transient
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, OSError) as e:
                last_error = e
                if attempt < _MAX_RETRIES:
                    wait = _RETRY_BACKOFF[attempt] if attempt < len(_RETRY_BACKOFF) else _RETRY_BACKOFF[-1]
                    logger.warning(
                        f"[LiteLLM] Request to {path} failed (attempt {attempt + 1}): {e}, "
                        f"retrying in {wait}s"
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"[LiteLLM] Request to {path} failed after {_MAX_RETRIES + 1} attempts: {e}")
                    raise

    async def create_team(
        self, team_id: str, budget_usd: float, models: Optional[List[str]] = None,
    ) -> dict:
        """Create a team with budget on LiteLLM proxy.

        Args:
            team_id: Unique team identifier
            budget_usd: Max budget in USD
            models: Optional list of allowed model names

        Returns:
            LiteLLM team creation response
        """
        payload: Dict[str, Any] = {
            "team_alias": team_id,
            "max_budget": budget_usd,
        }
        if models:
            payload["models"] = models

        result = await self._request("POST", "/team/new", json=payload)
        logger.info(f"Created LiteLLM team '{team_id}' with ${budget_usd} budget")
        return result

    async def create_key(
        self,
        team_id: str,
        models: Optional[List[str]] = None,
        max_budget: Optional[float] = None,
    ) -> str:
        """Generate a virtual API key for a team.

        Args:
            team_id: Team to associate the key with
            models: Optional model restrictions
            max_budget: Optional per-key budget

        Returns:
            The generated virtual key string
        """
        payload: Dict[str, Any] = {"team_id": team_id}
        if models:
            payload["models"] = models
        if max_budget is not None:
            payload["max_budget"] = max_budget

        result = await self._request("POST", "/key/generate", json=payload)
        key = result.get("key", "")
        logger.info(f"Generated key for team '{team_id}'")
        return key

    async def get_team_spend(self, team_id: str) -> float:
        """Get total spend for a team.

        Args:
            team_id: Team identifier

        Returns:
            Total spend in USD
        """
        try:
            result = await self._request(
                "GET", "/team/info", params={"team_id": team_id},
            )
            return float(result.get("spend", 0.0))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return 0.0
            raise

    async def get_total_spend(self) -> dict:
        """Get global spend report.

        Returns:
            Dict with total spend and per-team breakdown
        """
        try:
            result = await self._request("GET", "/global/spend/report")
            return result
        except httpx.HTTPStatusError:
            logger.warning("Failed to fetch global spend report")
            return {"total_spend": 0.0}

    async def list_keys(self) -> list:
        """List all virtual keys.

        Returns:
            List of key info dicts
        """
        result = await self._request("GET", "/key/info")
        return result if isinstance(result, list) else result.get("keys", [])

    async def get_key_spend(self, key: str) -> float:
        """Get spend for a specific virtual key.

        Args:
            key: The virtual API key

        Returns:
            Total spend in USD for this key
        """
        try:
            result = await self._request(
                "GET", "/key/info", params={"key": key},
            )
            # LiteLLM returns key info with spend field
            if isinstance(result, dict):
                return float(result.get("info", {}).get("spend", 0.0))
            return 0.0
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return 0.0
            raise

    async def reconcile_spend(self, team_id: str, local_spend: float) -> Dict[str, Any]:
        """Compare local spend tracking with LiteLLM remote spend.

        Args:
            team_id: Team identifier
            local_spend: Locally tracked spend amount

        Returns:
            Dict with remote_spend, local_spend, drift, and drift_pct
        """
        remote_spend = await self.get_team_spend(team_id)
        drift = remote_spend - local_spend
        drift_pct = (abs(drift) / max(local_spend, 0.0001)) * 100

        result = {
            "team_id": team_id,
            "remote_spend": round(remote_spend, 4),
            "local_spend": round(local_spend, 4),
            "drift": round(drift, 4),
            "drift_pct": round(drift_pct, 1),
        }

        if drift_pct > 10:
            logger.warning(
                f"[LiteLLM] Spend drift for {team_id}: "
                f"local=${local_spend:.4f} vs remote=${remote_spend:.4f} "
                f"(drift={drift_pct:.1f}%)"
            )

        return result

    async def health(self) -> bool:
        """Check if LiteLLM proxy is reachable.

        Returns:
            True if healthy, False otherwise
        """
        try:
            client = self._get_client()
            response = await client.get(f"{self.base_url}/health", timeout=5)
            return response.status_code == 200
        except Exception:
            return False
