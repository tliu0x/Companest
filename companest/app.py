"""
Companest SDK Entry Point

Declarative Python API for the Companest incubator framework.
Users create and run autonomous AI companies in a few lines of code:

    from companest import Companest

    app = Companest()
    acme = app.company("acme", domain="cross-border ecommerce")
    acme.add_team("marketing", role="marketing", pis=[...])
    acme.goals("monitor competitor prices", "generate weekly analysis reports")
    app.run()
"""

import asyncio
import logging
import signal
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

from .company import (
    CompanyConfig,
    CompanyCEOConfig,
    CompanyPreferences,
    CompanyRegistry,
    _validate_company_id,
)
from .config import CompanestConfig
from .exceptions import CompanestError
from .pi import PiConfig
from .team import TeamConfig
from .templates import get_template, TemplateNotFoundError

logger = logging.getLogger(__name__)


class CompanyBuilder:
    """
    Fluent builder for company configuration.

    All methods only modify an in-memory model. No disk IO happens
    until materialize() is called (by Companest.run() or explicitly).
    """

    def __init__(
        self,
        app: "Companest",
        company_id: str,
        *,
        name: Optional[str] = None,
        domain: str = "",
        **kwargs: Any,
    ):
        _validate_company_id(company_id)
        self._app = app
        self._company_id = company_id
        self._name = name or company_id
        self._domain = domain
        self._goals: List[str] = []
        self._teams: List[dict] = []  # raw team dicts for materialize
        self._budget_hourly: float = kwargs.get("budget_hourly", 1.0)
        self._budget_monthly: float = kwargs.get("budget_monthly", 200.0)
        self._ceo_model: Optional[str] = None
        self._ceo_cycle_interval: int = 1800
        self._ceo_max_turns: int = 50
        self._output_sinks: list = []
        self._extra: Dict[str, Any] = kwargs

    #  Template Support 

    @classmethod
    def from_template(
        cls,
        app: "Companest",
        company_id: str,
        template_name: str,
        **overrides: Any,
    ) -> "CompanyBuilder":
        """Create a CompanyBuilder pre-populated from a built-in template.

        Args:
            app: Parent Companest instance.
            company_id: Unique company identifier.
            template_name: Name of the built-in template (e.g. "ecommerce").
            **overrides: Override any template defaults (name, domain, etc.).

        Returns:
            A CompanyBuilder with goals, budget, and teams from the template.
        """
        tpl = get_template(template_name)

        name = overrides.pop("name", tpl.get("name", company_id))
        domain = overrides.pop("domain", tpl.get("domain", ""))

        budget = tpl.get("budget", {})
        budget_hourly = overrides.pop("budget_hourly", budget.get("hourly", 1.0))
        budget_monthly = overrides.pop("budget_monthly", budget.get("monthly", 200.0))

        builder = cls(
            app,
            company_id,
            name=name,
            domain=domain,
            budget_hourly=budget_hourly,
            budget_monthly=budget_monthly,
            **overrides,
        )

        # Pre-populate goals
        for goal in tpl.get("goals", []):
            builder._goals.append(goal)

        # Pre-populate teams
        for team_def in tpl.get("teams", []):
            builder.add_team(
                team_id=team_def["team_id"],
                role=team_def.get("role", "general"),
                pis=team_def.get("pis"),
                lead_pi=team_def.get("lead_pi"),
                mode=team_def.get("mode", "default"),
            )

        return builder

    #  Fluent API 

    def add_team(
        self,
        team_id: str,
        role: str = "general",
        pis: Optional[List[dict]] = None,
        lead_pi: Optional[str] = None,
        mode: str = "default",
    ) -> "CompanyBuilder":
        """Add a private team. pis accepts dicts for brevity.

        Minimal pi dict: {"id": "analyst", "soul": "You are an analyst..."}
        Full pi dict: {"id": "analyst", "model": "claude-sonnet-4-5", "soul": "...", "tools": "researcher"}
        """
        self._teams.append({
            "team_id": team_id,
            "role": role,
            "pis": pis or [],
            "lead_pi": lead_pi,
            "mode": mode,
        })
        return self

    def goals(self, *goals: str) -> "CompanyBuilder":
        """Set company operating goals (injected into CEO soul)."""
        self._goals.extend(goals)
        return self

    def budget(
        self, hourly: float = 1.0, monthly: float = 200.0,
    ) -> "CompanyBuilder":
        """Set budget caps."""
        self._budget_hourly = hourly
        self._budget_monthly = monthly
        return self

    def ceo(
        self,
        model: Optional[str] = None,
        cycle_interval: int = 1800,
        max_turns: int = 50,
    ) -> "CompanyBuilder":
        """Customize CEO agent configuration."""
        if model is not None:
            self._ceo_model = model
        self._ceo_cycle_interval = cycle_interval
        self._ceo_max_turns = max_turns
        return self

    def output(self, *sinks: Any) -> "CompanyBuilder":
        """Register output sinks for CEO cycle results."""
        self._output_sinks.extend(sinks)
        return self

    #  Export 

    def to_config(self) -> CompanyConfig:
        """Export as CompanyConfig (pure data, no IO)."""
        ceo_kwargs: Dict[str, Any] = {
            "cycle_interval": self._ceo_cycle_interval,
            "max_turns": self._ceo_max_turns,
        }
        if self._ceo_model:
            ceo_kwargs["model"] = self._ceo_model
        if self._goals:
            ceo_kwargs["goals"] = list(self._goals)

        return CompanyConfig(
            id=self._company_id,
            name=self._name,
            domain=self._domain,
            preferences=CompanyPreferences(
                budget_hourly_usd=self._budget_hourly,
                budget_monthly_usd=self._budget_monthly,
            ),
            ceo=CompanyCEOConfig(**ceo_kwargs),
        )

    def materialize(self, base_dir: Path) -> None:
        """Write company config + team files to disk.

        Called by Companest.materialize(). Not meant to be called directly
        unless you know what you're doing.
        """
        import yaml

        companies_dir = base_dir / "companies" / self._company_id
        companies_dir.mkdir(parents=True, exist_ok=True)

        # Write company.yaml
        config = self.to_config()
        config_path = companies_dir / "company.yaml"
        data = config.model_dump(exclude_none=False)
        text = yaml.dump(
            data, default_flow_style=False, allow_unicode=True, sort_keys=False,
        )
        config_path.write_text(text, encoding="utf-8")
        logger.info(f"[Companest] Materialized company config: {config_path}")

        # Write private teams
        for team_def in self._teams:
            self._materialize_team(companies_dir, team_def)

        # Write CEO goals into soul.md (augment existing or create)
        if self._goals:
            self._materialize_ceo_goals(base_dir)

    def _materialize_team(self, companies_dir: Path, team_def: dict) -> None:
        """Write team.md and pi soul.md files for a private team."""
        team_id = team_def["team_id"]
        team_dir = companies_dir / "teams" / team_id
        team_dir.mkdir(parents=True, exist_ok=True)

        pis = team_def.get("pis", [])
        lead_pi = team_def.get("lead_pi")
        if not lead_pi and pis:
            lead_pi = pis[0].get("id", "agent")

        # Generate team.md
        team_md_path = team_dir / "team.md"
        lines = [
            f"# Team: {team_id}",
            f"- role: {team_def.get('role', 'general')}",
        ]
        if lead_pi:
            lines.append(f"- lead_pi: {lead_pi}")
        lines.append("")

        # Pi sections
        for pi_def in pis:
            pi_id = pi_def.get("id", "agent")
            model = pi_def.get("model", "deepseek-chat")
            tools = pi_def.get("tools", "researcher")
            max_turns = pi_def.get("max_turns", 10)
            lines.append(f"#### Pi: {pi_id}")
            lines.append(f"- model: {model}")
            lines.append(f"- tools: {tools}")
            lines.append(f"- max_turns: {max_turns}")
            lines.append("")

            # Write soul.md if provided
            soul = pi_def.get("soul")
            if soul:
                soul_dir = team_dir / "pis" / pi_id
                soul_dir.mkdir(parents=True, exist_ok=True)
                soul_path = soul_dir / "soul.md"
                if not soul_path.exists():
                    soul_path.write_text(soul, encoding="utf-8")

        team_md_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"[Companest] Materialized team: {self._company_id}/{team_id}")

    def _materialize_ceo_goals(self, base_dir: Path) -> None:
        """Write goals section to CEO soul.md, creating it if needed."""
        team_id = f"company-{self._company_id}"
        ceo_soul_path = (
            base_dir / "teams" / team_id / "pis" / "ceo" / "soul.md"
        )

        goals_section = "\n## Company Goals\n" + "\n".join(
            f"- {g}" for g in self._goals
        ) + "\n"

        if ceo_soul_path.exists():
            existing = ceo_soul_path.read_text(encoding="utf-8")
            if "## Company Goals" not in existing:
                ceo_soul_path.write_text(
                    existing + goals_section, encoding="utf-8",
                )
        else:
            # Create a minimal CEO soul.md with goals so they are
            # available on first run (before orchestrator generates one).
            ceo_soul_path.parent.mkdir(parents=True, exist_ok=True)
            ceo_soul_path.write_text(
                f"# CEO  {self._name}\n{goals_section}",
                encoding="utf-8",
            )
            logger.info(f"[Companest] Created CEO soul with goals: {ceo_soul_path}")


class Companest:
    """
    Top-level entry point  the incubator itself.

    Usage:
        app = Companest()
        acme = app.company("acme", domain="cross-border ecommerce")
        acme.goals("monitor competitor prices")
        app.run()
    """

    def __init__(
        self,
        data_dir: str = ".companest",
        memory_backend: str = "file",
        memory_config: Optional[Dict[str, Any]] = None,
        **config_overrides: Any,
    ):
        self._data_dir = data_dir
        self._memory_backend = memory_backend
        self._memory_config = memory_config or {}
        self._config_overrides = config_overrides
        self._builders: Dict[str, CompanyBuilder] = {}
        self._orchestrator = None
        self._materialized = False

    def company(
        self,
        company_id: str,
        *,
        name: Optional[str] = None,
        domain: str = "",
        template: Optional[str] = None,
        **kwargs: Any,
    ) -> CompanyBuilder:
        """Register a company and return a CompanyBuilder for chained config.

        Args:
            company_id: Unique company identifier.
            name: Display name (defaults to company_id).
            domain: Company domain description.
            template: Optional built-in template name (e.g. "ecommerce", "research").
                      When specified, pre-populates goals, budget, and teams.
            **kwargs: Additional overrides passed to CompanyBuilder.

        Only modifies in-memory model. Call run() or materialize() to persist.
        """
        if company_id in self._builders:
            return self._builders[company_id]

        if template is not None:
            # Build from template, allowing name/domain overrides
            overrides = dict(kwargs)
            if name is not None:
                overrides["name"] = name
            if domain:
                overrides["domain"] = domain
            builder = CompanyBuilder.from_template(
                self, company_id, template, **overrides,
            )
        else:
            builder = CompanyBuilder(
                self, company_id, name=name, domain=domain, **kwargs,
            )
        self._builders[company_id] = builder
        return builder

    def materialize(self) -> None:
        """Write all company configs and team files to disk.

        Explicit IO boundary  everything before this is pure in-memory.
        """
        base_dir = Path(self._data_dir)
        base_dir.mkdir(parents=True, exist_ok=True)

        # Also ensure companies dir under data_dir for CompanyRegistry
        (base_dir / "companies").mkdir(parents=True, exist_ok=True)

        for builder in self._builders.values():
            builder.materialize(base_dir)

        self._materialized = True
        logger.info(
            f"[Companest] Materialized {len(self._builders)} companies to {base_dir}",
        )

    def _build_config(self) -> CompanestConfig:
        """Build CompanestConfig from overrides."""
        overrides = dict(self._config_overrides)
        overrides.setdefault("data_dir", self._data_dir)
        overrides.setdefault("memory_backend", self._memory_backend)
        if self._memory_config:
            overrides.setdefault("memory_config", self._memory_config)
        return CompanestConfig(**overrides)

    def run(self, blocking: bool = True) -> None:
        """Materialize  initialize orchestrator  start all CEO cycles.

        Args:
            blocking: If True, block until interrupted (Ctrl+C).
                      If False, start in background and return.
        """
        if blocking:
            try:
                asyncio.run(self._run_async())
            except KeyboardInterrupt:
                logger.info("[Companest] Interrupted, shutting down...")
        else:
            loop = asyncio.new_event_loop()
            import threading
            t = threading.Thread(
                target=loop.run_until_complete,
                args=(self._run_async(),),
                daemon=True,
            )
            t.start()

    async def start(self) -> None:
        """Async version of run(). For callers with an existing event loop."""
        await self._run_async()

    async def stop(self) -> None:
        """Gracefully stop all company operations."""
        if self._orchestrator and hasattr(self._orchestrator, "scheduler"):
            await self._orchestrator.scheduler.stop()
            logger.info("[Companest] All companies stopped")

    async def _run_async(self) -> None:
        """Core async run loop."""
        from .orchestrator import CompanestOrchestrator

        if not self._materialized:
            self.materialize()

        config = self._build_config()
        self._orchestrator = CompanestOrchestrator(config)
        self._orchestrator.init_teams(self._data_dir)

        # Register output sinks from CompanyBuilders
        for company_id, builder in self._builders.items():
            for sink in builder._output_sinks:
                self._orchestrator.register_output_sink(company_id, sink)

        # Start scheduler (CEO cycles, enrichment, dreamer, etc.)
        if hasattr(self._orchestrator, "scheduler"):
            await self._orchestrator.scheduler.start()

        logger.info(
            f"[Companest] Running with {len(self._builders)} companies. "
            f"Press Ctrl+C to stop.",
        )

        # Keep alive until cancelled
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            await self.stop()

    @property
    def orchestrator(self):
        """Access the underlying CompanestOrchestrator (available after run/start)."""
        return self._orchestrator
