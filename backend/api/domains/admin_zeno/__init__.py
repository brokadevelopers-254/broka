"""Administrative Zeno - Jarvis Mode (Phase 1).

A dedicated, read-only intelligence domain for administrators. It is
deliberately independent of ``zeno_assistant`` (the customer-facing assistant)
except for shared AI-provider and cost infrastructure: administrative prompts,
tools, permissions, conversations and usage accounting must never mix with a
buyer's or seller's Zeno.

Entry points:
    router.py            the /admin/zeno API
    orchestrator.py      plan -> execute -> synthesize
    tool_registry.py     the closed set of approved read-only tools
    model_gateway.py     the administrative model adapter
    briefing_service.py  the deterministic intelligence briefing
"""
