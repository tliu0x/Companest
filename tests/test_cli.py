import sys
from types import SimpleNamespace

from companest.cli import ExitCode, cmd_job_submit, cmd_team_list


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_team_list_sends_bearer_token(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _Response(
            {
                "registered": [],
                "active": [],
                "meta": [],
                "configs": {},
            }
        )

    monkeypatch.setenv("COMPANEST_API_TOKEN", "secret-token")
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=fake_get))

    result = cmd_team_list(SimpleNamespace(api_url="http://localhost:8000"))

    assert result == ExitCode.SUCCESS
    assert captured["url"] == "http://localhost:8000/api/teams"
    assert captured["headers"] == {"Authorization": "Bearer secret-token"}


def test_job_submit_sends_bearer_token(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _Response({"job_id": "job-1", "status": "queued"})

    monkeypatch.setenv("COMPANEST_API_TOKEN", "secret-token")
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(post=fake_post))

    result = cmd_job_submit(
        SimpleNamespace(
            api_url="http://localhost:8000",
            task="test task",
        )
    )

    assert result == ExitCode.SUCCESS
    assert captured["url"] == "http://localhost:8000/api/jobs"
    assert captured["headers"] == {"Authorization": "Bearer secret-token"}
    assert captured["json"] == {"task": "test task", "submitted_by": "cli"}