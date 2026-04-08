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


def test_company_registration_flow_smoke(tmp_path):
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
        meta = client.get("/api/meta")
        assert meta.status_code == 200, meta.text
        meta_data = meta.json()
        assert meta_data["capabilities"]["companies"] is True
        assert meta_data["capabilities"]["scheduler"] is True

        create = client.post("/api/companies", json=manifest)
        assert create.status_code == 200, create.text

        companies_list = client.get("/api/companies")
        assert companies_list.status_code == 200, companies_list.text
        listed_company = companies_list.json()["companies"][0]
        assert listed_company["id"] == "prediction-market"
        assert listed_company["active_team_count"] == 2
        assert "recent_job_count" in listed_company
        assert "last_activity_timestamp" in listed_company

        company_detail = client.get("/api/companies/prediction-market")
        assert company_detail.status_code == 200, company_detail.text
        company_data = company_detail.json()
        assert company_data["summary"]["active_team_count"] == 2
        assert sorted(company_data["teams"]) == [
            "prediction-market/analyst-team",
            "prediction-market/collector-team",
        ]
        assert "ceo_prediction-market" in company_data["schedule_status"]
        assert "company_prediction-market_market-collection" in company_data["schedule_status"]
        assert company_data["finance"]["hourly_budget_usd"] == 1.0

        submit = client.post(
            "/api/jobs",
            json={
                "task": "Summarize the top prediction markets",
                "company_id": "prediction-market",
                "context": {"priority": "normal"},
            },
        )
        assert submit.status_code == 200, submit.text
        job_id = submit.json()["job_id"]

        job_detail = client.get(f"/api/jobs/{job_id}")
        assert job_detail.status_code == 200, job_detail.text
        job_data = job_detail.json()
        assert job_data["company_id"] == "prediction-market"
        assert job_data["context"]["company_id"] == "prediction-market"

        companies_list = client.get("/api/companies")
        listed_company = companies_list.json()["companies"][0]
        assert listed_company["recent_job_count"] >= 1
        assert listed_company["last_activity_timestamp"] is not None
        fleet = client.get("/api/fleet/status")
        assert fleet.status_code == 200, fleet.text
        fleet_company = fleet.json()["companies"]["prediction-market"]
        assert fleet_company["active_teams"] == 2
        assert fleet_company["total_jobs"] >= 1

        delete = client.delete("/api/companies/prediction-market")
        assert delete.status_code == 200, delete.text

    assert orchestrator.team_registry.get_configs_by_company("prediction-market") == {}
    assert "ceo_prediction-market" not in orchestrator.scheduler.get_status()["tasks"]
    assert "company_prediction-market_market-collection" not in orchestrator.scheduler.get_status()["tasks"]


def test_create_company_rejects_invalid_team_id_without_persisting(tmp_path):
    _write_minimal_runtime(tmp_path)

    config = CompanestConfig(debug=True)
    orchestrator = CompanestOrchestrator(config)
    orchestrator.init_teams(str(tmp_path))
    job_manager = JobManager(orchestrator, data_dir=tmp_path)
    server = CompanestAPIServer(config, job_manager, orchestrator)

    bad_manifest = {
        "id": "bad-company",
        "name": "Bad Company",
        "domain": "Testing",
        "enabled": True,
        "teams": [
            {
                "id": "../../escape",
                "team_md": "# Team: bad\n- role: research\n- lead_pi: analyst\n",
                "pis": [],
            }
        ],
    }

    with TestClient(server.create_app()) as client:
        response = client.post("/api/companies", json=bad_manifest)

    assert response.status_code == 400
    assert orchestrator.company_registry.get("bad-company") is None


def test_uninitialized_endpoints_return_stable_note_shapes(tmp_path):
    config = CompanestConfig(debug=True)
    job_manager = JobManager(data_dir=tmp_path)
    server = CompanestAPIServer(config, job_manager, orchestrator=None)

    with TestClient(server.create_app()) as client:
        teams = client.get("/api/teams")
        assert teams.status_code == 200, teams.text
        teams_data = teams.json()
        assert teams_data["registered"] == []
        assert teams_data["active"] == []
        assert teams_data["configs"] == {}
        assert teams_data["note"] == "Teams not initialized"

        finance_summary = client.get("/api/finance/summary")
        assert finance_summary.status_code == 200, finance_summary.text
        finance_summary_data = finance_summary.json()
        assert finance_summary_data["total"] == 0.0
        assert finance_summary_data["pending_approvals"] == []
        assert finance_summary_data["note"] == "CostGate not initialized"

        finance_report = client.get("/api/finance/report?hours=12")
        assert finance_report.status_code == 200, finance_report.text
        finance_report_data = finance_report.json()
        assert finance_report_data["window_hours"] == 12
        assert finance_report_data["by_team"] == {}
        assert finance_report_data["note"] == "CostGate not initialized"

        scheduler = client.get("/api/scheduler/status")
        assert scheduler.status_code == 200, scheduler.text
        scheduler_data = scheduler.json()
        assert scheduler_data["started"] is False
        assert scheduler_data["tasks"] == {}
        assert scheduler_data["note"] == "Scheduler not initialized"

        schedules = client.get("/api/schedules")
        assert schedules.status_code == 200, schedules.text
        schedules_data = schedules.json()
        assert schedules_data["schedules"] == []
        assert schedules_data["total"] == 0
        assert schedules_data["status"]["started"] is False
        assert schedules_data["note"] == "UserScheduler not initialized"

        companies = client.get("/api/companies")
        assert companies.status_code == 200, companies.text
        companies_data = companies.json()
        assert companies_data["companies"] == []
        assert companies_data["total"] == 0
        assert companies_data["note"] == "Company registry not initialized"
