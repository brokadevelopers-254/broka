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

## Data protection

These records describe how people use the app, which is personal data under
Kenya's Data Protection Act 2019. Activity is kept to timestamps and durations,
retained for the periods above, readable only by administrators with the
permissions named here, and never used to act on an account automatically.
Systematic monitoring of users and suspension decisions are the kind of
processing the Act asks a data protection impact assessment for; complete one
before relying on these figures in production, and record suspensions'
evidence references, not the evidence itself.
