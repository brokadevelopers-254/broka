# Admin intelligence: activity, account status and the Command Center

What the admin dashboard measures about people and the platform, how each
figure is defined, and where the code is. The dashboard is
`brokadevelopers-254/broka-admin`; every figure it shows comes from the
endpoints below.

## Signed-in sessions and foreground activity

Code: `backend/api/core/activity.py`, `backend/api/models/user_activity.py`,
`PATCH /auth/heartbeat` (`backend/api/domains/auth/router.py`).

- **Session**: opened by every sign-in that issues tokens (password, SMS code,
  sign-up, password change or reset). The access token carries its id as
  `sid`; a refresh keeps it. A token issued before sessions existed gets one
  the first time it is refreshed.
- **Foreground time**: the app sends a heartbeat about every 60 seconds while
  it is in front and stops when it goes to the background
  (`flutter_app/lib/main.dart`). Each heartbeat credits the seconds since the
  previous one, at most one write per 30 seconds per session. A gap over 150
  seconds earns nothing. A heartbeat sent with `{"foreground": false}` is not
  counted. Other API requests never count: the app polls in the background.
- **Session end**: logout (`logout`), sign-out everywhere or a password change
  (`revoked`), a suspension (`suspended`), or five minutes without a heartbeat
  (`inactive`), closed by the one-minute sweep at the last heartbeat, so the
  silence is not counted. Only an `inactive` session can be reopened, by a
  later heartbeat, again without crediting the gap.
- **Several devices**: one session per signed-in device. An account is active
  when any of its sessions is.

| Figure | Definition |
|---|---|
| Active now | Accounts with an open session whose last foreground heartbeat is under 5 minutes old |
| Active sessions | Open sessions with a heartbeat under 5 minutes old |
| Active in last 5/15/60 min | Accounts with a foreground heartbeat in that span |
| Peak concurrent, 24 h | The highest one-minute sample of active accounts; a peak between samples can be missed |
| Active on a day | A `user_activity_days` row exists: a credited heartbeat that UTC day |
| DAU / WAU / MAU | Accounts active on the last UTC day of the range / any of the 7 / any of the 30 days ending on it |
| Active share | Accounts active in the range divided by accounts registered before it ends, × 100 |
| Session duration | Foreground seconds of sessions that have ended; open sessions are excluded |
| New vs returning | Returning: active in the range and active before it; new: first active inside it |
| Retention day N | Share of a registration day's accounts active again exactly N days later; null until day N has passed, and for cohorts registered before tracking began |

Days before tracking began are `null` in every series (`active_users` in
`GET /admin/analytics/timeseries` included), never zero. Responses say when
tracking began (`coverage_started_on`).

**Retention of the records** (`backend/api/core/config.py`): sessions 180 days
(`ACTIVITY_SESSION_RETENTION_DAYS`), daily rows 400 days
(`ACTIVITY_DAY_RETENTION_DAYS`), one-minute samples 45 days
(`ACTIVITY_SAMPLE_RETENTION_DAYS`). The sweep deletes older rows. No record holds
contact details, device identifiers, IP addresses, message or call content.

**Admin reads**: `GET /admin/activity/live`, `/engagement`, `/retention`
(ADMIN_SUMMARY) and `/admin/activity/users`, `/users/{id}` (MANAGE_USERS).

## Account status: restriction and suspension

Code: `backend/api/core/account_status.py`, `backend/api/domains/admin/moderation.py`,
`backend/api/models/account_moderation.py`.

| Status | Effect |
|---|---|
| `active` | None |
| `restricted` | Can sign in; loses creating listings, finalising deals, boosting and buying verification (the set a trust score under 20 removes) |
| `suspended` | Sign-in, code sign-in, password reset and token refresh are refused. Every HTTP and WebSocket request with the account's access token is refused with 403 by `AccountStatusMiddleware`. Applying it ends the account's sessions, revokes its refresh tokens and unregisters its push devices |

A suspension lasts 24 hours, 7 days, 30 days or is permanent. A temporary one
reads as ended from `suspended_until` on, and the sweep writes the expiry to
the history. Flagging (`POST /admin/users/{id}/flag`) is separate: it records
a concern and blocks nothing. Scam reports' protective restrictions
(`backend/api/domains/scam_reports/`) are separate again: scoped, always
expiring, and applied from a report.

Rules for administrators (`MANAGE_ACCOUNT_STATUS`): a reason is required; you
cannot change your own account; an administrator's account can only be changed
after they are demoted; each change names the `expected_version` it was based
on, and a stale one is refused with 409. Every change writes an
`account_moderation_events` row (append-only by convention: nothing updates or
deletes them) and an audit row.

Known limits:
- Each process caches an account's status for 10 seconds; the process that
  applies a change forgets it at once, the others within that window.
- A WebSocket already open when a suspension lands stays open until it
  reconnects; the new handshake is refused.
- Call tokens already issued (5 minutes) are not revoked by a suspension.
- If the status cannot be read, the request is refused: a database outage
  answers the app's `DB_UNAVAILABLE` 503, any other error the app's 500.

## The Command Center

`GET /admin/command-center?window_days=7` (ADMIN_SUMMARY), built in
`backend/api/core/command_center.py`. One read gives five groups of figures,
the decisions they call for, and the marketplace pulse the page's other
sections use.

- **Periods**: current `[now - window_days, now)`, previous the same length
  just before it, as in `GET /admin/intelligence/pulse`, whose figures are
  reused rather than recounted.
- **Each figure** carries its value, the previous value, `delta` (value minus
  previous, percentage points for a percent), `change_pct` (relative, for
  counts and money only), its kind (`period`, `point_in_time`, `live`), the
  direction that is good, a definition, and a drill-down link.
- **Point-in-time figures** (live listings, flagged accounts, open disputes,
  open fraud events) have no previous value where no history is kept.
  Registered accounts, listings posted and suspended accounts are rebuilt as of
  the period's start from creation times and the moderation history.
- **Unavailable figures** say why: API availability needs an external uptime
  monitor; activity figures wait until tracking covers the whole period;
  message delivery rows are kept 60 days.
- **API error rate and latency** come from `backend/api/core/request_metrics.py`:
  the requests the answering process served in the last 15 minutes against the
  15 before, kept in memory. Each replica counts its own, and the counts reset
  on restart.
- **Scheduled work** is the age of the newest one-minute presence sample, written
  by whichever replica holds the scheduler lease: over three minutes means the
  scheduled sweeps are not running anywhere.
- **Messages on the recipient's phone** counts only rows the recipient's app
  confirmed (DELIVERED or READ). Firebase accepting a push is not delivery.

**Decisions** are the founder insights (`backend/api/core/insights.py`) plus
operational signals, ordered by severity: scheduled work stalled (critical),
payment failures at 20% of finished attempts or up 10 points (3 failures at
least), payment prompts unanswered after 15 minutes, active accounts down 20%
(from 10 at least), and API server errors at 5% of 50 requests or more. Each
names its evidence and where to look; none changes an account, a payment or a
setting.

## Marketplace and listing intelligence

Code: `backend/api/core/market_intel.py` (rankings, conversion, attention,
listing detail, demand, seller and buyer performance) and
`backend/api/core/market_models.py` (anomalies, forecasts, churn, supply and
payment models). Routes under `/admin/intelligence/market/` (ADMIN_SUMMARY).

**Stages a buyer reaches on a listing**, each counted once per buyer in the
window, with administrators and the listing's own seller left out:

| Stage | Counted from |
|---|---|
| Viewed | `listing_views`: one row per account and listing, the first time it opened it, so repeat opens and guests are never counted |
| Saved, liked | `wishlists`, `listing_likes` |
| Contacted | the buyer's own chat message on the listing, or an `interests` row |
| Negotiated | the seller wrote in the buyer's thread (the conversation went both ways), or the buyer made a price offer (`interests.offer_price`) |
| Deal agreed | a deal on the listing created in the window and now past negotiation, not cancelled |
| Deal completed | the escrow was released to the seller (after the buyer confirmed delivery) in the window |

A negotiation is not a sale, and contact is engagement, not conversion: the
rankings keep *popularity* (viewed, saved, liked, contacted) apart from
*conversion* (contact-to-deal rate, deals, completions).

**Score**, behind trending, growing and losing attention:

    score = 1 x viewers + 2 x savers + 2 x likers + 4 x contacters
          + 6 x negotiators + 10 x buyers who agreed a deal

All counts are distinct users in the window. The weights are configurable with
`MARKET_TRENDING_WEIGHTS` (for example `views=1,saves=2,likes=2,contacts=4,negotiations=6,deals=10`);
an unknown stage, a negative or a non-number is ignored and logged. Every
ranking response returns the weights in force and the formula. Completions
are reported beside the score, not in it: a release can come weeks after the
attention that led to it.

| List | Rule |
|---|---|
| Trending | score at least 3 and higher than in the window of equal length before; ordered by the gain |
| Growing | relative gain, only for listings that scored at least 5 before (a rise from almost nothing is not growth) |
| Losing attention | scored at least 10 before, at most half of that now |
| Popular, not converting | at least 5 viewers and at most half the median contact rate (needs 4 such listings to benchmark) |
| Hidden gems | fewer viewers than the median, at least 2 contacts, at least twice the median contact rate |
| Talked about, not bought | at least 3 buyers contacted and none agreed a deal |
| Unusual interest | contacters at least 3 times the median across contacted listings (needs 4 of them), and at least 3 |
| Needs the seller | a buyer whose conversation opened in the window has waited over 24 hours for the seller's first reply; Zeno answering does not count |

Ties are broken by listing id, so the same data always gives the same order.
Windows: the last 24 hours, 7, 30 or 90 days, or a custom range of up to 90
days (its end is held at the present). Filters: category and county.

**Listing detail** shows each stage, the deals by status, distinct users per
day, and the nightly snapshots (`listing_metric_snapshots`, cumulative views,
likes and interested buyers). A day the snapshot job did not run has no point;
nothing is filled in. The county is the finest location shown.

**Demand** (`/market/demand`): by category, buyer conversations + interests +
Buying Agent requests, against live listings now, with the previous window's
demand, new listings and deals beside it. Live supply is the state now: no
history of which listings were live is kept, so only flows are compared. By
county: buyer conversations on listings in that county; buyers' own locations
are not used.

**Sellers and buyers** (`/market/performance`): per seller, live listings,
viewers, contacters (with the previous window), negotiators, deals agreed and
completed, and the average first-reply time, measured as in the seller
rating. Per buyer, how far they went, and a funnel of distinct buyers per
stage. Searches are not recorded, so nothing measures search.

## Operations

`GET /admin/operations?window_hours=1..336` (`api/core/operations.py`,
`ADMIN_SUMMARY`) answers whether BROKA is working, and where not, for the
window against the one before.

**Samples.** Every API process writes one `ops_samples` row a minute
(`api/core/ops_sampler.py`), with no lease, because each process only knows
what it answered and ran. A row holds whole minutes: requests, 4xx and 5xx,
a latency histogram (edges in `request_metrics.LATENCY_BOUNDS_MS`), the 25
busiest route templates, database statement and pool counters, push results,
queued-job counts, and each background loop's last pass. The counters live in
`api/core/ops_metrics.py` between samples; a row that cannot be saved puts
them back. Rows are deleted after `OPS_SAMPLE_RETENTION_DAYS` (14).

- Latency percentiles over a window are estimated from the histograms added
  together, never averaged from per-minute percentiles. Streams (`/stream`)
  are counted but not timed.
- Endpoints are route templates (`GET /listings/{listing_id}`). A path with
  its ids is never stored.
- Pool use is checked out over the pool's maximum at the moment of each sample;
  saturations count every time the pool filled.
- A background loop is stale when it has not finished a pass for three of its
  intervals. Every task of the five-minute sweep counts its own failure.
- Before the first sample, every figure built on samples is unavailable with
  the reason, never zero.

**From rows, for any window:** payments by provider and status (M-Pesa
intents and ZetuPay) with the success rate, attempts pending over 15 minutes,
ZetuPay's webhook delay and the failure reasons (digits masked); chat message
delivery (`message_deliveries`); call outcomes from the call cards.

**Not measured, and said so:** call connection delay, M-Pesa callback delay
(no callback time is stored), device receipt for any push but a chat message,
a waiting-connections gauge, and uptime, which needs an outside monitor. FCM
accepting a push is never shown as delivery.

## Report builder

`/admin/report-builder` (`api/core/report_builder.py`, `api/models/report_builder.py`).
The daily, weekly and monthly founder briefs (`admin_reports.py`) are separate
and unchanged.

**A spec** chooses categories (platform growth, user engagement, marketplace,
listing performance, seller performance, revenue and payments, fraud and
moderation, system health, and an overall preset) or single metrics, a range
of whole UTC days (fixed dates or the last N days, at most 366), a comparison
(the period before, a year earlier, or none), category and county filters, a
grouping (by day, 7 days, month, category or county) and a visualization.

**Figures** come from the module that already defines each one, so a report
cannot disagree with the page the figure comes from. A figure that cannot be
measured for the period (activity before tracking began, system health before
sampling began, delivery older than its retention) is unavailable with the
reason and left out of the summary, the changes and the observations.
Point-in-time figures are as of generation and never compared. Filters apply
to listing performance and the category and county breakdowns only.

**A report** holds an executive summary, the figures with their comparison,
charts for grouped figures (a day nobody measured is a gap, never a zero),
supporting tables, the important changes (20% or more on a base of 10, or 5
points for a rate), the days that stood out against the same weekday
(`market_models.evaluate`, at most the last 92 days), observations that each
name the figure and source behind them, and every figure's definition and
source.

**Runs** are stored with the spec they were built from. Regenerating writes a
new run that points at the old one, which stays. A run downloads as a PDF
(`api/core/report_pdf.py`, ReportLab: A4, numbered pages, charts) and, in the
dashboard, as CSV, both from the stored payload. Every generation,
regeneration and change to a saved report writes an audit row.

**Schedules.** A saved report can run daily, weekly, monthly or every N days
at an hour (UTC). The five-minute sweep runs what is due, once: a run missed
while the service was down is made once, not once per missed slot, and a
failed build is stored as a failed run and raises an admin alert. Reports go
by email only to administrators with an address, chosen by id, and only with
`SEND_ADMIN_EMAIL`: a plain-text summary of the figures, with a link when
`ADMIN_DASHBOARD_URL` is set. No attachment, no personal data. Scheduled runs
follow `ADMIN_REPORTS_ENABLED` with the founder briefs.

## Data protection

These records describe how people use the app, which is personal data under
Kenya's Data Protection Act 2019. Activity is kept to timestamps and durations,
retained for the periods above, readable only by administrators with the
permissions named here, and never used to act on an account automatically.
Systematic monitoring of users and suspension decisions are the kind of
processing the Act asks a data protection impact assessment for; complete one
before relying on these figures in production, and record suspensions'
evidence references, not the evidence itself.
