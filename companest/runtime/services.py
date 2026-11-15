"""Job-scoped access to business services owned by the control plane."""

import uuid
import httpx

from ..orchestration.cost_gate import CostDecision, CostEstimate
from ..jobs.user_scheduler import ScheduledJob


class ActorServices:
    def __init__(self, url: str, job_id: str, capability: str):
        if not url or httpx.URL(url).scheme not in ("http", "https"):
            raise ValueError("Actor requires a valid control_plane_url")
        self.url = url.rstrip("/") + f"/internal/jobs/{job_id}"
        self.capability = capability

    async def call(self, operation, payload):
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            response = await client.post(
                self.url + "/" + operation, json=payload,
                headers={"Authorization": "Bearer " + self.capability},
            )
            if response.status_code != 200:
                raise RuntimeError(f"Control-plane {operation} refused (HTTP {response.status_code})")
            return response.json()

    async def evaluate(self, task, target_team_id, target_model, priority="normal", **_):
        value = await self.call("authorize", {
            "request_id": str(uuid.uuid4()), "task": task, "team_id": target_team_id,
            "model": target_model, "priority": priority,
        })
        value["estimate"] = CostEstimate(**value["estimate"])
        return CostDecision(**value)

    async def add_job(self, **kwargs):
        return ScheduledJob(**await self.call("schedule", {"action": "add", **kwargs}))

    async def list_jobs(self, **kwargs):
        return [ScheduledJob(**item) for item in await self.call("schedule", {"action": "list", **kwargs})]

    async def cancel_job(self, job_id, **kwargs):
        return await self.call("schedule", {"action": "cancel", "job_id": job_id, **kwargs})
