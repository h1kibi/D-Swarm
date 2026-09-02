"""Auto-title a solve conversation from the operator's opening prompt.

ChatGPT/Claude-style: the rail row starts as a "new conversation" placeholder,
then a short title quietly replaces it. We ask deepseek-v4-flash (cheap, fast)
for a 3-6 word title IN THE PROMPT'S OWN LANGUAGE. If the model is slow, errors,
or returns junk, we fall back to the first few words of the prompt — so the rail
ALWAYS shows something readable, never a bare run id.

The call is fire-and-forget from the start endpoint: it never blocks swarm
launch. On success it emits RUN_TITLED on the run's bus, which the rail picks up
(both via SSE and the /api/runs poll).
"""

from __future__ import annotations

import re
from typing import Optional

from dswarm.core.events import Event, EventType
from dswarm.core.event_bus import EventBus
from dswarm.core.llm import LLMClient
from dswarm.core.usage_journal import UsageContext, UsageWriter

TITLE_MODEL = "deepseek-v4-flash"

_SYSTEM = (
    "You name chat conversations. Given the user's opening message, reply with a "
    "SHORT title of 3 to 6 words that captures its topic. Use the SAME LANGUAGE as "
    "the message. No quotes, no punctuation at the end, no prefixes like 'Title:'. "
    "Do not visit URLs, follow links, or browse the web; simply extract the topic "
    "from the text. Reply with the title only."
)

# ── rule-based naming (operator spec 2026-09-01): {category}-{identifier} ───
# The identifier is whatever independently pins down THE challenge, tried in
# order: stated 题目名 (structured or `题目：X` in the prompt) → URL host[:port]
# → attachment filename → the LLM 题目名. Ports are kept for non-default
# endpoints because arena instances differ ONLY by port (web-node4…:22966 vs
# :23456 are different challenges). Deterministic paths skip the LLM entirely.
# A uniqueness guard in RunManager suffixes `-2`/`-3` so two runs can never
# render as the same row label.
_URL_HOST_RE = re.compile(r"https?://([^\s\"'<>/:]+)(?::(\d+))?", re.IGNORECASE)
_DEFAULT_PORTS = {"80", "443"}
_STATED_NAME_RE = re.compile(
    r"(?:题目名|题目|挑战名|challenge(?:\s*name)?)\s*(?:[:：是]|叫|名为)\s*[:：]?\s*"
    r"([^\s，。；,.;！!？?\"'“”‘’：:]{2,24})",
    re.IGNORECASE,
)


def url_host(prompt: str) -> str:
    """The first URL's hostname (www. stripped), or ""."""
    m = _URL_HOST_RE.search(prompt or "")
    if not m:
        return ""
    host = m.group(1).lower()
    return host[4:] if host.startswith("www.") else host


def url_endpoint(prompt: str) -> str:
    """`host` or `host:port` for the first URL; the port is kept unless it is
    the implicit 80/443 — same host on two ports means two arena instances,
    and the rail label must tell them apart."""
    m = _URL_HOST_RE.search(prompt or "")
    if not m:
        return ""
    host = m.group(1).lower()
    host = host[4:] if host.startswith("www.") else host
    port = m.group(2)
    if port and port not in _DEFAULT_PORTS:
        return f"{host}:{port}"
    return host


def stated_challenge_name(prompt: str) -> str:
    """A 题目名 spelled out in the prompt (`题目：flexcheck` / `题目叫 ret2libc` /
    `challenge: xor`), or "". Capped at 24 chars so a `题目：` that opens prose
    degrades to a short head instead of swallowing the whole sentence."""
    m = _STATED_NAME_RE.search(prompt or "")
    if not m:
        return ""
    return m.group(1).strip("。-—_·").strip()


def attachment_stem(names: list[str] | None) -> str:
    """The first attachment's filename stem, or ""."""
    for n in names or []:
        stem = re.sub(r"[^\w.\-一-龥]+", "-", str(n or "").strip()).strip("-")
        if stem:
            return stem[:40]
    return ""


def compose_title(category: str, name_part: str) -> str:
    """`{category}-{name}` with double-prefix and length guards."""
    name_part = (name_part or "").strip().strip("-")
    cat = (category or "").strip().lower()
    if not name_part:
        return ""
    if not cat or name_part.lower().startswith(f"{cat}-"):
        return name_part[:64]
    return f"{cat}-{name_part}"[:64]


def rule_name_part(
    prompt: str, category: str, attachment_names: list[str] | None = None,
    challenge_name: str | None = None,
) -> tuple[str, bool]:
    """The spec's identifier derivation — `方向-标识`, most-specific first:
    structured 题目名 (operator-supplied at dispatch) → prompt-stated 题目名 →
    URL host[:port] → attachment filename. Everything deterministic applies to
    ANY category; when nothing pins the challenge down we defer to the LLM
    title. Returns (identifier, deterministic)."""
    for candidate in (
        str(challenge_name or "").strip(),
        stated_challenge_name(prompt),
        url_endpoint(prompt),
        attachment_stem(attachment_names),
    ):
        if candidate:
            return candidate, True
    return "", False


_REFUSAL_STARTS = (
    "抱歉", "对不起", "我无法", "不能访问", "无法访问",
    "I cannot", "I can't", "I'm sorry", "Sorry",
)


def fallback_title(prompt: str, max_words: int = 6, max_chars: int = 48) -> str:
    """First few words of the prompt — the always-available degraded title.

    Collapses whitespace, strips a leading flag-format/url noise, and caps length
    so the rail row stays one line. CJK text has no spaces, so for those we cap by
    characters instead of words.
    """
    text = re.sub(r"\s+", " ", (prompt or "").strip())
    if not text:
        return ""
    # CJK-ish (no spaces): just clip by characters.
    if " " not in text:
        return text[:max_chars]
    title = " ".join(text.split(" ")[:max_words])
    return title[:max_chars]


def _clean(raw: str, prompt: str) -> str:
    """Sanitize the model's answer; fall back to the prompt head if it's unusable."""
    title = (raw or "").strip().strip("\"'“”‘’").strip()
    # one line only; drop a trailing period the model sometimes adds
    title = title.splitlines()[0].strip().rstrip(".。") if title else ""
    # reject empty or absurdly long answers (model ignored the instruction)
    if not title or len(title) > 80 or any(title.startswith(r) for r in _REFUSAL_STARTS):
        return fallback_title(prompt)
    return title


async def generate_title(
    prompt: str,
    *,
    llm: Optional[LLMClient] = None,
    bus: Optional[EventBus] = None,
    run_id: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    usage_writer: Optional[UsageWriter] = None,
    usage_context: Optional[UsageContext] = None,
    category: str = "",
    attachment_names: Optional[list[str]] = None,
    challenge_name: Optional[str] = None,
) -> str:
    """Return a short title for `prompt`; emit RUN_TITLED on `bus` if given.

    Naming rule (operator spec): the title is `{category}-{identifier}` where
    the identifier is, most-specific first, the 题目名 (structured or stated in
    the prompt) → URL host[:port] → attachment filename → the LLM 题目名.
    Deterministic paths skip the LLM call entirely.

    Never raises: any LLM failure degrades to `fallback_title`. The caller runs
    this as a detached task, so swallowing errors here keeps a flaky title API
    from surfacing as an unhandled-task warning.

    `base_url` overrides the titler endpoint (DESIGN §2.2 補强A) — empty/None =
    default DeepSeek. The API key is NOT passed here; it stays in .env. Only used
    when `llm` is not injected (we own the client lifecycle).
    """
    title = fallback_title(prompt)
    name_part, deterministic = rule_name_part(
        prompt, category, attachment_names, challenge_name,
    )
    if deterministic:
        # rule hit: compose and skip the LLM entirely
        title = compose_title(category, name_part)
        if bus is not None and run_id is not None and title:
            await bus.emit(
                Event(
                    event_type=EventType.RUN_TITLED,
                    run_id=run_id,
                    payload={"title": title},
                )
            )
        return title
    owns_llm = llm is None
    try:
        if llm is not None:
            client = llm
        elif (base_url or "").strip():
            client = LLMClient(
                base_url=base_url,
                usage_writer=usage_writer,
                usage_context=usage_context,
            )
        else:
            client = LLMClient(
                usage_writer=usage_writer,
                usage_context=usage_context,
            )
        try:
            resp = await client.chat(
                model=model or TITLE_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt[:2000]},
                ],
                temperature=0.3,
                max_tokens=2000,  # reasoning model: tokens go to reasoning first
                stream=False,
            )
            title = _clean(resp.content, prompt)
        finally:
            if owns_llm:
                await client.aclose()
    except Exception:
        # keep the fallback title; titling must never break a dispatch
        title = title or fallback_title(prompt)

    title = compose_title(category, title) or title
    if bus is not None and run_id is not None and title:
        await bus.emit(
            Event(
                event_type=EventType.RUN_TITLED,
                run_id=run_id,
                payload={"title": title},
            )
        )
    return title
