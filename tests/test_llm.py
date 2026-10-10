"""LLM client unit tests with mocked transport (no key, deterministic).

Covers the reasoning-model handling and streaming tool-call reassembly that the
meta-executor depends on. A separate live smoke test (test_llm_live.py) hits the
real endpoint when DSWARM_DEEPSEEK_API_KEY is set.
"""

import asyncio
import json

import httpx
import pytest
from pathlib import Path

from dswarm.core.cost import CostController
from dswarm.core.event_bus import EventBus
from dswarm.core.events import EventType
from dswarm.core.usage_journal import UsageJournal
from dswarm.core.llm import LLMClient


def _sse(chunks: list[dict]) -> bytes:
    body = ""
    for c in chunks:
        body += f"data: {json.dumps(c)}\n\n"
    body += "data: [DONE]\n\n"
    return body.encode()


def _journal_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

def _client_with(handler, **kw) -> LLMClient:
    transport = httpx.MockTransport(handler)
    c = LLMClient(api_key="test", **kw)
    c._client = httpx.AsyncClient(transport=transport, trust_env=False)
    return c


async def test_reasoning_effort_is_sent_to_openai_compatible_provider() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    async with _client_with(handler, reasoning_effort="high") as client:
        response = await client.chat(
            model="gpt-5.6-terra",
            messages=[{"role": "user", "content": "ping"}],
            stream=False,
        )
    assert response.content == "OK"
    assert seen["reasoning_effort"] == "high"

async def test_reasoning_effort_none_is_omitted() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
        })

    async with _client_with(handler) as client:
        await client.chat(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "ping"}],
            stream=False,
        )
    assert "reasoning_effort" not in seen
async def test_nonstreaming_splits_reasoning_and_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "the answer is 4",
                            "reasoning_content": "2+2 is 4",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8},
                "model": "deepseek-v4-pro",
            },
        )

    async with _client_with(handler) as c:
        r = await c.chat(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": "2+2?"}],
            stream=False,
        )
    assert r.content == "the answer is 4"
    assert r.reasoning == "2+2 is 4"
    assert r.input_tokens == 10 and r.output_tokens == 8
    assert not r.has_tool_calls


async def test_nonstreaming_tool_calls_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "run_python",
                                        "arguments": '{"code": "print(2+2)"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 12},
            },
        )

    async with _client_with(handler) as c:
        r = await c.chat(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": "compute"}],
            tools=[{"type": "function", "function": {"name": "run_python", "parameters": {}}}],
            stream=False,
        )
    assert r.has_tool_calls
    tc = r.tool_calls[0]
    assert tc.name == "run_python"
    assert tc.parsed_args() == {"code": "print(2+2)"}


async def test_streaming_reassembles_fragmented_tool_call_and_emits() -> None:
    # tool call arguments fragmented across deltas, as the real API does
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "reasoning_content": "I should "}}]},
        {"choices": [{"delta": {"reasoning_content": "call the tool"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "run_python", "arguments": "{\"code\""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ": \"print(1)\"}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 7, "completion_tokens": 20}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(chunks), headers={"content-type": "text/event-stream"})

    bus = EventBus()
    cost = CostController()
    reasoning_events = []

    async def consume() -> None:
        async for e in bus.subscribe():
            if e.event_type is EventType.REASONING_DELTA:
                reasoning_events.append(e.payload["text"])
            if e.event_type is EventType.REASONING_DELTA and "call the tool" in e.payload["text"]:
                return

    async with _client_with(handler, bus=bus, cost=cost) as c:
        t = asyncio.create_task(consume())
        await asyncio.sleep(0.02)
        r = await c.chat(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": "go"}],
            tools=[{"type": "function", "function": {"name": "run_python", "parameters": {}}}],
            stream=True,
            run_id="r1",
            solver_id="s1",
        )
        await asyncio.wait_for(t, timeout=5)

    assert r.has_tool_calls
    tc = r.tool_calls[0]
    assert tc.name == "run_python"
    assert tc.parsed_args() == {"code": "print(1)"}  # reassembled correctly
    assert r.finish_reason == "tool_calls"
    assert "".join(reasoning_events) == "I should call the tool"
    # cost recorded
    assert cost.global_usd() > 0


async def test_streaming_emits_content_and_reasoning_separately() -> None:
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "thinking..."}}]},
        {"choices": [{"delta": {"content": "Hello"}}]},
        {"choices": [{"delta": {"content": " world"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 4}},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(chunks), headers={"content-type": "text/event-stream"})

    bus = EventBus()
    text_events, reasoning_events = [], []

    async def consume() -> None:
        seen = 0
        async for e in bus.subscribe():
            if e.event_type is EventType.TEXT_MESSAGE_DELTA:
                text_events.append(e.payload["text"])
            elif e.event_type is EventType.REASONING_DELTA:
                reasoning_events.append(e.payload["text"])
            seen += 1
            if seen >= 3:
                return

    async with _client_with(handler, bus=bus) as c:
        t = asyncio.create_task(consume())
        await asyncio.sleep(0.02)
        r = await c.chat(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            run_id="r1",
        )
        await asyncio.wait_for(t, timeout=5)

    assert r.content == "Hello world"
    assert r.reasoning == "thinking..."
    assert "".join(text_events) == "Hello world"
    assert reasoning_events == ["thinking..."]


async def test_max_tokens_omitted_when_none() -> None:
    """max_tokens=None must drop the field from the request body entirely (let the
    API use the model's own maximum). The Reason planner relies on this: a small cap
    on a reasoning model is spent on reasoning_content first and truncates the JSON
    answer (run-7349: 0 intents → endless retry_bootstrap)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "{}"},
                               "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                  "model": "deepseek-v4-pro"},
        )

    async with _client_with(handler) as c:
        await c.chat(model="deepseek-v4-pro",
                     messages=[{"role": "user", "content": "x"}],
                     max_tokens=None, stream=False)
    assert "max_tokens" not in captured["body"], \
        "max_tokens=None must omit the cap so reasoning output isn't truncated"


async def test_max_tokens_sent_when_set() -> None:
    """A concrete max_tokens is still passed through (back-compat for capped calls)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"},
                               "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                  "model": "deepseek-v4-flash"},
        )

    async with _client_with(handler) as c:
        await c.chat(model="deepseek-v4-flash",
                     messages=[{"role": "user", "content": "x"}],
                     max_tokens=512, stream=False)
    assert captured["body"].get("max_tokens") == 512

@pytest.mark.asyncio
async def test_llm_usage_writer_records_durable_success_and_canonical_event(tmp_path: Path) -> None:
    from dswarm.core.usage_journal import UsageContext, UsageWriter

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )

    class CheckedBus:
        def __init__(self) -> None:
            self.events = []

        async def emit_checked(self, event):
            self.events.append(event)
            return event

    bus = CheckedBus()
    journal = UsageJournal(tmp_path / "run-1-usage-journal.jsonl")
    writer = UsageWriter(journal, bus=bus)
    context = UsageContext(
        run_id="run-1", challenge_id="challenge-1", worker_instance_id="worker-1",
        solver_id="solver-1", profile_id="pi-web",
        configured_account_id="acct-1", billing_account_id=None,
    )
    async with _client_with(handler, usage_writer=writer, usage_context=context) as client:
        response = await client.chat(
            model="deepseek-v4-pro", messages=[{"role": "user", "content": "hi"}],
            stream=False,
        )

    assert response.content == "ok"
    assert [event.event_type.value for event in bus.events] == ["usage.recorded"]
    payload = bus.events[0].payload
    assert payload["producer"] == "internal"
    assert payload["usage_status"] == "measured"
    assert payload["input_tokens"] == 11
    assert payload["output_tokens"] == 7
    rows = _journal_rows(tmp_path / "run-1-usage-journal.jsonl")
    assert [row["phase"] for row in rows[1:]] == ["started", "finished"]
    assert rows[-1]["provider_call_id"] == payload["provider_call_id"]


@pytest.mark.asyncio
async def test_llm_usage_writer_records_provider_error_before_reraising(tmp_path: Path) -> None:
    from dswarm.core.usage_journal import UsageContext, UsageWriter

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"message": "balance depleted"}})

    class CheckedBus:
        def __init__(self) -> None:
            self.events = []

        async def emit_checked(self, event):
            self.events.append(event)
            return event

    bus = CheckedBus()
    journal = UsageJournal(tmp_path / "run-1-usage-journal.jsonl")
    writer = UsageWriter(
        journal,
        bus=bus,
        context=UsageContext(
            run_id="run-1", challenge_id="challenge-1", worker_instance_id="worker-1",
            solver_id="solver-1", profile_id="pi-web",
            configured_account_id="acct-1", billing_account_id=None,
        ),
    )
    async with _client_with(handler, usage_writer=writer) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.chat(
                model="deepseek-v4-pro", messages=[{"role": "user", "content": "hi"}],
                stream=False,
            )

    assert len(bus.events) == 1
    payload = bus.events[0].payload
    assert payload["call_outcome"] == "provider_error"
    assert payload["usage_status"] == "unknown"
    assert payload["input_tokens"] is None
    rows = _journal_rows(tmp_path / "run-1-usage-journal.jsonl")
    assert [row["phase"] for row in rows[1:]] == ["started", "finished"]


@pytest.mark.asyncio
async def test_llm_usage_writer_records_timeout_terminal(tmp_path: Path) -> None:
    from dswarm.core.usage_journal import UsageContext, UsageWriter

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream stalled", request=request)

    class CheckedBus:
        def __init__(self) -> None:
            self.events = []

        async def emit_checked(self, event):
            self.events.append(event)
            return event

    bus = CheckedBus()
    writer = UsageWriter(
        UsageJournal(tmp_path / "run-timeout-usage-journal.jsonl"),
        bus=bus,
        context=UsageContext(run_id="run-timeout", solver_id="titler"),
    )
    async with _client_with(handler, usage_writer=writer) as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.chat(
                model="deepseek-v4-flash",
                messages=[{"role": "user", "content": "title this"}],
                stream=False,
            )

    assert len(bus.events) == 1
    payload = bus.events[0].payload
    assert payload["call_outcome"] == "timeout"
    assert payload["usage_status"] == "unknown"
    rows = _journal_rows(tmp_path / "run-timeout-usage-journal.jsonl")
    assert [row["phase"] for row in rows[1:]] == ["started", "finished"]

@pytest.mark.asyncio
async def test_iter_chat_deltas_usage_writer_finishes_terminal_record(tmp_path: Path) -> None:
    from dswarm.core.usage_journal import UsageContext, UsageWriter

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse([
                {"choices": [{"delta": {"content": "hello"}}]},
                {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
            ]),
        )

    class CheckedBus:
        def __init__(self) -> None:
            self.events = []

        async def emit_checked(self, event):
            self.events.append(event)
            return event

    bus = CheckedBus()
    writer = UsageWriter(
        UsageJournal(tmp_path / "run-1-usage-journal.jsonl"),
        bus=bus,
        context=UsageContext(run_id="run-1", solver_id="btw-1"),
    )
    chunks = []
    async with _client_with(handler, usage_writer=writer) as client:
        async for chunk in client.iter_chat_deltas(
            model="deepseek-v4-flash", messages=[{"role": "user", "content": "hi"}],
            record_cost=True,
        ):
            chunks.append(chunk)
    assert chunks == ["hello"]
    assert len(bus.events) == 1
    assert bus.events[0].payload["usage_status"] == "measured"

@pytest.mark.asyncio
async def test_streaming_usage_writer_does_not_double_record_legacy_cost(tmp_path: Path) -> None:
    from dswarm.core.usage_journal import UsageContext, UsageWriter

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse([
                {"choices": [{"delta": {"content": "hello"}}]},
                {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
            ]),
        )

    class CheckedBus:
        async def emit_checked(self, event):
            return event

    class CountingCost:
        def __init__(self):
            self.calls = []

        async def record(self, **kwargs):
            self.calls.append(kwargs)

    cost = CountingCost()
    writer = UsageWriter(
        UsageJournal(tmp_path / "run-1-usage-journal.jsonl"),
        bus=CheckedBus(),
        context=UsageContext(run_id="run-1", solver_id="reason"),
    )
    async with _client_with(handler, usage_writer=writer, cost=cost) as client:
        response = await client.chat(
            model="deepseek-v4-flash", messages=[{"role": "user", "content": "hi"}],
            stream=True, run_id="run-1", challenge_id="ch-1", solver_id="reason",
        )

    assert response.content == "hello"
    assert cost.calls == []


@pytest.mark.asyncio
async def test_anthropic_messages_protocol_translates_request_and_response() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "inspect first"},
                {"type": "text", "text": "answer"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 8},
        })

    async with _client_with(handler, wire_api="anthropic") as c:
        response = await c.chat(
            model="claude-sonnet-4-5",
            messages=[
                {"role": "system", "content": "Be precise."},
                {"role": "user", "content": "hello"},
            ],
            stream=False,
        )

    assert captured["path"] == "/v1/messages"
    headers = captured["headers"]
    assert headers["x-api-key"] == "test"
    assert headers["anthropic-version"] == "2023-06-01"
    body = captured["body"]
    assert body["system"][0]["text"] == "Be precise."
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert response.content == "answer"
    assert response.reasoning == "inspect first"
    assert response.input_tokens == 12 and response.output_tokens == 8


@pytest.mark.asyncio
async def test_anthropic_streaming_reassembles_thinking_text_and_tool_use() -> None:
    chunks = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 9}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "consider "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "this"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tool_1", "name": "run_python", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"code"'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ': "print(1)"}'}},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "done"}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 15}},
        {"type": "message_stop"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        return httpx.Response(200, content=_sse(chunks), headers={"content-type": "text/event-stream"})

    async with _client_with(handler, wire_api="anthropic") as c:
        response = await c.chat(
            model="claude-sonnet-4-5",
            messages=[{"role": "user", "content": "compute"}],
            tools=[{"type": "function", "function": {"name": "run_python", "description": "execute", "parameters": {"type": "object"}}}],
            stream=True,
        )

    assert response.reasoning == "consider this"
    assert response.content == "done"
    assert response.finish_reason == "tool_calls"
    assert response.input_tokens == 9 and response.output_tokens == 15
    assert response.tool_calls[0].id == "tool_1"
    assert response.tool_calls[0].parsed_args() == {"code": "print(1)"}


def test_default_provider_templates_include_requested_official_and_relay_entries():
    from dswarm.solver.llm_providers import DEFAULT_PROVIDER_TEMPLATES

    providers = {str(item["id"]): item for item in DEFAULT_PROVIDER_TEMPLATES}
    assert {"deepseek", "openai", "anthropic", "zhipu", "moonshot", "xai", "custom-openai"} <= providers.keys()
    assert providers["anthropic"]["wire_api"] == "anthropic"
    assert providers["xai"]["base_url"] == "https://api.x.ai/v1"
    assert providers["custom-openai"]["base_url"] == ""
