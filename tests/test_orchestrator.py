class TestCompanyScoping:
    """Company-scoped routing helpers."""

    def test_can_access_team_respects_explicit_shared_team_whitelist(self):
        from companest.companies.company import CompanyConfig
        from companest.orchestration.orchestrator import CompanestOrchestrator

        orch = CompanestOrchestrator.__new__(CompanestOrchestrator)
        orch.company_registry = type("Registry", (), {
            "get": lambda self, company_id: {
                "acme": CompanyConfig(id="acme", name="Acme", shared_teams=[]),
                "beta": CompanyConfig(id="beta", name="Beta", shared_teams=["general"]),
            }.get(company_id)
        })()

        assert not orch.can_access_team("acme", "general")
        assert orch.can_access_team("beta", "general")
        assert not orch.can_access_team("beta", "finance")

    def test_initialized_company_ids_include_registry_only_companies(self):
        from companest.orchestration.orchestrator import CompanestOrchestrator

        orch = CompanestOrchestrator.__new__(CompanestOrchestrator)
        orch._ceo_pis = {}
        orch.company_registry = type("Registry", (), {
            "list_companies": lambda self: ["ceo-disabled"]
        })()
        orch.team_registry = type("Teams", (), {
            "list_teams": lambda self: []
        })()

        assert orch._initialized_company_ids() == ["ceo-disabled"]
