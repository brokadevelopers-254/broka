"""Shared helpers for the intelligence tools - no analytics live here."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from api.domains.admin_zeno.schemas import Evidence, Period, ToolInput
from api.domains.admin_zeno.evidence import make_evidence, unavailable_evidence


class WindowArgs(ToolInput):
    window_days: int = 7


def bounds(window_days: int, now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    end = now or datetime.utcnow()
    return end - timedelta(days=window_days), end


def metric_evidence(
    *,
    prefix: str,
    metric: dict[str, Any],
    source: str,
    start: datetime,
    end: datetime,
) -> Evidence:
    """Turn one metric dict from command_center/operations into evidence.

    Those services already carry the definition, availability and previous
    value; this only translates the shape. ``available=False`` keeps its
    reason instead of being flattened to zero.
    """
    available = bool(metric.get("available", True))
    return make_evidence(
        metric_key=f"{prefix}.{metric.get('key')}",
        label=str(metric.get("label") or metric.get("key")),
        value=metric.get("value"),
        previous_value=metric.get("previous"),
        unit=str(metric.get("unit") or "count"),
        source=source,
        definition=str(metric.get("definition") or ""),
        period_start=start,
        period_end=end,
        available=available,
        unavailable_reason=metric.get("unavailable_reason") if not available else None,
        confidence="measured" if metric.get("kind") != "live" else "measured",
        href=metric.get("href"),
    )


def simple(
    *,
    prefix: str,
    key: str,
    label: str,
    value: Any,
    source: str,
    definition: str,
    unit: str = "count",
    previous: Any = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    confidence: str = "measured",
    href: Optional[str] = None,
) -> Evidence:
    return make_evidence(
        metric_key=f"{prefix}.{key}",
        label=label,
        value=value,
        previous_value=previous,
        unit=unit,
        source=source,
        definition=definition,
        period_start=start,
        period_end=end,
        confidence=confidence,
        href=href,
    )


def missing(
    *, prefix: str, key: str, label: str, source: str, reason: str, definition: str = ""
) -> Evidence:
    return unavailable_evidence(
        metric_key=f"{prefix}.{key}", label=label, source=source, reason=reason,
        definition=definition,
    )


def coverage_period(start: datetime, end: datetime) -> Period:
    return Period(start=start, end=end)
