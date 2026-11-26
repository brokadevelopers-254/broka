"""Platform intelligence: service health, scheduled work and integrations.

Application-level instrumentation cannot prove end-to-end uptime. This tool
reports what BROKA measures and says plainly what it does not.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import operations
from api.core.config import settings
from api.core.permissions import Permission
from api.domains.admin_zeno.schemas import ToolInput, ToolOutput
from api.domains.admin_zeno.tool_registry import ToolDefinition
from api.domains.admin_zeno.tools._common import metric_evidence, simple

SOURCE = "api.core.operations"


class HealthArgs(ToolInput):
    window_hours: int = Field(default=6, ge=1, le=72)


async def service_health(db: AsyncSession, args: HealthArgs) -> ToolOutput:
    report = await operations.operations_report(db, window_hours=args.window_hours)
    start_raw = report["window"]["from"]
    end_raw = report["window"]["to"]
    start = datetime.fromisoformat(str(start_raw).replace("Z", ""))
    end = datetime.fromisoformat(str(end_raw).replace("Z", ""))
    evidence = []
    for section in report.get("sections", []):
        if section.get("key") not in ("api", "database", "background"):
            continue
        for metric in section.get("metrics", []):
            evidence.append(metric_evidence(
                prefix=f"platform.{section.get('key')}",
                metric=metric, source=SOURCE, start=start, end=end,
            ))
    coverage = report.get("coverage") or {}
    evidence.append(simple(
        prefix="platform", key="instances_reporting",
        label="API instances reporting samples", value=coverage.get("instances_reporting"),
        source=SOURCE, definition="Instances that wrote an operations sample recently.",
        start=start, end=end,
    ))
    return ToolOutput(
        evidence=evidence,
        data={
            "coverage": coverage,
            "live_pool": next(
                (s.get("live") for s in report.get("sections", []) if s.get("key") == "database"),
                {},
            ),
            "process_requests": (report.get("live") or {}).get("requests"),
        },
        limitations=[
            "Complete infrastructure uptime is not measurable from application-level samples alone.",
            "Database pool figures describe this process only; other replicas have their own.",
        ],
    )


async def integrations(db: AsyncSession, args: HealthArgs) -> ToolOutput:
    """Which integrations are configured and which operational coverage exists.

    Booleans only. No key, token, DSN or endpoint is returned.
    """
    flags = {
        "redis": settings.redis_enabled,
        "sentry": bool(getattr(settings, "sentry_dsn", "")),
        "gemini": bool(settings.gemini_api_key),
        "deepseek": bool(settings.deepseek_api_key),
        "openrouter": bool(settings.openrouter_api_key),
        "groq": bool(settings.groq_api_key),
        "mpesa": bool(settings.mpesa_consumer_key),
        "zetupay": bool(getattr(settings, "zetupay_secret_key", "")),
        "econfirm": bool(getattr(settings, "econfirm_api_key", "")),
        "email": bool(getattr(settings, "email_enabled", False)),
    }
    evidence = [
        simple(
            prefix="platform.integration", key=name, label=f"{name} configured",
            value=bool(value), unit="boolean", source="api.core.config.settings",
            definition="Whether the integration is configured. Configuration is not a health check.",
            confidence="derived",
        )
        for name, value in flags.items()
    ]
    return ToolOutput(
        evidence=evidence,
        data={"configured": flags},
        limitations=[
            "A configured provider is not necessarily healthy; use operations_overview for outcomes.",
        ],
    )


def register(registry) -> None:
    common = dict(
        domain="platform",
        permission=Permission.ADMIN_SUMMARY,
        evidence_source=SOURCE,
        data_availability="Available from operations sampling and configuration presence.",
        timeout_seconds=12.0,
    )
    registry.register_all([
        ToolDefinition(
            name="platform_health",
            description="API, database pool and background-work health over a short window.",
            input_model=HealthArgs, handler=service_health, **common,
        ),
        ToolDefinition(
            name="platform_integrations",
            description="Which integrations and monitoring sources are configured (presence only, no secrets).",
            input_model=HealthArgs, handler=integrations, **common,
        ),
    ])
