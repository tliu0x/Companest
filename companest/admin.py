"""
Companest Admin UI  NiceGUI-based web dashboard

Provides a browser-based admin interface mounted on the existing FastAPI app.
All data comes from the orchestrator instance directly (same process).

Pages:
    /admin/login        Token login
    /admin/             Dashboard overview
    /admin/companies    Company management
    /admin/finance      Spending charts & reports
    /admin/teams        Team overview (read-only)
    /admin/bindings     Global binding management
    /admin/scheduler    Scheduled tasks status

Usage:
    from companest.admin import init_admin
    init_admin(app, orchestrator, auth_token)

Requires: pip install nicegui>=2.0.0
"""

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .orchestrator import CompanestOrchestrator

logger = logging.getLogger(__name__)


def _require_auth(func):
    """Decorator: redirect to /admin/login if not authenticated."""
    from functools import wraps

    @wraps(func)
    def wrapper(*args, **kwargs):
        from nicegui import app
        if not app.storage.user.get("authenticated"):
            from nicegui import ui
            ui.navigate.to("/admin/login")
            return
        return func(*args, **kwargs)
    return wrapper


def init_admin(
    fastapi_app,
    orchestrator: "CompanestOrchestrator",
    auth_token: str,
) -> None:
    """Mount NiceGUI admin UI on the existing FastAPI app."""
    from nicegui import ui, app

    #  Login 

    @ui.page("/admin/login")
    def login_page():
        def try_login():
            if token_input.value == auth_token:
                app.storage.user["authenticated"] = True
                ui.navigate.to("/admin/")
            else:
                ui.notify("Invalid token", type="negative")

        with ui.card().classes("absolute-center w-96"):
            ui.label("Companest Admin").classes("text-2xl font-bold mb-4")
            token_input = ui.input(
                "API Token", password=True, password_toggle_button=True,
            ).classes("w-full").on("keydown.enter", try_login)
            ui.button("Login", on_click=try_login).classes("w-full mt-2")

    #  Dashboard 

    @ui.page("/admin/")
    @_require_auth
    def dashboard_page():
        _header("Dashboard")

        with ui.row().classes("w-full gap-4"):
            # Companies card
            companies = []
            if hasattr(orchestrator, "company_registry"):
                companies = orchestrator.company_registry.list_companies()
            with ui.card().classes("flex-1"):
                ui.label("Companies").classes("text-lg font-bold")
                ui.label(f"{len(companies)} registered")

            # Teams card
            teams = []
            if hasattr(orchestrator, "team_registry"):
                teams = orchestrator.team_registry.list_teams()
            with ui.card().classes("flex-1"):
                ui.label("Teams").classes("text-lg font-bold")
                ui.label(f"{len(teams)} registered")

            # Spend card
            spend_today = 0.0
            if hasattr(orchestrator, "cost_gate"):
                summary = orchestrator.cost_gate.get_spending_summary()
                spend_today = summary.get("today", 0)
            with ui.card().classes("flex-1"):
                ui.label("Today's Spend").classes("text-lg font-bold")
                ui.label(f"${spend_today:.4f}")

            # CEO agents card
            ceo_count = len(getattr(orchestrator, "_ceo_pis", {}))
            with ui.card().classes("flex-1"):
                ui.label("CEO Agents").classes("text-lg font-bold")
                ui.label(f"{ceo_count} active")

        # Recent spending by team
        if hasattr(orchestrator, "cost_gate"):
            ui.separator().classes("my-4")
            ui.label("Spending by Team (rolling window)").classes("text-lg font-bold")
            summary = orchestrator.cost_gate.get_spending_summary()
            by_team = summary.get("by_team", {})
            if by_team:
                rows = [
                    {"team": tid, "spend": f"${amt:.4f}"}
                    for tid, amt in sorted(by_team.items(), key=lambda x: -x[1])
                ]
                columns = [
                    {"name": "team", "label": "Team", "field": "team"},
                    {"name": "spend", "label": "Spend", "field": "spend"},
                ]
                ui.table(columns=columns, rows=rows).classes("w-full")
            else:
                ui.label("No spending recorded yet.").classes("text-gray-500")

    #  Companies 

    @ui.page("/admin/companies")
    @_require_auth
    def companies_page():
        _header("Companies")

        if not hasattr(orchestrator, "company_registry"):
            ui.label("Company registry not initialized.").classes("text-gray-500")
            return

        registry = orchestrator.company_registry

        def refresh_table():
            table_container.clear()
            with table_container:
                companies = []
                for cid in registry.list_companies():
                    config = registry.get(cid)
                    if config:
                        companies.append({
                            "id": config.id,
                            "name": config.name,
                            "enabled": "Yes" if config.enabled else "No",
                            "bindings": str(len(config.bindings)),
                            "ceo": "Yes" if config.ceo.enabled else "No",
                        })
                if companies:
                    columns = [
                        {"name": "id", "label": "ID", "field": "id"},
                        {"name": "name", "label": "Name", "field": "name"},
                        {"name": "enabled", "label": "Enabled", "field": "enabled"},
                        {"name": "bindings", "label": "Bindings", "field": "bindings"},
                        {"name": "ceo", "label": "CEO", "field": "ceo"},
                    ]
                    ui.table(columns=columns, rows=companies).classes("w-full")
                else:
                    ui.label("No companies configured.").classes("text-gray-500")

        # Create company form
        with ui.expansion("Create Company", icon="add").classes("w-full mb-4"):
            id_input = ui.input("Company ID").classes("w-full")
            name_input = ui.input("Company Name").classes("w-full")
            domain_input = ui.textarea("Domain Knowledge").classes("w-full")

            def create_company():
                if not id_input.value or not name_input.value:
                    ui.notify("ID and Name are required", type="warning")
                    return
                try:
                    from .company import CompanyConfig
                    config = CompanyConfig(
                        id=id_input.value.strip(),
                        name=name_input.value.strip(),
                        domain=domain_input.value.strip(),
                    )
                    registry.save(config)
                    ui.notify(f"Company '{config.id}' created", type="positive")
                    id_input.value = ""
                    name_input.value = ""
                    domain_input.value = ""
                    refresh_table()
                except Exception as e:
                    ui.notify(f"Error: {e}", type="negative")

            ui.button("Create", on_click=create_company)

        table_container = ui.column().classes("w-full")
        refresh_table()

    #  Finance 

    @ui.page("/admin/finance")
    @_require_auth
    def finance_page():
        _header("Finance")

        if not hasattr(orchestrator, "cost_gate"):
            ui.label("CostGate not initialized.").classes("text-gray-500")
            return

        cg = orchestrator.cost_gate

        # Summary
        summary = cg.get_spending_summary()
        with ui.row().classes("w-full gap-4"):
            with ui.card().classes("flex-1"):
                ui.label("Total Spend").classes("text-lg font-bold")
                ui.label(f"${summary.get('total', 0):.4f}")
            with ui.card().classes("flex-1"):
                ui.label("Today").classes("text-lg font-bold")
                ui.label(f"${summary.get('today', 0):.4f}")
            with ui.card().classes("flex-1"):
                ui.label("Window Spend").classes("text-lg font-bold")
                ui.label(f"${summary.get('window_spend', 0):.4f}")
            with ui.card().classes("flex-1"):
                ui.label("Mode").classes("text-lg font-bold")
                ui.label(summary.get("mode", "unknown"))

        # Circuit breaker status
        cb = summary.get("circuit_breaker")
        if cb:
            ui.separator().classes("my-4")
            with ui.card().classes("w-full"):
                status_text = "TRIPPED" if cb.get("tripped") else "OK"
                color = "red" if cb.get("tripped") else "green"
                ui.label(f"Circuit Breaker: {status_text}").classes(f"text-lg font-bold text-{color}")
                ui.label(
                    f"Window: {cb.get('window_minutes')}min, "
                    f"Spend: ${cb.get('window_spend', 0):.4f}, "
                    f"Events: {cb.get('events_in_window', 0)}"
                )

        # Per-team report
        ui.separator().classes("my-4")
        report = cg.get_daily_report(hours=24)
        by_team = report.get("by_team", {})
        if by_team:
            ui.label("24h Spending by Team").classes("text-lg font-bold")
            rows = [
                {"team": tid, "spend": f"${amt:.4f}"}
                for tid, amt in sorted(by_team.items(), key=lambda x: -x[1])
            ]
            columns = [
                {"name": "team", "label": "Team", "field": "team"},
                {"name": "spend", "label": "Spend (24h)", "field": "spend"},
            ]
            ui.table(columns=columns, rows=rows).classes("w-full")

        # Per-company spending
        if hasattr(orchestrator, "company_registry"):
            company_ids = orchestrator.company_registry.list_companies()
            if company_ids:
                ui.separator().classes("my-4")
                ui.label("Company Spending (1h)").classes("text-lg font-bold")
                rows = []
                for cid in company_ids:
                    spent = cg._get_company_window_spending(cid, hours=1.0)
                    if spent > 0:
                        rows.append({"company": cid, "spend": f"${spent:.4f}"})
                if rows:
                    columns = [
                        {"name": "company", "label": "Company", "field": "company"},
                        {"name": "spend", "label": "Spend (1h)", "field": "spend"},
                    ]
                    ui.table(columns=columns, rows=rows).classes("w-full")
                else:
                    ui.label("No company spending in last hour.").classes("text-gray-500")

    #  Teams 

    @ui.page("/admin/teams")
    @_require_auth
    def teams_page():
        _header("Teams")

        if not hasattr(orchestrator, "team_registry"):
            ui.label("Team registry not initialized.").classes("text-gray-500")
            return

        tr = orchestrator.team_registry
        fleet = tr.get_fleet_status()

        rows = []
        for tid, info in fleet.get("configs", {}).items():
            rows.append({
                "id": tid,
                "role": info.get("role", ""),
                "always_on": "Yes" if info.get("always_on") else "No",
                "pis": str(info.get("pi_count", 0)),
                "lead": info.get("lead_pi", ""),
                "active": "Yes" if tid in fleet.get("active", []) else "No",
            })

        if rows:
            columns = [
                {"name": "id", "label": "Team ID", "field": "id"},
                {"name": "role", "label": "Role", "field": "role"},
                {"name": "always_on", "label": "Always On", "field": "always_on"},
                {"name": "pis", "label": "Pis", "field": "pis"},
                {"name": "lead", "label": "Lead Pi", "field": "lead"},
                {"name": "active", "label": "Active", "field": "active"},
            ]
            ui.table(columns=columns, rows=rows).classes("w-full")
        else:
            ui.label("No teams registered.").classes("text-gray-500")

    #  Bindings 

    @ui.page("/admin/bindings")
    @_require_auth
    def bindings_page():
        _header("Global Bindings")

        if not hasattr(orchestrator, "company_registry"):
            ui.label("Company registry not initialized.").classes("text-gray-500")
            return

        registry = orchestrator.company_registry

        def refresh_bindings():
            bindings_container.clear()
            with bindings_container:
                bindings = registry.get_global_bindings()
                if bindings:
                    rows = []
                    for b in bindings:
                        rows.append({
                            "team_id": b.team_id,
                            "channel": b.channel or "*",
                            "chat_id": b.chat_id or "*",
                            "user_id": b.user_id or "*",
                            "mode": b.mode,
                            "priority": str(b.priority),
                        })
                    columns = [
                        {"name": "team_id", "label": "Team", "field": "team_id"},
                        {"name": "channel", "label": "Channel", "field": "channel"},
                        {"name": "chat_id", "label": "Chat ID", "field": "chat_id"},
                        {"name": "user_id", "label": "User ID", "field": "user_id"},
                        {"name": "mode", "label": "Mode", "field": "mode"},
                        {"name": "priority", "label": "Priority", "field": "priority"},
                    ]
                    ui.table(columns=columns, rows=rows).classes("w-full")
                else:
                    ui.label("No global bindings configured.").classes("text-gray-500")

        # Add binding form
        with ui.expansion("Add Binding", icon="add").classes("w-full mb-4"):
            team_input = ui.input("Team ID").classes("w-full")
            channel_input = ui.input("Channel (optional)").classes("w-full")
            chat_input = ui.input("Chat ID (optional)").classes("w-full")
            mode_select = ui.select(
                ["cascade", "default", "loop", "council"],
                value="cascade", label="Mode",
            ).classes("w-full")

            def add_binding():
                if not team_input.value:
                    ui.notify("Team ID is required", type="warning")
                    return
                from .company import GlobalBinding
                new_binding = GlobalBinding(
                    team_id=team_input.value.strip(),
                    channel=channel_input.value.strip() or None,
                    chat_id=chat_input.value.strip() or None,
                    mode=mode_select.value,
                )
                current = registry.get_global_bindings()
                current.append(new_binding)
                registry.save_global_bindings(current)
                ui.notify("Binding added", type="positive")
                team_input.value = ""
                channel_input.value = ""
                chat_input.value = ""
                refresh_bindings()

            ui.button("Add", on_click=add_binding)

        bindings_container = ui.column().classes("w-full")
        refresh_bindings()

    #  Scheduler 

    @ui.page("/admin/scheduler")
    @_require_auth
    def scheduler_page():
        _header("Scheduler")

        if not hasattr(orchestrator, "scheduler"):
            ui.label("Scheduler not initialized.").classes("text-gray-500")
            return

        status = orchestrator.scheduler.get_status()

        with ui.card().classes("w-full mb-4"):
            ui.label(f"Scheduler: {'Running' if status.get('started') else 'Stopped'}").classes(
                "text-lg font-bold"
            )

        tasks = status.get("tasks", {})
        if tasks:
            rows = []
            for name, info in tasks.items():
                rows.append({
                    "name": name,
                    "enabled": "Yes" if info.get("enabled") else "No",
                    "interval": f"{info.get('interval_seconds', 0)}s",
                    "runs": str(info.get("run_count", 0)),
                    "errors": str(info.get("error_count", 0)),
                    "last_run": info.get("last_run") or "never",
                    "running": "Yes" if info.get("running") else "No",
                })
            columns = [
                {"name": "name", "label": "Task", "field": "name"},
                {"name": "enabled", "label": "Enabled", "field": "enabled"},
                {"name": "interval", "label": "Interval", "field": "interval"},
                {"name": "runs", "label": "Runs", "field": "runs"},
                {"name": "errors", "label": "Errors", "field": "errors"},
                {"name": "last_run", "label": "Last Run", "field": "last_run"},
                {"name": "running", "label": "Running", "field": "running"},
            ]
            ui.table(columns=columns, rows=rows).classes("w-full")

            # Manual trigger buttons
            ui.separator().classes("my-4")
            ui.label("Manual Trigger").classes("text-lg font-bold")
            with ui.row().classes("gap-2 flex-wrap"):
                for name in tasks:
                    async def trigger(n=name):
                        ok = await orchestrator.scheduler.run_now(n)
                        if ok:
                            ui.notify(f"Triggered: {n}", type="positive")
                        else:
                            ui.notify(f"Not found: {n}", type="negative")
                    ui.button(name, on_click=trigger).props("dense outline")
        else:
            ui.label("No scheduled tasks.").classes("text-gray-500")

    #  Logout 

    @ui.page("/admin/logout")
    def logout_page():
        app.storage.user["authenticated"] = False
        ui.navigate.to("/admin/login")

    #  Helper: navigation header 

    def _header(title: str):
        with ui.header().classes("bg-blue-800 text-white"):
            with ui.row().classes("w-full items-center"):
                ui.label("Companest Admin").classes("text-xl font-bold mr-8")
                ui.link("Dashboard", "/admin/").classes("text-white")
                ui.link("Companies", "/admin/companies").classes("text-white")
                ui.link("Finance", "/admin/finance").classes("text-white")
                ui.link("Teams", "/admin/teams").classes("text-white")
                ui.link("Bindings", "/admin/bindings").classes("text-white")
                ui.link("Scheduler", "/admin/scheduler").classes("text-white")
                ui.space()
                ui.link("Logout", "/admin/logout").classes("text-white")
        ui.label(title).classes("text-2xl font-bold my-4")

    #  Mount on FastAPI 

    ui.run_with(
        fastapi_app,
        mount_path="/admin",
        storage_secret=auth_token,
        title="Companest Admin",
    )
