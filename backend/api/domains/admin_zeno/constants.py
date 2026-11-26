"""Central execution limits for administrative Zeno.

Every value is deliberately conservative. The orchestrator refuses to plan
more than one bounded investigation per turn, so a single question cannot
cause an unbounded query storm even if the model misbehaves.
"""
from __future__ import annotations

import os

from api.core.config import settings

# One administrator turn may execute at most this many approved tools.
MAX_TOOL_EXECUTIONS_PER_TURN = 4

# Planning + synthesis. One extra stage is allowed only for a bounded
# format repair; there is no retry loop beyond that.
MAX_MODEL_STAGES = 3
MAX_REPAIR_ATTEMPTS = 1

# Whole-turn ceiling. A tool timeout is shorter than this so a slow tool is
# cancelled and the answer can still be synthesized from what returned.
TOTAL_ORCHESTRATION_TIMEOUT_SECONDS = float(
    os.getenv("ADMIN_ZENO_TOTAL_TIMEOUT_SECONDS", "45")
)
MODEL_CALL_TIMEOUT_SECONDS = float(
    os.getenv("ADMIN_ZENO_MODEL_TIMEOUT_SECONDS", "25")
)

# Bounded prompt and conversation context.
MAX_CONTEXT_TURNS = int(os.getenv("ADMIN_ZENO_CONTEXT_TURNS", "6"))
MAX_CONTEXT_CHARS_PER_TURN = 1200
MAX_EVIDENCE_FOR_MODEL = 40
MAX_MODEL_OUTPUT_CHARS = 16000

# Retention and pagination.
CONVERSATION_RETENTION_DAYS = 30
MAX_CONVERSATION_LIST_LIMIT = 50
DEFAULT_CONVERSATION_LIST_LIMIT = 20
MAX_CONVERSATION_TURNS = 40

# Briefing freshness: briefings are deterministic and cheap relative to a
# model call, but not free - one page load must not run every analytics
# service. A short per-process TTL is enough for a dashboard refresh; the
# response states when it was generated so a reader can tell.
BRIEFING_CACHE_SECONDS = float(os.getenv("ADMIN_ZENO_BRIEFING_CACHE_SECONDS", "60"))

# Rate limit: per administrator, separate from every customer-facing bucket.
ADMIN_ZENO_RATE_LIMIT_PER_MINUTE = settings.admin_zeno_rate_limit_per_minute


def is_enabled() -> bool:
    return bool(settings.admin_zeno_enabled)
