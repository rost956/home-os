from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import AISettings
from .errors import (
    AIDisabledError,
    AIProtocolError,
    AIRequestTooLargeError,
    AITimeoutError,
    AIUnavailableError,
)
from .schemas import AIAvailability, AICompletionRequest, AICompletionResponse, parse_structured_response

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class AIClient(Protocol):
    async def complete(self, request: AICompletionRequest) -> AICompletionResponse: ...

    async def complete_json(self, request: AICompletionRequest, schema: type[SchemaT]) -> SchemaT: ...

    async def probe(self) -> AIAvailability: ...


class DisabledAIClient:
    async def complete(self, request: AICompletionRequest) -> AICompletionResponse:
        del request
        raise AIDisabledError("Home AI is disabled")

    async def complete_json(self, request: AICompletionRequest, schema: type[SchemaT]) -> SchemaT:
        del request, schema
        raise AIDisabledError("Home AI is disabled")

    async def probe(self) -> AIAvailability:
        return AIAvailability(state="disabled")


class LlamaCppClient:
    """Small OpenAI-compatible client for a locally managed llama-server."""

    def __init__(self, settings: AISettings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not settings.enabled or not settings.base_url or not settings.model:
            raise ValueError("LlamaCppClient requires enabled AI settings")
        self.settings = settings
        self.transport = transport

    async def complete(self, request: AICompletionRequest) -> AICompletionResponse:
        prompt_size = sum(len(message.content) for message in request.messages)
        if prompt_size > self.settings.context_budget:
            raise AIRequestTooLargeError("AI request exceeds the configured context budget")

        payload: dict[str, object] = {
            "model": self.settings.model,
            "messages": [message.model_dump() for message in request.messages],
            "max_tokens": min(request.max_tokens, self.settings.max_tokens),
            "temperature": request.temperature,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": self.settings.enable_thinking},
        }
        if request.output_mode == "json":
            payload["response_format"] = {"type": "json_object"}
        response_payload = await self._request("POST", "chat/completions", json=payload)
        try:
            choice = response_payload["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("message content is not a string")
            return AICompletionResponse(
                content=content,
                model=response_payload.get("model"),
                finish_reason=choice.get("finish_reason"),
            )
        except (IndexError, KeyError, TypeError, ValidationError) as exc:
            raise AIProtocolError("AI returned an unexpected completion response") from exc

    async def complete_json(self, request: AICompletionRequest, schema: type[SchemaT]) -> SchemaT:
        response = await self.complete(request.model_copy(update={"output_mode": "json"}))
        return parse_structured_response(response.content, schema)

    async def probe(self) -> AIAvailability:
        try:
            await self._request("GET", "models")
        except (AITimeoutError, AIUnavailableError, AIProtocolError) as exc:
            return AIAvailability(state="unavailable", detail=exc.__class__.__name__)
        return AIAvailability(state="available")

    async def _request(self, method: str, path: str, **kwargs: object) -> dict:
        timeout = httpx.Timeout(
            connect=self.settings.connect_timeout_seconds,
            read=self.settings.read_timeout_seconds,
            write=self.settings.read_timeout_seconds,
            pool=self.settings.connect_timeout_seconds,
        )
        headers = {"Accept": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        url = f"{self.settings.base_url}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport, headers=headers) as client:
                response = await client.request(method, url, **kwargs)
                response.raise_for_status()
                payload = response.json()
        except httpx.TimeoutException as exc:
            raise AITimeoutError("Local AI backend timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise AIUnavailableError(f"Local AI backend returned HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise AIUnavailableError("Local AI backend is unavailable") from exc
        except ValueError as exc:
            raise AIProtocolError("Local AI backend returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AIProtocolError("Local AI backend returned an unexpected JSON value")
        return payload


class FakeAIClient:
    """Deterministic client for tests; it never contacts a model server."""

    def __init__(
        self,
        responses: Sequence[AICompletionResponse | Exception] = (),
        availability: AIAvailability | None = None,
    ) -> None:
        self.responses = deque(responses)
        self.availability = availability or AIAvailability(state="available")
        self.requests: list[AICompletionRequest] = []

    async def complete(self, request: AICompletionRequest) -> AICompletionResponse:
        self.requests.append(request)
        if not self.responses:
            raise AIProtocolError("Fake AI client has no queued response")
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    async def complete_json(self, request: AICompletionRequest, schema: type[SchemaT]) -> SchemaT:
        response = await self.complete(request.model_copy(update={"output_mode": "json"}))
        return parse_structured_response(response.content, schema)

    async def probe(self) -> AIAvailability:
        return self.availability
