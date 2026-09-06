from __future__ import annotations

import json
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import AIResponseValidationError

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class AIMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=16_000)


class AICompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[AIMessage] = Field(min_length=1, max_length=20)
    max_tokens: int = Field(default=512, ge=16, le=2048)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    output_mode: Literal["text", "json"] = "text"


class AICompletionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=16_000)
    model: str | None = Field(default=None, max_length=200)
    finish_reason: str | None = Field(default=None, max_length=100)


class AIJsonMessage(BaseModel):
    """Minimal structured response used to exercise schema validation in phase 1."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=4_000)
    data: dict[str, Any] = Field(default_factory=dict)


class AIAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["disabled", "available", "unavailable"]
    detail: str | None = Field(default=None, max_length=200)


class AIHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["disabled", "available", "unavailable"]
    model: str | None = Field(default=None, max_length=200)


class ExpenseCategorySelection(BaseModel):
    """Bounded LLM output for a category proposal; never a write command."""

    model_config = ConfigDict(extra="forbid")

    category_id: int | None = Field(default=None, gt=0)
    confidence: float = Field(ge=0.0, le=1.0)
    ambiguous: bool = False


def parse_structured_response(content: str, schema: type[SchemaT]) -> SchemaT:
    try:
        payload = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise AIResponseValidationError("AI returned invalid JSON") from exc
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise AIResponseValidationError("AI response does not match the expected schema") from exc
