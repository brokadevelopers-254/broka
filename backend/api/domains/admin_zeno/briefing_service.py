"""The initial intelligence briefing.

Deterministic by design: it aggregates signals BROKA already trusts (Command
Center decisions, founder insights, open alerts, operational and marketplace
anomalies) and never calls a model. A model outage therefore cannot make the
briefing unavailable, and a page load cannot trigger an expensive,
uncontrolled investigation.

A short per-process TTL keeps a dashboard refresh from running every
analytics service on every request. The response carries ``cached`` and
``generated_at`` so an administrator can see how fresh it is; the cache is
process-local, so a deploy or restart simply regenerates it.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from api.core import admin_alerts, command_center, market_models, ops_anomalies, operations
from api.domains.admin_zeno.constants import BRIEFING_CACHE_SECONDS
from api.domains.admin_zeno.schemas import (
    BriefingFinding,
    BriefingOpportunity,
    BriefingResponse,
    Period,
)
from api.domains.admin_zeno.tools._common import missing, simple

log = logging.getLogger(__name__)

_cache: dict[int, tuple[float, BriefingResponse]] = {}

MAX_FINDINGS = 12
MAX_OPPORTUNITIES = 5


def _dedupe_findings(rows: list[BriefingFinding]) -> list[BriefingFinding]:
    seen: set[tuple[str, str]] = set()
    out: list[BriefingFinding] = []
    for row in rows:
        key = (row.id, row.title)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _action_href(action: Any) -> str | None:
    if isinstance(action, (list, tuple)) and len(action) > 1 and isinstance(action[1], str):
        return action[1] if action[1].startswith("/") else None
    return None


async def _alerts(db: AsyncSession) -> list[dict]:
    rows, _total, _open = await admin_alerts.list_alerts(
        db, status="open", limit=50
    )
    high = [
        admin_alerts.alert_to_dict(row) for row in rows
        if row.severity in ("critical", "high")
    ]
    # The alert service already dedupes by kind + meta reference; this keeps a
    # repeated title from appearing twice in one briefing.
    seen: set[tuple] = set()
    out = []
    for alert in high:
        key = (
            alert.get("kind"),
            alert.get("title"),
            json.dumps(alert.get("meta") or {}, sort_keys=True),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(alert)
    return out


async def build_briefing(
    db: AsyncSession, *, window_days: int = 7, use_cache: bool = True
) -> BriefingResponse:
    now = datetime.utcnow()
    if use_cache:
        cached = _cache.get(window_days)
        if cached and (time.monotonic() - cached[0]) < BRIEFING_CACHE_SECONDS:
            return cached[1].model_copy(update={"cached": True})

    cc = await command_center.command_center(db, window_days=window_days, now=now)
    decisions = (cc.get("decisions") or {}).get("insights") or []
    pulse = cc.get("pulse") or {}

    alerts = await _alerts(db)
    ops = await ops_anomalies.detect(db, now=now)
    market = await market_models.detect(db, now=now)
    ops_coverage = await operations.first_sample_at(db)

    findings: list[BriefingFinding] = []
    for decision in decisions[:MAX_FINDINGS]:
        severity = decision.get("severity")
        if severity not in ("critical", "warning", "positive", "info"):
            continue
        findings.append(BriefingFinding(
            id=str(decision.get("id") or "decision")[:120],
            severity="high" if severity == "critical" else severity,
            title=str(decision.get("title") or "Signal")[:300],
            detail=str(decision.get("detail") or "")[:1200],
            area=str(decision.get("area") or "business")[:60],
            evidence=[],
            href=_action_href(decision.get("action")),
        ))

    for alert in alerts[:8]:
        findings.append(BriefingFinding(
            id=f"alert:{alert.get('id')}"[:120],
            severity="critical" if alert.get("severity") == "critical" else "high",
            title=str(alert.get("title") or "Open alert")[:300],
            detail=str(alert.get("body") or "")[:1200],
            area="operations",
            evidence=[],
            href=alert.get("href") if str(alert.get("href") or "").startswith("/") else None,
        ))

    for anomaly in (ops.get("anomalies") or [])[:6]:
        severity = "critical" if anomaly.get("severity") == "high" else "warning"
        findings.append(BriefingFinding(
            id=f"ops:{anomaly.get('key') or anomaly.get('metric')}"[:120],
            severity=severity,  # type: ignore[arg-type]
            title=str(anomaly.get("label") or anomaly.get("key") or "Operational anomaly")[:300],
            detail=str(anomaly.get("impact") or "")[:1200],
            area="operations",
            evidence=[f"ops.anomaly.{anomaly.get('key') or anomaly.get('metric')}"][:1],
            href="/operations",
        ))

    for anomaly in (market.get("anomalies") or [])[:6]:
        findings.append(BriefingFinding(
            id=f"market:{anomaly.get('key')}"[:120],
            severity="high" if anomaly.get("severity") == "high" else "warning",
            title=str(anomaly.get("label") or anomaly.get("key") or "Marketplace anomaly")[:300],
            detail=str(anomaly.get("impact") or "")[:1200],
            area="marketplace",
            evidence=[f"market.anomaly.{anomaly.get('key')}"][:1],
            href="/market",
        ))

    order = {"critical": 0, "high": 1, "warning": 2, "positive": 3, "info": 4}
    findings = sorted(_dedupe_findings(findings), key=lambda f: order.get(f.severity, 5))[:MAX_FINDINGS]

    opportunities: list[BriefingOpportunity] = []
    for decision in decisions:
        if decision.get("severity") == "positive" and len(opportunities) < MAX_OPPORTUNITIES:
            opportunities.append(BriefingOpportunity(
                title=str(decision.get("title") or "Opportunity")[:300],
                detail=str(decision.get("detail") or "")[:1200],
                href=_action_href(decision.get("action")),
            ))
    for gap in (pulse.get("targets") or []):
        if not gap.get("met") and len(opportunities) < MAX_OPPORTUNITIES:
            opportunities.append(BriefingOpportunity(
                title=f"Target not met: {gap.get('label')}"[:300],
                detail=f"Actual {gap.get('actual')} against a target of {gap.get('target')} "
                       f"({gap.get('progress_pct')}% progress)."[:1200],
                href="/growth",
            ))

    evidence = []
    for group in cc.get("groups", []):
        for metric in group.get("metrics", []):
            if not metric.get("available"):
                # Unavailable, not zero: value is None and the reason travels.
                evidence.append(missing(
                    prefix=f"briefing.{group.get('key')}", key=str(metric.get("key")),
                    label=str(metric.get("label")),
                    source="api.core.command_center",
                    reason=str(metric.get("unavailable_reason") or "Not measured."),
                    definition=str(metric.get("definition") or ""),
                ))
    for anomaly in (ops.get("anomalies") or [])[:6]:
        evidence.append(simple(
            prefix="briefing.ops_anomaly", key=str(anomaly.get("key") or anomaly.get("metric")),
            label=str(anomaly.get("label") or "Operational anomaly"),
            value=(anomaly.get("deviation") or {}).get("z_score"), unit="z_score",
            source="api.core.ops_anomalies.detect",
            definition="Robust z-score against the same weekday baseline.",
            confidence="measured", href="/operations",
        ))
    for anomaly in (market.get("anomalies") or [])[:6]:
        evidence.append(simple(
            prefix="briefing.market_anomaly", key=str(anomaly.get("key")),
            label=str(anomaly.get("label") or "Marketplace anomaly"),
            value=anomaly.get("z_score") or (anomaly.get("deviation") or {}).get("z_score"),
            unit="z_score", source="api.core.market_models.detect",
            definition="Robust z-score against the same weekday baseline.",
            confidence="measured", href="/market",
        ))

    critical = sum(1 for f in findings if f.severity == "critical")
    high = sum(1 for f in findings if f.severity == "high")
    headline = (
        f"{critical} critical and {high} high-priority signals in the last "
        f"{window_days} day{'s' if window_days != 1 else ''}"
        if findings else
        f"No critical signals in the last {window_days} day{'s' if window_days != 1 else ''}"
    )

    summary_parts = [
        f"{len(alerts)} open critical or high alert(s).",
        f"{len(ops.get('anomalies') or [])} operational anomalie(s) on the last complete day.",
        f"{len(market.get('anomalies') or [])} marketplace anomalie(s) on the last complete day.",
    ]
    top = [f.title for f in findings[:3]]
    if top:
        summary_parts.append("Most urgent: " + "; ".join(top) + ".")
    coverage = cc.get("activity_coverage_started_on")
    if coverage:
        summary_parts.append(f"Activity tracking began on {coverage}.")

    limitations = []
    for metric_evidence in evidence:
        if not metric_evidence.available and metric_evidence.unavailable_reason:
            note = f"{metric_evidence.label}: {metric_evidence.unavailable_reason}"
            if note not in limitations:
                limitations.append(note)
    if ops_coverage is None:
        limitations.append("No operations sample has been recorded yet.")
    limitations = limitations[:8]

    response = BriefingResponse(
        headline=headline[:300],
        executive_summary=" ".join(summary_parts)[:6000],
        priority_findings=findings,
        opportunities=opportunities,
        recommended_investigations=[
            f.title for f in findings[:5]
        ],
        evidence=evidence[:20],
        period=Period(start=now.replace(hour=0, minute=0, second=0, microsecond=0)),
        generated_at=now,
        data_freshness={
            "generated_at": now.isoformat() + "Z",
            "activity_coverage_started_on": coverage,
            "operations_sampling_started_at": (
                ops_coverage.isoformat() + "Z" if ops_coverage else None
            ),
            "evaluated_day": ops.get("evaluated_day"),
            "cached_for_seconds": BRIEFING_CACHE_SECONDS,
        },
        limitations=limitations,
        cached=False,
    )
    _cache[window_days] = (time.monotonic(), response)
    return response


def clear_cache() -> None:
    _cache.clear()
