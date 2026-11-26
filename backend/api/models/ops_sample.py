"""OpsSample - one process's operations counters for one stretch of minutes.

Written once a minute by every API process (api/core/ops_sampler.py), with
no lease: each process only knows the requests it answered, its own database
pool and the pushes and background tasks it ran. The admin operations
workspace and the reports add the processes' rows together.

Only counts, timings and fixed names: route templates ("GET
/listings/{listing_id}"), provider codes, task names. Never an id, a path
with its ids, a token or any text a user wrote. Rows older than
OPS_SAMPLE_RETENTION_DAYS are deleted by the sampler.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa

from api.database import Base


class OpsSample(Base):
    __tablename__ = "ops_samples"

    id = sa.Column(sa.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Host and pid (api/core/ops_metrics.INSTANCE_ID): which process wrote it.
    instance = sa.Column(sa.String(80), nullable=False)
    # Naive UTC, half-open [period_start, period_end), whole minutes.
    period_start = sa.Column(sa.DateTime, nullable=False)
    period_end = sa.Column(sa.DateTime, nullable=False)

    requests = sa.Column(sa.Integer, nullable=False, default=0)
    server_errors = sa.Column(sa.Integer, nullable=False, default=0)
    client_errors = sa.Column(sa.Integer, nullable=False, default=0)
    # Counts per bucket of request_metrics.LATENCY_BOUNDS_MS (and one past the
    # last edge), so percentiles over any window can be estimated by adding
    # rows up rather than averaging percentiles.
    latency_histogram = sa.Column(sa.JSON, nullable=False, default=list)
    p50_ms = sa.Column(sa.Float, nullable=True)
    p95_ms = sa.Column(sa.Float, nullable=True)
    p99_ms = sa.Column(sa.Float, nullable=True)

    # The pool at the moment of sampling: a gauge, not a maximum.
    db_checked_out = sa.Column(sa.Integer, nullable=True)
    db_max_connections = sa.Column(sa.Integer, nullable=True)

    # Since the last sample: {"db.queries": n, "push.failed.UNAVAILABLE": n, ...}
    counters = sa.Column(sa.JSON, nullable=False, default=dict)
    # As of the sample: {"queues": {"notifications": 0, ...}, "loops": {...}}
    gauges = sa.Column(sa.JSON, nullable=False, default=dict)
    # The busiest route templates of the period, with counts and a histogram each.
    endpoints = sa.Column(sa.JSON, nullable=False, default=list)

    created_at = sa.Column(sa.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        sa.Index("ix_ops_samples_period_start", "period_start"),
    )
