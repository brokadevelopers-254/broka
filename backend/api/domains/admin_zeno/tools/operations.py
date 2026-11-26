"""Operations intelligence tools.

Reuses ``api.core.operations`` and ``api.core.ops_anomalies``. Their documented
limits travel with the figures: application-level measurements cannot prove
external uptime, and FCM accepting a push is not proof a phone received it.
"""
from __future__ import annotations

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import operations, ops_anomalies
from api.core.permissions import Permission
from api.domains.admin_zeno.schemas import ToolInput, ToolOutput
from api.domains.admin_zeno.tool_registry import ToolDefinition
from api.domains.admin_zeno.tools._common import metric_evidence, simple

SOURCE = "api.core.operations"


class OperationsArgs(ToolInput):
    window_hours: int = Field(default=24, ge=1, le=168, description="Look-back window in hours.")


async def operations_overview(db: AsyncSession, args: OperationsArgs) -> ToolOutput:
    report = await operations.operations_report(db, window_hours=args.window_hours)
    start = report["window"]["from"], report["window"]["to"]
    from datetime import datetime
    start_dt, end_dt = datetime.fromisoformat(start[0].replace("Z", "")), datetime.fromisoformat(start[1].replace("Z", ""))
    evidence = []
    for section in report.get("sections", []):
        for metric in section.get("metrics", []):
            evidence.append(metric_evidence(
                prefix=f"ops.{section.get('key')}",
                metric=metric, source=SOURCE, start=start_dt, end=end_dt,
            ))
    coverage = report.get("coverage") or {}
    if not coverage.get("samples"):
        evidence.append(simple(
            prefix="ops", key="coverage", label="Operational samples in window",
            value=0, unit="count", source=SOURCE,
            definition="Samples recorded by the operations sampler.",
            confidence="measured",
        ))
    limitations = [
        "Availability is not measured from outside: the API cannot record the time it was down.",
        "A notification accepted by FCM is not proof the recipient's device received it.",
        "Request metrics are process-local; each replica counts only its own traffic.",
    ]
    return ToolOutput(
        evidence=evidence[:32],
        data={
            "window": report.get("window"),
            "coverage": coverage,
            "not_instrumented": report.get("not_instrumented", []),
            "live": report.get("live", {}),
        },
        limitations=limitations,
    )


class AnomalyArgs(ToolInput):
    pass


async def operational_anomalies(db: AsyncSession, args: AnomalyArgs) -> ToolOutput:
    report = await ops_anomalies.detect(db)
    evidence = []
    for a in report.get("anomalies", [])[:10]:
        evidence.append(simple(
            prefix="ops.anomaly", key=str(a.get("key") or a.get("metric"))[:60],
            label=f"Operational anomaly: {a.get('label') or a.get('metric')}",
            value=(a.get("deviation") or {}).get("z_score"), unit="z_score",
            source="api.core.ops_anomalies.detect",
            definition="Robust z-score of the last complete day against the same weekday baseline.",
            confidence="measured", href="/operations",
        ))
    return ToolOutput(
        evidence=evidence,
        data={
            "evaluated_day": report.get("evaluated_day"),
            "anomalies": [
                {"key": a.get("key") or a.get("metric"), "label": a.get("label"),
                 "severity": a.get("severity"), "impact": a.get("impact"),
                 "suggested_action": a.get("suggested_action")}
                for a in report.get("anomalies", [])[:10]
            ],
            "not_covered": report.get("not_covered", []),
            "covered_elsewhere": report.get("covered_elsewhere", []),
        },
        limitations=["Anomaly detection never changes an account, payment or deployment."],
    )


def register(registry) -> None:
    common = dict(
        domain="operations",
        permission=Permission.ADMIN_SUMMARY,
        evidence_source=SOURCE,
        data_availability="Available from the operations sampler and request instrumentation.",
        timeout_seconds=12.0,
    )
    registry.register_all([
        ToolDefinition(
            name="operations_overview",
            description="API error rate and latency, database health, payments, notifications, calls and background work.",
            input_model=OperationsArgs, handler=operations_overview, **common,
        ),
        ToolDefinition(
            name="operational_anomalies",
            description="Operational metrics outside their usual range for the last complete day.",
            input_model=AnomalyArgs, handler=operational_anomalies, **common,
        ),
    ])
