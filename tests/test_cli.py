import sys
import json
from types import SimpleNamespace

from companest.cli import ExitCode, cmd_job_list, cmd_job_submit, cmd_team_list, main


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


def test_job_list_prefers_top_level_total(monkeypatch, capsys):
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        captured["timeout"] = timeout
        return _Response(
            {
                "jobs": [
                    {
                        "job_id": "job-1",
                        "task": "test task",
                        "status": "completed",
                    }
                ],
                "total": 12,
                "stats": {"total": 137},
            }
        )

    monkeypatch.setenv("COMPANEST_API_TOKEN", "secret-token")
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=fake_get))

    result = cmd_job_list(
        SimpleNamespace(
            api_url="http://localhost:8000",
            limit=20,
            status="completed",
        )
    )

    out = capsys.readouterr().out

    assert result == ExitCode.SUCCESS
    assert captured["url"] == "http://localhost:8000/api/jobs"
    assert captured["headers"] == {"Authorization": "Bearer secret-token"}
    assert captured["params"] == {"limit": 20, "status": "completed"}
    assert "Jobs (12 total)" in out


def test_session_submit_parses_scope_and_json_flags(monkeypatch, capsys):
    captured = {}
    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return _Response({"job_id": "job-1", "status": "queued"})
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(post=post))
    monkeypatch.setenv("COMPANEST_API_URL", "http://localhost:9000/")
    monkeypatch.setenv("COMPANEST_API_TOKEN", "test-token")
    monkeypatch.setattr(sys, "argv", ["companest", "job", "submit", "研究任务", "--company-id", "acme", "--team-id", "acme/research", "--workspace-id", "project", "--submitted-by", "claude-code", "--json"])
    assert main() == ExitCode.SUCCESS
    assert captured["url"] == "http://localhost:9000/api/jobs"
    assert captured["headers"] == {"Authorization": "Bearer test-token"}
    assert captured["json"] == {
        "task": "研究任务", "company_id": "acme", "submitted_by": "claude-code",
        "context": {"team_id": "acme/research", "workspace_id": "project"},
    }
    assert json.loads(capsys.readouterr().out) == {"job_id": "job-1", "status": "queued"}


def test_session_status_returns_full_result(monkeypatch, capsys):
    payload = {"job_id": "job-1", "status": "completed", "result": "完整结果" * 1000}
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=lambda *a, **kw: _Response(payload)))
    monkeypatch.setenv("COMPANEST_API_TOKEN", "test-token")
    monkeypatch.setattr(sys, "argv", ["companest", "job", "status", "job-1", "--json"])
    assert main() == ExitCode.SUCCESS
    assert json.loads(capsys.readouterr().out) == payload


def test_session_error_uses_stderr_and_nonzero_exit(monkeypatch, capsys):
    def get(*args, **kwargs):
        raise RuntimeError("service unavailable")
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=get))
    monkeypatch.setenv("COMPANEST_API_TOKEN", "test-token")
    monkeypatch.setattr(sys, "argv", ["companest", "job", "status", "job-1", "--json"])
    assert main() == ExitCode.ERROR
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err) == {"error": "service unavailable"}


def test_session_can_filter_and_cancel_jobs(monkeypatch, capsys):
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return _Response({"jobs": []})
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return _Response({"job_id": "job-1", "status": "cancelled"})
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=get, post=post))
    monkeypatch.setenv("COMPANEST_API_TOKEN", "test-token")
    monkeypatch.setattr(sys, "argv", ["companest", "job", "list", "--company-id", "acme", "--status", "running", "--json"])
    assert main() == ExitCode.SUCCESS
    assert calls[-1][1]["params"] == {"company_id": "acme", "status": "running", "limit": 20}
    assert json.loads(capsys.readouterr().out) == {"jobs": []}
    monkeypatch.setattr(sys, "argv", ["companest", "job", "cancel", "job-1", "--json"])
    assert main() == ExitCode.SUCCESS
    assert calls[-1][0].endswith("/api/jobs/job-1/cancel")
    assert json.loads(capsys.readouterr().out)["status"] == "cancelled"
