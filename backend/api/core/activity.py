"""Signed-in sessions, foreground activity and the figures built on them.

Only a heartbeat the app sends while it is in the foreground counts as
activity (PATCH /auth/heartbeat, about every 60 seconds). Other API calls do
not: the app polls and fetches in the background, and none of that is usage.
A heartbeat that says it came from the background is ignored.

Definitions (times are naive UTC; days are UTC calendar days):
  * active session   open, and its last heartbeat is within PRESENCE_WINDOW_SECONDS.
  * active user      an account with at least one active session.
  * foreground time  seconds credited from consecutive heartbeats. A gap longer
                     than HEARTBEAT_CONTINUITY_SECONDS earns nothing: the app may
                     have been backgrounded or offline, and nothing here can tell
                     which. A session's foreground time is its usage duration.
  * active on a day  a UserActivityDay row exists: the user sent a credited
                     foreground heartbeat that UTC day.

Writes are kept small. A heartbeat updates the session and the account at most
once per HEARTBEAT_WRITE_INTERVAL_SECONDS; a heartbeat that arrives sooner is
accepted and stores nothing. Each update is conditional on the value it read,
so a duplicate or concurrent heartbeat cannot credit the same time twice.
"""
from __future__ import annotations

import logging
import statistics
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import delete, desc, distinct, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.presence import ONLINE_THRESHOLD_SECS, online_status
from api.models.user_activity import ActivitySample, UserActivityDay, UserSession

logger = logging.getLogger(__name__)

PRESENCE_WINDOW_SECONDS = ONLINE_THRESHOLD_SECS
HEARTBEAT_WRITE_INTERVAL_SECONDS = 30
# The app beats about every 60 seconds. A gap of 2.5 beats is a missed beat
# or two on a slow network; anything longer is treated as a break in use.
HEARTBEAT_CONTINUITY_SECONDS = 150
# Sessions go quiet after the presence window, so an open session that has
# been silent that long is closed at its last heartbeat (not at the sweep
# time, which would count the silence as usage).
SESSION_IDLE_CLOSE_SECONDS = PRESENCE_WINDOW_SECONDS
SWEEP_INTERVAL_SECONDS = 60
END_REASONS = ("logout", "revoked", "suspended", "inactive")
# The metrics read at most this many closed sessions for the median. Above it
# the median is computed over the newest sample and says so.
MEDIAN_SESSION_LIMIT = 50_000
_PEAK_WINDOW_HOURS = 24


def utc_now() -> datetime:
    return datetime.utcnow()


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() + "Z" if value is not None else None


# ── Session lifecycle ────────────────────────────────────────────────────────

async def open_session(db: AsyncSession, user_id: str, *, now: Optional[datetime] = None) -> str:
    """Start a session for a newly issued token pair. The caller commits."""
    now = now or utc_now()
    session_id = str(uuid.uuid4())
    db.add(UserSession(
        id=session_id, user_id=user_id, started_at=now, last_active_at=now,
        foreground_seconds=0,
    ))
    return session_id


async def end_session(
    db: AsyncSession, session_id: str, reason: str, *, now: Optional[datetime] = None,
) -> None:
    """End one session. The caller commits.

    A session closed for inactivity is ended again with the stronger reason
    too: a later heartbeat may reopen only an "inactive" session, so a logout
    or suspension recorded over it must stop that.
    """
    if reason not in END_REASONS:
        raise ValueError(f"unknown session end reason: {reason}")
    now = now or utc_now()
    await db.execute(
        update(UserSession)
        .where(
            UserSession.id == session_id,
            or_(UserSession.ended_at.is_(None), UserSession.end_reason == "inactive"),
        )
        .values(ended_at=func.coalesce(UserSession.ended_at, now), end_reason=reason)
        .execution_options(synchronize_session=False)
    )


async def end_user_sessions(
    db: AsyncSession, user_id: str, reason: str, *, now: Optional[datetime] = None,
) -> int:
    """End every open session of one account. The caller commits."""
    if reason not in END_REASONS:
        raise ValueError(f"unknown session end reason: {reason}")
    now = now or utc_now()
    result = await db.execute(
        update(UserSession)
        .where(
            UserSession.user_id == user_id,
            or_(UserSession.ended_at.is_(None), UserSession.end_reason == "inactive"),
        )
        .values(ended_at=func.coalesce(UserSession.ended_at, now), end_reason=reason)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


# ── Heartbeats ───────────────────────────────────────────────────────────────

def _accepts_heartbeats(session: UserSession) -> bool:
    # Logout, revocation and suspension are final. Only an inactivity close
    # reopens: the app was alive and simply went quiet.
    return session.ended_at is None or session.end_reason == "inactive"


async def record_heartbeat(
    db: AsyncSession,
    user_id: str,
    session_id: Optional[str],
    *,
    foreground: bool = True,
    now: Optional[datetime] = None,
) -> None:
    """Credit one heartbeat. The caller commits.

    A token from before sessions existed has no session id. Its heartbeat still
    counts as presence (User.last_seen) but creates no session record.
    """
    if not foreground:
        return
    now = now or utc_now()

    # Account presence, throttled: a conditional update, so a beat inside the
    # interval changes nothing.
    stale_before = now - timedelta(seconds=HEARTBEAT_WRITE_INTERVAL_SECONDS)
    from api.database import User
    await db.execute(
        update(User)
        .where(User.id == user_id, or_(User.last_seen.is_(None), User.last_seen <= stale_before))
        .values(last_seen=now)
        .execution_options(synchronize_session=False)
    )
    if not session_id:
        return

    session = (await db.execute(
        select(UserSession).where(UserSession.id == session_id, UserSession.user_id == user_id)
    )).scalar_one_or_none()
    if session is None or not _accepts_heartbeats(session):
        return

    gap = (now - session.last_active_at).total_seconds()
    if session.ended_at is None:
        if gap < HEARTBEAT_WRITE_INTERVAL_SECONDS:
            return
        credit = int(gap) if gap <= HEARTBEAT_CONTINUITY_SECONDS else 0
    else:
        # Reopening after an inactivity close: the silent time is not usage.
        credit = 0

    previous_beat = session.last_active_at
    result = await db.execute(
        update(UserSession)
        .where(
            UserSession.id == session_id,
            UserSession.last_active_at == previous_beat,
            or_(UserSession.ended_at.is_(None), UserSession.end_reason == "inactive"),
        )
        .values(
            last_active_at=now,
            foreground_seconds=UserSession.foreground_seconds + credit,
            ended_at=None,
            end_reason=None,
        )
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:
        # Another beat or an end landed first; that one wrote the state.
        return
    await _credit_day(db, user_id, now, credit)


async def _credit_day(db: AsyncSession, user_id: str, now: datetime, credit: int) -> None:
    day = now.date()
    result = await db.execute(
        update(UserActivityDay)
        .where(UserActivityDay.user_id == user_id, UserActivityDay.activity_date == day)
        .values(
            last_active_at=now,
            foreground_seconds=UserActivityDay.foreground_seconds + credit,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount:
        return
    # The first beat of the day. Two devices beating at the same moment can
    # both reach this insert; the second fails the primary key, and the route
    # retries the whole heartbeat once, which then takes the update path.
    db.add(UserActivityDay(
        user_id=user_id, activity_date=day, first_active_at=now,
        last_active_at=now, foreground_seconds=credit,
    ))
    await db.flush()


# ── Counting active accounts and sessions ────────────────────────────────────

async def _active_counts(db: AsyncSession, now: datetime) -> tuple[int, int]:
    """(active users, active sessions) at `now`."""
    cutoff = now - timedelta(seconds=PRESENCE_WINDOW_SECONDS)
    row = (await db.execute(
        select(
            func.count(distinct(UserSession.user_id)),
            func.count(UserSession.id),
        ).where(UserSession.ended_at.is_(None), UserSession.last_active_at >= cutoff)
    )).one()
    return int(row[0] or 0), int(row[1] or 0)


async def sample_presence(db: AsyncSession, *, now: Optional[datetime] = None) -> bool:
    """Record this minute's active counts once. The caller commits."""
    now = now or utc_now()
    minute = now.replace(second=0, microsecond=0)
    already = (await db.execute(
        select(ActivitySample.sampled_at).where(ActivitySample.sampled_at == minute)
    )).scalar_one_or_none()
    if already is not None:
        return False
    users, sessions = await _active_counts(db, now)
    db.add(ActivitySample(sampled_at=minute, active_users=users, active_sessions=sessions))
    return True


async def close_inactive_sessions(db: AsyncSession, *, now: Optional[datetime] = None) -> int:
    """Close open sessions that have been silent past the idle limit. Caller commits."""
    now = now or utc_now()
    cutoff = now - timedelta(seconds=SESSION_IDLE_CLOSE_SECONDS)
    result = await db.execute(
        update(UserSession)
        .where(UserSession.ended_at.is_(None), UserSession.last_active_at < cutoff)
        .values(ended_at=UserSession.last_active_at, end_reason="inactive")
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def run_sweep(*, now: Optional[datetime] = None) -> dict:
    """One pass of the activity sweep: close silent sessions, sample presence, prune.

    Runs under the scheduler lease, so one replica does each pass. Each step
    commits on its own; a failure in one does not hold up the others.
    """
    from api.core.config import settings
    from api.database import AsyncSessionLocal

    now = now or utc_now()
    summary = {"closed_inactive": 0, "sampled": False, "pruned": {}}
    async with AsyncSessionLocal() as db:
        summary["closed_inactive"] = await close_inactive_sessions(db, now=now)
        summary["sampled"] = await sample_presence(db, now=now)
        await db.commit()

    async with AsyncSessionLocal() as db:
        session_cut = now - timedelta(days=settings.activity_session_retention_days)
        pruned_sessions = 0
        while True:
            # Batches keep each delete short, and the next batch starts from
            # the rows still left.
            ids = select(UserSession.id).where(UserSession.started_at < session_cut).limit(5000)
            result = await db.execute(delete(UserSession).where(UserSession.id.in_(ids)))
            batch = int(result.rowcount or 0)
            await db.commit()
            pruned_sessions += batch
            if batch < 5000:
                break
        summary["pruned"]["user_sessions"] = pruned_sessions

        day_cut = (now - timedelta(days=settings.activity_day_retention_days)).date()
        result = await db.execute(delete(UserActivityDay).where(UserActivityDay.activity_date < day_cut))
        summary["pruned"]["user_activity_days"] = int(result.rowcount or 0)

        sample_cut = now - timedelta(days=settings.activity_sample_retention_days)
        result = await db.execute(delete(ActivitySample).where(ActivitySample.sampled_at < sample_cut))
        summary["pruned"]["activity_samples"] = int(result.rowcount or 0)
        await db.commit()
    if summary["closed_inactive"]:
        logger.info("[activity] closed %d silent session(s)", summary["closed_inactive"])
    return summary


# ── Metrics ──────────────────────────────────────────────────────────────────

def _as_date(value) -> Optional[date]:
    """MIN() over a Date column comes back as a date on some drivers and as text on others."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


async def _coverage_start(db: AsyncSession) -> Optional[date]:
    """The first UTC day with any recorded activity, or None before tracking began."""
    return _as_date((await db.execute(select(func.min(UserActivityDay.activity_date)))).scalar())


async def live_snapshot(db: AsyncSession, *, now: Optional[datetime] = None) -> dict:
    now = now or utc_now()
    users_now, sessions_now = await _active_counts(db, now)

    windows: dict[str, int] = {}
    for minutes in (5, 15, 60):
        cutoff = now - timedelta(minutes=minutes)
        value = (await db.execute(
            select(func.count(distinct(UserSession.user_id)))
            .where(UserSession.last_active_at >= cutoff)
        )).scalar()
        windows[f"last_{minutes}m"] = int(value or 0)

    from api.database import User
    registered = int((await db.execute(
        select(func.count(User.id)).where(User.created_at <= now)
    )).scalar() or 0)

    peak = (await db.execute(
        select(ActivitySample.active_users, ActivitySample.sampled_at)
        .where(ActivitySample.sampled_at >= now - timedelta(hours=_PEAK_WINDOW_HOURS))
        .order_by(desc(ActivitySample.active_users), desc(ActivitySample.sampled_at))
        .limit(1)
    )).first()
    samples_in_window = int((await db.execute(
        select(func.count(ActivitySample.sampled_at))
        .where(ActivitySample.sampled_at >= now - timedelta(hours=_PEAK_WINDOW_HOURS))
    )).scalar() or 0)

    coverage = await _coverage_start(db)
    return {
        "generated_at": _iso(now),
        "presence_window_seconds": PRESENCE_WINDOW_SECONDS,
        "active_users_now": users_now,
        "active_sessions_now": sessions_now,
        "active_users": windows,
        "registered_users": registered,
        "active_user_percentage": {
            key: _pct(value, registered) for key, value in windows.items()
        },
        "peak_concurrent_users_24h": {
            "value": int(peak[0]) if peak else None,
            "sampled_at": _iso(peak[1]) if peak else None,
            "sample_interval_seconds": 60,
            "samples_in_window": samples_in_window,
        },
        "coverage_started_on": coverage.isoformat() if coverage else None,
    }


def _pct(part: int, whole: int) -> Optional[float]:
    if not whole:
        return None
    return round(100.0 * part / whole, 2)


async def _distinct_active_users(db: AsyncSession, first_day, last_day) -> int:
    from api.models.user_activity import UserActivityDay as Day
    value = (await db.execute(
        select(func.count(distinct(Day.user_id)))
        .where(Day.activity_date >= first_day, Day.activity_date <= last_day)
    )).scalar()
    return int(value or 0)


async def engagement_summary(
    db: AsyncSession, *, start: datetime, end: datetime, now: Optional[datetime] = None,
) -> dict:
    """Engagement over [start, end). DAU, WAU and MAU end on the range's last day."""
    from api.database import User
    now = now or utc_now()
    start_day = start.date()
    last_day = (end - timedelta(microseconds=1)).date()

    dau = await _distinct_active_users(db, last_day, last_day)
    wau = await _distinct_active_users(db, last_day - timedelta(days=6), last_day)
    mau = await _distinct_active_users(db, last_day - timedelta(days=29), last_day)

    in_range = (
        UserActivityDay.activity_date >= start_day,
        UserActivityDay.activity_date <= last_day,
    )
    row = (await db.execute(
        select(
            func.count(distinct(UserActivityDay.user_id)),
            func.count(UserActivityDay.user_id),
            func.sum(UserActivityDay.foreground_seconds),
        ).where(*in_range)
    )).one()
    active_in_range = int(row[0] or 0)
    user_days = int(row[1] or 0)
    total_seconds = int(row[2] or 0)

    returning = int((await db.execute(
        select(func.count(distinct(UserActivityDay.user_id)))
        .where(
            *in_range,
            UserActivityDay.user_id.in_(
                select(UserActivityDay.user_id).where(UserActivityDay.activity_date < start_day)
            ),
        )
    )).scalar() or 0)

    eligible = int((await db.execute(
        select(func.count(User.id)).where(User.created_at < end)
    )).scalar() or 0)

    closed = (
        UserSession.started_at >= start,
        UserSession.started_at < end,
        UserSession.ended_at.is_not(None),
    )
    session_row = (await db.execute(
        select(func.count(UserSession.id), func.avg(UserSession.foreground_seconds))
        .where(*closed)
    )).one()
    sessions_started = int((await db.execute(
        select(func.count(UserSession.id))
        .where(UserSession.started_at >= start, UserSession.started_at < end)
    )).scalar() or 0)
    median_values = [
        int(value or 0) for (value,) in (await db.execute(
            select(UserSession.foreground_seconds).where(*closed)
            .order_by(desc(UserSession.started_at)).limit(MEDIAN_SESSION_LIMIT)
        )).all()
    ]
    coverage = await _coverage_start(db)

    return {
        "from": _iso(start),
        "to": _iso(end),
        "as_of_date": last_day.isoformat(),
        "dau": dau,
        "wau": wau,
        "mau": mau,
        "dau_mau_ratio": round(dau / mau, 4) if mau else None,
        "active_users_in_range": active_in_range,
        "eligible_registered_users": eligible,
        "active_user_percentage": _pct(active_in_range, eligible),
        "new_active_users": max(0, active_in_range - returning),
        "returning_active_users": returning,
        "sessions_started_in_range": sessions_started,
        "closed_sessions_in_range": int(session_row[0] or 0),
        "average_session_foreground_seconds": round(float(session_row[1]), 1) if session_row[1] is not None else None,
        "median_session_foreground_seconds": (
            float(statistics.median(median_values)) if median_values else None
        ),
        "median_sample_truncated": len(median_values) >= MEDIAN_SESSION_LIMIT,
        "average_daily_active_seconds_per_user": (
            round(total_seconds / user_days, 1) if user_days else None
        ),
        "coverage_started_on": coverage.isoformat() if coverage else None,
    }


RETENTION_MAX_DAYS = 90
_RETENTION_HORIZONS = (1, 7, 30)


async def registration_retention(
    db: AsyncSession, *, start: datetime, end: datetime, now: Optional[datetime] = None,
) -> dict:
    """Share of each registration day's new accounts that were active again N days later.

    A cohort day is only scored when its accounts were registered after
    tracking began, and its N-day mark has already passed. Other cells are
    null: the account might have been active on a day nothing recorded.
    """
    from api.database import User
    now = now or utc_now()
    today = now.date()
    start_day = start.date()
    end_day = (end - timedelta(microseconds=1)).date()
    coverage = await _coverage_start(db)

    cohort_rows = (await db.execute(
        select(User.id, User.created_at).where(User.created_at >= start, User.created_at < end)
    )).all()
    horizon_last = min(end_day + timedelta(days=max(_RETENTION_HORIZONS)), today)
    activity_rows = (await db.execute(
        select(UserActivityDay.user_id, UserActivityDay.activity_date)
        .join(User, User.id == UserActivityDay.user_id)
        .where(
            User.created_at >= start, User.created_at < end,
            UserActivityDay.activity_date >= start_day,
            UserActivityDay.activity_date <= horizon_last,
        )
    )).all()

    # Per account, the set of active days as ordinals: a membership test per
    # horizon, instead of a query per cohort member.
    active_ordinals: dict[str, set[int]] = {}
    for user_id, active_day in activity_rows:
        active_ordinals.setdefault(user_id, set()).add(active_day.toordinal())
    cohorts: dict[date, list[str]] = {}
    for user_id, created_at in cohort_rows:
        cohorts.setdefault(created_at.date(), []).append(user_id)

    rows = []
    for day in sorted(cohorts):
        ids = cohorts[day]
        scored = coverage is not None and day > coverage
        record = {"cohort_date": day.isoformat(), "registered": len(ids)}
        for horizon in _RETENTION_HORIZONS:
            due = day + timedelta(days=horizon)
            key = f"d{horizon}"
            if not scored or due > today:
                record[f"{key}_retained"] = None
                record[f"{key}_pct"] = None
                continue
            retained = sum(
                1 for user_id in ids
                if due.toordinal() in active_ordinals.get(user_id, ())
            )
            record[f"{key}_retained"] = retained
            record[f"{key}_pct"] = _pct(retained, len(ids))
        rows.append(record)
    return {
        "from": _iso(start),
        "to": _iso(end),
        "horizons_days": list(_RETENTION_HORIZONS),
        "cohorts": rows,
        "coverage_started_on": coverage.isoformat() if coverage else None,
    }


async def user_activity_table(
    db: AsyncSession,
    *,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    now: Optional[datetime] = None,
) -> tuple[int, list[dict]]:
    """A page of accounts with their recent activity. Four queries per page, however long it is."""
    from api.database import Listing, User
    now = now or utc_now()
    since_dt = now - timedelta(days=30)
    since_day = now.date() - timedelta(days=29)

    conditions = []
    if search and search.strip():
        term = f"%{search.strip()}%"
        conditions.append(or_(
            User.name.ilike(term),
            User.nickname.ilike(term),
            User.phone.ilike(term),
            User.email.ilike(term),
        ))
    total = int((await db.execute(
        select(func.count(User.id)).where(*conditions)
    )).scalar() or 0)
    users = (await db.execute(
        select(User).where(*conditions)
        .order_by(User.last_seen.is_(None), desc(User.last_seen), desc(User.id))
        .limit(limit).offset(offset)
    )).scalars().all()
    ids = [user.id for user in users]
    if not ids:
        return total, []

    session_rows = {
        uid: (int(count or 0), int(seconds or 0))
        for uid, count, seconds in (await db.execute(
            select(UserSession.user_id, func.count(UserSession.id),
                   func.sum(UserSession.foreground_seconds))
            .where(UserSession.user_id.in_(ids), UserSession.started_at >= since_dt)
            .group_by(UserSession.user_id)
        )).all()
    }
    day_rows = {
        uid: (int(days or 0), int(seconds or 0))
        for uid, days, seconds in (await db.execute(
            select(UserActivityDay.user_id, func.count(UserActivityDay.user_id),
                   func.sum(UserActivityDay.foreground_seconds))
            .where(UserActivityDay.user_id.in_(ids), UserActivityDay.activity_date >= since_day)
            .group_by(UserActivityDay.user_id)
        )).all()
    }
    listing_rows = {
        uid: int(count or 0)
        for uid, count in (await db.execute(
            select(Listing.seller_id, func.count(Listing.id))
            .where(Listing.seller_id.in_(ids)).group_by(Listing.seller_id)
        )).all()
    }

    items = []
    for user in users:
        is_online, label = online_status(user.last_seen)
        sessions, session_seconds = session_rows.get(user.id, (0, 0))
        active_days, day_seconds = day_rows.get(user.id, (0, 0))
        items.append({
            "id": user.id,
            "name": user.name,
            "account_type": user.account_type.value if user.account_type else None,
            "registered_at": _iso(user.created_at),
            "last_seen": _iso(user.last_seen),
            "is_online": is_online,
            "presence_label": label,
            "listings": listing_rows.get(user.id, 0),
            "sessions_30d": sessions,
            "active_days_30d": active_days,
            "foreground_seconds_30d": day_seconds,
            "is_flagged": bool(user.is_flagged),
            "is_admin": bool(user.is_admin),
            "trust_score": user.trust_score if user.trust_score is not None else 100,
        })
    return total, items


async def user_activity_profile(
    db: AsyncSession, user_id: str, *, now: Optional[datetime] = None,
) -> Optional[dict]:
    from api.database import User
    now = now or utc_now()
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        return None
    is_online, label = online_status(user.last_seen)

    session_totals = (await db.execute(
        select(
            func.count(UserSession.id),
            func.sum(UserSession.foreground_seconds),
            func.min(UserSession.started_at),
            func.max(UserSession.last_active_at),
        ).where(UserSession.user_id == user_id)
    )).one()
    day_totals = (await db.execute(
        select(
            func.count(UserActivityDay.user_id),
            func.sum(UserActivityDay.foreground_seconds),
            func.min(UserActivityDay.activity_date),
        ).where(UserActivityDay.user_id == user_id)
    )).one()
    recent_sessions = (await db.execute(
        select(UserSession).where(UserSession.user_id == user_id)
        .order_by(desc(UserSession.started_at)).limit(20)
    )).scalars().all()
    daily = (await db.execute(
        select(UserActivityDay).where(
            UserActivityDay.user_id == user_id,
            UserActivityDay.activity_date >= now.date() - timedelta(days=29),
        ).order_by(desc(UserActivityDay.activity_date)).limit(30)
    )).scalars().all()
    last_30 = (await db.execute(
        select(func.count(UserActivityDay.user_id), func.sum(UserActivityDay.foreground_seconds))
        .where(UserActivityDay.user_id == user_id,
               UserActivityDay.activity_date >= now.date() - timedelta(days=29))
    )).one()

    return {
        "generated_at": _iso(now),
        "user": {
            "id": user.id,
            "name": user.name,
            "account_type": user.account_type.value if user.account_type else None,
            "is_flagged": bool(user.is_flagged),
            "is_admin": bool(user.is_admin),
            "registered_at": _iso(user.created_at),
            "last_seen": _iso(user.last_seen),
            "is_online": is_online,
            "presence_label": label,
        },
        "totals": {
            "sessions": int(session_totals[0] or 0),
            "foreground_seconds": int(session_totals[1] or 0),
            "first_session_at": _iso(session_totals[2]),
            "last_active_at": _iso(session_totals[3]),
            "active_days": int(day_totals[0] or 0),
            "first_active_on": _as_date(day_totals[2]).isoformat() if day_totals[2] else None,
        },
        "last_30_days": {
            "active_days": int(last_30[0] or 0),
            "foreground_seconds": int(last_30[1] or 0),
        },
        "recent_sessions": [
            {
                "id": s.id,
                "started_at": _iso(s.started_at),
                "last_active_at": _iso(s.last_active_at),
                "ended_at": _iso(s.ended_at),
                "end_reason": s.end_reason,
                "foreground_seconds": int(s.foreground_seconds or 0),
                "is_open": s.ended_at is None,
            }
            for s in recent_sessions
        ],
        "daily": [
            {
                "date": d.activity_date.isoformat(),
                "foreground_seconds": int(d.foreground_seconds or 0),
                "first_active_at": _iso(d.first_active_at),
                "last_active_at": _iso(d.last_active_at),
            }
            for d in daily
        ],
    }


async def active_users_series(db: AsyncSession, buckets, bucket: str) -> dict:
    """Distinct active users per bucket, with null where tracking did not reach.

    A bucket is scored only when it starts after the first recorded day: a
    partial first day would undercount, and before tracking began the count
    is not known at all.
    """
    coverage = await _coverage_start(db)
    if coverage is None:
        return {
            "points": [{"t": b_start.isoformat() + "Z", "v": None} for b_start, _ in buckets],
            "coverage_started_on": None,
        }

    values: list[int] = []
    if bucket == "day":
        first = buckets[0][0].date() if buckets else coverage
        last = buckets[-1][1].date() if buckets else coverage
        counts = dict((await db.execute(
            select(UserActivityDay.activity_date, func.count(UserActivityDay.user_id))
            .where(UserActivityDay.activity_date >= first, UserActivityDay.activity_date < last)
            .group_by(UserActivityDay.activity_date)
        )).all())
        for b_start, _ in buckets:
            values.append(int(counts.get(b_start.date(), 0)))
    else:
        for b_start, b_end in buckets:
            values.append(await _distinct_active_users(
                db, b_start.date(), b_end.date() - timedelta(days=1),
            ))

    points = []
    for (b_start, _), value in zip(buckets, values):
        scored = b_start.date() > coverage
        points.append({"t": b_start.isoformat() + "Z", "v": value if scored else None})
    return {"points": points, "coverage_started_on": coverage.isoformat()}
