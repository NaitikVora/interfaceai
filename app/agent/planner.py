"""LLM planner: turns an observation into one strict ``Decision``.

Provider-agnostic: any OpenAI-compatible chat-completions endpoint works (``LLM_BASE_URL``).
Malformed responses are fed back with the validation error and retried a bounded number of times;
transport errors are retried a bounded number of times; anything else raises ``PlannerError``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import openai
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)
from openai.types.shared_params import ResponseFormatJSONObject
from pydantic import ValidationError

from app.agent.models import Decision
from app.agent.prompts import SYSTEM_PROMPT, build_retry_feedback, build_user_prompt
from app.config import Settings

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
TRANSPORT_RETRIES = 2


class PlannerError(RuntimeError):
    """The planner could not produce a valid decision."""


@dataclass
class PlannerCall:
    """Evidence of one model call (prompt text is redacted before being stored)."""

    step: int
    model: str
    prompt_chars: int
    attempts: int
    raw_response: str
    decision: dict[str, Any] | None
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None


class Planner(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def calls(self) -> list[PlannerCall]: ...

    async def decide(self, *, step: int, goal: str, observation_text: str) -> Decision: ...


def parse_decision(raw: str) -> Decision:
    """Parse model output into a ``Decision``; tolerant of code fences, strict about content."""
    cleaned = _FENCE_RE.sub("", raw.strip())
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"response is not valid JSON ({exc.msg} at char {exc.pos})") from exc
    if not isinstance(payload, dict):
        raise ValueError("response must be a single JSON object")
    try:
        return Decision.model_validate(payload)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'root'}: {e['msg']}" for e in exc.errors()
        )
        raise ValueError(f"decision failed validation: {problems}") from exc


class OpenAICompatiblePlanner:
    def __init__(self, settings: Settings) -> None:
        if not settings.llm_configured:
            raise PlannerError("LLM_API_KEY is not configured; discovery needs a model")
        assert settings.llm_api_key is not None
        self._client = AsyncOpenAI(
            api_key=settings.llm_api_key.get_secret_value(),
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_s,
            max_retries=0,
        )
        self._settings = settings
        self._calls: list[PlannerCall] = []

    @property
    def model_name(self) -> str:
        return self._settings.llm_model

    @property
    def calls(self) -> list[PlannerCall]:
        return self._calls

    async def decide(self, *, step: int, goal: str, observation_text: str) -> Decision:
        messages: list[ChatCompletionMessageParam] = [
            ChatCompletionSystemMessageParam(role="system", content=SYSTEM_PROMPT),
            ChatCompletionUserMessageParam(
                role="user", content=build_user_prompt(goal, observation_text)
            ),
        ]
        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
        record = PlannerCall(
            step=step,
            model=self.model_name,
            prompt_chars=prompt_chars,
            attempts=0,
            raw_response="",
            decision=None,
        )
        self._calls.append(record)
        last_error = "no response"
        for _ in range(self._settings.llm_max_parse_retries):
            record.attempts += 1
            raw, usage = await self._complete(messages)
            record.raw_response = raw
            for key, value in usage.items():
                record.usage[key] = record.usage.get(key, 0) + value
            try:
                decision = parse_decision(raw)
            except ValueError as exc:
                last_error = str(exc)
                messages.append(ChatCompletionAssistantMessageParam(role="assistant", content=raw))
                messages.append(
                    ChatCompletionUserMessageParam(
                        role="user", content=build_retry_feedback(last_error)
                    )
                )
                continue
            record.decision = decision.model_dump(mode="json", exclude_none=True)
            return decision
        record.error = last_error
        raise PlannerError(
            f"model returned no valid decision after {record.attempts} attempts: {last_error}"
        )

    async def _complete(
        self, messages: list[ChatCompletionMessageParam]
    ) -> tuple[str, dict[str, int]]:
        last: Exception | None = None
        for _ in range(TRANSPORT_RETRIES + 1):
            try:
                response = await self._client.chat.completions.create(
                    model=self._settings.llm_model,
                    messages=messages,
                    temperature=self._settings.llm_temperature,
                    max_completion_tokens=self._settings.llm_max_output_tokens,
                    response_format=ResponseFormatJSONObject(type="json_object"),
                )
            except (
                openai.APITimeoutError,
                openai.APIConnectionError,
                openai.RateLimitError,
            ) as exc:
                last = exc
                continue
            except openai.APIStatusError as exc:
                raise PlannerError(f"LLM request failed: {exc.status_code} {exc.message}") from exc
            choice = response.choices[0] if response.choices else None
            content = (choice.message.content if choice and choice.message else None) or ""
            usage: dict[str, int] = {}
            if response.usage is not None:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens or 0,
                    "completion_tokens": response.usage.completion_tokens or 0,
                }
            return content, usage
        raise PlannerError(f"LLM unreachable after {TRANSPORT_RETRIES + 1} attempts: {last}")
