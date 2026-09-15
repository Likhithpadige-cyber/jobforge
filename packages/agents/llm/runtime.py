"""Small structured-call boundary for B1, using the documented LLM_MODE modes.

Replay is cassette-only, with no fallback. Prices are supplied explicitly for the
configured model; no hard-coded pricing or guessed cost. Transport is injectable
for offline tests. Prompts are not cached. B1 records validated, prose-free
selections or a null schema-failure marker, plus accounting metadata.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as SchemaError

from packages.agents.base.pii import assert_prompt_safe
from packages.shared.config import LLMMode, Settings
from packages.shared.errors import DependencyUnavailableError, ValidationError


class ModelOutputError(ValidationError):
    """A response is not valid structured output."""


class ReplayMissError(DependencyUnavailableError):
    """No exact request cassette exists; replay never calls the network."""


class CostLimitError(ValidationError):
    """The call would exceed, or has exceeded, its run budget."""


class ModelCallError(DependencyUnavailableError):
    """A provider call failed without exposing its response or credentials."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True)
class Request:
    model: str
    system: str
    user: str
    schema: dict[str, Any]
    prompt_version: str
    max_tokens: int = 4096

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical(self.__dict__).encode()).hexdigest()


class Response(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    body: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=0, allow_inf_nan=False)
    stop_reason: str


@dataclass(frozen=True)
class Prices:
    model: str
    input_per_million: Decimal
    output_per_million: Decimal

    def __post_init__(self) -> None:
        if any(
            not p.is_finite() or p < 0
            for p in (self.input_per_million, self.output_per_million)
        ):
            raise ValidationError("Invalid model prices")

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        return (
            input_tokens * self.input_per_million
            + output_tokens * self.output_per_million
        ) / Decimal(1_000_000)


class Transport(Protocol):
    async def __call__(self, request: Request) -> dict[str, Any]: ...


class AnthropicTransport:
    """One bounded tool-use call; no implicit retries or non-LLM requests."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def __call__(self, request: Request) -> dict[str, Any]:
        if not self.api_key:
            raise ModelCallError("LLM API key is not configured")
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                result = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": request.model,
                        "max_tokens": request.max_tokens,
                        "temperature": 0,
                        "system": request.system,
                        "messages": [{"role": "user", "content": request.user}],
                        "tools": [
                            {
                                "name": "select_content",
                                "description": "Select fact IDs only",
                                "input_schema": request.schema,
                            }
                        ],
                        "tool_choice": {"type": "tool", "name": "select_content"},
                    },
                )
                result.raise_for_status()
                return result.json()
        except (httpx.HTTPError, ValueError):
            raise ModelCallError("LLM provider request failed") from None


class StructuredClient:
    def __init__(
        self,
        settings: Settings,
        *,
        cassette_dir: Path = Path("tests/fixtures/cassettes"),
        prices: Prices | None = None,
        transport: Transport | None = None,
        model: str | None = None,
    ):
        self.mode = settings.llm_mode
        # Defaults to the tailoring model (B1/B2/B3, unchanged). B5's
        # independent verifier call passes settings.llm_model_verification
        # explicitly instead.
        self.model = model or settings.llm_model_tailoring
        self.max_cost = Decimal(str(settings.llm_max_cost_usd_per_run))
        self.cassette_dir = cassette_dir
        self.prices = prices
        self.transport = transport or AnthropicTransport(settings.anthropic_api_key)
        if not self.max_cost.is_finite() or self.max_cost <= 0:
            raise CostLimitError("A positive finite run budget is required")

    async def complete(
        self, request: Request, *, spent: Decimal = Decimal(0)
    ) -> Response:
        # Check even replay/repair requests: bypassing the prompt builder must
        # never allow residual PII to reach an injected or real transport.
        assert_prompt_safe(request.system, request.user, canonical(request.schema))
        if request.model != self.model:
            raise ModelCallError(
                "Request model does not match configured tailoring model"
            )
        if spent >= self.max_cost:
            raise CostLimitError("Run budget exhausted")
        if self.mode == LLMMode.REPLAY:
            try:
                return Response.model_validate_json(
                    (self.cassette_dir / f"{request.digest}.json").read_text()
                )
            except FileNotFoundError:
                raise ReplayMissError("No cassette for the exact B1 request") from None
            except (SchemaError, ValueError):
                raise ModelOutputError("Malformed replay cassette") from None
        if self.prices is None or self.prices.model != request.model:
            raise ModelCallError(
                "Explicit prices for the configured model are required"
            )
        # UTF-8 bytes are a conservative input-token estimate; reserve output
        # ceiling too. Fail before contacting the provider when budget is short.
        upper_input = len(canonical(request.__dict__).encode()) + 4096
        if spent + self.prices.cost(upper_input, request.max_tokens) > self.max_cost:
            raise CostLimitError("Insufficient budget for the bounded B1 call")
        raw = await self.transport(request)
        # Validate usage separately from model content. Once usable usage is
        # available, always return it so B1 audits/budgets the charged attempt
        # before rejecting malformed content (including missing tool input).
        try:
            usage = raw["usage"]
            input_tokens, output_tokens = usage["input_tokens"], usage["output_tokens"]
            if (
                type(input_tokens) is not int
                or type(output_tokens) is not int
                or input_tokens < 0
                or output_tokens < 0
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ModelOutputError("Malformed provider usage metadata") from None

        body = "null"  # Safe invalid-output marker, never rejected model prose.
        stop_reason = raw.get("stop_reason")
        if not isinstance(stop_reason, str):
            stop_reason = "invalid_response"
        try:
            blocks = raw.get("content")
            if (
                isinstance(blocks, list)
                and len(blocks) == 1
                and isinstance(blocks[0], dict)
                and blocks[0].get("type") == "tool_use"
                and blocks[0].get("name") == "select_content"
            ):
                body = canonical(blocks[0]["input"])
        except (KeyError, TypeError, ValueError):
            pass
        return Response(
            body=body,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=self.prices.cost(input_tokens, output_tokens),
            stop_reason=stop_reason,
        )

    def record(self, request: Request, response: Response) -> None:
        """Cache validated selections or a prose-free schema-failure marker."""
        if self.mode == LLMMode.RECORD:
            self.cassette_dir.mkdir(parents=True, exist_ok=True)
            (self.cassette_dir / f"{request.digest}.json").write_text(
                response.model_dump_json()
            )
