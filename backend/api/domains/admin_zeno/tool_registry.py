"""The closed registry of approved, read-only intelligence tools.

Jarvis never sees a Python function, a SQL string or a table name. The model
chooses a *name* from this registry; the registry validates the arguments
against the tool's typed schema, checks the administrator's permission in the
backend, and runs only that tool's handler.

Consequences that are tested:
- an unknown name is rejected before anything is executed;
- an unknown argument is rejected by ``extra="forbid"``;
- a tool whose permission the administrator lacks cannot be executed even if
  the model asks for it (prompt text is not an authorisation);
- a slow tool is cancelled by a timeout and a failing tool cannot abort the
  rest of the turn;
- tools are executed sequentially on the request's session, so no two
  statements share an ``AsyncSession`` concurrently.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Type

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.permissions import Permission, get_user_permissions
from api.database import User
from api.domains.admin_zeno.schemas import Evidence, ToolInput, ToolOutput

log = logging.getLogger(__name__)

# A tool may not be given more argument data than this. The model builds
# arguments from a bounded prompt, so anything larger is a malfunction or an
# attempt to smuggle a payload into a handler.
MAX_ARGUMENT_BYTES = 4096

# A tool that has not answered in this many seconds is cancelled. Every handler
# wraps an existing analytics function with its own bounded queries; the
# timeout is the backstop that keeps one slow query from holding the turn.
DEFAULT_TOOL_TIMEOUT_SECONDS = 8.0


class ToolError(Exception):
    """Base class for a refused or failed tool execution."""

    category = "tool_error"


class UnknownToolError(ToolError):
    category = "unknown_tool"


class InvalidArgumentsError(ToolError):
    category = "invalid_arguments"


class ToolPermissionError(ToolError):
    category = "permission_denied"


class ToolTimeoutError(ToolError):
    category = "timeout"


class ToolExecutionError(ToolError):
    category = "execution_error"


ToolHandler = Callable[[AsyncSession, BaseModel], Awaitable[ToolOutput]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    domain: str
    input_model: Type[BaseModel]
    permission: Permission
    handler: ToolHandler
    timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    output_schema: str = "ToolOutput"
    evidence_source: str = ""
    data_availability: str = ""

    def prompt_spec(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        props = schema.get("properties", {})
        arguments = {}
        for key, spec in props.items():
            arguments[key] = {
                "type": spec.get("type") or spec.get("anyOf", [{}])[0].get("type", "string"),
                "description": spec.get("description", ""),
                "default": spec.get("default"),
            }
            if "enum" in spec:
                arguments[key]["enum"] = spec["enum"]
            if "minimum" in spec:
                arguments[key]["minimum"] = spec["minimum"]
            if "maximum" in spec:
                arguments[key]["maximum"] = spec["maximum"]
        return {
            "name": self.name,
            "description": self.description,
            "domain": self.domain,
            "arguments": arguments,
            "data_availability": self.data_availability,
        }


@dataclass
class ToolExecution:
    name: str
    ok: bool
    evidence: list[Evidence] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    error_category: Optional[str] = None
    error_message: Optional[str] = None
    duration_ms: int = 0


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"tool {definition.name!r} is already registered")
        self._tools[definition.name] = definition

    def register_all(self, definitions: list[ToolDefinition]) -> None:
        for definition in definitions:
            self.register(definition)

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(f"tool {name!r} is not registered") from None

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[ToolDefinition]:
        return [self._tools[name] for name in self.names()]

    def permitted(self, actor: User) -> list[ToolDefinition]:
        effective = get_user_permissions(actor)
        return [t for t in self.all() if t.permission in effective]

    def prompt_specs(self, actor: User) -> list[dict[str, Any]]:
        return [t.prompt_spec() for t in self.permitted(actor)]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        db: AsyncSession,
        actor: User,
    ) -> ToolExecution:
        """Validate and run one registered tool. Never raises ToolError."""
        started = time.monotonic()
        try:
            definition = self._authorise(name, actor)
            if not isinstance(arguments, dict):
                raise InvalidArgumentsError("arguments must be an object")
            encoded = json.dumps(arguments, default=str)
            if len(encoded.encode("utf-8")) > MAX_ARGUMENT_BYTES:
                raise InvalidArgumentsError("arguments exceed the permitted size")
            try:
                parsed = definition.input_model.model_validate(arguments)
            except ValidationError as exc:
                fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
                raise InvalidArgumentsError(
                    "invalid arguments: " + ", ".join(fields[:8])
                ) from None

            try:
                output = await asyncio.wait_for(
                    definition.handler(db, parsed), timeout=definition.timeout_seconds
                )
            except asyncio.TimeoutError:
                raise ToolTimeoutError(
                    f"tool {name!r} exceeded {definition.timeout_seconds:g}s"
                ) from None
            if not isinstance(output, ToolOutput):
                raise ToolExecutionError("tool returned an unexpected result type")
            return ToolExecution(
                name=name,
                ok=True,
                evidence=output.evidence,
                data=output.data,
                limitations=output.limitations,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except ToolError as exc:
            log.warning("[admin_zeno] tool refused name=%s category=%s", name, exc.category)
            return ToolExecution(
                name=name,
                ok=False,
                error_category=exc.category,
                # Safe message only: no SQL, no provider payload, no traceback.
                error_message=str(exc)[:300],
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # a handler failure must not abort the turn
            log.warning(
                "[admin_zeno] tool failed name=%s error=%s", name, type(exc).__name__
            )
            return ToolExecution(
                name=name,
                ok=False,
                error_category="execution_error",
                error_message=f"{type(exc).__name__} while running the tool",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

    def _authorise(self, name: str, actor: User) -> ToolDefinition:
        definition = self.get(name)
        if definition.permission not in get_user_permissions(actor):
            # Checked here, not only in the prompt: model output is untrusted.
            raise ToolPermissionError(
                f"missing permission {definition.permission.value} for tool {name!r}"
            )
        return definition


_default: Optional[ToolRegistry] = None


def default_registry() -> ToolRegistry:
    """The process-wide registry, populated once from the tool modules."""
    global _default
    if _default is None:
        from api.domains.admin_zeno.tools import (  # local import avoids a cycle
            business,
            finance,
            growth,
            marketplace,
            operations,
            platform,
            trust,
            zeno,
        )

        registry = ToolRegistry()
        for module in (
            business, marketplace, operations, finance,
            trust, growth, zeno, platform,
        ):
            module.register(registry)
        _default = registry
    return _default


# Re-exported so a tool module needs one import for its common pieces.
__all__ = [
    "ToolDefinition",
    "ToolRegistry",
    "ToolExecution",
    "ToolInput",
    "ToolOutput",
    "ToolError",
    "UnknownToolError",
    "InvalidArgumentsError",
    "ToolPermissionError",
    "ToolTimeoutError",
    "ToolExecutionError",
    "default_registry",
]
