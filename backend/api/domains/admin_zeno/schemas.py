"""Typed contracts for administrative Zeno.

Every figure the model is allowed to discuss travels as an :class:`Evidence`
object built by a tool from an existing analytics service. The final answer
names evidence by ``metric_key``; the orchestrator rejects any reference it
did not actually supply, so a model cannot cite a figure it invented.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["measured", "derived", "estimated", "hypothesis", "unavailable"]
TurnRole = Literal["user", "assistant"]


class Period(BaseModel):
    """The window a metric describes, always reported in UTC."""

    model_config = ConfigDict(extra="forbid")

    start: Optional[datetime] = None
    end: Optional[datetime] = None
    timezone: str = "UTC"


class Evidence(BaseModel):
    """One quantitative finding, traceable to a named analytics source.

    ``available=False`` is how a metric that cannot be measured travels: the
    value is ``None`` and ``unavailable_reason`` says what is missing. A tool
    must never return zero for an unmeasured period.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=160)
    metric_key: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=160)
    value: Any = None
    unit: str = Field(default="count", max_length=40)
    previous_value: Any = None
    period: Period = Field(default_factory=Period)
    source: str = Field(min_length=1, max_length=120)
    definition: str = Field(default="", max_length=1200)
    generated_at: datetime
    available: bool = True
    unavailable_reason: Optional[str] = Field(default=None, max_length=800)
    confidence: Confidence = "measured"
    href: Optional[str] = Field(default=None, max_length=240)


class ToolInput(BaseModel):
    """Base for every tool's argument model: unknown keys are refused."""

    model_config = ConfigDict(extra="forbid")


class ToolOutput(BaseModel):
    """What a tool returns: evidence plus optional structured context.

    ``data`` is small and already aggregate: it may hold top-N names and
    counts, never message contents, phone numbers or other personal data.
    """

    model_config = ConfigDict(extra="forbid")

    evidence: list[Evidence] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)


class ChatContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: Optional[str] = Field(default=None, max_length=80)
    period_days: Optional[int] = Field(default=None, ge=1, le=90)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    conversation_id: Optional[str] = Field(default=None, max_length=64)
    message: str = Field(min_length=1, max_length=2000)
    context: Optional[ChatContext] = None


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str = Field(max_length=300)
    summary: str = Field(max_length=4000)


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(max_length=800)
    evidence: list[str] = Field(default_factory=list, max_length=12)
    confidence: Confidence = "measured"


class Interpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(max_length=800)


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(max_length=600)
    rationale: str = Field(default="", max_length=800)
    page: Optional[str] = Field(default=None, max_length=240)


class InvestigationMeta(BaseModel):
    """Safe metadata about how an answer was produced.

    Deliberately excludes the model's private reasoning: only the tool names
    an administrator could see on their own dashboard, the provider/model and
    timing.
    """

    model_config = ConfigDict(extra="forbid")

    tools_used: list[str] = Field(default_factory=list)
    tool_errors: list[str] = Field(default_factory=list)
    provider: Optional[str] = None
    model: Optional[str] = None
    latency_ms: int = 0
    synthesis: Literal["model", "fallback", "clarify", "no_evidence"] = "model"


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    answer: Answer
    findings: list[Finding] = Field(default_factory=list)
    possible_causes: list[Interpretation] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    related_pages: list[str] = Field(default_factory=list)
    suggested_questions: list[str] = Field(default_factory=list)
    generated_at: datetime
    investigation: InvestigationMeta = Field(default_factory=InvestigationMeta)


class ToolStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    domain: str
    permission: str


class StatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    model_gateway_ready: bool
    providers_configured: list[str]
    tool_registry_ready: bool
    tool_count: int
    last_error_category: Optional[str] = None
    generated_at: datetime


class BriefingFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=120)
    severity: Literal["critical", "high", "warning", "positive", "info"]
    title: str = Field(max_length=300)
    detail: str = Field(default="", max_length=1200)
    area: str = Field(default="business", max_length=60)
    evidence: list[str] = Field(default_factory=list, max_length=12)
    href: Optional[str] = Field(default=None, max_length=240)


class BriefingOpportunity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=300)
    detail: str = Field(default="", max_length=1200)
    href: Optional[str] = Field(default=None, max_length=240)


class BriefingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str = Field(max_length=300)
    executive_summary: str = Field(max_length=6000)
    priority_findings: list[BriefingFinding] = Field(default_factory=list)
    opportunities: list[BriefingOpportunity] = Field(default_factory=list)
    recommended_investigations: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    period: Period
    generated_at: datetime
    data_freshness: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    cached: bool = False


class ConversationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    turn_count: int = 0


class ConversationList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversations: list[ConversationSummary] = Field(default_factory=list)
    total: int = 0
    limit: int = 20
    offset: int = 0


class TurnOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    role: TurnRole
    content: str
    evidence: list[Evidence] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ConversationDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation: ConversationSummary
    turns: list[TurnOut] = Field(default_factory=list)
    limit: int = 40
    offset: int = 0
