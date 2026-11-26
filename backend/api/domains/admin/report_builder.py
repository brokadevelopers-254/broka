"""The admin report builder: catalog, previews, stored runs, PDFs and schedules.

Mounted at /admin/report-builder. Reads, previews and runs need ADMIN_SUMMARY,
the reports' permission; they write only report rows and audit rows and change
no marketplace or money state. Emailing a schedule's reports to
administrators needs SEND_ADMIN_EMAIL as well, since it sends mail under
BROKA's domain.

Every run is stored (api/models/report_builder.py) with the spec it was built
from; regenerating writes a new run that points at the old one, which stays in
the archive. Builds run in the request with a rate limit per administrator;
scheduled ones run in the 5-minute sweep, outside any request.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import report_builder as rb
from api.core.audit import record_audit
from api.core.client_ip import client_ip_or_none
from api.core.permissions import Permission, get_user_permissions, require_permission
from api.core.rate_limit import report_builder_limiter
from api.database import Listing, User, get_db
from api.models.report_builder import ReportDefinition, ReportRun

router = APIRouter()

MAX_RECIPIENTS = 10


class FiltersIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    category: Optional[str] = Field(None, max_length=100)
    county: Optional[str] = Field(None, max_length=100)


class SpecIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=120)
    categories: list[str] = Field(default_factory=list, max_length=len(rb.CATEGORY_KEYS))
    metrics: list[str] = Field(default_factory=list, max_length=len(rb.METRICS))
    date_from: Optional[str] = Field(None, max_length=10)
    date_to: Optional[str] = Field(None, max_length=10)
    last_days: Optional[int] = Field(None, ge=1, le=rb.MAX_RANGE_DAYS)
    comparison: Literal["previous_period", "previous_year", "none"] = "previous_period"
    filters: FiltersIn = Field(default_factory=FiltersIn)
    group_by: Literal["none", "day", "week", "month", "category", "county"] = "none"
    visualization: Literal["auto", "line", "bar", "table"] = "auto"


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: SpecIn
    definition_id: Optional[str] = Field(None, max_length=36)


class ScheduleFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    schedule: Literal["none", "daily", "weekly", "monthly", "custom"] = "none"
    schedule_every_days: Optional[int] = Field(None, ge=2, le=90)
    schedule_hour_utc: int = Field(4, ge=0, le=23)
    schedule_weekday: Optional[int] = Field(None, ge=0, le=6)
    schedule_day: Optional[int] = Field(None, ge=1, le=28)
    recipients: list[str] = Field(default_factory=list, max_length=MAX_RECIPIENTS)
    enabled: bool = True


class DefinitionIn(ScheduleFields):
    name: str = Field(min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=500)
    spec: SpecIn


class DefinitionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_version: int = Field(ge=1)
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=500)
    spec: Optional[SpecIn] = None
    schedule: Optional[Literal["none", "daily", "weekly", "monthly", "custom"]] = None
    schedule_every_days: Optional[int] = Field(None, ge=2, le=90)
    schedule_hour_utc: Optional[int] = Field(None, ge=0, le=23)
    schedule_weekday: Optional[int] = Field(None, ge=0, le=6)
    schedule_day: Optional[int] = Field(None, ge=1, le=28)
    recipients: Optional[list[str]] = Field(None, max_length=MAX_RECIPIENTS)
    enabled: Optional[bool] = None


def _spec(body: SpecIn, *, require_range: bool = True) -> dict:
    try:
        return rb.normalize_spec(body.model_dump(), require_range=require_range)
    except rb.SpecError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _range(spec: dict, now: datetime) -> tuple[datetime, datetime]:
    try:
        return rb.spec_range(spec, now=now)
    except rb.SpecError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _audit_detail(**fields) -> str:
    return json.dumps(fields, separators=(",", ":"), default=str)


async def _admin_recipients(db: AsyncSession, ids: list[str]) -> list[str]:
    """Recipients must be administrators with an email address: a report is
    business data, and goes to no one outside the admin team."""
    wanted = list(dict.fromkeys(i.strip() for i in ids if i and i.strip()))
    if not wanted:
        return []
    found = {row[0] for row in (await db.execute(
        select(User.id).where(User.id.in_(wanted), User.is_admin == True,  # noqa: E712
                              User.email.is_not(None))
    )).all()}
    missing = [i for i in wanted if i not in found]
    if missing:
        raise HTTPException(status_code=422, detail="every recipient must be an administrator with an email address")
    return wanted


def _can_email(admin: User) -> bool:
    return Permission.SEND_ADMIN_EMAIL in get_user_permissions(admin)


def _require_email_permission(admin: User, recipients: list[str]) -> None:
    if recipients and not _can_email(admin):
        raise HTTPException(status_code=403, detail=f"Missing permission: {Permission.SEND_ADMIN_EMAIL.value}")


def _schedule(fields: dict) -> None:
    try:
        rb.validate_schedule(fields["schedule"], every_days=fields.get("schedule_every_days"),
                             hour=fields.get("schedule_hour_utc", 4), weekday=fields.get("schedule_weekday"),
                             day=fields.get("schedule_day"))
    except rb.SpecError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


async def _definition(db: AsyncSession, definition_id: str) -> ReportDefinition:
    d = (await db.execute(
        select(ReportDefinition).where(ReportDefinition.id == definition_id, ReportDefinition.deleted_at.is_(None))
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if d is None:
        raise HTTPException(status_code=404, detail="Saved report not found")
    return d


async def _run(db: AsyncSession, run_id: str) -> ReportRun:
    run = await db.get(ReportRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return run


@router.get("/catalog")
async def report_catalog(
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    """What a report can contain, and who a schedule can email."""
    from api.core.email import admin_email_sender, settings as email_settings

    catalog = rb.catalog()
    options = {}
    for name, column in (("category", Listing.category), ("county", Listing.location_county)):
        values = (await db.execute(
            select(func.trim(column)).where(column.is_not(None), func.trim(column) != "")
            .group_by(func.trim(column)).order_by(func.trim(column)).limit(200)
        )).all()
        options[name] = [v for (v,) in values if v]
    catalog["filter_options"] = options
    can_email = _can_email(admin)
    recipients = []
    if can_email:
        rows = (await db.execute(
            select(User.id, User.name, User.email).where(User.is_admin == True, User.email.is_not(None))  # noqa: E712
            .order_by(User.name).limit(100)
        )).all()
        for uid, name, email in rows:
            local, _, domain = (email or "").partition("@")
            recipients.append({"id": uid, "name": name, "email_hint": f"{local[:1]}…@{domain}" if domain else None})
    catalog["email"] = {
        "configured": bool(email_settings.resend_api_key and admin_email_sender()),
        "can_send": can_email,
        "recipients": recipients,
        "note": ("Scheduled reports are emailed as a plain-text summary with a link to the full report; "
                 "attachments are not sent."),
    }
    return catalog


@router.post("/preview")
async def report_preview(
    body: SpecIn,
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    """Build a report without storing it."""
    spec = _spec(body)
    now = datetime.utcnow()
    start, end = _range(spec, now)
    await report_builder_limiter.check_and_record(admin.id)
    payload = await rb.build(db, spec, start=start, end=end, now=now)
    return {"id": None, "stored": False, "title": spec["title"], "spec": spec, "payload": payload}


@router.post("/runs")
async def create_run(
    body: RunIn,
    request: Request,
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    """Build a report and store it in the archive."""
    spec = _spec(body.spec)
    if body.definition_id:
        await _definition(db, body.definition_id)
    now = datetime.utcnow()
    start, end = _range(spec, now)
    await report_builder_limiter.check_and_record(admin.id)
    run = await rb.store_run(db, spec=spec, start=start, end=end, trigger="manual", generated_by=admin.id,
                             definition_id=body.definition_id, now=now)
    await record_audit(db, admin.id, "report_run_generated", "report_run", run.id,
                       detail=_audit_detail(definition_id=body.definition_id, period_start=rb.iso(start),
                                            period_end=rb.iso(end), metrics=len(spec["metrics"])),
                       ip_address=client_ip_or_none(request))
    await db.commit()
    return rb.run_detail(run)


@router.get("/runs")
async def list_runs(
    definition_id: Optional[str] = Query(None, max_length=36),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    """The archive, newest first."""
    conds = [ReportRun.definition_id == definition_id] if definition_id else []
    total = (await db.execute(select(func.count(ReportRun.id)).where(*conds))).scalar() or 0
    rows = (await db.execute(
        select(ReportRun).where(*conds).order_by(ReportRun.generated_at.desc(), ReportRun.id)
        .limit(limit).offset(offset)
    )).scalars().all()
    return {"items": [rb.run_meta(r) for r in rows], "total": int(total), "limit": limit, "offset": offset,
            "generated_at": rb.iso(datetime.utcnow())}


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    return rb.run_detail(await _run(db, run_id))


@router.post("/runs/{run_id}/regenerate")
async def regenerate_run(
    run_id: str,
    request: Request,
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    """Build the same spec over the same period again, as a new run. The old
    run stays in the archive as it was."""
    old = await _run(db, run_id)
    try:
        spec = rb.normalize_spec(dict(old.spec or {}), require_range=False)
    except rb.SpecError as exc:
        raise HTTPException(status_code=422, detail=f"this report's spec can no longer be built: {exc}")
    now = datetime.utcnow()
    await report_builder_limiter.check_and_record(admin.id)
    run = await rb.store_run(db, spec=spec, start=old.period_start, end=min(old.period_end, now),
                             trigger="regenerate", generated_by=admin.id, definition_id=old.definition_id,
                             regenerated_from=old.id, now=now)
    await record_audit(db, admin.id, "report_run_regenerated", "report_run", run.id,
                       detail=_audit_detail(regenerated_from=old.id, definition_id=old.definition_id),
                       ip_address=client_ip_or_none(request))
    await db.commit()
    return rb.run_detail(run)


@router.get("/runs/{run_id}/pdf")
async def run_pdf(
    run_id: str,
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    """The stored run as a PDF."""
    from api.core.report_pdf import pdf_filename, render_pdf

    run = await _run(db, run_id)
    if run.status != "ready" or not run.payload:
        raise HTTPException(status_code=409, detail="This report failed to build; regenerate it first.")
    meta = rb.run_meta(run)
    pdf = render_pdf(run.payload)
    return Response(content=pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{pdf_filename(meta)}"',
        "Cache-Control": "no-store",
    })


@router.get("/definitions")
async def list_definitions(
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(ReportDefinition).where(ReportDefinition.deleted_at.is_(None))
        .order_by(ReportDefinition.updated_at.desc(), ReportDefinition.id).limit(200)
    )).scalars().all()
    return {"items": [rb.definition_out(d) for d in rows], "total": len(rows),
            "generated_at": rb.iso(datetime.utcnow())}


@router.post("/definitions")
async def create_definition(
    body: DefinitionIn,
    request: Request,
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    """Save a report, optionally on a schedule."""
    fields = body.model_dump(exclude={"spec", "name", "description"})
    _schedule(fields)
    spec = _spec(body.spec, require_range=fields["schedule"] == "none")
    _require_email_permission(admin, body.recipients)
    recipients = await _admin_recipients(db, body.recipients)
    now = datetime.utcnow()
    d = ReportDefinition(
        name=body.name, description=body.description, spec=spec, schedule=fields["schedule"],
        schedule_every_days=fields["schedule_every_days"] if fields["schedule"] == "custom" else None,
        schedule_hour_utc=fields["schedule_hour_utc"],
        schedule_weekday=fields["schedule_weekday"] if fields["schedule"] == "weekly" else None,
        schedule_day=fields["schedule_day"] if fields["schedule"] == "monthly" else None,
        recipients=recipients, enabled=fields["enabled"], created_by=admin.id, created_at=now,
        updated_by=admin.id, updated_at=now, version=1,
    )
    d.next_run_at = rb.next_run_after(d, now)
    db.add(d)
    await db.flush()
    await record_audit(db, admin.id, "report_definition_created", "report_definition", d.id,
                       detail=_audit_detail(schedule=d.schedule, recipients=len(recipients)),
                       ip_address=client_ip_or_none(request))
    await db.commit()
    return rb.definition_out(d)


@router.patch("/definitions/{definition_id}")
async def update_definition(
    definition_id: str,
    body: DefinitionPatch,
    request: Request,
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    """Change a saved report. `expected_version` must be the version read:
    two administrators editing at once cannot overwrite each other."""
    d = await _definition(db, definition_id)
    if d.version != body.expected_version:
        raise HTTPException(status_code=409, detail="This saved report changed since you opened it; reload it.")
    changes = body.model_dump(exclude_unset=True, exclude={"expected_version", "spec"})
    merged = {
        "schedule": changes.get("schedule", d.schedule),
        "schedule_every_days": changes.get("schedule_every_days", d.schedule_every_days),
        "schedule_hour_utc": changes.get("schedule_hour_utc", d.schedule_hour_utc),
        "schedule_weekday": changes.get("schedule_weekday", d.schedule_weekday),
        "schedule_day": changes.get("schedule_day", d.schedule_day),
    }
    _schedule(merged)
    if body.spec is not None:
        d.spec = _spec(body.spec, require_range=merged["schedule"] == "none")
    elif merged["schedule"] == "none":
        try:
            rb.spec_range(rb.normalize_spec(dict(d.spec or {}), require_range=True), now=datetime.utcnow())
        except rb.SpecError:
            raise HTTPException(status_code=422, detail="an unscheduled report needs a date range")
    if "recipients" in changes:
        _require_email_permission(admin, changes["recipients"] or [])
        d.recipients = await _admin_recipients(db, changes["recipients"] or [])
    for name in ("name", "description", "enabled"):
        if name in changes and changes[name] is not None:
            setattr(d, name, changes[name])
    d.schedule = merged["schedule"]
    d.schedule_every_days = merged["schedule_every_days"] if d.schedule == "custom" else None
    d.schedule_hour_utc = merged["schedule_hour_utc"]
    d.schedule_weekday = merged["schedule_weekday"] if d.schedule == "weekly" else None
    d.schedule_day = merged["schedule_day"] if d.schedule == "monthly" else None
    now = datetime.utcnow()
    d.next_run_at = rb.next_run_after(d, now)
    d.version += 1
    d.updated_by, d.updated_at = admin.id, now
    await record_audit(db, admin.id, "report_definition_updated", "report_definition", d.id,
                       detail=_audit_detail(schedule=d.schedule, enabled=d.enabled, version=d.version,
                                            recipients=len(d.recipients or [])),
                       ip_address=client_ip_or_none(request))
    await db.commit()
    return rb.definition_out(d)


@router.post("/definitions/{definition_id}/delete")
async def delete_definition(
    definition_id: str,
    request: Request,
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    """Remove a saved report and stop its schedule. Its runs stay in the archive."""
    d = await _definition(db, definition_id)
    now = datetime.utcnow()
    d.deleted_at, d.enabled, d.next_run_at = now, False, None
    d.version += 1
    d.updated_by, d.updated_at = admin.id, now
    await record_audit(db, admin.id, "report_definition_deleted", "report_definition", d.id,
                       detail=_audit_detail(schedule=d.schedule), ip_address=client_ip_or_none(request))
    await db.commit()
    return {"id": d.id, "deleted": True}


@router.post("/definitions/{definition_id}/run")
async def run_definition(
    definition_id: str,
    request: Request,
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    """Run a saved report now: over its own dates, or, when it is scheduled,
    over the period its last scheduled run would cover."""
    d = await _definition(db, definition_id)
    now = datetime.utcnow()
    try:
        spec = rb.normalize_spec(dict(d.spec or {}), require_range=False)
        if spec.get("date_from") or spec.get("last_days"):
            start, end = rb.spec_range(spec, now=now)
        elif d.schedule != "none":
            start, end = rb.scheduled_period(d, now)
        else:
            raise rb.SpecError("this saved report has no date range")
    except rb.SpecError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await report_builder_limiter.check_and_record(admin.id)
    run = await rb.store_run(db, spec=spec, start=start, end=end, trigger="manual", generated_by=admin.id,
                             definition_id=d.id, now=now)
    await record_audit(db, admin.id, "report_run_generated", "report_run", run.id,
                       detail=_audit_detail(definition_id=d.id, period_start=rb.iso(start), period_end=rb.iso(end)),
                       ip_address=client_ip_or_none(request))
    await db.commit()
    return rb.run_detail(run)
