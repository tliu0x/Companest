"""
Companest Built-in Company Templates

Provides pre-built company configurations that can be used with
CompanyBuilder.from_template() or Companest.company(..., template="name").

Each template defines:
- domain: company domain description
- goals: list of operating goals
- budget: {"hourly": float, "monthly": float}
- teams: list of team definitions with pis
"""

from typing import Any, Dict, List, Optional

from .exceptions import CompanestError


class TemplateNotFoundError(CompanestError):
    """Raised when a requested template does not exist."""
    pass


#  Built-in Templates 

BUILTIN_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "ecommerce": {
        "name": "Cross-Border Ecommerce Company",
        "domain": "Cross-border ecommerce operations",
        "description": "Cross-border ecommerce company with competitor monitoring and content generation.",
        "goals": [
            "Monitor competitor pricing and trends",
            "Generate ecommerce marketing content",
            "Analyze market trends and provide operational recommendations",
        ],
        "budget": {
            "hourly": 2.0,
            "monthly": 500.0,
        },
        "teams": [
            {
                "team_id": "competitor-watch",
                "role": "Competitor monitoring",
                "pis": [
                    {
                        "id": "price-tracker",
                        "soul": "You are a cross-border ecommerce competitor price tracking expert. Your role is to monitor major competitors' pricing strategies, promotional activities, and inventory changes, and produce structured competitor analysis reports.",
                        "tools": "researcher",
                        "max_turns": 15,
                    },
                    {
                        "id": "market-analyst",
                        "soul": "You are a market trend analyst. Your role is to analyze ecommerce platform market dynamics, consumer behavior trends, and industry reports to provide data-driven support for operational decisions.",
                        "tools": "researcher",
                        "max_turns": 10,
                    },
                ],
                "lead_pi": "price-tracker",
                "mode": "default",
            },
            {
                "team_id": "content",
                "role": "Content generation",
                "pis": [
                    {
                        "id": "copywriter",
                        "soul": "You are a cross-border ecommerce copywriting expert. Your role is to generate multilingual product descriptions, marketing copy, and social media content based on product features and target markets.",
                        "tools": "researcher",
                        "max_turns": 10,
                    },
                ],
                "lead_pi": "copywriter",
                "mode": "default",
            },
        ],
    },
    "research": {
        "name": "Research Company",
        "domain": "Academic and industry research",
        "description": "Research company with literature review and synthesis capabilities.",
        "goals": [
            "Systematically search and organize relevant literature",
            "Generate literature reviews and research summaries",
            "Track latest developments in the research field",
        ],
        "budget": {
            "hourly": 1.5,
            "monthly": 300.0,
        },
        "teams": [
            {
                "team_id": "literature-review",
                "role": "Literature review",
                "pis": [
                    {
                        "id": "searcher",
                        "soul": "You are a literature search expert. Your role is to systematically search academic papers, industry reports, and technical documents by research topic, and rank them by relevance and quality.",
                        "tools": "researcher",
                        "max_turns": 15,
                    },
                    {
                        "id": "synthesizer",
                        "soul": "You are a research synthesis expert. Your role is to read and analyze multiple papers, extract key findings, methodologies, and conclusions, and produce structured literature review reports.",
                        "tools": "researcher",
                        "max_turns": 12,
                    },
                ],
                "lead_pi": "searcher",
                "mode": "default",
            },
        ],
    },
}


def list_templates() -> List[str]:
    """Return list of available template names."""
    return list(BUILTIN_TEMPLATES.keys())


def get_template(name: str) -> Dict[str, Any]:
    """Get a template by name.

    Args:
        name: Template name (e.g. "ecommerce", "research").

    Returns:
        Template dict with keys: name, domain, description, goals, budget, teams.

    Raises:
        TemplateNotFoundError: If template name is not found.
    """
    if name not in BUILTIN_TEMPLATES:
        available = ", ".join(list_templates())
        raise TemplateNotFoundError(
            f"Template '{name}' not found. Available templates: {available}"
        )
    # Return a deep copy to prevent mutation of the built-in data
    import copy
    return copy.deepcopy(BUILTIN_TEMPLATES[name])
