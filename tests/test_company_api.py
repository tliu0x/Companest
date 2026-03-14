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
        create = client.post("/api/companies", json=manifest)
        assert create.status_code == 200, create.text

        company_detail = client.get("/api/companies/prediction-market")
        assert company_detail.status_code == 200, company_detail.text
        company_data = company_detail.json()
        assert sorted(company_data["teams"]) == [
            "prediction-market/analyst-team",
            "prediction-market/collector-team",
        ]
        assert "ceo_prediction-market" in company_data["schedule_status"]
        assert "company_prediction-market_market-collection" in company_data["schedule_status"]

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
