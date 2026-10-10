"""DeepSeek (OpenAI-compatible) LLM client with reasoning-model handling.

Empirically verified against the temporary endpoint:
- Models are REASONING models: responses split `reasoning_content` from
  `content`, and tokens go to reasoning FIRST. `max_tokens` must be generous or
  `content` comes back empty with finish_reason=length. We default high.
- Streaming deltas carry `reasoning_content` and `content` as separate fields ->
  emitted as REASONING_DELTA vs TEXT_MESSAGE_DELTA.
- Tool calling works (finish_reason=tool_calls, valid JSON args). In streaming
  mode tool_calls arrive fragmented across deltas (index + partial arguments);
  we reassemble them.

Usage from every call is fed to the CostController.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

import httpx

from dswarm.core.cost import CostController
from dswarm.core.event_bus import EventBus
from dswarm.core.events import Event, EventType
from dswarm.core.usage_journal import UsageCall, UsageContext, UsageWriter
from dswarm.solver.endpoint_probe import normalize_wire_api

DEFAULT_BASE_URL = os.environ.get("DSWARM_DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")


def classify_llm_exception(exc: BaseException) -> dict[str, Any]:
    """Map an LLM client exception to a stable diagnostic code and detail."""
    status = None
    text = str(exc) or type(exc).__name__
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else None
    lowered = text.lower()
    if status == 401 or "401" in lowered or "unauthorized" in lowered or "illegal header" in lowered:
        code = "http_401" if "illegal header" not in lowered else "missing_api_key"
        detail = "Planner endpoint returned 401; check API key or relay auth mode."
        if code == "missing_api_key":
            detail = "Planner API key is missing; select a credential account or set DSWARM_DEEPSEEK_API_KEY."
    elif status == 403 or "403" in lowered or "forbidden" in lowered:
        code = "http_403"
        detail = "Planner endpoint returned 403; the key is not allowed to use this relay/model."
    elif status == 404 or "model" in lowered and ("not found" in lowered or "does not exist" in lowered):
        code = "model_not_found"
        detail = "Planner model was not accepted by the relay; choose a tested model."
    elif isinstance(exc, (httpx.TimeoutException, TimeoutError)) or "timeout" in lowered or "timed out" in lowered:
        code = "timeout"
        detail = "Planner endpoint timed out."
    elif isinstance(exc, httpx.InvalidURL):
        code = "invalid_base_url"
        detail = "Planner Base URL is invalid."
    else:
        code = "network_error"
        detail = text[:240]
    return {"code": code, "detail": detail, "status": status, "raw": text[:240]}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _raise_for_status(r: httpx.Response) -> None:
    """Like httpx.raise_for_status but includes the response body — the API's
    error message (e.g. which field is malformed) is otherwise swallowed."""
    if r.status_code < 400:
        return
    body = r.text[:2000]
    raise httpx.HTTPStatusError(
        f"{r.status_code} from {r.request.url}: {body}",
        request=r.request,
        response=r,
    )


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string as returned by the model

    def parsed_args(self) -> dict[str, Any]:
        try:
            return json.loads(self.arguments) if self.arguments else {}
        except json.JSONDecodeError:
            return {}


@dataclass
class LLMResponse:
    content: str
    reasoning: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class ModelSpec:
    """A configured model 'persona' in the worker roster."""

    solver_id: str
    model: str
    temperature: float = 0.4
    max_tokens: int = 8000
    label: str = ""
    role: str = ""  # strategic-prior key; "" = generalist (no preamble)


class LLMClient:
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        bus: Optional[EventBus] = None,
        cost: Optional[CostController] = None,
        timeout: float = 180.0,
        overall_timeout: float = 300.0,
        trust_env: Optional[bool] = None,
        auth_mode: str = "bearer",
        auth_header: Optional[str] = None,
        auth_prefix: Optional[str] = None,
        wire_api: str = "openai-chat",
        reasoning_effort: Optional[str] = None,
        usage_writer: UsageWriter | None = None,
        usage_context: UsageContext | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("DSWARM_DEEPSEEK_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.reasoning_effort = str(reasoning_effort or "").strip().lower() or None
        self.bus = bus
        self.cost = cost
        self.usage_writer = usage_writer
        self.usage_context = usage_context
        self.auth_mode = (auth_mode or "bearer").strip().lower()
        if self.auth_mode not in {"bearer", "x-api-key", "custom"}:
            self.auth_mode = "bearer"
        self.auth_header = (
            auth_header or ("x-api-key" if self.auth_mode == "x-api-key" else "Authorization")
        ).strip()
        if auth_prefix is not None:
            self.auth_prefix = auth_prefix.strip()
        elif self.auth_mode == "x-api-key":
            self.auth_prefix = ""
        else:
            self.auth_prefix = "Bearer"
        self.wire_api = normalize_wire_api(wire_api)
        if self.wire_api == "auto":
            self.wire_api = "openai-chat"
        # Native Anthropic uses x-api-key. Keep an explicit custom auth
        # configuration intact for compatible relays, but make the protocol
        # safe by default when callers only select wire_api="anthropic".
        if (
            self.wire_api == "anthropic"
            and self.auth_mode == "bearer"
            and self.auth_header == "Authorization"
            and self.auth_prefix == "Bearer"
        ):
            self.auth_mode = "x-api-key"
            self.auth_header = "x-api-key"
            self.auth_prefix = ""
        # Keep tests and local evals isolated from desktop-wide proxy settings.
        # Users who really need a proxy for the LLM API can opt in explicitly.
        self.trust_env = _env_bool("DSWARM_LLM_TRUST_ENV", False) if trust_env is None else trust_env
        # explicit per-phase timeouts: a stalled stream between bytes must error,
        # not hang. `read` bounds the gap between streamed chunks.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=20.0, read=timeout),
            trust_env=self.trust_env,
        )
        # hard wall-clock ceiling on a single chat() — even a slow-trickle stream
        # that evades the per-read timeout cannot wedge a solver past this.
        self.overall_timeout = overall_timeout

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def _headers(self) -> dict[str, str]:
        value = f"{self.auth_prefix} {self.api_key}".strip() if self.auth_prefix else self.api_key
        headers = {
            self.auth_header: value,
            "Content-Type": "application/json",
        }
        if self.wire_api == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        return headers

    @staticmethod
    def _anthropic_message_content(content: Any) -> Any:
        if content is None:
            return ""
        if isinstance(content, (str, list)):
            return content
        return str(content)

    @classmethod
    def _anthropic_request_body(cls, body: dict[str, Any]) -> dict[str, Any]:
        """Translate the internal OpenAI-shaped turn into Messages API JSON."""
        system: list[Any] = []
        messages: list[dict[str, Any]] = []
        for message in body.get("messages") or []:
            role = str(message.get("role") or "user")
            content = cls._anthropic_message_content(message.get("content"))
            if role == "system":
                if isinstance(content, list):
                    system.extend(content)
                elif content:
                    system.append({"type": "text", "text": content})
                continue
            if role == "tool":
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": str(message.get("tool_call_id") or ""),
                        "content": content,
                    }],
                })
                continue
            if role == "assistant" and message.get("tool_calls"):
                blocks: list[dict[str, Any]] = []
                if isinstance(content, str) and content:
                    blocks.append({"type": "text", "text": content})
                for tool_call in message.get("tool_calls") or []:
                    function = tool_call.get("function") or {}
                    try:
                        tool_input = json.loads(function.get("arguments") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        tool_input = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": str(tool_call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "input": tool_input,
                    })
                content = blocks
            messages.append({"role": "assistant" if role == "assistant" else "user", "content": content})
        result: dict[str, Any] = {
            "model": body["model"],
            # max_tokens is required by the native Messages API. Keep the
            # OpenAI None semantics (provider chooses its cap) bounded here.
            "max_tokens": body.get("max_tokens") if body.get("max_tokens") is not None else 8192,
            "messages": messages,
        }
        if system:
            result["system"] = system
        if body.get("temperature") is not None:
            result["temperature"] = body["temperature"]
        if body.get("stream") is not None:
            result["stream"] = bool(body["stream"])
        tools = body.get("tools") or []
        if tools:
            result["tools"] = [{
                "name": str((tool.get("function") or {}).get("name") or tool.get("name") or ""),
                "description": str((tool.get("function") or {}).get("description") or tool.get("description") or ""),
                "input_schema": (tool.get("function") or {}).get("parameters") or tool.get("input_schema") or {"type": "object"},
            } for tool in tools]
        return result

    @staticmethod
    def _anthropic_response(data: dict[str, Any], *, model: str) -> LLMResponse:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                content_parts.append(str(block.get("text") or ""))
            elif kind in {"thinking", "reasoning"}:
                reasoning_parts.append(str(block.get("thinking") or block.get("text") or ""))
            elif kind == "tool_use":
                tool_calls.append(ToolCall(
                    id=str(block.get("id") or ""), name=str(block.get("name") or ""),
                    arguments=json.dumps(block.get("input") or {}, ensure_ascii=False),
                ))
        usage = data.get("usage") or {}
        stop_reason = str(data.get("stop_reason") or "")
        if stop_reason == "tool_use":
            stop_reason = "tool_calls"
        return LLMResponse(
            content="".join(content_parts), reasoning="".join(reasoning_parts),
            tool_calls=tool_calls, finish_reason=stop_reason, model=model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )

    async def _emit(self, etype: EventType, *, run_id, challenge_id, solver_id, **payload):
        if self.bus is not None and run_id is not None:
            await self.bus.emit(
                Event(
                    event_type=etype,
                    run_id=run_id,
                    challenge_id=challenge_id,
                    solver_id=solver_id,
                    payload=payload,
                )
            )

    async def _record_cost(self, model, usage, run_id, challenge_id, solver_id):
        if self.cost is not None and run_id is not None and usage:
            await self.cost.record(
                model=model,
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                run_id=run_id,
                challenge_id=challenge_id,
                solver_id=solver_id,
            )

    def _usage_context_for(
        self,
        *,
        run_id: str | None,
        challenge_id: str | None,
        solver_id: str | None,
    ) -> UsageContext | None:
        base = self.usage_context
        effective_run_id = run_id or (base.run_id if base else None)
        if not effective_run_id:
            return None
        return UsageContext(
            run_id=effective_run_id,
            challenge_id=challenge_id if challenge_id is not None else (base.challenge_id if base else None),
            worker_instance_id=base.worker_instance_id if base else None,
            solver_id=solver_id if solver_id is not None else (base.solver_id if base else None),
            profile_id=base.profile_id if base else None,
            configured_account_id=base.configured_account_id if base else None,
            billing_account_id=base.billing_account_id if base else None,
            producer=base.producer if base else "internal",
        )

    @staticmethod
    def _usage_outcome(exc: BaseException) -> str:
        if isinstance(exc, asyncio.CancelledError):
            return "cancelled"
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
            return "timeout"
        if isinstance(exc, httpx.HTTPStatusError):
            return "provider_error"
        return "transport_error"

    async def _finish_usage(
        self,
        call: UsageCall,
        *,
        outcome: str,
        response: LLMResponse | None = None,
    ) -> None:
        if self.usage_writer is None:
            return
        usage: dict[str, Any] = {}
        if response is not None and (response.input_tokens or response.output_tokens):
            usage = {
                "prompt_tokens": response.input_tokens,
                "completion_tokens": response.output_tokens,
            }
        status = "measured" if usage else "unknown"
        await self.usage_writer.finish(
            call,
            call_outcome=outcome,
            usage_status=status,
            usage=usage,
        )

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.4,
        max_tokens: Optional[int] = 8000,
        stream: bool = True,
        run_id: Optional[str] = None,
        challenge_id: Optional[str] = None,
        solver_id: Optional[str] = None,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        # max_tokens=None → omit the cap entirely (let the API use the model's own
        # maximum). Critical for reasoning models (deepseek-v4-pro): tokens go to
        # reasoning_content FIRST, so a small cap can be fully consumed by thinking
        # and truncate the actual answer. The Reason planner relies on this.
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        usage_call: UsageCall | None = None
        if self.usage_writer is not None:
            usage_call = await self.usage_writer.start(
                context=self._usage_context_for(
                    run_id=run_id, challenge_id=challenge_id, solver_id=solver_id
                )
            )
        coro = (
            self._chat_stream(
                body, run_id=run_id, challenge_id=challenge_id, solver_id=solver_id,
                record_cost=usage_call is None,
            )
            if stream
            else self._chat_once(
                body, run_id=run_id, challenge_id=challenge_id, solver_id=solver_id,
                record_cost=usage_call is None,
            )
        )
        # hard wall-clock guard: a stalled/half-open SSE stream must not wedge the
        # caller forever. On timeout we surface it as a normal error the solver
        # loop can recover from (treated like an empty turn), not a hang.
        try:
            resp = await asyncio.wait_for(coro, timeout=self.overall_timeout)
        except asyncio.TimeoutError:
            resp = LLMResponse(
                content="", reasoning="", tool_calls=[],
                finish_reason="timeout", model=body["model"],
            )
            if usage_call is not None:
                await self._finish_usage(usage_call, outcome="timeout", response=resp)
            return resp
        except BaseException as exc:
            if usage_call is not None:
                await self._finish_usage(
                    usage_call, outcome=self._usage_outcome(exc), response=None
                )
            raise
        if usage_call is not None:
            await self._finish_usage(usage_call, outcome="succeeded", response=resp)
        return resp

    async def _chat_once(self, body, *, run_id, challenge_id, solver_id, record_cost: bool = True) -> LLMResponse:
        if self.wire_api == "anthropic":
            return await self._anthropic_once(body, run_id=run_id, challenge_id=challenge_id, solver_id=solver_id, record_cost=record_cost)
        body = {**body, "stream": False}
        if body.get("stream") is True:  # safety
            body["stream"] = False
        r = await self._client.post(
            f"{self.base_url}/chat/completions", headers=self._headers(), json=body
        )
        _raise_for_status(r)
        data = r.json()
        choice = data["choices"][0]
        msg = choice["message"]
        usage = data.get("usage", {})
        if record_cost:
            await self._record_cost(body["model"], usage, run_id, challenge_id, solver_id)
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc["function"]
            tool_calls.append(
                ToolCall(id=tc.get("id", ""), name=fn["name"], arguments=fn.get("arguments", ""))
            )
        reasoning = msg.get("reasoning_content") or ""
        content = msg.get("content") or ""
        if reasoning:
            await self._emit(
                EventType.REASONING_DELTA,
                run_id=run_id, challenge_id=challenge_id, solver_id=solver_id, text=reasoning,
            )
        if content:
            await self._emit(
                EventType.TEXT_MESSAGE_DELTA,
                run_id=run_id, challenge_id=challenge_id, solver_id=solver_id, text=content,
            )
        return LLMResponse(
            content=content,
            reasoning=reasoning,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason", ""),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=body["model"],
        )

    async def _anthropic_once(self, body, *, run_id, challenge_id, solver_id, record_cost: bool = True) -> LLMResponse:
        request_body = self._anthropic_request_body({**body, "stream": False})
        r = await self._client.post(
            f"{self.base_url}/messages", headers=self._headers(), json=request_body
        )
        _raise_for_status(r)
        response = self._anthropic_response(r.json(), model=body["model"])
        if record_cost:
            await self._record_cost(body["model"], {
                "prompt_tokens": response.input_tokens,
                "completion_tokens": response.output_tokens,
            }, run_id, challenge_id, solver_id)
        if response.reasoning:
            await self._emit(EventType.REASONING_DELTA, run_id=run_id, challenge_id=challenge_id, solver_id=solver_id, text=response.reasoning)
        if response.content:
            await self._emit(EventType.TEXT_MESSAGE_DELTA, run_id=run_id, challenge_id=challenge_id, solver_id=solver_id, text=response.content)
        return response

    async def _anthropic_stream(self, body, *, run_id, challenge_id, solver_id, record_cost: bool = True) -> LLMResponse:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict[str, str]] = {}
        finish_reason = ""
        usage: dict[str, int] = {}
        request_body = self._anthropic_request_body({**body, "stream": True})
        async with self._client.stream("POST", f"{self.base_url}/messages", headers=self._headers(), json=request_body) as r:
            if r.status_code >= 400:
                await r.aread()
                _raise_for_status(r)
            async for line in r.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                try:
                    data = json.loads(line[len("data:"):].strip())
                except json.JSONDecodeError:
                    continue
                event = data.get("type")
                if event == "message_start":
                    usage.update({k: int(v or 0) for k, v in (data.get("message", {}).get("usage") or {}).items() if k in {"input_tokens", "output_tokens"}})
                elif event == "content_block_start":
                    block = data.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        idx = int(data.get("index") or 0)
                        tool_acc[idx] = {"id": str(block.get("id") or ""), "name": str(block.get("name") or ""), "arguments": ""}
                elif event == "content_block_delta":
                    idx = int(data.get("index") or 0)
                    delta = data.get("delta") or {}
                    kind = delta.get("type")
                    if kind == "text_delta":
                        text = str(delta.get("text") or "")
                        content_parts.append(text)
                        await self._emit(EventType.TEXT_MESSAGE_DELTA, run_id=run_id, challenge_id=challenge_id, solver_id=solver_id, text=text)
                    elif kind == "thinking_delta":
                        text = str(delta.get("thinking") or "")
                        reasoning_parts.append(text)
                        await self._emit(EventType.REASONING_DELTA, run_id=run_id, challenge_id=challenge_id, solver_id=solver_id, text=text)
                    elif kind == "input_json_delta":
                        tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})["arguments"] += str(delta.get("partial_json") or "")
                elif event == "message_delta":
                    finish_reason = str((data.get("delta") or {}).get("stop_reason") or finish_reason)
                    usage.update({k: int(v or 0) for k, v in (data.get("usage") or {}).items() if k in {"input_tokens", "output_tokens"}})
        if finish_reason == "tool_use":
            finish_reason = "tool_calls"
        response = LLMResponse(
            content="".join(content_parts), reasoning="".join(reasoning_parts),
            tool_calls=[ToolCall(id=v["id"], name=v["name"], arguments=v["arguments"]) for _, v in sorted(tool_acc.items()) if v.get("name")],
            finish_reason=finish_reason, input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0), model=body["model"],
        )
        if record_cost:
            await self._record_cost(body["model"], {"prompt_tokens": response.input_tokens, "completion_tokens": response.output_tokens}, run_id, challenge_id, solver_id)
        return response

    async def _chat_stream(self, body, *, run_id, challenge_id, solver_id, record_cost: bool = True) -> LLMResponse:
        if self.wire_api == "anthropic":
            return await self._anthropic_stream(body, run_id=run_id, challenge_id=challenge_id, solver_id=solver_id, record_cost=record_cost)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        # tool calls reassembled by index
        tc_acc: dict[int, dict[str, str]] = {}
        finish_reason = ""
        usage: dict[str, int] = {}

        async with self._client.stream(
            "POST", f"{self.base_url}/chat/completions", headers=self._headers(), json=body
        ) as r:
            if r.status_code >= 400:
                await r.aread()
                _raise_for_status(r)
            async for line in r.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[len("data:") :].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                ch = choices[0]
                delta = ch.get("delta") or {}
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]

                rc = delta.get("reasoning_content")
                if rc:
                    reasoning_parts.append(rc)
                    await self._emit(
                        EventType.REASONING_DELTA,
                        run_id=run_id, challenge_id=challenge_id, solver_id=solver_id, text=rc,
                    )
                cc = delta.get("content")
                if cc:
                    content_parts.append(cc)
                    await self._emit(
                        EventType.TEXT_MESSAGE_DELTA,
                        run_id=run_id, challenge_id=challenge_id, solver_id=solver_id, text=cc,
                    )
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = tc_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]

        if record_cost:
            await self._record_cost(body["model"], usage, run_id, challenge_id, solver_id)

        tool_calls = [
            ToolCall(id=v["id"], name=v["name"], arguments=v["arguments"])
            for _, v in sorted(tc_acc.items())
            if v["name"]
        ]
        return LLMResponse(
            content="".join(content_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=body["model"],
        )

    async def iter_chat_deltas(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.3,
        max_tokens: Optional[int] = 4000,
        run_id: Optional[str] = None,
        challenge_id: Optional[str] = None,
        solver_id: Optional[str] = None,
        emit: bool = False,
        record_cost: bool = False,
    ) -> "AsyncIterator[str]":
        """Stream content deltas and optionally record one internal provider call.

        With an injected ``UsageWriter`` and ``record_cost=True`` the journal
        started row is fsynced before the request, and a terminal row is written
        for success, provider/transport failure, timeout, or cancellation.
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        usage: dict[str, int] = {}
        usage_call: UsageCall | None = None
        if self.usage_writer is not None and record_cost:
            usage_call = await self.usage_writer.start(
                context=self._usage_context_for(
                    run_id=run_id, challenge_id=challenge_id, solver_id=solver_id
                )
            )
        request_body = self._anthropic_request_body(body) if self.wire_api == "anthropic" else body
        request_path = "messages" if self.wire_api == "anthropic" else "chat/completions"
        try:
            async with self._client.stream(
                "POST", f"{self.base_url}/{request_path}",
                headers=self._headers(), json=request_body,
            ) as r:
                if r.status_code >= 400:
                    await r.aread()
                    _raise_for_status(r)
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if self.wire_api == "anthropic":
                        event = chunk.get("type")
                        if event == "message_start":
                            start_usage = (chunk.get("message") or {}).get("usage") or {}
                            usage = {
                                "prompt_tokens": int(start_usage.get("input_tokens") or 0),
                                "completion_tokens": int(start_usage.get("output_tokens") or 0),
                            }
                        elif event == "content_block_delta":
                            delta = chunk.get("delta") or {}
                            if delta.get("type") == "thinking_delta":
                                text = str(delta.get("thinking") or "")
                                if emit and text:
                                    await self._emit(EventType.REASONING_DELTA, run_id=run_id, challenge_id=challenge_id, solver_id=solver_id, text=text)
                            elif delta.get("type") == "text_delta":
                                text = str(delta.get("text") or "")
                                if emit and text:
                                    await self._emit(EventType.TEXT_MESSAGE_DELTA, run_id=run_id, challenge_id=challenge_id, solver_id=solver_id, text=text)
                                if text:
                                    yield text
                        elif event == "message_delta":
                            delta_usage = chunk.get("usage") or {}
                            usage["completion_tokens"] = int(delta_usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    rc = delta.get("reasoning_content")
                    if rc and emit:
                        await self._emit(
                            EventType.REASONING_DELTA,
                            run_id=run_id, challenge_id=challenge_id,
                            solver_id=solver_id, text=rc,
                        )
                    cc = delta.get("content")
                    if cc:
                        if emit:
                            await self._emit(
                                EventType.TEXT_MESSAGE_DELTA,
                                run_id=run_id, challenge_id=challenge_id,
                                solver_id=solver_id, text=cc,
                            )
                        yield cc
        except BaseException as exc:
            if usage_call is not None:
                await self._finish_usage(
                    usage_call, outcome=self._usage_outcome(exc), response=None
                )
            raise
        else:
            if usage_call is not None:
                await self.usage_writer.finish(
                    usage_call,
                    call_outcome="succeeded",
                    usage_status="measured" if usage else "unknown",
                    usage=usage,
                )
            elif record_cost:
                await self._record_cost(
                    body["model"], usage, run_id, challenge_id, solver_id
                )
