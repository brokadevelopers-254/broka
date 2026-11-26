"""Listing and marketplace intelligence: rankings, conversion, attention,
listing detail, demand growth and seller / buyer performance.

Built only from rows BROKA already keeps - listing views, saves (wishlists),
likes, buyer contact (a buyer's own chat message or an interests row), the
seller's replies, price offers, deals and the daily listing snapshots - so
every figure can be traced to a table. Search queries are not recorded, so
nothing here measures search.

Definitions, repeated in every response:

  * Deduplicated means distinct USERS per listing inside the window. One
    person who views, saves and messages is one viewer, one saver and one
    contact; a person who messages ten times is one contact.
  * Staff (is_admin) and the listing's own seller are never counted.
  * listing_views holds one row per (listing, viewer): the first time an
    account viewed it. A "viewer" in a window is therefore a first-time
    viewer in that window, never a repeat visit counted again.
  * The stages a buyer can reach on a listing, each counted once per buyer:
        viewed, saved or liked, contacted, negotiated, deal agreed, deal completed.
    Negotiated: the seller wrote in the buyer's thread (the conversation went
    both ways) or the buyer made a price offer. A negotiation is not a sale:
    a deal agreed needs a deal row past negotiation, and a deal completed needs
    the escrow released to the seller after the buyer confirmed delivery.
  * Rates are capped at 100: a buyer may message without a first view in the
    same window.
  * Score: the weighted sum of distinct users at each stage (DEFAULT_WEIGHTS,
    overridable with MARKET_TRENDING_WEIGHTS). Trending, growing and losing
    attention compare it with the window of equal length just before.

Windows are half-open [from, to) in naive UTC. Row-level reads are bounded by
ROW_CAP and report `truncated` instead of silently dropping rows.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.config import settings
from api.core.intelligence import (
    _AGREED_STATES, REPLY_WINDOW, ROW_CAP, _chunks, _staff, categories_between, change_pct,
    geography, iso, median,
)
from api.core.timeutil import to_naive_utc
from api.database import (
    Deal, DealStatus, Interest, Listing, ListingLike, ListingMetricSnapshot, ListingStatus,
    ListingType, ListingView, NegotiationMessage, User, Wishlist,
)

logger = logging.getLogger(__name__)

SIGNALS = ("views", "saves", "likes", "contacts", "negotiations", "deals", "completed")
# What the score adds up. Completion is reported beside it, not inside it: a
# release can come weeks after the attention that led to it.
SCORED = ("views", "saves", "likes", "contacts", "negotiations", "deals")
DEFAULT_WEIGHTS = {"views": 1.0, "saves": 2.0, "likes": 2.0, "contacts": 4.0, "negotiations": 6.0, "deals": 10.0}
_SCORE_LABELS = {"views": "viewers", "saves": "savers", "likes": "likers", "contacts": "contacters",
                 "negotiations": "negotiators", "deals": "buyers who agreed a deal"}

MIN_VIEWERS = 5
MIN_TRENDING_SCORE = 3
MIN_GROWTH_BASE = 5
MIN_LOSING_BASE = 10
LOSING_SHARE = 0.5
MIN_BENCHMARK_LISTINGS = 4
MIN_CONTACTS_WITHOUT_DEAL = 3
MIN_UNUSUAL_CONTACTS = 3
UNUSUAL_MULTIPLE = 3
MIN_DEMAND_FOR_GROWTH = 3

DEFINITIONS = {
    "deduplicated": "Distinct users per listing in the window; repeat actions by one user count once.",
    "excluded": "Administrator accounts and the listing's own seller are never counted.",
    "views": "First-time viewers in the window (listing_views keeps one row per listing and viewer).",
    "saves": "Users who added the listing to their wishlist in the window.",
    "likes": "Users who liked the listing in the window.",
    "contacts": "Users who sent a buyer chat message or showed interest in the window.",
    "negotiations": ("Buyers whose conversation on the listing went both ways in the window (the seller "
                     "wrote in their thread) or who made a price offer. A negotiation is not a sale."),
    "deals": "Buyers with a deal on the listing created in the window and now past negotiation (not cancelled).",
    "completed": ("Buyers whose deal on the listing completed in the window: the escrow was released to "
                  "the seller after the buyer confirmed delivery. Deals settled off BROKA are not seen."),
    "contact_rate": "Contacters / viewers, capped at 100%.",
    "deal_rate": ("Buyers who agreed a deal / buyers who contacted, capped at 100%. Contact is engagement; "
                  "this rate is conversion."),
    "trending": "Score gain against the previous window of equal length; the score must be at least 3 and higher than before.",
    "growing": ("Relative score gain, for listings that scored at least 5 in the previous window: a rise "
                "from almost nothing is not growth."),
    "losing_attention": "Listings that scored at least 10 in the previous window and at most half of that now.",
    "needs_seller": ("Buyers whose first message in the window has had no reply from the seller for over 24 "
                     "hours, as of the end of the window. Zeno answering does not count as the seller."),
}

NOT_INSTRUMENTED = [
    {"metric": "searches", "reason": "Search queries are not recorded, so search-to-contact conversion cannot be measured."},
]


def trending_weights(raw: Optional[str] = None) -> dict[str, float]:
    """The score's weights: DEFAULT_WEIGHTS, overridden by MARKET_TRENDING_WEIGHTS
    ("views=1,saves=2,..."). An unknown stage, a negative or a non-number is
    ignored and logged, so a typo cannot zero the ranking."""
    text = settings.market_trending_weights if raw is None else raw
    weights = dict(DEFAULT_WEIGHTS)
    for part in (text or "").split(","):
        if not part.strip():
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        try:
            number = float(value)
        except ValueError:
            number = float("nan")
        if key not in weights or not math.isfinite(number) or number < 0:
            logger.warning("[market] ignored trending weight %r", part.strip()[:40])
            continue
        weights[key] = number
    return weights


def _formula(weights: dict[str, float]) -> str:
    terms = " + ".join(f"{weights[s]:g} x {_SCORE_LABELS[s]}" for s in SCORED)
    return f"Score = {terms}, counting distinct users at each stage."


def _window(start: datetime, end: datetime) -> dict:
    width = end - start
    return {"from": iso(start), "to": iso(end), "days": round(width.total_seconds() / 86400, 2),
            "previous_from": iso(start - width), "previous_to": iso(start)}


def _sources():
    """signal -> [(model, listing column, user column, time column, extra conditions)]."""
    agreed = list(_AGREED_STATES)
    return {
        "views": [(ListingView, ListingView.listing_id, ListingView.viewer_id, ListingView.created_at, [])],
        "saves": [(Wishlist, Wishlist.listing_id, Wishlist.user_id, Wishlist.created_at, [])],
        "likes": [(ListingLike, ListingLike.listing_id, ListingLike.user_id, ListingLike.created_at, [])],
        "contacts": [
            (Interest, Interest.listing_id, Interest.buyer_id, Interest.created_at, []),
            # visibility-ok: distinct sender ids and first-message times, no content.
            (NegotiationMessage, NegotiationMessage.listing_id, NegotiationMessage.sender_id,
             NegotiationMessage.created_at, [NegotiationMessage.role == "buyer"]),
        ],
        "negotiations": [
            # visibility-ok: thread keys and the seller's own message times, no content.
            (NegotiationMessage, NegotiationMessage.listing_id, NegotiationMessage.buyer_id,
             NegotiationMessage.created_at,
             [NegotiationMessage.role == "seller", NegotiationMessage.deleted_at.is_(None)]),
            (Interest, Interest.listing_id, Interest.buyer_id, Interest.created_at,
             [Interest.offer_price.is_not(None), Interest.offer_price > 0]),
        ],
        "deals": [(Deal, Deal.listing_id, Deal.buyer_id, Deal.created_at, [Deal.status.in_(agreed)])],
        "completed": [(Deal, Deal.listing_id, Deal.buyer_id, Deal.released_at,
                       [Deal.status == DealStatus.released])],
    }


async def engagement(
    db: AsyncSession, start: datetime, end: datetime, listing_id: Optional[str] = None,
    signals: Iterable[str] = SIGNALS,
) -> tuple[dict[str, dict[str, dict[str, Optional[datetime]]]], bool]:
    """signal -> listing id -> {user id: first time in the window}."""
    staff = _staff()
    sources = _sources()
    out: dict[str, dict[str, dict]] = {s: defaultdict(dict) for s in SIGNALS}
    truncated = False
    for signal in signals:
        for model, lcol, ucol, tcol, extra in sources[signal]:
            conds = [tcol >= start, tcol < end, lcol.is_not(None), ucol.is_not(None),
                     ucol.not_in(staff), *extra]
            if listing_id is not None:
                conds.append(lcol == listing_id)
            rows = (await db.execute(
                select(lcol, ucol, func.min(tcol)).where(*conds)
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
        out |= set(eng.get(signal, {}))
    return out


def _matches(m: dict, category: Optional[str], county: Optional[str]) -> bool:
    if category and (m["category"] or "").strip().lower() != category.strip().lower():
        return False
    if county and (m["county"] or "").strip().lower() != county.strip().lower():
        return False
    return True


def _tally(eng: dict, meta: dict, category: Optional[str] = None,
           county: Optional[str] = None) -> dict[str, dict[str, set]]:
    tallies: dict[str, dict[str, set]] = {}
    for lid, m in meta.items():
        if not _matches(m, category, county):
            continue
        sets = {s: set(eng.get(s, {}).get(lid, {})) - {m["seller_id"]} for s in SIGNALS}
        if any(sets.values()):
            tallies[lid] = sets
    return tallies


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return None if not denominator else min(100.0, round(numerator / denominator * 100, 1))


def _score(sets: Optional[dict[str, set]], weights: dict[str, float]) -> tuple[float, dict[str, float]]:
    breakdown = {s: round(weights[s] * len((sets or {}).get(s, ())), 2) for s in SCORED}
    return round(sum(breakdown.values()), 2), breakdown


def _row(lid: str, m: dict, sets: dict[str, set], weights: dict[str, float]) -> dict:
    counts = {s: len(sets.get(s, ())) for s in SIGNALS}
    score, breakdown = _score(sets, weights)
    return {
        "listing_id": lid, "name": m["name"], "category": m["category"], "county": m["county"],
        "seller_id": m["seller_id"],
        "viewers": counts["views"], "savers": counts["saves"], "likers": counts["likes"],
        "contacters": counts["contacts"], "negotiators": counts["negotiations"],
        "deal_buyers": counts["deals"], "completed_buyers": counts["completed"],
        "view_to_save_pct": _rate(counts["saves"], counts["views"]),
        "view_to_contact_pct": _rate(counts["contacts"], counts["views"]),
        "contact_to_deal_pct": _rate(counts["deals"], counts["contacts"]),
        "score": score, "breakdown": breakdown,
    }


def _top(rows: list[dict], key: str, limit: int) -> list[dict]:
    ranked = sorted((r for r in rows if r[key] > 0), key=lambda r: (-r[key], r["listing_id"]))
    return ranked[:limit]


def conversion_insights(rows: list[dict], limit: int) -> dict:
    """Popularity is not conversion. Flags listings many people see but few
    contact, listings few see that convert well, listings buyers talk about
    without agreeing a deal, and listings with unusually high interest."""
    out: dict[str, Any] = {"benchmark": None, "popular_not_converting": [], "hidden_gems": [],
                           "engaged_not_converting": [], "unusual_interest": [], "note": None}

    stalled = [dict(r, note=(f"{r['contacters']} buyers contacted and {r['negotiators']} negotiated, but none "
                             "agreed a deal in the window: check the price, the stock and how the seller answers."))
               for r in rows if r["contacters"] >= MIN_CONTACTS_WITHOUT_DEAL and r["deal_buyers"] == 0]
    stalled.sort(key=lambda r: (-r["contacters"], -r["negotiators"], r["listing_id"]))
    out["engaged_not_converting"] = stalled[:limit]

    contacted = [r for r in rows if r["contacters"] > 0]
    if len(contacted) >= MIN_BENCHMARK_LISTINGS:
        med_contacts = median(r["contacters"] for r in contacted) or 0.0
        threshold = max(MIN_UNUSUAL_CONTACTS, UNUSUAL_MULTIPLE * med_contacts)
        unusual = [dict(r, note=(f"{r['contacters']} buyers contacted it, {r['contacters'] / med_contacts:.1f} times "
                                 f"the median of {med_contacts:g} across {len(contacted)} contacted listings."))
                   for r in contacted if med_contacts and r["contacters"] >= threshold]
        unusual.sort(key=lambda r: (-r["contacters"], r["listing_id"]))
        out["unusual_interest"] = unusual[:limit]

    eligible = [r for r in rows if r["viewers"] >= MIN_VIEWERS]
    if len(eligible) < MIN_BENCHMARK_LISTINGS:
        out["note"] = f"Needs at least {MIN_BENCHMARK_LISTINGS} listings with {MIN_VIEWERS}+ viewers to benchmark contact rates."
        return out
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
    out.update(benchmark={"median_contact_rate_pct": med_rate, "median_viewers": med_viewers,
                          "listings_compared": len(eligible)},
               popular_not_converting=low[:limit], hidden_gems=gems[:limit])
    return out


def attention_changes(current: dict[str, dict], previous: dict[str, dict], meta: dict,
                      weights: dict[str, float], limit: int) -> dict[str, list[dict]]:
    """Trending (biggest score gain), growing (biggest relative gain on a real
    base) and losing attention (score at most half of a real base)."""
    trending, growing, losing = [], [], []
    for lid in sorted(set(current) | set(previous)):
        row = _row(lid, meta[lid], current.get(lid, {}), weights)
        before = _score(previous.get(lid), weights)[0]
        score = row["score"]
        entry = dict(row, previous_score=before, delta=round(score - before, 2), growth_pct=change_pct(score, before))
        if score >= MIN_TRENDING_SCORE and score > before:
            trending.append(entry)
        if before >= MIN_GROWTH_BASE and score > before:
            growing.append(entry)
        if before >= MIN_LOSING_BASE and score <= before * LOSING_SHARE:
            losing.append(dict(entry, note=f"Score {score:g}, down from {before:g} in the window before."))
    trending.sort(key=lambda r: (-r["delta"], r["listing_id"]))
    growing.sort(key=lambda r: (-(r["growth_pct"] or 0.0), -r["delta"], r["listing_id"]))
    losing.sort(key=lambda r: (r["delta"], r["listing_id"]))
    return {"trending": trending[:limit], "growing": growing[:limit], "losing_attention": losing[:limit]}


async def unanswered_buyers(db: AsyncSession, start: datetime, end: datetime) -> tuple[dict[str, dict], bool]:
    """listing id -> buyers whose conversation opened in [start, end) and whose
    first message the seller had not answered after REPLY_WINDOW, as of `end`.

    Only conversations whose first message ever is in the window: a later
    "thanks" in an older thread needs no answer, the rule the seller rating
    uses (domains/trust/response_time)."""
    from api.domains.trust.response_time import asks_the_seller

    staff = _staff()
    m = NegotiationMessage
    # visibility-ok: thread keys and first-ask times only, no content is selected
    asks = (await db.execute(
        select(m.listing_id, m.buyer_id, func.min(m.created_at))
        .where(m.created_at < end, m.listing_id.is_not(None),
               m.buyer_id.is_not(None), m.buyer_id.not_in(staff), asks_the_seller(m))
        .group_by(m.listing_id, m.buyer_id)
        .having(func.min(m.created_at) >= start)
        .limit(ROW_CAP + 1)
    )).all()
    # visibility-ok: thread keys and the seller's latest message time only, no content
    replies = (await db.execute(
        select(m.listing_id, m.buyer_id, func.max(m.created_at))
        .where(m.created_at >= start, m.created_at < end, m.role == "seller",
               m.listing_id.is_not(None), m.buyer_id.is_not(None))
        .group_by(m.listing_id, m.buyer_id).limit(ROW_CAP + 1)
    )).all()
    truncated = len(asks) > ROW_CAP or len(replies) > ROW_CAP
    last_reply = {(lid, bid): to_naive_utc(at) for lid, bid, at in replies[:ROW_CAP] if at is not None}
    out: dict[str, dict] = {}
    for lid, bid, first in asks[:ROW_CAP]:
        if first is None:
            continue
        first = to_naive_utc(first)
        reply = last_reply.get((lid, bid))
        if (reply is not None and reply > first) or end - first < REPLY_WINDOW:
            continue
        entry = out.setdefault(lid, {"waiting_buyers": 0, "oldest_wait_hours": 0.0})
        entry["waiting_buyers"] += 1
        entry["oldest_wait_hours"] = max(entry["oldest_wait_hours"], round((end - first).total_seconds() / 3600, 1))
    return out, truncated


async def rankings(
    db: AsyncSession, *, window_days: int = 7, limit: int = 10,
    category: Optional[str] = None, county: Optional[str] = None,
    start: Optional[datetime] = None, end: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> dict:
    now = now or datetime.utcnow()
    if start is None or end is None:
        end, start = now, now - timedelta(days=window_days)
    prev_start = start - (end - start)
    weights = trending_weights()
    eng, cut = await engagement(db, start, end)
    prev_eng, prev_cut = await engagement(db, prev_start, start, signals=SCORED)
    meta = await _listing_meta(db, _ids(eng) | _ids(prev_eng))
    current = _tally(eng, meta, category, county)
    previous = _tally(prev_eng, meta, category, county)
    rows = [_row(lid, meta[lid], sets, weights) for lid, sets in current.items()]

    converted = sorted((r for r in rows if r["deal_buyers"] or r["completed_buyers"]),
                       key=lambda r: (-r["deal_buyers"], -r["completed_buyers"], r["listing_id"]))
    changes = attention_changes(current, previous, meta, weights, limit)

    waiting, waiting_cut = await unanswered_buyers(db, start, end)
    # A conversation opened by Zeno's message to the seller has no buyer
    # contact row, so its listing may not be loaded yet.
    meta.update(await _listing_meta(db, set(waiting) - set(meta)))
    by_id = {r["listing_id"]: r for r in rows}
    needs_seller = []
    for lid, entry in waiting.items():
        if lid not in meta or not _matches(meta[lid], category, county):
            continue
        base = by_id.get(lid) or _row(lid, meta[lid], {}, weights)
        needs_seller.append(dict(base, **entry, note=(
            f"{entry['waiting_buyers']} buyer{'s' if entry['waiting_buyers'] != 1 else ''} waiting for the seller, "
            f"the longest {entry['oldest_wait_hours']:g} hours.")))
    needs_seller.sort(key=lambda r: (-r["waiting_buyers"], -r["oldest_wait_hours"], r["listing_id"]))

    return {
        "generated_at": iso(now),
        "window": _window(start, end),
        "category": category,
        "county": county,
        "truncated": cut or prev_cut or waiting_cut,
        "listings_engaged": len(rows),
        "weights": weights,
        "formula": _formula(weights),
        "rankings": {
            "viewed": _top(rows, "viewers", limit),
            "saved": _top(rows, "savers", limit),
            "liked": _top(rows, "likers", limit),
            "contacted": _top(rows, "contacters", limit),
            "negotiated": _top(rows, "negotiators", limit),
            "converted": converted[:limit],
            "trending": changes["trending"],
            "growing": changes["growing"],
        },
        "conversion": conversion_insights(rows, limit),
        "attention": {"losing_attention": changes["losing_attention"], "needs_seller": needs_seller[:limit]},
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
    weights = trending_weights()
    staff = _staff()
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
        .where(Deal.listing_id == listing_id, Deal.created_at >= start, Deal.created_at < now,
               Deal.buyer_id.not_in(staff))
        .group_by(Deal.status)
    )).all()
    deals = {getattr(s, "value", str(s)): int(n) for s, n in deal_rows}
    agreed = sum(n for s, n in deals.items() if s in {st.value for st in _AGREED_STATES})
    completed = int((await db.execute(
        select(func.count(Deal.id)).where(
            Deal.listing_id == listing_id, Deal.status == DealStatus.released,
            Deal.released_at >= start, Deal.released_at < now, Deal.buyer_id.not_in(staff))
    )).scalar() or 0)

    snapshots = (await db.execute(
        select(ListingMetricSnapshot).where(
            ListingMetricSnapshot.listing_id == listing_id,
            ListingMetricSnapshot.snapshot_date >= first_day)
        .order_by(ListingMetricSnapshot.snapshot_date)
    )).scalars().all()

    seller_name = (await db.execute(
        select(func.coalesce(User.business_display_name, User.name)).where(User.id == seller)
    )).scalar()

    # Where it stands among its category's engaged listings in the window.
    all_eng, all_cut = await engagement(db, start, now, signals=("views", "contacts"))
    meta = await _listing_meta(db, _ids(all_eng) | {listing_id})
    peers = [_row(lid, meta[lid], s, weights) for lid, s in _tally(all_eng, meta, listing.category).items()]
    own = _row(listing_id, {"name": listing.name, "category": listing.category,
                            "county": listing.location_county, "seller_id": seller}, sets, weights)
    by_viewers = sorted({r["viewers"] for r in peers} | {own["viewers"]}, reverse=True)
    peer_rates = [r["view_to_contact_pct"] for r in peers if r["viewers"] >= MIN_VIEWERS]

    created = listing.created_at
    return {
        "generated_at": iso(now),
        "window": {"from": iso(start), "to": iso(now), "days": window_days},
        "truncated": cut or all_cut,
        "listing": {
            "id": listing.id, "name": listing.name, "category": listing.category,
            # The county is the finest location shown: never coordinates.
            "county": listing.location_county, "seller_id": seller, "seller_name": seller_name,
            "price": float(listing.price),
            "status": getattr(listing.status, "value", str(listing.status)),
            "created_at": iso(created),
            "age_days": int((now - to_naive_utc(created)).days) if created else None,
            "lifetime_views_counter": int(listing.views or 0),
        },
        "totals": own,
        "funnel": {"viewers": own["viewers"], "savers_or_likers": len(sets["saves"] | sets["likes"]),
                   "contacters": own["contacters"], "negotiators": own["negotiators"],
                   "deals_agreed": agreed, "deals_completed": completed},
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
        "weights": weights,
        "definitions": {
            **DEFINITIONS,
            "lifetime_views_counter": ("The listing's own view counter since it was posted, guests and repeat "
                                       "views included. The daily series counts first views by signed-in accounts."),
            "snapshots": ("Cumulative counts the nightly job recorded; a day it did not run has no point, and "
                          "nothing is filled in for days before the job started."),
        },
    }


async def stage_totals(
    db: AsyncSession, start: datetime, end: datetime, *, category: Optional[str] = None,
    county: Optional[str] = None, group_by: Optional[str] = None,
) -> dict:
    """Each stage's distinct buyers summed over the listings engaged in
    [start, end): a buyer who viewed three listings counts three times, as
    three listings were each seen by one buyer. With `group_by` ("category"
    or "county") the same per group. For the report builder, which needs
    totals where rankings keep only the top listings."""
    eng, cut = await engagement(db, start, end)
    meta = await _listing_meta(db, _ids(eng))
    tallies = _tally(eng, meta, category, county)
    totals = {s: 0 for s in SIGNALS}
    groups: dict[str, dict[str, int]] = {}
    for lid, sets in tallies.items():
        counts = {s: len(sets[s]) for s in SIGNALS}
        for s in SIGNALS:
            totals[s] += counts[s]
        if group_by in ("category", "county"):
            label = (meta[lid][group_by] or "").strip() or ("Uncategorised" if group_by == "category" else "Unknown")
            entry = groups.setdefault(label, {"listings": 0, **{s: 0 for s in SIGNALS}})
            entry["listings"] += 1
            for s in SIGNALS:
                entry[s] += counts[s]
    return {"listings_engaged": len(tallies), "totals": totals, "groups": groups, "truncated": cut}


async def first_reply_minutes(db: AsyncSession, start: datetime, end: datetime) -> tuple[dict[str, float], bool]:
    """Average minutes to the seller's first reply per seller, for threads opened
    in [start, end): the rule the seller rating uses (domains/trust/response_time),
    with an unanswered first message charged up to 48 hours and at least 3
    conversations needed. Staff conversations are left out. Too many messages
    to read at once gives no figures rather than a partial average."""
    from api.domains.trust.response_time import compute_response_times, thread_rows_clause

    m = NegotiationMessage
    # visibility-ok: seller ids, thread keys, roles and times for reply timing; no content is selected
    rows = (await db.execute(
        select(Listing.seller_id, m.listing_id, m.buyer_id, m.role, m.recipient_role, m.created_at)
        .join(Listing, Listing.id == m.listing_id)
        .where(thread_rows_clause(start), m.created_at < end, m.buyer_id.not_in(_staff()))
        .order_by(m.listing_id, m.buyer_id, m.created_at)
        .limit(ROW_CAP + 1)
    )).all()
    if len(rows) > ROW_CAP:
        return {}, True
    return compute_response_times([tuple(r) for r in rows], end), False


async def _live_listings(db: AsyncSession, seller_ids: list[str], now: datetime) -> dict[str, int]:
    from api.domains.listings.paid import live_clause

    out: dict[str, int] = {}
    for chunk in _chunks(sorted(set(seller_ids)), 500):
        for sid, n in (await db.execute(
            select(Listing.seller_id, func.count(Listing.id)).where(
                Listing.seller_id.in_(chunk), Listing.status == ListingStatus.active, live_clause(now))
            .group_by(Listing.seller_id)
        )).all():
            out[sid] = int(n)
    return out


async def performance(
    db: AsyncSession, *, window_days: int = 30, limit: int = 10,
    start: Optional[datetime] = None, end: Optional[datetime] = None, now: Optional[datetime] = None,
) -> dict:
    """Sellers by the attention, contact and deals their listings earn, and
    buyers by how far they go from browsing to a completed deal."""
    now = now or datetime.utcnow()
    if start is None or end is None:
        end, start = now, now - timedelta(days=window_days)
    staff = _staff()
    eng, cut = await engagement(db, start, end)
    prev_eng, prev_cut = await engagement(db, start - (end - start), start, signals=("views", "contacts"))
    meta = await _listing_meta(db, _ids(eng) | _ids(prev_eng))
    tallies = _tally(eng, meta)

    sellers: dict[str, dict[str, Any]] = {}
    buyers: dict[str, dict[str, set]] = defaultdict(lambda: {s: set() for s in SIGNALS})
    for lid, sets in tallies.items():
        entry = sellers.setdefault(meta[lid]["seller_id"], {"listings": 0, **{s: set() for s in SIGNALS}})
        entry["listings"] += 1
        for signal in SIGNALS:
            entry[signal] |= sets[signal]
            for uid in sets[signal]:
                buyers[uid][signal].add(lid)
    before: dict[str, dict[str, set]] = defaultdict(lambda: {"views": set(), "contacts": set()})
    for lid, sets in _tally(prev_eng, meta).items():
        entry = before[meta[lid]["seller_id"]]
        entry["views"] |= sets["views"]
        entry["contacts"] |= sets["contacts"]

    agreed_states = list(_AGREED_STATES)

    async def deal_counts(owner, *conds) -> dict[str, int]:
        return {k: int(n) for k, n in (await db.execute(
            select(owner, func.count(Deal.id)).where(
                *conds, Deal.seller_id.not_in(staff), Deal.buyer_id.not_in(staff)).group_by(owner))).all()}

    agreed_in = (Deal.created_at >= start, Deal.created_at < end, Deal.status.in_(agreed_states))
    released_in = (Deal.released_at >= start, Deal.released_at < end, Deal.status == DealStatus.released)
    seller_deals = await deal_counts(Deal.seller_id, *agreed_in)
    seller_completed = await deal_counts(Deal.seller_id, *released_in)
    buyer_deals = await deal_counts(Deal.buyer_id, *agreed_in)
    buyer_completed = await deal_counts(Deal.buyer_id, *released_in)

    seller_rows = []
    for sid, e in sellers.items():
        prior = before.get(sid, {"views": set(), "contacts": set()})
        contacters = len(e["contacts"])
        seller_rows.append({
            "seller_id": sid, "listings_engaged": e["listings"], "viewers": len(e["views"]),
            "savers": len(e["saves"]), "contacters": contacters, "negotiators": len(e["negotiations"]),
            "contact_rate_pct": _rate(contacters, len(e["views"])),
            "deals_agreed": int(seller_deals.get(sid, 0)), "deals_completed": int(seller_completed.get(sid, 0)),
            "viewers_previous": len(prior["views"]), "contacters_previous": len(prior["contacts"]),
            "contacters_change_pct": change_pct(contacters, len(prior["contacts"])),
        })
    seller_rows.sort(key=lambda r: (-r["contacters"], -r["viewers"], r["seller_id"]))

    buyer_rows = [{
        "buyer_id": uid, "listings_viewed": len(e["views"]), "listings_saved": len(e["saves"]),
        "listings_contacted": len(e["contacts"]), "listings_negotiated": len(e["negotiations"]),
        "deals_agreed": int(buyer_deals.get(uid, 0)), "deals_completed": int(buyer_completed.get(uid, 0)),
    } for uid, e in buyers.items()]
    buyer_rows.sort(key=lambda r: (-r["listings_contacted"], -r["listings_viewed"], r["buyer_id"]))
    browsers = sum(1 for e in buyers.values() if e["views"] and not e["contacts"])

    def stage(*signals: str) -> int:
        return sum(1 for e in buyers.values() if any(e[s] for s in signals))

    viewed = stage("views")
    funnel = []
    for key, label, signals in (
        ("viewed", "Viewed a listing", ("views",)),
        ("saved_or_liked", "Saved or liked one", ("saves", "likes")),
        ("contacted", "Contacted a seller", ("contacts",)),
        ("negotiated", "Negotiated", ("negotiations",)),
        ("deal_agreed", "Agreed a deal", ("deals",)),
        ("deal_completed", "Completed a deal", ("completed",)),
    ):
        count = stage(*signals)
        funnel.append({"key": key, "label": label, "buyers": count, "pct_of_viewed": _rate(count, viewed)})

    top_sellers, top_buyers = seller_rows[:limit], buyer_rows[:limit]
    wanted = [r["seller_id"] for r in top_sellers] + [r["buyer_id"] for r in top_buyers]
    names = {}
    if wanted:
        names = dict((await db.execute(
            select(User.id, func.coalesce(User.business_display_name, User.name))
            .where(User.id.in_(wanted)))).all())
    live = await _live_listings(db, [r["seller_id"] for r in top_sellers], now)
    replies, replies_cut = await first_reply_minutes(db, start, end)
    for row in top_sellers:
        row["name"] = names.get(row["seller_id"])
        row["live_listings"] = int(live.get(row["seller_id"], 0))
        row["avg_first_reply_minutes"] = replies.get(row["seller_id"])
    for row in top_buyers:
        row["name"] = names.get(row["buyer_id"])
    return {
        "generated_at": iso(now),
        "window": _window(start, end),
        "truncated": cut or prev_cut,
        "response_time_truncated": replies_cut,
        "sellers": {"total": len(seller_rows), "top": top_sellers},
        "buyers": {"total": len(buyer_rows), "browsers_without_contact": browsers, "top": top_buyers},
        "buyer_funnel": funnel,
        "not_instrumented": NOT_INSTRUMENTED,
        "definitions": {
            **DEFINITIONS,
            "seller_contact_rate": "Distinct contacters / distinct viewers across the seller's listings.",
            "live_listings": "The seller's listings buyers can see now.",
            "avg_first_reply_minutes": ("Average minutes from a buyer's first message to the seller's first reply, "
                                        "for conversations opened in the window; an unanswered one counts up to 48 hours. "
                                        "Empty with fewer than 3 conversations. The seller rating uses the same rule."),
            "contacters_previous": "Contacters across the seller's listings in the window of equal length just before.",
            "browsers_without_contact": "Buyers who viewed at least one listing and contacted none in the window.",
            "buyer_funnel": ("Distinct buyers at each stage in the window. Stages need not nest: a buyer can "
                             "message without a recorded first view in the same window."),
        },
    }


async def demand(db: AsyncSession, *, window_days: int = 30, now: Optional[datetime] = None) -> dict:
    """Demand and supply by category and county, against the window before.

    Live supply is the state now. Only flows - demand, new listings, buyer
    conversations, deals - are compared with the previous window: BROKA keeps no
    history of which listings were live then."""
    now = now or datetime.utcnow()
    start = now - timedelta(days=window_days)
    current = await categories_between(db, start, now, now=now)
    previous = await categories_between(db, start - timedelta(days=window_days), start, now=now)
    before = {c["key"]: c for c in previous["categories"]}
    categories = []
    for c in current["categories"]:
        p = before.pop(c["key"], None) or {}
        categories.append({
            "key": c["key"], "label": c["label"], "signal": c["signal"], "recommendation": c["recommendation"],
            "supply_live": c["supply_live"], "demand": c["demand"],
            "demand_previous": int(p.get("demand", 0)), "demand_change_pct": change_pct(c["demand"], p.get("demand", 0)),
            "demand_per_listing": c["demand_per_listing"], "buyer_threads": c["buyer_threads"],
            "interests": c["interests"], "buy_agent_requests": c["buy_agent_requests"],
            "new_listings": c["new_listings"], "new_listings_previous": int(p.get("new_listings", 0)),
            "deals_agreed": c["deals_agreed"], "deals_agreed_previous": int(p.get("deals_agreed", 0)),
            "median_price_kes": c["median_price_kes"],
        })
    for p in before.values():
        if not p["demand"] and not p["new_listings"]:
            continue
        categories.append({
            "key": p["key"], "label": p["label"], "signal": "inactive",
            "recommendation": "No activity in this window after some in the one before.",
            "supply_live": 0, "demand": 0, "demand_previous": p["demand"], "demand_change_pct": change_pct(0, p["demand"]),
            "demand_per_listing": None, "buyer_threads": 0, "interests": 0, "buy_agent_requests": 0,
            "new_listings": 0, "new_listings_previous": p["new_listings"],
            "deals_agreed": 0, "deals_agreed_previous": p["deals_agreed"], "median_price_kes": None,
        })
    categories.sort(key=lambda c: (-c["demand"], -c["demand_previous"], c["label"]))

    def moves(rows: list[dict], key: str, base: str, rising: bool) -> list[str]:
        moved = [r for r in rows if r[base] >= MIN_DEMAND_FOR_GROWTH and r[key] is not None
                 and (r[key] > 0 if rising else r[key] < 0)]
        moved.sort(key=lambda r: ((-r[key]) if rising else r[key], r["label"]))
        return [r["label"] for r in moved[:5]]

    geo_now = await geography(db, window_days=window_days, now=now)
    geo_before = {c["key"]: c for c in (await geography(db, window_days=window_days, now=start))["counties"]}
    counties = []
    for c in geo_now["counties"]:
        p = geo_before.pop(c["key"], None) or {}
        counties.append({
            "key": c["key"], "label": c["label"], "supply_live": c["supply_live"], "stores": c["stores"],
            "buyer_threads": c["buyer_threads"], "buyer_threads_previous": int(p.get("buyer_threads", 0)),
            "buyer_threads_change_pct": change_pct(c["buyer_threads"], p.get("buyer_threads", 0)),
            "new_listings": c["new_listings"], "new_listings_previous": int(p.get("new_listings", 0)),
            "threads_per_listing": c["threads_per_listing"],
        })
    for p in geo_before.values():
        if not p["buyer_threads"] and not p["new_listings"]:
            continue
        counties.append({
            "key": p["key"], "label": p["label"], "supply_live": 0, "stores": 0,
            "buyer_threads": 0, "buyer_threads_previous": p["buyer_threads"],
            "buyer_threads_change_pct": change_pct(0, p["buyer_threads"]),
            "new_listings": 0, "new_listings_previous": p["new_listings"], "threads_per_listing": None,
        })
    counties.sort(key=lambda c: (-c["supply_live"], -c["buyer_threads"], c["label"]))

    return {
        "generated_at": iso(now),
        "window": _window(start, now),
        "categories": categories,
        "growth": {
            "rising": moves(categories, "demand_change_pct", "demand_previous", True),
            "falling": moves(categories, "demand_change_pct", "demand_previous", False),
        },
        "counties": counties,
        "definitions": {
            **current["definitions"],
            "demand_previous": "The same demand count in the window of equal length just before.",
            "growth": (f"Categories whose demand moved, among those with at least {MIN_DEMAND_FOR_GROWTH} "
                       "demand signals in the window before; a change from zero has no percentage."),
            "county": ("The listing's county. Buyer conversations are counted by where the listing is; buyers' "
                       "own locations are not used, and nothing finer than a county is shown."),
            "supply_live": "Listings buyers can see now. Only flows are compared with the window before.",
        },
    }
