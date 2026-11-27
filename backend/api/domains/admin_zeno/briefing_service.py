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
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from api.core import admin_alerts, command_center, market_models, ops_anomalies, operations
from api.domains.admin_zeno.constants import BRIEFING_CACHE_SECONDS
from api.domains.admin_zeno.evidence import index_by_key
from api.domains.admin_zeno.schemas import (
    BriefingFinding,
    BriefingOpportunity,
    BriefingResponse,
    Evidence,
    Period,
)
from api.domains.admin_zeno.tools._common import metric_evidence, simple

log = logging.getLogger(__name__)

_cache: dict[int, tuple[float, BriefingResponse]] = {}

MAX_FINDINGS = 12
MAX_OPPORTUNITIES = 5
# One bounded set that both the references and the response use, so a
# truncated list can never leave a finding pointing at a dropped metric.
MAX_BRIEFING_EVIDENCE = 60


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
    if isinstance(action, dict):
        href = action.get("href")
        return href if isinstance(href, str) and href.startswith("/") else None
    if isinstance(action, (list, tuple)) and len(action) > 1 and isinstance(action[1], str):
        return action[1] if action[1].startswith("/") else None
    return None


def _day_bounds(value: Any) -> tuple[Optional[datetime], Optional[datetime]]:
    """The UTC day an anomaly detector actually evaluated."""
    try:
        day = datetime.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None, None
    return day, day + timedelta(days=1)


def _unit_for(field: str) -> str:
    if field.endswith("_pct") or "pct" in field:
        return "percentage"
    if field.endswith("_minor"):
        return "kes_minor"
    if field.endswith("_hours"):
        return "hours"
    if field.endswith("_ms"):
        return "ms"
    return "count"


def _humanise(field: str) -> str:
    text = field.replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else "Figure"


def _decision_evidence(
    decision: dict, *, index: int, start: datetime, end: datetime
) -> list[Evidence]:
    """Turn a Command Center decision's own evidence into briefing evidence.

    The insight already carries the figures it was drawn from; inventing a
    reference, or leaving the finding with none, would both be wrong. Metric
    keys are positional so a decision id (which can embed a user id) never
    becomes part of an identifier.
    """
    data = decision.get("evidence") or {}
    if not isinstance(data, dict):
        return []
    title = str(decision.get("title") or "Decision")[:80]
    out: list[Evidence] = []
    for field, value in data.items():
        if value is None or isinstance(value, (dict, list, str)):
            continue
        if not isinstance(value, (int, float, bool)):
            continue
        out.append(simple(
            prefix=f"briefing.decision.{index}",
            key=str(field)[:40],
            label=f"{_humanise(str(field))}: {title}"[:160],
            value=value,
            unit="boolean" if isinstance(value, bool) else _unit_for(str(field)),
            source="api.core.command_center",
            definition=f"Figure carried by the Command Center decision '{title}'.",
            start=start, end=end, confidence="derived", href=_action_href(decision.get("action")),
        ))
    return out


def _unique_evidence(items: list[Evidence]) -> list[Evidence]:
    seen: set[str] = set()
    out: list[Evidence] = []
    for item in items:
        if item.metric_key in seen:
            continue
        seen.add(item.metric_key)
        out.append(item)
    return out


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
    # The briefing reports the interval that was actually requested: seven
    # days means now-7d .. now, not "since midnight".
    start = now - timedelta(days=window_days)
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
    evidence: list[Evidence] = []

    # Command Center metrics, available and unavailable alike, so a finding
    # can reference a metric that was actually returned.
    for group in cc.get("groups", []):
        for metric in group.get("metrics", []):
            evidence.append(metric_evidence(
                prefix=f"briefing.{group.get('key')}", metric=metric,
                source="api.core.command_center", start=start, end=now,
            ))

    for index, decision in enumerate(decisions[:MAX_FINDINGS]):
        severity = decision.get("severity")
        if severity not in ("critical", "warning", "positive", "info"):
            continue
        refs = _decision_evidence(decision, index=index, start=start, end=now)
        evidence.extend(refs)
        findings.append(BriefingFinding(
            # Positional: a decision id can embed a customer's user id.
            id=f"decision:{index}",
            severity="high" if severity == "critical" else severity,
            title=str(decision.get("title") or "Signal")[:300],
            detail=str(decision.get("detail") or "")[:1200],
            area=str(decision.get("area") or "business")[:60],
            evidence=[e.metric_key for e in refs],
            evidence_backed=bool(refs),
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
            # An alert about a failure is a real signal, but it carries no
            # aggregate figure of its own; say so rather than invent one.
            evidence_backed=False,
            href=alert.get("href") if str(alert.get("href") or "").startswith("/") else None,
        ))

    ops_day_start, ops_day_end = _day_bounds(ops.get("evaluated_day"))
    for anomaly in (ops.get("anomalies") or [])[:6]:
        severity = "critical" if anomaly.get("severity") == "high" else "warning"
        key = str(anomaly.get("key") or anomaly.get("metric") or "anomaly")[:60]
        findings.append(BriefingFinding(
            id=f"ops:{key}"[:120],
            severity=severity,  # type: ignore[arg-type]
            title=str(anomaly.get("label") or key or "Operational anomaly")[:300],
            detail=str(anomaly.get("impact") or "")[:1200],
            area="operations",
            # Must match the key of the evidence appended below.
            evidence=[f"briefing.ops_anomaly.{key}"],
            href="/operations",
        ))
        evidence.append(simple(
            prefix="briefing.ops_anomaly", key=key,
            label=str(anomaly.get("label") or "Operational anomaly"),
            value=(anomaly.get("deviation") or {}).get("z_score"), unit="z_score",
            source="api.core.ops_anomalies.detect",
            definition=(
                "Robust z-score for the last complete UTC day against the same weekday "
                "baseline. This detector evaluates that one day, not the whole briefing window."
            ),
            start=ops_day_start, end=ops_day_end,
            confidence="measured", href="/operations",
        ))

    market_day_start, market_day_end = _day_bounds(market.get("evaluated_day"))
    for anomaly in (market.get("anomalies") or [])[:6]:
        key = str(anomaly.get("key") or "anomaly")[:60]
        findings.append(BriefingFinding(
            id=f"market:{key}"[:120],
            severity="high" if anomaly.get("severity") == "high" else "warning",
            title=str(anomaly.get("label") or key or "Marketplace anomaly")[:300],
            detail=str(anomaly.get("impact") or "")[:1200],
            area="marketplace",
            evidence=[f"briefing.market_anomaly.{key}"],
            href="/market",
        ))
        evidence.append(simple(
            prefix="briefing.market_anomaly", key=key,
            label=str(anomaly.get("label") or "Marketplace anomaly"),
            value=(anomaly.get("deviation") or {}).get("z_score"), unit="z_score",
            source="api.core.market_models.detect",
            definition=(
                "Robust z-score for the last complete UTC day against the same weekday "
                "baseline. This detector evaluates that one day, not the whole briefing window."
            ),
            start=market_day_start, end=market_day_end,
            confidence="measured", href="/market",
        ))

    evidence = _unique_evidence(evidence)[:MAX_BRIEFING_EVIDENCE]
    order = {"critical": 0, "high": 1, "warning": 2, "positive": 3, "info": 4}
    findings = sorted(_dedupe_findings(findings), key=lambda f: order.get(f.severity, 5))[:MAX_FINDINGS]

    # Never return a dangling reference. A finding whose only references were
    # not actually returned becomes an explicitly non-evidence-backed signal.
    by_key = index_by_key(evidence)
    for finding in findings:
        valid = [ref for ref in finding.evidence if ref in by_key]
        if len(valid) != len(finding.evidence):
            finding.evidence = valid
            finding.evidence_backed = bool(valid)
        elif not valid:
            finding.evidence_backed = False

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

    critical = sum(1 for f in findings if f.severity == "critical")
    high = sum(1 for f in findings if f.severity == "high")
    headline = (
        f"{critical} critical and {high} high-priority signals in the last "
        f"{window_days} day{'s' if window_days != 1 else ''}"
        if findings else
        f"No critical signals in the last {window_days} day{'s' if window_days != 1 else ''}"
    )

    summary_parts = [
        (f"Reporting window: {(start.isoformat(timespec='minutes'))}Z to "
         f"{now.isoformat(timespec='minutes')}Z ({window_days} day"
         f"{'s' if window_days != 1 else ''})."),
        f"{len(alerts)} open critical or high alert(s).",
        (f"{len(ops.get('anomalies') or [])} operational anomalie(s) on the last "
         f"complete day ({ops.get('evaluated_day') or 'not evaluated'})."),
        (f"{len(market.get('anomalies') or [])} marketplace anomalie(s) on the last "
         f"complete day ({market.get('evaluated_day') or 'not evaluated'})."),
    ]
    top = [f.title for f in findings[:3]]
    if top:
        summary_parts.append("Most urgent: " + "; ".join(top) + ".")
    coverage = cc.get("activity_coverage_started_on")
    if coverage:
        summary_parts.append(f"Activity tracking began on {coverage}.")

    limitations = []
    if any(not f.evidence_backed for f in findings):
        limitations.append(
            "Some findings are existing decisions or alerts without quantitative "
            "evidence and are marked evidence_backed=false."
        )
    for metric in evidence:
        if not metric.available and metric.unavailable_reason:
            note = f"{metric.label}: {metric.unavailable_reason}"
            if note not in limitations:
                limitations.append(note)
    if ops_coverage is None:
        limitations.append("No operations sample has been recorded yet.")
    limitations.append(
        "Operational and marketplace anomaly detectors evaluate only the last complete "
        "UTC day, not the whole reporting window; the evaluated dates are reported separately."
    )
    limitations = limitations[:10]

    response = BriefingResponse(
        headline=headline[:300],
        executive_summary=" ".join(summary_parts)[:6000],
        priority_findings=findings,
        opportunities=opportunities,
        recommended_investigations=[
            f.title for f in findings[:5]
        ],
        evidence=evidence,
        period=Period(start=start, end=now),
        generated_at=now,
        data_freshness={
            "generated_at": now.isoformat() + "Z",
            "period_start": start.isoformat() + "Z",
            "period_end": now.isoformat() + "Z",
            "activity_coverage_started_on": coverage,
            "operations_sampling_started_at": (
                ops_coverage.isoformat() + "Z" if ops_coverage else None
            ),
            # The detectors judge one completed day, not the window above.
            "ops_evaluated_day": ops.get("evaluated_day"),
            "market_evaluated_day": market.get("evaluated_day"),
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
