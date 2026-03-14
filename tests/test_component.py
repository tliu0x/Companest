"""Tests for CompanyComponent protocol, CompanyContext, and CompanyMemoryNamespace."""

import asyncio
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import List

import pytest

from companest.company import CompanyConfig, CompanySchedule, CompanyCEOConfig, CompanyPreferences
from companest.component import CompanyComponent, CompanyContext, CompanyMemoryNamespace
from companest.memory.manager import MemoryManager
from companest.team import TeamConfig


#  Test CompanyComponent protocol conformance 


@dataclass
class DummyComponent:
    """A minimal CompanyComponent implementation for testing."""
    _company_id: str = "test-co"
    _config: CompanyConfig = None

    def __post_init__(self):
        if self._config is None:
            self._config = CompanyConfig(
                id=self._company_id,
                name="Test Company",
                domain="testing",
            )

    @property
    def company_id(self) -> str:
        return self._company_id

    @property
    def config(self) -> CompanyConfig:
        return self._config

    def teams(self) -> List[TeamConfig]:
        return []

    def schedules(self) -> List[CompanySchedule]:
        return []

    def on_init(self, ctx: CompanyContext) -> None:
        self._init_called = True
        self._ctx = ctx

    def on_teardown(self) -> None:
        self._teardown_called = True


def test_component_protocol_conformance():
    """DummyComponent satisfies CompanyComponent protocol."""
    comp = DummyComponent()
    assert isinstance(comp, CompanyComponent)


def test_component_properties():
    comp = DummyComponent("acme")
    assert comp.company_id == "acme"
    assert comp.config.id == "acme"
    assert comp.teams() == []
    assert comp.schedules() == []


#  Test CompanyMemoryNamespace 


@pytest.fixture
def memory_dir():
    with tempfile.TemporaryDirectory() as td:
        # Create required directory structure
        (Path(td) / "teams").mkdir()
        (Path(td) / "companies" / "acme" / "shared").mkdir(parents=True)
        (Path(td) / "companies" / "acme" / "teams").mkdir(parents=True)
        yield td


@pytest.fixture
def manager(memory_dir):
    return MemoryManager(memory_dir)


@pytest.fixture
def namespace(manager):
    return CompanyMemoryNamespace(manager, "acme")


def test_namespace_company_id(namespace):
    assert namespace.company_id == "acme"


def test_namespace_scoped_team_id(namespace):
    # Company's own private team stays as-is
    assert namespace._scoped_team_id("acme/marketing") == "acme/marketing"
    # CEO team stays as-is
    assert namespace._scoped_team_id("company-acme") == "company-acme"
    # Bare private team names are scoped to the company
    assert namespace._scoped_team_id("marketing") == "acme/marketing"
    # Another company's team is rejected
    with pytest.raises(ValueError, match="cannot access"):
        namespace._scoped_team_id("other/secret")


def test_namespace_shared_read_write(namespace, memory_dir):
    namespace.write_shared("config.json", {"key": "value"})

    # Verify file is in the right place
    shared_path = Path(memory_dir) / "companies" / "acme" / "shared" / "config.json"
    assert shared_path.exists()

    data = namespace.read_shared("config.json")
    assert data == {"key": "value"}


def test_namespace_shared_list(namespace):
    namespace.write_shared("a.json", {"a": 1})
    namespace.write_shared("b.json", {"b": 2})

    keys = namespace.list_shared()
    assert "a.json" in keys
    assert "b.json" in keys


def test_namespace_shared_empty(namespace):
    keys = namespace.list_shared()
    assert keys == []


def test_namespace_team_memory_delegation(namespace, manager, memory_dir):
    """Team memory operations delegate to MemoryManager with scoped team_id."""
    # Create team memory dir
    team_dir = Path(memory_dir) / "teams" / "acme" / "marketing" / "memory"
    team_dir.mkdir(parents=True)
    namespace.write_team_memory("marketing", "data.json", {"price": 42})
    result = namespace.read_team_memory("marketing", "data.json")
    assert result == {"price": 42}
    assert manager.read_team_memory("acme/marketing", "data.json") == {"price": 42}
    keys = namespace.list_team_memory("marketing")
    assert "data.json" in keys


def test_namespace_isolation(manager, memory_dir):
    """Two namespaces for different companies cannot read each other's shared memory."""
    ns_acme = CompanyMemoryNamespace(manager, "acme")
    (Path(memory_dir) / "companies" / "beta" / "shared").mkdir(parents=True)
    ns_beta = CompanyMemoryNamespace(manager, "beta")

    ns_acme.write_shared("secret.json", {"acme": True})
    ns_beta.write_shared("secret.json", {"beta": True})

    assert ns_acme.read_shared("secret.json") == {"acme": True}
    assert ns_beta.read_shared("secret.json") == {"beta": True}


#  Test CompanyRegistry.register_component 


def test_registry_register_component():
    from companest.company import CompanyRegistry

    with tempfile.TemporaryDirectory() as td:
        registry = CompanyRegistry(td)
        comp = DummyComponent("myco")

        registry.register_component(comp)

        assert registry.get("myco") is not None
        assert registry.get("myco").name == "Test Company"
        assert registry.get_component("myco") is comp
        assert "myco" in registry.list_components()


def test_registry_component_is_none_for_yaml_only():
    from companest.company import CompanyRegistry

    with tempfile.TemporaryDirectory() as td:
        registry = CompanyRegistry(td)
        assert registry.get_component("nonexistent") is None
        assert registry.list_components() == []


def test_registry_scan_component_takes_precedence_over_yaml():
    from companest.company import CompanyRegistry

    with tempfile.TemporaryDirectory() as td:
        registry = CompanyRegistry(td)
        comp = DummyComponent("acme")
        registry.register_component(comp)

        company_dir = Path(td) / "companies" / "acme"
        company_dir.mkdir(parents=True)
        (company_dir / "company.yaml").write_text(
            "id: acme\nname: YAML Company\ndomain: yaml\nenabled: true\n",
            encoding="utf-8",
        )

        registry.scan()

        assert registry.get("acme").name == "Test Company"


def test_company_config_shared_teams_default_is_unrestricted():
    cfg = CompanyConfig(id="acme", name="Acme")
    assert cfg.shared_teams is None


def test_job_submit_normalizes_top_level_company_id_into_context():
    from companest.jobs import JobManager

    jm = JobManager.__new__(JobManager)
    jm._jobs = {}
    jm._queue = asyncio.Queue()

    async def _persist_job(_job):
        return None

    jm._persist_job = _persist_job

    async def run():
        job_id = await JobManager.submit(jm, "Assess market", company_id="acme")
        return jm._jobs[job_id]

    job = asyncio.run(run())

    assert job.company_id == "acme"
    assert job.context["company_id"] == "acme"
