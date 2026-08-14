"""OpenAI-compatible endpoint discovery and model probes.

This module is shared by the web settings draft probe and the solver health check.
It never returns or logs credential material.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

WireAPI = str
HttpRequest = Callable[..., tuple[int, Any, str]]

_WIRE_ALIASES = {
    "": "auto", "auto": "auto", "openai": "auto", "chat": "openai-chat",
    "openai-chat": "openai-chat", "openai-completions": "openai-chat",
    "responses": "openai-responses", "openai-responses": "openai-responses",
}
_AUTH_MODES = {"bearer", "x-api-key", "custom"}
_KNOWN_SUFFIXES = ("/chat/completions", "/responses", "/models")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_PROTOCOL_FALLBACK_STATUSES = {404, 405, 415, 501}


def normalize_wire_api(value: Any) -> WireAPI:
    return _WIRE_ALIASES.get(str(value or "").strip().lower(), "auto")


def normalize_base_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Base URL 不能为空")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Base URL 必须是有效的 http:// 或 https:// 地址")
    path = parsed.path.rstrip("/")
    lowered = path.lower()
    for suffix in _KNOWN_SUFFIXES:
        if lowered.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def endpoint_url(base_url: Any, path: str) -> str:
    base = normalize_base_url(base_url)
    return f"{base}/{path.lstrip('/')}"


def normalize_auth(body: dict[str, Any]) -> tuple[str, str, str]:
    mode = str(body.get("auth_mode") or "bearer").strip().lower()
    if mode not in _AUTH_MODES:
        mode = "bearer"
    if mode == "bearer":
        return mode, "Authorization", "Bearer"
    if mode == "x-api-key":
        return mode, "x-api-key", ""
    header = str(body.get("auth_header") or "").strip()
    if not header or not _HEADER_NAME.fullmatch(header):
        raise ValueError("自定义认证 Header 名称无效")
    return mode, header, str(body.get("auth_prefix") or "").strip()


def auth_headers(body: dict[str, Any], api_key: str) -> dict[str, str]:
    _, header, prefix = normalize_auth(body)
    value = f"{prefix} {api_key}".strip() if prefix else api_key
    return {header: value}


def _http_json(method: str, url: str, *, headers: dict[str, str],
               body: dict[str, Any] | None = None, timeout: float = 20.0) -> tuple[int, Any, str]:
    with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as client:
        response = client.request(method, url, headers=headers, json=body)
    text = response.text[:4000]
    try:
        payload: Any = response.json()
    except (json.JSONDecodeError, ValueError):
        payload = None
    return response.status_code, payload, text


def parse_models(payload: Any) -> list[dict[str, str]]:
    rows: Any
    if isinstance(payload, dict):
        rows = payload.get("data") if payload.get("data") is not None else payload.get("models")
    else:
        rows = payload
    if not isinstance(rows, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            model_id = name = row.strip()
        elif isinstance(row, dict):
            model_id = str(row.get("id") or row.get("model") or "").strip()
            name = str(row.get("name") or row.get("label") or model_id).strip()
        else:
            continue
        if model_id and model_id not in seen:
            seen.add(model_id)
            out.append({"id": model_id, "name": name or model_id})
    return out


def error_message(status: int | None, text: str = "") -> tuple[str, str]:
    if status in {401, 403}:
        return "authentication", f"服务器已收到请求，但认证失败（{status}）。请检查 API Key 和认证 Header。"
    if status == 404:
        return "protocol", "接口路径不存在（404）。请检查 Base URL 是否包含正确的 API 前缀。"
    if status == 429:
        return "rate_limit", "供应商返回限流（429），请稍后重试。"
    if status is not None and status >= 500:
        return "provider", f"供应商服务异常（{status}）。"
    if status is not None:
        return "protocol", f"供应商返回 HTTP {status}。"
    lower = text.lower()
    if "timed out" in lower or "timeout" in lower:
        return "network", "连接超时，请检查网络和 Base URL。"
    if "certificate" in lower or "ssl" in lower or "tls" in lower:
        return "network", "TLS 连接失败，请检查证书和 Base URL。"
    return "network", "无法连接供应商，请检查网络和 Base URL。"


def protocol_mismatch(status: int, payload: Any, text: str) -> bool:
    if status in _PROTOCOL_FALLBACK_STATUSES:
        return True
    if status not in {400, 422}:
        return False
    raw = json.dumps(payload, ensure_ascii=False) if payload is not None else text
    lower = raw.lower()
    return any(word in lower for word in (
        "messages", "input", "responses", "chat/completions", "unsupported endpoint",
        "unknown endpoint", "not supported",
    ))


def model_request(protocol: WireAPI, model: str) -> tuple[str, dict[str, Any]]:
    if protocol == "openai-responses":
        return "responses", {"model": model, "input": "Reply with OK", "max_output_tokens": 1}
    return "chat/completions", {
        "model": model, "messages": [{"role": "user", "content": "Reply with OK"}],
        "max_tokens": 1, "stream": False,
    }


def probe_endpoint(draft: dict[str, Any], *, api_key: str, validate_model: bool = False,
                   request_json: HttpRequest = _http_json) -> dict[str, Any]:
    """Probe an endpoint using a draft profile. The key is never in the result."""
    try:
        base_url = normalize_base_url(draft.get("base_url"))
        headers = {"Accept": "application/json", **auth_headers(draft, api_key)}
    except ValueError as exc:
        return {"ok": False, "detail": str(exc), "error_layer": "configuration", "models": []}
    if not api_key.strip():
        return {"ok": False, "base_url": base_url, "detail": "API Key 未配置。",
                "error_layer": "authentication", "models": []}
    result: dict[str, Any] = {
        "ok": False, "base_url": base_url,
        "connectivity": {"ok": False, "status": None},
        "authentication": {"ok": False, "status": None},
        "model_discovery": {"ok": False, "items": []},
        "model_probe": {"attempted": False, "ok": False},
        "detected_wire_api": None, "models": [],
    }
    try:
        status, payload, text = request_json("GET", endpoint_url(base_url, "models"),
                                             headers=headers, body=None, timeout=20.0)
    except Exception as exc:  # noqa: BLE001
        layer, detail = error_message(None, str(exc))
        result.update(detail=detail, error_layer=layer)
        return result
    result["connectivity"] = {"ok": True, "status": status}
    auth_ok = status not in {401, 403}
    result["authentication"] = {"ok": auth_ok, "status": status}
    discovered = parse_models(payload) if 200 <= status < 300 else []
    discovery_ok = 200 <= status < 300
    result["models"] = discovered
    result["model_discovery"] = {"ok": discovery_ok, "status": status, "items": discovered}
    if not auth_ok:
        layer, detail = error_message(status, text)
        result.update(detail=detail, error_layer=layer)
        return result
    model = str(draft.get("model") or "").strip()
    if not validate_model:
        if discovery_ok:
            result.update(ok=True, detail=f"连接成功，获取到 {len(discovered)} 个模型。")
        else:
            layer, detail = error_message(status, text)
            result.update(detail=detail, error_layer=layer)
        return result
    result["model_probe"] = {"attempted": True, "ok": False, "model": model}
    if not model:
        result.update(detail="请先选择或手动填写模型 ID。", error_layer="model")
        return result
    configured = normalize_wire_api(draft.get("wire_api"))
    protocols = [configured] if configured != "auto" else ["openai-chat", "openai-responses"]
    last: tuple[int | None, Any, str] = (None, None, "")
    for index, protocol in enumerate(protocols):
        path, body = model_request(protocol, model)
        try:
            status2, payload2, text2 = request_json(
                "POST", endpoint_url(base_url, path),
                headers={**headers, "Content-Type": "application/json"}, body=body, timeout=30.0,
            )
        except Exception as exc:  # noqa: BLE001
            layer, detail = error_message(None, str(exc))
            result.update(detail=detail, error_layer=layer)
            return result
        last = (status2, payload2, text2)
        if 200 <= status2 < 300:
            result["model_probe"] = {"attempted": True, "ok": True, "status": status2,
                                      "model": model, "protocol": protocol}
            result.update(ok=True, detail=f"模型验证成功（{protocol}）。", detected_wire_api=protocol)
            return result
        if index == 0 and configured == "auto" and protocol_mismatch(status2, payload2, text2):
            continue
        break
    status2, _, text2 = last
    layer, detail = error_message(status2, text2)
    result["model_probe"].update(status=status2)
    result.update(detail=detail, error_layer=layer)
    return result
