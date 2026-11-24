"""Listing and marketplace intelligence: rankings, conversion, listing
detail and seller / buyer performance.

Built only from rows BROKA already keeps - listing views, saves (wishlists),
likes, buyer contact (a buyer's own chat message or an interests row), deals
and the daily listing snapshots - so every figure can be traced to a table.

Definitions, repeated in every response:

  * Deduplicated means distinct USERS per listing inside the window. One
    person who views, saves and messages is one viewer, one saver and one
    contact; a person who messages ten times is one contact.
  * Staff (is_admin) and the listing's own seller are never counted.
  * listing_views holds one row per (listing, viewer): the first time an
    account viewed it. A "viewer" in a window is therefore a first-time
    viewer in that window, never a repeat visit counted again.
  * Rates are contacters / viewers, capped at 100: a buyer may message
    without a first view falling in the same window.
  * Trending compares an engagement score (viewers + 2 x (savers + likers)
    + 4 x contacters, all distinct users) with the window before it.

Row-level reads are bounded by ROW_CAP and report `truncated` instead of
silently dropping rows. Times are naive UTC.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.intelligence import _AGREED_STATES, ROW_CAP, _chunks, _staff, change_pct, iso, median
from api.core.timeutil import to_naive_utc
from api.database import (
    Deal, Interest, Listing, ListingLike, ListingMetricSnapshot, ListingType, ListingView,
    NegotiationMessage, User, Wishlist,
)

SIGNALS = ("views", "saves", "likes", "contacts")
MIN_VIEWERS = 5
MIN_TRENDING_SCORE = 3
MIN_BENCHMARK_LISTINGS = 4

DEFINITIONS = {
    "deduplicated": "Distinct users per listing in the window; repeat actions by one user count once.",
    "excluded": "Administrator accounts and the listing's own seller are never counted.",
    "views": "First-time viewers in the window (listing_views keeps one row per listing and viewer).",
    "saves": "Users who added the listing to their wishlist in the window.",
    "likes": "Users who liked the listing in the window.",
    "contacts": "Users who sent a buyer chat message or showed interest in the window.",
    "contact_rate": "Contacters / viewers, capped at 100%.",
    "trending": "Engagement score (viewers + 2 x (savers + likers) + 4 x contacters) against the previous window of equal length.",
}


def _sources():
    """signal -> [(model, listing column, user column, extra conditions)]."""
    return {
        "views": [(ListingView, ListingView.listing_id, ListingView.viewer_id, [])],
        "saves": [(Wishlist, Wishlist.listing_id, Wishlist.user_id, [])],
        "likes": [(ListingLike, ListingLike.listing_id, ListingLike.user_id, [])],
        "contacts": [
            (Interest, Interest.listing_id, Interest.buyer_id, []),
            # visibility-ok: distinct sender ids and first-message times, no content.
            (NegotiationMessage, NegotiationMessage.listing_id, NegotiationMessage.sender_id,
             [NegotiationMessage.role == "buyer"]),
        ],
    }


async def engagement(
    db: AsyncSession, start: datetime, end: datetime, listing_id: Optional[str] = None,
) -> tuple[dict[str, dict[str, dict[str, Optional[datetime]]]], bool]:
    """signal -> listing id -> {user id: first time in the window}."""
    staff = _staff()
    out: dict[str, dict[str, dict]] = {s: defaultdict(dict) for s in SIGNALS}
    truncated = False
    for signal, sources in _sources().items():
        for model, lcol, ucol, extra in sources:
            conds = [model.created_at >= start, model.created_at < end,
                     lcol.is_not(None), ucol.is_not(None), ucol.not_in(staff), *extra]
            if listing_id is not None:
                conds.append(lcol == listing_id)
            rows = (await db.execute(
                select(lcol, ucol, func.min(model.created_at)).where(*conds)
                .group_by(lcol, ucol).limit(ROW_CAP + 1)
            )).all()
            truncated = truncated or len(rows) > ROW_CAP
            for lid, uid, at in rows[:ROW_CAP]:
                at = to_naive_utc(at) if at else None
                seen = out[signal][lid]
                if uid not in seen or (at is not None and (seen[uid] is None or at < seen[uid])):
                    seen[uid] = at
    return out, truncated


async def _listing_meta(db: AsyncSession, ids: set[str]) -> dict[str, dict]:
    meta: dict[str, dict] = {}
    for chunk in _chunks(sorted(ids), 500):
        rows = (await db.execute(
            select(Listing.id, Listing.name, Listing.category, Listing.location_county,
                   Listing.seller_id, Listing.listing_type)
            .where(Listing.id.in_(chunk), Listing.seller_id.not_in(_staff()))
        )).all()
        for lid, name, category, county, seller_id, ltype in rows:
            if ltype == ListingType.auction:
                continue
            meta[lid] = {"name": name, "category": category, "county": county, "seller_id": seller_id}
    return meta


def _ids(eng: dict) -> set[str]:
    out: set[str] = set()
    for signal in SIGNALS:
        out |= set(eng[signal])
    return out


def _tally(eng: dict, meta: dict, category: Optional[str] = None) -> dict[str, dict[str, set]]:
    tallies: dict[str, dict[str, set]] = {}
    for lid, m in meta.items():
        if category and (m["category"] or "").lower() != category.lower():
            continue
        sets = {s: set(eng[s].get(lid, {})) - {m["seller_id"]} for s in SIGNALS}
        if any(sets.values()):
            tallies[lid] = sets
    return tallies


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return None if not denominator else min(100.0, round(numerator / denominator * 100, 1))


def _score(sets: Optional[dict[str, set]]) -> int:
    if not sets:
        return 0
    return (len(sets["views"]) + 2 * (len(sets["saves"]) + len(sets["likes"])) + 4 * len(sets["contacts"]))


def _row(lid: str, m: dict, sets: dict[str, set]) -> dict:
    viewers, savers, likers, contacters = (len(sets[s]) for s in SIGNALS)
    return {
        "listing_id": lid, "name": m["name"], "category": m["category"], "county": m["county"],
        "seller_id": m["seller_id"], "viewers": viewers, "savers": savers, "likers": likers,
        "contacters": contacters,
        "view_to_save_pct": _rate(savers, viewers),
        "view_to_contact_pct": _rate(contacters, viewers),
    }


def _top(rows: list[dict], key: str, limit: int) -> list[dict]:
    ranked = sorted((r for r in rows if r[key] > 0), key=lambda r: (-r[key], r["listing_id"]))
    return ranked[:limit]


def conversion_insights(rows: list[dict], limit: int) -> dict:
    """Popularity is not conversion: flag listings many people see but few
    contact, and listings few people see that convert well."""
    eligible = [r for r in rows if r["viewers"] >= MIN_VIEWERS]
    if len(eligible) < MIN_BENCHMARK_LISTINGS:
        return {"benchmark": None, "popular_not_converting": [], "hidden_gems": [],
                "note": f"Needs at least {MIN_BENCHMARK_LISTINGS} listings with {MIN_VIEWERS}+ viewers."}
    med_rate = median(r["view_to_contact_pct"] or 0.0 for r in eligible) or 0.0
    med_viewers = median(r["viewers"] for r in eligible) or 0.0
    low = [dict(r, note=f"{r['viewers']} viewers but {r['view_to_contact_pct']}% contacted (median {med_rate}%): "
                        "review price, photos or description.")
           for r in eligible
           if r["viewers"] >= med_viewers and (r["view_to_contact_pct"] or 0.0) <= med_rate / 2]
    gems = [dict(r, note=f"{r['view_to_contact_pct']}% of {r['viewers']} viewers contacted (median {med_rate}%): "
                         "worth promoting to reach more buyers.")
            for r in eligible
            if r["viewers"] < med_viewers and r["contacters"] >= 2
            and (r["view_to_contact_pct"] or 0.0) >= med_rate * 2]
    low.sort(key=lambda r: (-r["viewers"], r["listing_id"]))
    gems.sort(key=lambda r: (-(r["view_to_contact_pct"] or 0.0), r["listing_id"]))
    return {"benchmark": {"median_contact_rate_pct": med_rate, "median_viewers": med_viewers,
                          "listings_compared": len(eligible)},
            "popular_not_converting": low[:limit], "hidden_gems": gems[:limit], "note": None}


async def rankings(
    db: AsyncSession, *, window_days: int = 7, limit: int = 10,
    category: Optional[str] = None, now: Optional[datetime] = None,
) -> dict:
    now = now or datetime.utcnow()
    start = now - timedelta(days=window_days)
    prev_start = start - timedelta(days=window_days)
    eng, cut = await engagement(db, start, now)
    prev_eng, prev_cut = await engagement(db, prev_start, start)
    meta = await _listing_meta(db, _ids(eng) | _ids(prev_eng))
    current = _tally(eng, meta, category)
    previous = _tally(prev_eng, meta, category)
    rows = [_row(lid, meta[lid], sets) for lid, sets in current.items()]

    trending = []
    for lid, sets in current.items():
        score, before = _score(sets), _score(previous.get(lid))
        if score < MIN_TRENDING_SCORE or score <= before:
            continue
        trending.append(dict(_row(lid, meta[lid], sets), score=score, previous_score=before,
                             delta=score - before, growth_pct=change_pct(score, before)))
    trending.sort(key=lambda r: (-r["delta"], r["listing_id"]))
    return {
        "generated_at": iso(now),
        "window": {"from": iso(start), "to": iso(now), "days": window_days},
        "category": category,
        "truncated": cut or prev_cut,
        "listings_engaged": len(rows),
        "rankings": {
            "viewed": _top(rows, "viewers", limit),
            "saved": _top(rows, "savers", limit),
            "liked": _top(rows, "likers", limit),
            "contacted": _top(rows, "contacters", limit),
            "trending": trending[:limit],
        },
        "conversion": conversion_insights(rows, limit),
        "definitions": DEFINITIONS,
    }


async def listing_detail(
    db: AsyncSession, listing_id: str, *, window_days: int = 30, now: Optional[datetime] = None,
) -> Optional[dict]:
    listing = (await db.execute(select(Listing).where(Listing.id == listing_id))).scalar_one_or_none()
    if listing is None:
        return None
    now = now or datetime.utcnow()
    start = now - timedelta(days=window_days)
    eng, cut = await engagement(db, start, now, listing_id)
    seller = listing.seller_id
    sets = {s: set(eng[s].get(listing_id, {})) - {seller} for s in SIGNALS}

    first_day = start.date()
    days = [first_day + timedelta(days=i) for i in range((now.date() - first_day).days + 1)]
    daily = {d: {s: 0 for s in SIGNALS} for d in days}
    for signal in SIGNALS:
        for uid, at in eng[signal].get(listing_id, {}).items():
            if uid == seller or at is None:
                continue
            if at.date() in daily:
                daily[at.date()][signal] += 1

    deal_rows = (await db.execute(
        select(Deal.status, func.count(Deal.id))
        .where(Deal.listing_id == listing_id, Deal.created_at >= start).group_by(Deal.status)
    )).all()
    deals = {getattr(s, "value", str(s)): int(n) for s, n in deal_rows}
    agreed = sum(n for s, n in deals.items() if s in {st.value for st in _AGREED_STATES})

    snapshots = (await db.execute(
        select(ListingMetricSnapshot).where(
            ListingMetricSnapshot.listing_id == listing_id,
            ListingMetricSnapshot.snapshot_date >= first_day)
        .order_by(ListingMetricSnapshot.snapshot_date)
    )).scalars().all()

    # Where it stands among its category's engaged listings in the window.
    all_eng, all_cut = await engagement(db, start, now)
    meta = await _listing_meta(db, _ids(all_eng) | {listing_id})
    peers = [
        _row(lid, meta[lid], s) for lid, s in _tally(all_eng, meta, listing.category).items()
    ]
    own = _row(listing_id, {"name": listing.name, "category": listing.category,
                            "county": listing.location_county, "seller_id": seller}, sets)
    by_viewers = sorted({r["viewers"] for r in peers} | {own["viewers"]}, reverse=True)
    peer_rates = [r["view_to_contact_pct"] for r in peers if r["viewers"] >= MIN_VIEWERS]

    created = listing.created_at
    return {
        "generated_at": iso(now),
        "window": {"from": iso(start), "to": iso(now), "days": window_days},
        "truncated": cut or all_cut,
        "listing": {
            "id": listing.id, "name": listing.name, "category": listing.category,
            "county": listing.location_county, "seller_id": seller, "price": float(listing.price),
            "status": getattr(listing.status, "value", str(listing.status)),
            "created_at": iso(created),
            "age_days": int((now - to_naive_utc(created)).days) if created else None,
            "lifetime_views_counter": int(listing.views or 0),
        },
        "totals": own,
        "funnel": {"viewers": own["viewers"], "savers_or_likers": len(sets["saves"] | sets["likes"]),
                   "contacters": own["contacters"], "deals_agreed": agreed},
        "deals_by_status": deals,
        "daily": [{"date": d.isoformat(), **daily[d]} for d in days],
        "snapshots": [
            {"date": s.snapshot_date.isoformat(), "views": s.views, "likes": s.likes,
             "interested_buyers": s.interested_buyers, "sell_probability": s.sell_probability}
            for s in snapshots
        ],
        "category_position": {
            "listings_compared": len(peers),
            "viewers_rank": by_viewers.index(own["viewers"]) + 1,
            "median_contact_rate_pct": median(r for r in peer_rates if r is not None),
        },
        "definitions": DEFINITIONS,
    }


async def performance(
    db: AsyncSession, *, window_days: int = 30, limit: int = 10, now: Optional[datetime] = None,
) -> dict:
    """Sellers by the attention and contact their listings earn, and buyers
    by how much they browse against how often they reach out."""
    now = now or datetime.utcnow()
    start = now - timedelta(days=window_days)
    eng, cut = await engagement(db, start, now)
    meta = await _listing_meta(db, _ids(eng))
    tallies = _tally(eng, meta)

    sellers: dict[str, dict[str, Any]] = {}
    buyers: dict[str, dict[str, set]] = defaultdict(lambda: {s: set() for s in SIGNALS})
    for lid, sets in tallies.items():
        entry = sellers.setdefault(meta[lid]["seller_id"], {
            "listings": 0, **{s: set() for s in SIGNALS}})
        entry["listings"] += 1
        for signal in SIGNALS:
            entry[signal] |= sets[signal]
            for uid in sets[signal]:
                buyers[uid][signal].add(lid)

    agreed_states = list(_AGREED_STATES)
    seller_deals = dict((await db.execute(
        select(Deal.seller_id, func.count(Deal.id)).where(
            Deal.created_at >= start, Deal.created_at < now, Deal.status.in_(agreed_states),
            Deal.seller_id.not_in(_staff())).group_by(Deal.seller_id))).all())
    buyer_deals = dict((await db.execute(
        select(Deal.buyer_id, func.count(Deal.id)).where(
            Deal.created_at >= start, Deal.created_at < now, Deal.status.in_(agreed_states),
            Deal.buyer_id.not_in(_staff())).group_by(Deal.buyer_id))).all())

    seller_rows = [{
        "seller_id": sid, "listings_engaged": e["listings"], "viewers": len(e["views"]),
        "savers": len(e["saves"]), "contacters": len(e["contacts"]),
        "contact_rate_pct": _rate(len(e["contacts"]), len(e["views"])),
        "deals_agreed": int(seller_deals.get(sid, 0)),
    } for sid, e in sellers.items()]
    seller_rows.sort(key=lambda r: (-r["contacters"], -r["viewers"], r["seller_id"]))

    buyer_rows = [{
        "buyer_id": uid, "listings_viewed": len(e["views"]), "listings_saved": len(e["saves"]),
        "listings_contacted": len(e["contacts"]), "deals_agreed": int(buyer_deals.get(uid, 0)),
    } for uid, e in buyers.items()]
    buyer_rows.sort(key=lambda r: (-r["listings_contacted"], -r["listings_viewed"], r["buyer_id"]))
    browsers = sum(1 for e in buyers.values() if e["views"] and not e["contacts"])

    top_sellers, top_buyers = seller_rows[:limit], buyer_rows[:limit]
    wanted = [r["seller_id"] for r in top_sellers] + [r["buyer_id"] for r in top_buyers]
    names = {}
    if wanted:
        names = dict((await db.execute(
            select(User.id, func.coalesce(User.business_display_name, User.name))
            .where(User.id.in_(wanted)))).all())
    for row in top_sellers:
        row["name"] = names.get(row["seller_id"])
    for row in top_buyers:
        row["name"] = names.get(row["buyer_id"])
    return {
        "generated_at": iso(now),
        "window": {"from": iso(start), "to": iso(now), "days": window_days},
        "truncated": cut,
        "sellers": {"total": len(seller_rows), "top": top_sellers},
        "buyers": {"total": len(buyer_rows), "browsers_without_contact": browsers, "top": top_buyers},
        "definitions": {
            **DEFINITIONS,
            "seller_contact_rate": "Distinct contacters / distinct viewers across the seller's listings.",
            "browsers_without_contact": "Buyers who viewed at least one listing and contacted none in the window.",
        },
    }
