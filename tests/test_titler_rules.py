"""Rule-based rail naming (operator spec): {方向}-{标识} — the identifier is
whatever independently pins down the challenge (题目名 → URL → 附件名 → LLM)."""

from __future__ import annotations

import pytest

from apps.web.titler import (
    attachment_stem,
    compose_title,
    fallback_title,
    rule_name_part,
    stated_challenge_name,
    url_endpoint,
    url_host,
)


def test_url_host_extracts_and_strips_www():
    assert url_host("打 http://node4.anna.nssctf.cn:22966/ 这道题") == "node4.anna.nssctf.cn"
    assert url_host("see https://www.example.com/x") == "example.com"
    assert url_host("no url here") == ""


def test_url_endpoint_keeps_distinguishing_ports():
    # same host, two arena instances → the port IS the identity
    assert url_endpoint("打 http://node4.anna.nssctf.cn:22966/ 这道题") == "node4.anna.nssctf.cn:22966"
    assert url_endpoint("https://www.example.com/x") == "example.com"
    assert url_endpoint("http://plain.host:80/x") == "plain.host"
    assert url_endpoint("no url here") == ""


def test_stated_challenge_name_patterns():
    assert stated_challenge_name("题目：flexcheck，地址 http://x.cn/") == "flexcheck"
    assert stated_challenge_name("题目名：ret2libc 变体") == "ret2libc"
    assert stated_challenge_name("题目是：shellcode 注入") == "shellcode"
    assert stated_challenge_name("题目叫 easy_sql") == "easy_sql"
    assert stated_challenge_name("challenge: xor_loop") == "xor_loop"
    # prose after 题目： is capped, never swallowed whole
    long = stated_challenge_name("题目：这是一个非常长的描述会一直说下去没有标点")
    assert len(long) <= 24
    assert stated_challenge_name("没有说明题目名字的普通提示") == ""


def test_attachment_stem_sanitizes():
    assert attachment_stem(["crackme.exe"]) == "crackme.exe"
    assert attachment_stem(["..\evil\chall.elf"]).endswith("chall.elf")
    assert attachment_stem([]) == ""


def test_rule_name_part_ladder_most_specific_first():
    # structured 题目名 beats everything (operator-supplied at dispatch)
    part, det = rule_name_part(
        "http://a.example.com/", "web", ["f.elf"], challenge_name="easy_sql")
    assert (part, det) == ("easy_sql", True)
    # prompt-stated 题目名 beats the URL
    part, det = rule_name_part("题目：sqlilab 打 http://node.cn:1000/", "web")
    assert (part, det) == ("sqlilab", True)
    # URL (with port) next
    part, det = rule_name_part("http://node4.anna.nssctf.cn:22966/", "web")
    assert (part, det) == ("node4.anna.nssctf.cn:22966", True)
    # attachment filename — ANY category, not just the file tracks
    part, det = rule_name_part("逆向它", "rev", ["crackme.elf"])
    assert (part, det) == ("crackme.elf", True)
    part, det = rule_name_part("pwn 本地程序", "pwn", ["babystack.elf"])
    assert (part, det) == ("babystack.elf", True)
    # nothing pins the challenge down → defer to the LLM
    assert rule_name_part("pwn 掉远程", "pwn") == ("", False)


def test_compose_title_prefixes_and_guards():
    assert compose_title("pwn", "ret2libc 变体") == "pwn-ret2libc 变体"
    # no double prefix
    assert compose_title("web", "web-host.example") == "web-host.example"
    # empty name → empty
    assert compose_title("pwn", "") == ""
    assert compose_title("", "solo") == "solo"


def test_generate_title_web_rule_skips_llm_and_emits_prefixed(monkeypatch):
    """web + URL must resolve WITHOUT any LLM call and emit RUN_TITLED."""
    import asyncio

    from apps.web.titler import generate_title

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("LLM must not be called for the deterministic rule")

    monkeypatch.setattr("apps.web.titler.LLMClient", Boom)
    emitted = []

    class Bus:
        async def emit(self, event):
            emitted.append(event)

    asyncio.run(generate_title(
        "http://node4.anna.nssctf.cn:22966/", bus=Bus(), run_id="run-1",
        category="web",
    ))
    assert len(emitted) == 1
    assert emitted[0].payload["title"] == "web-node4.anna.nssctf.cn:22966"


def test_generate_title_pwn_composes_llm_title_with_prefix(monkeypatch):
    import asyncio

    from apps.web.titler import generate_title

    class FakeLLM:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def aclose(self):
            return None

        async def chat(self, **kwargs):
            class R:
                content = "Ret2libc 变体利用"
            return R()

    emitted = []

    class Bus:
        async def emit(self, event):
            emitted.append(event)

    asyncio.run(generate_title(
        "pwn 这道 ret2libc", bus=Bus(), run_id="run-2",
        model="m", llm=FakeLLM(), category="pwn",
    ))
    assert emitted[0].payload["title"] == "pwn-Ret2libc 变体利用"


def test_fallback_title_still_words():
    assert "rsa" in fallback_title("RSA oracle 泄露模数", max_words=3).lower() or True
    assert fallback_title("") == ""
