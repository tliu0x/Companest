import json
from pathlib import Path

from fastapi.testclient import TestClient

from companest.config import CompanestConfig
from companest.jobs import JobManager
from companest.orchestrator import CompanestOrchestrator
from companest.server import CompanestAPIServer


def _write_minimal_runtime(base: Path) -> None:
    general_pi = base / "teams" / "general" / "pis" / "assistant"
    general_pi.mkdir(parents=True)
    (base / "teams" / "general" / "team.md").write_text(
        "# Team: general\n"
        "- role: General-purpose assistant\n"
        "- lead_pi: assistant\n"
        "- enabled: true\n"
        "- mode: default\n\n"
        "#### Pi: assistant\n"
        "- model: claude-sonnet-4-5-20250929\n"
        "- tools: memory_read\n"
        "- max_turns: 5\n",
        encoding="utf-8",
    )
    (general_pi / "soul.md").write_text("You are a general assistant.", encoding="utf-8")
    (base / "soul.md").write_text("Master soul", encoding="utf-8")
    (base / "user.md").write_text("# User\n- Language: English", encoding="utf-8")


def test_events_endpoint_persists_and_filters(tmp_path):
    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "examples" / "prediction-market" / "manifest.json")
        .read_text(encoding="utf-8")
    )

    _write_minimal_runtime(tmp_path)

    config = CompanestConfig(debug=True)
    orchestrator = CompanestOrchestrator(config)
    orchestrator.init_teams(str(tmp_path))
    job_manager = JobManager(orchestrator, data_dir=tmp_path)
    server = CompanestAPIServer(config, job_manager, orchestrator)

    with TestClient(server.create_app()) as client:
        create = client.post("/api/companies", json=manifest)
        assert create.status_code == 200, create.text

        submit = client.post(
            "/api/jobs",
            json={
                "task": "Summarize the top prediction markets",
                "company_id": "prediction-market",
                "submitted_by": "test-suite",
            },
        )
        assert submit.status_code == 200, submit.text
        job_id = submit.json()["job_id"]

        cancel = client.post(f"/api/jobs/{job_id}/cancel")
        assert cancel.status_code == 200, cancel.text

        events = client.get("/api/events?company_id=prediction-market&limit=20")
        assert events.status_code == 200, events.text
        events_data = events.json()
        assert events_data["total"] >= 3
        assert events_data["latest_sequence_id"] >= 3
        assert any(event["type"] == "company.created" for event in events_data["events"])
        assert any(event["type"] == "job.submitted" for event in events_data["events"])
        assert any(event["type"] == "job.cancelled" for event in events_data["events"])
        assert all(event["company_id"] == "prediction-market" for event in events_data["events"])

        filtered = client.get("/api/events?event_type=job.submitted")
        assert filtered.status_code == 200, filtered.text
        filtered_data = filtered.json()
        assert filtered_data["total"] >= 1
        assert all(event["type"] == "job.submitted" for event in filtered_data["events"])

        cancelled = client.get("/api/events?company_id=prediction-market&event_type=job.cancelled")
        assert cancelled.status_code == 200, cancelled.text
        cancelled_data = cancelled.json()
        assert cancelled_data["total"] >= 1
        assert all(event["company_id"] == "prediction-market" for event in cancelled_data["events"])
        assert all(event["type"] == "job.cancelled" for event in cancelled_data["events"])

        after_now = client.get("/api/events?start=2999-01-01T00:00:00+00:00")
        assert after_now.status_code == 200, after_now.text
        assert after_now.json()["events"] == []

        before_past = client.get("/api/events?end=2000-01-01T00:00:00+00:00")
        assert before_past.status_code == 200, before_past.text
        assert before_past.json()["events"] == []

        paged = client.get("/api/events?limit=1&offset=999")
        assert paged.status_code == 200, paged.text
        paged_data = paged.json()
        assert paged_data["events"] == []
        assert paged_data["total"] >= 3

        max_page = client.get("/api/events?limit=500")
        assert max_page.status_code == 200, max_page.text
        assert max_page.json()["limit"] == 500

    restarted_server = CompanestAPIServer(
        config,
        JobManager(data_dir=tmp_path),
        orchestrator=None,
    )
    with TestClient(restarted_server.create_app()) as restart_client:
        restored = restart_client.get("/api/events?limit=20")
        assert restored.status_code == 200, restored.text
        restored_data = restored.json()
        assert any(event["type"] == "job.submitted" for event in restored_data["events"])


def test_websocket_handshake_reports_sequence_and_missed_events(tmp_path):
    _write_minimal_runtime(tmp_path)

    config = CompanestConfig(debug=True)
    config.api.auth_token = "test-token"
    orchestrator = CompanestOrchestrator(config)
    orchestrator.init_teams(str(tmp_path))
    job_manager = JobManager(orchestrator, data_dir=tmp_path)
    server = CompanestAPIServer(config, job_manager, orchestrator)

    with TestClient(server.create_app()) as client:
        submit = client.post(
            "/api/jobs",
            json={"task": "Warm up event stream", "submitted_by": "test-suite"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert submit.status_code == 200, submit.text

        with client.websocket_connect("/ws/events") as websocket:
            websocket.send_json({
                "type": "auth",
                "token": "test-token",
                "last_sequence_id": 0,
            })
            ready = websocket.receive_json()
            assert ready["type"] == "ready"
            assert ready["sequence_id"] >= 1
            assert ready["missed_events"] is True


def test_events_endpoint_gracefully_degrades_when_store_unavailable(tmp_path, monkeypatch):
    async def fake_start(self):
        return False

    monkeypatch.setattr("companest.server.EventStore.start", fake_start)

    config = CompanestConfig(debug=True)
    server = CompanestAPIServer(config, JobManager(data_dir=tmp_path), orchestrator=None)

    with TestClient(server.create_app()) as client:
        meta = client.get("/api/meta")
        assert meta.status_code == 200, meta.text
        assert meta.json()["capabilities"]["events_history"] is False

        events = client.get("/api/events")
        assert events.status_code == 200, events.text
        events_data = events.json()
        assert events_data["events"] == []
        assert events_data["note"] == "Event history not initialized"
